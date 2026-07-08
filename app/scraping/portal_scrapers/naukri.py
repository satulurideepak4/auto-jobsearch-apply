"""Naukri.com portal scraper and auto-applier.

Flow:
  1. Navigate to Naukri, check if logged in (via profile icon)
  2. If not logged in, fill email+password and submit
  3. Search by role + location using Naukri's search bar
  4. Collect job cards from the results page
  5. (Optional) Click Apply on each job and handle Naukri's native apply flow

Naukri native apply:
  - Many jobs have a "Apply" button that submits your saved Naukri profile directly
  - Some redirect to company site (these are skipped in auto-apply mode)
  - Naukri may ask: notice period, current CTC, expected CTC — answered from resume data
"""
from __future__ import annotations

import logging

from app.scraping.portal_scrapers import BasePortalScraper
from app.scraping.drivers.naukri import _build_search_url

logger = logging.getLogger(__name__)

_LOGIN_URL = "https://www.naukri.com/nlogin/login"


class NaukriScraper(BasePortalScraper):
    SESSION_NAME = "naukri"
    HOME_URL = "https://www.naukri.com"

    async def is_logged_in(self, page) -> bool:
        from app.config import get_settings
        s = get_settings()
        try:
            await page.goto("https://www.naukri.com", wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await page.wait_for_timeout(s.BROWSER_SHORT_WAIT_MS)
            if "/mnjuser/" in page.url:
                return True
            profile = await page.query_selector(
                ".nI-gNb-icon__useravtar, [class*='nI-gNb-icon__useravtar'], "
                "[data-ga-track*='myNaukri'], [data-ga-track*='loggedIn']"
            )
            if profile:
                return True
            login_link = await page.query_selector(
                "a[href*='/nlogin/login'], a:text-is('Login'), button:text-is('Login')"
            )
            return login_link is None
        except Exception:
            return False

    async def login(self, page, email: str, password: str) -> bool:
        from app.config import get_settings
        s = get_settings()
        logger.info("Naukri: logging in as %s", email)
        try:
            await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await page.wait_for_timeout(1_500)

            email_el = await page.query_selector(
                "input[placeholder*='Email'], input[type='email'], input[name='username']"
            )
            pw_el = await page.query_selector(
                "input[placeholder*='Password'], input[type='password']"
            )
            if not email_el or not pw_el:
                logger.error("Naukri: could not find login fields")
                return False

            await email_el.fill(email)
            await pw_el.fill(password)

            submit = await page.query_selector(
                "button[type='submit'], button:text-is('Login'), input[type='submit']"
            )
            if submit:
                await submit.click()
            else:
                await pw_el.press("Enter")

            await page.wait_for_timeout(4_000)  # allow redirect after submit
            return "login" not in page.url
        except Exception as exc:
            logger.error("Naukri login error: %s", exc)
            return False

    async def search(self, page, role: str, location: str, results: int = 30) -> list[dict]:
        from app.config import get_settings
        s = get_settings()
        role_slug = role.lower().strip().replace(" ", "-")
        loc_slug = location.lower().strip().replace(" ", "-")
        search_url = _build_search_url(role_slug, loc_slug)
        logger.info("Naukri: searching %s", search_url)

        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await page.wait_for_timeout(s.BROWSER_LONG_WAIT_MS)
        except Exception as exc:
            logger.error("Naukri: search navigation failed: %s", exc)
            return []

        jobs: list[dict] = []
        page_num = 0
        while len(jobs) < results and page_num < s.NAUKRI_PAGE_LIMIT:
            cards = await page.query_selector_all(
                "article.jobTuple, .jobTupleHeader, [class*='job-tuple'], "
                "[data-job-id], .list > li[class*='job']"
            )
            for card in cards:
                if len(jobs) >= results:
                    break
                try:
                    title_el = await card.query_selector(
                        ".title, a.title, h2 a, [class*='jobTitle'] a, .row1 a"
                    )
                    company_el = await card.query_selector(
                        ".subTitle, .companyName, [class*='companyName']"
                    )
                    link_el = title_el or await card.query_selector("a[href*='naukri.com/job']")

                    title = (await title_el.inner_text()).strip() if title_el else ""
                    company = (await company_el.inner_text()).strip() if company_el else ""
                    href = await link_el.get_attribute("href") if link_el else None

                    if not title or not href:
                        continue

                    if not href.startswith("http"):
                        href = f"https://www.naukri.com{href}"

                    job_id = await card.get_attribute("data-job-id") or ""
                    location_el = await card.query_selector("[class*='locWdth'], .location, .loc")
                    loc_text = (await location_el.inner_text()).strip() if location_el else location

                    jobs.append({
                        "title": title,
                        "company": company,
                        "location": loc_text,
                        "job_url": href,
                        "source": "naukri",
                        "id": job_id,
                        "description": "",
                        "salary_min": None,
                        "salary_max": None,
                        "currency": "INR",
                        "date_posted": None,
                        "raw_data": "",
                    })
                except Exception:
                    pass

            # Next page
            page_num += 1
            next_btn = await page.query_selector(
                "a[class*='fright'][title='Next'], a[aria-label='Next'], .pagination li:last-child a"
            )
            if not next_btn:
                break
            try:
                await next_btn.click()
                await page.wait_for_timeout(s.BROWSER_LONG_WAIT_MS)
            except Exception:
                break

        logger.info("Naukri: collected %d jobs", len(jobs))
        return jobs

    async def apply_to_job(self, page, job: dict, resume_path: str) -> bool:
        """Apply to a Naukri job using the native Naukri apply flow.

        Only handles Naukri-native apply (not company-site redirects).
        """
        from app.config import get_settings
        s = get_settings()
        job_url = job.get("job_url", "")
        if not job_url:
            return False

        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await page.wait_for_timeout(s.BROWSER_MEDIUM_WAIT_MS)

            # Find the Apply button — Naukri uses multiple IDs/classes
            apply_btn = await page.query_selector(
                "#apply-button, a#apply-button, button#applyBtn, "
                "button:text-is('Apply'), a:text-is('Apply'), "
                "[class*='apply-button']:not([class*='external'])"
            )
            if not apply_btn:
                logger.info("Naukri: no apply button found for %s", job_url)
                return False

            # Check if it redirects externally (company site)
            href = await apply_btn.get_attribute("href") or ""
            if href and "naukri.com" not in href and href.startswith("http"):
                logger.info("Naukri: external apply link detected — skipping (would redirect to %s)", href[:60])
                return False

            await apply_btn.click()
            await page.wait_for_timeout(s.BROWSER_LONG_WAIT_MS)

            confirm_btn = await page.query_selector(
                "button:text-is('Apply'), button:text-is('Confirm'), "
                "button[class*='applyBtn'], #confirmApply"
            )
            if confirm_btn:
                await confirm_btn.click()
                await page.wait_for_timeout(s.BROWSER_SHORT_WAIT_MS)

            await self._fill_quick_questions(page)

            final_submit = await page.query_selector(
                "button:text-is('Submit'), button[type='submit'], button:text-is('Apply Now')"
            )
            if final_submit:
                await final_submit.click()
                await page.wait_for_timeout(s.BROWSER_SHORT_WAIT_MS)

            # Check for success indicator
            success = await page.query_selector(
                "[class*='success'], [class*='applied'], h3:text-matches('applied', 'i')"
            )
            return success is not None

        except Exception as exc:
            logger.warning("Naukri apply error for %s: %s", job_url, exc)
            return False

    async def _fill_quick_questions(self, page) -> None:
        """Fill Naukri quick-apply questions (notice period, CTC)."""
        try:
            # Notice period dropdown
            notice_sel = await page.query_selector(
                "select[name*='notice'], select[placeholder*='Notice'], "
                "[data-label*='Notice Period'] select"
            )
            if notice_sel:
                await notice_sel.select_option(index=1)  # Pick first option (15 days / immediate)

            # Current CTC — leave blank (Naukri uses saved profile value)
            # Expected CTC — leave blank
        except Exception:
            pass
