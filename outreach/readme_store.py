"""
Stage 1: README Data Store
Reads config and tracking table from outreach/README.md.
All writes are append-only inside TRACKING_START/TRACKING_END tags.
Nothing outside those tags is ever touched.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

_README_PATH = Path(__file__).parent / "README.md"


def _readme_text() -> str:
    return _README_PATH.read_text(encoding="utf-8")


def _write_readme(text: str) -> None:
    _README_PATH.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def read_config() -> dict[str, str]:
    """Parse key: value pairs between CONFIG_START and CONFIG_END tags."""
    text = _readme_text()
    match = re.search(
        r"<!-- CONFIG_START -->(.*?)<!-- CONFIG_END -->", text, re.DOTALL
    )
    if not match:
        raise RuntimeError("CONFIG_START/CONFIG_END tags not found in README.md")

    config: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if ":" in line and not line.startswith("#") and not line.startswith("##"):
            key, _, value = line.partition(":")
            config[key.strip()] = value.strip()

    # Coerce DAILY_CAP to int-compatible string; default 15
    if "DAILY_CAP" not in config:
        config["DAILY_CAP"] = "15"

    return config


# ---------------------------------------------------------------------------
# Tracking table
# ---------------------------------------------------------------------------

_TABLE_HEADER = (
    "| date | company | contact | domain | email_guessed "
    "| draft_created | status | notes |"
)
_TABLE_SEP = "|------|---------|---------|--------|---------------|---------------|--------|-------|"

_COLUMNS = ["date", "company", "contact", "domain", "email_guessed", "draft_created", "status", "notes"]


def _parse_row(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("|") or line.startswith("|---"):
        return None
    cells = [c.strip() for c in line.strip("|").split("|")]
    if len(cells) < len(_COLUMNS):
        return None
    # Skip header row
    if cells[0].lower() == "date":
        return None
    return dict(zip(_COLUMNS, cells))


def read_tracking() -> list[dict]:
    """Parse markdown table between TRACKING_START and TRACKING_END tags."""
    text = _readme_text()
    match = re.search(
        r"<!-- TRACKING_START -->(.*?)<!-- TRACKING_END -->", text, re.DOTALL
    )
    if not match:
        return []

    rows: list[dict] = []
    for line in match.group(1).splitlines():
        row = _parse_row(line)
        if row:
            rows.append(row)
    return rows


def append_tracking_row(row: dict) -> None:
    """Append one row to the tracking table. Never touches anything else in README."""
    text = _readme_text()

    # Escape pipe characters in values so they don't break the table
    def _escape(val: str) -> str:
        return str(val).replace("|", "/").replace("\n", " ")

    cells = [_escape(str(row.get(col, ""))) for col in _COLUMNS]
    new_row = "| " + " | ".join(cells) + " |"

    # Insert the new row just before the TRACKING_END tag
    updated = text.replace(
        "<!-- TRACKING_END -->",
        new_row + "\n<!-- TRACKING_END -->",
    )
    _write_readme(updated)


def is_already_contacted(domain: str) -> bool:
    """Return True if this domain already has a tracking row."""
    if not domain:
        return False
    domain_lower = domain.lower().strip()
    for row in read_tracking():
        if row.get("domain", "").lower().strip() == domain_lower:
            return True
    return False


def get_todays_draft_count() -> int:
    """Count rows where date is today and draft_created is true."""
    today = str(date.today())
    count = 0
    for row in read_tracking():
        if row.get("date", "") == today and row.get("draft_created", "").lower() == "true":
            count += 1
    return count


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== read_config() ===")
    cfg = read_config()
    for k, v in cfg.items():
        print(f"  {k}: {v}")

    print("\n=== append_tracking_row() (test row) ===")
    test_row = {
        "date": str(date.today()),
        "company": "TEST_COMPANY",
        "contact": "Test Person",
        "domain": "test-company.com",
        "email_guessed": "test@test-company.com",
        "draft_created": "true",
        "status": "draft_created",
        "notes": "automated test row",
    }
    append_tracking_row(test_row)
    print("Row written.")

    print("\n=== read_tracking() (after write) ===")
    rows = read_tracking()
    for r in rows:
        print(" ", r)

    print(f"\n=== is_already_contacted('test-company.com') = {is_already_contacted('test-company.com')} ===")
    print(f"=== get_todays_draft_count() = {get_todays_draft_count()} ===")

    print("\n=== Removing test row ===")
    text = _readme_text()
    lines = text.splitlines(keepends=True)
    cleaned = [l for l in lines if "TEST_COMPANY" not in l]
    _write_readme("".join(cleaned))
    print("Test row removed. Final tracking rows:", len(read_tracking()))
