from __future__ import annotations

from app.llm.base import LLMProvider


class ClaudeProvider(LLMProvider):
    """LLM provider backed by Anthropic Claude via the Messages API.

    The Anthropic client is created once at instantiation and reused across calls.
    """

    def __init__(self, api_key: str | None, model: str) -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set. Set it in your .env file.")
        self.model = model
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)

    def generate(self, prompt: str, **params) -> str:
        from app.config import get_settings
        s = get_settings()

        response = self._client.messages.create(
            model=self.model,
            max_tokens=params.get("max_tokens", s.LLM_MAX_TOKENS),
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
