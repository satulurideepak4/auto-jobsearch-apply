"""
Stage 2: Job Scrapers
Sources: JobSpy (LinkedIn+Indeed+ZipRecruiter+Glassdoor), Twitter/X (Scweet),
         RemoteOK API, We Work Remotely RSS, Remotive API,
         WorkAtAStartup (YC), HN Who's Hiring, ProductHunt.
All search terms come from resume_parser.load_resume() — nothing hardcoded.
If one scraper fails, log and continue. Never crash the full pipeline.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))

import feedparser
import requests

from readme_store import read_config
from resume_parser import load_resume

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Resume-driven search terms
# ---------------------------------------------------------------------------
resume = load_resume()
SEARCH_TERMS: list[str] = resume.get("search_terms", [])
TWITTER_TERMS: list[str] = resume.get("twitter_search_terms", [])
TARGET_ROLES: list[str] = resume.get("target_roles", [])
CORE_SKILLS: list[str] = resume.get("core_skills", [])
PRIMARY_SKILLS: list[str] = resume.get("primary_skills", [])
AI_SKILLS: list[str] = resume.get("ai_skills", [])
TARGET_COMPANIES_STR: str = resume.get("target_companies", "")

_ALL_JOBSPY_TERMS = list(dict.fromkeys(SEARCH_TERMS))
_ALL_TWITTER_TERMS = list(dict.fromkeys(TWITTER_TERMS))

_JOB_TEMPLATE: dict = {
    "company": "", "title": "", "description": "",
    "contact_name": "", "job_url": "", "source": "", "domain": "",
}


def _make_job(**kwargs) -> dict:
    job = dict(_JOB_TEMPLATE)
    job.update(kwargs)
    return job


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def _domain_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
        hostname = parsed.hostname or ""
        return re.sub(r"^www\.", "", hostname).lower()
    except Exception:
        return ""


_STRIP_SUFFIXES = re.compile(
    r"\b(inc|ltd|llc|corp|co|technologies|technology|tech|solutions|group|"
    r"labs|lab|systems|consulting|services|software|pvt|plc|limited)\b",
    re.IGNORECASE,
)


def _domain_from_company(name: str) -> str:
    clean = _STRIP_SUFFIXES.sub("", name)
    clean = re.sub(r"[^a-zA-Z0-9]", "", clean).lower()
    return f"{clean}.com" if clean else ""


def _is_aggregator_url(url: str) -> bool:
    aggregators = {"linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com"}
    domain = _domain_from_url(url)
    return any(domain.endswith(a) for a in aggregators)


def _best_domain(job_url: str, company: str) -> str:
    if job_url and not _is_aggregator_url(job_url):
        d = _domain_from_url(job_url)
        if d:
            return d
    return _domain_from_company(company)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _dedup_by_domain(jobs: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for job in jobs:
        key = job.get("domain", "").lower().strip()
        if not key:
            out.append(job)
            continue
        if key not in seen:
            seen.add(key)
            out.append(job)
    return out


# ---------------------------------------------------------------------------
# SCRAPER 1: JobSpy — LinkedIn, Indeed, ZipRecruiter, Glassdoor
# ---------------------------------------------------------------------------

def scrape_jobspy() -> list[dict]:
    """Scrape LinkedIn, Indeed, ZipRecruiter, Glassdoor via JobSpy."""
    try:
        from jobspy import scrape_jobs
    except ImportError:
        logger.error("jobspy not installed — skipping")
        return []

    results: list[dict] = []
    seen_companies: set[str] = set()

    for term in _ALL_JOBSPY_TERMS:
        try:
            logger.info("[JobSpy] Searching: %s", term)
            df = scrape_jobs(
                site_name=["linkedin", "indeed"],
                search_term=term,
                results_wanted=50,
                hours_old=72,
                is_remote=True,
                linkedin_fetch_description=True,
            )
            if df is None or df.empty:
                logger.info("[JobSpy] No results for: %s", term)
                time.sleep(5)
                continue

            for _, row in df.iterrows():
                company = str(row.get("company", "") or "").strip()
                if not company or company.lower() == "nan":
                    continue
                company_key = company.lower()
                if company_key in seen_companies:
                    continue
                seen_companies.add(company_key)

                job_url = str(row.get("job_url", "") or "")
                domain = _best_domain(job_url, company)
                desc = str(row.get("description", "") or "")
                title = str(row.get("title", "") or "")

                results.append(_make_job(
                    company=company,
                    title=title,
                    description=desc,
                    contact_name="Hiring Team",
                    job_url=job_url,
                    source="jobspy",
                    domain=domain,
                ))

            logger.info("[JobSpy] %d unique companies after '%s'", len(results), term)
            time.sleep(5)

        except Exception as exc:
            logger.warning("[JobSpy] Error on '%s': %s", term, exc)
            time.sleep(5)

    logger.info("[JobSpy] Total: %d jobs", len(results))
    return results


# ---------------------------------------------------------------------------
# SCRAPER 2: Twitter/X via Scweet 5.x
# ---------------------------------------------------------------------------

def scrape_twitter() -> list[dict]:
    """Scrape Twitter/X hiring posts via Scweet 5.x (API-based, auth_token cookie)."""
    config = read_config()
    auth_token = config.get("TWITTER_AUTH_TOKEN", "")

    if not auth_token or auth_token in {"YOUR_TWITTER_AUTH_TOKEN_HERE", "", "None", "none"}:
        logger.warning("[Twitter] TWITTER_AUTH_TOKEN not set — skipping")
        return []

    try:
        from Scweet import Scweet as ScweetClient  # v5 client-based API
    except ImportError:
        logger.warning("[Twitter] Scweet not installed — run: pip install scweet")
        return []

    since = str(date.today() - timedelta(days=3))
    until = str(date.today())
    results: list[dict] = []
    seen_companies: set[str] = set()
    url_re = re.compile(r"https?://[^\s]+")

    # Store state DB in outreach dir so it persists between runs
    db_path = str(Path(__file__).parent / "scweet_state.db")

    try:
        client = ScweetClient(auth_token=auth_token, db_path=db_path)
    except Exception as exc:
        logger.warning("[Twitter] Scweet client init failed: %s", exc)
        return []

    for term in _ALL_TWITTER_TERMS:
        try:
            logger.info("[Twitter] Searching: %s", term)
            tweets = client.search(
                query=term,
                since=since,
                until=until,
                limit=50,
                display_type="Latest",
                save=False,
            )
            if not tweets:
                continue

            for tweet in tweets:
                text = tweet.get("text", "") or ""
                user = tweet.get("user", {}) or {}
                display_name = user.get("name", "") or tweet.get("Name", "") or "Unknown"
                company = display_name.strip()
                if not company or company.lower() in ("nan", "unknown", ""):
                    continue
                company_key = company.lower()
                if company_key in seen_companies:
                    continue
                seen_companies.add(company_key)

                url_match = url_re.search(text)
                url = url_match.group(0) if url_match else tweet.get("tweet_url", "")
                domain = _domain_from_url(url) if url else _domain_from_company(company)

                results.append(_make_job(
                    company=company,
                    title="(from Twitter post)",
                    description=text[:1000],
                    contact_name=display_name,
                    job_url=url,
                    source="twitter",
                    domain=domain,
                ))

        except Exception as exc:
            logger.warning("[Twitter] Error on '%s': %s", term, exc)

    logger.info("[Twitter] Total: %d jobs", len(results))
    return results


# ---------------------------------------------------------------------------
# SCRAPER 3: RemoteOK API (free, no auth)
# ---------------------------------------------------------------------------

_ENGINEERING_TITLE_KEYWORDS = [
    "engineer", "developer", "backend", "software", "architect", "platform",
    "devops", "infrastructure", "sre", "fullstack", "full stack", "java",
    "golang", "python", "ml", "ai", "llm", "data engineer", "cloud",
]

def _is_engineering_role(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _ENGINEERING_TITLE_KEYWORDS)


def scrape_remoteok() -> list[dict]:
    """Fetch jobs from RemoteOK public API."""
    results: list[dict] = []
    skill_keywords = [s.lower() for s in CORE_SKILLS + PRIMARY_SKILLS + AI_SKILLS]

    try:
        resp = requests.get(
            "https://remoteok.com/api",
            headers={"User-Agent": "Mozilla/5.0 (job-search-bot)"},
            timeout=20,
        )
        resp.raise_for_status()
        jobs = resp.json()

        for job in jobs[1:]:  # first item is metadata
            if not isinstance(job, dict):
                continue

            title = str(job.get("position", "") or "")
            if not _is_engineering_role(title):
                continue

            tags = [str(t).lower() for t in (job.get("tags") or [])]
            desc = str(job.get("description", "") or "")
            combined = (title + " " + desc + " " + " ".join(tags)).lower()

            if not any(kw in combined for kw in skill_keywords):
                continue

            company = str(job.get("company", "") or "")
            if not company or company.lower() in ("nan", "linkedin"):
                continue
            url = str(job.get("url", "") or "")
            domain = _best_domain(url, company)

            results.append(_make_job(
                company=company,
                title=title,
                description=desc[:2000],
                contact_name="Hiring Team",
                job_url=url,
                source="remoteok",
                domain=domain,
            ))

        logger.info("[RemoteOK] %d matching jobs", len(results))
    except Exception as exc:
        logger.warning("[RemoteOK] Failed: %s", exc)

    return results


# ---------------------------------------------------------------------------
# SCRAPER 4: We Work Remotely RSS
# ---------------------------------------------------------------------------

def scrape_weworkremotely() -> list[dict]:
    """Fetch backend and programming jobs from We Work Remotely RSS."""
    results: list[dict] = []
    skill_keywords = [s.lower() for s in CORE_SKILLS + PRIMARY_SKILLS + AI_SKILLS]
    feeds = [
        "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    ]

    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                title = entry.get("title", "") or ""
                summary = entry.get("summary", "") or ""
                combined = (title + " " + summary).lower()

                if not any(kw in combined for kw in skill_keywords):
                    continue

                link = entry.get("link", "") or ""
                domain = _domain_from_url(link)

                # WWR title format: "Company: Job Title" or "Job Title at Company"
                raw_title = title.strip()
                if ": " in raw_title:
                    company, job_title = raw_title.split(": ", 1)
                elif " at " in raw_title:
                    parts = raw_title.rsplit(" at ", 1)
                    job_title, company = parts[0].strip(), parts[1].strip()
                else:
                    company, job_title = "Unknown", raw_title

                if not company or company.lower() in ("unknown", "nan", ""):
                    continue

                results.append(_make_job(
                    company=company,
                    title=job_title,
                    description=summary[:2000],
                    contact_name="Hiring Team",
                    job_url=link,
                    source="weworkremotely",
                    domain=domain or _domain_from_company(company),
                ))
        except Exception as exc:
            logger.warning("[WeWorkRemotely] Feed %s failed: %s", feed_url, exc)

    logger.info("[WeWorkRemotely] %d matching jobs", len(results))
    return results


# ---------------------------------------------------------------------------
# SCRAPER 5: Remotive API (free, no auth)
# ---------------------------------------------------------------------------

def scrape_remotive() -> list[dict]:
    """Fetch remote jobs from Remotive public API."""
    results: list[dict] = []
    skill_keywords = [s.lower() for s in CORE_SKILLS + PRIMARY_SKILLS + AI_SKILLS]
    categories = ["software-dev", "data", "devops-sysadmin"]

    for category in categories:
        try:
            resp = requests.get(
                f"https://remotive.com/api/remote-jobs?category={category}&limit=100",
                timeout=20,
            )
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])

            for job in jobs:
                title = str(job.get("title", "") or "")
                if not _is_engineering_role(title):
                    continue
                desc = str(job.get("description", "") or "")
                tags = " ".join(job.get("tags") or [])
                combined = (title + " " + desc + " " + tags).lower()

                if not any(kw in combined for kw in skill_keywords):
                    continue

                company = str(job.get("company_name", "") or "")
                if not company or company.lower() == "nan":
                    continue
                url = str(job.get("url", "") or "")
                domain = _best_domain(url, company)

                results.append(_make_job(
                    company=company,
                    title=title,
                    description=desc[:2000],
                    contact_name="Hiring Team",
                    job_url=url,
                    source="remotive",
                    domain=domain,
                ))

            time.sleep(1)
        except Exception as exc:
            logger.warning("[Remotive] Category %s failed: %s", category, exc)

    logger.info("[Remotive] %d matching jobs", len(results))
    return results


# ---------------------------------------------------------------------------
# SCRAPER 6: WorkAtAStartup / YC Jobs
# ---------------------------------------------------------------------------

def scrape_yc_jobs() -> list[dict]:
    """Fetch YC startup remote engineering jobs.

    Strategy (waterfall — first that returns data wins):
      1. RSS feeds from workatastartup.com
      2. Direct HTML scrape of the public jobs listing page
    """
    results: list[dict] = []
    skill_keywords = [s.lower() for s in CORE_SKILLS + PRIMARY_SKILLS + AI_SKILLS]

    # --- Strategy 1: RSS feeds ---
    rss_feeds = [
        "https://www.workatastartup.com/jobs.rss?role=eng",
        "https://www.workatastartup.com/jobs.rss?role=eng&remote=yes",
    ]
    for feed_url in rss_feeds:
        try:
            feed = feedparser.parse(feed_url)
            if not feed.entries:
                continue
            for entry in feed.entries:
                title = entry.get("title", "") or ""
                if not _is_engineering_role(title):
                    continue
                summary = entry.get("summary", "") or ""
                combined = (title + " " + summary).lower()
                if not any(kw in combined for kw in skill_keywords):
                    continue
                link = entry.get("link", "") or ""
                raw = title.strip()
                if " at " in raw:
                    job_title, company = raw.rsplit(" at ", 1)
                elif ": " in raw:
                    company, job_title = raw.split(": ", 1)
                else:
                    job_title, company = raw, entry.get("author", "Unknown")
                company = company.strip()
                if not company or company.lower() in ("unknown", "nan"):
                    continue
                domain = _domain_from_url(link) or _domain_from_company(company)
                results.append(_make_job(
                    company=company,
                    title=job_title.strip(),
                    description=summary[:2000],
                    contact_name="Hiring Team",
                    job_url=link,
                    source="yc_jobs",
                    domain=domain,
                ))
        except Exception as exc:
            logger.debug("[YC] RSS feed %s failed: %s", feed_url, exc)

    if results:
        logger.info("[YC/WorkAtAStartup] %d matching jobs (from RSS)", len(results))
        return results

    # --- Strategy 2: Scrape public HTML jobs page ---
    logger.info("[YC/WorkAtAStartup] RSS empty — trying HTML scrape")
    try:
        resp = requests.get(
            "https://www.workatastartup.com/jobs?role=eng&remote=yes&jobType=fulltime",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/120.0.0.0 Safari/537.36"},
            timeout=20,
        )
        if resp.ok:
            # Extract job data from embedded JSON (Next.js __NEXT_DATA__ or similar)
            next_data_match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', resp.text, re.DOTALL)
            if next_data_match:
                import json as _json
                page_data = _json.loads(next_data_match.group(1))
                jobs_raw = (
                    page_data.get("props", {})
                             .get("pageProps", {})
                             .get("jobs", [])
                )
                for job_raw in jobs_raw:
                    title = str(job_raw.get("title", "") or "")
                    if not _is_engineering_role(title):
                        continue
                    desc = str(job_raw.get("description", "") or "")
                    combined = (title + " " + desc).lower()
                    if not any(kw in combined for kw in skill_keywords):
                        continue
                    company_obj = job_raw.get("company", {}) or {}
                    company = str(company_obj.get("name", "") or "")
                    if not company:
                        continue
                    slug = company_obj.get("slug", "")
                    job_url = f"https://www.workatastartup.com/companies/{slug}" if slug else ""
                    domain = _best_domain(job_url, company)
                    results.append(_make_job(
                        company=company,
                        title=title,
                        description=desc[:2000],
                        contact_name="Hiring Team",
                        job_url=job_url,
                        source="yc_jobs",
                        domain=domain,
                    ))

            # Fallback: simple regex extraction from HTML
            if not results:
                company_blocks = re.findall(
                    r'<div[^>]+class="[^"]*company-name[^"]*"[^>]*>([^<]+)</div>.*?'
                    r'<div[^>]+class="[^"]*job-title[^"]*"[^>]*>([^<]+)</div>',
                    resp.text, re.DOTALL
                )
                for company, title in company_blocks[:50]:
                    company = company.strip()
                    title = title.strip()
                    if not company or not _is_engineering_role(title):
                        continue
                    combined = (company + " " + title).lower()
                    if any(kw in combined for kw in skill_keywords):
                        domain = _domain_from_company(company)
                        results.append(_make_job(
                            company=company,
                            title=title,
                            description="",
                            contact_name="Hiring Team",
                            job_url="https://www.workatastartup.com/jobs",
                            source="yc_jobs",
                            domain=domain,
                        ))
    except Exception as exc:
        logger.warning("[YC/WorkAtAStartup] HTML scrape failed: %s", exc)

    logger.info("[YC/WorkAtAStartup] %d matching jobs", len(results))
    return results


# ---------------------------------------------------------------------------
# SCRAPER 7: HN Who's Hiring (parallelised)
# ---------------------------------------------------------------------------

def _fetch_hn_comment(kid_id: int, skill_keywords: list[str], email_re: re.Pattern) -> dict | None:
    """Fetch and parse one HN comment. Returns job dict or None.

    HN Who's Hiring posters are almost always the founder or a senior engineer —
    capture 'by' (HN username) as the contact so we email the actual decision-maker.
    """
    try:
        kid_r = requests.get(
            f"https://hacker-news.firebaseio.com/v0/item/{kid_id}.json",
            timeout=10,
        )
        kid_r.raise_for_status()
        comment = kid_r.json()
        text = comment.get("text", "") or ""
        text_lower = text.lower()

        if "remote" not in text_lower:
            return None
        if not any(skill in text_lower for skill in skill_keywords):
            return None

        lines = re.sub(r"<[^>]+>", " ", text).strip().split("\n")
        first_line = lines[0].strip() if lines else ""
        company_match = re.match(r"^([^|:]+)", first_line)
        company = company_match.group(1).strip() if company_match else "Unknown"

        email_match = email_re.search(text)
        email = email_match.group(0) if email_match else ""
        domain = email.split("@")[1] if email else _domain_from_company(company)
        plain_text = re.sub(r"<[^>]+>", " ", text)[:2000]

        # HN 'by' is the username of the person posting — typically founder/CTO
        hn_username = comment.get("by", "")
        contact_name = hn_username if hn_username else "Founder"

        return _make_job(
            company=company,
            title="(from HN Who's Hiring)",
            description=plain_text,
            contact_name=contact_name,
            job_url=f"https://news.ycombinator.com/item?id={kid_id}",
            source="hn_hiring",
            domain=domain,
        )
    except Exception:
        return None


def scrape_hn_hiring() -> list[dict]:
    """Scrape ALL comments from HN Who's Hiring thread, parallelised with 25 workers."""
    results: list[dict] = []
    skill_keywords = [s.lower() for s in CORE_SKILLS + PRIMARY_SKILLS + AI_SKILLS]
    email_re = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")

    try:
        r = requests.get(
            "https://hacker-news.firebaseio.com/v0/user/whoishiring/submitted.json",
            timeout=15,
        )
        r.raise_for_status()
        submitted = r.json() or []

        thread_id = submitted[0]
        thread_r = requests.get(
            f"https://hacker-news.firebaseio.com/v0/item/{thread_id}.json",
            timeout=15,
        )
        thread_r.raise_for_status()
        thread = thread_r.json()
        kids = thread.get("kids") or []

        logger.info("[HN] Fetching %d comments (parallelised, 25 workers) from thread %d", len(kids), thread_id)

        with ThreadPoolExecutor(max_workers=25) as pool:
            futures = {pool.submit(_fetch_hn_comment, kid_id, skill_keywords, email_re): kid_id for kid_id in kids}
            for future in as_completed(futures):
                job = future.result()
                if job:
                    results.append(job)

        logger.info("[HN] %d matching comments", len(results))
    except Exception as exc:
        logger.warning("[HN] Failed: %s", exc)

    return results


