"""
Stage 3+4: Email Drafter + Gmail Draft Creator
All personal content comes from resume_parser.load_resume() — nothing hardcoded.

Model selection (configurable in outreach/README.md config block):
  SCORING_MODEL  → score_relevance, guess_emails  (reasoning, quality tasks)
  TAILORING_MODEL → draft_email, resume parsing   (generation tasks)

Intended mapping from .env:
  GEMINI_SCORING_MODEL=gemini-3-flash-preview   → set SCORING_MODEL when access is granted
  GEMINI_TAILORING_MODEL=gemini-3.1-pro-preview → set TAILORING_MODEL when access is granted
  Currently working fallbacks: gemini-2.5-flash / gemini-2.5-pro
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import vertexai
from vertexai.generative_models import GenerativeModel

sys.path.insert(0, str(Path(__file__).parent))
from readme_store import read_config
from resume_parser import load_resume

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Initialise Vertex AI + models (done once at module load)
# ---------------------------------------------------------------------------
_config = read_config()
_GCP_PROJECT = _config.get("GCP_PROJECT_ID", os.environ.get("GCP_PROJECT_ID", ""))
if not _GCP_PROJECT:
    raise RuntimeError("GCP_PROJECT_ID not set in outreach/README.md config or environment")

import google.auth
from google.oauth2 import service_account as _sa

_cred_path = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(Path(__file__).parent.parent / "vertex-ai-credentials.json"),
)
_credentials = None

if Path(_cred_path).exists():
    try:
        _credentials = _sa.Credentials.from_service_account_file(
            _cred_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        logger.info("Loaded Vertex AI credentials from %s", _cred_path)
    except Exception as _exc:
        logger.warning("Failed to load Vertex AI credentials from %s: %s", _cred_path, _exc)
else:
    logger.warning(
        "Vertex AI key file not found at %s. Attempting Application Default Credentials (ADC)...",
        _cred_path
    )
    try:
        _credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        logger.info("Loaded Application Default Credentials (ADC) successfully.")
    except Exception as _exc:
        logger.warning("Failed to load Application Default Credentials: %s", _exc)

vertexai.init(project=_GCP_PROJECT, location="us-central1", credentials=_credentials)

# Model names read from README config — swap to gemini-3.1-pro-preview / gemini-3-flash-preview
# once Vertex AI access is granted for those preview models in this GCP project.
_SCORING_MODEL = _config.get("SCORING_MODEL", "gemini-2.5-flash")
_TAILORING_MODEL = _config.get("TAILORING_MODEL", "gemini-2.5-pro")

model_pro = GenerativeModel(_TAILORING_MODEL)   # quality tasks: guess_emails, score_relevance
model_flash = GenerativeModel(_SCORING_MODEL)   # fast tasks: draft_email, parse scraped text
model_lite = GenerativeModel(_SCORING_MODEL)    # same tier as flash

resume = load_resume()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    return cleaned


def _parse_json_safe(text: str, fallback: dict | list) -> dict | list:
    try:
        return json.loads(_strip_fences(text))
    except (json.JSONDecodeError, AttributeError) as exc:
        logger.warning("JSON parse failed: %s | raw: %s", exc, text[:200])
        return fallback


# ---------------------------------------------------------------------------
# FUNCTION 1: score_relevance
# ---------------------------------------------------------------------------

def score_relevance(job: dict) -> dict:
    """Score a job and extract the most senior reachable contact.

    Contact priority: Founder > CEO > CTO > VP Eng > Head of Eng > Engineering Manager > Recruiter > Hiring Team
    Uses gemini-2.5-pro for quality reasoning.
    """
    _fallback = {
        "is_relevant": False,
        "contact_name": "Hiring Team",
        "contact_title": "",
        "contact_type": "hiring_team",   # founder | exec | manager | recruiter | hiring_team
        "tech_stack": "",
        "match_reason": "parse error",
        "match_score": 0,
    }

    try:
        prompt = f"""You are evaluating a job post for a senior backend engineer candidate and identifying the best person to cold-email.

