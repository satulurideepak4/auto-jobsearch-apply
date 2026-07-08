"""Wellfound (formerly AngelList Talent) portal scraper and auto-applier.

Flow:
  1. Login to wellfound.com
  2. Search startups/jobs by role + skills
  3. Collect matching job listings
  4. For each job: click Apply, fill intro + interest statement, submit

Wellfound apply flow:
  - Click "Apply" on a role listing → opens an application modal or redirects
  - Ask: "Why are you interested in {company}?" (short essay, ~200 chars)
  - Ask: optional questions per startup
  - Some startups use external ATS links (greenhouse, lever etc.) — these are
    collected as job leads and handled by the main FormFiller separately
"""
from __future__ import annotations

import logging

from app.scraping.portal_scrapers import BasePortalScraper

logger = logging.getLogger(__name__)

_LOGIN_URL = "https://wellfound.com/login"
_SEARCH_URL = "https://wellfound.com/role/r/{role_slug}?location={location}"
_JOBS_URL = "https://wellfound.com/jobs?q={role}&location_slugs={location_slug}"


class WellfoundScraper(BasePortalScraper):
    SESSION_NAME = "wellfound"
    HOME_URL = "https://wellfound.com"

    async def is_logged_in(self, page) -> bool:
        from app.config import get_settings
        s = get_settings()
        try:
            await page.goto("https://wellfound.com", wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await page.wait_for_timeout(s.BROWSER_SHORT_WAIT_MS)
            profile = await page.query_selector(
                "a[href*='/me'], a[href*='/profile'], [class*='user-avatar'], "
                "[data-test='user-avatar'], img[alt*='avatar']"
            )
            login_btn = await page.query_selector(
                "a:text-is('Log In'), a:text-is('Sign In'), button:text-is('Log In')"
            )
            return profile is not None and login_btn is None
        except Exception:
            return False

    async def login(self, page, email: str, password: str) -> bool:
        from app.config import get_settings
        s = get_settings()
        logger.info("Wellfound: logging in as %s", email)
        try:
            await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await page.wait_for_timeout(1_500)

            email_el = await page.query_selector(
                "input[type='email'], input[name='email'], input[placeholder*='Email']"
            )
            pw_el = await page.query_selector(
                "input[type='password'], input[name='password']"
            )
            if not email_el or not pw_el:
                logger.error("Wellfound: login fields not found")
                return False

            await email_el.fill(email)
            await pw_el.fill(password)

            submit = await page.query_selector(
                "button[type='submit'], input[type='submit'], button:text-is('Log in')"
            )
            if submit:
                await submit.click()
            else:
                await pw_el.press("Enter")

            await page.wait_for_timeout(5_000)
            return "login" not in page.url.lower() and "signin" not in page.url.lower()
        except Exception as exc:
            logger.error("Wellfound login error: %s", exc)
            return False

    async def search(self, page, role: str, location: str, results: int = 30) -> list[dict]:
        from urllib.parse import quote
        from app.config import get_settings
        s = get_settings()
        role_slug = role.lower().replace(" ", "-")
        loc_slug = location.lower().replace(",", "").replace(" ", "-")
        search_url = _JOBS_URL.format(role=quote(role, safe=""), location_slug=quote(loc_slug, safe=""))
        logger.info("Wellfound: searching %s", search_url)

        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await page.wait_for_timeout(s.BROWSER_LONG_WAIT_MS)
        except Exception as exc:
            logger.error("Wellfound: search navigation failed: %s", exc)
            return []

        jobs: list[dict] = []
        scroll_attempts = 0

        while len(jobs) < results and scroll_attempts < s.WELLFOUND_SCROLL_LIMIT:
            cards = await page.query_selector_all(
                "[data-test='JobSearchResult'], [class*='job-listing'], "
                "[class*='JobListing'], .styles_component__jOT2b, "
                "[data-testid*='job-card']"
            )
            for card in cards:
                if len(jobs) >= results:
                    break
                try:
                    title_el = await card.query_selector(
                        "h2, h3, [class*='title'], [data-test='job-title']"
                    )
                    company_el = await card.query_selector(
                        "[class*='company'], [data-test='company-name'], a[href*='/company/']"
                    )
                    link_el = await card.query_selector(
                        "a[href*='/jobs/'], a[href*='/role/']"
                    )

                    title = (await title_el.inner_text()).strip() if title_el else ""
                    company = (await company_el.inner_text()).strip() if company_el else ""
                    href = await link_el.get_attribute("href") if link_el else None

                    if not title:
                        continue

                    # Wellfound card itself might be the job link
                    if not href:
                        href = await card.get_attribute("href")
                    if not href:
                        continue

                    if not href.startswith("http"):
                        href = f"https://wellfound.com{href}"

                    # Deduplicate
                    if any(j["job_url"] == href for j in jobs):
                        continue

                    salary_el = await card.query_selector("[class*='salary'], [class*='compensation']")
                    salary_text = (await salary_el.inner_text()).strip() if salary_el else ""

                    jobs.append({
                        "title": title,
                        "company": company,
                        "location": location,
                        "job_url": href,
                        "source": "wellfound",
                        "id": None,
                        "description": salary_text,
                        "salary_min": None,
                        "salary_max": None,
                        "currency": "USD",
                        "date_posted": None,
                        "raw_data": "",
                    })
                except Exception:
                    pass

            # Scroll to load more
            scroll_attempts += 1
            prev_count = len(jobs)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(s.BROWSER_MEDIUM_WAIT_MS)
            new_count = len(jobs)
            if new_count == prev_count and scroll_attempts > 2:
                break

        logger.info("Wellfound: collected %d jobs", len(jobs))
        return jobs

    async def apply_to_job(self, page, job: dict, resume_path: str) -> bool:
        """Apply to a Wellfound job.

        Wellfound-native apply: fills interest statement + optional Q&A.
        External-redirect jobs (Greenhouse/Lever) are collected as leads only.
        """
        from app.config import get_settings
        s = get_settings()
        job_url = job.get("job_url", "")
        if not job_url:
            return False

        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await page.wait_for_timeout(s.BROWSER_MEDIUM_WAIT_MS)

            apply_btn = await page.query_selector(
                "button:text-is('Apply'), a:text-is('Apply'), "
                "[data-test='apply-button'], button[class*='apply']"
            )
            if not apply_btn:
                return False

            # Check for external ATS links
            href = await apply_btn.get_attribute("href") or ""
            if href and any(ats in href for ats in ["greenhouse.io", "lever.co", "ashbyhq.com", "workday"]):
                logger.info("Wellfound: external ATS link detected — recording as lead, not applying")
                job["external_apply_url"] = href
                return False

            await apply_btn.click()
            await page.wait_for_timeout(s.BROWSER_MEDIUM_WAIT_MS)

            interest_field = await page.query_selector(
                "textarea[placeholder*='interest'], textarea[placeholder*='Why'], "
                "textarea[name*='intro'], textarea[name*='message'], "
                "[data-test='why-interested'] textarea"
            )
            if interest_field:
                company = job.get("company", "this company")
                role_title = job.get("title", "this role")
                interest_text = s.WELLFOUND_INTEREST_TEMPLATE.format(
                    role_title=role_title, company=company
                )
                await interest_field.fill(interest_text)
                await page.wait_for_timeout(500)

            submit_btn = await page.query_selector(
                "button[type='submit'], button:text-is('Submit Application'), "
                "button:text-is('Apply'), [data-test='submit-application']"
            )
            if submit_btn:
                await submit_btn.click()
                await page.wait_for_timeout(s.BROWSER_LONG_WAIT_MS)

            # Check success
            success = await page.query_selector(
                "[class*='success'], [class*='applied'], "
                "h1:text-matches('Application Sent', 'i'), "
                "p:text-matches('application has been sent', 'i')"
            )
            return success is not None

        except Exception as exc:
            logger.warning("Wellfound apply error for %s: %s", job_url, exc)
            return False
