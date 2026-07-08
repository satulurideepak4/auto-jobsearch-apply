"""Native Chrome integration — open tabs, detect logins, read cookies.

Works with the user's already-running Chrome (same profile, same window).
No Playwright needed for the login step. Uses:
  - macOS `open -a` to open a new tab in the existing Chrome process
  - Chrome's SQLite History DB to detect when login completes (no decryption)
  - Chrome's SQLite Cookies DB + macOS Keychain to extract auth cookies
  - AppleScript to close just the portal tab when done

macOS primary; Windows/Linux stubs return sensible no-ops.
"""
from __future__ import annotations

import logging
import platform
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
_SYSTEM = platform.system()

# Chrome Windows epoch offset: microseconds between 1601-01-01 and 1970-01-01
_CHROME_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000


# ── Chrome data directory ────────────────────────────────────────────────────

def chrome_data_dir() -> Path:
    if _SYSTEM == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    if _SYSTEM == "Windows":
        return Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    return Path.home() / ".config" / "google-chrome"


# ── All Chrome profile directories ──────────────────────────────────────────

def all_profile_dirs() -> list[Path]:
    """Return all Chrome profile directories that contain a Cookies file."""
    base = chrome_data_dir()
    if not base.exists():
        return []
    dirs = []
    try:
        for p in base.iterdir():
            if p.is_dir() and (p / "Cookies").exists():
                dirs.append(p)
    except Exception as exc:
        logger.warning("all_profile_dirs failed: %s", exc)
    return dirs


# ── Open URL in the user's running Chrome ───────────────────────────────────

def open_url_in_chrome(url: str) -> bool:
    """Open url in a new tab inside the user's current Chrome process.

    Returns True if the command was dispatched (no guarantee the tab opened).
    """
    try:
        if _SYSTEM == "Darwin":
            # 'open -a' routes to the existing Chrome app without starting a second instance
            subprocess.run(["open", "-a", "Google Chrome", url], check=True, timeout=10)
        elif _SYSTEM == "Windows":
            subprocess.Popen(["start", "chrome", "--", url], shell=True)
        else:
            subprocess.Popen(["xdg-open", url])
        return True
    except Exception as exc:
        logger.error("open_url_in_chrome failed: %s", exc)
        return False


# ── Close a specific Chrome tab ─────────────────────────────────────────────

def close_chrome_tab(domain: str) -> None:
    """Close the Chrome tab whose URL contains domain (macOS AppleScript)."""
    if _SYSTEM != "Darwin":
        return
    safe = domain.replace('"', "")  # basic sanitisation for the AppleScript string
    script = f"""
tell application "Google Chrome"
    repeat with w in windows
        set tab_list to (get tabs of w)
        repeat with t in tab_list
            if URL of t contains "{safe}" then
                close t
                return
            end if
        end repeat
    end repeat
end tell
"""
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception as exc:
        logger.warning("close_chrome_tab failed: %s", exc)


# ── Read Chrome History (no encryption) ─────────────────────────────────────

def unix_to_chrome_epoch(unix_ts: float) -> int:
    """Convert a Unix timestamp (seconds) to Chrome's Windows epoch (microseconds)."""
    return int(unix_ts * 1_000_000) + _CHROME_EPOCH_OFFSET_US


def read_history_urls(profile_dir: Path, domain: str, after_chrome_epoch: int) -> list[str]:
    """Return URLs from Chrome's History for domain visited after after_chrome_epoch.

    WHY direct read instead of copy: Chrome writes to History-wal (SQLite WAL file).
    Copying only the main History file misses all recent writes — they live in the WAL
    until Chrome checkpoints them.  Opening the live file in mode=ro is safe because
    SQLite WAL mode explicitly allows concurrent read-only connections alongside the writer.
    Readers always see a consistent snapshot of all committed transactions including
    those in the WAL.
    """
    history_db = profile_dir / "History"
    if not history_db.exists():
        logger.warning("Chrome History not found at %s", history_db)
        return []

    try:
        # Path.as_uri() percent-encodes spaces (e.g. "Application Support", "Profile 9")
        # which are invalid in SQLite URIs.  f"file:{path}" leaves spaces raw and fails silently.
        conn = sqlite3.connect(history_db.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT url FROM urls WHERE url LIKE ? AND last_visit_time > ? "
            "ORDER BY last_visit_time DESC LIMIT 30",
            (f"%{domain}%", after_chrome_epoch),
        )
        rows = [row[0] for row in cursor.fetchall()]
        conn.close()
        return rows
    except Exception as exc:
        logger.warning("read_history_urls failed: %s", exc)
        return []


# ── Read & decrypt Chrome Cookies ────────────────────────────────────────────

_keychain_key_cache: Optional[bytes] = None
_keychain_key_fetched: bool = False