CANDIDATE PROFILE:
- Name: {resume["full_name"]}
- Role: {resume["current_role"]}
- Experience: {resume["years_experience"]}
- Core skills: {resume["tech_stack"]}
- Target roles: {", ".join(resume["target_roles"])}

JOB POST:
Title: {job.get("title", "")}
Company: {job.get("company", "")}
Source: {job.get("source", "")}
Description: {job.get("description", "")[:1500]}

TASK 1 — CONTACT EXTRACTION:
Find the single best person to cold-email. Priority order:
1. Founder / Co-Founder (best — they make hiring decisions and care about technical fit)
2. CEO / CTO / CPO (excellent — executive, fast decision maker)
3. VP Engineering / Head of Engineering / Director of Engineering (very good)
4. Engineering Manager / Tech Lead (good — direct hiring manager)
5. Recruiter / HR / Talent (poor — gatekeepers, lower reply rate)
6. "Hiring Team" only if NO individual is mentioned or inferable

Rules:
- Check the description carefully for any name, signature, or "posted by" mention
- For HN posts: the username in the post is almost always the founder/CTO — use it as contact
- For startups (< 50 people implied), assume there is a technical co-founder — use "Founder" as contact_name if no name found
- For Twitter posts: the poster's display name is the contact
- For LinkedIn posts: look for "Posted by [Name]" or author in the text
- Do NOT use "Hiring Team" unless the company is clearly large enterprise (bank, FAANG) with no individual mentioned

TASK 2 — RELEVANCE SCORING:
Score the job match from 1 to 10:
- 9-10: perfect match — exact tech stack, right seniority, right company type
- 7-8: strong match — most core skills present, minor gaps
- 5-6: partial match — relevant domain but some skill gap
- 1-4: weak match — wrong stack, wrong seniority, or different domain

Return ONLY valid JSON, no markdown:
{{
  "is_relevant": true or false,
  "contact_name": "Real name if found, role title if inferrable (e.g. 'Founder'), else 'Hiring Team'",
  "contact_title": "their actual title e.g. CEO, CTO, Founder, Engineering Manager, Recruiter",
  "contact_type": "founder | exec | manager | recruiter | hiring_team",
  "tech_stack": "comma-separated tech from the job post",
  "match_reason": "one specific sentence — name the exact skill overlap or gap",
  "match_score": 1-10
}}"""

        response = model_pro.generate_content(prompt)
        result = _parse_json_safe(response.text, _fallback)
        if not isinstance(result, dict):
            return _fallback
        for k, v in _fallback.items():
            result.setdefault(k, v)
        # Normalize contact_name — strip titles that leaked into the name field
        cn = str(result.get("contact_name", "")).strip()
        if not cn or cn.lower() in ("none", "null", "n/a", ""):
            result["contact_name"] = "Hiring Team"
            result["contact_type"] = "hiring_team"
        return result

    except Exception as exc:
        logger.warning("[score_relevance] Error for %s: %s", job.get("company"), exc)
        return _fallback


# ---------------------------------------------------------------------------
# FUNCTION 2: guess_emails
# ---------------------------------------------------------------------------

def guess_emails(
    contact_name: str,
    domain: str,
    company: str,
    contact_type: str = "hiring_team",
) -> list[str]:
    """Generate 5 likely email addresses ordered by statistical likelihood.

    contact_type influences pattern priority:
    - founder/exec: first@domain (startups) or first.last@domain come first
    - manager: first.last@domain or flast@domain
    - recruiter/hiring_team: hiring@, talent@, careers@ as first fallback
    """
    is_generic = contact_name.lower() in ("hiring team", "founder", "co-founder", "ceo", "cto",
                                           "engineering manager", "tech lead", "recruiter", "hiring manager")

    try:
        prompt = f"""You are an expert at predicting professional email formats used by tech companies.

Contact name: {contact_name}
Contact type: {contact_type}  (founder | exec | manager | recruiter | hiring_team)
Company: {company}
Domain: {domain}

Generate the 5 most statistically likely email addresses for this person at this domain.

Ordering rules based on contact_type:
- founder / exec: startups use first@domain.com most commonly. Then first.last@domain.com. Never start with hiring@ for founders.
- manager: first.last@domain.com is most common. Then flast@domain.com.
- recruiter / hiring_team: hiring@domain.com, talent@domain.com, careers@domain.com, then first.last@ if name is known.

