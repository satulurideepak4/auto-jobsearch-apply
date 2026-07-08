"""Instahyre portal scraper and auto-applier.

Flow:
  1. Login with email + password (Google SSO also works but requires manual auth)
  2. Search jobs by role using Instahyre's search
  3. For each matching job, click "Apply" (Instahyre native 1-click apply)
  4. If the job redirects to an external site, skip in auto-apply mode

Instahyre apply:
  - Most applies are 1-click (profile is already on Instahyre)
  - A modal may appear asking for a note to the recruiter
  - Some jobs link to the company's own ATS (skipped here)
"""
from __future__ import annotations

import logging

from app.scraping.portal_scrapers import BasePortalScraper

logger = logging.getLogger(__name__)

_LOGIN_URL = "https://www.instahyre.com/login/"
_SEARCH_URL = "https://www.instahyre.com/search-jobs/?keyword={role}&location={location}"


class InstaHyreScraper(BasePortalScraper):
    SESSION_NAME = "instahyre"
    HOME_URL = "https://www.instahyre.com"

    async def is_logged_in(self, page) -> bool:
        from app.config import get_settings
        s = get_settings()
        try:
            await page.goto("https://www.instahyre.com", wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await page.wait_for_timeout(s.BROWSER_SHORT_WAIT_MS)
            profile = await page.query_selector(
                ".user-avatar, [class*='user-profile'], [class*='navbar-profile'], "
                "a[href*='/candidate/profile']"
            )
            return profile is not None
        except Exception:
            return False

    async def login(self, page, email: str, password: str) -> bool:
        from app.config import get_settings
        s = get_settings()
        logger.info("Instahyre: logging in as %s", email)
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
                logger.error("Instahyre: login fields not found")
                return False

            await email_el.fill(email)
            await pw_el.fill(password)

            submit = await page.query_selector("button[type='submit'], input[type='submit']")
            if submit:
                await submit.click()
            else:
                await pw_el.press("Enter")

            await page.wait_for_timeout(4_000)
            return "login" not in page.url.lower()
        except Exception as exc:
            logger.error("Instahyre login error: %s", exc)
            return False

    async def search(self, page, role: str, location: str, results: int = 30) -> list[dict]:
        from urllib.parse import quote
        from app.config import get_settings
        s = get_settings()
        search_url = _SEARCH_URL.format(
            role=quote(role, safe=""),
            location=quote(location, safe=""),
        )
        logger.info("Instahyre: searching %s", search_url)

        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await page.wait_for_timeout(s.BROWSER_LONG_WAIT_MS)
        except Exception as exc:
            logger.error("Instahyre: search navigation failed: %s", exc)
            return []

        jobs: list[dict] = []
        page_num = 0

        while len(jobs) < results and page_num < s.INSTAHYRE_PAGE_LIMIT:
            cards = await page.query_selector_all(
                ".job-listing, .job-card, [class*='JobCard'], "
                "[class*='job-item'], .jobs-list-item"
            )
            if not cards:
                # Try a broader selector
                cards = await page.query_selector_all("li[class*='job'], div[class*='job-result']")

            for card in cards:
                if len(jobs) >= results:
                    break
                try:
                    title_el = await card.query_selector(
                        "h2, h3, .job-title, [class*='job-title'], [class*='title']"
                    )
                    company_el = await card.query_selector(
                        ".company-name, [class*='company'], .employer"
                    )
                    link_el = await card.query_selector("a[href*='/job/'], a[href*='/jobs/']")

                    title = (await title_el.inner_text()).strip() if title_el else ""
                    company = (await company_el.inner_text()).strip() if company_el else ""
                    href = await link_el.get_attribute("href") if link_el else None

                    if not title or not href:
                        continue
                    if not href.startswith("http"):
                        href = f"https://www.instahyre.com{href}"

                    jobs.append({
                        "title": title,
                        "company": company,
                        "location": location,
                        "job_url": href,
                        "source": "instahyre",
                        "id": None,
                        "description": "",
                        "salary_min": None,
                        "salary_max": None,
                        "currency": "INR",
                        "date_posted": None,
                        "raw_data": "",
                    })
                except Exception:
                    pass

            page_num += 1
            next_btn = await page.query_selector(
                "a[rel='next'], a:text-is('Next'), [aria-label='Next page']"
            )
            if not next_btn:
                break
            try:
                await next_btn.click()
                await page.wait_for_timeout(s.BROWSER_LONG_WAIT_MS)
            except Exception:
                break

        logger.info("Instahyre: collected %d jobs", len(jobs))
        return jobs

    async def apply_to_job(self, page, job: dict, resume_path: str) -> bool:
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
                "button[class*='apply'], a[class*='apply-btn']"
            )
            if not apply_btn:
                return False

            href = await apply_btn.get_attribute("href") or ""
            if href and "instahyre.com" not in href and href.startswith("http"):
                logger.info("Instahyre: external redirect — skipping %s", job_url)
                return False

            await apply_btn.click()
            await page.wait_for_timeout(s.BROWSER_MEDIUM_WAIT_MS)

            note_area = await page.query_selector(
                "textarea[placeholder*='note'], textarea[placeholder*='message'], "
                "textarea[name*='message']"
            )
            if note_area:
                await note_area.fill(s.INSTAHYRE_APPLY_MESSAGE)

            confirm_btn = await page.query_selector(
                "button:text-is('Submit'), button:text-is('Send Application'), "
                "button[type='submit'], button:text-is('Apply Now')"
            )
            if confirm_btn:
                await confirm_btn.click()
                await page.wait_for_timeout(s.BROWSER_SHORT_WAIT_MS)

            success = await page.query_selector(
                "[class*='success'], [class*='applied'], .applied-badge"
            )
            return success is not None

        except Exception as exc:
            logger.warning("Instahyre apply error for %s: %s", job_url, exc)
            return False
