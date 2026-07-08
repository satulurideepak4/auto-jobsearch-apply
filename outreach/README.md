# Outreach Pipeline

Automated job outreach pipeline. Drop your resume in base_resume.pdf, fill the config below, run the pipeline, review Gmail drafts every morning.

## Setup
1. Drop your resume at outreach/base_resume.pdf
2. Fill GCP_PROJECT_ID and TWITTER_AUTH_TOKEN in config below
3. Download Gmail OAuth credentials from GCP console, save as outreach/gmail_credentials.json
4. Run: python outreach/resume_parser.py to parse resume and verify extracted profile
5. Run: python outreach/main.py
6. Open Gmail drafts, review, click send on genuine ones, delete rest
7. Update status column manually after sending (sent/bounced/replied)

## Notes
- Pipeline creates max 15 drafts per day
- Resume is parsed once and cached in resume_cache.json
- Run python outreach/resume_parser.py --refresh-resume to re-parse after updating resume
- All search terms, target roles, email content driven by your resume automatically

---

<!-- CONFIG_START -->
## Config

GCP_PROJECT_ID: startup-1cd80
TWITTER_AUTH_TOKEN: cf56a7496601486af175c25e61aaec7d0407f438
LINKEDIN_LI_AT_COOKIE: YOUR_LINKEDIN_LI_AT_COOKIE_HERE
GITHUB_TOKEN: YOUR_GITHUB_TOKEN_HERE_OPTIONAL
GMAIL_CREDENTIALS_PATH: outreach/google_credentials.json
RESUME_PATH: outreach/base_resume.pdf
RESUME_CACHE_PATH: outreach/resume_cache.json
TOKEN_PATH: outreach/gmail_token.json
DAILY_CAP: 15
SCORING_MODEL: gemini-2.5-flash
TAILORING_MODEL: gemini-2.5-pro
PORTFOLIO_URL: https://satulurideepak4.github.io/satulurideepak4/
LINKEDIN_URL: https://www.linkedin.com/in/satuluri-deepak-69227a1a1/
GITHUB_URL: https://github.com/satulurideepak4
<!-- CONFIG_END -->

---

<!-- TRACKING_START -->
## Tracking