def _macos_keychain_key() -> Optional[bytes]:
    """Derive Chrome's AES cookie encryption key from the macOS Keychain entry.

    Cached after the first call — triggers at most ONE macOS Keychain dialog
    per server process lifetime.
    """
    global _keychain_key_cache, _keychain_key_fetched
    if _keychain_key_fetched:
        return _keychain_key_cache

    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-wa", "Chrome", "-s", "Chrome Safe Storage"],
            capture_output=True, text=True, timeout=30,
        )
        password = result.stdout.strip()
        if not password:
            logger.warning("Chrome Safe Storage key not found in Keychain")
            _keychain_key_fetched = True
            return None

        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.backends import default_backend

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA1(),
            length=16,
            salt=b"saltysalt",
            iterations=1003,
            backend=default_backend(),
        )
        _keychain_key_cache = kdf.derive(password.encode())
        logger.info("Chrome Safe Storage key loaded from Keychain")
    except Exception as exc:
        logger.warning("_macos_keychain_key failed: %s", exc)
        _keychain_key_cache = None
    finally:
        _keychain_key_fetched = True

    return _keychain_key_cache


def _decrypt_v10(encrypted_value: bytes, key: bytes) -> str:
    """Decrypt a Chrome v10 AES-CBC-128 cookie value (macOS format)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    ciphertext = encrypted_value[3:]   # strip b"v10" prefix
    iv = b" " * 16                     # Chrome uses 16 space bytes as IV
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decrypted = cipher.decryptor().update(ciphertext)
    pad = decrypted[-1]                # PKCS7 padding
    return decrypted[:-pad].decode("utf-8", errors="replace")


def read_cookie_names_count(profile_dir: Path, domain: str) -> tuple[set, int, int]:
    """Read ONLY cookie names, total count, and secure+httponly count — no decryption, no Keychain.

    Returns (names, total_count, secure_httponly_count).
    secure_httponly_count counts only is_secure=1 AND is_httponly=1 cookies,
    which are the auth session cookies — never triggers a macOS Keychain prompt.
    """
    cookies_db = profile_dir / "Cookies"
    if not cookies_db.exists():
        return set(), 0, 0
    try:
        conn = sqlite3.connect(cookies_db.as_uri() + "?mode=ro", uri=True)
        cursor = conn.execute(
            "SELECT name, is_secure, is_httponly FROM cookies WHERE host_key LIKE ?",
            (f"%{domain}%",),
        )
        rows = cursor.fetchall()
        conn.close()
        names = {row[0] for row in rows}
        secure_httponly = sum(1 for row in rows if row[1] and row[2])
        return names, len(names), secure_httponly
    except Exception as exc:
        logger.warning("read_cookie_names_count failed: %s", exc)
        return set(), 0, 0


def read_cookies_for_domain(profile_dir: Path, domain: str) -> list[dict]:
    """Read and decrypt Chrome cookies for domain from the profile's Cookies SQLite file.

    Returns a list of cookie dicts in Playwright storage_state format.

    WHY direct read: same WAL reason as read_history_urls — auth cookies written during
    login live in Cookies-wal until Chrome checkpoints.  mode=ro reads them correctly.
    """
    cookies_db = profile_dir / "Cookies"
    if not cookies_db.exists():
        logger.warning("Chrome Cookies DB not found at %s", cookies_db)
        return []

    key: Optional[bytes] = None
    if _SYSTEM == "Darwin":
        key = _macos_keychain_key()

    try:
        conn = sqlite3.connect(cookies_db.as_uri() + "?mode=ro", uri=True)
        cursor = conn.execute(
            "SELECT host_key, name, encrypted_value, path, is_secure, "
            "expires_utc, is_httponly, samesite "
            "FROM cookies WHERE host_key LIKE ?",
            (f"%{domain}%",),
        )

        samesite_map = {-1: "None", 0: "None", 1: "Lax", 2: "Strict"}
        result = []

        for host, name, enc_val, path, is_secure, expires_utc, is_httponly, samesite in cursor:
            try:
                if isinstance(enc_val, bytes) and enc_val[:3] == b"v10" and key:
                    value = _decrypt_v10(enc_val, key)
                elif isinstance(enc_val, bytes):
                    value = enc_val.decode("utf-8", errors="replace")
                else:
                    value = str(enc_val)

                # Chrome epoch → Unix epoch (seconds); None/0 = session cookie → -1
                expires_unix = (
                    (expires_utc - _CHROME_EPOCH_OFFSET_US) / 1_000_000
                    if (expires_utc and expires_utc > 0) else -1
                )

                result.append({
                    "name": name,
                    "value": value,
                    "domain": host,
                    "path": path or "/",
                    "secure": bool(is_secure),
                    "httpOnly": bool(is_httponly),
                    "expires": expires_unix,
                    "sameSite": samesite_map.get(samesite, "Lax"),
                })
            except Exception as exc:
                logger.debug("Cookie decrypt error for %s/%s: %s", host, name, exc)

        conn.close()
        return result
    except Exception as exc:
        logger.warning("read_cookies_for_domain failed: %s", exc)
        return []
