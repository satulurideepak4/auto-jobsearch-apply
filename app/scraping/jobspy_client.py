from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

# LinkedIn excluded — bot detection risk; Naukri/Instahyre/Wellfound handled separately
_SITE_NAMES = ["indeed", "glassdoor", "zip_recruiter"]


class JobSpyClient:
    """Thin wrapper around the jobspy library for scraping job postings."""

    def scrape(
        self,
        search_term: str,
        location: str,
        site_names: list[str],
        results_wanted: int,
        country_indeed: str = "USA",
    ) -> list[dict]:
        """Scrape jobs and return a normalised list of dicts.

        Args:
            search_term: Job title / keywords to search for.
            location: Geographic location string.
            site_names: Job board site names (e.g. ["indeed", "linkedin"]).
            results_wanted: Maximum number of results to request.
            country_indeed: Country string passed to jobspy for Indeed searches.

        Returns:
            List of normalised job dicts.
        """
        import jobspy  # lazy import

        try:
            df = jobspy.scrape_jobs(
                site_name=site_names,
                search_term=search_term,
                location=location,
                results_wanted=results_wanted,
                country_indeed=country_indeed,
            )
        except Exception as exc:
            logger.error(
                "jobspy.scrape_jobs failed for search_term=%r location=%r: %s",
                search_term,
                location,
                exc,
            )
            return []

        if df is None or df.empty:
            logger.info(
                "No results returned for search_term=%r location=%r",
                search_term,
                location,
            )
            return []

        jobs: list[dict] = []
        for _, row in df.iterrows():
            row_dict = row.to_dict()

            # Determine source from the dataframe column if present
            source = str(row_dict.get("site", site_names[0] if site_names else "unknown"))

            # Normalise salary fields (jobspy may expose them with various names)
            salary_min = row_dict.get("min_amount") or row_dict.get("salary_min")
            salary_max = row_dict.get("max_amount") or row_dict.get("salary_max")
            currency = row_dict.get("currency", None)

            # date_posted may be a Timestamp; convert to str for JSON safety
            date_posted = row_dict.get("date_posted")
            if hasattr(date_posted, "isoformat"):
                date_posted = date_posted.isoformat()

            try:
                raw_data = json.dumps(
                    {k: (v if not hasattr(v, "isoformat") else v.isoformat()) for k, v in row_dict.items()},
                    default=str,
                )
            except Exception:
                raw_data = str(row_dict)

            jobs.append(
                {
                    "id": None,
                    "title": str(row_dict.get("title") or ""),
                    "company": str(row_dict.get("company") or ""),
                    "location": str(row_dict.get("location") or location),
                    "description": str(row_dict.get("description") or ""),
                    "job_url": str(row_dict.get("job_url") or ""),
                    "salary_min": float(salary_min) if salary_min is not None else None,
                    "salary_max": float(salary_max) if salary_max is not None else None,
                    "currency": str(currency) if currency else None,
                    "date_posted": date_posted,
                    "source": source,
                    "raw_data": raw_data,
                }
            )

        logger.info(
            "Scraped %d jobs for search_term=%r location=%r",
            len(jobs),
            search_term,
            location,
        )
        return jobs


def _deduplicate(jobs: list[dict]) -> list[dict]:
    """Remove duplicate jobs by (company, title, location) — case-insensitive."""
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for job in jobs:
        key = (
            job.get("company", "").lower(),
            job.get("title", "").lower(),
            job.get("location", "").lower(),
        )
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique


def scrape_india_profiles(settings: "Settings", roles: list[str] | None = None) -> list[dict]:
    """Scrape Indian job market for each role across all configured locations.

    Args:
        settings: Application settings instance.
        roles: List of search terms derived from resume. Falls back to
               INDIA_SEARCH_TERMS env var if not provided.

    Returns:
        Deduplicated list of job dicts.
    """
    client = JobSpyClient()
    locations = [loc.strip() for loc in settings.INDIA_LOCATIONS.split(",") if loc.strip()]
    search_terms = roles if roles else [t.strip() for t in settings.INDIA_SEARCH_TERMS.split(",") if t.strip()]
    all_jobs: list[dict] = []

    for term in search_terms:
        for location in locations:
            jobs = client.scrape(
                search_term=term,
                location=location,
                site_names=_SITE_NAMES,
                results_wanted=settings.INDIA_RESULTS_WANTED,
                country_indeed="India",
            )
            all_jobs.extend(jobs)

    deduped = _deduplicate(all_jobs)
    logger.info(
        "India scrape: %d terms × %d locations → %d raw → %d deduplicated",
        len(search_terms), len(locations), len(all_jobs), len(deduped),
    )
    return deduped


