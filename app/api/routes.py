from __future__ import annotations

import logging
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.db import get_db

# In-memory registry of active portal login jobs {login_id: {status, portal, error}}
_login_jobs: dict[str, dict] = {}

_PORTAL_LOGIN_URLS = {
    "naukri":    "https://www.naukri.com/nlogin/login",
    "instahyre": "https://www.instahyre.com/login/",
    "wellfound": "https://wellfound.com/login",
}

_PORTAL_DOMAINS = {
    "naukri":    "naukri.com",
    "instahyre": "instahyre.com",
    "wellfound": "wellfound.com",
}

# Cookies that are present even when NOT logged in (Cloudflare bot protection,
# generic analytics). Used to distinguish "has a real session" from "just visited".
_NON_AUTH_COOKIES = {"cf_clearance", "datadome", "_ga", "_gid", "_clck", "_clsk", "ajs_anonymous_id"}

# Known session cookie names per portal — presence of any one means the user is logged in
_PORTAL_SESSION_COOKIES = {
    "naukri":    {"nauk_sid", "nauk_rt", "J", "is_login"},
    "wellfound": {"_wellfound"},
    "instahyre": {"sessionid"},
}

# ---------------------------------------------------------------------------
# Outreach: add outreach/ dir to sys.path so its modules are importable
# ---------------------------------------------------------------------------
_OUTREACH_DIR = Path(__file__).parent.parent.parent / "outreach"
if str(_OUTREACH_DIR) not in sys.path:
    sys.path.insert(0, str(_OUTREACH_DIR))