| date | company | contact | domain | email_guessed | draft_created | status | notes |
|------|---------|---------|--------|---------------|---------------|--------|-------|
| 2026-07-07 | Ostium Labs | Hiring Team | ostium.com | hiring@ostium.com | false | dry_run | The role is a strong match as it requires a Senior Backend Engineer with Golang experience to build trading systems, aligning perfectly with the candidate's core skills and target roles. |
| 2026-07-07 | Novem Legion, LLC | Hiring Team | novemlegion.com | hiring@novemlegion.com | false | dry_run | The candidate's experience with RAG pipelines, LLM integration (GPT, Gemini), and backend systems is a direct match for building a conversational AI agent with xAI Grok. |
| 2026-07-07 | Revstar | Hiring Team | revstar.com | hiring@revstar.com | false | dry_run | The candidate's extensive experience in AI, cloud-native development, and AWS aligns perfectly with RevStar's focus as an AI-first innovation shop and AWS Advanced Tier Partner. |
| 2026-07-07 | Tivity Health | Hiring Team | tivityhealth.com | hiring@tivityhealth.com | false | dry_run | The role's focus on building APIs and services on AWS aligns with the candidate's extensive backend, microservices, and cloud experience, and the Principal-level technical leadership matches his career goals, despite a potential gap in the required full-stack/UI skills. |
| 2026-07-07 | nan | Hiring Team | nan.com | hiring@nan.com | true | draft_created | The candidate has direct experience with the entire requested tech stack, including FastAPI, MongoDB, Postgres, and a deep, specialized background in building with production LLMs, which is the core focus of the role. |
| 2026-07-07 | Ostium Labs | Hiring Team | ostium.com | hiring@ostium.com | true | draft_created | Manual draft via send_draft.py |
| 2026-07-07 | Ostium Labs | Hiring Team | ostium.com | hiring@ostium.com | true | draft_created | Manual draft via send_draft.py |
| 2026-07-07 | Ostium Labs | Hiring Team | ostium.com | hiring@ostium.com | true | draft_created | Manual draft via send_draft.py |
| 2026-07-08 | Birlasoft | Hiring Team | birlasoft.com | hiring@birlasoft.com | true | draft_created | The role for a Senior Java Backend Engineer focusing on AI and Microservices is a perfect fit for the candidate's extensive experience with Java, Spring Boot, and specialized skills in Spring AI, RAG pipelines, and LLM integration. |
| 2026-07-08 | Pax8 | Hiring Team | pax8.com | hiring@pax8.com | true | draft_created | The candidate's senior backend experience in building distributed systems, combined with a strong and modern AI/LLM skillset, is a perfect match for this greenfield, AI-forward FinTech platform role. |
| 2026-07-08 | BrickRed Systems | Hiring Team | brickred.com | hiring@brickred.com | true | draft_created | The candidate's core skills in Java, Spring Boot, Microservices, and Kafka, combined with extensive experience in AI-enabled solutions and LLM integration, perfectly match the requirements for building scalable, privacy-compliant backend and AI-powered applications. |
| 2026-07-08 | Alignerr | Hiring Team | alignerr.com | hiring@alignerr.com | true | draft_created | The role requires a backend expert in Java, Go, or Python to review AI-generated code, which perfectly aligns with the candidate's senior experience in these languages and specialized skills in LLM integration and evaluation. |
| 2026-07-08 | Chuwa America | Hiring Team | chuwaamerica.com | hiring@chuwaamerica.com | true | draft_created | The role for a Senior Java Engineer focusing on Spring Boot, AWS, microservices, and event-driven architecture is an excellent match for the candidate's core skills and experience. |
| 2026-07-08 | Ritchie Bros. | Hiring Team | ritchiebros.com | careers@ritchiebros.com | true | draft_created | The candidate's deep experience with API-driven services and event-driven architecture using technologies like Spring Boot, Kafka, and Microservices directly aligns with the core requirements of the role. |
| 2026-07-08 | YO IT Consulting | Hiring Team | yoit.com | hiring@yoit.com | true | draft_created | The candidate's expertise in Java, Spring Boot, and Microservices perfectly aligns with the core responsibilities, and their extensive AI/LLM experience is a significant advantage for the role's goal of training AI systems. |
| 2026-07-08 | Xplor Technologies | Hiring Team | xplor.com | hiring@xplor.com | true | draft_created | The candidate's core skills in Java and Spring Boot are a direct match for this role building a payment gateway, and his seniority and experience with microservices are highly relevant. |
| 2026-07-08 | Ladders | Hiring Team | ladders.com | hiring@ladders.com | true | draft_created | The candidate's extensive experience in building low-latency, high-throughput distributed systems with Java/Go and direct expertise in AI/LLM integration are a perfect match for this role at the intersection of finance and AI. |
| 2026-07-08 | Addepar | Hiring Team | addepar.com | hiring@addepar.com | true | draft_created | The candidate's profile as a Senior Backend Engineer with extensive experience in Java and distributed systems is a direct match for the primary requirements of this Senior Software Engineer role on the Order Management System team. |
| 2026-07-08 | Cypress HCM | Hiring Team | cypresshcm.com | hiring@cypresshcm.com | true | draft_created | The candidate's 4 years of experience and deep expertise in Java, Spring, Microservices, Kafka, Redis, Docker, Kubernetes, and AWS align perfectly with the job's core requirements and tech stack. |
| 2026-07-08 | First Citizens Bank | Hiring Team | firstcitizensbank.com | hiring@firstcitizensbank.com | true | draft_created | The candidate's extensive experience as a Senior Backend Engineer with a core focus on Java and Spring Boot is a strong match for this Senior Java Engineer role within a banking technology team. |
| 2026-07-08 | IntraEdge | Hiring Team | intraedge.com | hiring@intraedge.com | true | draft_created | The candidate is an excellent match, possessing all the core skills required by the job post, including senior-level experience in Java 17, Spring Boot, Microservices, REST APIs, and CI/CD, plus familiarity with the specified Google Cloud Platform. |
| 2026-07-08 | Otter | Hiring Team | otter.com | hiring@otter.com | true | draft_created | The candidate's extensive experience building distributed systems, microservices, and middleware with Java, Go, and event-driven architectures is a direct match for the role's focus on architecting an integrations platform. |
| 2026-07-08 | Close | Hiring Team | close.com | hiring@close.com | true | draft_created | The candidate has direct experience with the core technologies listed, including Python, Flask, MongoDB, PostgreSQL, Redis, and the AWS/Kubernetes infrastructure. |
<!-- TRACKING_END -->