# ---------------------------------------------------------------------------
# SCRAPER 8: LinkedIn Posts (hiring posts via Voyager API, li_at cookie)
# ---------------------------------------------------------------------------

def scrape_linkedin_posts() -> list[dict]:
    """Scrape LinkedIn hiring posts using the unofficial Voyager search API.

    Requires LINKEDIN_LI_AT_COOKIE in outreach/README.md config.
    How to get li_at: Chrome → linkedin.com → F12 → Application → Cookies → li_at value.
    """
    config = read_config()
    li_at = config.get("LINKEDIN_LI_AT_COOKIE", "")

    if not li_at or li_at in {"YOUR_LINKEDIN_LI_AT_COOKIE_HERE", "", "None", "none"}:
        logger.info("[LinkedIn Posts] LINKEDIN_LI_AT_COOKIE not set — skipping")
        return []

    results: list[dict] = []
    seen_companies: set[str] = set()
    skill_keywords = [s.lower() for s in PRIMARY_SKILLS + AI_SKILLS[:4]]
    url_re = re.compile(r"https?://[^\s\"'<>]+")

    # Voyager API endpoint for post search
    search_url = "https://www.linkedin.com/voyager/api/voyagerSearchDashClusters"

    # Use only the top hiring-signal search terms to avoid rate limits
    linkedin_hiring_queries = [
        f"hiring {skill} engineer remote" for skill in PRIMARY_SKILLS[:3]
    ] + [
        "hiring backend engineer remote",
        "looking for java engineer",
        "hiring go engineer remote",
        "hiring senior backend engineer",
        "we are hiring remote engineer",
    ]

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/vnd.linkedin.normalized+json+2.1",
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "x-li-track": '{"clientVersion":"1.13.13"}',
        "csrf-token": "ajax:0",
    })
    session.cookies.set("li_at", li_at, domain=".linkedin.com")
    session.cookies.set("JSESSIONID", "ajax:0", domain=".linkedin.com")

    for query in linkedin_hiring_queries:
        try:
            params = {
                "decorationId": "com.linkedin.voyager.dash.deco.search.SearchClusterCollection-175",
                "q": "all",
                "query": (
                    f"(origin:GLOBAL_SEARCH_HEADER,keywords:{query},"
                    "selectedFilters:(resultType:List(CONTENT)),"
                    "spellCorrectionEnabled:true)"
                ),
                "start": "0",
                "count": "25",
            }
            resp = session.get(search_url, params=params, timeout=15)

            if resp.status_code == 401:
                logger.warning("[LinkedIn Posts] Auth failed — li_at may be expired. Re-copy from Chrome.")
                break
            if resp.status_code == 429:
                logger.warning("[LinkedIn Posts] Rate limited — stopping LinkedIn post search.")
                break
            if not resp.ok:
                logger.debug("[LinkedIn Posts] Non-200 for '%s': %d", query, resp.status_code)
                time.sleep(3)
                continue

            data = resp.json()
            elements = data.get("elements", [])

            for cluster in elements:
                items = cluster.get("items", [])
                for item in items:
                    entity = item.get("item", {}).get("entityResult", {}) or {}
                    summary = entity.get("summary", {}) or {}
                    text = (summary.get("text", {}) or {}).get("text", "") or ""
                    text_lower = text.lower()

                    if not any(kw in text_lower for kw in skill_keywords):
                        continue
                    if not any(kw in text_lower for kw in ("hiring", "looking for", "we're hiring", "join us", "open role")):
                        continue

                    title_obj = entity.get("title", {}) or {}
                    author_line = (title_obj.get("text", {}) or {}).get("text", "") or ""
                    company = author_line.split(" • ")[0].strip() if " • " in author_line else author_line[:60].strip()
                    if not company or company.lower() in ("nan", ""):
                        company = "LinkedIn Post"

                    company_key = company.lower()
                    if company_key in seen_companies:
                        continue
                    seen_companies.add(company_key)

                    nav_url = entity.get("navigationUrl", "") or ""
                    url_match = url_re.search(text)
                    url = url_match.group(0) if url_match else nav_url
                    domain = _domain_from_url(url) if url else _domain_from_company(company)

                    results.append(_make_job(
                        company=company,
                        title="(from LinkedIn hiring post)",
                        description=text[:2000],
                        contact_name=author_line[:80] or "Hiring Team",
                        job_url=nav_url or url,
                        source="linkedin_posts",
                        domain=domain,
                    ))

            time.sleep(3)  # respectful delay between searches

        except Exception as exc:
            logger.warning("[LinkedIn Posts] Error on '%s': %s", query, exc)
            time.sleep(3)

    logger.info("[LinkedIn Posts] Total: %d posts", len(results))
    return results


