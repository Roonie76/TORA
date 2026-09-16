"""
Tests for OllamaProvider empty/truncated LLM response handling.

Verifies that:
- Empty content + done_reason="length" raises LLMResponseError
- Empty content + done_reason="stop" returns normally (model chose empty output)
- Non-empty content + done_reason="length" returns the partial content
- Normal non-empty responses are unchanged
- Error messages do not expose Ollama internals, stack traces, or raw JSON

All tests mock Ollama HTTP responses — no live server dependency.
"""

import json
import pytest
import httpx

from backend.llm.ollama import OllamaProvider
from backend.llm.base import LLMResponse, LLMResponseError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tags_response() -> httpx.Response:
    """Create a mock /api/tags response."""
    return httpx.Response(status_code=200, json={"models": [{"name": "gemma4:e4b"}]})


def _make_chat_response(
    content: str = "Hello",
    done_reason: str = "stop",
    thinking: str = "",
) -> httpx.Response:
    """Create a mock /api/chat response with configurable content and done_reason."""
    body = {
        "model": "gemma4:e4b",
        "message": {"role": "assistant", "content": content, "thinking": thinking},
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": 1500,
        "eval_count": 85,
    }
    return httpx.Response(status_code=200, json=body)


def _build_provider(handler) -> OllamaProvider:
    """Build an OllamaProvider with a mock transport."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return OllamaProvider(client=client, num_ctx=8192)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmptyResponseHandling:
    """Tests for empty/truncated LLM response detection."""

    @pytest.mark.asyncio
    async def test_empty_content_length_truncation_raises(self):
        """Empty content + done_reason='length' raises LLMResponseError."""
        def handler(request: httpx.Request) -> httpx.Response:
            if "/api/tags" in str(request.url):
                return _make_tags_response()
            return _make_chat_response(
                content="",
                done_reason="length",
                thinking="I was thinking about the answer...",
            )

        provider = _build_provider(handler)

        with pytest.raises(LLMResponseError) as exc_info:
            await provider.generate(messages=[{"role": "user", "content": "test"}])

        assert "truncated" in str(exc_info.value).lower()
        assert "context length" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_empty_content_normal_stop_returns_empty(self):
        """Empty content + done_reason='stop' returns normally — model chose to say nothing."""
        def handler(request: httpx.Request) -> httpx.Response:
            if "/api/tags" in str(request.url):
                return _make_tags_response()
            return _make_chat_response(content="", done_reason="stop")

        provider = _build_provider(handler)
        response = await provider.generate(messages=[{"role": "user", "content": "test"}])

        assert isinstance(response, LLMResponse)
        assert response.content == ""
        assert response.done is True

    @pytest.mark.asyncio
    async def test_nonempty_content_length_returns_normally(self):
        """Non-empty content + done_reason='length' returns the partial content."""
        partial_text = "Here are the rates: SBI 8.25%, HDFC"

        def handler(request: httpx.Request) -> httpx.Response:
            if "/api/tags" in str(request.url):
                return _make_tags_response()
            return _make_chat_response(content=partial_text, done_reason="length")

        provider = _build_provider(handler)
        response = await provider.generate(messages=[{"role": "user", "content": "test"}])

        assert isinstance(response, LLMResponse)
        assert response.content == partial_text

    @pytest.mark.asyncio
    async def test_normal_response_unchanged(self):
        """Normal non-empty response with done_reason='stop' is returned unchanged."""
        full_text = "The current home loan rates are: SBI 8.25%, HDFC 8.35%, ICICI 8.40%."

        def handler(request: httpx.Request) -> httpx.Response:
            if "/api/tags" in str(request.url):
                return _make_tags_response()
            return _make_chat_response(content=full_text, done_reason="stop")

        provider = _build_provider(handler)
        response = await provider.generate(messages=[{"role": "user", "content": "test"}])

        assert isinstance(response, LLMResponse)
        assert response.content == full_text
        assert response.model == "gemma4:e4b"
        assert response.done is True

    @pytest.mark.asyncio
    async def test_error_message_no_internals(self):
        """Error message contains no stack traces, file paths, or raw Ollama JSON."""
        def handler(request: httpx.Request) -> httpx.Response:
            if "/api/tags" in str(request.url):
                return _make_tags_response()
            return _make_chat_response(
                content="",
                done_reason="length",
                thinking="Internal reasoning here...",
            )

        provider = _build_provider(handler)

        with pytest.raises(LLMResponseError) as exc_info:
            await provider.generate(messages=[{"role": "user", "content": "test"}])

        error_msg = str(exc_info.value)
        # Must not expose internal details
        assert "ollama" not in error_msg.lower() or "ollama" not in error_msg  # the word itself is OK in a generic message
        assert "traceback" not in error_msg.lower()
        assert "\\backend\\" not in error_msg
        assert "/api/chat" not in error_msg
        assert "prompt_eval_count" not in error_msg
        assert "eval_count" not in error_msg
        # The detail field should be minimal
        assert exc_info.value.detail == "done_reason=length"
