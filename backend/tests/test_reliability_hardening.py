"""
Re-audit regression tests: API input limits, rate limiting, CORS, request-scoped memory,
overflow-retry precision, model-list caching, tool timeout wiring and calculator overflow.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.agent.agent import ToraAgent
from backend.llm.base import LLMProvider, LLMResponse, LLMResponseError
from backend.llm.ollama import OllamaProvider
from backend.tools.calculator import safe_eval


@pytest.fixture
def client():
    return TestClient(main_module.app)


def _ok_response():
    return AsyncMock(return_value=type("R", (), {"content": "ok", "model": "m", "done": True})())


# ---------------------------------------------------------------------------
# API limits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        {"message": "a" * (main_module.MAX_MESSAGE_CHARS + 1)},
        {"message": "hi", "temperature": 5},
        {"message": "hi", "temperature": -1},
        {"message": "hi", "model": "gemma; rm -rf /"},
        {"message": "hi", "messages": [{"role": "user", "content": "x"}] * (main_module.MAX_HISTORY_ITEMS + 1)},
        {"message": "hi", "messages": [{"role": "user", "content": "x" * (main_module.MAX_MESSAGE_CHARS + 1)}]},
    ],
)
def test_oversized_or_invalid_requests_rejected_before_llm(client, payload):
    with patch.object(main_module.agent, "run", new_callable=AsyncMock) as run:
        r = client.post("/api/chat", json=payload)
    assert r.status_code == 422
    run.assert_not_called()


def test_rate_limit_returns_429(client):
    limiter = main_module.rate_limiter
    original = limiter.limit
    limiter.limit = 2
    try:
        with patch.object(main_module.agent, "run", _ok_response()):
            codes = [client.post("/api/chat", json={"message": "hi"}).status_code for _ in range(3)]
    finally:
        limiter.limit = original
    assert codes == [200, 200, 429]


def test_cors_rejects_unknown_origin(client):
    r = client.options(
        "/api/chat",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert r.headers.get("access-control-allow-origin") is None


def test_cors_allows_dev_origin_without_credentials(client):
    r = client.options(
        "/api/chat",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert r.headers.get("access-control-allow-credentials") is None


def test_tool_executor_has_default_timeout():
    assert main_module.tool_executor.default_timeout_seconds == main_module.TOOL_TIMEOUT_SECONDS
    assert main_module.TOOL_TIMEOUT_SECONDS > 0


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class _EchoProvider(LLMProvider):
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    @property
    def default_model(self):
        return "m"

    def resolve_model(self, requested=None, available=None):
        return "m"

    async def generate(self, messages, model=None, options=None):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return LLMResponse(content="ok", model="m", done=True)

    async def list_models(self):
        return ["m"]

    async def health_check(self):
        return {}


def test_agent_does_not_share_profile_between_calls_without_context():
    provider = _EchoProvider()
    agent = ToraAgent(llm_provider=provider)

    async def scenario():
        await agent.run("My salary is 95000 per month")
        await agent.run("Hello, what do you know about me?")

    asyncio.run(scenario())
    second_system = provider.calls[1][0]["content"]
    assert "95,000" not in second_system


def test_non_overflow_response_error_is_not_retried():
    provider = _EchoProvider(error=LLMResponseError(message="Invalid Content-Length header", status_code=400))
    agent = ToraAgent(llm_provider=provider)
    with pytest.raises(LLMResponseError):
        asyncio.run(agent.run("hello"))
    assert len(provider.calls) == 1


def test_context_length_error_from_ollama_is_retried_once():
    provider = _EchoProvider(
        error=LLMResponseError(message="Ollama error (500): input exceeds context length", status_code=500)
    )
    agent = ToraAgent(llm_provider=provider)
    with pytest.raises(LLMResponseError):
        asyncio.run(agent.run("hello"))
    assert len(provider.calls) == 2


# ---------------------------------------------------------------------------
# Ollama provider
# ---------------------------------------------------------------------------

def test_model_list_cached_across_generate_calls():
    tag_calls = []

    def handler(request: httpx.Request):
        if request.url.path == "/api/tags":
            tag_calls.append(1)
            return httpx.Response(200, json={"models": [{"name": "gemma4:e4b"}]})
        return httpx.Response(200, json={"message": {"content": "hi"}, "done": True, "done_reason": "stop"})

    async def scenario():
        provider = OllamaProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await provider.generate([{"role": "user", "content": "a"}])
        await provider.generate([{"role": "user", "content": "b"}])
        await provider.list_models()  # explicit listing is never cached

    asyncio.run(scenario())
    assert len(tag_calls) == 2


def test_empty_model_list_is_not_cached():
    responses = iter([
        httpx.Response(500),
        httpx.Response(200, json={"models": [{"name": "gemma4:e4b"}]}),
    ])

    def handler(request: httpx.Request):
        if request.url.path == "/api/tags":
            return next(responses)
        return httpx.Response(200, json={"message": {"content": "hi"}, "done": True})

    async def scenario():
        provider = OllamaProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await provider.generate([{"role": "user", "content": "a"}])
        assert provider._models_cache is None
        await provider.generate([{"role": "user", "content": "b"}])
        assert provider._models_cache == ["gemma4:e4b"]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("expr", ["999999**1000", "2**400", "(10**50)*(10**51)"])
def test_huge_integer_results_rejected_with_clear_error(expr):
    with pytest.raises(ValueError, match="maximum allowed magnitude"):
        safe_eval(expr)


def test_answer_token_cap_is_optional_and_forwarded(monkeypatch):
    import asyncio
    import httpx
    from backend.agent.agent import _max_answer_tokens
    from backend.llm.ollama import OllamaProvider

    monkeypatch.delenv("TORA_MAX_ANSWER_TOKENS", raising=False)
    assert _max_answer_tokens() == 0
    monkeypatch.setenv("TORA_MAX_ANSWER_TOKENS", "10")
    assert _max_answer_tokens() == 0  # nonsense-small values ignored
    monkeypatch.setenv("TORA_MAX_ANSWER_TOKENS", "600")
    assert _max_answer_tokens() == 600

    seen = {}

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "m"}]})
        seen.update(__import__("json").loads(request.content))
        return httpx.Response(200, json={"message": {"content": "Part one"}, "done": True, "done_reason": "length"})

    provider = OllamaProvider(host="http://ollama.test", default_model="m")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resp = asyncio.run(provider.generate([{"role": "user", "content": "hi"}], options={"num_predict": 600}))
    assert seen["options"]["num_predict"] == 600
    assert resp.content.startswith("Part one …") and "shortened" in resp.content
