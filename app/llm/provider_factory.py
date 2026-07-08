from __future__ import annotations

from typing import TYPE_CHECKING

from app.llm.base import LLMProvider
from app.llm.providers.claude_provider import ClaudeProvider
from app.llm.providers.gemini_provider import GeminiProvider
from app.llm.providers.openai_provider import OpenAIProvider

if TYPE_CHECKING:
    from app.config import Settings


def get_provider(
    settings: "Settings | None" = None,
    task: str = "scoring",
) -> LLMProvider:
    """Instantiate and return the configured LLM provider for the given task.

    Args:
        settings: Optional Settings instance. If None, the cached singleton is used.
        task: Either "scoring" (high-volume filter) or "tailoring" (quality generation).
              Controls which model is selected when per-task env vars are set.

    Returns:
        An LLMProvider implementation.

    Raises:
        ValueError: For unknown LLM_PROVIDER values or missing API keys.
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()

    provider_name = settings.llm_provider.lower()

    if provider_name == "claude":
        model = (
            settings.claude_tailoring_model
            if task == "tailoring"
            else settings.claude_scoring_model
        )
        return ClaudeProvider(settings.anthropic_api_key, model)
    elif provider_name == "gemini":
        model = (
            settings.gemini_tailoring_model
            if task == "tailoring"
            else settings.gemini_scoring_model
        )
        return GeminiProvider(settings.gcp_project_id, settings.gcp_location, model)
    elif provider_name == "openai":
        model = (
            settings.openai_tailoring_model
            if task == "tailoring"
            else settings.openai_scoring_model
        )
        return OpenAIProvider(settings.openai_api_key, model)
    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{settings.llm_provider}'. "
            "Valid values: claude, gemini, openai"
        )