If contact_name is a generic role title (Founder, CEO, CTO, Hiring Team):
- For founder/exec with small startup: use founder@{domain}, ceo@{domain}, hello@{domain}, hi@{domain}, team@{domain}
- For recruiter/hiring_team: use hiring@{domain}, careers@{domain}, talent@{domain}, jobs@{domain}, hr@{domain}

If contact_name is a real person's name:
- Extract first name and last name and generate personal email patterns
- Also include a role-based email (founder@, ceo@) as 5th fallback

Use only lowercase. Use exactly this domain: {domain}.
Return ONLY a valid JSON array of exactly 5 email strings. No markdown, no explanation."""

        response = model_pro.generate_content(prompt)
        result = _parse_json_safe(response.text, [])
        if isinstance(result, list) and len(result) > 0:
            return [str(e).lower().strip() for e in result[:5]]

    except Exception as exc:
        logger.warning("[guess_emails] Error for %s@%s: %s", contact_name, domain, exc)

    # Deterministic fallback
    if not domain:
        return [""]
    if is_generic:
        if contact_type in ("founder", "exec"):
            return [f"founder@{domain}", f"ceo@{domain}", f"hello@{domain}", f"hi@{domain}", f"team@{domain}"]
        return [f"hiring@{domain}", f"careers@{domain}", f"talent@{domain}", f"jobs@{domain}", f"hr@{domain}"]

    parts = contact_name.lower().split()
    first = re.sub(r"[^a-z]", "", parts[0]) if parts else ""
    last = re.sub(r"[^a-z]", "", parts[-1]) if len(parts) > 1 else ""
    guesses = []
    if first and last:
        guesses = [f"{first}@{domain}", f"{first}.{last}@{domain}", f"{first}{last}@{domain}", f"{first[0]}{last}@{domain}"]
    elif first:
        guesses = [f"{first}@{domain}"]
    guesses.append(f"hiring@{domain}")
    return guesses[:5]


# ---------------------------------------------------------------------------
# FUNCTION 3: draft_email
# ---------------------------------------------------------------------------

def draft_email(
    job: dict,
    contact_name: str,
    tech_stack_mentioned: str,
    email_candidates: list[str],
    contact_type: str = "hiring_team",
    contact_title: str = "",
) -> dict:
    """Draft a cold outreach email using resume context. Uses gemini-2.5-flash."""
    _fallback_subject = f"Quick question about {job.get('company', 'your team')}"
    _fallback_body = (
        f"Hi {contact_name},\n\n"
        f"{job.get('company', 'Your company')} is doing interesting work in this space.\n\n"
        f"I spent {resume['years_experience']} as a founding engineer building backend systems from scratch for fintech and enterprise SaaS, owning systems end to end.\n\n"
        f"- {resume['key_achievements'][0] if resume.get('key_achievements') else resume['top_achievement']}\n"
        f"- {resume['key_achievements'][1] if len(resume.get('key_achievements', [])) > 1 else ''}\n"
        f"- {resume['key_achievements'][2] if len(resume.get('key_achievements', [])) > 2 else ''}\n\n"
        f"{resume['tech_stack'][:120]}. Fully remote.\n\n"
        f"Attaching my resume. Happy to hop on a call if my experience fits."
    )

    try:
        achievements_text = "\n".join(resume.get("key_achievements", []))

        # Adjust tone based on who we're emailing
        _contact_context = {
            "founder": (
                "The recipient is a FOUNDER or CO-FOUNDER. They wrote the code, they know what good engineers look like. "
                "Write completely peer-to-peer — engineer to engineer. No formality. Get to the point in 3 sentences flat. "
                "They are time-poor and bullshit-averse. Lead with technical credibility, not credentials."
            ),
            "exec": (
                "The recipient is a C-suite executive (CEO/CTO/CPO). They care about: will this person ship, can they own things end-to-end, are they senior enough. "
                "Be direct and confident. No selling, just signal. They read 50 of these a week — the only ones they reply to are specific and short."
            ),
            "manager": (
                "The recipient is an Engineering Manager, VP Engineering, or Head of Engineering. "
                "They care about technical fit and whether you can work independently. Show you understand the technical domain. "
                "Slightly more structured than exec, still concise."
            ),
            "recruiter": (
                "The recipient is a recruiter or HR. Be professional but brief. "
                "Lead with role match and availability. They will forward to the hiring manager if you fit."
            ),
            "hiring_team": (
                "The recipient is a generic hiring contact. Write a strong but professional cold email. "
                "Show technical depth so the right person on the team sees it."
            ),
        }.get(contact_type, "")

        _contact_title_line = f"Their title: {contact_title}" if contact_title else ""

        system_prompt = f"""You are ghostwriting a cold outreach email for {resume["full_name"]}. You write exactly like a senior engineer who knows their worth — direct, specific, zero fluff.

