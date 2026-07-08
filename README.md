# Job Agent

## Overview

Job Agent is an automated job search pipeline that scrapes job postings from multiple boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter), scores each posting against your resume using an LLM (Claude, Gemini, or OpenAI), and — once you approve a match — tailors your resume materials and fills the application form automatically using Playwright. The entire workflow is controlled via a REST API and a nightly scheduled pipeline, keeping your job search running hands-free while keeping you in the loop before any form is submitted.

---

## Setup

1. **Install dependencies and Playwright browser:**
   ```bash
   ./requirements.sh
   ```

2. **Configure your environment:**
   ```bash
   cp .env.example .env
   # Open .env and fill in DATABASE_URL, your chosen LLM API key, etc.
   ```

3. **Start PostgreSQL** (locally or via Docker):
   ```bash
   # Example with Docker:
   docker run -d --name jobagent-db \
     -e POSTGRES_USER=user \
     -e POSTGRES_PASSWORD=password \
     -e POSTGRES_DB=jobagent \
     -p 5432:5432 postgres:16
   ```

4. **Run the server:**
   ```bash
   ./run.sh
   ```
   The API will be available at `http://localhost:8000`.

   To run a one-off scrape immediately (without waiting for the nightly cron):
   ```bash
   ./run.sh --scrape-now
   ```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Health check — returns `{"status": "ok"}` |
| POST | `/api/v1/apply` | **One-shot apply** — give a job URL, pipeline fetches JD → scores → tailors → fills → submits |
| POST | `/api/v1/scrape` | Trigger an on-demand scrape + scoring pipeline in the background |
| GET | `/api/v1/jobs` | List all scraped jobs (query params: `limit`, `offset`) |
| GET | `/api/v1/matches` | List match records (query param: `status` — default `pending`) |
| POST | `/api/v1/matches/{match_id}/approve` | Approve a match; triggers resume tailoring in background |
| POST | `/api/v1/matches/{match_id}/reject` | Reject a match |
| GET | `/api/v1/applications` | List all applications (optional `status` filter) |
| GET | `/api/v1/applications/{application_id}` | Get a single application with job + tailored material |
| POST | `/api/v1/applications/{application_id}/fill` | Launch Playwright to fill the application form |

### One-shot Apply (`POST /api/v1/apply`)

Paste any job URL and the pipeline runs end-to-end in the background:

```bash
curl -X POST http://localhost:8000/api/v1/apply \
  -H "Content-Type: application/json" \
  -d '{"url": "https://boards.greenhouse.io/company/jobs/123456"}'
```

```json
{
  "application_id": "...",
  "job_id": "...",
  "status": "started",
  "message": "Pipeline running in background. Poll GET /api/v1/applications/<id> for status."
}
```

**Body parameters:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | required | Direct link to the job posting |
| `auto_submit` | bool | `false` | Click the submit button automatically |
| `force` | bool | `false` | Apply even if score is below `MATCH_SCORE_THRESHOLD` |

**Application status progression:**

`pending` → `filled` (form filled, waiting for review) → `submitted` (auto-submitted)

If the score is below threshold and `force=false`, status is set to `rejected` with a note explaining the score.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection URL (`postgresql+asyncpg://...`) |
| `LLM_PROVIDER` | No | LLM backend: `claude` (default), `gemini`, or `openai` |
| `ANTHROPIC_API_KEY` | If claude | API key for Anthropic Claude |
| `GEMINI_API_KEY` | If gemini | API key for Google Gemini |
| `OPENAI_API_KEY` | If openai | API key for OpenAI |
| `CLAUDE_SCORING_MODEL` | No | Claude model for job scoring (default: `claude-haiku-4-5-20251001`) |
| `CLAUDE_TAILORING_MODEL` | No | Claude model for resume tailoring (default: `claude-sonnet-4-6`) |
| `GEMINI_SCORING_MODEL` | No | Gemini model for job scoring (default: `gemini-2.5-flash-lite`) |
| `GEMINI_TAILORING_MODEL` | No | Gemini model for resume tailoring (default: `gemini-2.5-flash`) |
| `OPENAI_SCORING_MODEL` | No | OpenAI model for job scoring (default: `gpt-4o-mini`) |
| `OPENAI_TAILORING_MODEL` | No | OpenAI model for resume tailoring (default: `gpt-4o`) |
| `AUTO_SUBMIT` | No | `true` to click submit button automatically (default: `false`) |
| `MATCH_SCORE_THRESHOLD` | No | Minimum LLM score (0-100) to save a match (default: `70`) |
| `SALARY_FLOOR_INR` | No | Reject jobs below this salary in INR (optional) |
| `SALARY_FLOOR_USD` | No | Reject jobs below this salary in USD (optional) |
| `INDIA_SEARCH_TERMS` | No | Search keywords for India job boards (default: `software engineer`) |
| `INDIA_LOCATIONS` | No | Comma-separated locations to search in India (default: `Bengaluru,Hyderabad,Remote`) |
| `INDIA_RESULTS_WANTED` | No | Max results per location for India search (default: `50`) |
| `INTL_SEARCH_TERMS` | No | Search keywords for international remote jobs (default: `software engineer`) |
| `INTL_RESULTS_WANTED` | No | Max results for international search (default: `50`) |
| `INTL_ENABLED` | No | Enable international remote search (default: `true`) |
| `SCRAPE_CRON_HOUR` | No | UTC hour for nightly scrape (default: `2`) |
| `SCRAPE_CRON_MINUTE` | No | UTC minute for nightly scrape (default: `0`) |
| `RESUME_PATH` | No | Path to base resume — `.pdf` and `.docx` both supported (default: `resumes/base_resume.pdf`) |

---

## Search Profiles

Job Agent runs two parallel search profiles:

**India Profile** (`INDIA_*` variables)
- Searches each location listed in `INDIA_LOCATIONS` (comma-separated) independently.
- Uses `country_indeed="India"` for Indeed searches.
- Deduplicates results across locations by `(company, title, location)`.

**International Remote Profile** (`INTL_*` variables)
- Searches with `location="Remote"` and `country_indeed="USA"`.
- Only runs when `INTL_ENABLED=true`.
- Useful for finding fully-remote international positions open to global candidates.

Both profiles scrape Indeed, LinkedIn, Glassdoor, and ZipRecruiter simultaneously.

---

## Form Automation

Job Agent uses Playwright (Chromium, non-headless by default) to fill application forms.

**Safety defaults:**
- `AUTO_SUBMIT=false` — the browser opens and fills fields but stops before clicking submit. You can review everything before the form is sent.
- Set `AUTO_SUBMIT=true` in `.env` to enable fully automated submission.

**Supported ATS platforms** (auto-detected from URL):
- Greenhouse (`greenhouse.io`)
- Lever (`lever.co`)
- Ashby (`ashby.com`, `jobs.ashbyhq.com`)
- Workday (`myworkday`, `wd1/wd3/wd5`)
- Generic (heuristic fallback for unlisted platforms)

Personal info fields (`APPLICANT_FIRST_NAME`, `APPLICANT_LAST_NAME`, `APPLICANT_EMAIL`, `APPLICANT_PHONE`, `APPLICANT_LINKEDIN`) are read from environment variables at fill time.

---

## Running Tests

```bash
pytest tests/
```

Tests use `unittest.mock` and do not require any external API keys or a running database.
