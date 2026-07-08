from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings

logger = logging.getLogger(__name__)


async def run_nightly_pipeline() -> None:
    """Execute the full scrape → score → persist pipeline."""
    import asyncio
    from app.llm.provider_factory import get_provider
    from app.matching.resume_parser import get_resume_data
    from app.matching.scorer import JobScorer
    from app.scraping.jobspy_client import scrape_india_profiles, scrape_intl_profiles, scrape_portal_jobs
    from app.storage.db import AsyncSessionLocal
    from app.storage.repository import save_match, upsert_job

    settings = get_settings()
    scoring_llm = get_provider(settings, task="scoring")
    scorer = JobScorer(scoring_llm, settings)
    loop = asyncio.get_event_loop()

    logger.info("Nightly pipeline: starting scrape...")
    # Synchronous jobspy calls offloaded to thread pool — don't block event loop
    india_jobs = await loop.run_in_executor(None, scrape_india_profiles, settings)
    intl_jobs = await loop.run_in_executor(None, scrape_intl_profiles, settings)
    portal_jobs = await scrape_portal_jobs(settings)
    all_jobs = india_jobs + intl_jobs + portal_jobs

    logger.info("Nightly pipeline: %d total jobs fetched", len(all_jobs))

    try:
        resume_data = await loop.run_in_executor(None, get_resume_data, settings.resume_path, scoring_llm)
    except Exception as exc:
        logger.error("Nightly pipeline: failed to parse resume at %s: %s", settings.resume_path, exc)
        resume_data = {}

    above_threshold = 0
    async with AsyncSessionLocal() as db:
        for job_data in all_jobs:
            try:
                job = await upsert_job(db, job_data)
                # LLM scoring offloaded to thread — keeps event loop free
                score_data = await loop.run_in_executor(None, scorer.score, job_data, resume_data)
                if score_data["score"] >= settings.match_score_threshold:
                    await save_match(db, job.id, score_data)
                    above_threshold += 1
            except Exception as exc:
                logger.error(
                    "Nightly pipeline: error processing job '%s': %s",
                    job_data.get("title"),
                    exc,
                )

        await db.commit()

    logger.info(
        "Nightly pipeline complete: %d jobs scraped, %d matches above threshold=%d",
        len(all_jobs),
        above_threshold,
        settings.match_score_threshold,
    )


def create_scheduler(app) -> AsyncIOScheduler:
    """Create and configure the APScheduler instance.

    The scheduler is configured with a nightly cron job but NOT started here.
    Call scheduler.start() in the application lifespan handler.

    Args:
        app: The FastAPI application instance (unused directly, kept for
             consistency with FastAPI lifespan patterns).

    Returns:
        A configured (not yet started) AsyncIOScheduler.
    """
    settings = get_settings()
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        run_nightly_pipeline,
        trigger="cron",
        hour=settings.scrape_cron_hour,
        minute=settings.scrape_cron_minute,
        id="nightly_scrape",
        replace_existing=True,
    )

    logger.info(
        "Scheduler configured: nightly pipeline at %02d:%02d UTC",
        settings.scrape_cron_hour,
        settings.scrape_cron_minute,
    )

    return scheduler