# ---------------------------------------------------------------------------
# SCRAPER 9: ProductHunt
# ---------------------------------------------------------------------------

def scrape_producthunt() -> list[dict]:
    """Fetch ProductHunt RSS and filter by resume keywords."""
    results: list[dict] = []
    try:
        feed = feedparser.parse("https://www.producthunt.com/feed")
        target_keywords = [k.strip() for k in TARGET_COMPANIES_STR.lower().split(",") if k.strip()]
        skill_keywords = [s.lower() for s in CORE_SKILLS + PRIMARY_SKILLS + AI_SKILLS]
        all_keywords = target_keywords + skill_keywords

        for entry in feed.entries:
            title = entry.get("title", "") or ""
            summary = entry.get("summary", "") or entry.get("description", "") or ""
            combined = (title + " " + summary).lower()

            if not any(kw in combined for kw in all_keywords):
                continue

            link = entry.get("link", "") or ""
            domain = _domain_from_url(link)
            company = title.split(" - ")[0].strip() if " - " in title else title[:60]

            results.append(_make_job(
                company=company,
                title=title,
                description=summary[:2000],
                contact_name="Hiring Team",
                job_url=link,
                source="producthunt",
                domain=domain,
            ))

        logger.info("[ProductHunt] %d matching entries", len(results))
    except Exception as exc:
        logger.warning("[ProductHunt] Failed: %s", exc)

    return results


