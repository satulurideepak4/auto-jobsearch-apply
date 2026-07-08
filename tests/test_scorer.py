"""Tests for JobScorer."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.llm.base import LLMProvider
from app.matching.scorer import JobScorer


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


class FakeLLM(LLMProvider):
    """A fake LLM that returns a preset response string."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.last_prompt: str = ""

    def generate(self, prompt: str, **params) -> str:
        self.last_prompt = prompt
        return self.response


def _make_score_json(score: int = 85) -> str:
    return json.dumps(
        {
            "score": score,
            "reasoning": "Candidate has strong Python skills matching the JD.",
            "matched_skills": ["Python", "FastAPI"],
            "missing_skills": ["Kubernetes"],
        }
    )


def _make_job(
    title: str = "Senior Python Engineer",
    description: str = "Looking for a Python developer with FastAPI experience.",
    salary_min: float | None = None,
    salary_max: float | None = None,
    currency: str | None = None,
) -> dict:
    return {
        "title": title,
        "company": "Acme Corp",
        "location": "Remote",
        "description": description,
        "job_url": "https://example.com/job/1",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "currency": currency,
    }


def _make_resume() -> dict:
    return {
        "skills": ["Python", "FastAPI", "PostgreSQL", "Docker"],
        "years_of_experience": 5,
        "current_role": "Backend Engineer",
        "summary": "Experienced backend engineer specialising in Python web services.",
        "role_history": [],
        "education": ["B.Tech Computer Science"],
    }


def _make_settings(
    salary_floor_inr: int | None = None,
    salary_floor_usd: int | None = None,
) -> MagicMock:
    settings = MagicMock()
    settings.salary_floor_inr = salary_floor_inr
    settings.salary_floor_usd = salary_floor_usd
    return settings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_score_returns_expected_keys():
    """score() result must contain score, reasoning, matched_skills, missing_skills."""
    llm = FakeLLM(_make_score_json(85))
    scorer = JobScorer(llm)

    result = scorer.score(_make_job(), _make_resume())

    assert "score" in result
    assert "reasoning" in result
    assert "matched_skills" in result
    assert "missing_skills" in result


def test_score_value_is_integer_in_range():
    """score must be an integer between 0 and 100."""
    llm = FakeLLM(_make_score_json(85))
    scorer = JobScorer(llm)

    result = scorer.score(_make_job(), _make_resume())

    assert isinstance(result["score"], int)
    assert 0 <= result["score"] <= 100


def test_score_returns_correct_value():
    """score value should match the JSON returned by the LLM."""
    llm = FakeLLM(_make_score_json(72))
    scorer = JobScorer(llm)

    result = scorer.score(_make_job(), _make_resume())

    assert result["score"] == 72


def test_below_salary_floor_usd_returns_zero():
    """score() should return 0 with 'Below salary floor' reasoning when salary is below USD floor."""
    settings = _make_settings(salary_floor_usd=150000)
    llm = FakeLLM(_make_score_json(90))  # LLM would score high, but salary filter wins
    scorer = JobScorer(llm, settings=settings)

    job = _make_job(salary_min=80000, salary_max=120000, currency="USD")
    result = scorer.score(job, _make_resume())

    assert result["score"] == 0
    assert "salary floor" in result["reasoning"].lower()
    assert result["matched_skills"] == []
    assert result["missing_skills"] == []


def test_below_salary_floor_inr_returns_zero():
    """score() should return 0 when INR salary is below the INR floor."""
    settings = _make_settings(salary_floor_inr=3000000)
    llm = FakeLLM(_make_score_json(88))
    scorer = JobScorer(llm, settings=settings)

    job = _make_job(salary_min=1500000, salary_max=2000000, currency="INR")
    result = scorer.score(job, _make_resume())

    assert result["score"] == 0
    assert result["reasoning"] == "Below salary floor"


def test_above_salary_floor_calls_llm():
    """When salary exceeds floor, LLM should be called and score returned."""
    settings = _make_settings(salary_floor_usd=100000)
    llm = FakeLLM(_make_score_json(80))
    scorer = JobScorer(llm, settings=settings)

    job = _make_job(salary_min=110000, salary_max=140000, currency="USD")
    result = scorer.score(job, _make_resume())

    assert result["score"] == 80


def test_llm_called_with_job_title_and_skills():
    """The prompt sent to the LLM must include the job title and candidate skills."""
    llm = FakeLLM(_make_score_json(70))
    scorer = JobScorer(llm)
    resume = _make_resume()
    job = _make_job(title="Staff Python Engineer")

    scorer.score(job, resume)

    assert "Staff Python Engineer" in llm.last_prompt
    assert "Python" in llm.last_prompt


def test_json_code_fence_is_stripped():
    """score() should correctly parse JSON wrapped in ```json ``` fences."""
    score_json = _make_score_json(65)
    fenced_response = f"```json\n{score_json}\n```"

    llm = FakeLLM(fenced_response)
    scorer = JobScorer(llm)

    result = scorer.score(_make_job(), _make_resume())

    assert result["score"] == 65
    assert result["matched_skills"] == ["Python", "FastAPI"]


def test_no_salary_data_does_not_filter():
    """When job has no salary data, the salary floor filter should not apply."""
    settings = _make_settings(salary_floor_usd=200000)
    llm = FakeLLM(_make_score_json(78))
    scorer = JobScorer(llm, settings=settings)

    job = _make_job()  # salary_min=None, salary_max=None
    result = scorer.score(job, _make_resume())

    # LLM should have been called and returned 78
    assert result["score"] == 78


def test_matched_and_missing_skills_returned():
    """matched_skills and missing_skills lists should be returned from the LLM response."""
    llm = FakeLLM(_make_score_json(85))
    scorer = JobScorer(llm)

    result = scorer.score(_make_job(), _make_resume())

    assert "Python" in result["matched_skills"]
    assert "Kubernetes" in result["missing_skills"]