# Module-level subprocess handle for the outreach pipeline
_outreach_process: Optional[subprocess.Popen] = None
from app.storage.repository import (
    create_application,
    get_job,
    get_tailored_material,
    list_applications,
    list_jobs,
    list_matches,
    save_match,
    save_tailored_material,
    update_application_status,
    update_match_status,
    upsert_job,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Background pipeline helpers
# ---------------------------------------------------------------------------


async def _run_scrape_and_score(db: AsyncSession) -> None:
    """Background task: scrape jobs, score them, and persist results."""
    import asyncio
    from app.config import get_settings
    from app.llm.provider_factory import get_provider
    from app.matching.resume_parser import get_resume_data
    from app.matching.scorer import JobScorer
    from app.scraping.jobspy_client import scrape_india_profiles, scrape_intl_profiles, scrape_portal_jobs

    settings = get_settings()
    scoring_llm = get_provider(settings, task="scoring")
    scorer = JobScorer(scoring_llm, settings)
    loop = asyncio.get_event_loop()

    # ── Determine search roles from resume (LLM-detected + user customisations) ──
    from app.scraping.search_roles import detect_roles_from_resume, get_all_search_terms
    await loop.run_in_executor(None, detect_roles_from_resume, settings.resume_path, scoring_llm, False)
    roles = get_all_search_terms()
    logger.info("Scraping with %d roles: %s", len(roles), roles[:6])

    # Scrape — jobspy (Indeed, Glassdoor, ZipRecruiter) + portal browsers (Naukri, Instahyre, Wellfound)
    # jobspy is synchronous — offload to thread so the event loop stays responsive
    india_jobs = await loop.run_in_executor(None, scrape_india_profiles, settings, roles)
    intl_jobs = await loop.run_in_executor(None, scrape_intl_profiles, settings, roles)
    portal_jobs = await scrape_portal_jobs(settings, roles)
    all_jobs = india_jobs + intl_jobs + portal_jobs

    logger.info("Background scrape: %d total jobs fetched", len(all_jobs))

    # Parse resume once — synchronous, offload to thread
    try:
        resume_data = await loop.run_in_executor(None, get_resume_data, settings.resume_path, scoring_llm)
    except Exception as exc:
        logger.error("Failed to parse resume at %s: %s", settings.resume_path, exc)
        resume_data = {}

    above_threshold = 0
    for job_data in all_jobs:
        try:
            job = await upsert_job(db, job_data)
            # scorer.score() calls LLM synchronously — offload to thread pool so
            # the event loop is not blocked during each LLM round-trip
            score_data = await loop.run_in_executor(None, scorer.score, job_data, resume_data)
            if score_data["score"] >= settings.match_score_threshold:
                await save_match(db, job.id, score_data)
                above_threshold += 1
        except Exception as exc:
            logger.error("Error processing job '%s': %s", job_data.get("title"), exc)

    await db.commit()
    logger.info(
        "Scrape pipeline complete: %d jobs scraped, %d above threshold (%d)",
        len(all_jobs),
        above_threshold,
        settings.match_score_threshold,
    )


async def _run_tailoring(match_id: str, db: AsyncSession) -> None:
    """Background task: generate tailored materials for an approved match."""
    from app.config import get_settings
    from app.llm.provider_factory import get_provider
    from app.matching.resume_parser import get_resume_data
    from app.storage.models import Match
    from app.storage.repository import get_job
    from app.tailoring.resume_tailor import ResumeTailor
    from sqlalchemy import select

    settings = get_settings()
    scoring_llm = get_provider(settings, task="scoring")
    tailoring_llm = get_provider(settings, task="tailoring")

    result = await db.execute(
        select(Match).where(Match.id == match_id)  # type: ignore[arg-type]
    )
    match = result.scalar_one_or_none()
    if match is None:
        logger.error("Tailoring: match %s not found", match_id)
        return

    job = await get_job(db, match.job_id)
    if job is None:
        logger.error("Tailoring: job %s not found", match.job_id)
        return

    try:
        resume_data = get_resume_data(settings.resume_path, scoring_llm)
    except Exception as exc:
        logger.error("Failed to parse resume: %s", exc)
        return

    tailor = ResumeTailor(tailoring_llm)
    job_dict = {
        "title": job.title,
        "company": job.company,
        "description": job.description or "",
        "location": job.location,
        "job_url": job.job_url,
    }
    material = tailor.tailor(job_dict, resume_data)

    # Store resume_data alongside the material so the preview endpoint can use it
    material["_resume_data"] = resume_data

    await save_tailored_material(db, job.id, material)
    await db.commit()

    # Generate the tailored DOCX that will be uploaded to ATS forms
    from app.tailoring.resume_builder import build_tailored_resume
    build_tailored_resume(settings.resume_path, material, job.id, resume_data, job_title=job.title)

    logger.info("Tailoring complete for job '%s' (match %s)", job.title, match_id)


async def _run_portal_apply(match_id: str, job_id: str, application_id: str) -> None:
    """Background task: apply to a portal job using the driver's native apply flow.

    Routing logic:
      - Finds the driver whose name matches job.source (naukri / instahyre / wellfound)
      - Calls driver.run_apply(job_dict) → opens one browser with saved session
      - If driver sets external_url (ATS redirect) → hands off to IntelligentFiller
      - Updates application status in DB when done
    """
    from app.config import get_settings
    from app.scraping.drivers import get_registry
    from app.storage.db import AsyncSessionLocal
    from app.storage.models import Job
    from sqlalchemy import select

    settings = get_settings()

    # ── 1. Load job record (short-lived session) ──────────────────────────────
    async with AsyncSessionLocal() as db:
        job_result = await db.execute(select(Job).where(Job.id == job_id))
        job = job_result.scalar_one_or_none()

    if not job:
        logger.error("portal_apply: job %s not found", job_id)
        return

    source = job.source or ""
    job_dict = {
        "title":    job.title,
        "company":  job.company,
        "location": job.location,
        "job_url":  job.job_url,
        "source":   source,
    }

    # ── 2. Find driver by source name ─────────────────────────────────────────
    registry = get_registry()
    driver = next((d for d in registry.all_enabled() if d.name == source), None)

    if driver is None:
        # No portal driver — treat job_url as a direct ATS link
        logger.info("portal_apply: no driver for source=%r — routing to IntelligentFiller", source)
        await _intelligent_fill_apply(job_dict, application_id, settings)
        return

    # ── 3. Portal native apply (browser automation, no DB session held) ───────
    logger.info("portal_apply [%s]: applying to %s", source, job.job_url)
    result = await driver.run_apply(job_dict)

    # ── 4. External ATS redirect → hand off to IntelligentFiller ─────────────
    if result.get("external_url"):
        logger.info(
            "portal_apply [%s]: ATS redirect detected → %s",
            source, result["external_url"][:80],
        )
        job_dict["job_url"] = result["external_url"]
        await _intelligent_fill_apply(job_dict, application_id, settings)
        return

    # ── 5. Update application status ──────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        if result.get("applied"):
            await update_application_status(db, application_id, "submitted")
            logger.info(
                "portal_apply [%s]: ✓ submitted — %s @ %s",
                source, job.title, job.company,
            )
        else:
            err = result.get("error") or "apply_returned_false"
            await update_application_status(db, application_id, "failed", notes=err)
            logger.warning(
                "portal_apply [%s]: ✗ failed — %s | error: %s",
                source, job.job_url, err,
            )
        await db.commit()


async def _intelligent_fill_apply(job_dict: dict, application_id: str, settings) -> None:
    """Apply to an external ATS URL using IntelligentFiller (Greenhouse, Workday, Lever, etc.)."""
    from app.automation.intelligent_filler import IntelligentFiller
    from app.llm.provider_factory import get_provider
    from app.matching.resume_parser import get_resume_data
    from app.storage.db import AsyncSessionLocal
    from app.tailoring.answer_generator import AnswerGenerator
    from app.tailoring.resume_tailor import ResumeTailor

    scoring_llm  = get_provider(settings, task="scoring")
    tailoring_llm = get_provider(settings, task="tailoring")

    # Parse resume
    try:
        resume_data = get_resume_data(settings.resume_path, scoring_llm)
    except Exception as exc:
        logger.error("portal_apply ATS-fallback: resume parse failed: %s", exc)
        async with AsyncSessionLocal() as db:
            await update_application_status(
                db, application_id, "failed", notes=f"Resume parse error: {exc}"
            )
            await db.commit()
        return

    # Tailor resume to the job
    tailor   = ResumeTailor(tailoring_llm)
    material = tailor.tailor(job_dict, resume_data)

    # Answer generator for open-ended questions
    answer_gen = AnswerGenerator(scoring_llm)
    def _make_answer(q: str) -> str:
        return answer_gen.generate(q, job_dict, resume_data)

    # Fill the ATS form
    filler      = IntelligentFiller(llm=tailoring_llm)
    fill_result = await filler.fill(
        application_url=job_dict["job_url"],
        tailored_material={
            "summary":  material.get("summary", ""),
            "bullets":  material.get("bullets", []),
            "keywords": material.get("keywords", []),
        },
        resume_path=settings.resume_path,
        auto_submit=settings.auto_submit,
        answer_generator=_make_answer,
    )

    async with AsyncSessionLocal() as db:
        if fill_result.get("submitted"):
            await update_application_status(db, application_id, "submitted")
            logger.info("portal_apply ATS-fallback: ✓ submitted %s", job_dict.get("job_url"))
        elif fill_result.get("error"):
            await update_application_status(
                db, application_id, "failed", notes=fill_result["error"]
            )
            logger.error("portal_apply ATS-fallback: ✗ error: %s", fill_result["error"])
        else:
            await update_application_status(db, application_id, "filled")
            logger.info("portal_apply ATS-fallback: form filled (auto_submit=False)")
        await db.commit()


async def _run_apply_pipeline(
    url: str,
    job_id: str,
    application_id: str,
    auto_submit: bool,
    force: bool,
) -> None:
    """Full one-shot pipeline: fetch JD → score → tailor → fill → submit.

    Runs in a background task with its own DB session so the request session
    lifecycle does not affect the long-running work.
    """
    from app.config import get_settings
    from app.llm.provider_factory import get_provider
    from app.matching.resume_parser import get_resume_data
    from app.matching.scorer import JobScorer
    from app.scraping.jd_fetcher import fetch_jd_from_url
    from app.storage.db import AsyncSessionLocal
    from app.storage.models import Application as AppModel
    from app.storage.models import Job as JobModel
    from app.tailoring.resume_tailor import ResumeTailor
    from sqlalchemy import select

    settings = get_settings()
    scoring_llm = get_provider(settings, task="scoring")
    tailoring_llm = get_provider(settings, task="tailoring")

    async with AsyncSessionLocal() as db:
        try:
            # ── 1. Fetch JD ──────────────────────────────────────────────
            logger.info("Apply pipeline [%s]: fetching JD", application_id)
            job_data = await fetch_jd_from_url(url, scoring_llm)

            # Update the stub job with real data now that we have it
            result = await db.execute(select(JobModel).where(JobModel.id == job_id))
            job_row = result.scalar_one_or_none()
            if job_row:
                job_row.title = job_data["title"] or job_row.title
                job_row.company = job_data["company"] or job_row.company
                job_row.location = job_data["location"] or job_row.location
                job_row.description = job_data["description"]
                from datetime import datetime
                job_row.updated_at = datetime.utcnow()
                await db.flush()

            # ── 2. Parse resume ───────────────────────────────────────────
            try:
                resume_data = get_resume_data(settings.resume_path, scoring_llm)
            except Exception as exc:
                logger.error("Apply pipeline [%s]: resume parse failed: %s", application_id, exc)
                await update_application_status(db, application_id, "rejected", notes=f"Resume parse error: {exc}")
                await db.commit()
                return

            # ── 3. Score ──────────────────────────────────────────────────
            scorer = JobScorer(scoring_llm, settings)
            score_data = scorer.score(job_data, resume_data)
            score = score_data["score"]
            logger.info(
                "Apply pipeline [%s]: score=%d for '%s' @ %s",
                application_id, score, job_data.get("title"), job_data.get("company"),
            )

            if score < settings.match_score_threshold and not force:
                logger.warning(
                    "Apply pipeline [%s]: score %d below threshold %d — stopping "
                    "(set force=true to apply anyway)",
                    application_id, score, settings.match_score_threshold,
                )
                await update_application_status(
                    db, application_id, "rejected",
                    notes=f"Score {score} is below threshold {settings.match_score_threshold}. "
                          "Re-submit with force=true to apply regardless.",
                )
                await db.commit()
                return

            # ── 4. Save match & link it to the application ────────────────
            match = await save_match(db, job_id, score_data)
            await db.flush()

            result = await db.execute(select(AppModel).where(AppModel.id == application_id))
            app_row = result.scalar_one_or_none()
            if app_row:
                app_row.match_id = match.id
                await db.flush()

            # ── 5. Tailor ─────────────────────────────────────────────────
            tailor = ResumeTailor(tailoring_llm)
            job_dict = {
                "title": job_data["title"],
                "company": job_data["company"],
                "description": job_data["description"],
                "location": job_data["location"],
                "job_url": url,
            }
            material = tailor.tailor(job_dict, resume_data)

            # Store resume_data alongside so the preview endpoint can use it
            material["_resume_data"] = resume_data

            await save_tailored_material(db, job_id, material)
            await db.commit()

            # ── 6. Generate tailored DOCX ─────────────────────────────────
            from app.tailoring.resume_builder import build_tailored_resume
            tailored_resume_path = build_tailored_resume(
                settings.resume_path, material, job_id, resume_data,
                job_title=job_data.get("title", ""),
            )

            # ── 7. Fill (and optionally submit) form ──────────────────────
            from app.automation.intelligent_filler import IntelligentFiller
            from app.tailoring.answer_generator import AnswerGenerator
            import os as _os

            answer_gen = AnswerGenerator(scoring_llm)

            def _make_answer(question: str) -> str:
                return answer_gen.generate(question, job_dict, resume_data)

            filler = IntelligentFiller(llm=tailoring_llm)
            fill_result = await filler.fill(
                application_url=url,
                tailored_material={
                    "summary": material.get("summary", ""),
                    "bullets": material.get("bullets", []),
                    "keywords": material.get("keywords", []),
                },
                resume_path=tailored_resume_path,
                auto_submit=auto_submit,
                answer_generator=_make_answer,
            )

            # Delete the temp DOCX — content is already in DB, file no longer needed
            try:
                if tailored_resume_path != settings.resume_path:
                    _os.unlink(tailored_resume_path)
                    logger.info("Deleted temp tailored DOCX: %s", tailored_resume_path)
            except OSError:
                pass

            fill_error = fill_result.get("error")
            if fill_error:
                final_status = "rejected"
                notes = f"Form fill failed: {fill_error}"
                logger.error("Apply pipeline [%s]: form fill error: %s", application_id, fill_error)
            elif fill_result.get("submitted"):
                final_status = "submitted"
                notes = None
            else:
                final_status = "filled"
                notes = None

            await update_application_status(db, application_id, final_status, notes=notes)
            await db.commit()

            logger.info(
                "Apply pipeline [%s]: complete | job='%s' score=%d status=%s",
                application_id, job_data["title"], score, final_status,
            )

        except Exception as exc:
            logger.error("Apply pipeline [%s]: unhandled error: %s", application_id, exc)
            try:
                await update_application_status(
                    db, application_id, "rejected", notes=f"Pipeline error: {exc}"
                )
                await db.commit()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ApplyRequest(BaseModel):
    url: str
    auto_submit: bool = False
    force: bool = False  # if True, apply even when score is below threshold


class TailorRequest(BaseModel):
    url: str


class CrawlRequest(BaseModel):
    url: str                          # any careers page or job board URL
    role: str = ""                    # optional keyword filter
    limit: int = 30                   # max jobs to discover
    score_and_save: bool = True       # score against resume and persist matches


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/tailor")
async def tailor_resume_from_url(request: TailorRequest):
    """Fetch a JD from URL, tailor the resume, and return a DOCX file for download.

    Body:
        url — direct link to the job posting

    Returns the tailored .docx as a file attachment.
    """
    from pathlib import Path
    from fastapi.responses import FileResponse
    from app.config import get_settings
    from app.llm.provider_factory import get_provider
    from app.matching.resume_parser import get_resume_data
    from app.scraping.jd_fetcher import fetch_jd_from_url
    from app.tailoring.resume_builder import build_tailored_resume, extract_skill_categories
    from app.tailoring.resume_tailor import ResumeTailor

    settings = get_settings()
    scoring_llm = get_provider(settings, task="scoring")
    tailoring_llm = get_provider(settings, task="tailoring")

    try:
        job = await fetch_jd_from_url(request.url, scoring_llm)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Failed to fetch JD: {exc}")

    try:
        resume_data = get_resume_data(settings.resume_path, scoring_llm)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to parse resume: {exc}")

    resume_data["skill_categories"] = extract_skill_categories(settings.resume_path)

    tailor = ResumeTailor(tailoring_llm)
    material = tailor.tailor(job, resume_data)

    job_title = job.get("title", "role")
    out_path = build_tailored_resume(
        settings.resume_path, material, "download", resume_data, job_title=job_title
    )

    return FileResponse(
        path=out_path,
        filename=Path(out_path).name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@router.post("/scrape")
async def trigger_scrape(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Trigger a background scrape-and-score pipeline."""
    background_tasks.add_task(_run_scrape_and_score, db)
    return {"status": "started", "message": "Scrape and scoring pipeline started in background"}


@router.post("/crawl")
async def crawl_url(
    request: CrawlRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Crawl any careers page or job board URL to discover and optionally score jobs.

    Works on:
      - Company careers pages (e.g. https://stripe.com/jobs)
      - Job boards in config/job_boards.yaml
      - Any public URL with job listings

    Body:
        url             URL to crawl
        role            Optional search keyword to filter links
        limit           Max jobs to discover (default 30)
        score_and_save  Score each job against your resume and save matches (default true)
    """
    async def _run_crawl():
        from app.config import get_settings
        from app.llm.provider_factory import get_provider
        from app.matching.resume_parser import get_resume_data
        from app.matching.scorer import JobScorer
        from app.scraping.universal_crawler import UniversalCrawler
        from app.storage.db import AsyncSessionLocal

        settings = get_settings()
        crawler = UniversalCrawler()
        jobs = await crawler.crawl(url=request.url, role=request.role, limit=request.limit)
        logger.info("Crawl %s: %d jobs discovered", request.url, len(jobs))

        if not request.score_and_save or not jobs:
            return

        scoring_llm = get_provider(settings, task="scoring")
        scorer = JobScorer(scoring_llm, settings)
        try:
            resume_data = get_resume_data(settings.resume_path, scoring_llm)
        except Exception as exc:
            logger.error("Crawl: resume parse failed: %s", exc)
            return

        async with AsyncSessionLocal() as crawl_db:
            saved = 0
            for job_data in jobs:
                try:
                    job = await upsert_job(crawl_db, job_data)
                    score_data = scorer.score(job_data, resume_data)
                    if score_data["score"] >= settings.match_score_threshold:
                        await save_match(crawl_db, job.id, score_data)
                        saved += 1
                except Exception as exc:
                    logger.error("Crawl: error processing '%s': %s", job_data.get("title"), exc)
            await crawl_db.commit()
            logger.info("Crawl complete: %d jobs, %d above threshold", len(jobs), saved)

    background_tasks.add_task(_run_crawl)
    return {
        "status": "started",
        "url": request.url,
        "limit": request.limit,
        "message": (
            f"Crawling {request.url} in background. "
            "Poll GET /api/v1/matches to see scored results."
        ),
    }


@router.post("/apply")
async def apply_from_url(
    request: ApplyRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """One-shot apply pipeline: fetch JD from URL → score → tailor → fill → submit.

    Creates a job and application record immediately and returns their IDs so you
    can poll GET /api/v1/applications/{application_id} to track progress.

    Body:
        url         — direct link to the job posting (company site, Greenhouse, Lever, etc.)
        auto_submit — set true to click the submit button automatically (default false)
        force       — set true to apply even if the score is below MATCH_SCORE_THRESHOLD
    """
    # Create a stub job so we have a job_id before the background task runs.
    # upsert_job handles the case where this URL already exists in the DB.
    stub_job = await upsert_job(db, {
        "title": "Fetching…",
        "company": "Fetching…",
        "location": "",
        "description": None,
        "job_url": request.url,
        "source": "direct_url",
    })

    application = await create_application(db, stub_job.id)
    await db.commit()

    background_tasks.add_task(
        _run_apply_pipeline,
        request.url,
        stub_job.id,
        application.id,
        request.auto_submit,
        request.force,
    )

    return {
        "application_id": application.id,
        "job_id": stub_job.id,
        "status": "started",
        "message": (
            "Pipeline running in background. "
            f"Poll GET /api/v1/applications/{application.id} for status."
        ),
    }


@router.get("/matches")
async def get_matches(
    status: str = "pending",
    db: AsyncSession = Depends(get_db),
):
    """List match records with their associated job info."""
    from app.storage.models import Job, Match
    from sqlalchemy import select

    result = await db.execute(
        select(Match, Job)
        .join(Job, Match.job_id == Job.id)
        .where(Match.status == status)
        .order_by(Match.score.desc())
    )
    rows = result.all()

    matches = []
    for match, job in rows:
        matches.append(
            {
                "match_id": match.id,
                "score": match.score,
                "reasoning": match.reasoning,
                "matched_skills": match.matched_skills,
                "missing_skills": match.missing_skills,
                "status": match.status,
                "created_at": match.created_at.isoformat() if match.created_at else None,
                "job": {
                    "id": job.id,
                    "title": job.title,
                    "company": job.company,
                    "location": job.location,
                    "job_url": job.job_url,
                    "salary_min": job.salary_min,
                    "salary_max": job.salary_max,
                    "currency": job.currency,
                    "source": job.source,
                    "date_posted": job.date_posted.isoformat() if job.date_posted else None,
                },
            }
        )
    return matches


@router.post("/matches/{match_id}/approve")
async def approve_match(
    match_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Approve a match and trigger tailoring in the background."""
    try:
        match = await update_match_status(db, match_id, "approved")
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # Create an application record
    application = await create_application(db, match.job_id, match_id)
    await db.commit()

    # Run tailoring in background
    background_tasks.add_task(_run_tailoring, match_id, db)

    return {
        "match_id": match.id,
        "status": match.status,
        "application_id": application.id,
        "message": "Match approved and tailoring started in background",
    }


@router.post("/matches/{match_id}/portal-apply")
async def portal_apply_match(
    match_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Apply to a portal job (Naukri / Instahyre / Wellfound) using native portal apply.

    - For portal-native jobs (Naukri profile apply, Instahyre connect, Wellfound interest form)
      the driver opens a browser with the saved session and submits the application.
    - When the portal redirects to an external ATS (Greenhouse, Workday, Lever, etc.)
      the request is automatically handed off to IntelligentFiller to fill the ATS form.

    Poll GET /api/v1/applications/{application_id} to check progress.
    """
    from app.storage.models import Job, Match
    from sqlalchemy import select

    row = await db.execute(
        select(Match, Job).join(Job, Match.job_id == Job.id).where(Match.id == match_id)
    )
    pair = row.first()
    if not pair:
        raise HTTPException(status_code=404, detail=f"Match {match_id} not found")

    match, job = pair

    application = await create_application(db, job.id, match_id)
    await db.commit()

    background_tasks.add_task(_run_portal_apply, match_id, job.id, application.id)

    return {
        "match_id":       match_id,
        "application_id": application.id,
        "job_url":        job.job_url,
        "source":         job.source,
        "status":         "started",
        "message": (
            f"Portal apply started for '{job.source}' job. "
            f"Poll GET /api/v1/applications/{application.id} for status."
        ),
    }


@router.post("/matches/{match_id}/reject")
async def reject_match(
    match_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Reject a match."""
    try:
        match = await update_match_status(db, match_id, "rejected")
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return {"match_id": match.id, "status": match.status}


async def _do_portal_login(login_id: str, portal: str) -> None:
    """Background task: open a new tab in the user's running Chrome for portal login.

    Two-phase detection:
    1. If Chrome already has the portal's session cookies → extract immediately, no tab needed.
       (All three portals use Google OAuth and stay logged in across visits.)
    2. If no session found → open login tab, poll all Chrome profiles for the known
       session cookie to appear (handles whichever profile Chrome opens the tab in).
    """
    import asyncio
    from app.scraping.portal_auth import save_storage_state
    from app.scraping.chrome_native import (
        all_profile_dirs, open_url_in_chrome, close_chrome_tab,
        read_cookie_names_count, read_cookies_for_domain,
    )

    _login_jobs[login_id]["status"] = "browser_opened"
    login_url      = _PORTAL_LOGIN_URLS[portal]
    portal_domain  = _PORTAL_DOMAINS[portal]
    session_names  = _PORTAL_SESSION_COOKIES.get(portal, set())
    profile_dirs   = all_profile_dirs()

    def _best_session_profile():
        """Return (profile_dir, cookie_names) for the profile that has the most
        known session cookies, or None if no profile is logged in."""
        best_pd, best_names = None, set()
        for pd in profile_dirs:
            names, _, _ = read_cookie_names_count(pd, portal_domain)
            found = names & session_names
            if len(found) > len(best_names):
                best_pd, best_names = pd, found
        return (best_pd, best_names) if best_pd else None

    try:
        # ── Phase 1: use existing Chrome session if already logged in ────────────
        logger.info("Portal login [%s]: scanning %d Chrome profiles for session cookies %s",
                    portal, len(profile_dirs), session_names)
        existing = _best_session_profile()

        if existing:
            best_pd, found_names = existing
            logger.info("Portal login [%s]: found session cookies %s in %s — extracting",
                        portal, found_names, best_pd.name)
            cookies = read_cookies_for_domain(best_pd, portal_domain)
            if cookies:
                save_storage_state(portal, {"cookies": cookies, "origins": []})
                _login_jobs[login_id]["status"] = "success"
                logger.info("Portal login [%s]: success via existing session (%d cookies)", portal, len(cookies))
                return
            logger.warning("Portal login [%s]: session cookies found but read returned empty — proceeding to login tab", portal)

        # ── Phase 2: open login tab and poll for session cookies to appear ───────
        # Snapshot current cookie NAMES across all profiles before opening tab
        pre_names_map: dict = {}
        for pd in profile_dirs:
            names, _, _ = read_cookie_names_count(pd, portal_domain)
            pre_names_map[pd] = names

        if not open_url_in_chrome(login_url):
            _login_jobs[login_id]["status"] = "failed"
            _login_jobs[login_id]["error"] = "Could not open Chrome. Is Google Chrome installed?"
            return

        logger.info("Portal login [%s]: tab opened — waiting for session cookies %s", portal, session_names)
        await asyncio.sleep(3)

        deadline = asyncio.get_event_loop().time() + 300

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)

            for pd in profile_dirs:
                cur_names, cur_total, _ = read_cookie_names_count(pd, portal_domain)
                new_found = cur_names & session_names
                pre_found = pre_names_map[pd] & session_names

                if cur_names != pre_names_map[pd]:
                    logger.info("Portal login [%s] %s: cookies=%d, session_found=%s",
                                portal, pd.name, cur_total, new_found)

                if new_found and new_found != pre_found:
                    logger.info("Portal login [%s]: session detected in %s — %s", portal, pd.name, new_found)
                    cookies = read_cookies_for_domain(pd, portal_domain)
                    if not cookies:
                        logger.warning("Portal login [%s]: session detected but read empty — retrying", portal)
                        pre_names_map[pd] = cur_names
                        continue
                    save_storage_state(portal, {"cookies": cookies, "origins": []})
                    _login_jobs[login_id]["status"] = "success"
                    logger.info("Portal login success: %s (%d cookies from %s)", portal, len(cookies), pd.name)
                    close_chrome_tab(portal_domain)
                    return

        # Timed out
        _login_jobs[login_id]["status"] = "failed"
        _login_jobs[login_id]["error"] = (
            "Login timed out after 5 minutes. Sign in in the Chrome tab that opened."
        )
        close_chrome_tab(portal_domain)
        logger.warning("Portal login timeout: %s", portal)

    except Exception as exc:
        logger.error("Portal login error [%s]: %s", portal, exc, exc_info=True)
        _login_jobs[login_id]["status"] = "failed"
        _login_jobs[login_id]["error"] = f"Unexpected error: {exc}"


@router.post("/portal-login/{portal}")
async def start_portal_login(portal: str, background_tasks: BackgroundTasks):
    """Open a visible browser for Google SSO login to a portal.

    The browser window opens on the machine running the server.
    Poll GET /portal-login/{portal}/status?login_id=... to track progress.

    portal — one of: naukri | instahyre | wellfound
    """
    if portal not in _PORTAL_LOGIN_URLS:
        raise HTTPException(status_code=404, detail=f"Unknown portal '{portal}'. Use: naukri, instahyre, wellfound")

    login_id = str(uuid.uuid4())
    _login_jobs[login_id] = {"status": "pending", "portal": portal, "error": None}
    background_tasks.add_task(_do_portal_login, login_id, portal)

    return {
        "login_id": login_id,
        "portal": portal,
        "status": "browser_opening",
        "message": "A browser window is opening. Complete the Google login and it will close automatically.",
    }


@router.get("/portal-login/{portal}/status")
async def portal_login_status(portal: str, login_id: str):
    """Poll the status of an in-progress portal login.

    status values:
      pending        — job queued, not started yet
      browser_opened — browser is open, waiting for user to log in
      success        — login detected, session saved
      failed         — error or timeout
    """
    if login_id not in _login_jobs:
        raise HTTPException(status_code=404, detail="Login job not found — it may have expired")

    job = _login_jobs[login_id]
    if job["portal"] != portal:
        raise HTTPException(status_code=400, detail="login_id does not match portal")

    return {
        "login_id": login_id,
        "portal": portal,
        "status": job["status"],
        "error": job.get("error"),
    }


@router.delete("/portal-session/{portal}")
async def delete_portal_session(portal: str):
    """Delete the saved session for a portal, requiring re-login."""
    if portal not in _PORTAL_LOGIN_URLS:
        raise HTTPException(status_code=404, detail=f"Unknown portal '{portal}'")

    from app.scraping.portal_auth import delete_session
    deleted = delete_session(portal)
    return {"portal": portal, "deleted": deleted}


@router.get("/portal-status")
async def get_portal_status():
    """Check session state for all portal scrapers.

    Returns one entry per portal with status, session age, expiry estimate,
    and whether the session is ready to use.
    status values: present | missing | empty | corrupt | expired
    """
    from app.scraping.portal_auth import session_info

    portals = ["naukri", "instahyre", "wellfound"]
    statuses = []

    for name in portals:
        info = session_info(name)
        statuses.append({
            "portal":          name,
            "status":          info["status"],
            "ready":           info["ready"],
            "cookie_count":    info["cookie_count"],
            "age_days":        info["age_days"],
            "saved_at":        info["saved_at"],
            "expires_in_days": info["expires_in_days"],
            "session_file":    info["session_file"],
        })

    all_ready = all(s["ready"] for s in statuses)
    return {
        "all_ready": all_ready,
        "portals":   statuses,
    }


@router.get("/applications")
async def get_applications(
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """List all application records with associated job info."""
    from app.storage.models import Application, Job
    from sqlalchemy import select

    query = (
        select(Application, Job)
        .join(Job, Application.job_id == Job.id)
        .order_by(Application.created_at.desc())
        .limit(100)
    )
    if status is not None:
        query = query.where(Application.status == status)

    result = await db.execute(query)
    rows = result.all()

    return [
        {
            "id": app.id,
            "job_id": app.job_id,
            "match_id": app.match_id,
            "status": app.status,
            "applied_at": app.applied_at.isoformat() if app.applied_at else None,
            "notes": app.notes,
            "created_at": app.created_at.isoformat() if app.created_at else None,
            "job": {
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "job_url": job.job_url,
            },
        }
        for app, job in rows
    ]


@router.get("/applications/{application_id}")
async def get_application(
    application_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get a single application with associated job and tailored material."""
    from app.storage.models import Application
    from sqlalchemy import select

    result = await db.execute(
        select(Application).where(Application.id == application_id)
    )
    application = result.scalar_one_or_none()
    if application is None:
        raise HTTPException(status_code=404, detail="Application not found")

    job = await get_job(db, application.job_id)
    tailored = await get_tailored_material(db, application.job_id)

    return {
        "id": application.id,
        "status": application.status,
        "applied_at": application.applied_at.isoformat() if application.applied_at else None,
        "notes": application.notes,
        "created_at": application.created_at.isoformat() if application.created_at else None,
        "job": {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "job_url": job.job_url,
            "description": job.description,
            "source": job.source,
        } if job else None,
        "tailored_material": {
            "id": tailored.id,
            "summary": tailored.resume_summary,
            "bullets": tailored.resume_bullets,
            "keywords": tailored.keywords,
            "application_answers": tailored.application_answers,
        } if tailored else None,
    }


@router.post("/applications/{application_id}/fill")
async def fill_application(
    application_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Trigger Playwright form fill for an approved application."""
    from app.config import get_settings
    from app.storage.models import Application, Match
    from sqlalchemy import select

    result = await db.execute(
        select(Application).where(Application.id == application_id)
    )
    application = result.scalar_one_or_none()
    if application is None:
        raise HTTPException(status_code=404, detail="Application not found")

    # Verify the match is approved
    if application.match_id:
        match_result = await db.execute(
            select(Match).where(Match.id == application.match_id)
        )
        match = match_result.scalar_one_or_none()
        if match and match.status != "approved":
            raise HTTPException(
                status_code=400,
                detail=f"Match must be approved before filling. Current status: {match.status}",
            )

    job = await get_job(db, application.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Associated job not found")

    tailored = await get_tailored_material(db, application.job_id)

    settings = get_settings()

    tailored_material = {}
    answers: dict[str, str] = {}

    if tailored:
        tailored_material = {
            "summary": tailored.resume_summary or "",
            "bullets": tailored.resume_bullets or [],
            "keywords": tailored.keywords or [],
        }
        answers = tailored.application_answers or {}

    async def _do_fill():
        from app.automation.form_filler import FormFiller
        from app.storage.db import AsyncSessionLocal
        from app.llm.provider_factory import get_provider

        _settings = get_settings()
        _llm = get_provider(_settings, task="scoring")
        filler = FormFiller(llm=_llm)
        result = await filler.fill(
            application_url=job.job_url,
            tailored_material=tailored_material,
            resume_path=settings.resume_path,
            answers=answers,
            auto_submit=settings.auto_submit,
        )
        async with AsyncSessionLocal() as fill_db:
            new_status = "submitted" if result.get("submitted") else "filled"
            await update_application_status(fill_db, application_id, new_status)
            await fill_db.commit()
        logger.info("Form fill result for application %s: %s", application_id, result)

    background_tasks.add_task(_do_fill)

    return {
        "application_id": application_id,
        "status": "fill_started",
        "job_url": job.job_url,
        "auto_submit": settings.auto_submit,
        "message": "Form fill started in background",
    }



@router.get("/applications/{application_id}/resume-preview")
async def resume_preview(
    application_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Return an HTML preview of the tailored resume as it would be submitted."""
    from fastapi.responses import HTMLResponse
    from app.storage.models import Application
    from sqlalchemy import select
    import os

    result = await db.execute(select(Application).where(Application.id == application_id))
    application = result.scalar_one_or_none()
    if application is None:
        raise HTTPException(status_code=404, detail="Application not found")

    job = await get_job(db, application.job_id)
    tailored = await get_tailored_material(db, application.job_id) if job else None

    if not tailored:
        return HTMLResponse("<p style='font-family:sans-serif;color:#888;padding:40px'>No tailored material yet.</p>")

    tm_summary = tailored.resume_summary or ""
    tm_bullets = tailored.resume_bullets or []
    tm_keywords = tailored.keywords or []
    resume_data = (tailored.application_answers or {}).get("_resume_data", {})

    role_history = resume_data.get("role_history", [])
    education = resume_data.get("education", [])

    first = os.environ.get("APPLICANT_FIRST_NAME", "")
    last = os.environ.get("APPLICANT_LAST_NAME", "")
    email = os.environ.get("APPLICANT_EMAIL", "")
    phone = os.environ.get("APPLICANT_PHONE", "")
    linkedin = os.environ.get("APPLICANT_LINKEDIN", "")

    name = f"{first} {last}".strip() or "Candidate"

    # Distribute bullets across roles
    bullets_per_role = _distribute_bullets(tm_bullets, role_history)

    roles_html = ""
    for i, role in enumerate(role_history):
        role_bullets = bullets_per_role.get(i, [])
        bullets_html = "".join(f"<li>{_esc(b)}</li>" for b in role_bullets)
        roles_html += f"""
        <div class="role">
          <div class="role-header">
            <span class="role-title">{_esc(role.get('title',''))}</span>
            <span class="role-duration">{_esc(role.get('duration',''))}</span>
          </div>
          <div class="role-company">{_esc(role.get('company',''))}</div>
          {"<ul>" + bullets_html + "</ul>" if bullets_html else ""}
        </div>"""

    edu_html = "".join(f"<li>{_esc(e)}</li>" for e in education)
    kw_html = "".join(f"<span class='kw'>{_esc(k)}</span>" for k in tm_keywords)

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Tailored Resume — {_esc(name)}</title>
<style>
  body{{font-family:'Segoe UI',Arial,sans-serif;max-width:820px;margin:40px auto;padding:0 24px;color:#1a1a1a;font-size:14px;line-height:1.6}}
  .resume-header{{text-align:center;border-bottom:2px solid #1a1a1a;padding-bottom:12px;margin-bottom:20px}}
  h1{{font-size:22px;font-weight:800;margin:0 0 4px;letter-spacing:0.5px}}
  .contact{{font-size:12px;color:#444;margin:4px 0}}
  .contact a{{color:#444;text-decoration:none}}
  .section-title{{font-size:12px;font-weight:800;letter-spacing:1.5px;text-transform:uppercase;border-bottom:1px solid #1a1a1a;margin:18px 0 8px;padding-bottom:3px}}
  .summary{{font-size:13.5px;line-height:1.7;margin-bottom:4px}}
  .role{{margin-bottom:14px}}
  .role-header{{display:flex;justify-content:space-between;font-weight:700;font-size:13.5px}}
  .role-company{{font-size:12.5px;color:#555;margin-bottom:4px}}
  .role-duration{{font-weight:400;font-size:12px;color:#555}}
  ul{{margin:4px 0 0 16px;padding:0}}
  li{{margin-bottom:3px;font-size:13px}}
  .kw{{display:inline-block;background:#f0f0f0;border-radius:3px;padding:2px 7px;margin:2px;font-size:11.5px}}
  .job-tag{{background:#f0f0f0;border-radius:4px;padding:3px 10px;font-size:12px;color:#555;display:inline-block;margin-bottom:14px}}
</style>
</head><body>
<div class="resume-header">
  <h1>{_esc(name)}</h1>
  <div class="contact">
    {_esc(phone)}{' | ' if phone and email else ''}{_esc(email)}{' | ' if email and linkedin else ''}
    {'<a href="' + _esc(linkedin) + '">' + _esc(linkedin) + '</a>' if linkedin else ''}
  </div>
</div>
{"<div class='job-tag'>Tailored for: " + _esc((job.title or '') + ' at ' + (job.company or '')) + "</div>" if job else ""}
{"<div class='section-title'>Professional Summary</div><p class='summary'>" + _esc(tm_summary) + "</p>" if tm_summary else ""}
{"<div class='section-title'>Professional Experience</div>" + roles_html if roles_html else ""}
{"<div class='section-title'>ATS Keywords Matched</div><div style='margin-top:6px'>" + kw_html + "</div>" if kw_html else ""}
{"<div class='section-title'>Education</div><ul>" + edu_html + "</ul>" if edu_html else ""}
</body></html>"""

    return HTMLResponse(html)


def _esc(s: str) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _distribute_bullets(bullets: list[str], roles: list[dict]) -> dict[int, list[str]]:
    """Spread tailored bullets across roles proportionally."""
    if not roles or not bullets:
        return {0: bullets} if bullets else {}
    per_role = max(1, len(bullets) // len(roles))
    result, idx = {}, 0
    for i, _ in enumerate(roles):
        count = per_role if i < len(roles) - 1 else len(bullets) - idx
        result[i] = bullets[idx: idx + count]
        idx += count
    return result


# ---------------------------------------------------------------------------
# Search Roles — LLM-detected + user customisations
# ---------------------------------------------------------------------------

@router.get("/search-roles")
async def get_search_roles():
    """Return current search roles (detected from resume + user additions)."""
    from app.scraping.search_roles import get_roles, get_all_search_terms
    data = get_roles()
    return {
        "roles": data.get("roles", []),
        "custom_roles": data.get("custom_roles", []),
        "all_terms": get_all_search_terms(),
        "detected_from_resume": data.get("detected_from_resume", False),
    }


class RoleRequest(BaseModel):
    role: str


@router.post("/search-roles/add")
async def add_search_role(req: RoleRequest):
    """Add a custom role to the search list."""
    from app.scraping.search_roles import add_custom_role
    data = add_custom_role(req.role.strip())
    return {"status": "added", "custom_roles": data.get("custom_roles", [])}


@router.post("/search-roles/remove")
async def remove_search_role(req: RoleRequest):
    """Remove a role from detected or custom list."""
    from app.scraping.search_roles import remove_role
    data = remove_role(req.role.strip())
    return {"status": "removed", "roles": data.get("roles", []), "custom_roles": data.get("custom_roles", [])}


@router.post("/search-roles/detect")
async def detect_search_roles(background_tasks: BackgroundTasks):
    """Re-detect search roles from resume using LLM (runs in background)."""
    async def _detect():
        from app.config import get_settings
        from app.llm.provider_factory import get_provider
        from app.scraping.search_roles import detect_roles_from_resume
        settings = get_settings()
        llm = get_provider(settings, task="scoring")
        detect_roles_from_resume(settings.resume_path, llm, force=True)
        logger.info("Role detection complete")

    background_tasks.add_task(_detect)
    return {"status": "detecting", "message": "Detecting roles from resume — refresh in ~10s"}


# ---------------------------------------------------------------------------
# Admin utilities
# ---------------------------------------------------------------------------

@router.post("/admin/clear-jobs")
async def clear_all_jobs(db: AsyncSession = Depends(get_db)):
    """Delete all jobs, matches, and applications from the database."""
    from sqlalchemy import text
    await db.execute(text("DELETE FROM tailored_materials"))
    await db.execute(text("DELETE FROM applications"))
    await db.execute(text("DELETE FROM matches"))
    await db.execute(text("DELETE FROM jobs"))
    await db.commit()
    logger.info("All jobs, matches, applications, and tailored materials cleared")
    return {"status": "cleared", "message": "All job data deleted — ready for fresh scrape"}


@router.get("/jobs")
async def get_jobs(
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """List scraped jobs with pagination."""
    import math

    def _safe_float(v):
        if v is None:
            return None
        try:
            return None if (math.isnan(v) or math.isinf(v)) else v
        except Exception:
            return None

    jobs = await list_jobs(db, limit=limit, offset=offset)
    return [
        {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "job_url": job.job_url,
            "salary_min": _safe_float(job.salary_min),
            "salary_max": _safe_float(job.salary_max),
            "currency": job.currency,
            "source": job.source,
            "date_posted": job.date_posted.isoformat() if job.date_posted else None,
            "scraped_at": job.scraped_at.isoformat() if job.scraped_at else None,
            "is_active": job.is_active,
        }
        for job in jobs
    ]


# ---------------------------------------------------------------------------
# Outreach routes
# ---------------------------------------------------------------------------


class OutreachRunRequest(BaseModel):
    cap: int = 15
    dry_run: bool = False


class OutreachDraftJobRequest(BaseModel):
    company: str
    title: str = ""
    source: str = ""
    domain: str = ""
    job_url: str = ""
    description: str = ""
    contact_name: str = "Hiring Team"
    contact_title: str = ""
    contact_type: str = "hiring_team"
    tech_stack: str = ""
    match_reason: str = ""


@router.post("/outreach/run")
async def outreach_run(request: OutreachRunRequest):
    """Start the outreach pipeline in a background subprocess.

    Body:
        cap     — max emails to draft today (default 15)
        dry_run — if true, skip Gmail draft creation

    Returns: {"status": "started"} or {"status": "already_running"}
    """
    global _outreach_process

    # Check if still running
    if _outreach_process is not None and _outreach_process.poll() is None:
        return {"status": "already_running"}

    project_root = Path(__file__).parent.parent.parent
    python_bin = project_root / ".venv" / "bin" / "python"
    if not python_bin.exists():
        python_bin = Path(sys.executable)

    cmd = [str(python_bin), str(project_root / "outreach" / "main.py"), "--cap", str(request.cap)]
    if request.dry_run:
        cmd.append("--dry-run")

    log_path = project_root / "outreach" / "pipeline.log"
    log_file = open(log_path, "a")  # noqa: WPS515 — intentionally kept open by subprocess

    _outreach_process = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(project_root),
    )
    logger.info("Outreach pipeline started (pid=%d cap=%d dry_run=%s)", _outreach_process.pid, request.cap, request.dry_run)
    return {"status": "started"}


@router.get("/outreach/status")
async def outreach_status():
    """Return whether the pipeline subprocess is running and today's draft count.

    Returns: {"running": bool, "drafts_today": int, "cap": int}
    """
    global _outreach_process

    running = _outreach_process is not None and _outreach_process.poll() is None

    try:
        from readme_store import get_todays_draft_count, read_config
        drafts_today = get_todays_draft_count()
        cfg = read_config()
        cap = int(cfg.get("DAILY_CAP", 15))
    except Exception as exc:
        logger.warning("outreach/status: could not read readme_store: %s", exc)
        drafts_today = 0
        cap = 15

    return {"running": running, "drafts_today": drafts_today, "cap": cap}


@router.post("/outreach/draft-job")
async def outreach_draft_job(request: OutreachDraftJobRequest):
    """Create a Gmail draft for a single job on demand (no pipeline needed).

    Called from the UI Jobs tab "Draft Email" button.
    Uses the full outreach email drafting stack: guess_emails → draft_email → create_gmail_draft.
    Appends a tracking row on success.

    Returns: {success, email, all_emails, subject, error?}
    """
    import json as _json
    from datetime import date as _date

    try:
        from email_drafter import create_gmail_draft, draft_email, guess_emails
        from readme_store import append_tracking_row, read_config
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not load outreach modules: {exc}")

    config = read_config()

    contact_name = request.contact_name or "Hiring Team"
    contact_type = request.contact_type or "hiring_team"
    contact_title = request.contact_title or ""
    domain = request.domain

    if not domain:
        raise HTTPException(status_code=400, detail=f"No domain for {request.company} — cannot guess email")

    # Step 1: guess emails
    try:
        email_candidates = guess_emails(contact_name, domain, request.company, contact_type=contact_type)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Email guessing failed: {exc}")

    if not email_candidates or not email_candidates[0]:
        raise HTTPException(status_code=400, detail=f"Could not generate email for domain {domain}")

    # Step 2: draft email
    job_dict = {
        "company": request.company,
        "title": request.title,
        "description": request.description,
        "domain": domain,
        "job_url": request.job_url,
        "source": request.source,
    }
    try:
        draft = draft_email(
            job=job_dict,
            contact_name=contact_name,
            tech_stack_mentioned=request.tech_stack,
            email_candidates=email_candidates,
            contact_type=contact_type,
            contact_title=contact_title,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Email drafting failed: {exc}")

    # Step 3: create Gmail draft
    try:
        success = create_gmail_draft(
            to_email=email_candidates[0],
            subject=draft.get("subject", ""),
            body=draft.get("body", ""),
            resume_path=config.get("RESUME_PATH", "outreach/base_resume.pdf"),
            credentials_path=config.get("GMAIL_CREDENTIALS_PATH", "outreach/google_credentials.json"),
            token_path=config.get("TOKEN_PATH", "outreach/gmail_token.json"),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gmail API failed: {exc}")

    if not success:
        raise HTTPException(status_code=500, detail="Gmail draft creation returned False")

    # Step 4: append tracking row
    append_tracking_row({
        "date": str(_date.today()),
        "company": request.company,
        "contact": f"{contact_name} ({contact_title})" if contact_title else contact_name,
        "domain": domain,
        "email_guessed": email_candidates[0],
        "draft_created": "true",
        "status": "draft_created",
        "notes": request.match_reason or "",
    })

    # Step 5: mark drafted in scored_jobs.json
    project_root = Path(__file__).parent.parent.parent
    scored_path = project_root / "outreach" / "scored_jobs.json"
    try:
        if scored_path.exists():
            rows = _json.loads(scored_path.read_text(encoding="utf-8"))
            for row in rows:
                if row.get("company") == request.company:
                    row["drafted"] = True
                    break
            scored_path.write_text(_json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # non-critical

    logger.info("Manual draft created: %s → %s", request.company, email_candidates[0])
    return {
        "success": True,
        "email": email_candidates[0],
        "all_emails": email_candidates,
        "subject": draft.get("subject", ""),
    }


@router.get("/outreach/tracking")
async def outreach_tracking():
    """Return the tracking table rows parsed from outreach/README.md.

    Returns: list of row dicts with keys: date, company, contact, domain,
             email_guessed, draft_created, status, notes
    """
    try:
        from readme_store import read_tracking
        return read_tracking()
    except Exception as exc:
        logger.error("outreach/tracking: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/outreach/jobs")
async def outreach_jobs():
    """Return scored job leads from the last outreach pipeline run.

    Reads outreach/scored_jobs.json written by main.py after each scoring phase.
    Falls back to an empty list when the pipeline has not yet produced results.

    Returns:
        {
          "jobs": [...],          # list of scored job dicts
          "last_run": "ISO string" | null,   # mtime of scored_jobs.json
          "total": int
        }
    """
    import json as _json
    import os as _os
    from datetime import timezone as _tz
    project_root = Path(__file__).parent.parent.parent
    scored_path = project_root / "outreach" / "scored_jobs.json"

    if not scored_path.exists():
        return {"jobs": [], "last_run": None, "total": 0}

    try:
        mtime = _os.path.getmtime(str(scored_path))
        from datetime import datetime as _dt
        last_run = _dt.fromtimestamp(mtime, tz=_tz.utc).isoformat()

        data = _json.loads(scored_path.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, list):
            data.sort(key=lambda x: (-int(x.get("relevant", False)), -x.get("score", 0)))
            return {"jobs": data, "last_run": last_run, "total": len(data)}
    except Exception as exc:
        logger.warning("outreach/jobs: scored_jobs.json parse error: %s", exc)

    return {"jobs": [], "last_run": None, "total": 0}


@router.get("/outreach/resume")
async def outreach_resume():
    """Return the parsed resume dict from outreach/resume_cache.json.

    Returns the cached dict produced by resume_parser.load_resume().
    """
    try:
        from resume_parser import load_resume
        return load_resume()
    except Exception as exc:
        logger.error("outreach/resume: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/outreach/log")
async def outreach_log(lines: int = 50):
    """Return the last N lines from outreach/pipeline.log.

    Query param:
        lines — number of tail lines to return (default 50)

    Returns: {"lines": [...]}
    """
    project_root = Path(__file__).parent.parent.parent
    log_path = project_root / "outreach" / "pipeline.log"

    if not log_path.exists():
        return {"lines": []}

    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        all_lines = content.splitlines()
        # Deduplicate consecutive identical lines (caused by old FileHandler + stdout redirect)
        deduped: list[str] = []
        for line in all_lines:
            if not deduped or line != deduped[-1]:
                deduped.append(line)
        tail = deduped[-lines:] if len(deduped) > lines else deduped
        return {"lines": tail}
    except Exception as exc:
        logger.error("outreach/log: %s", exc)
        return {"lines": [f"Error reading log: {exc}"]}
