from __future__ import annotations

import abc


class LLMProvider(abc.ABC):
    """Abstract base class for all LLM provider implementations."""

    @abc.abstractmethod
    def generate(self, prompt: str, **params) -> str:
        """Send *prompt* to the underlying model and return the response text.

        Args:
            prompt: The user prompt to send.
            **params: Optional provider-specific parameters (e.g. max_tokens).

        Returns:
            The model's response as a plain string.
        """
