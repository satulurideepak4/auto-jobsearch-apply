from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.config import Settings
    from app.llm.base import LLMProvider

logger = logging.getLogger(__name__)


class JobScorer:
    """Scores a job against a parsed resume using an LLM."""

    def __init__(self, llm: "LLMProvider", settings: Optional["Settings"] = None) -> None:
        self.llm = llm
        self._settings = settings

    def _get_settings(self) -> "Settings":
        if self._settings is not None:
            return self._settings
        from app.config import get_settings

        return get_settings()

    def _below_salary_floor(self, job: dict) -> bool:
        """Return True if the job salary is definitively below the configured floor."""
        settings = self._get_settings()
        salary_min = job.get("salary_min")
        salary_max = job.get("salary_max")
        currency = (job.get("currency") or "").upper()

        # Use the higher of min/max as the reference salary
        ref_salary = salary_max if salary_max is not None else salary_min
        if ref_salary is None:
            return False  # No salary data — cannot apply floor

        if currency == "INR" and settings.salary_floor_inr is not None:
            return ref_salary < settings.salary_floor_inr

        if currency in ("USD", "US", "") and settings.salary_floor_usd is not None:
            return ref_salary < settings.salary_floor_usd

        return False

    def score(self, job: dict, resume_data: dict) -> dict:
        """Score a job against resume data.

        Applies salary floor hard filter first, then calls the LLM for scoring.

        Args:
            job: Normalised job dict (from JobSpyClient).
            resume_data: Parsed resume dict (from ResumeParser).

        Returns:
            Dict with keys: score (int 0-100), reasoning (str),
            matched_skills (list[str]), missing_skills (list[str]).
        """
        # Hard filter: salary floor
        if self._below_salary_floor(job):
            logger.info(
                "Job '%s' at '%s' is below salary floor — score=0",
                job.get("title"),
                job.get("company"),
            )
            return {
                "score": 0,
                "reasoning": "Below salary floor",
                "matched_skills": [],
                "missing_skills": [],
            }

        skills = resume_data.get("skills", [])
        experience = resume_data.get("years_of_experience", 0)
        summary = resume_data.get("summary", "")
        current_role = resume_data.get("current_role", "")

        job_title = job.get("title", "")
        job_description = job.get("description", "")[:3000]  # Truncate to save tokens

        prompt = f"""You are a recruitment expert. Score how well the following candidate matches the job posting.

Candidate profile:
- Current role: {current_role}
- Years of experience: {experience}
- Skills: {", ".join(skills)}
- Summary: {summary}

Job posting:
- Title: {job_title}
- Description:
{job_description}

Return a single JSON object with exactly these keys:
- "score": integer 0-100 (how well the candidate matches)
- "reasoning": string, 1-2 sentences explaining the score
- "matched_skills": list of strings — skills the candidate has that are relevant to this job
- "missing_skills": list of strings — important skills the job requires that the candidate lacks

Return ONLY the JSON object. No markdown fences, no extra text.
"""

        raw_response = self.llm.generate(prompt)

        # Strip markdown code fences
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_response.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())

        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error(
                "Failed to parse scorer JSON for job '%s': %s", job_title, exc
            )
            logger.debug("Raw LLM response: %s", raw_response)
            result = {
                "score": 0,
                "reasoning": "Failed to parse LLM scoring response.",
                "matched_skills": [],
                "missing_skills": [],
            }

        # Ensure score is an integer in [0, 100]
        try:
            result["score"] = max(0, min(100, int(result.get("score", 0))))
        except (TypeError, ValueError):
            result["score"] = 0

        return result