# ---------------------------------------------------------------------------
# SCRAPER 10: Greenhouse ATS (public API, no auth)
# ---------------------------------------------------------------------------

_GREENHOUSE_BOARDS: dict[str, str] = {
    # AI / ML
    "anthropic": "Anthropic",
    "cohere": "Cohere",
    "scaleai": "Scale AI",
    "huggingface": "Hugging Face",
    "together": "Together AI",
    "mistralai": "Mistral AI",
    "perplexity": "Perplexity AI",
    # Fintech
    "brex": "Brex",
    "plaid": "Plaid",
    "stripe": "Stripe",
    "gusto": "Gusto",
    "carta": "Carta",
    "rippling": "Rippling",
    "ramp": "Ramp",
    "deel": "Deel",
    "remote": "Remote",
    "payoneer": "Payoneer",
    # Cloud / Infra / DevTools
    "hashicorp": "HashiCorp",
    "mongodb": "MongoDB",
    "snowflake": "Snowflake",
    "databricks": "Databricks",
    "datadog": "Datadog",
    "confluent": "Confluent",
    "cockroachlabs": "CockroachDB",
    "grafana": "Grafana Labs",
    "pulumi": "Pulumi",
    "dbtlabs": "dbt Labs",
    "airbyte": "Airbyte",
    "fivetran": "Fivetran",
    "temporal": "Temporal",
    "timescale": "Timescale",
    "redpanda": "Redpanda",
    "immuta": "Immuta",
    # SaaS / Productivity
    "notion": "Notion",
    "airtable": "Airtable",
    "retool": "Retool",
    "lattice": "Lattice",
    "figma": "Figma",
    "loom": "Loom",
    "clickup": "ClickUp",
    "coda": "Coda",
    "amplitude": "Amplitude",
    "intercom": "Intercom",
    # Security
    "1password": "1Password",
    "crowdstrike": "CrowdStrike",
    "snyk": "Snyk",
    "lacework": "Lacework",
    "orca": "Orca Security",
    # E-commerce / Platform
    "faire": "Faire",
    "doordash": "DoorDash",
    "instacart": "Instacart",
    "roblox": "Roblox",
    "canva": "Canva",
    # Enterprise
    "pagerduty": "PagerDuty",
    "digitalocean": "DigitalOcean",
    "cloudflare": "Cloudflare",
    "twilio": "Twilio",
    "zendesk": "Zendesk",
}