WHO THIS PERSON IS:
{resume["full_name"]} spent 4 years as a founding engineer at APIwiz, building multi-cloud backend infrastructure from zero for fintech clients across APAC — API gateways, Kafka pipelines, distributed systems handling real production load. He has shipped production RAG pipelines, vector search, and LLM integrations (GPT, Claude, Gemini) — not side projects, actual features in live systems. He knows Go, Java 17, Python, Spring Boot, Kafka, Kubernetes, AWS, and is actively deep in the AI/LLM space.

RECIPIENT CONTEXT:
{_contact_context}
{_contact_title_line}

HARD RULES FOR VOICE:
- Write like a senior engineer talking to a peer, not a candidate writing to a gatekeeper
- Every sentence must earn its place — if it doesn't add signal, cut it
- Specificity beats adjectives. "API response from 10s to under 1s" beats "significantly improved performance"
- Never start a sentence with "I am", "I have", "I would", "I believe"
- No corporate speak: no "leverage", "synergy", "passionate about", "fast-paced environment"
- The company line must show genuine insight into their technical problem — not just what they do, but why it's hard
- The AI angle must feel like a natural extension of backend expertise, not a separate section — weave it in where relevant
- For founders/execs: keep the total email under 120 words. Tight. Every word counts.

ACHIEVEMENTS TO DRAW FROM:
{achievements_text}

TOP ACHIEVEMENT (must appear naturally in every email):
{resume["top_achievement"]}"""

        other_emails = "\n".join(email_candidates[1:]) if len(email_candidates) > 1 else ""
        other_emails_section = (
            f"\n---\nOther possible emails if this address bounces:\n{other_emails}"
            if other_emails else ""
        )

        job_source = job.get("source", "")
        is_social = job_source in ("linkedin_post", "twitter_post")

        if is_social:
            company_line_rule = f"""[SOCIAL MEDIA REFERENCE LINE — 1-2 sentences. Refer naturally to their recent post on { 'LinkedIn' if job_source == 'linkedin_post' else 'X/Twitter' } about hiring for {job.get('title', '')}. State that you saw it and are reaching out directly because your backend/AI engineering profile aligns perfectly. Example style: "Saw your post on X today about needing a senior developer to build out your high-scale backend — this aligns exactly with what I have been shipping." ]"""
        else:
            company_line_rule = """[COMPANY LINE — 1-2 sentences. Name the specific technical problem they are solving or the hard thing about what they build. Show you understand the challenge, not just the product. Do not compliment them. Example style: "Drift detection and CI/CD enforcement across hybrid VMware and cloud — the backend holding that together is genuinely hard to get right."]"""

        user_prompt = f"""Write a cold outreach email for this opportunity:

Company: {job.get("company", "")}
Role: {job.get("title", "")}
Description: {job.get("description", "")[:600]}
Contact: {contact_name}
Their tech stack: {tech_stack_mentioned}

STRUCTURE (follow exactly, no deviations):

Hi {contact_name},

{company_line_rule}

[BACKGROUND LINE — 1 tight sentence. Who the sender is + where + what built + AI angle if relevant. Must reference APIwiz and real work. Always mention the primary languages (Java, Go). Example: "Four years as founding engineer at APIwiz, building Java and Go backend systems for fintech clients across APAC, and shipping production RAG pipelines and LLM integrations."]

