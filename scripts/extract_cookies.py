#!/usr/bin/env python3
"""Extract job board cookies from running Chrome via CDP.

Which boards are extracted is driven entirely by config/job_boards.yaml.
Add a new board with requires_login: true — this script picks it up
automatically with no code changes.

Chrome decrypts cookies itself and hands them back — no Keychain needed.

Steps:
  1. Run:  scripts/start_chrome_debug.sh   (opens Chrome with debug port)
  2. Log into each board listed in job_boards.yaml in that Chrome window
  3. Run:  .venv/bin/python scripts/extract_cookies.py
  4. Done — cookies saved, Playwright uses them automatically

Re-run step 3 whenever sessions expire (~30 days).
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CDP_URL       = "http://localhost:9222"
SAVE_DIR      = Path.home() / ".auto-jobsearch" / "sessions"
BOARDS_CONFIG = Path(__file__).parents[1] / "config" / "job_boards.yaml"


def _load_auth_boards() -> list[tuple[str, str, list[str]]]:
    """Read job_boards.yaml and return boards that need cookie auth.

    Returns list of (key, name, domain_list).
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
        board_name = cfg.get("name", key)

        if home_url:
            netloc  = urlparse(home_url).netloc
            domains = [netloc, f".{netloc}"]
        else:
            domains = []

        if domains:
            boards.append((key, board_name, domains))

    # Hardcoded fallback if YAML gave nothing
    if not boards:
        boards = [
            ("naukri",    "Naukri",    [".naukri.com", "naukri.com"]),
            ("instahyre", "Instahyre", [".instahyre.com", "instahyre.com"]),
            ("wellfound", "Wellfound", [".wellfound.com", "wellfound.com", ".angel.co"]),
        ]

    return boards


async def get_all_cookies_via_cdp(boards: list[tuple[str, str, list[str]]]) -> list[dict]:
    """Extract all cookies from Chrome by connecting to an actual page tab."""
    import aiohttp
    import websockets

    timeout = aiohttp.ClientTimeout(total=5)

    # Get all open tabs
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{CDP_URL}/json", timeout=timeout) as r:
            tabs = await r.json(content_type=None)

    # Pick a real page tab (not devtools/extension tabs)
    page_tabs = [t for t in tabs if t.get("type") == "page" and "webSocketDebuggerUrl" in t]
    if not page_tabs:
        raise RuntimeError("No page tabs found. Open at least one regular tab in Chrome.")

    # Prefer a tab that's on one of our target board domains, otherwise use any tab
    board_domains = {d.lstrip(".") for _, _, domains in boards for d in domains}
    target_tab = next(
        (t for t in page_tabs if any(d in t.get("url", "") for d in board_domains)),
        page_tabs[0]
    )
    ws_url = target_tab["webSocketDebuggerUrl"]
    print(f"  Using tab: {target_tab.get('title','?')[:50]}")

    async with websockets.connect(ws_url, ping_interval=None) as ws:
        # Enable Network domain (required before getAllCookies on some Chrome versions)
        await ws.send(json.dumps({"id": 1, "method": "Network.enable", "params": {}}))

        # Use Storage.getCookies — more reliable than Network.getAllCookies
        await ws.send(json.dumps({"id": 2, "method": "Storage.getCookies", "params": {}}))

        cookies = []
        got_enable = False
        deadline = asyncio.get_event_loop().time() + 10
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                break
            if msg.get("id") == 1:
                got_enable = True
            elif msg.get("id") == 2:
                cookies = msg.get("result", {}).get("cookies", [])
                break

        # Fallback: try Network.getAllCookies if Storage.getCookies gave nothing
        if not cookies:
            await ws.send(json.dumps({"id": 3, "method": "Network.getAllCookies", "params": {}}))
            deadline = asyncio.get_event_loop().time() + 5
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                except asyncio.TimeoutError:
                    break
                if msg.get("id") == 3:
                    cookies = msg.get("result", {}).get("cookies", [])
                    break

        return cookies


def to_playwright_format(cookies: list[dict], domains: list[str]) -> list[dict]:
    """Filter cookies by domain and convert to Playwright's add_cookies() format."""
    out = []
    for c in cookies:
        if any(d in c.get("domain", "") for d in domains):
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
            out.append(pw_cookie)
    return out


async def main():
    SAVE_DIR.mkdir(parents=True, exist_ok=True)

    boards = _load_auth_boards()
    print(f"Extracting cookies for {len(boards)} board(s) from job_boards.yaml:")
    for key, name, domains in boards:
        print(f"  {name}  ({domains[0]})")
    print()

    print(f"Connecting to Chrome at {CDP_URL}...")
    try:
        all_cookies = await get_all_cookies_via_cdp(boards)
    except Exception as e:
        print(f"\nFAILED: {e}")
        print("\nMake sure Chrome is running with the debug port:")
        print("  scripts/start_chrome_debug.sh")
        return

    print(f"Got {len(all_cookies)} total cookies from Chrome\n")

    for key, name, domains in boards:
        filtered = to_playwright_format(all_cookies, domains)
        if not filtered:
            print(f"  {name}: 0 cookies — are you logged in at {domains[0]}?")
            continue
        out = SAVE_DIR / f"{key}_cookies.json"
        out.write_text(json.dumps(filtered, indent=2))
        print(f"  {name}: {len(filtered)} cookies saved → {out}")

    print("\nDone. Playwright will inject these cookies automatically.")
    print("Re-run this script if you see login errors (~30 days).")


if __name__ == "__main__":
    asyncio.run(main())
