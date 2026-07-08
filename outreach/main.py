"""
Stage 5: Main Orchestrator
Runs the full outreach pipeline end to end:
  1. Load config + resume
  2. Scrape jobs from all sources
  3. Filter already-contacted domains
  4. Score relevance with Gemini
  5. Guess emails + draft cold email for each match
  6. Create Gmail draft (never sends)
  7. Append tracking row to README.md
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent))

from readme_store import (
    append_tracking_row,
    get_todays_draft_count,
    is_already_contacted,
    read_config,
)
from resume_parser import load_resume

logger = logging.getLogger(__name__)


def _setup_logging(log_path: str) -> None:
    fmt = "%(asctime)s %(levelname)-8s %(message)s"
    # Only stream to stdout — the API subprocess redirect writes stdout to the log file.
    # Adding a FileHandler here would double every log line in the file.
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def main(dry_run: bool = False, cap_override: int | None = None) -> None:
    _setup_logging(str(Path(__file__).parent / "pipeline.log"))

    # ------------------------------------------------------------------
    # 1. Load config + resume
    # ------------------------------------------------------------------
    config = read_config()
    daily_cap = cap_override if cap_override is not None else int(config.get("DAILY_CAP", 15))

    resume = load_resume()
    logger.info("=== OUTREACH PIPELINE START ===")
    logger.info("Resume owner : %s", resume["full_name"])
    logger.info("Current role : %s", resume["current_role"])
    logger.info("Target roles : %s", ", ".join(resume["target_roles"]))
    logger.info("Daily cap    : %d", daily_cap)

    todays_count = get_todays_draft_count()
    logger.info("Drafts created today so far: %d / %d", todays_count, daily_cap)

    if todays_count >= daily_cap:
        logger.info("Daily cap reached. Exiting — run again tomorrow.")
        return

    # ------------------------------------------------------------------
    # 2. Scrape
    # ------------------------------------------------------------------
    import scraper as scraper_mod
    logger.info("--- Scraping jobs ---")
    all_jobs = scraper_mod.scrape_all()
    logger.info("Total scraped (unique by domain): %d", len(all_jobs))

    # ------------------------------------------------------------------
    # 3. Filter already-contacted domains
    # ------------------------------------------------------------------
    new_jobs = [j for j in all_jobs if not is_already_contacted(j.get("domain", ""))]
    skipped = len(all_jobs) - len(new_jobs)
    logger.info("Already contacted (skipped): %d | New: %d", skipped, len(new_jobs))

    if not new_jobs:
        logger.info("No new companies to contact. Exiting.")
        return

    # ------------------------------------------------------------------
    # 4. Score relevance
    # Cap scoring: score at most daily_cap*8 companies per run. Stop early
    # once we have daily_cap*2 relevant hits — enough to fill the cap with
    # good options. This prevents scoring 200 companies when only 2 are needed.
    # ------------------------------------------------------------------
    from email_drafter import score_relevance

    score_limit = daily_cap * 15   # more sources = more candidates, score generously
    early_stop_relevant = daily_cap * 3
    candidates = new_jobs[:score_limit]
    logger.info(
        "--- Scoring relevance for %d jobs (limit %d, stop after %d relevant) ---",
        len(candidates), score_limit, early_stop_relevant,
    )

    scored_jobs: list[tuple[dict, dict]] = []
    all_scored_rows: list[dict] = []   # for scored_jobs.json (all scored, not just relevant)

    for job in candidates:
        scored = score_relevance(job)
        is_rel = scored.get("is_relevant", False)
        score = scored.get("match_score", 0)
        logger.info(
            "[score] %s | score=%s | relevant=%s | %s",
            job.get("company", "?"),
            score,
            is_rel,
            scored.get("match_reason", ""),
        )
        all_scored_rows.append({
            "company":       job.get("company", ""),
            "title":         job.get("title", ""),
            "source":        job.get("source", ""),
            "score":         score,
            "relevant":      is_rel,
            "match_reason":  scored.get("match_reason", ""),
            "tech_stack":    scored.get("tech_stack", ""),
            "domain":        job.get("domain", ""),
            "job_url":       job.get("job_url", ""),
            "description":   job.get("description", "")[:2000],
            "contact_name":  scored.get("contact_name", "Hiring Team"),
            "contact_title": scored.get("contact_title", ""),
            "contact_type":  scored.get("contact_type", "hiring_team"),
            "drafted":       False,   # updated by UI after manual draft
        })
        if is_rel and score >= 5:
            scored_jobs.append((job, scored))
            if len(scored_jobs) >= early_stop_relevant:
                logger.info("Reached %d relevant jobs — stopping scoring early.", early_stop_relevant)
                break

    # Persist all scored rows so the UI Jobs tab can read them
    scored_json_path = Path(__file__).parent / "scored_jobs.json"
    try:
        scored_json_path.write_text(
            json.dumps(all_scored_rows, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Wrote %d scored rows to scored_jobs.json", len(all_scored_rows))
    except OSError as exc:
        logger.warning("Could not write scored_jobs.json: %s", exc)

    # Sort descending by match score
    scored_jobs.sort(key=lambda x: x[1].get("match_score", 0), reverse=True)
    logger.info("Relevant jobs (score >= 5): %d", len(scored_jobs))

    if not scored_jobs:
        logger.info("No relevant jobs found today. Exiting.")
        return

    # ------------------------------------------------------------------
    # 5+6. Process each job up to daily cap
    # ------------------------------------------------------------------
    from email_drafter import create_gmail_draft, draft_email, guess_emails

    logger.info("--- Processing top matches ---")

    for job, scored in scored_jobs:
        todays_count = get_todays_draft_count()
        remaining_cap = daily_cap - todays_count
        if remaining_cap <= 0:
            logger.info("Daily cap reached mid-run. Stopping.")
            break

        company = job.get("company", "Unknown")
        domain = job.get("domain", "")

        # Gemini extracts the most senior reachable contact from the job description.
        # Priority: Founder > CEO/CTO > VP Eng > Manager > Recruiter > Hiring Team
        contact_name = (
            scored.get("contact_name")
            or job.get("contact_name")
            or "Hiring Team"
        )
        if contact_name.lower() in ("", "none", "null"):
            contact_name = "Hiring Team"

        contact_type = scored.get("contact_type", "hiring_team")
        contact_title = scored.get("contact_title", "")

        logger.info(
            "Processing: %s | score=%s | contact=%s (%s)",
            company, scored.get("match_score"), contact_name, contact_type,
        )

        # 5a. Guess emails — pattern depends on contact type
        email_candidates = guess_emails(contact_name, domain, company, contact_type=contact_type)
        if not email_candidates or not email_candidates[0]:
            logger.warning("  No email guesses for %s — skipping", company)
            continue
        logger.info("  Email guesses: %s", email_candidates)

        # 5b. Draft email — tone adjusted for founder/exec vs manager vs recruiter
        draft = draft_email(
            job=job,
            contact_name=contact_name,
            tech_stack_mentioned=scored.get("tech_stack", ""),
            email_candidates=email_candidates,
            contact_type=contact_type,
            contact_title=contact_title,
        )
        logger.info("  Subject: %s", draft.get("subject", ""))

        if dry_run:
            logger.info("  [DRY RUN] Skipping Gmail draft creation")
            logger.info("  Body preview: %s…", draft.get("body", "")[:120])
            append_tracking_row({
                "date": str(date.today()),
                "company": company,
                "contact": f"{contact_name} ({contact_title})" if contact_title else contact_name,
                "domain": domain,
                "email_guessed": email_candidates[0],
                "draft_created": "false",
                "status": "dry_run",
                "notes": scored.get("match_reason", ""),
            })
            time.sleep(2)
            continue

        # 5c. Create Gmail draft
        success = create_gmail_draft(
            to_email=email_candidates[0],
            subject=draft.get("subject", ""),
            body=draft.get("body", ""),
            resume_path=config.get("RESUME_PATH", "outreach/base_resume.pdf"),
            credentials_path=config.get("GMAIL_CREDENTIALS_PATH", "outreach/gmail_credentials.json"),
            token_path=config.get("TOKEN_PATH", "outreach/gmail_token.json"),
        )

        if success:
            append_tracking_row({
                "date": str(date.today()),
                "company": company,
                "contact": f"{contact_name} ({contact_title})" if contact_title else contact_name,
                "domain": domain,
                "email_guessed": email_candidates[0],
                "draft_created": "true",
                "status": "draft_created",
                "notes": scored.get("match_reason", ""),
            })
            # Mark as drafted in scored_jobs.json so UI shows correct state
            for row in all_scored_rows:
                if row.get("company") == company:
                    row["drafted"] = True
                    break
            logger.info("  Draft created: %s → %s", company, email_candidates[0])
            time.sleep(10)
        else:
            logger.warning("  Draft FAILED for %s — not tracked", company)
            time.sleep(5)

    # Re-write scored_jobs.json with drafted flags updated
    try:
        scored_json_path.write_text(
            json.dumps(all_scored_rows, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------
    final_count = get_todays_draft_count()
    pending_relevant = len([r for r in all_scored_rows if r.get("relevant") and not r.get("drafted")])
    logger.info(
        "=== PIPELINE COMPLETE | Scraped: %d | Relevant: %d | Auto-drafted: %d | "
        "Pending in UI: %d (click Draft Email on any row) ===",
        len(all_jobs),
        len(scored_jobs),
        final_count,
        pending_relevant,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Outreach pipeline orchestrator")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Score and draft but do NOT create Gmail drafts. Writes dry_run rows to tracking.",
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=None,
        help="Override DAILY_CAP for this run (e.g. --cap 1 for a quick test)",
    )
    args = parser.parse_args()

    main(dry_run=args.dry_run, cap_override=args.cap)
