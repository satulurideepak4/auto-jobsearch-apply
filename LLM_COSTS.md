# LLM Usage & Cost Analysis
## Auto Job Search Apply — AI Cost Breakdown

---

## 1. Where LLM Is Used (Every Single Call)

The system makes **6 distinct LLM calls** across the pipeline. Each has a different purpose, frequency, and cost weight.

| # | Module | What It Does | Model Type | Input Tokens | Output Tokens | Frequency |
|---|--------|-------------|------------|-------------|---------------|-----------|
| 1 | `resume_parser.py` | Reads your DOCX/PDF resume → extracts skills, job history, years of exp, summary as structured JSON | Tailoring | ~875 | ~400 | **Once per session** (result cached in memory) |
| 2 | `scorer.py` | Scores every scraped job 0–100 against your resume — decides which jobs to apply to | Scoring | ~950 | ~150 | **Every single scraped job** (high volume) |
| 3 | `jd_fetcher.py` | Renders a job URL with Playwright, strips DOM noise, extracts title/company/JD text | Scoring | ~2,100 | ~400 | Only when you **paste a job URL manually** |
| 4 | `resume_tailor.py` | Rewrites your resume summary, bullets, and tech skills to match the specific job description | Tailoring | ~2,000 | ~1,200 | Per job that **scores ≥ threshold** (default 70) |
| 5 | `answer_generator.py` | Writes answers to open-ended ATS questions ("Why do you want this role?", "Describe a challenge you overcame") | Tailoring | ~875 | ~300 | Per **free-text question** on external ATS form (avg 2 per app) |
| 6 | `form_filler.py` | Looks at every visible form field on the ATS page, maps your 21-field profile to fill actions | Scoring | ~875 | ~200 | Per **form step** on external ATS (avg 1.5 steps per app) |

---

## 2. Model Assignment (Current Config in .env)

The system splits tasks between two model tiers — a fast/cheap model for high-volume filtering, and a quality model for content generation.

```
Scoring tasks  → fast model  (high volume, simple JSON output)
Tailoring tasks → quality model (low volume, creative writing)
```

| Provider | Scoring Model | Tailoring Model |
|----------|--------------|-----------------|
| **Gemini** (current default) | `gemini-2.5-flash` | `gemini-2.5-pro` |
| **Claude** | `claude-haiku-4-5` | `claude-sonnet-4-6` |
| **OpenAI** | `gpt-4o-mini` | `gpt-4o` |

Which calls use which tier:

| LLM Call | Tier Used |
|----------|-----------|
| Resume Parse | Tailoring |
| Job Score | **Scoring** |
| JD Fetch | **Scoring** |
| Resume Tailor | Tailoring |
| Answer Generator | Tailoring |
| Form Fill Mapping | **Scoring** |

---

## 3. Provider Pricing (Mid-2025)

| Provider | Model | Input (per 1M tokens) | Output (per 1M tokens) | Used For |
|----------|-------|-----------------------|------------------------|---------|
| Google Gemini | `gemini-2.5-flash` | $0.075 | $0.30 | Scoring, Form Fill |
| Google Gemini | `gemini-2.5-pro` | $1.25 | $10.00 | Tailoring, Answers |
| Anthropic Claude | `claude-haiku-4-5` | $0.80 | $4.00 | Scoring, Form Fill |
| Anthropic Claude | `claude-sonnet-4-6` | $3.00 | $15.00 | Tailoring, Answers |
| OpenAI | `gpt-4o-mini` | $0.15 | $0.60 | Scoring, Form Fill |
| OpenAI | `gpt-4o` | $2.50 | $10.00 | Tailoring, Answers |

> **Note:** You are currently on **Vertex AI** (`GCP_PROJECT_ID=startup-1cd80`). Vertex AI pricing matches the above. Committed use discounts are available for high-volume usage.

---

## 4. Cost Per Application — By Job Board & Apply Type

### Assumptions used in this calculation
- 1 job scored = 1 score call
- 1 job applied to = 1 tailor call + form fill + answer questions
- External ATS application = 1.5 form-fill LLM steps + 2 open questions answered
- Native 1-click apply (Naukri/Instahyre) = no form fill LLM needed

---

### Naukri — Native 1-Click Apply
*(Your Naukri profile is used directly, no external form)*

| LLM Call | # Calls | Gemini Flash+Pro | Claude Haiku+Sonnet | GPT-4o-mini+4o |
|----------|---------|-----------------|---------------------|----------------|
| Score job | 1 | $0.0001 | $0.0009 | $0.0002 |
| Tailor resume | 1 | $0.0145 | $0.0240 | $0.0220 |
| Form fill LLM | 0 | $0.0000 | $0.0000 | $0.0000 |
| Answer questions | 0 | $0.0000 | $0.0000 | $0.0000 |
| **Total per app** | | **~$0.015** | **~$0.025** | **~$0.022** |
| **Per 100 apps** | | **~$1.50** | **~$2.50** | **~$2.20** |

