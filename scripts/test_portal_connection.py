#!/usr/bin/env python3
"""Test that Playwright can connect to Chrome and see logged-in sessions.

Run AFTER scripts/start_chrome_debug.sh:
    .venv/bin/python scripts/test_portal_connection.py
"""
from __future__ import annotations
import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


async def main():
    from playwright.async_api import async_playwright

    CDP_URL = "http://localhost:9222"

    print("Connecting to Chrome via CDP...")
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=5_000)
        except Exception as exc:
            print(f"\nFAILED — Could not connect to Chrome on {CDP_URL}")
            print(f"Error: {exc}")
            print("\nFix: run   scripts/start_chrome_debug.sh   first, then retry.")
            return

        contexts = browser.contexts
        print(f"Connected! Browser contexts: {len(contexts)}")

        ctx = contexts[0] if contexts else await browser.new_context()
        page = await ctx.new_page()

        portals = [
            ("Naukri",     "https://www.naukri.com",     _check_naukri),
            ("Instahyre",  "https://www.instahyre.com",  _check_instahyre),
            ("Wellfound",  "https://wellfound.com",       _check_wellfound),
        ]

        results = {}
        for name, url, checker in portals:
            print(f"\nChecking {name} ({url})...")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(2_500)
                logged_in = await checker(page)
                status = "LOGGED IN" if logged_in else "NOT logged in"
                results[name] = logged_in
                print(f"  {name}: {status}")
            except Exception as exc:
                print(f"  {name}: ERROR — {exc}")
                results[name] = False

        await page.close()

        print("\n" + "="*50)
        print("SUMMARY")
        print("="*50)
        all_good = True
        for name, ok in results.items():
            mark = "✓" if ok else "✗"
            print(f"  {mark}  {name}: {'ready' if ok else 'needs login'}")
            if not ok:
                all_good = False

        if all_good:
            print("\nAll portals connected — scraper is ready to run.")
        else:
            print(
                "\nFor portals showing 'NOT logged in':\n"
                "  1. In the Chrome debug window, open the portal URL\n"
                "  2. Click Sign in with Google\n"
                "  3. Re-run this script to verify"
            )


async def _check_naukri(page) -> bool:
    # Naukri shows avatar/profile icon when logged in
    el = await page.query_selector(
        ".nI-gNb-icon__useravtar, [class*='nI-gNb-bell'], [data-ga-track*='myNaukri']"
    )
    return el is not None


async def _check_instahyre(page) -> bool:
    el = await page.query_selector(
        "a[href*='/candidate/profile'], [class*='user-avatar'], [class*='navbar-profile']"
    )
    return el is not None


async def _check_wellfound(page) -> bool:
    # Not-logged-in shows "Log In" button; logged in shows user menu
    login_btn = await page.query_selector("a:text-is('Log In'), a:text-is('Sign In')")
    return login_btn is None


if __name__ == "__main__":
    asyncio.run(main())
