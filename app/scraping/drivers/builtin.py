"""BuiltIn job board driver."""
from __future__ import annotations

import logging
import urllib.parse
from app.scraping.drivers import BaseJobBoardDriver, _apply_stealth, _human_delay, _natural_scroll

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://builtin.com/jobs?search={role}"


class BuiltInDriver(BaseJobBoardDriver):
    name = "builtin"
    home_url = "https://builtin.com"
    login_url = ""
    requires_login = False
    region = "international"

    async def is_logged_in(self, page) -> bool:
        # BuiltIn does not require active logins for general job searches
        return True

    async def search(self, page, role: str, location: str, limit: int = 20) -> list[dict]:
        from app.config import get_settings
        s = get_settings()

        encoded_role = urllib.parse.quote(role)
        url = _SEARCH_URL.format(role=encoded_role)
        logger.info("BuiltIn: searching %s", url)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await _human_delay(page, 2000, 4000)
            await _natural_scroll(page, steps=2)
        except Exception as exc:
            logger.error("BuiltIn: search navigation failed: %s", exc)
            return []

        jobs: list[dict] = []

        # Standard BuiltIn DOM card wrappers
        cards = await page.query_selector_all(
            "[data-id='job-card'], "
            ".job-card, "
            "[class*='job-card'], "
            "div[class*='job-row'], "
            ".card-job"
        )

        if not cards:
            # Broader fallbacks if classes are compiled differently
            cards = await page.query_selector_all(
                "div.job-item, [class*='jobItem'], article[class*='job']"
            )

        logger.info("BuiltIn: found %d raw job cards", len(cards))

        for card in cards:
            if len(jobs) >= limit:
                break
            try:
                # Title
                title_el = await card.query_selector(
                    "h2, h3, .job-title, [class*='title'], [data-id='job-title']"
                )
                title = (await title_el.inner_text()).strip() if title_el else ""

                # Company
                company_el = await card.query_selector(
                    ".company-title, .company-name, [class*='company'], [class*='employer']"
                )
                company = (await company_el.inner_text()).strip() if company_el else ""

                # Link
                link_el = await card.query_selector(
                    "a[href*='/job/'], a[href*='/jobs/']"
                )
                href = await link_el.get_attribute("href") if link_el else None

                if not title or not href:
                    continue

                if not href.startswith("http"):
                    href = f"https://builtin.com{href}"

                if any(j["job_url"] == href for j in jobs):
                    continue

                jobs.append({
                    "title": title,
                    "company": company,
                    "location": "Remote",
                    "job_url": href,
                    "source": "builtin",
                    "id": None,
                    "description": "",
                    "salary_min": None,
                    "salary_max": None,
                    "currency": None,
                    "date_posted": None,
                    "raw_data": "",
                })
            except Exception as e:
                logger.debug("Skipping parsing single BuiltIn job card: %s", e)

        logger.info("BuiltIn: successfully extracted %d jobs", len(jobs))
        return jobs

    async def apply_to_job(self, page, job: dict) -> bool:
        """Applying to BuiltIn jobs routes back to direct ATS forms or handoffs."""
        logger.info("BuiltIn: direct apply routing to external ATS fallback.")
        return False


# Export instantiated driver for automatic dynamic bootstrapping by JobBoardRegistry
DRIVER = BuiltInDriver()
