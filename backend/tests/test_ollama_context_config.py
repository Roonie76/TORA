"""
Tests for OllamaProvider configurable context window size (num_ctx).

Verifies default value (8192), TORA_LLM_NUM_CTX environment variable override,
constructor parameter override, generated Ollama payload contents,
invalid configuration handling, and caller options override.

All tests mock Ollama HTTP responses — no live server dependency.
"""

import os
import json
import pytest
import httpx

from unittest.mock import patch, AsyncMock
from backend.llm.ollama import OllamaProvider, _resolve_num_ctx, DEFAULT_NUM_CTX, MIN_NUM_CTX


# ---------------------------------------------------------------------------
# _resolve_num_ctx unit tests
# ---------------------------------------------------------------------------

class TestResolveNumCtx:
    """Tests for the _resolve_num_ctx helper function."""

    def test_default_num_ctx_is_8192(self):
        """Default context size is 8192 when no parameter or env var is set."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove TORA_LLM_NUM_CTX if present
            os.environ.pop("TORA_LLM_NUM_CTX", None)
            assert _resolve_num_ctx() == 8192

    def test_env_var_override(self):
        """TORA_LLM_NUM_CTX environment variable overrides the default."""
        with patch.dict(os.environ, {"TORA_LLM_NUM_CTX": "16384"}):
            assert _resolve_num_ctx() == 16384

    def test_constructor_override_takes_precedence(self):
        """Explicit num_ctx parameter overrides env and default."""
        with patch.dict(os.environ, {"TORA_LLM_NUM_CTX": "16384"}):
            assert _resolve_num_ctx(explicit=4096) == 4096

    def test_invalid_env_var_uses_default(self):
        """Non-integer TORA_LLM_NUM_CTX falls back to 8192."""
        with patch.dict(os.environ, {"TORA_LLM_NUM_CTX": "not_a_number"}):
            assert _resolve_num_ctx() == DEFAULT_NUM_CTX

    def test_below_minimum_env_var_uses_default(self):
        """TORA_LLM_NUM_CTX below MIN_NUM_CTX falls back to default."""
        with patch.dict(os.environ, {"TORA_LLM_NUM_CTX": "100"}):
            assert _resolve_num_ctx() == DEFAULT_NUM_CTX

    def test_below_minimum_explicit_uses_default(self):
        """Explicit num_ctx below MIN_NUM_CTX falls back to default."""
        assert _resolve_num_ctx(explicit=256) == DEFAULT_NUM_CTX

    def test_empty_env_var_uses_default(self):
        """Empty TORA_LLM_NUM_CTX string uses the default."""
        with patch.dict(os.environ, {"TORA_LLM_NUM_CTX": ""}):
            assert _resolve_num_ctx() == DEFAULT_NUM_CTX


# ---------------------------------------------------------------------------
# OllamaProvider integration tests
# ---------------------------------------------------------------------------

def _make_ollama_response(content: str = "Hello", done_reason: str = "stop") -> httpx.Response:
    """Create a mock Ollama /api/chat response."""
    body = {
        "model": "gemma4:e4b",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": done_reason,
    }
    return httpx.Response(status_code=200, json=body)


def _make_tags_response(models=None) -> httpx.Response:
    """Create a mock Ollama /api/tags response."""
    if models is None:
        models = [{"name": "gemma4:e4b"}]
    return httpx.Response(status_code=200, json={"models": models})


class TestOllamaProviderNumCtx:
    """Tests for OllamaProvider's configurable num_ctx."""

    def test_provider_default_num_ctx(self):
        """OllamaProvider defaults to 8192 num_ctx."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TORA_LLM_NUM_CTX", None)
            provider = OllamaProvider()
            assert provider.num_ctx == 8192

    def test_provider_constructor_override(self):
        """OllamaProvider accepts num_ctx in constructor."""
        provider = OllamaProvider(num_ctx=4096)
        assert provider.num_ctx == 4096

    def test_provider_env_override(self):
        """OllamaProvider reads TORA_LLM_NUM_CTX from env."""
        with patch.dict(os.environ, {"TORA_LLM_NUM_CTX": "16384"}):
            provider = OllamaProvider()
            assert provider.num_ctx == 16384

    @pytest.mark.asyncio
    async def test_payload_contains_configured_num_ctx(self):
        """Generated Ollama payload uses the configured num_ctx value."""
        captured_payload = {}

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/api/tags" in url:
                return _make_tags_response()
            if "/api/chat" in url:
                captured_payload.update(json.loads(request.content))
                return _make_ollama_response()
            return httpx.Response(status_code=404)

        transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=transport)

        provider = OllamaProvider(client=client, num_ctx=4096)
        await provider.generate(messages=[{"role": "user", "content": "hello"}])

        assert captured_payload["options"]["num_ctx"] == 4096

    @pytest.mark.asyncio
    async def test_options_override_still_works(self):
        """Caller options={"num_ctx": X} still overrides the provider default."""
        captured_payload = {}

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/api/tags" in url:
                return _make_tags_response()
            if "/api/chat" in url:
                captured_payload.update(json.loads(request.content))
                return _make_ollama_response()
            return httpx.Response(status_code=404)

        transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=transport)

        provider = OllamaProvider(client=client, num_ctx=4096)
        await provider.generate(
            messages=[{"role": "user", "content": "hello"}],
            options={"num_ctx": 32768},
        )

        assert captured_payload["options"]["num_ctx"] == 32768

    @pytest.mark.asyncio
    async def test_default_payload_uses_8192(self):
        """With no configuration, the default payload num_ctx is 8192."""
        captured_payload = {}

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/api/tags" in url:
                return _make_tags_response()
            if "/api/chat" in url:
                captured_payload.update(json.loads(request.content))
                return _make_ollama_response()
            return httpx.Response(status_code=404)

        transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=transport)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TORA_LLM_NUM_CTX", None)
            provider = OllamaProvider(client=client)
            await provider.generate(messages=[{"role": "user", "content": "hello"}])

        assert captured_payload["options"]["num_ctx"] == 8192
