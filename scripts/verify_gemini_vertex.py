#!/usr/bin/env python3
"""Smoke-test both Gemini model configurations with a minimal prompt.

Run from the repo root:
    python scripts/verify_gemini_vertex.py

Prints which model responded and its usage_metadata so you can confirm
both are reachable and billing correctly against your Gemini API quota.
"""
from __future__ import annotations

import sys
import os

# Allow running from repo root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _call(api_key: str, model_name: str, prompt: str) -> None:
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)
    response = model.generate_content(prompt)

    meta = response.usage_metadata
    print(f"  model            : {model_name}")
    print(f"  response snippet : {response.text[:120].strip()!r}")
    print(f"  prompt_tokens    : {meta.prompt_token_count}")
    print(f"  output_tokens    : {meta.candidates_token_count}")
    print(f"  total_tokens     : {meta.total_token_count}")


def main() -> None:
    from app.config import get_settings

    settings = get_settings()

    if not settings.GEMINI_API_KEY:
        sys.exit("GEMINI_API_KEY is not set in .env — cannot run verification.")

    prompt = (
        "In one sentence, what is the most important thing to include in a software "
        "engineer resume? Reply concisely."
    )

    print(f"\n--- Scoring model ({settings.GEMINI_SCORING_MODEL}) ---")
    _call(settings.GEMINI_API_KEY, settings.GEMINI_SCORING_MODEL, prompt)

    print(f"\n--- Tailoring model ({settings.GEMINI_TAILORING_MODEL}) ---")
    _call(settings.GEMINI_API_KEY, settings.GEMINI_TAILORING_MODEL, prompt)

    print("\nBoth models responded successfully.")


if __name__ == "__main__":
    main()
