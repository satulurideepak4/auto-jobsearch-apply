"""Instahyre job board driver."""
from __future__ import annotations

import logging
from urllib.parse import quote

from app.scraping.drivers import BaseJobBoardDriver, _apply_stealth, _human_delay, _natural_scroll

logger = logging.getLogger(__name__)

_LOGIN_URL  = "https://www.instahyre.com/login/"
_SEARCH_URL = "https://www.instahyre.com/search-jobs/?keyword={role}&location={location}"


class InstahyreDriver(BaseJobBoardDriver):
    name = "instahyre"
    home_url = "https://www.instahyre.com"
    login_url = _LOGIN_URL
    requires_login = True
    region = "india"

    async def is_logged_in(self, page) -> bool:
        from app.config import get_settings
        s = get_settings()
        try:
            await _apply_stealth(page)
            await page.goto("https://www.instahyre.com", wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await _human_delay(page, 2000, 4000)

            # Profile/avatar elements that only appear when logged in
            profile = await page.query_selector(
                ".user-avatar, [class*='user-profile'], [class*='navbar-profile'], "
                "a[href*='/candidate/profile'], a[href*='/profile/'], "
                "[class*='candidate-nav'], [class*='loggedIn'], "
                "img[class*='avatar'], [data-testid*='user-avatar']"
            )
            if profile:
                return True

            # If login button is present → not logged in
            login_btn = await page.query_selector(
                "a:text-is('Login'), button:text-is('Login'), a[href*='/login']"
            )
            return login_btn is None
        except Exception:
            return False

    async def search(self, page, role: str, location: str, limit: int = 30) -> list[dict]:
        from app.config import get_settings
        s = get_settings()
        url = _SEARCH_URL.format(role=quote(role, safe=""), location=quote(location, safe=""))
        logger.info("Instahyre: searching %s", url)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await _human_delay(page, 2500, 5000)
            await _natural_scroll(page, steps=2)
        except Exception as exc:
            logger.error("Instahyre: navigation failed: %s", exc)
            return []

        jobs: list[dict] = []
        page_num = 0

        while len(jobs) < limit and page_num < s.INSTAHYRE_PAGE_LIMIT:
            # Primary selectors — class names Instahyre has used
            cards = await page.query_selector_all(
                ".job-listing, .job-card, [class*='JobCard'], "
                "[class*='job-item'], .jobs-list-item, "
                "[class*='jobListing'], [class*='job-listing-card'], "
                "[class*='opportunity-card']"
            )
            if not cards:
                # Broader fallbacks
                cards = await page.query_selector_all(
                    "li[class*='job'], div[class*='job-result'], "
                    "article[class*='job'], [data-job-id]"
                )

            logger.info("Instahyre: %d cards on page %d", len(cards), page_num + 1)

            for card in cards:
                if len(jobs) >= limit:
                    break
                try:
                    title_el = await card.query_selector(
                        "h2, h3, .job-title, [class*='job-title'], "
                        "[class*='title'], [class*='role-name'], [class*='position']"
                    )
                    company_el = await card.query_selector(
                        ".company-name, [class*='company-name'], [class*='company'], "
                        ".employer, [class*='employer'], [class*='org-name']"
                    )
                    link_el = await card.query_selector(
                        "a[href*='/job/'], a[href*='/jobs/'], "
                        "a[href*='/opportunity/'], a[href*='/role/']"
                    )

                    title   = (await title_el.inner_text()).strip()   if title_el   else ""
                    company = (await company_el.inner_text()).strip()  if company_el else ""
                    href    = await link_el.get_attribute("href")      if link_el    else None

                    if not title or not href:
                        continue
                    if not href.startswith("http"):
                        href = f"https://www.instahyre.com{href}"

                    if any(j["job_url"] == href for j in jobs):
                        continue

                    jobs.append({
                        "title": title, "company": company, "location": location,
                        "job_url": href, "source": "instahyre", "id": None,
                        "description": "", "salary_min": None, "salary_max": None,
                        "currency": "INR", "date_posted": None, "raw_data": "",
                    })
                except Exception:
                    pass

            page_num += 1
            next_btn = await page.query_selector(
                "a[rel='next'], a:text-is('Next'), [aria-label='Next page'], "
                "button:text-is('Next'), [class*='next-page']"
            )
            if not next_btn:
                break
            try:
                await next_btn.click()
                await _human_delay(page, s.BROWSER_LONG_WAIT_MS, s.BROWSER_LONG_WAIT_MS + 1000)
            except Exception:
                break

        return jobs

    async def apply_to_job(self, page, job: dict) -> bool:
        """Apply to an Instahyre job. Returns True on confirmed success."""
        from app.config import get_settings
        s = get_settings()
        job_url = job.get("job_url", "")
        if not job_url:
            return False
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await _human_delay(page, s.BROWSER_MEDIUM_WAIT_MS, s.BROWSER_LONG_WAIT_MS)

            # Instahyre uses "Apply", "Connect", or "Show Interest" depending on job type
            apply_btn = await page.query_selector(
                "button:text-is('Apply'), a:text-is('Apply'), "
                "button:text-is('Apply Now'), a:text-is('Apply Now'), "
                "button:text-is('Connect'), a:text-is('Connect'), "
                "button:text-is('Show Interest'), "
                "button[class*='apply'], a[class*='apply-btn'], "
                "[data-testid*='apply']"
            )
            if not apply_btn:
                logger.info("Instahyre apply: no apply button at %s", job_url)
                return False

            # Detect external redirect before clicking
            href = await apply_btn.get_attribute("href") or ""
            if href and href.startswith("http") and "instahyre.com" not in href:
                logger.info("Instahyre apply: external redirect — %s", href[:80])
                job["external_apply_url"] = href
                return False

            await apply_btn.click()
            await _human_delay(page, s.BROWSER_MEDIUM_WAIT_MS, s.BROWSER_LONG_WAIT_MS)

            # Detect external redirect after clicking (some jobs redirect post-click)
            if "instahyre.com" not in page.url:
                logger.info("Instahyre apply: post-click redirect — %s", page.url[:80])
                job["external_apply_url"] = page.url
                return False

            # Fill note/message textarea if a modal appeared
            note_area = await page.query_selector(
                "textarea[placeholder*='note'], textarea[placeholder*='message'], "
                "textarea[placeholder*='interest'], textarea[placeholder*='Why'], "
                "textarea[placeholder*='Introduce'], "
                "textarea[name*='message'], textarea[name*='note'], "
                "textarea[name*='intro'], [data-testid*='message'] textarea"
            )
            if note_area:
                await note_area.fill(s.INSTAHYRE_APPLY_MESSAGE)
                await _human_delay(page, 300, 600)

            # Submit / Send Application
            confirm_btn = await page.query_selector(
                "button:text-is('Submit'), button:text-is('Send Application'), "
                "button:text-is('Send'), button:text-is('Apply Now'), "
                "button:text-is('Connect'), button[type='submit'], "
                "[data-testid*='submit']"
            )
            if confirm_btn:
                await confirm_btn.click()
                await _human_delay(page, s.BROWSER_SHORT_WAIT_MS, s.BROWSER_MEDIUM_WAIT_MS)

            # Success detection — Instahyre shows a confirmation banner/badge
            success = await page.query_selector(
                "[class*='success'], [class*='applied'], .applied-badge, "
                "[class*='Applied'], [class*='connected'], "
                "div:text-matches('applied', 'i'), "
                "div:text-matches('Application sent', 'i'), "
                "div:text-matches('Successfully applied', 'i'), "
                "p:text-matches('applied', 'i'), "
                "[class*='toast']:text-matches('applied|sent', 'i')"
            )
            return success is not None

        except Exception as exc:
            logger.warning("Instahyre apply error: %s", exc)
            return False


DRIVER = InstahyreDriver()