def _fetch_greenhouse_board(slug: str, company_name: str, skill_keywords: list[str]) -> list[dict]:
    """Fetch one Greenhouse board; return first 3 matching engineering jobs."""
    try:
        resp = requests.get(
            f"https://api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
            timeout=15,
        )
        if resp.status_code in (404, 410):
            return []
        resp.raise_for_status()
        jobs_raw = resp.json().get("jobs", [])
        results: list[dict] = []
        for job in jobs_raw:
            if len(results) >= 3:
                break
            title = str(job.get("title", "") or "")
            if not _is_engineering_role(title):
                continue
            content = str(job.get("content", "") or "")
            combined = (title + " " + content).lower()
            if not any(kw in combined for kw in skill_keywords):
                continue
            url = str(job.get("absolute_url", "") or "")
            domain = _best_domain(url, company_name)
            results.append(_make_job(
                company=company_name,
                title=title,
                description=re.sub(r"<[^>]+>", " ", content)[:2000],
                contact_name="Hiring Team",
                job_url=url,
                source="greenhouse",
                domain=domain,
            ))
        return results
    except Exception:
        return []


def scrape_greenhouse() -> list[dict]:
    """Fetch jobs from 60+ companies using Greenhouse ATS public API (no auth)."""
    skill_keywords = [s.lower() for s in CORE_SKILLS + PRIMARY_SKILLS + AI_SKILLS]
    all_results: list[dict] = []

    logger.info("[Greenhouse] Querying %d boards (20 parallel workers)", len(_GREENHOUSE_BOARDS))
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {
            pool.submit(_fetch_greenhouse_board, slug, name, skill_keywords): (slug, name)
            for slug, name in _GREENHOUSE_BOARDS.items()
        }
        for future in as_completed(futures):
            all_results.extend(future.result())

    logger.info("[Greenhouse] %d matching jobs", len(all_results))
    return all_results