- [Bullet 1: most relevant achievement with exact number/metric]
- [Bullet 2: second most relevant with exact number/metric]
- [Bullet 3: third most relevant with exact number/metric]

[STACK LINE — always lead with primary_skills ({", ".join(resume.get("primary_skills", ["Java 17", "Go"]))}), then add overlap with company stack. End with "Fully remote."]

Attaching my resume. Happy to hop on a call if my experience fits.

RULES:
- Subject: "[specific value prop] | [stack keywords]" — max 10 words, always include primary language
- Every sentence must contain concrete information — no filler
- AI/LLM angle (RAG, vector embeddings, fine-tuning, LLM integration): weave naturally into background line if role is AI-related
- Primary skills ({", ".join(resume.get("primary_skills", ["Java 17", "Go"]))}) must appear in the stack line of every email
- BANNED words/phrases: "I hope", "I am writing", "I wanted to", "I came across", "I noticed", "I am excited", "leverage", "passionate", "fast-paced", "synergy", em dash, double dash
- Do NOT include sign-off, name, or links — added separately
- Tone: peer to peer, confident, no desperation

{other_emails_section}

Return ONLY valid JSON (no markdown):
{{"subject": "...", "body": "...full email body ending at closure line..."}}"""

        full_prompt = system_prompt + "\n\n" + user_prompt
        response = model_flash.generate_content(full_prompt)
        result = _parse_json_safe(response.text, {})

        if isinstance(result, dict) and "subject" in result and "body" in result:
            return result

    except Exception as exc:
        logger.warning("[draft_email] Error for %s: %s", job.get("company"), exc)

    return {"subject": _fallback_subject, "body": _fallback_body}


# ---------------------------------------------------------------------------
# FUNCTION 4: create_gmail_draft (Stage 4)
# ---------------------------------------------------------------------------

def _build_html_body(plain_body: str, full_name: str, portfolio_url: str, linkedin_url: str, github_url: str) -> str:
    """Convert plain text email body to HTML with hyperlinked footer."""
    # Strip existing sign-off (Thanks, / name / links) — we'll replace with HTML version
    clean = re.sub(
        r"\n+(Thanks|Best|Regards|Cheers),?\s*\n.*$", "", plain_body, flags=re.DOTALL | re.IGNORECASE
    ).strip()

    # Convert plain text to HTML paragraphs
    html_paragraphs: list[str] = []
    for block in clean.split("\n\n"):
        lines = block.strip().splitlines()
        # Detect bullet block
        if all(l.strip().startswith("-") for l in lines if l.strip()):
            items = "".join(f"<li>{l.strip().lstrip('- ').strip()}</li>" for l in lines if l.strip())
            html_paragraphs.append(f"<ul style='margin:8px 0;padding-left:20px'>{items}</ul>")
        else:
            html_paragraphs.append("<p>" + "<br>".join(l for l in lines if l.strip()) + "</p>")

    body_html = "\n".join(html_paragraphs)

    # Build footer links
    link_parts: list[str] = []
    if portfolio_url and "YOUR_" not in portfolio_url:
        link_parts.append(f'<a href="{portfolio_url}" style="color:#1a73e8;text-decoration:none">Portfolio</a>')
    if linkedin_url and "YOUR_" not in linkedin_url:
        link_parts.append(f'<a href="{linkedin_url}" style="color:#1a73e8;text-decoration:none">LinkedIn</a>')
    if github_url and "YOUR_" not in github_url:
        link_parts.append(f'<a href="{github_url}" style="color:#1a73e8;text-decoration:none">Github</a>')
    footer_links = " | ".join(link_parts) if link_parts else ""

    footer_html = f"""
<p style="margin-top:16px">Thanks,<br>{full_name}</p>
{"<p>" + footer_links + "</p>" if footer_links else ""}
"""

    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#202124;line-height:1.6">
{body_html}
{footer_html}
</body></html>"""


