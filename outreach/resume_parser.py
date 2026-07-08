"""
Stage 0: Resume Parser
Foundation of the entire pipeline. Parses base_resume.pdf (or .docx) using
pdfplumber + Gemini and caches the result to resume_cache.json.

Every other module calls load_resume() — nothing is ever hardcoded.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

import pdfplumber
import vertexai
from vertexai.generative_models import GenerativeModel

logger = logging.getLogger(__name__)

_OUTREACH_DIR = Path(__file__).parent
_DEFAULT_RESUME_PATH = _OUTREACH_DIR / "base_resume.pdf"
_DEFAULT_CACHE_PATH = _OUTREACH_DIR / "resume_cache.json"
_GCP_LOCATION = "us-central1"


def _get_project_and_model() -> tuple[str, str]:
    """Read GCP project and scoring model from README config, fall back to env."""
    try:
        # Lazy import to avoid circular dependency at module level
        readme_path = _OUTREACH_DIR / "README.md"
        import re as _re
        text = readme_path.read_text(encoding="utf-8")
        match = _re.search(r"<!-- CONFIG_START -->(.*?)<!-- CONFIG_END -->", text, _re.DOTALL)
        cfg: dict[str, str] = {}
        if match:
            for line in match.group(1).splitlines():
                line = line.strip()
                if ":" in line and not line.startswith("#") and not line.startswith("##"):
                    k, _, v = line.partition(":")
                    cfg[k.strip()] = v.strip()
        project = cfg.get("GCP_PROJECT_ID", os.environ.get("GCP_PROJECT_ID", ""))
        # Resume parsing is a one-time quality task → use tailoring model
        model = cfg.get("TAILORING_MODEL", "gemini-2.5-pro")
        return project, model
    except Exception:
        return os.environ.get("GCP_PROJECT_ID", ""), "gemini-2.5-pro"

_EXTRACTION_PROMPT = """\
You are an expert technical recruiter and career coach parsing a resume for a fully automated job outreach pipeline.

Analyse the resume deeply. Every field you produce will be used directly by an AI agent to search for jobs, filter candidates, and write cold emails. Be specific, accurate, and thorough.

Return ONLY valid JSON, no markdown fences:

{
  "full_name": "candidate full name",
  "email": "candidate email if present",
  "linkedin": "linkedin URL if present",
  "location": "city, country",
  "current_role": "most recent job title",
  "years_experience": "total years of experience as string e.g. 4 years",

  "primary_skills": [
    "list of 5 PRIMARY languages/frameworks that define this person — the ones they are strongest in and should always appear in emails and searches. Extract from actual job experience, not just listed skills."
  ],
  "core_skills": [
    "list of top 15 technical skills this person has real experience with — languages, frameworks, tools, platforms. Ordered by strength/frequency in resume."
  ],
  "ai_skills": [
    "list all AI/ML related skills: RAG, vector embeddings, LLMs, fine-tuning, semantic search, specific models (GPT, Claude, Gemini), ML frameworks, etc. Empty list if none."
  ],
  "tech_stack": "comma separated FULL tech stack from resume — every technology mentioned",

  "key_achievements": [
    "achievement 1: exact metric, exact system, exact impact — do not paraphrase, extract verbatim with numbers",
    "achievement 2: exact metric, exact system, exact impact",
    "achievement 3: exact metric, exact system, exact impact",
    "achievement 4 if present",
    "achievement 5 if present"
  ],

  "target_roles": [
    "list of 8-12 specific job titles this person should apply to based on their actual experience — be comprehensive, cover backend, AI backend, platform, distributed systems, fintech, Java, Go etc."
  ],
  "target_companies": "comma separated list of company types: e.g. fintech startup, AI product company, enterprise SaaS, cloud infrastructure — based on resume experience",
  "seniority": "junior / mid / senior / staff / principal based on experience and impact",

  "search_terms": [
    "Generate 20 specific job search query strings for LinkedIn, Indeed, ZipRecruiter.",
    "Rules: 3-6 words each, end with Remote, cover ALL major skills from resume.",
    "Must include: primary language searches (Java, Go), AI/LLM searches if applicable, role-based searches, stack-based searches.",
    "Examples: Senior Java Backend Engineer Remote, LLM Engineer Remote, Go Distributed Systems Remote, Java Spring Boot Microservices Remote, AI Backend RAG Remote"
  ],
  "twitter_search_terms": [
    "Generate 12 short Twitter/X search queries to find hiring posts.",
    "Mix: role-based, stack-based, AI-based.",
    "Examples: hiring java backend engineer remote, looking for spring boot developer, LLM engineer hiring, hiring go engineer distributed systems, java microservices developer remote"
  ],

  "outreach_headline": "one tight sentence: who this person is, what they built, and what makes them stand out. Must include primary language and one specific achievement. For use in cold emails.",
  "top_achievement": "the single most impressive quantified achievement from the resume — written as a natural confident sentence a human would say. Include exact numbers. This appears in every cold email."
}

