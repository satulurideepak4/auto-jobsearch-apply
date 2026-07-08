from __future__ import annotations

from app.llm.base import LLMProvider


class OpenAIProvider(LLMProvider):
    """LLM provider backed by OpenAI Chat Completions API.

    The OpenAI client is created once at instantiation and reused across calls.
    """

    def __init__(self, api_key: str | None, model: str) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is not set. Set it in your .env file.")
        self.model = model
        import openai
        self._client = openai.OpenAI(api_key=api_key)

    def generate(self, prompt: str, **params) -> str:
        from app.config import get_settings
        s = get_settings()

        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=params.get("max_tokens", s.LLM_MAX_TOKENS),
            temperature=params.get("temperature", s.LLM_TEMPERATURE),
        )
        return response.choices[0].message.content
