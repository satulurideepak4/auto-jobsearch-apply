"""Portal session management — save/load/validate Playwright storage_state."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SESSION_DIR = Path.home() / ".auto-jobsearch" / "sessions"
_MAX_AGE_DAYS = 60  # storage_state with Google OAuth tokens valid ~60-90 days


def _state_path(portal: str) -> Path:
    _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return _SESSION_DIR / f"{portal}_storage_state.json"


def _legacy_cookies_path(portal: str) -> Path:
    return _SESSION_DIR / f"{portal}_cookies.json"


def save_storage_state(portal: str, state: dict) -> None:
    """Persist Playwright storage_state dict to disk with a timestamp."""
    import math
    path = _state_path(portal)
    to_save = {k: v for k, v in state.items()}
    to_save["_saved_at"] = datetime.now(timezone.utc).isoformat()
    # Sanitize cookie expires: json.dumps rejects NaN/Inf (can come from Chrome's max-date cookies)
    for cookie in to_save.get("cookies", []):
        exp = cookie.get("expires")
        if isinstance(exp, float) and (math.isnan(exp) or math.isinf(exp)):
            cookie["expires"] = -1
    path.write_text(json.dumps(to_save, indent=2))
    logger.info(
        "Saved storage_state for %s (%d cookies, %d origins)",
        portal, len(state.get("cookies", [])), len(state.get("origins", [])),
    )


def load_storage_state(portal: str) -> Optional[dict]:
    """Load storage_state for Playwright new_context(storage_state=...).

    Returns a dict (cookies + localStorage) or None if no usable session exists.
    Falls back to old cookies-only format for backward compatibility.
    """
    path = _state_path(portal)

    if path.exists():
        try:
            raw = json.loads(path.read_text())
            if raw.get("cookies") or raw.get("origins"):
                # Strip our private metadata keys before passing to Playwright
                return {k: v for k, v in raw.items() if not k.startswith("_")}
        except Exception as exc:
            logger.warning("Corrupt storage_state for %s: %s", portal, exc)

    # Backward-compat: old cookies-only file
    legacy = _legacy_cookies_path(portal)
    if legacy.exists():
        try:
            cookies = json.loads(legacy.read_text())
            if cookies:
                logger.info("Using legacy cookies for %s (upgrading on next login)", portal)
                return {"cookies": cookies, "origins": []}
        except Exception:
            pass

    return None


def session_info(portal: str) -> dict:
    """Return session metadata dict.

    Keys: status, age_days, saved_at, expires_in_days, ready, cookie_count, session_file
    status values: missing | empty | corrupt | present | expired
    """
    path = _state_path(portal)
    legacy = _legacy_cookies_path(portal)

    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except Exception:
            return {
                "status": "corrupt", "age_days": None, "saved_at": None,
                "expires_in_days": None, "ready": False, "cookie_count": 0,
                "session_file": str(path),
            }

        cookies = raw.get("cookies", [])
        origins = raw.get("origins", [])

        if not cookies and not origins:
            return {
                "status": "empty", "age_days": None, "saved_at": None,
                "expires_in_days": None, "ready": False, "cookie_count": 0,
                "session_file": str(path),
            }

        saved_at_str = raw.get("_saved_at")
        age_days = None
        expires_in = None

        if saved_at_str:
            try:
                saved_at = datetime.fromisoformat(saved_at_str)
                age_days = (datetime.now(timezone.utc) - saved_at).total_seconds() / 86400
                expires_in = max(0, int(_MAX_AGE_DAYS - age_days))
            except Exception:
                saved_at_str = None

        ready = age_days is None or age_days < _MAX_AGE_DAYS
        return {
            "status": "present" if ready else "expired",
            "age_days": round(age_days, 1) if age_days is not None else None,
            "saved_at": saved_at_str,
            "expires_in_days": expires_in,
            "ready": ready,
            "cookie_count": len(cookies),
            "session_file": str(path),
        }

    # Fall back to legacy cookies file
    if legacy.exists():
        try:
            cookies = json.loads(legacy.read_text())
            count = len(cookies)
            if count == 0:
                return {
                    "status": "empty", "age_days": None, "saved_at": None,
                    "expires_in_days": None, "ready": False, "cookie_count": 0,
                    "session_file": str(legacy),
                }
            return {
                "status": "present",
                "age_days": None,
                "saved_at": None,
                "expires_in_days": 30,  # conservative estimate for cookie-only sessions
                "ready": True,
                "cookie_count": count,
                "session_file": str(legacy),
            }
        except Exception:
            pass

    return {
        "status": "missing", "age_days": None, "saved_at": None,
        "expires_in_days": None, "ready": False, "cookie_count": 0,
        "session_file": str(path),
    }


def delete_session(portal: str) -> bool:
    """Delete all session files for a portal. Returns True if anything was deleted."""
    deleted = False
    for p in [_state_path(portal), _legacy_cookies_path(portal)]:
        if p.exists():
            p.unlink()
            logger.info("Deleted session: %s", p)
            deleted = True
    return deleted
