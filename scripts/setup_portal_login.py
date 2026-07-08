#!/usr/bin/env python3
"""One-time setup: log into any job board that requires authentication.

Which boards are opened is driven entirely by config/job_boards.yaml.
Add a new board with requires_login: true there — this script picks it up
automatically with no code changes.

Run this ONCE per board (or whenever sessions expire, ~30 days):

    .venv/bin/python scripts/setup_portal_login.py

Log into each site in the browser window that opens, then press Ctrl+C.
Cookies are exported automatically to ~/.auto-jobsearch/sessions/.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PROFILE_DIR = os.path.expanduser("~/.auto-jobsearch/chrome-debug")
SAVE_DIR    = Path.home() / ".auto-jobsearch" / "sessions"
BOARDS_CONFIG = Path(__file__).parents[1] / "config" / "job_boards.yaml"


def _load_auth_boards() -> list[tuple[str, str, str, list[str]]]:
    """Read job_boards.yaml and return boards that need cookie auth.

    Returns list of (key, name, login_url, domain_list).
    Falls back to hardcoded defaults if YAML is missing or unparseable.
    """
    try:
        import yaml
        data = yaml.safe_load(BOARDS_CONFIG.read_text()) or {}
    except ImportError:
        print("Warning: pyyaml not installed. Run: pip install pyyaml")
        data = {}
    except Exception as exc:
        print(f"Warning: could not read job_boards.yaml: {exc}")
        data = {}

    boards = []
    for key, cfg in (data.get("boards") or {}).items():
        if not cfg.get("enabled", True):
            continue
        if not cfg.get("requires_login", False):
            continue
        if cfg.get("auth_method", "none") != "cookie":
            continue

        home_url   = cfg.get("home_url", "")
        login_url  = cfg.get("login_url") or home_url  # boards can add a login_url field
        board_name = cfg.get("name", key)

        # Derive cookie domains from home_url
        if home_url:
            from urllib.parse import urlparse
            netloc = urlparse(home_url).netloc
            domains = [netloc, f".{netloc}"]
        else:
            domains = []

        if login_url:
            boards.append((key, board_name, login_url, domains))

    # Hardcoded fallback if YAML gave nothing (keeps script usable before YAML is set up)
    if not boards:
        boards = [
            ("naukri",    "Naukri",    "https://www.naukri.com/nlogin/login",    [".naukri.com", "naukri.com"]),
            ("instahyre", "Instahyre", "https://www.instahyre.com/login/",        [".instahyre.com", "instahyre.com"]),
            ("wellfound", "Wellfound", "https://wellfound.com/login",             [".wellfound.com", "wellfound.com", ".angel.co"]),
        ]

    return boards


async def _export_cookies(context, boards: list[tuple[str, str, str, list[str]]]) -> None:
    """Save per-site cookies from the Playwright context to JSON files."""
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    all_cookies = await context.cookies()
    print(f"\nExporting cookies ({len(all_cookies)} total from browser)...")

    for key, name, _, domains in boards:
        site_cookies = [
            c for c in all_cookies
            if any(d in c.get("domain", "") for d in domains)
        ]
        if not site_cookies:
            print(f"  {name}: 0 cookies — are you logged in at {domains[0] if domains else '?'}?")
            continue

        pw_cookies = []
        for c in site_cookies:
            pw_cookie = {
                "name":     c["name"],
                "value":    c["value"],
                "domain":   c["domain"],
                "path":     c.get("path", "/"),
                "secure":   c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
                "sameSite": c.get("sameSite", "Lax"),
            }
            if c.get("expires") and c["expires"] > 0:
                pw_cookie["expires"] = int(c["expires"])
            pw_cookies.append(pw_cookie)

        out = SAVE_DIR / f"{key}_cookies.json"
        out.write_text(json.dumps(pw_cookies, indent=2))
        print(f"  {name}: {len(pw_cookies)} cookies → {out}")


async def main() -> None:
    from playwright.async_api import async_playwright

    boards = _load_auth_boards()
    if not boards:
        print("No boards with requires_login: true found in job_boards.yaml.")
        return

    print(f"Opening browser for {len(boards)} board(s) that need login:")
    for _, name, login_url, _ in boards:
        print(f"  {name}  →  {login_url}")
    print("\nLog into each tab via Google (or email/password), then press Ctrl+C.\n")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--no-first-run", "--no-default-browser-check"],
        )

        for _, name, login_url, _ in boards:
            p = await context.new_page()
            await p.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
            print(f"  Opened {name}")

        print("\nPress Ctrl+C when all tabs are logged in — cookies save automatically.")

        try:
            while True:
                await asyncio.sleep(5)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

        print("\nSaving sessions...")
        await _export_cookies(context, boards)
        await context.close()
        print("\nDone. Verify with:  .venv/bin/python scripts/test_portal_connection.py")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
