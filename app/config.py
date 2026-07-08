from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Database
    DATABASE_URL: str

    # LLM provider selection
    LLM_PROVIDER: str = Field(default="gemini")
    ANTHROPIC_API_KEY: Optional[str] = Field(default=None)
    GEMINI_API_KEY: Optional[str] = Field(default=None)  # only needed for AI Studio; Vertex AI uses ADC
    OPENAI_API_KEY: Optional[str] = Field(default=None)

    # Vertex AI config (used when LLM_PROVIDER=gemini)
    GCP_PROJECT_ID: Optional[str] = Field(default=None)
    GCP_LOCATION: str = Field(default="us-central1")

    # Model names — per-task overrides (scoring = high-volume filter, tailoring = quality generation)
    CLAUDE_SCORING_MODEL: str = Field(default="claude-haiku-4-5-20251001")
    CLAUDE_TAILORING_MODEL: str = Field(default="claude-sonnet-4-6")
    GEMINI_SCORING_MODEL: str = Field(default="gemini-2.5-flash")
    GEMINI_TAILORING_MODEL: str = Field(default="gemini-2.5-pro")
    OPENAI_SCORING_MODEL: str = Field(default="gpt-4o-mini")
    OPENAI_TAILORING_MODEL: str = Field(default="gpt-4o")

    # LLM generation parameters
    LLM_TEMPERATURE: float = Field(default=0.1)
    LLM_MAX_TOKENS: int = Field(default=4096)

    # Automation
    AUTO_SUBMIT: bool = Field(default=False)

    # Matching / filtering
    MATCH_SCORE_THRESHOLD: int = Field(default=70)
    SALARY_FLOOR_INR: Optional[int] = Field(default=None)
    SALARY_FLOOR_USD: Optional[int] = Field(default=None)

    # India search profile
    INDIA_SEARCH_TERMS: str = Field(default="software engineer")
    INDIA_LOCATIONS: str = Field(default="Bengaluru,Hyderabad,Remote")
    INDIA_RESULTS_WANTED: int = Field(default=50)

    # International search profile
    INTL_SEARCH_TERMS: str = Field(default="software engineer")
    INTL_RESULTS_WANTED: int = Field(default=50)
    INTL_ENABLED: bool = Field(default=True)

    # Scheduler (24h UTC)
    SCRAPE_CRON_HOUR: int = Field(default=2)
    SCRAPE_CRON_MINUTE: int = Field(default=0)

    # Resume
    RESUME_PATH: str = Field(default="resumes/base_resume.pdf")

    # Applicant identity — used for resume filename generation and form filling
    APPLICANT_FIRST_NAME: str = Field(default="")
    APPLICANT_LAST_NAME: str = Field(default="")
    APPLICANT_EMAIL: str = Field(default="")
    APPLICANT_PHONE: str = Field(default="")
    APPLICANT_LINKEDIN: str = Field(default="")
    APPLICANT_PASSWORD: str = Field(default="")  # used for ATS account creation (Workday etc.)

    # Extended profile — used by LLM to fill ATS-specific fields automatically
    APPLICANT_YEARS_EXP: str = Field(default="")
    APPLICANT_CURRENT_EMPLOYER: str = Field(default="")
    APPLICANT_CURRENT_TITLE: str = Field(default="")
    APPLICANT_WORK_AUTH: str = Field(default="Yes")
    APPLICANT_VISA_NEEDED: str = Field(default="No")
    APPLICANT_RELOCATE: str = Field(default="No")
    APPLICANT_SALARY_EXPECT: str = Field(default="")
    APPLICANT_NOTICE_PERIOD: str = Field(default="Immediate")
    APPLICANT_GITHUB: str = Field(default="")
    APPLICANT_PORTFOLIO: str = Field(default="")
    APPLICANT_CITY: str = Field(default="")
    APPLICANT_COUNTRY: str = Field(default="India")

    # Naukri search filters
    NAUKRI_SALARY_MIN_LPA: Optional[int] = Field(default=None)
    NAUKRI_SALARY_MAX_LPA: Optional[int] = Field(default=None)
    NAUKRI_EXP_MIN: int = Field(default=0)
    NAUKRI_EXP_MAX: Optional[int] = Field(default=None)
    NAUKRI_WORK_MODE: str = Field(default="any")   # wfh | hybrid | office | any
    NAUKRI_POSTED_DAYS: int = Field(default=30)    # 1 | 7 | 15 | 30 | 0 (any)
    NAUKRI_JOB_TYPE: str = Field(default="fulltime")  # fulltime | parttime | contract | any
    NAUKRI_PAGE_LIMIT: int = Field(default=5)
    NAUKRI_CURRENT_CTC: Optional[str] = Field(default=None)  # e.g. "18" for 18 LPA; used in quick-apply

    # Instahyre / Wellfound scraping limits
    INSTAHYRE_PAGE_LIMIT: int = Field(default=5)
    WELLFOUND_SCROLL_LIMIT: int = Field(default=8)
    CRAWLER_PAGE_LIMIT: int = Field(default=8)

    # Browser automation — shared across all portal drivers
    BROWSER_HEADLESS: bool = Field(default=False)
    BROWSER_TIMEOUT_MS: int = Field(default=30_000)
    BROWSER_SHORT_WAIT_MS: int = Field(default=2_000)
    BROWSER_MEDIUM_WAIT_MS: int = Field(default=2_500)
    BROWSER_LONG_WAIT_MS: int = Field(default=3_000)
    BROWSER_UA: str = Field(
        default=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    )

    # Portal apply messages — support {role_title} and {company} placeholders
    INSTAHYRE_APPLY_MESSAGE: str = Field(
        default=(
            "I am excited about this opportunity and believe my background "
            "aligns well with the role."
        )
    )
    WELLFOUND_INTEREST_TEMPLATE: str = Field(
        default=(
            "I am excited about the {role_title} opportunity at {company}. "
            "My background aligns with the technical challenges described, "
            "and I am eager to contribute to the product's growth."
        )
    )

    # Form filling limits
    MAX_FORM_STEPS: int = Field(default=20)
    MAX_WORKDAY_STEPS: int = Field(default=18)

    # Portal credentials — fallback only (primary auth is via saved session cookies)
    NAUKRI_EMAIL: Optional[str] = Field(default=None)
    NAUKRI_PASSWORD: Optional[str] = Field(default=None)
    INSTAHYRE_EMAIL: Optional[str] = Field(default=None)
    INSTAHYRE_PASSWORD: Optional[str] = Field(default=None)
    WELLFOUND_EMAIL: Optional[str] = Field(default=None)
    WELLFOUND_PASSWORD: Optional[str] = Field(default=None)

    # Browser session storage directory (persists logins across runs)
    SESSION_DIR: str = Field(default="~/.auto-jobsearch/sessions")

    # Chrome profile name to read cookies/history from (e.g. "Default", "Profile 9")
    CHROME_PROFILE: str = Field(default="Default")

    # Convenience lower-case properties so the rest of the code can use either style
    @property
    def llm_provider(self) -> str:
        return self.LLM_PROVIDER

    @property
    def anthropic_api_key(self) -> Optional[str]:
        return self.ANTHROPIC_API_KEY

    @property
    def gemini_api_key(self) -> Optional[str]:
        return self.GEMINI_API_KEY

    @property
    def openai_api_key(self) -> Optional[str]:
        return self.OPENAI_API_KEY

    @property
    def gcp_project_id(self) -> Optional[str]:
        return self.GCP_PROJECT_ID

    @property
    def gcp_location(self) -> str:
        return self.GCP_LOCATION

    @property
    def claude_scoring_model(self) -> str:
        return self.CLAUDE_SCORING_MODEL

    @property
    def claude_tailoring_model(self) -> str:
        return self.CLAUDE_TAILORING_MODEL

    @property
    def gemini_scoring_model(self) -> str:
        return self.GEMINI_SCORING_MODEL

    @property
    def gemini_tailoring_model(self) -> str:
        return self.GEMINI_TAILORING_MODEL

    @property
    def openai_scoring_model(self) -> str:
        return self.OPENAI_SCORING_MODEL

    @property
    def openai_tailoring_model(self) -> str:
        return self.OPENAI_TAILORING_MODEL

    @property
    def auto_submit(self) -> bool:
        return self.AUTO_SUBMIT

    @property
    def match_score_threshold(self) -> int:
        return self.MATCH_SCORE_THRESHOLD

    @property
    def salary_floor_inr(self) -> Optional[int]:
        return self.SALARY_FLOOR_INR

    @property
    def salary_floor_usd(self) -> Optional[int]:
        return self.SALARY_FLOOR_USD

    @property
    def india_search_terms(self) -> str:
        return self.INDIA_SEARCH_TERMS

    @property
    def india_locations(self) -> str:
        return self.INDIA_LOCATIONS

    @property
    def india_results_wanted(self) -> int:
        return self.INDIA_RESULTS_WANTED

    @property
    def intl_search_terms(self) -> str:
        return self.INTL_SEARCH_TERMS

    @property
    def intl_results_wanted(self) -> int:
        return self.INTL_RESULTS_WANTED

    @property
    def intl_enabled(self) -> bool:
        return self.INTL_ENABLED

    @property
    def scrape_cron_hour(self) -> int:
        return self.SCRAPE_CRON_HOUR

    @property
    def scrape_cron_minute(self) -> int:
        return self.SCRAPE_CRON_MINUTE

    @property
    def resume_path(self) -> str:
        return self.RESUME_PATH


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