---

### Naukri → External ATS (Greenhouse / Lever / Workday / Eightfold / Custom)
*(Job is on Naukri but "Apply on company site" redirects to the company's own form)*

| LLM Call | # Calls | Gemini Flash+Pro | Claude Haiku+Sonnet | GPT-4o-mini+4o |
|----------|---------|-----------------|---------------------|----------------|
| Score job | 1 | $0.0001 | $0.0009 | $0.0002 |
| Tailor resume | 1 | $0.0145 | $0.0240 | $0.0220 |
| Form fill LLM | 1.5 steps | $0.0001 | $0.0011 | $0.0002 |
| Answer questions | 2 | $0.0026 | $0.0090 | $0.0032 |
| **Total per app** | | **~$0.017** | **~$0.035** | **~$0.026** |
| **Per 100 apps** | | **~$1.70** | **~$3.50** | **~$2.60** |

---

### Instahyre — Native Apply
*(Direct apply with short cover note — no external ATS)*

| LLM Call | # Calls | Gemini Flash+Pro | Claude Haiku+Sonnet | GPT-4o-mini+4o |
|----------|---------|-----------------|---------------------|----------------|
| Score job | 1 | $0.0001 | $0.0009 | $0.0002 |
| Tailor resume | 1 | $0.0145 | $0.0240 | $0.0220 |
| Form fill LLM | 0 | $0.0000 | $0.0000 | $0.0000 |
| Answer questions | 0 | $0.0000 | $0.0000 | $0.0000 |
| **Total per app** | | **~$0.015** | **~$0.025** | **~$0.022** |
| **Per 100 apps** | | **~$1.50** | **~$2.50** | **~$2.20** |

---

### Wellfound (AngelList) — External ATS
*(Wellfound jobs almost always redirect to Greenhouse / Lever / Ashby)*

| LLM Call | # Calls | Gemini Flash+Pro | Claude Haiku+Sonnet | GPT-4o-mini+4o |
|----------|---------|-----------------|---------------------|----------------|
| Score job | 1 | $0.0001 | $0.0009 | $0.0002 |
| Tailor resume | 1 | $0.0145 | $0.0240 | $0.0220 |
| Form fill LLM | 1.5 steps | $0.0001 | $0.0011 | $0.0002 |
| Answer questions | 2 | $0.0026 | $0.0090 | $0.0032 |
| **Total per app** | | **~$0.017** | **~$0.035** | **~$0.026** |
| **Per 100 apps** | | **~$1.70** | **~$3.50** | **~$2.60** |

---

### Indeed / Glassdoor / ZipRecruiter → External ATS
*(Via JobSpy library — scrape only, apply goes to company ATS)*

| LLM Call | # Calls | Gemini Flash+Pro | Claude Haiku+Sonnet | GPT-4o-mini+4o |
|----------|---------|-----------------|---------------------|----------------|
| Score job | 1 | $0.0001 | $0.0009 | $0.0002 |
| Tailor resume | 1 | $0.0145 | $0.0240 | $0.0220 |
| Form fill LLM | 1.5 steps | $0.0001 | $0.0011 | $0.0002 |
| Answer questions | 2 | $0.0026 | $0.0090 | $0.0032 |
| **Total per app** | | **~$0.017** | **~$0.035** | **~$0.026** |
| **Per 100 apps** | | **~$1.70** | **~$3.50** | **~$2.60** |

---

### Direct URL (You paste a job link manually)
*(Extra JD Fetch call needed since there is no pre-scraped description)*

| LLM Call | # Calls | Gemini Flash+Pro | Claude Haiku+Sonnet | GPT-4o-mini+4o |
|----------|---------|-----------------|---------------------|----------------|
| JD Fetch (extra) | 1 | $0.0003 | $0.0020 | $0.0004 |
| Score job | 1 | $0.0001 | $0.0009 | $0.0002 |
| Tailor resume | 1 | $0.0145 | $0.0240 | $0.0220 |
| Form fill LLM | 1.5 steps | $0.0001 | $0.0011 | $0.0002 |
| Answer questions | 2 | $0.0026 | $0.0090 | $0.0032 |
| **Total per app** | | **~$0.020** | **~$0.037** | **~$0.028** |
| **Per 100 apps** | | **~$2.00** | **~$3.70** | **~$2.80** |

---

## 5. Full Daily Run Cost

A complete nightly pipeline run (2 AM scheduled):
- **100 jobs scraped** (50 India + 50 International)
- **25 jobs pass** score threshold (25% match rate)
- **10 applications submitted** (of the 25 tailored, 10 are actually applied to)

| Cost Component | Gemini Flash+Pro | Claude Haiku+Sonnet | GPT-4o-mini+4o |
|----------------|-----------------|---------------------|----------------|
| Score 100 jobs | $0.012 | $0.10 | $0.020 |
| Resume parse (1×) | ~$0.001 | ~$0.005 | ~$0.003 |
| Tailor 25 matched jobs | $0.363 | $0.735 | $0.580 |
| Form fill 10 apps | $0.001 | $0.011 | $0.002 |
| Answer questions (20 total) | $0.026 | $0.090 | $0.032 |
| **Total per daily run** | **~$0.40** | **~$0.94** | **~$0.64** |
| **Per month (30 days)** | **~$12** | **~$28** | **~$19** |
| **Per month (100 apps/day)** | **~$51** | **~$96** | **~$72** |

---

## 6. Cost by ATS Platform (External Forms)

Different ATS platforms need different numbers of LLM form-fill steps.

| ATS Platform | Avg Form Steps | LLM Calls for Fill | Extra Cost vs Native |
|-------------|----------------|-------------------|---------------------|
| Greenhouse | 1 page | 1 | +$0.003 |
| Lever | 1 page | 1 | +$0.003 |
| Ashby | 1 page | 1 | +$0.003 |
| Workday | 4–6 steps | 5 | +$0.010 |
| SmartRecruiters | 2–3 steps | 2 | +$0.005 |
| iCIMS | 3–5 steps | 4 | +$0.008 |
| Eightfold.ai | 2–3 steps | 2 | +$0.005 |
| Custom company form | 1–3 steps | 2 | +$0.005 |

> Workday is the most expensive ATS to fill — it has the most form steps.

---

## 7. Key Cost Insights

| Insight | Action |
|---------|--------|
| **Resume tailoring = 85% of total cost** | Raise score threshold from 70 → 75 to tailor fewer, better-matched jobs |
| **Scoring 100 jobs costs $0.01 on Gemini Flash** | Score everything — no reason to skip any job |
| **Form fill LLM is negligible** | No cost reason to use heuristic fallback — always use LLM on forms |
| **Answer generator adds ~15% per external ATS app** | Worth it — open questions are where most bots fail |
| **Tailoring a job that scores 71 is wasteful** | A 71-score job is unlikely to convert — set threshold to 75+ |
| **Gemini is ~2.4× cheaper than Claude for this workload** | Good default. Switch to Claude Sonnet only for quality-critical tailoring |
| **You are on Vertex AI (GCP_PROJECT_ID set)** | Committed use discounts available if volume grows |

---

## 8. Cost Optimisation Options

### Option A — Raise score threshold (biggest impact)
```env
MATCH_SCORE_THRESHOLD=75   # was 70 — saves ~20% on tailoring cost
```
At 75, roughly 15 jobs tailored instead of 25 per run → saves ~$0.10/day on Gemini.

### Option B — Two-stage scoring (skip LLM for obvious mismatches)
Add a keyword pre-filter before calling the LLM scorer:
- If job title shares 0 words with your search terms → score=0 without LLM call
- Saves ~10–15% of scoring calls

### Option C — Cache tailored resumes
If you apply to the same company twice (common with job boards), reuse the tailored resume.
Already partially in place via `resumes/tailored/{job_id}.docx`.

### Option D — Batch scoring
Instead of 1 LLM call per job, send 5 jobs in one call.
Reduces API overhead. Saves ~5% on scoring.

---

## 9. Summary Table

| Board | Apply Type | Cost/App (Gemini) | Cost/App (Claude) | Cost/100 Apps (Gemini) | Cost/100 Apps (Claude) |
|-------|-----------|------------------|-------------------|----------------------|----------------------|
| Naukri (native) | 1-click | **$0.015** | $0.025 | **$1.50** | $2.50 |
| Naukri (external ATS) | Form fill | **$0.017** | $0.035 | **$1.70** | $3.50 |
| Instahyre (native) | 1-click | **$0.015** | $0.025 | **$1.50** | $2.50 |
| Wellfound → ATS | Form fill | **$0.017** | $0.035 | **$1.70** | $3.50 |
| Indeed/Glassdoor → ATS | Form fill | **$0.017** | $0.035 | **$1.70** | $3.50 |
| Direct URL pasted | Form fill | **$0.020** | $0.037 | **$2.00** | $3.70 |
| Workday specifically | Multi-step form | **$0.025** | $0.050 | **$2.50** | $5.00 |

**Bottom line: Applying to 300 jobs/month costs approximately $12–$28/month depending on provider.**