# ---------------------------------------------------------------------------
# SCRAPER 11: Lever ATS (public API, no auth)
# ---------------------------------------------------------------------------

# Companies known (or likely) to use Lever. 404s are silently skipped.
_LEVER_COMPANIES: dict[str, str] = {
    "coinbase": "Coinbase",
    "netflix": "Netflix",
    "postman": "Postman",
    "netlify": "Netlify",
    "twitch": "Twitch",
    "robinhood": "Robinhood",
    "lyft": "Lyft",
    "metabase": "Metabase",
    "posthog": "PostHog",
    "replit": "Replit",
    "vercel": "Vercel",
    "supabase": "Supabase",
    "neon": "Neon",
    "render": "Render",
    "railway": "Railway",
    "dagger": "Dagger",
    "modal": "Modal",
    "warp": "Warp",
    "linear": "Linear",
    "prefect": "Prefect",
    "inngest": "Inngest",
    "resend": "Resend",
    "clerk": "Clerk",
    "liveblocks": "Liveblocks",
    "cal": "Cal.com",
    "formbricks": "Formbricks",
    "documenso": "Documenso",
    "trigger": "Trigger.dev",
    "turso": "Turso",
    "upstash": "Upstash",
    "fly": "Fly.io",
    "cube": "Cube Dev",
    "duckdb": "DuckDB",
    "motherduck": "MotherDuck",
    "qdrant": "Qdrant",
    "weaviate": "Weaviate",
    "chroma": "Chroma",
    "langchain": "LangChain",
    "llamaindex": "LlamaIndex",
}


def _fetch_lever_postings(company: str, company_name: str, skill_keywords: list[str]) -> list[dict]:
    """Fetch postings from Lever for one company; silently skip 404/403."""
    try:
        resp = requests.get(
            f"https://api.lever.co/v0/postings/{company}?mode=json",
            timeout=15,
        )
        if resp.status_code in (403, 404, 410):
            return []
        resp.raise_for_status()
        postings = resp.json()
        if not isinstance(postings, list):
            return []

        results: list[dict] = []
        for posting in postings:
            if len(results) >= 3:
                break
            title = str(posting.get("text", "") or "")
            if not _is_engineering_role(title):
                continue
            # Combine all content sections
            desc_plain = str(posting.get("descriptionPlain", "") or posting.get("description", "") or "")
            lists_text = " ".join(
                str(lst.get("content", ""))
                for lst in (posting.get("lists") or [])
                if isinstance(lst, dict)
            )
            additional = str(posting.get("additional", "") or "")
            full_text = " ".join([desc_plain, lists_text, additional])
            combined = (title + " " + full_text).lower()
            if not any(kw in combined for kw in skill_keywords):
                continue
            url = str(posting.get("hostedUrl", "") or posting.get("applyUrl", "") or "")
            domain = _best_domain(url, company_name)
            results.append(_make_job(
                company=company_name,
                title=title,
                description=re.sub(r"<[^>]+>", " ", full_text)[:2000],
                contact_name="Hiring Team",
                job_url=url,
                source="lever",
                domain=domain,
            ))
        return results
    except Exception:
        return []


def scrape_lever() -> list[dict]:
    """Fetch jobs from companies using Lever ATS (public API, 404s silently skipped)."""
    skill_keywords = [s.lower() for s in CORE_SKILLS + PRIMARY_SKILLS + AI_SKILLS]
    all_results: list[dict] = []

    logger.info("[Lever] Querying %d companies (20 parallel workers)", len(_LEVER_COMPANIES))
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {
            pool.submit(_fetch_lever_postings, slug, name, skill_keywords): (slug, name)
            for slug, name in _LEVER_COMPANIES.items()
        }
        for future in as_completed(futures):
            all_results.extend(future.result())

    logger.info("[Lever] %d matching jobs", len(all_results))
    return all_results


# ---------------------------------------------------------------------------
# SCRAPER 12: Ashby ATS (public API, no auth) — popular with modern startups
# ---------------------------------------------------------------------------

_ASHBY_BOARDS: dict[str, str] = {
    "Linear": "Linear",
    "mercury": "Mercury",
    "Warp": "Warp",
    "Modal": "Modal",
    "Railway": "Railway",
    "Render": "Render",
    "Vercel": "Vercel",
    "Supabase": "Supabase",
    "PostHog": "PostHog",
    "PlanetScale": "PlanetScale",
    "Cal.com": "Cal.com",
    "Dagger": "Dagger",
    "Prefect": "Prefect",
    "Inngest": "Inngest",
    "Resend": "Resend",
    "Clerk": "Clerk",
    "Liveblocks": "Liveblocks",
    "Trigger.dev": "Trigger.dev",
    "Turso": "Turso",
    "Upstash": "Upstash",
    "Neon": "Neon",
    "Fly.io": "Fly.io",
    "Qdrant": "Qdrant",
    "Weaviate": "Weaviate",
    "LangChain": "LangChain",
    "MotherDuck": "MotherDuck",
    "Replit": "Replit",
    "Formbricks": "Formbricks",
    "Documenso": "Documenso",
    "Twenty": "Twenty CRM",
}


