from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import TYPE_CHECKING

from app.scraping.social_dorker import SocialDorker
from app.scraping.universal_crawler import UniversalCrawler

if TYPE_CHECKING:
    from app.llm.base import LLMProvider

logger = logging.getLogger(__name__)


class CompanyTargetedAgent:
    """Agent that identifies, crawls, and filters relevant positions within a target company."""

    def __init__(self, llm: "LLMProvider") -> None:
        self.llm = llm

    def find_careers_url(self, company_name: str) -> str | None:
        """Query DuckDuckGo for the company's active career page or Greenhouse/Lever board."""
        logger.info("CompanyAgent: finding careers page for %r", company_name)
        dorker = SocialDorker()
        query = f'"{company_name}" (careers OR jobs OR "working at" OR "greenhouse.io" OR "lever.co")'
        raw_results = dorker._search_ddg(query)

        if not raw_results:
            return None

        # Prioritize Greenhouse, Lever, Ashby, Workday boards
        for r in raw_results:
            url = r["url"].lower()
            if any(term in url for term in ["greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs"]):
                logger.info("CompanyAgent: found high-probability ATS board for company: %s", r["url"])
                return r["url"]

        # General careers pages fallback
        for r in raw_results:
            url = r["url"].lower()
            if "career" in url or "job" in url:
                logger.info("CompanyAgent: found careers page: %s", r["url"])
                return r["url"]

        # First indexed URL fallback if nothing matches
        logger.info("CompanyAgent: falling back to first search link: %s", raw_results[0]["url"])
        return raw_results[0]["url"]

    async def crawl_jobs(self, url: str) -> list[dict]:
        """Crawl the careers page and return raw job titles and links."""
        logger.info("CompanyAgent: crawling %s", url)
        crawler = UniversalCrawler()
        jobs = await crawler.crawl(url, limit=40, headless=True)
        return jobs

    def filter_eligible_jobs(self, jobs: list[dict], resume_data: dict) -> list[dict]:
        """Runs a fast AI-pass over titles to filter roles aligning with candidate's profile."""
        if not jobs:
            return []

        skills = resume_data.get("skills", [])
        target_roles = resume_data.get("target_roles", [])
        current_role = resume_data.get("current_role", "")

        job_titles = [j.get("title", "") for j in jobs]
        logger.info("CompanyAgent: filtering %d raw crawled positions...", len(job_titles))

        prompt = f"""You are an advanced recruitment matching engine. Filter the following list of job titles based on the candidate's profile.

CANDIDATE PROFILE:
- Current role: {current_role}
- Target roles: {", ".join(target_roles)}
- Tech Skills: {", ".join(skills)}

JOB LIST:
{json.dumps(job_titles, indent=2)}

Task: Identify which job titles are a strong match for this candidate.
- Select only relevant engineering or technical developer roles that match their level (e.g. Senior Software Engineer, Backend Engineer, Staff Developer).
- Do NOT select irrelevant departments (Sales, Marketing, HR, Finance, Design, QA/Testing unless specifically requested, or Product Management).
- Do NOT select junior roles if candidate is senior.
- Do NOT select roles that require tech stacks completely absent from the candidate's profile (e.g. do not select PHP roles if candidate is purely Go/Java).

Return a single JSON array of integers representing the zero-based indices of matching roles (e.g., [0, 3, 7]).
Return ONLY the JSON array. No markdown code fences, no extra text.
"""

        try:
            raw_response = self.llm.generate(prompt)
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw_response.strip(), flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned.strip())

            matching_indices = json.loads(cleaned)
            if not isinstance(matching_indices, list):
                logger.warning("CompanyAgent: LLM returned non-list indices.")
                return []

            matched_jobs = []
            for idx in matching_indices:
                try:
                    idx_int = int(idx)
                    if 0 <= idx_int < len(jobs):
                        matched_jobs.append(jobs[idx_int])
                except (ValueError, TypeError):
                    continue

            logger.info("CompanyAgent: filtered down to %d matching jobs", len(matched_jobs))
            return matched_jobs
        except Exception as exc:
            logger.error("CompanyAgent: failed filtering suitable jobs: %s", exc)
            return []
