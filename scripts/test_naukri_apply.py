#!/usr/bin/env python3
"""Test Naukri search + apply using extracted Chrome cookies.

Setup (run once from your Terminal):
    .venv/bin/python scripts/extract_cookies.py

Then test:
    .venv/bin/python scripts/test_naukri_apply.py
"""
import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

COOKIES_FILE = Path.home() / ".auto-jobsearch/sessions/naukri_cookies.json"


async def main():
    from playwright.async_api import async_playwright

    if not COOKIES_FILE.exists():
        print("No cookies found. Run this first from your Terminal:")
        print("  .venv/bin/python scripts/extract_cookies.py")
        return

    cookies = json.loads(COOKIES_FILE.read_text())
    print(f"Loaded {len(cookies)} Naukri cookies")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--no-first-run", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
        )
        await context.add_cookies(cookies)
        page = await context.new_page()

        # ── Verify login ──────────────────────────────────────────────────
        print("Navigating to Naukri...")
        await page.goto("https://www.naukri.com", wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3_000)

        # Naukri redirects to /mnjuser/homepage when logged in
        logged_in = (
            "/mnjuser/" in page.url
            or await page.query_selector("a[href*='/mnjuser/'], [class*='nI-gNb-icon__useravtar']")
        )
        if not logged_in:
            print("Session expired — log into Naukri in Chrome and re-run extract_cookies.py")
            await browser.close()
            return

        print("Logged in to Naukri ✓")

        # ── Search — scan listings for a native apply job ─────────────────
        search_url = "https://www.naukri.com/java-developer-jobs?experience=0"
        print(f"Searching: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3_000)

        job_links = await page.query_selector_all(
            "article.jobTuple a.title, .jobTupleHeader a, "
            "[class*='job-title'] a, .row1 a, a[href*='/job-listings/']"
        )
        if not job_links:
            print("No jobs found — screenshot at /tmp/naukri_search.png")
            await page.screenshot(path="/tmp/naukri_search.png")
            await browser.close()
            return

        print(f"Found {len(job_links)} listings — scanning for native apply button...")

        native_job = None
        job_page = await context.new_page()

        for i, link in enumerate(job_links[:15]):
            try:
                title = (await link.inner_text()).strip()
                href  = await link.get_attribute("href") or ""
                if not href.startswith("http"):
                    href = f"https://www.naukri.com{href}"

                await job_page.goto(href, wait_until="domcontentloaded", timeout=30_000)
                await job_page.wait_for_timeout(2_000)

                apply_btn = await job_page.query_selector(
                    "#apply-button, button#apply-button, a#apply-button, "
                    "[class*='apply-button'], [class*='applyBtn']"
                )
                if not apply_btn:
                    print(f"  [{i+1}] {title[:50]} — no button, skipping")
                    continue

                btn_text = (await apply_btn.inner_text()).strip().replace("\n", " ")
                is_external = "company site" in btn_text.lower()
                print(f"  [{i+1}] {title[:50]} — '{btn_text}' {'(external)' if is_external else '(NATIVE ✓)'}")

                if not is_external:
                    native_job = (title, href, apply_btn)
                    break
            except Exception as e:
                print(f"  [{i+1}] error: {e}")

        if not native_job:
            print("\nAll scanned jobs redirect to external ATS — try a different search term.")
            await browser.close()
            return

        title, href, apply_btn = native_job
        print(f"\nApplying natively to: {title}")
        print(f"URL: {href}")
        await job_page.screenshot(path="/tmp/naukri_before_apply.png")
        print("Screenshot (before): /tmp/naukri_before_apply.png")

        # ── Native 1-click apply ──────────────────────────────────────────
        print("Clicking Apply...")
        await apply_btn.click()
        await job_page.wait_for_timeout(3_000)
        await job_page.screenshot(path="/tmp/naukri_after_apply.png")
        print("Screenshot (after click): /tmp/naukri_after_apply.png")

        # Handle confirm dialog / quick questions
        confirm = await job_page.query_selector(
            "button:text-is('Apply'), button:text-is('Confirm'), "
            "button[class*='applyBtn'], #confirmApply"
        )
        if confirm:
            confirm_text = (await confirm.inner_text()).strip()
            print(f"Confirm dialog appeared — clicking '{confirm_text}'")
            await confirm.click()
            await job_page.wait_for_timeout(2_500)
            await job_page.screenshot(path="/tmp/naukri_confirmed.png")
            print("Screenshot (confirmed): /tmp/naukri_confirmed.png")

        # Check for success indicator
        success = await job_page.query_selector(
            "[class*='success'], [class*='applied'], #applySuccess, "
            "h3:text-matches('applied', 'i'), [class*='thankYou']"
        )
        if success:
            msg = (await success.inner_text()).strip()
            print(f"\nSUCCESS: Applied! Message: '{msg}'")
        else:
            print("\nApply flow completed — check screenshots for result")

        print("\nLeaving open 15s so you can see the result...")
        await asyncio.sleep(15)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