def _fetch_ashby_board(board_id: str, company_name: str, skill_keywords: list[str]) -> list[dict]:
    """Fetch one Ashby public job board; silently skip non-200 responses."""
    try:
        resp = requests.post(
            "https://api.ashbyhq.com/posting-api/job-board",
            json={"organizationHostedJobsPageName": board_id},
            timeout=15,
        )
        if resp.status_code in (400, 404, 422):
            return []
        resp.raise_for_status()
        data = resp.json()
        jobs_raw = data.get("jobs", []) or data.get("jobPostings", [])

        results: list[dict] = []
        for job in jobs_raw:
            if len(results) >= 3:
                break
            title = str(job.get("title", "") or job.get("jobTitle", "") or "")
            if not _is_engineering_role(title):
                continue
            desc_html = str(job.get("descriptionHtml", "") or job.get("description", "") or "")
            desc_plain = re.sub(r"<[^>]+>", " ", desc_html)
            combined = (title + " " + desc_plain).lower()
            if not any(kw in combined for kw in skill_keywords):
                continue
            url = str(job.get("jobUrl", "") or job.get("applyUrl", "") or "")
            domain = _best_domain(url, company_name)
            results.append(_make_job(
                company=company_name,
                title=title,
                description=desc_plain[:2000],
                contact_name="Hiring Team",
                job_url=url,
                source="ashby",
                domain=domain,
            ))
        return results
    except Exception:
        return []


def scrape_ashby() -> list[dict]:
    """Fetch jobs from modern startups using Ashby ATS public API (no auth)."""
    skill_keywords = [s.lower() for s in CORE_SKILLS + PRIMARY_SKILLS + AI_SKILLS]
    all_results: list[dict] = []

    logger.info("[Ashby] Querying %d boards (15 parallel workers)", len(_ASHBY_BOARDS))
    with ThreadPoolExecutor(max_workers=15) as pool:
        futures = {
            pool.submit(_fetch_ashby_board, board_id, name, skill_keywords): (board_id, name)
            for board_id, name in _ASHBY_BOARDS.items()
        }
        for future in as_completed(futures):
            all_results.extend(future.result())

    logger.info("[Ashby] %d matching jobs", len(all_results))
    return all_results


# ---------------------------------------------------------------------------
# SCRAPER 13: Wellfound (formerly AngelList) — startup job board
# ---------------------------------------------------------------------------

def scrape_wellfound() -> list[dict]:
    """Scrape Wellfound startup job listings. Tries __NEXT_DATA__ then BeautifulSoup."""
    results: list[dict] = []
    skill_keywords = [s.lower() for s in CORE_SKILLS + PRIMARY_SKILLS + AI_SKILLS]

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    urls_to_try = [
        "https://wellfound.com/jobs?role=Software+Engineer&remote=true",
        "https://wellfound.com/jobs?role=Backend+Engineer&remote=true",
    ]

    for page_url in urls_to_try:
        try:
            resp = requests.get(page_url, headers=headers, timeout=20)
            if not resp.ok:
                continue

            # Strategy 1: extract __NEXT_DATA__ JSON
            import json as _json
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', resp.text, re.DOTALL)
            if match:
                page_data = _json.loads(match.group(1))
                # Try multiple known nested paths
                jobs_raw = (
                    page_data.get("props", {}).get("pageProps", {}).get("jobs", [])
                    or page_data.get("props", {}).get("pageProps", {}).get("initialData", {}).get("jobs", [])
                    or []
                )
                for job in jobs_raw:
                    role = job.get("role", {}) or job
                    title = str(role.get("title", "") or job.get("title", "") or "")
                    if not _is_engineering_role(title):
                        continue
                    desc = str(job.get("description", "") or "")
                    combined = (title + " " + desc).lower()
                    if not any(kw in combined for kw in skill_keywords):
                        continue
                    startup = job.get("startup", {}) or job.get("company", {}) or {}
                    company = str(startup.get("name", "") or "")
                    if not company:
                        continue
                    slug = startup.get("slug", "")
                    url = str(job.get("canonicalUrl", "") or (f"https://wellfound.com/company/{slug}" if slug else ""))
                    domain = _best_domain(url, company)
                    results.append(_make_job(
                        company=company, title=title,
                        description=desc[:2000],
                        contact_name="Hiring Team",
                        job_url=url, source="wellfound", domain=domain,
                    ))

            # Strategy 2: BeautifulSoup parse job cards
            if not results:
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(resp.text, "html.parser")
                    seen: set[str] = set()
                    for card in soup.find_all(["div", "li"], class_=re.compile(r"job|listing|posting|startup", re.I))[:80]:
                        title_el = card.find(["h2", "h3", "a"], class_=re.compile(r"title|role", re.I))
                        company_el = card.find(class_=re.compile(r"company|startup|name", re.I))
                        if not title_el or not company_el:
                            continue
                        title = title_el.get_text(strip=True)
                        company = company_el.get_text(strip=True)
                        if not _is_engineering_role(title) or not company or company.lower() in seen:
                            continue
                        combined = (title + " " + company).lower()
                        if not any(kw in combined for kw in skill_keywords):
                            continue
                        seen.add(company.lower())
                        link_el = card.find("a", href=True)
                        url = link_el["href"] if link_el else ""
                        if url and not url.startswith("http"):
                            url = "https://wellfound.com" + url
                        domain = _best_domain(url, company)
                        results.append(_make_job(
                            company=company, title=title, description="",
                            contact_name="Hiring Team",
                            job_url=url, source="wellfound", domain=domain,
                        ))
                except ImportError:
                    logger.debug("[Wellfound] bs4 not available for fallback scrape")

            if results:
                break  # got results from first successful URL

        except Exception as exc:
            logger.warning("[Wellfound] Error on %s: %s", page_url, exc)

    logger.info("[Wellfound] %d matching jobs", len(results))
    return results


# ---------------------------------------------------------------------------
# SCRAPER 14: BuiltIn — tech job board (HTML scrape; API returns 405)
# ---------------------------------------------------------------------------

