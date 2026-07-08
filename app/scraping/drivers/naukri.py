"""Naukri.com job board driver."""
from __future__ import annotations

import logging
import random

from app.scraping.drivers import BaseJobBoardDriver, _apply_stealth, _human_delay, _natural_scroll

logger = logging.getLogger(__name__)

_SEARCH_BASE = "https://www.naukri.com/{role}-jobs-in-{location}"
_LOGIN_URL = "https://www.naukri.com/nlogin/login"

_WORK_MODE_CODES = {"wfh": "1", "hybrid": "2", "office": "4"}
_JOB_TYPE_CODES  = {"fulltime": "1", "parttime": "2", "contract": "3"}


def _build_search_url(role_slug: str, loc_slug: str) -> str:
    """Build Naukri search URL with all active filters from settings."""
    from app.config import get_settings
    s = get_settings()

    url = _SEARCH_BASE.format(role=role_slug, location=loc_slug)

    exp = (
        f"{s.NAUKRI_EXP_MIN}-{s.NAUKRI_EXP_MAX}"
        if s.NAUKRI_EXP_MAX is not None
        else str(s.NAUKRI_EXP_MIN)
    )
    params = [f"experience={exp}"]

    if s.NAUKRI_SALARY_MIN_LPA and s.NAUKRI_SALARY_MAX_LPA:
        params.append(f"salary={s.NAUKRI_SALARY_MIN_LPA}-{s.NAUKRI_SALARY_MAX_LPA}")
    elif s.NAUKRI_SALARY_MIN_LPA:
        params.append(f"salary={s.NAUKRI_SALARY_MIN_LPA}-99")

    wfh_code = _WORK_MODE_CODES.get(s.NAUKRI_WORK_MODE.lower())
    if wfh_code:
        params.append(f"wfhType={wfh_code}")

    jt_code = _JOB_TYPE_CODES.get(s.NAUKRI_JOB_TYPE.lower())
    if jt_code:
        params.append(f"jobType={jt_code}")

    if s.NAUKRI_POSTED_DAYS and s.NAUKRI_POSTED_DAYS > 0:
        params.append(f"postedDate={s.NAUKRI_POSTED_DAYS}")

    return url + "?" + "&".join(params)


