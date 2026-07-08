"""Portal scrapers for Naukri, Instahyre, and Wellfound.

Authentication:
  Run scripts/extract_cookies.py from your Terminal once after logging into
  each site in Chrome. Cookies are saved to ~/.auto-jobsearch/sessions/.
  Playwright injects them on every run — no browser login needed.

  Re-run extract_cookies.py if you see login errors (~30 days).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SESSION_DIR = Path.home() / ".auto-jobsearch" / "sessions"


def _load_cookies(site: str) -> list[dict]:
    """Load saved cookies for a site. Returns [] if not found."""
    path = _SESSION_DIR / f"{site}_cookies.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


class BasePortalScraper:
    SESSION_NAME: str = "portal"
    HOME_URL: str = ""

    async def is_logged_in(self, page) -> bool:
        raise NotImplementedError

    async def search(self, page, role: str, location: str, results: int) -> list[dict]:
        raise NotImplementedError

    async def apply_to_job(self, page, job: dict, resume_path: str) -> bool:
        raise NotImplementedError

    async def search_and_apply(
        self,
        role: str,
        location: str,
        email: str = "",
        password: str = "",
        resume_path: str = "",
        results_wanted: int = 30,
        auto_apply: bool = False,
    ) -> list[dict]:
        from playwright.async_api import async_playwright

        cookies = _load_cookies(self.SESSION_NAME)
        if not cookies:
            logger.warning(
                "%s: no saved cookies found. "
                "Log into %s in Chrome then run: "
                ".venv/bin/python scripts/extract_cookies.py",
                self.__class__.__name__, self.HOME_URL,
            )
            return []

        collected: list[dict] = []

        from app.config import get_settings
        s = get_settings()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=s.BROWSER_HEADLESS,
                args=["--no-first-run", "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=s.BROWSER_UA,
            )
            await context.add_cookies(cookies)
            page = await context.new_page()

            try:
                logged = await self.is_logged_in(page)
                if not logged:
                    logger.warning(
                        "%s: cookies loaded but session expired. "
                        "Log into %s in Chrome and re-run scripts/extract_cookies.py",
                        self.__class__.__name__, self.HOME_URL,
                    )
                    return []

                logger.info("%s: logged in — searching %r in %r", self.__class__.__name__, role, location)
                collected = await self.search(page, role, location, results_wanted)
                logger.info("%s: found %d jobs", self.__class__.__name__, len(collected))

                if auto_apply and resume_path:
                    applied = 0
                    for job in collected:
                        try:
                            if await self.apply_to_job(page, job, resume_path):
                                job["applied"] = True
                                applied += 1
                                logger.info("Applied: %s @ %s", job.get("title"), job.get("company"))
                        except Exception as exc:
                            logger.warning("Apply failed for %s: %s", job.get("job_url"), exc)
                    logger.info("%s: applied to %d / %d", self.__class__.__name__, applied, len(collected))

            finally:
                await page.close()
                await context.close()
                await browser.close()

        return collected