def scrape_intl_profiles(settings: "Settings", roles: list[str] | None = None) -> list[dict]:
    """Scrape international (remote) jobs for each role if INTL_ENABLED.

    Args:
        settings: Application settings instance.
        roles: List of search terms derived from resume. Falls back to
               INTL_SEARCH_TERMS env var if not provided.

    Returns:
        Deduplicated list of job dicts, or empty list if INTL_ENABLED is False.
    """
    if not settings.INTL_ENABLED:
        logger.info("International scraping is disabled (INTL_ENABLED=false).")
        return []

    client = JobSpyClient()
    search_terms = roles if roles else [t.strip() for t in settings.INTL_SEARCH_TERMS.split(",") if t.strip()]
    all_jobs: list[dict] = []

    for term in search_terms:
        jobs = client.scrape(
            search_term=term,
            location="Remote",
            site_names=_SITE_NAMES,
            results_wanted=settings.INTL_RESULTS_WANTED,
            country_indeed="USA",
        )
        all_jobs.extend(jobs)

    deduped = _deduplicate(all_jobs)
    logger.info(
        "Intl scrape: %d terms → %d raw → %d deduplicated",
        len(search_terms), len(all_jobs), len(deduped),
    )
    return deduped


async def scrape_portal_jobs(settings: "Settings", roles: list[str] | None = None) -> list[dict]:
    """Scrape all enabled job boards via Playwright using the driver registry.

    Each driver opens ONE browser session and searches all relevant locations
    inside it — no repeated open/close per location.

    Drivers are auto-discovered from app/scraping/drivers/ and config/job_boards.yaml.
    """
    from app.scraping.drivers import get_registry

    registry = get_registry()
    all_jobs: list[dict] = []

    india_roles = roles if roles else [t.strip() for t in settings.INDIA_SEARCH_TERMS.split(",") if t.strip()]
    intl_roles = roles if roles else [t.strip() for t in settings.INTL_SEARCH_TERMS.split(",") if t.strip()]
    india_locations = [loc.strip() for loc in settings.INDIA_LOCATIONS.split(",") if loc.strip()]
    primary_india = india_locations[0] if india_locations else "India"

    for driver in registry.all_enabled():
        region = getattr(driver, "region", "any")
        try:
            if region == "india":
                # Search each role across all India locations
                for role in india_roles[:4]:  # cap at 4 roles per portal to avoid rate limits
                    jobs = await driver.run_multi_search(role, india_locations, settings.INDIA_RESULTS_WANTED)
                    all_jobs.extend(jobs)
                    logger.info("%s [%s]: %d jobs", driver.name, role, len(jobs))
            elif region == "international":
                if settings.INTL_ENABLED:
                    for role in intl_roles[:4]:
                        jobs = await driver.run_multi_search(role, ["Remote"], settings.INTL_RESULTS_WANTED)
                        all_jobs.extend(jobs)
                        logger.info("%s [%s] Remote: %d jobs", driver.name, role, len(jobs))
            else:
                for role in india_roles[:2]:
                    jobs = await driver.run_multi_search(role, [primary_india], settings.INDIA_RESULTS_WANTED)
                    all_jobs.extend(jobs)
                    logger.info("%s [%s] %s: %d jobs", driver.name, role, primary_india, len(jobs))
        except Exception as exc:
            logger.error("%s scrape failed: %s", driver.name, exc)

    deduped = _deduplicate(all_jobs)
    logger.info("Portal scrape: %d raw → %d after deduplication", len(all_jobs), len(deduped))
    return deduped
