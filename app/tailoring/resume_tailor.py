from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.llm.base import LLMProvider

logger = logging.getLogger(__name__)


def _strip_em_dashes(obj):
    """Recursively replace em dashes in all string values."""
    if isinstance(obj, str):
        return obj.replace("—", ",").replace("–", "-")
    if isinstance(obj, list):
        return [_strip_em_dashes(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _strip_em_dashes(v) for k, v in obj.items()}
    return obj


class ResumeTailor:
    """Tailors resume content to a specific job description using an LLM."""

    def __init__(self, llm: "LLMProvider") -> None:
        self.llm = llm

    def tailor(self, job: dict, resume_data: dict) -> dict:
        """Generate tailored resume materials for a specific job.

        Args:
            job: Normalised job dict containing title, company, description, etc.
            resume_data: Parsed resume dict from ResumeParser.

        Returns:
            Dict with keys:
                - summary (str): 3-4 sentence tailored professional summary
                - bullets (list[str]): 5-7 achievement bullets
                - keywords (list[str]): ATS keywords from JD present in resume
        """
        job_title = job.get("title", "")
        job_company = job.get("company", "")
        job_description = job.get("description", "")[:4000]

        skills = resume_data.get("skills", [])
        current_role = resume_data.get("current_role", "")
        years_exp = resume_data.get("years_of_experience", 0)
        summary = resume_data.get("summary", "")
        role_history = resume_data.get("role_history", [])
        education = resume_data.get("education", [])
        skill_categories = resume_data.get("skill_categories", [])

        role_history_lines = []
        for r in role_history:
            role_history_lines.append(
                f"  - {r.get('title', '')} at {r.get('company', '')} ({r.get('duration', '')})"
            )
            for b in r.get("bullets", []):
                role_history_lines.append(f"      • {b}")
        role_history_text = "\n".join(role_history_lines)
        skill_categories_text = "\n".join(
            f"  {entry['category']}: {', '.join(entry['skills'])}"
            for entry in skill_categories
        ) if skill_categories else f"  (flat list) {', '.join(skills)}"

        prompt = f"""You are an expert ATS optimization specialist and professional resume writer.

TASK: Tailor the candidate's resume to perfectly match the job description below.

STRICT CONSTRAINT: Only use facts present in the candidate's resume. Do NOT fabricate experience, skills, companies, titles, achievements, or metrics that are not in the resume data below.

═══════════════════════════════════
JOB DETAILS
═══════════════════════════════════
Title   : {job_title}
Company : {job_company}

Description:
{job_description}

═══════════════════════════════════
CANDIDATE RESUME
═══════════════════════════════════
Current Role       : {current_role}
Years of Experience: {years_exp}
Summary            : {summary}
Role History:
{role_history_text}
Education          : {", ".join(education)}

Tech Skills (categorised — preserve these exact category names):
{skill_categories_text}

═══════════════════════════════════
STEP-BY-STEP INSTRUCTIONS
═══════════════════════════════════

STEP 1 — Extract ALL keywords from the job description:
  • Job title and seniority level
  • Required skills and technologies
  • Preferred / nice-to-have skills
  • Core responsibilities (note the JD's exact phrasing)
  • Tools, platforms, and frameworks
  • Soft skills and leadership signals
  • Domain and industry terms

STEP 2 — Map each keyword to the candidate's resume using exactly one rule:
  a) PRESENT & STRONG   → rewrite the relevant content to emphasize it more prominently
  b) PRESENT BUT WEAK   → strengthen it, move it higher, add measurable impact if data exists
  c) RELATED EXPERIENCE → candidate has adjacent experience; add one truthful bridging sentence
  d) NOT PRESENT        → omit entirely; never invent it

STEP 3 — Write the tailored summary (3–4 sentences):
  • Naturally incorporate the target role title within the first sentence — vary the opening structure, do not start every summary with "As a <title>"
  • Weave in the top 6–8 keywords identified in Step 1
  • State years of experience and domain fit explicitly
  • Close with a concrete value proposition — what specific technical capability can you bring to this exact role at this company?

STEP 4 — Write 7–10 achievement bullets ordered by role, most recent first:
  • Current role gets 4–5 bullets; each previous role gets 1–3 bullets
  • Output as a FLAT array in chronological-descending order — bullets are injected sequentially into the resume per role, so ordering is critical
  • Rewrite the candidate's existing bullets (provided above) to emphasise JD requirements — preserve real metrics, company names, scale figures, and proper nouns from the originals
  • Use strong action verbs that mirror JD language (without copying verbatim)
  • No invented details; every bullet must trace back to the candidate's actual bullet points above

STEP 5 — Compile the ATS keyword list:
  • Include only JD keywords that are genuinely reflected in the output above
  • Exclude any keyword absent from the candidate's background

STEP 6 — Tailor the tech skills section:
  • Use EXACTLY the same category names as in the candidate's resume (do not rename or merge categories)
  • Keep ALL categories — do not drop any
  • Within each category, reorder skills so the most JD-relevant ones appear first
  • You may use a JD's exact terminology for a skill the candidate already has (e.g. "Apache Kafka" instead of just "Kafka") but only if it maps truthfully to their background
  • Do NOT add skills the candidate does not have
  • Output the full list for every category even if its order is unchanged

═══════════════════════════════════
ATS FORMATTING RULES
═══════════════════════════════════
• No icons, tables, images, or markdown formatting
• No verbatim copying from the JD
• Standard clean phrasing only
• Summary may use first person; bullets should omit the subject pronoun
• NEVER use the em dash character (—) anywhere; use a comma, colon, or rewrite the phrase instead

═══════════════════════════════════
OUTPUT
═══════════════════════════════════
Return a single JSON object with EXACTLY these keys:

{{
  "summary": "<3–4 sentence tailored professional summary>",
  "bullets": ["<bullet 1>", "<bullet 2>", ..., "<bullet 7>"],
  "keywords": ["<keyword 1>", "<keyword 2>", ...],
  "tech_skills": [
    {{"category": "<exact category name>", "skills": ["<skill 1>", "<skill 2>", ...]}},
    ...
  ]
}}

Return ONLY the JSON object. No markdown fences, no commentary, no extra text.
"""

        raw_response = self.llm.generate(prompt)

        # Strip markdown code fences
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_response.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())

        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error(
                "Failed to parse tailor JSON for job '%s' at '%s': %s",
                job_title,
                job_company,
                exc,
            )
            logger.debug("Raw LLM response: %s", raw_response)
            result = {
                "summary": summary,
                "bullets": [],
                "keywords": [],
            }

        return _strip_em_dashes(result)
