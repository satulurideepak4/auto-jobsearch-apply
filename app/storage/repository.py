from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import Application, Job, Match, TailoredMaterial


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------


async def upsert_job(session: AsyncSession, job_data: dict) -> Job:
    """Insert a new job or update an existing one matched by job_url.

    Args:
        session: Active async database session.
        job_data: Normalised job dict (from JobSpyClient).

    Returns:
        The persisted Job ORM instance.
    """
    job_url = job_data.get("job_url", "")

    # Try to find an existing job with the same URL
    result = await session.execute(select(Job).where(Job.job_url == job_url))
    existing = result.scalar_one_or_none()

    if existing is not None:
        # Update mutable fields
        existing.title = job_data.get("title", existing.title)
        existing.company = job_data.get("company", existing.company)
        existing.location = job_data.get("location", existing.location)
        existing.description = job_data.get("description", existing.description)
        existing.salary_min = job_data.get("salary_min", existing.salary_min)
        existing.salary_max = job_data.get("salary_max", existing.salary_max)
        existing.currency = job_data.get("currency", existing.currency)
        existing.raw_data = job_data.get("raw_data", existing.raw_data)
        existing.source = job_data.get("source", existing.source)
        existing.scraped_at = datetime.utcnow()
        existing.updated_at = datetime.utcnow()
        await session.flush()
        return existing

    # Insert new job
    new_job = Job(
        id=str(uuid.uuid4()),
        title=job_data.get("title", ""),
        company=job_data.get("company", ""),
        location=job_data.get("location", ""),
        description=job_data.get("description"),
        job_url=job_url,
        salary_min=job_data.get("salary_min"),
        salary_max=job_data.get("salary_max"),
        currency=job_data.get("currency"),
        date_posted=_parse_date(job_data.get("date_posted")),
        source=job_data.get("source", ""),
        raw_data=job_data.get("raw_data"),
        scraped_at=datetime.utcnow(),
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(new_job)
    await session.flush()
    return new_job


def _parse_date(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        from datetime import date

        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


async def get_job(session: AsyncSession, job_id: str) -> Optional[Job]:
    """Fetch a single Job by primary key.

    Args:
        session: Active async database session.
        job_id: UUID string of the job.

    Returns:
        Job ORM instance or None if not found.
    """
    result = await session.execute(select(Job).where(Job.id == job_id))
    return result.scalar_one_or_none()


async def list_jobs(
    session: AsyncSession,
    limit: int = 100,
    offset: int = 0,
) -> list[Job]:
    """List jobs with pagination.

    Args:
        session: Active async database session.
        limit: Maximum records to return.
        offset: Number of records to skip.

    Returns:
        List of Job ORM instances.
    """
    result = await session.execute(
        select(Job).order_by(Job.scraped_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Match CRUD
# ---------------------------------------------------------------------------


async def save_match(session: AsyncSession, job_id: str, score_data: dict) -> Match:
    """Persist a new Match record for a job.

    Args:
        session: Active async database session.
        job_id: UUID string of the related Job.
        score_data: Dict from JobScorer.score() with score, reasoning, etc.

    Returns:
        The persisted Match ORM instance.
    """
    match = Match(
        id=str(uuid.uuid4()),
        job_id=job_id,
        score=int(score_data.get("score", 0)),
        reasoning=score_data.get("reasoning"),
        matched_skills=score_data.get("matched_skills", []),
        missing_skills=score_data.get("missing_skills", []),
        status="pending",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(match)
    await session.flush()
    return match


async def list_matches(
    session: AsyncSession,
    status: str = "pending",
    limit: int = 100,
) -> list[Match]:
    """List Match records filtered by status.

    Args:
        session: Active async database session.
        status: Filter status ('pending', 'approved', 'rejected').
        limit: Maximum records to return.

    Returns:
        List of Match ORM instances.
    """
    result = await session.execute(
        select(Match)
        .where(Match.status == status)
        .order_by(Match.score.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def update_match_status(
    session: AsyncSession, match_id: str, status: str
) -> Match:
    """Update the status of a Match record.

    Args:
        session: Active async database session.
        match_id: UUID string of the Match.
        status: New status value.

    Returns:
        Updated Match ORM instance.

    Raises:
        ValueError: If the match_id does not exist.
    """
    result = await session.execute(select(Match).where(Match.id == match_id))
    match = result.scalar_one_or_none()
    if match is None:
        raise ValueError(f"Match '{match_id}' not found")
    match.status = status
    match.updated_at = datetime.utcnow()
    await session.flush()
    return match


# ---------------------------------------------------------------------------
# TailoredMaterial CRUD
# ---------------------------------------------------------------------------


async def save_tailored_material(
    session: AsyncSession, job_id: str, material: dict
) -> TailoredMaterial:
    """Persist tailored material for a job.

    Args:
        session: Active async database session.
        job_id: UUID string of the related Job.
        material: Dict from ResumeTailor.tailor() with summary, bullets, keywords.

    Returns:
        The persisted TailoredMaterial ORM instance.
    """
    tm = TailoredMaterial(
        id=str(uuid.uuid4()),
        job_id=job_id,
        resume_summary=material.get("summary"),
        resume_bullets=material.get("bullets", []),
        keywords=material.get("keywords", []),
        application_answers=material.get("application_answers"),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(tm)
    await session.flush()
    return tm


async def get_tailored_material(
    session: AsyncSession, job_id: str
) -> Optional[TailoredMaterial]:
    """Fetch the most recent TailoredMaterial for a job.

    Args:
        session: Active async database session.
        job_id: UUID string of the related Job.

    Returns:
        TailoredMaterial ORM instance or None.
    """
    result = await session.execute(
        select(TailoredMaterial)
        .where(TailoredMaterial.job_id == job_id)
        .order_by(TailoredMaterial.created_at.desc())
    )
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Application CRUD
# ---------------------------------------------------------------------------


async def create_application(
    session: AsyncSession,
    job_id: str,
    match_id: Optional[str] = None,
) -> Application:
    """Create a new Application record.

    Args:
        session: Active async database session.
        job_id: UUID string of the related Job.
        match_id: Optional UUID string of the related Match.

    Returns:
        The persisted Application ORM instance.
    """
    app = Application(
        id=str(uuid.uuid4()),
        job_id=job_id,
        match_id=match_id,
        status="pending",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(app)
    await session.flush()
    return app


async def update_application_status(
    session: AsyncSession,
    application_id: str,
    status: str,
    notes: Optional[str] = None,
) -> Application:
    """Update the status (and optionally notes) of an Application.

    Args:
        session: Active async database session.
        application_id: UUID string of the Application.
        status: New status value.
        notes: Optional notes string to store.

    Returns:
        Updated Application ORM instance.

    Raises:
        ValueError: If the application_id does not exist.
    """
    result = await session.execute(
        select(Application).where(Application.id == application_id)
    )
    application = result.scalar_one_or_none()
    if application is None:
        raise ValueError(f"Application '{application_id}' not found")

    application.status = status
    application.updated_at = datetime.utcnow()

    if status in ("submitted", "filled"):
        application.applied_at = datetime.utcnow()

    if notes is not None:
        application.notes = notes

    await session.flush()
    return application


async def list_applications(
    session: AsyncSession,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[Application]:
    """List Application records with optional status filter.

    Args:
        session: Active async database session.
        status: Optional status filter.
        limit: Maximum records to return.

    Returns:
        List of Application ORM instances.
    """
    query = select(Application).order_by(Application.created_at.desc()).limit(limit)
    if status is not None:
        query = query.where(Application.status == status)
    result = await session.execute(query)
    return list(result.scalars().all())
