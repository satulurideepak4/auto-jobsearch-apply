"""Universal job board and careers page crawler.

Works on any public URL — known job boards, company careers pages, or custom boards.

Discovery strategy (in order of preference):
  1. Hint selectors from config  (board-specific, high precision)
  2. Heuristic CSS patterns       (common job board structures)
  3. JS link scoring              (any anchor whose text/href looks like a job)

Pagination: supports next-button click AND infinite-scroll boards.

Standalone use:
    crawler = UniversalCrawler()
    jobs = await crawler.crawl("https://careers.stripe.com", role="engineer", limit=20)

Called internally by ConfigDrivenDriver for config-only boards.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

# CSS selectors that commonly contain job title links on boards
_HEURISTIC_SELECTORS = [
    # Greenhouse
    ".opening a", ".job-post a",
    # Lever
    ".posting h5 a", ".posting-title a",
    # Ashby
    "a[href*='/jobs/']", "a[href*='/careers/']",
    # Workday
    "a[data-automation-id*='jobTitle']",
    # Generic
    "[class*='job-title'] a", "[class*='jobTitle'] a",
    "[class*='job-card'] a[href]", "[class*='job-listing'] a[href]",
    "[class*='position'] a[href]", "[class*='vacancy'] a[href]",
    "[class*='opening'] a[href]", "[class*='role'] a[href]",
    "article a[href]",
]

# Words that appear in the href of real job links
_JOB_URL_KEYWORDS = {
    "job", "jobs", "career", "careers", "position", "opening",
    "vacancy", "role", "roles", "posting", "apply", "join",
    "opportunity", "requisition", "req", "jd",
}

# Words that appear in job title text (to score anchor text)
_JOB_TITLE_WORDS = {
    "engineer", "developer", "manager", "analyst", "designer",
    "scientist", "lead", "architect", "intern", "associate",
    "specialist", "consultant", "director", "coordinator",
    "recruiter", "marketing", "product", "operations",
    "senior", "junior", "staff", "principal",
}

# Anchors whose text matches these patterns are almost certainly not jobs
_NOISE_PATTERNS = re.compile(
    r"(privacy|cookie|terms|login|sign.?in|sign.?up|about|blog|contact|"
    r"home|faq|help|support|tweet|share|back|next|previous|more|all|see)",
    re.IGNORECASE,
)


def _base_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _resolve(href: str, page_url: str) -> str | None:
    if not href:
        return None
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return _base_url(page_url) + href
    return None


def _score_link(text: str, href: str) -> int:
    """Return a relevance score; higher = more likely a job link."""
    score = 0
    href_lower = href.lower()
    text_lower = text.lower().strip()

    if not text_lower or len(text_lower) < 4 or len(text_lower) > 150:
        return 0
    if _NOISE_PATTERNS.search(text_lower):
        return 0

    for kw in _JOB_URL_KEYWORDS:
        if kw in href_lower:
            score += 2

    for word in _JOB_TITLE_WORDS:
        if word in text_lower:
            score += 3

    if any(char.isdigit() for char in href):
        score += 1  # numeric ID in URL suggests a specific job

    return score


class UniversalCrawler:
    """Crawls any URL to extract job listings."""

    # ── Public API ─────────────────────────────────────────────────────────────

    async def crawl(
        self,
        url: str,
        role: str = "",
        limit: int = 30,
        headless: bool = True,
    ) -> list[dict]:
        """Crawl a careers page or job board URL and return job listings.

        Manages its own browser. For Playwright pages already open, use
        discover_jobs_on_page() directly.
        """
        from playwright.async_api import async_playwright
        from app.config import get_settings
        s = get_settings()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=headless,
                args=["--no-first-run", "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=s.BROWSER_UA,
            )
            page = await context.new_page()
            try:
                from app.scraping.drivers import _apply_stealth
                await _apply_stealth(page)
            except Exception as _e:
                logger.debug("Failed to apply stealth in universal_crawler: %s", _e)
            try:
                jobs = await self.discover_jobs_on_page(page, url, limit=limit)
                return jobs
            finally:
                await browser.close()

    async def discover_jobs_on_page(
        self,
        page,
        url: str,
        hint_selectors: list[str] | None = None,
        next_selectors: list[str] | None = None,
        scroll_to_load: bool = False,
        limit: int = 30,
        source: str = "crawl",
    ) -> list[dict]:
        """Discover job links on *url* using an already-open Playwright page.

        Parameters
        ----------
        page            Live Playwright page object.
        url             URL to navigate to.
        hint_selectors  Board-specific CSS selectors for job links (optional).
        next_selectors  CSS selectors for the next-page control (optional).
        scroll_to_load  True for infinite-scroll boards.
        limit           Max number of jobs to return.
        source          String written to the 'source' field of each job dict.
        """
        from app.config import get_settings
        s = get_settings()
        logger.info("UniversalCrawler: navigating to %s", url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=s.BROWSER_TIMEOUT_MS + 15_000)
            await page.wait_for_timeout(s.BROWSER_LONG_WAIT_MS)
        except Exception as exc:
            logger.error("UniversalCrawler: navigation failed for %s: %s", url, exc)
            return []

        jobs: list[dict] = []
        seen_urls: set[str] = set()
        page_num = 0

        while len(jobs) < limit and page_num < s.CRAWLER_PAGE_LIMIT:
            links = await self._collect_links(page, url, hint_selectors or [])

            for title, href in links:
                if len(jobs) >= limit:
                    break
                if href in seen_urls:
                    continue
                seen_urls.add(href)
                jobs.append(_make_job_dict(title, href, source))

            if scroll_to_load:
                prev = len(jobs)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(s.BROWSER_MEDIUM_WAIT_MS)
                page_num += 1
                if len(jobs) == prev and page_num > 2:
                    break
                continue

            next_btn = await self._find_next_button(page, next_selectors or [])
            if not next_btn:
                break
            try:
                await next_btn.click()
                await page.wait_for_timeout(s.BROWSER_LONG_WAIT_MS)
                page_num += 1
            except Exception:
                break

        logger.info("UniversalCrawler: discovered %d jobs at %s", len(jobs), url)
        return jobs

    # ── Internals ──────────────────────────────────────────────────────────────

    async def _collect_links(
        self, page, page_url: str, hints: list[str]
    ) -> list[tuple[str, str]]:
        """Return (title, href) pairs for job links on the current page."""
        result: dict[str, str] = {}  # href → title

        async def _try_selectors(selectors: list[str]) -> bool:
            found = False
            for sel in selectors:
                try:
                    elements = await page.query_selector_all(sel)
                    for el in elements:
                        text = (await el.inner_text()).strip()
                        href = await el.get_attribute("href") or ""
                        full = _resolve(href, page_url)
                        if full and text and full not in result:
                            result[full] = text
                            found = True
                except Exception:
                    pass
            return found

        # 1. Hint selectors
        found = await _try_selectors(hints) if hints else False

        # 2. Heuristic selectors
        if not found:
            found = await _try_selectors(_HEURISTIC_SELECTORS)

        # 3. JS link scoring fallback
        if not found:
            try:
                raw = await page.evaluate("""() => {
                    const out = [];
                    document.querySelectorAll('a[href]').forEach(a => {
                        out.push({
                            text: a.innerText.trim(),
                            href: a.getAttribute('href') || ''
                        });
                    });
                    return out;
                }""")
                scored: list[tuple[int, str, str]] = []
                for item in (raw or []):
                    text = item.get("text", "").strip()
                    href = item.get("href", "")
                    full = _resolve(href, page_url)
                    if not full:
                        continue
                    score = _score_link(text, href)
                    if score >= 3:
                        scored.append((score, text, full))
                scored.sort(reverse=True, key=lambda x: x[0])
                for _, text, href in scored:
                    if href not in result:
                        result[href] = text
            except Exception:
                pass

        return [(title, href) for href, title in result.items()]

    async def _find_next_button(self, page, selectors: list[str]):
        """Return the first visible next-page element, or None."""
        all_selectors = selectors + [
            "a[aria-label='Next'], a[aria-label='Next page']",
            "button[aria-label='Next'], button[aria-label='Next page']",
            "a:text-is('Next'), a:text-is('Next →'), a:text-is('›')",
            "[class*='pagination'] a:last-child",
        ]
        for sel in all_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:
                pass
        return None


def _make_job_dict(title: str, url: str, source: str) -> dict:
    return {
        "title": title,
        "company": "",
        "location": "",
        "job_url": url,
        "source": source,
        "id": None,
        "description": "",
        "salary_min": None,
        "salary_max": None,
        "currency": None,
        "date_posted": None,
        "raw_data": "",
    }
