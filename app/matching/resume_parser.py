from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pdfplumber

if TYPE_CHECKING:
    from app.llm.base import LLMProvider

logger = logging.getLogger(__name__)

# Module-level cache: path → parsed dict
_resume_cache: dict[str, dict] = {}


class ResumeParser:
    """Parses a PDF or DOCX resume and extracts structured data using an LLM."""

    def __init__(self, llm: "LLMProvider") -> None:
        self.llm = llm

    def _extract_text(self, path: str) -> str:
        """Extract raw text from a PDF or DOCX file."""
        suffix = Path(path).suffix.lower()
        if suffix == ".docx":
            return self._extract_text_docx(path)
        return self._extract_text_pdf(path)

    def _extract_text_pdf(self, path: str) -> str:
        text_parts: list[str] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return "\n".join(text_parts)

    def _extract_text_docx(self, path: str) -> str:
        import docx  # lazy import — only needed when a .docx is supplied
        doc = docx.Document(path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    def parse(self, path: str) -> dict:
        """Parse a PDF or DOCX resume and return structured data.

        Args:
            path: Filesystem path to the resume (.pdf or .docx).

        Returns:
            Dict with keys: skills, years_of_experience, current_role,
            role_history, education, summary, target_roles, search_terms.
        """
        raw_text = self._extract_text(path)

        prompt = f"""You are a resume parser and expert technical recruiter. Given the following resume text, extract structured information and return it as a single JSON object with exactly these keys:

- "skills": list of strings — technical and professional skills
- "years_of_experience": integer — total years of professional experience
- "current_role": string — most recent job title
- "role_history": list of dicts, each with keys:
    "title" (str) — job title,
    "company" (str) — company name,
    "duration" (str) — date range (e.g. "Jan 2022 – Mar 2024"),
    "bullets" (list[str]) — the exact achievement/responsibility bullet points for this role, copied verbatim from the resume text; preserve all numbers, metrics, and proper nouns
- "education": list of strings — degrees and institutions
- "summary": string — 2-3 sentence professional summary
- "target_roles": list of 4-6 specific job titles this person should apply for based on their actual experience and skills — be precise (e.g. "Backend Software Engineer", "Senior Java Developer", "Platform Engineer"), NOT generic (avoid "Software Engineer" alone)
- "search_terms": list of 8-12 specific search query strings for job boards (Indeed, Naukri, etc.) — 3-6 words each, specific to this person's stack and seniority (e.g. "Senior Java Backend Engineer", "Backend Engineer Python Microservices", "Platform Engineer Kubernetes Remote") — these will be used as search_term in job scraping

Return ONLY the JSON object. Do not include markdown fences or any other text.

Resume:
{raw_text}
"""
        raw_response = self.llm.generate(prompt)

        # Strip markdown code fences if the LLM added them
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_response.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse resume JSON from LLM response: %s", exc)
            logger.debug("Raw LLM response: %s", raw_response)
            # Return a safe default structure
            data = {
                "skills": [],
                "years_of_experience": 0,
                "current_role": "",
                "role_history": [],
                "education": [],
                "summary": raw_text[:500],
                "target_roles": [],
                "search_terms": [],
            }

        return data


def get_resume_data(path: str, llm: "LLMProvider") -> dict:
    """Parse a resume once and cache the result by file path.

    Args:
        path: Filesystem path to the PDF resume.
        llm: LLM provider instance used for extraction.

    Returns:
        Parsed resume data dict (cached after first call).
    """
    if path not in _resume_cache:
        parser = ResumeParser(llm)
        _resume_cache[path] = parser.parse(path)
        logger.info("Parsed and cached resume from %s", path)
    else:
        logger.debug("Returning cached resume data for %s", path)
    return _resume_cache[path]
