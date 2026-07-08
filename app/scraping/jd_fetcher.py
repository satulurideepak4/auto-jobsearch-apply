from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.llm.base import LLMProvider

logger = logging.getLogger(__name__)

# Strip non-content DOM nodes before reading innerText so the LLM sees
# a clean article-style block instead of nav/footer/cookie-banner noise.
_STRIP_AND_EXTRACT_JS = """() => {
    document.querySelectorAll(
        'script, style, noscript, nav, footer, header, [role="banner"], [role="navigation"]'
    ).forEach(el => el.remove());
    return document.body ? document.body.innerText : '';
}"""


async def fetch_jd_from_url(url: str, llm: "LLMProvider") -> dict:
    """Render a job posting URL with Playwright and extract structured JD data via LLM.

    Runs Playwright headless to handle JS-rendered pages (Greenhouse, Lever, Ashby,
    Workday, company career sites). The raw page text is passed to the LLM to extract
    a normalised job dict.

    Note: Pages that require login (e.g. LinkedIn) will return partial content; the
    LLM extraction will still attempt a best-effort parse.

    Returns:
        Normalised job dict with keys:
            title, company, location, description, job_url, source
    """
    from playwright.async_api import async_playwright

    page_text = ""
    page_title = ""

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            # Extra settle time for JS-heavy ATS pages that lazy-render the JD
            await page.wait_for_timeout(2_000)
            page_title = await page.title()
            page_text = await page.evaluate(_STRIP_AND_EXTRACT_JS)
        except Exception as exc:
            logger.error("Playwright failed to load %s: %s", url, exc)
            raise
        finally:
            await browser.close()

    # Keep the first 8 000 chars — enough for any JD, cheap for the LLM
    excerpt = page_text[:8_000]

    prompt = f"""Extract structured job posting information from the web page text below.

Page title : {page_title}
Page URL   : {url}

Page content:
{excerpt}

Return a single JSON object with EXACTLY these keys:
- "title"       : string — the job title (e.g. "Senior Software Engineer")
- "company"     : string — the hiring company name
- "location"    : string — job location or "Remote" if fully remote
- "description" : string — the complete job description including all responsibilities,
                  requirements and nice-to-haves; preserve all details, do not summarise

If a field cannot be determined from the page text, use an empty string.
Return ONLY the JSON object. No markdown fences, no extra text.
"""

    raw = llm.generate(prompt)
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("JD extraction JSON parse failed: %s — raw snippet: %.300s", exc, raw)
        data = {
            "title": page_title,
            "company": "",
            "location": "",
            "description": excerpt,
        }

    return {
        "title": data.get("title") or page_title,
        "company": data.get("company", ""),
        "location": data.get("location", ""),
        "description": data.get("description", ""),
        "job_url": url,
        "source": "direct_url",
    }
