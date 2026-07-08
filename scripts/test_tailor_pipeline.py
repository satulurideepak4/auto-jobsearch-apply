#!/usr/bin/env python3
"""End-to-end pipeline test: fetch JD → parse resume → tailor → build DOCX.

Run from repo root:
    .venv/bin/python scripts/test_tailor_pipeline.py <jd_url>
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("pipeline_test")


async def main(url: str) -> None:
    from app.config import get_settings
    from app.llm.provider_factory import get_provider
    from app.scraping.jd_fetcher import fetch_jd_from_url
    from app.matching.resume_parser import get_resume_data
    from app.tailoring.resume_tailor import ResumeTailor
    from app.tailoring.resume_builder import build_tailored_resume, extract_skill_categories

    settings = get_settings()
    logger.info("LLM provider : %s", settings.LLM_PROVIDER)
    logger.info("Scoring model: %s", settings.GEMINI_SCORING_MODEL)
    logger.info("Tailoring model: %s", settings.GEMINI_TAILORING_MODEL)

    # ── Step 1: Fetch JD ──────────────────────────────────────────────────────
    logger.info("\n=== STEP 1: Fetching JD from URL ===")
    scoring_llm = get_provider(settings, task="scoring")
    job = await fetch_jd_from_url(url, scoring_llm)
    logger.info("Title   : %s", job.get("title"))
    logger.info("Company : %s", job.get("company"))
    logger.info("Location: %s", job.get("location"))
    logger.info("Description snippet: %.300s", job.get("description", ""))

    # ── Step 2: Parse resume ──────────────────────────────────────────────────
    logger.info("\n=== STEP 2: Parsing base resume ===")
    resume_path = settings.RESUME_PATH
    logger.info("Resume path: %s", resume_path)
    resume_data = get_resume_data(resume_path, scoring_llm)
    logger.info("Current role    : %s", resume_data.get("current_role"))
    logger.info("Years experience: %s", resume_data.get("years_of_experience"))
    logger.info("Skills          : %s", resume_data.get("skills"))
    logger.info("Role history    :")
    for r in resume_data.get("role_history", []):
        logger.info("  - %s at %s (%s) | %d bullets", r.get("title"), r.get("company"), r.get("duration"), len(r.get("bullets", [])))

    # Inject structured skill categories so the tailor LLM can reorder them
    resume_data["skill_categories"] = extract_skill_categories(resume_path)
    logger.info("Skill categories extracted: %d", len(resume_data["skill_categories"]))

    # ── Step 3: Tailor content ────────────────────────────────────────────────
    logger.info("\n=== STEP 3: Tailoring resume content ===")
    tailoring_llm = get_provider(settings, task="tailoring")
    tailor = ResumeTailor(tailoring_llm)
    material = tailor.tailor(job, resume_data)

    print("\n--- TAILORED SUMMARY ---")
    print(material.get("summary", ""))
    print("\n--- TAILORED BULLETS ---")
    for b in material.get("bullets", []):
        print(" •", b)
    print("\n--- ATS KEYWORDS ---")
    print(", ".join(material.get("keywords", [])))
    print("\n--- TAILORED TECH SKILLS ---")
    for entry in material.get("tech_skills", []):
        print(f"  {entry.get('category')}: {', '.join(entry.get('skills', []))}")

    # ── Step 4: Build tailored DOCX ───────────────────────────────────────────
    logger.info("\n=== STEP 4: Building tailored DOCX ===")
    first = settings.APPLICANT_FIRST_NAME
    last = settings.APPLICANT_LAST_NAME
    name_part = f"{first}_{last}".strip("_") if (first or last) else "Resume"
    title_raw = re.split(r"\s*[-–|]\s*", job.get("title", "role"))[0].strip()
    title_part = re.sub(r"[^a-zA-Z0-9]+", "_", title_raw).strip("_")
    job_id = f"{name_part}_{title_part}"
    logger.info("Output filename: %s.docx", job_id)
    out_path = build_tailored_resume(resume_path, material, job_id, resume_data)
    logger.info("Output file: %s", out_path)

    # ── Verify the output ─────────────────────────────────────────────────────
    from pathlib import Path
    p = Path(out_path)
    if p.exists() and p.stat().st_size > 0:
        logger.info("SUCCESS: Tailored DOCX saved (%d bytes) at %s", p.stat().st_size, out_path)
        print(f"\nTailored resume saved to: {out_path}")
    else:
        logger.error("FAILED: Output file missing or empty at %s", out_path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: .venv/bin/python scripts/test_tailor_pipeline.py <jd_url>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
