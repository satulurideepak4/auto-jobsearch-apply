"""
One-shot script: draft and create a Gmail draft for a specific company.
Bypasses scraping and scoring — useful for re-drafting or testing.

Usage:
  .venv/bin/python outreach/send_draft.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from email_drafter import create_gmail_draft, draft_email, guess_emails
from readme_store import append_tracking_row, read_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Job to draft for — change this to any company
# ---------------------------------------------------------------------------
JOB = {
    "company": "Ostium Labs",
    "title": "Senior Backend Engineer - Golang Trading Systems",
    "description": (
        "Ostium Labs is building a decentralized perpetuals trading protocol handling $20B+ in volume. "
        "We're looking for a Senior Backend Engineer with deep Golang expertise to build high-performance "
        "trading infrastructure, matching engines, and real-time data pipelines. "
        "Stack: Go, Kafka, PostgreSQL, Redis, Kubernetes, AWS. Remote-friendly."
    ),
    "domain": "ostium.com",
    "source": "manual",
    "job_url": "",
}
CONTACT_NAME = "Hiring Team"
TECH_STACK = "Go, Kafka, PostgreSQL, Redis, Kubernetes, AWS"


def main() -> None:
    config = read_config()

    # 1. Guess emails
    logger.info("Guessing emails for %s...", JOB["company"])
    emails = guess_emails(CONTACT_NAME, JOB["domain"], JOB["company"])
    logger.info("Email candidates: %s", emails)

    # 2. Draft email
    logger.info("Drafting email...")
    draft = draft_email(
        job=JOB,
        contact_name=CONTACT_NAME,
        tech_stack_mentioned=TECH_STACK,
        email_candidates=emails,
    )

    print("\n" + "=" * 60)
    print(f"SUBJECT: {draft['subject']}")
    print("=" * 60)
    print(draft["body"])
    print("=" * 60 + "\n")

    # 3. Create Gmail draft
    logger.info("Creating Gmail draft to %s...", emails[0])
    success = create_gmail_draft(
        to_email=emails[0],
        subject=draft["subject"],
        body=draft["body"],
        resume_path=config.get("RESUME_PATH", "outreach/base_resume.pdf"),
        credentials_path=config.get("GMAIL_CREDENTIALS_PATH", "outreach/google_credentials.json"),
        token_path=config.get("TOKEN_PATH", "outreach/gmail_token.json"),
    )

    if success:
        logger.info("Gmail draft created successfully!")
        append_tracking_row({
            "date": str(date.today()),
            "company": JOB["company"],
            "contact": CONTACT_NAME,
            "domain": JOB["domain"],
            "email_guessed": emails[0],
            "draft_created": "true",
            "status": "draft_created",
            "notes": f"Manual draft via send_draft.py",
        })
        logger.info("Tracking row added to README.md")
    else:
        logger.error("Failed to create Gmail draft.")
        sys.exit(1)


if __name__ == "__main__":
    main()
