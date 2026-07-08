"""Wellfound (AngelList Talent) job board driver."""
from __future__ import annotations

import logging
from urllib.parse import quote

from app.scraping.drivers import BaseJobBoardDriver, _apply_stealth, _human_delay, _natural_scroll

logger = logging.getLogger(__name__)

_LOGIN_URL = "https://wellfound.com/login"
_JOBS_URL  = "https://wellfound.com/jobs?q={role}&location_slugs={location}"

# External ATS domains — when Wellfound's Apply button links to one of these,
# we store the URL and let IntelligentFiller handle the ATS form.
_EXTERNAL_ATS = [
    "greenhouse.io", "lever.co", "ashbyhq.com",
    "myworkdayjobs.com", "myworkday.com",
    "smartrecruiters.com", "icims.com",
    "jobvite.com", "breezy.hr", "workable.com",
    "bamboohr.com", "eightfold.ai", "taleo.net",
]


class WellfoundDriver(BaseJobBoardDriver):
    name = "wellfound"
    home_url = "https://wellfound.com"
    login_url = _LOGIN_URL
    requires_login = True
    region = "international"

    async def is_logged_in(self, page) -> bool:
        from app.config import get_settings
        s = get_settings()
        try:
            await _apply_stealth(page)
            await page.goto("https://wellfound.com", wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await _human_delay(page, 2000, 4000)

            # Wellfound shows avatar / user menu only when logged in
            profile = await page.query_selector(
                "a[href*='/me'], a[href*='/profile'], "
                "[class*='user-avatar'], [data-test='user-avatar'], "
                "img[alt*='avatar'], img[alt*='profile'], "
                "[class*='UserAvatar'], [data-test='nav-profile']"
            )
            login_btn = await page.query_selector(
                "a:text-is('Log In'), a:text-is('Sign In'), "
                "button:text-is('Log In'), button:text-is('Sign In')"
            )
            return profile is not None and login_btn is None
        except Exception:
            return False

    async def search(self, page, role: str, location: str, limit: int = 30) -> list[dict]:
        from app.config import get_settings
        s = get_settings()
        loc_slug = location.lower().replace(",", "").replace(" ", "-")
        url = _JOBS_URL.format(role=quote(role, safe=""), location=quote(loc_slug, safe=""))
        logger.info("Wellfound: searching %s", url)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await _human_delay(page, 3000, 5500)
            await _natural_scroll(page, steps=2)
        except Exception as exc:
            logger.error("Wellfound: navigation failed: %s", exc)
            return []

        jobs: list[dict] = []
        scroll_attempts = 0

        while len(jobs) < limit and scroll_attempts < s.WELLFOUND_SCROLL_LIMIT:
            cards = await page.query_selector_all(
                "[data-test='JobSearchResult'], "
                "[class*='job-listing'], [class*='JobListing'], "
                ".styles_component__jOT2b, "
                "[data-testid*='job-card'], "
                "[class*='StartupResult'], [class*='startup-result']"
            )

            for card in cards:
                if len(jobs) >= limit:
                    break
                try:
                    title_el = await card.query_selector(
                        "h2, h3, [class*='title'], [data-test='job-title'], "
                        "[class*='role-title'], [class*='jobTitle']"
                    )
                    company_el = await card.query_selector(
                        "[class*='company'], [data-test='company-name'], "
                        "a[href*='/company/'], [class*='startup-name'], "
                        "[class*='org-name']"
                    )
                    link_el = await card.query_selector(
                        "a[href*='/jobs/'], a[href*='/role/'], "
                        "a[href*='/l/']"
                    )

                    title   = (await title_el.inner_text()).strip()   if title_el   else ""
                    company = (await company_el.inner_text()).strip()  if company_el else ""
                    href    = await link_el.get_attribute("href")      if link_el    else None

                    if not title:
                        continue
                    if not href:
                        href = await card.get_attribute("href")
                    if not href:
                        continue
                    if not href.startswith("http"):
                        href = f"https://wellfound.com{href}"

                    if any(j["job_url"] == href for j in jobs):
                        continue

                    salary_el = await card.query_selector(
                        "[class*='salary'], [class*='compensation'], "
                        "[data-test='salary']"
                    )
                    salary_text = (await salary_el.inner_text()).strip() if salary_el else ""

                    jobs.append({
                        "title": title, "company": company, "location": location,
                        "job_url": href, "source": "wellfound", "id": None,
                        "description": salary_text, "salary_min": None, "salary_max": None,
                        "currency": "USD", "date_posted": None, "raw_data": "",
                    })
                except Exception:
                    pass

            scroll_attempts += 1
            prev_count = len(jobs)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(s.BROWSER_MEDIUM_WAIT_MS)
            if len(jobs) == prev_count and scroll_attempts > 2:
                break

        return jobs

    async def apply_to_job(self, page, job: dict) -> bool:
        """Apply to a Wellfound job.

        Native Wellfound apply: fills interest statement + optional questions.
        External ATS jobs: records external_apply_url so IntelligentFiller can handle them.
        Returns True only when a native Wellfound application is submitted.
        """
        from app.config import get_settings
        s = get_settings()
        job_url = job.get("job_url", "")
        if not job_url:
            return False
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await _human_delay(page, s.BROWSER_MEDIUM_WAIT_MS, s.BROWSER_LONG_WAIT_MS)

            # Find the Apply / Request Introduction button
            apply_btn = await page.query_selector(
                "[data-test='apply-button'], "
                "button:text-is('Apply'), a:text-is('Apply'), "
                "button:text-is('Apply Now'), a:text-is('Apply Now'), "
                "button:text-is('Request Introduction'), "
                "button[class*='apply'], a[class*='apply']"
            )
            if not apply_btn:
                logger.info("Wellfound apply: no apply button at %s", job_url)
                return False

            # Check href BEFORE clicking — external ATS links are detectable here
            href = await apply_btn.get_attribute("href") or ""
            if href and href.startswith("http"):
                if any(ats in href for ats in _EXTERNAL_ATS):
                    logger.info("Wellfound apply: external ATS href — %s", href[:80])
                    job["external_apply_url"] = href
                    return False

            await apply_btn.click()
            await _human_delay(page, s.BROWSER_MEDIUM_WAIT_MS, s.BROWSER_LONG_WAIT_MS)

            # Detect post-click redirect to external ATS
            current_url = page.url
            if any(ats in current_url for ats in _EXTERNAL_ATS):
                logger.info("Wellfound apply: post-click ATS redirect — %s", current_url[:80])
                job["external_apply_url"] = current_url
                return False

            # New tab may have opened with an external ATS
            # (handled at run_apply() level via run_multi_search context)

            # ── Interest / message textarea ────────────────────────────────
            interest_field = await page.query_selector(
                "textarea[placeholder*='interest'], textarea[placeholder*='Why'], "
                "textarea[placeholder*='Introduce'], textarea[placeholder*='message'], "
                "textarea[name*='intro'], textarea[name*='message'], textarea[name*='why'], "
                "[data-test='why-interested'] textarea, "
                "[data-testid*='interest'] textarea, "
                "[data-testid*='message'] textarea"
            )
            if interest_field:
                company    = job.get("company", "this company")
                role_title = job.get("title", "this role")
                interest_text = s.WELLFOUND_INTEREST_TEMPLATE.format(
                    role_title=role_title, company=company
                )
                await interest_field.fill(interest_text)
                await _human_delay(page, 300, 600)

            # ── Submit ─────────────────────────────────────────────────────
            submit_btn = await page.query_selector(
                "button[type='submit'], "
                "button:text-is('Submit Application'), button:text-is('Submit'), "
                "button:text-is('Apply'), button:text-is('Send'), "
                "[data-test='submit-application'], [data-testid*='submit']"
            )
            if submit_btn:
                await submit_btn.click()
                await _human_delay(page, s.BROWSER_LONG_WAIT_MS, s.BROWSER_LONG_WAIT_MS + 1000)

            # ── Success detection ──────────────────────────────────────────
            success = await page.query_selector(
                "[class*='success'], [class*='applied'], [class*='Applied'], "
                "[class*='application-sent'], "
                "h1:text-matches('Application Sent', 'i'), "
                "h2:text-matches('Applied', 'i'), "
                "p:text-matches('application has been sent', 'i'), "
                "p:text-matches('successfully applied', 'i'), "
                "div:text-matches('Application Sent', 'i'), "
                "[data-test='application-confirmation']"
            )
            return success is not None

        except Exception as exc:
            logger.warning("Wellfound apply error: %s", exc)
            return False


DRIVER = WellfoundDriver()
