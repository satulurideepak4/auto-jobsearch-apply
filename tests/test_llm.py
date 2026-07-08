"""Tests for LLM providers and provider factory."""
from __future__ import annotations

import sys
import types
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

from app.llm.base import LLMProvider
from app.llm.provider_factory import get_provider
from app.llm.providers.claude_provider import ClaudeProvider
from app.llm.providers.gemini_provider import GeminiProvider
from app.llm.providers.openai_provider import OpenAIProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_settings():
    """Return a mock settings object."""
    settings = MagicMock()
    settings.llm_provider = "claude"
    settings.anthropic_api_key = "sk-ant-fake"
    settings.gemini_api_key = "fake-gemini-key"
    settings.openai_api_key = "sk-fake-openai"
    settings.claude_model = "claude-sonnet-4-6"
    settings.gemini_model = "gemini-1.5-pro"
    settings.openai_model = "gpt-4o"
    return settings


# ---------------------------------------------------------------------------
# Test: provider raises ValueError when api_key is missing
# ---------------------------------------------------------------------------


def test_claude_provider_raises_if_api_key_none():
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        ClaudeProvider(api_key=None, model="claude-sonnet-4-6")


def test_claude_provider_raises_if_api_key_empty():
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        ClaudeProvider(api_key="", model="claude-sonnet-4-6")


def test_gemini_provider_raises_if_api_key_none():
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiProvider(api_key=None, model="gemini-1.5-pro")


def test_gemini_provider_raises_if_api_key_empty():
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiProvider(api_key="", model="gemini-1.5-pro")


def test_openai_provider_raises_if_api_key_none():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIProvider(api_key=None, model="gpt-4o")


def test_openai_provider_raises_if_api_key_empty():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIProvider(api_key="", model="gpt-4o")


# ---------------------------------------------------------------------------
# Test: get_provider returns correct class
# ---------------------------------------------------------------------------


def test_get_provider_returns_claude(mock_settings):
    mock_settings.llm_provider = "claude"
    provider = get_provider(mock_settings)
    assert isinstance(provider, ClaudeProvider)


def test_get_provider_returns_gemini(mock_settings):
    mock_settings.llm_provider = "gemini"
    provider = get_provider(mock_settings)
    assert isinstance(provider, GeminiProvider)


def test_get_provider_returns_openai(mock_settings):
    mock_settings.llm_provider = "openai"
    provider = get_provider(mock_settings)
    assert isinstance(provider, OpenAIProvider)


def test_get_provider_raises_for_unknown(mock_settings):
    mock_settings.llm_provider = "unknown_llm"
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        get_provider(mock_settings)


# ---------------------------------------------------------------------------
# Test: generate() returns text (mocking vendor SDK)
# ---------------------------------------------------------------------------


def test_claude_provider_generate_returns_text():
    """ClaudeProvider.generate() should return the text from the first content block."""
    provider = ClaudeProvider(api_key="sk-ant-fake", model="claude-sonnet-4-6")

    # Build a mock response matching the Anthropic Messages API shape
    mock_content_block = MagicMock()
    mock_content_block.text = "Hello from Claude"
    mock_response = MagicMock()
    mock_response.content = [mock_content_block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    mock_anthropic_module = MagicMock()
    mock_anthropic_module.Anthropic.return_value = mock_client

    with patch.dict(sys.modules, {"anthropic": mock_anthropic_module}):
        result = provider.generate("Say hello")

    assert result == "Hello from Claude"
    mock_client.messages.create.assert_called_once_with(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": "Say hello"}],
    )


def test_claude_provider_generate_respects_max_tokens():
    """ClaudeProvider.generate() should pass max_tokens from **params."""
    provider = ClaudeProvider(api_key="sk-ant-fake", model="claude-sonnet-4-6")

    mock_content_block = MagicMock()
    mock_content_block.text = "Short response"
    mock_response = MagicMock()
    mock_response.content = [mock_content_block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    mock_anthropic_module = MagicMock()
    mock_anthropic_module.Anthropic.return_value = mock_client

    with patch.dict(sys.modules, {"anthropic": mock_anthropic_module}):
        result = provider.generate("Short prompt", max_tokens=512)

    assert result == "Short response"
    call_kwargs = mock_client.messages.create.call_args
    assert call_kwargs.kwargs["max_tokens"] == 512


def test_openai_provider_generate_returns_text():
    """OpenAIProvider.generate() should return the message content from the first choice."""
    provider = OpenAIProvider(api_key="sk-fake", model="gpt-4o")

    mock_message = MagicMock()
    mock_message.content = "Hello from GPT"
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_client_instance = MagicMock()
    mock_client_instance.chat.completions.create.return_value = mock_response

    mock_openai_module = MagicMock()
    mock_openai_module.OpenAI.return_value = mock_client_instance

    with patch.dict(sys.modules, {"openai": mock_openai_module}):
        result = provider.generate("Say hello")

    assert result == "Hello from GPT"
    mock_client_instance.chat.completions.create.assert_called_once_with(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Say hello"}],
        max_tokens=4096,
    )


# ---------------------------------------------------------------------------
# Test: LLMProvider is abstract
# ---------------------------------------------------------------------------


def test_llm_provider_is_abstract():
    """LLMProvider cannot be instantiated directly."""
    with pytest.raises(TypeError):
        LLMProvider()  # type: ignore[abstract]
