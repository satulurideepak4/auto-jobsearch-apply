from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.llm.base import LLMProvider

logger = logging.getLogger(__name__)


class AnswerGenerator:
    """Generates answers to job application questions using resume data and an LLM."""

    def __init__(self, llm: "LLMProvider") -> None:
        self.llm = llm

    def generate(self, question: str, job: dict, resume_data: dict) -> str:
        """Generate a concise answer to a single application question.

        Args:
            question: The application question text.
            job: Normalised job dict (title, company, description).
            resume_data: Parsed resume dict from ResumeParser.

        Returns:
            Plain-text answer string (under 300 words).
        """
        job_title = job.get("title", "")
        job_company = job.get("company", "")
        # Use a short excerpt of the description to provide context
        job_description_excerpt = job.get("description", "")[:1500]

        skills = resume_data.get("skills", [])
        current_role = resume_data.get("current_role", "")
        years_exp = resume_data.get("years_of_experience", 0)
        summary = resume_data.get("summary", "")
        role_history = resume_data.get("role_history", [])

        role_history_text = "\n".join(
            f"  - {r.get('title', '')} at {r.get('company', '')} ({r.get('duration', '')})"
            for r in role_history
        )

        prompt = f"""You are an expert job application specialist helping a candidate craft a compelling, ATS-aware answer to an application question.

STRICT RULES:
1. Only use information explicitly present in the candidate's resume below. Do NOT invent experience, metrics, companies, or facts.
2. Write in first person ("I have…", "In my role at…").
3. Target 150–250 words; hard cap at 300.
4. Mirror terminology from the job description wherever it matches the candidate's real experience — this signals keyword alignment to ATS filters and hiring managers.
5. Lead with the most relevant point; do not bury the answer in setup or preamble.
6. Be specific: name tools, outcomes, or situations from the resume rather than making generic claims.
7. Close with one forward-looking sentence connecting the candidate's background to this specific role.
8. NEVER use the em dash character (—) anywhere; use a comma, colon, or rewrite the phrase instead.

JOB CONTEXT:
  Role       : {job_title} at {job_company}
  Description: {job_description_excerpt}

CANDIDATE RESUME:
  Current Role       : {current_role}
  Years of Experience: {years_exp}
  Skills             : {", ".join(skills)}
  Summary            : {summary}
  Role History:
{role_history_text}

APPLICATION QUESTION:
{question}

Provide only the answer text. No preamble, no labels, no JSON.
"""

        answer = self.llm.generate(prompt).strip().replace("—", ",").replace("–", "-")
        logger.info(
            "Generated answer for question: %r (job: %s @ %s)",
            question[:80],
            job_title,
            job_company,
        )
        return answer

    def generate_batch(
        self,
        questions: list[str],
        job: dict,
        resume_data: dict,
    ) -> dict[str, str]:
        """Generate answers for multiple questions.

        Args:
            questions: List of application question strings.
            job: Normalised job dict.
            resume_data: Parsed resume dict.

        Returns:
            Dict mapping each question string to its generated answer.
        """
        results: dict[str, str] = {}
        for question in questions:
            try:
                results[question] = self.generate(question, job, resume_data)
            except Exception as exc:
                logger.error("Failed to generate answer for question %r: %s", question[:80], exc)
                results[question] = ""
        return results