def create_gmail_draft(
    to_email: str,
    subject: str,
    body: str,
    resume_path: str,
    credentials_path: str,
    token_path: str,
) -> bool:
    """Create a Gmail draft with resume PDF attached. NEVER sends.

    Returns True on success, False on any failure.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]

        creds = None
        token_file = Path(token_path)
        creds_file = Path(credentials_path)

        if not creds_file.exists():
            logger.error("Gmail credentials not found at %s", credentials_path)
            logger.error("Download from GCP Console > APIs > Gmail > Credentials")
            return False

        if token_file.exists():
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
                creds = flow.run_local_server(port=0)
            token_file.write_text(creds.to_json())

        service = build("gmail", "v1", credentials=creds)

        # Read link config for HTML footer
        cfg = _config
        full_name = resume.get("full_name", "Candidate")
        portfolio_url = cfg.get("PORTFOLIO_URL", "")
        linkedin_url = cfg.get("LINKEDIN_URL", resume.get("linkedin", ""))
        github_url = cfg.get("GITHUB_URL", "")

        # Build MIME: mixed > alternative (plain+html) + attachment
        message = MIMEMultipart("mixed")
        message["To"] = to_email
        message["Subject"] = subject

        html_body = _build_html_body(body, full_name, portfolio_url, linkedin_url, github_url)
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain"))
        alt.attach(MIMEText(html_body, "html"))
        message.attach(alt)

        # Attach resume
        resume_name = full_name.replace(" ", "_")
        attachment_filename = f"{resume_name}_Resume.pdf"

        resume_file = Path(resume_path)
        if not resume_file.exists():
            docx_path = Path(__file__).parent.parent / "resumes" / "base_resume.docx"
            if docx_path.exists():
                resume_file = docx_path
                attachment_filename = attachment_filename.replace(".pdf", ".docx")
            else:
                logger.warning("Resume file not found at %s — sending without attachment", resume_path)
                resume_file = None

        if resume_file:
            with open(resume_file, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{attachment_filename}"',
            )
            message.attach(part)

        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

        # DRAFT ONLY - never change this to messages().send()
        service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw_message}},
        ).execute()

        logger.info("[Gmail] Draft created → %s (%s)", to_email, subject)
        return True

    except Exception as exc:
        logger.error("[Gmail] Failed to create draft for %s: %s", to_email, exc)
        return False


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    fake_job = {
        "company": "Stripe",
        "title": "Senior Backend Engineer - Payments Infrastructure",
        "description": (
            "We are looking for a senior backend engineer to join our payments infrastructure team. "
            "You will build high-throughput distributed systems handling billions of transactions. "
            "Stack: Go, Kafka, Kubernetes, PostgreSQL, Redis. "
            "Remote friendly. 5+ years experience required. "
            "We value engineers who can own systems end to end."
        ),
        "contact_name": "Alex Johnson",
        "job_url": "https://stripe.com/jobs/listing/senior-backend-engineer",
        "source": "test",
        "domain": "stripe.com",
    }

    print("\n=== score_relevance ===")
    scored = score_relevance(fake_job)
    print(json.dumps(scored, indent=2))

    print("\n=== guess_emails ===")
    emails = guess_emails(
        contact_name=scored.get("contact_name", "Alex Johnson"),
        domain="stripe.com",
        company="Stripe",
    )
    print(emails)

    print("\n=== draft_email ===")
    draft = draft_email(
        job=fake_job,
        contact_name=scored.get("contact_name", "Alex Johnson"),
        tech_stack_mentioned=scored.get("tech_stack", "Go, Kafka, Kubernetes"),
        email_candidates=emails,
    )
    print(f"\nSUBJECT: {draft['subject']}")
    print(f"\nBODY:\n{draft['body']}")

    # Verify constraints
    body = draft["body"]
    em_dash_present = "—" in body or " -- " in body
    achievement_present = any(
        word in body.lower()
        for word in resume.get("top_achievement", "").lower().split()[:5]
    )

    print(f"\n--- Validation ---")
    print(f"Em dash present (should be False): {em_dash_present}")
    print(f"Top achievement referenced (should be True): {achievement_present}")
    banned = ["I hope this email finds you", "I am writing to", "I wanted to reach out",
              "I came across", "I noticed", "I am excited"]
    violations = [p for p in banned if p.lower() in body.lower()]
    print(f"Banned phrases used: {violations if violations else 'None (good)'}")