def scrape_builtin() -> list[dict]:
    """Scrape BuiltIn remote engineering jobs via __NEXT_DATA__ then BeautifulSoup."""
    results: list[dict] = []
    skill_keywords = [s.lower() for s in CORE_SKILLS + PRIMARY_SKILLS + AI_SKILLS]
    seen_companies: set[str] = set()

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    urls_to_try = [
        "https://builtin.com/jobs/remote/dev-engineering",
        "https://builtin.com/jobs/remote?specializations=Software+Engineer%2FProgrammer",
        "https://builtin.com/jobs/remote?specializations=Backend+Engineer",
    ]

    for page_url in urls_to_try:
        try:
            resp = requests.get(page_url, headers=headers, timeout=20)
            if not resp.ok:
                logger.debug("[BuiltIn] Non-200 on %s: %d", page_url, resp.status_code)
                continue

            # Strategy 1: __NEXT_DATA__ JSON
            import json as _json
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', resp.text, re.DOTALL)
            if match:
                try:
                    page_data = _json.loads(match.group(1))
                    page_props = page_data.get("props", {}).get("pageProps", {})
                    jobs_raw = (
                        page_props.get("jobs", [])
                        or page_props.get("jobListings", [])
                        or []
                    )
                    for job in jobs_raw:
                        title = str(job.get("title", "") or job.get("jobTitle", "") or "")
                        if not _is_engineering_role(title):
                            continue
                        company_obj = job.get("company", {}) or {}
                        company = str(company_obj.get("name", "") or job.get("companyName", "") or "")
                        if not company or company.lower() in seen_companies:
                            continue
                        desc = str(job.get("description", "") or "")
                        combined = (title + " " + desc).lower()
                        if not any(kw in combined for kw in skill_keywords):
                            continue
                        seen_companies.add(company.lower())
                        job_url = str(job.get("url", "") or job.get("jobUrl", "") or "")
                        if job_url and not job_url.startswith("http"):
                            job_url = "https://builtin.com" + job_url
                        domain = _best_domain(job_url, company)
                        results.append(_make_job(
                            company=company, title=title,
                            description=desc[:2000],
                            contact_name="Hiring Team",
                            job_url=job_url, source="builtin", domain=domain,
                        ))
                except Exception:
                    pass

            # Strategy 2: BeautifulSoup parse
            if not results:
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(resp.text, "html.parser")
                    # BuiltIn uses article tags or divs with data-id for job cards
                    job_cards = (
                        soup.find_all("article")
                        or soup.find_all("div", attrs={"data-id": True})
                        or soup.find_all("li", class_=re.compile(r"job|listing", re.I))
                    )
                    for card in job_cards[:60]:
                        title_el = (
                            card.find(["h2", "h3"], class_=re.compile(r"title|position", re.I))
                            or card.find("a", class_=re.compile(r"title|job", re.I))
                        )
                        company_el = card.find(class_=re.compile(r"company|employer", re.I))
                        if not title_el:
                            continue
                        title = title_el.get_text(strip=True)
                        company = company_el.get_text(strip=True) if company_el else ""
                        if not title or not _is_engineering_role(title):
                            continue
                        if not company or company.lower() in seen_companies:
                            continue
                        combined = (title + " " + company).lower()
                        if not any(kw in combined for kw in skill_keywords):
                            continue
                        seen_companies.add(company.lower())
                        link_el = card.find("a", href=True)
                        job_url = link_el["href"] if link_el else ""
                        if job_url and not job_url.startswith("http"):
                            job_url = "https://builtin.com" + job_url
                        domain = _best_domain(job_url, company)
                        results.append(_make_job(
                            company=company, title=title, description="",
                            contact_name="Hiring Team",
                            job_url=job_url, source="builtin", domain=domain,
                        ))
                except ImportError:
                    logger.debug("[BuiltIn] bs4 not available for fallback scrape")

            if results:
                break

        except Exception as exc:
            logger.warning("[BuiltIn] Error on %s: %s", page_url, exc)

    logger.info("[BuiltIn] %d matching jobs", len(results))
    return results


# ---------------------------------------------------------------------------
# Unified scrape_all
# ---------------------------------------------------------------------------

def scrape_all() -> list[dict]:
    """Run all scrapers and return merged, deduplicated list by domain."""
    all_jobs: list[dict] = []
    source_counts: dict[str, int] = {}

    scrapers = [
        ("jobspy",           scrape_jobspy),
        ("twitter",          scrape_twitter),
        ("linkedin_posts",   scrape_linkedin_posts),
        ("remoteok",         scrape_remoteok),
        ("weworkremotely",   scrape_weworkremotely),
        ("remotive",         scrape_remotive),
        ("yc_jobs",          scrape_yc_jobs),
        ("hn_hiring",        scrape_hn_hiring),
        ("producthunt",      scrape_producthunt),
        ("greenhouse",       scrape_greenhouse),
        ("lever",            scrape_lever),
        ("ashby",            scrape_ashby),
        ("wellfound",        scrape_wellfound),
        ("builtin",          scrape_builtin),
    ]

    for name, fn in scrapers:
        try:
            jobs = fn()
            source_counts[name] = len(jobs)
            all_jobs.extend(jobs)
        except Exception as exc:
            logger.error("[%s] Unexpected failure: %s", name, exc)
            source_counts[name] = 0

    logger.info("Source counts: %s", source_counts)
    deduped = _dedup_by_domain(all_jobs)
    logger.info("Total unique jobs (by domain): %d", len(deduped))
    return deduped


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(f"Search terms ({len(_ALL_JOBSPY_TERMS)}): {_ALL_JOBSPY_TERMS}\n")

    print("Testing ATS scrapers (Greenhouse, Lever, Ashby)...")
    for name, fn in [
        ("Greenhouse",        scrape_greenhouse),
        ("Lever",             scrape_lever),
        ("Ashby",             scrape_ashby),
        ("Wellfound",         scrape_wellfound),
        ("BuiltIn",           scrape_builtin),
        ("RemoteOK",          scrape_remoteok),
        ("WeWorkRemotely",    scrape_weworkremotely),
        ("Remotive",          scrape_remotive),
        ("YC/WorkAtAStartup", scrape_yc_jobs),
        ("HN Hiring",         scrape_hn_hiring),
        ("ProductHunt",       scrape_producthunt),
        ("LinkedIn Posts",    scrape_linkedin_posts),
    ]:
        jobs = fn()
        print(f"[{name}] {len(jobs)} jobs")
        if jobs:
            print(f"  First: {jobs[0]['company']} | {jobs[0]['title'][:60]}")
