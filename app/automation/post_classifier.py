from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.llm.base import LLMProvider

logger = logging.getLogger(__name__)


class PostClassifier:
    """Classifies unstructured social media posts (X, LinkedIn) and extracts structured job roles."""

    def __init__(self, llm: "LLMProvider") -> None:
        self.llm = llm

    def classify(self, post_text: str, platform: str = "unknown") -> Optional[dict]:
        """Classify a social media post and extract key hiring attributes.

        Args:
            post_text: The raw text of the tweet/post.
            platform: "linkedin", "twitter", or "unknown".

        Returns:
            Dict containing:
                is_hiring (bool), job_title (str), company (str), location (str),
                extracted_email (str/None), contact_method (str), implied_skills (list[str]),
                description (str)
            Or None if not a genuine hiring post.
        """
        if not post_text or len(post_text.strip()) < 15:
            return None

        prompt = f"""You are an advanced recruitment parser. Analyze this raw social media post (from {platform}) and extract structured details about the job opportunity.

CRITICAL DISCRIMINATION RULE:
Determine if this post is genuinely someone HIRING/OFFERING a job.
- Set "is_hiring": false if the post is:
  * A candidate looking for a job themselves (e.g., "I'm looking for a python role").
  * General advice, news, industry discussion, or unrelated content.
  * A post that doesn't mention an actual job opening.

SOCIAL POST:
\"\"\"
{post_text}
\"\"\"

If and only if "is_hiring" is true, extract the details. Use empty string/null for missing fields, but try to infer the company if mentioned or if the poster is a founder.

Return a single JSON object with EXACTLY these keys:
- "is_hiring"       : boolean (true if genuinely hiring/offering a role, false otherwise)
- "job_title"       : string (the inferred role title, e.g., "Senior Python Engineer")
- "company"         : string (the inferred company name, or "Unknown Startup" if not explicitly named)
- "location"        : string (location or "Remote" if implied/stated)
- "extracted_email" : string or null (any email address mentioned in the post, e.g. "jobs@startup.co")
- "contact_method"  : string ("EMAIL", "DM", or "URL" based on the primary call to action)
- "implied_skills"  : list of strings (core technical skills/languages mentioned or strongly implied, e.g. ["Python", "FastAPI"])
- "description"     : string (clean, normalized summary of the hiring post or the post content itself)

Return ONLY the JSON object. No markdown fences (no ```json ... ```), no extra text.
"""

        try:
            raw_response = self.llm.generate(prompt)
            # Clean markdown code fences if any
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw_response.strip(), flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned.strip())

            result = json.loads(cleaned)
        except Exception as exc:
            logger.error("Failed to parse LLM response in PostClassifier: %s", exc)
            return None

        if not isinstance(result, dict):
            return None

        if not result.get("is_hiring", False):
            logger.info("Social post classified as NOT hiring (filtered out).")
            return None

        # Clean/sanitize output fields
        result["job_title"] = result.get("job_title", "Software Engineer") or "Software Engineer"
        result["company"] = result.get("company", "Unknown Startup") or "Unknown Startup"
        result["location"] = result.get("location", "Remote") or "Remote"
        result["implied_skills"] = result.get("implied_skills", []) or []
        result["contact_method"] = (result.get("contact_method") or "DM").upper()
        result["extracted_email"] = result.get("extracted_email")

        # Fallback description to original text if missing
        if not result.get("description"):
            result["description"] = post_text.strip()

        logger.info(
            "✓ Social post classified: '%s' @ '%s' (Contact: %s)",
            result["job_title"],
            result["company"],
            result["extracted_email"] or result["contact_method"],
        )
        return result
