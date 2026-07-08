"""Manage job search roles derived from resume + user customisations.

Roles are saved to ~/.auto-jobsearch/search_roles.json so they persist
across restarts. On first run (or when force_refresh=True) the LLM parses
the resume to produce target_roles and search_terms; the user can then add
or remove roles via the API.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.llm.base import LLMProvider

logger = logging.getLogger(__name__)

_ROLES_FILE = Path.home() / ".auto-jobsearch" / "search_roles.json"


def _load_file() -> dict:
    if _ROLES_FILE.exists():
        try:
            return json.loads(_ROLES_FILE.read_text())
        except Exception:
            pass
    return {"roles": [], "custom_roles": [], "detected_from_resume": False}


def _save_file(data: dict) -> None:
    _ROLES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ROLES_FILE.write_text(json.dumps(data, indent=2))


def get_roles() -> dict:
    """Return current roles state: detected roles + custom additions."""
    return _load_file()


def get_all_search_terms() -> list[str]:
    """Return merged list of detected roles + custom roles for scraping."""
    data = _load_file()
    combined = list(dict.fromkeys(data.get("roles", []) + data.get("custom_roles", [])))
    return combined or ["Software Engineer"]  # fallback if nothing detected yet


def add_custom_role(role: str) -> dict:
    """Add a user-supplied role to the custom list."""
    data = _load_file()
    role = role.strip()
    if role and role not in data.get("custom_roles", []):
        data.setdefault("custom_roles", []).append(role)
        _save_file(data)
    return data


def remove_role(role: str) -> dict:
    """Remove a role from either detected or custom list."""
    data = _load_file()
    data["roles"] = [r for r in data.get("roles", []) if r != role]
    data["custom_roles"] = [r for r in data.get("custom_roles", []) if r != role]
    _save_file(data)
    return data


def detect_roles_from_resume(resume_path: str, llm: "LLMProvider", force: bool = False) -> dict:
    """Parse resume with LLM to extract target roles and search terms.

    Skips LLM call if roles were already detected and force=False.
    Returns the full roles state dict.
    """
    data = _load_file()
    if data.get("detected_from_resume") and not force:
        logger.info("search_roles: using cached detected roles (%d)", len(data.get("roles", [])))
        return data

    from app.matching.resume_parser import get_resume_data, _resume_cache

    # Clear cache so the updated prompt runs
    _resume_cache.pop(resume_path, None)
    resume_data = get_resume_data(resume_path, llm)

    target_roles = resume_data.get("target_roles") or []
    search_terms = resume_data.get("search_terms") or []

    # Merge: target_roles are the primary display names; search_terms are additional query strings
    combined = list(dict.fromkeys(target_roles + search_terms))

    data["roles"] = combined
    data["detected_from_resume"] = True
    _save_file(data)

    logger.info("search_roles: detected %d roles from resume: %s", len(combined), combined[:5])
    return data