class NaukriDriver(BaseJobBoardDriver):
    name = "naukri"
    home_url = "https://www.naukri.com"
    login_url = _LOGIN_URL
    requires_login = True
    region = "india"

    async def is_logged_in(self, page) -> bool:
        from app.config import get_settings
        s = get_settings()
        try:
            await _apply_stealth(page)
            await page.goto("https://www.naukri.com", wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await _human_delay(page, 2000, 4000)
            await _natural_scroll(page, steps=2)

            # Redirect to /mnjuser/ means definitely logged in
            if "/mnjuser/" in page.url:
                return True

            # Look for profile avatar / user menu elements
            profile = await page.query_selector(
                ".nI-gNb-icon__useravtar, [class*='nI-gNb-icon__useravtar'], "
                "[data-ga-track*='myNaukri'], [data-ga-track*='loggedIn'], "
                "[class*='user-avatar'], [class*='userAvatar'], "
                "a[href*='/mnjuser/'], img[class*='avatar']"
            )
            if profile:
                return True

            # If Login button is visible → not logged in
            login_link = await page.query_selector(
                "a[href*='/nlogin/login'], a:text-is('Login'), button:text-is('Login')"
            )
            return login_link is None
        except Exception:
            return False

    async def search(self, page, role: str, location: str, limit: int = 30) -> list[dict]:
        from app.config import get_settings
        s = get_settings()
        role_slug = role.lower().strip().replace(" ", "-")
        loc_slug = location.lower().strip().replace(" ", "-")
        url = _build_search_url(role_slug, loc_slug)
        logger.info("Naukri: searching %s", url)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await _human_delay(page, 3000, 6000)
            await _natural_scroll(page, steps=random.randint(2, 4))
        except Exception as exc:
            logger.error("Naukri: navigation failed: %s", exc)
            return []

        jobs: list[dict] = []
        page_num = 0

        while len(jobs) < limit and page_num < s.NAUKRI_PAGE_LIMIT:
            # Naukri 2024/25 DOM uses [data-job-id] on each job card.
            # Multiple selector fallbacks handle layout variations.
            cards = await page.query_selector_all(
                "[data-job-id], "
                "article.jobTuple, "
                ".srp-jobtuple-wrapper, "
                "[class*='jobtuple'], "
                "[class*='job-tuple'], "
                ".cust-job-tuple"
            )

            if not cards:
                # Broad fallback — any list item that looks like a job row
                cards = await page.query_selector_all(
                    ".list > li, [class*='jobCard'], [class*='job-card']"
                )

            logger.info("Naukri: found %d cards on page %d", len(cards), page_num + 1)

            for card in cards:
                if len(jobs) >= limit:
                    break
                try:
                    # Title — try multiple selector patterns
                    title_el = await card.query_selector(
                        "a.title, a[class*='title'], "
                        "[class*='jobTitle'] a, [class*='job-title'] a, "
                        ".info a, h2 a, h3 a, .row1 a"
                    )
                    # Company
                    company_el = await card.query_selector(
                        "a[class*='comp-name'], [class*='comp-name'], "
                        ".subTitle, a.subTitle, "
                        "[class*='companyName'], [class*='company-name']"
                    )
                    # Link — prefer title element, fallback to any naukri job href
                    link_el = title_el or await card.query_selector(
                        "a[href*='naukri.com/'], a[href*='/job-listings/'], a[href*='/jobs/']"
                    )

                    title   = (await title_el.inner_text()).strip()   if title_el   else ""
                    company = (await company_el.inner_text()).strip()  if company_el else ""
                    href    = await link_el.get_attribute("href")      if link_el    else None

                    if not title or not href:
                        continue
                    if not href.startswith("http"):
                        href = f"https://www.naukri.com{href}"

                    job_id  = await card.get_attribute("data-job-id") or ""
                    loc_el  = await card.query_selector(
                        "[class*='locWdth'], [class*='location'], .loc, "
                        "[class*='loc-wrap'], [class*='jobLocation']"
                    )
                    loc_text = (await loc_el.inner_text()).strip() if loc_el else location

                    # Deduplicate
                    if any(j["job_url"] == href for j in jobs):
                        continue

                    jobs.append({
                        "title": title, "company": company, "location": loc_text,
                        "job_url": href, "source": "naukri", "id": job_id,
                        "description": "", "salary_min": None, "salary_max": None,
                        "currency": "INR", "date_posted": None, "raw_data": "",
                    })
                except Exception:
                    pass

            page_num += 1
            next_btn = await page.query_selector(
                "a[class*='fright'][title='Next'], "
                "a[aria-label='Next'], "
                ".pagination li:last-child a, "
                "a[class*='next'], button[class*='next']"
            )
            if not next_btn:
                break
            try:
                await next_btn.click()
                await _human_delay(page, 3000, 7000)
                await _natural_scroll(page, steps=2)
            except Exception:
                break

        return jobs

    async def apply_to_job(self, page, job: dict) -> bool:
        """Native Naukri apply (profile-based). Returns True on confirmed success."""
        from app.config import get_settings
        s = get_settings()
        job_url = job.get("job_url", "")
        if not job_url:
            return False
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS)
            await _human_delay(page, s.BROWSER_MEDIUM_WAIT_MS, s.BROWSER_LONG_WAIT_MS)

            # Try multiple apply button patterns Naukri has used
            apply_btn = await page.query_selector(
                "#apply-button, a#apply-button, button#applyBtn, "
                "button#apply-button, "
                "[class*='apply-button']:not([class*='external']):not([class*='disabled']), "
                "button:text-is('Apply'), a:text-is('Apply'), "
                "button:text-is('Apply Now'), a:text-is('Apply Now'), "
                "[data-ga-track*='Apply'], [class*='applyBtn']"
            )
            if not apply_btn:
                logger.info("Naukri apply: no apply button found at %s", job_url)
                return False

            # Detect external ATS redirect before clicking
            href = await apply_btn.get_attribute("href") or ""
            if href and href.startswith("http") and "naukri.com" not in href:
                logger.info("Naukri apply: external ATS href detected — %s", href[:80])
                job["external_apply_url"] = href
                return False

            await apply_btn.click()
            await _human_delay(page, s.BROWSER_LONG_WAIT_MS, s.BROWSER_LONG_WAIT_MS + 1000)

            # Some jobs redirect to external site after clicking Apply
            if "naukri.com" not in page.url:
                logger.info("Naukri apply: redirected to external site — %s", page.url[:80])
                job["external_apply_url"] = page.url
                return False

            # Confirmation button inside the apply modal
            confirm_btn = await page.query_selector(
                "button:text-is('Apply'), button:text-is('Confirm'), "
                "button[class*='applyBtn'], #confirmApply, "
                "button:text-is('Submit Application')"
            )
            if confirm_btn:
                await confirm_btn.click()
                await _human_delay(page, s.BROWSER_SHORT_WAIT_MS, s.BROWSER_MEDIUM_WAIT_MS)

            # Fill any quick screening questions
            await self._fill_quick_questions(page)

            # Final submit button (may appear after answering questions)
            final = await page.query_selector(
                "button:text-is('Submit'), button[type='submit'], "
                "button:text-is('Apply Now'), button:text-is('Done')"
            )
            if final:
                await final.click()
                await _human_delay(page, s.BROWSER_SHORT_WAIT_MS, s.BROWSER_MEDIUM_WAIT_MS)

            # Success indicators: toast / banner / text
            success = await page.query_selector(
                "[class*='success'], [class*='applied'], [class*='Applied'], "
                "h3:text-matches('applied', 'i'), "
                "div:text-matches('Applied Successfully', 'i'), "
                "div:text-matches('application.*submitted', 'i'), "
                "p:text-matches('applied', 'i'), "
                "[class*='toast']:text-matches('applied', 'i')"
            )
            return success is not None

        except Exception as exc:
            logger.warning("Naukri apply error: %s", exc)
            return False

    async def _fill_quick_questions(self, page) -> None:
        """Fill Naukri quick-apply screening questions: notice period, CTC."""
        from app.config import get_settings
        s = get_settings()
        try:
            # ── Notice period ──────────────────────────────────────────────
            notice_sel = await page.query_selector(
                "select[name*='notice'], select[id*='notice'], "
                "[data-label*='Notice Period'] select, "
                "select[placeholder*='Notice']"
            )
            if notice_sel:
                notice_val = s.APPLICANT_NOTICE_PERIOD.lower()
                # Try to match option text; fall back to first option
                try:
                    if "immediate" in notice_val or notice_val == "0":
                        await notice_sel.select_option(label="0 days")
                    elif "15" in notice_val:
                        await notice_sel.select_option(label="15 days")
                    elif "30" in notice_val or "1 month" in notice_val:
                        await notice_sel.select_option(label="1 month")
                    elif "60" in notice_val or "2 month" in notice_val:
                        await notice_sel.select_option(label="2 months")
                    else:
                        await notice_sel.select_option(index=1)
                except Exception:
                    await notice_sel.select_option(index=0)

            # ── Current CTC ────────────────────────────────────────────────
            if s.NAUKRI_CURRENT_CTC:
                cur_ctc = await page.query_selector(
                    "input[name*='currentCtc'], input[name*='current_ctc'], "
                    "input[id*='currentCtc'], [data-label*='Current CTC'] input, "
                    "input[placeholder*='Current CTC'], input[placeholder*='Current Salary']"
                )
                if cur_ctc:
                    await cur_ctc.fill(str(s.NAUKRI_CURRENT_CTC))

            # ── Expected CTC ───────────────────────────────────────────────
            if s.APPLICANT_SALARY_EXPECT:
                exp_ctc = await page.query_selector(
                    "input[name*='expectedCtc'], input[name*='expected_ctc'], "
                    "input[id*='expectedCtc'], [data-label*='Expected CTC'] input, "
                    "input[placeholder*='Expected CTC'], input[placeholder*='Expected Salary']"
                )
                if exp_ctc:
                    await exp_ctc.fill(str(s.APPLICANT_SALARY_EXPECT))

        except Exception as exc:
            logger.debug("Naukri quick questions error: %s", exc)


DRIVER = NaukriDriver()