Resume text:
"""


def _extract_text(resume_path: str) -> str:
    path = Path(resume_path)
    if not path.exists():
        raise FileNotFoundError(f"Resume not found at {resume_path}")

    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _extract_text_docx(str(path))
    return _extract_text_pdf(str(path))


def _extract_text_pdf(path: str) -> str:
    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _extract_text_docx(path: str) -> str:
    import docx
    doc = docx.Document(path)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _strip_fences(text: str) -> str:
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    return cleaned


def parse_resume(resume_path: str) -> dict:
    """Parse resume PDF/DOCX using Gemini and return structured dict."""
    logger.info("Extracting text from %s", resume_path)
    raw_text = _extract_text(resume_path)
    if not raw_text.strip():
        raise ValueError(f"No text extracted from {resume_path}")

    gcp_project, model_name = _get_project_and_model()
    if not gcp_project:
        raise RuntimeError("GCP_PROJECT_ID not set in outreach/README.md config or environment")
    logger.info("Initialising Vertex AI (project=%s, model=%s)", gcp_project, model_name)
    from google.oauth2 import service_account as _sa
    _credentials = _sa.Credentials.from_service_account_file(
        os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS",
            str(Path(__file__).parent.parent / "vertex-ai-credentials.json"),
        ),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    vertexai.init(project=gcp_project, location=_GCP_LOCATION, credentials=_credentials)
    model = GenerativeModel(model_name)

    prompt = _EXTRACTION_PROMPT + raw_text
    logger.info("Calling Gemini to parse resume…")
    response = model.generate_content(prompt)
    raw_json = _strip_fences(response.text)

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        logger.error("JSON parse failed: %s\nRaw response: %s", exc, raw_json[:500])
        raise

    logger.info("Resume parsed successfully for: %s", data.get("full_name", "unknown"))
    return data


def _save_cache(data: dict, cache_path: str) -> None:
    Path(cache_path).write_text(json.dumps(data, indent=2, ensure_ascii=False))
    logger.info("Cached resume to %s", cache_path)


def load_resume(
    resume_path: str | None = None,
    cache_path: str | None = None,
    force_refresh: bool = False,
) -> dict:
    """Return parsed resume dict, using cache when available.

    This is the single entry point every other module uses.
    If cache_path does not exist, parse resume_path and write the cache.
    Pass force_refresh=True to re-parse even if cache exists.
    """
    if resume_path is None:
        resume_path = str(_DEFAULT_RESUME_PATH)
    if cache_path is None:
        cache_path = str(_DEFAULT_CACHE_PATH)

    cache = Path(cache_path)

    if not force_refresh and cache.exists():
        logger.debug("Loading resume from cache %s", cache_path)
        return json.loads(cache.read_text())

    data = parse_resume(resume_path)
    _save_cache(data, cache_path)
    return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Parse resume and cache result")
    parser.add_argument(
        "--resume",
        default=str(_DEFAULT_RESUME_PATH),
        help="Path to resume PDF or DOCX (default: outreach/base_resume.pdf)",
    )
    parser.add_argument(
        "--cache",
        default=str(_DEFAULT_CACHE_PATH),
        help="Path to write resume_cache.json",
    )
    parser.add_argument(
        "--refresh-resume",
        action="store_true",
        help="Force re-parse even if cache exists",
    )
    args = parser.parse_args()

    resume_path = args.resume

    # If default PDF path doesn't exist, fall back to the docx in resumes/
    if not Path(resume_path).exists():
        docx_fallback = str(Path(__file__).parent.parent / "resumes" / "base_resume.docx")
        if Path(docx_fallback).exists():
            logger.warning(
                "PDF not found at %s, falling back to %s", resume_path, docx_fallback
            )
            resume_path = docx_fallback
        else:
            print(f"ERROR: Resume not found at {resume_path}")
            print("Drop your resume at outreach/base_resume.pdf and re-run.")
            sys.exit(1)

    data = load_resume(
        resume_path=resume_path,
        cache_path=args.cache,
        force_refresh=args.refresh_resume,
    )

    print("\n=== PARSED RESUME ===")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"\nCache written to: {args.cache}")
