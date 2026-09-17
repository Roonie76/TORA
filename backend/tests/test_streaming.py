"""Streaming chat (SSE), progress events, provider token streaming and suggestions."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.agent.suggestions import suggest
from backend.llm.base import LLMResponse
from backend.llm.ollama import OllamaProvider
from backend.observability import progress
from backend.tests.test_sessions_api import ScriptedLLM


def parse_sse(text):
    events = []
    for block in text.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            events.append((name, data))
    return events


@pytest.fixture
def client(monkeypatch):
    fake = ScriptedLLM()
    monkeypatch.setattr(main_module.agent, "llm_provider", fake)
    monkeypatch.setattr(main_module.planner, "llm_provider", fake)
    monkeypatch.setenv("TORA_FAST_PATH", "on")
    return TestClient(main_module.app), fake


def test_stream_emits_stages_tool_tokens_and_final(client):
    c, _ = client
    r = c.post("/api/chat/stream", json={"message": "EMI on 30 lakh loan at 8.4% for 25 yrs"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    names = [n for n, _ in events]
    stages = [d["stage"] for n, d in events if n == "stage"]
    assert stages[0] == "understanding" and "calculating" in stages and "writing" in stages
    assert stages.index("calculating") < stages.index("writing")
    tools = [d for n, d in events if n == "tool"]
    assert [t["status"] for t in tools] == ["running", "done"]
    assert tools[1]["label"] == "Finance engine" and "₹23,955" in tools[1]["summary"]
    assert "token" in names and names[-1] == "final"
    final = events[-1][1]
    assert final["conversation_id"] and final["turn"] == 1 and final["complexity"] == "simple"
    assert final["tools"][0]["operation"] == "emi"
    assert "What if I prepay ₹5,000 every month?" in final["suggestions"]
    assert "".join(d["text"] for n, d in events if n == "token") == final["response"]
    # labels are human readable
    assert all(d.get("label") for n, d in events if n == "stage")


def test_stream_replace_event_when_grounding_rewrites(client, monkeypatch):
    c, fake = client
    answers = iter(["You will pay ₹99,999 a month.", "Your EMI is ₹23,955 a month."])

    async def generate(messages, model=None, options=None):
        if messages[0]["content"].startswith("You are TORA's Tool Planner"):
            return LLMResponse(content='{"thought":"x","requires_tools":false,"steps":[]}', model="fake")
        return LLMResponse(content=next(answers), model="fake")

    monkeypatch.setattr(fake, "generate", generate)
    events = parse_sse(c.post("/api/chat/stream", json={"message": "EMI on 30 lakh loan at 8.4% for 25 yrs"}).text)
    replace = [d for n, d in events if n == "replace"]
    assert replace and replace[0]["text"] == "Your EMI is ₹23,955 a month." and replace[0]["reason"] == "regenerated"
    assert events[-1][1]["response"] == "Your EMI is ₹23,955 a month."


def test_stream_errors_are_events(client):
    c, fake = client
    fake.fail_next_answer = True
    events = parse_sse(c.post("/api/chat/stream", json={"message": "hello"}).text)
    assert events[-1][0] == "error" and events[-1][1]["status"] == 503
    bad = c.post("/api/chat/stream", json={"message": "hi", "conversation_id": "x" * 32})
    assert parse_sse(bad.text)[-1][1]["status"] == 404
    assert c.post("/api/chat/stream", json={"message": "a" * 9000}).status_code == 422


def test_stream_continues_conversation_and_suggests(client):
    c, _ = client
    first = parse_sse(c.post("/api/chat/stream", json={"message": "My salary is 80k"}).text)[-1][1]
    assert first["suggestions"] == ["Check my financial health", "Help me plan my budget"]
    second = parse_sse(c.post("/api/chat/stream", json={"conversation_id": first["conversation_id"],
                                                        "message": "What is the 80C limit?"}).text)
    final = second[-1][1]
    assert final["turn"] == 2 and final["tools"][0]["label"] == "Rules library"
    assert "Section 123" in final["tools"][0]["summary"]
    assert any(d["stage"] == "reading_rules" for n, d in second if n == "stage")


def test_plain_chat_also_returns_extras(client):
    c, _ = client
    body = c.post("/api/chat", json={"message": "What is 20% of 60000?"}).json()
    assert body["tools"][0]["label"] == "Calculator" and body["suggestions"]


def test_progress_is_silent_without_a_sink():
    progress.emit("stage", stage="writing")  # no error, nothing to do
    assert not progress.active()


def test_ollama_streams_tokens():
    lines = [
        {"message": {"role": "assistant", "content": "", "thinking": "hmm"}, "done": False},
        {"message": {"role": "assistant", "content": "Your EMI "}, "done": False},
        {"message": {"role": "assistant", "content": "is ₹23,955."}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "eval_count": 7},
    ]
    seen = {}

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "m"}]})
        seen.update(json.loads(request.content))
        return httpx.Response(200, content="\n".join(json.dumps(l) for l in lines).encode())

    provider = OllamaProvider(host="http://o.test", default_model="m")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    chunks = []

    async def on_token(t):
        chunks.append(t)

    resp = asyncio.run(provider.generate([{"role": "user", "content": "hi"}], options={"on_token": on_token}))
    assert seen["stream"] is True and "on_token" not in seen["options"]
    assert chunks == ["Your EMI ", "is ₹23,955."] and resp.content == "Your EMI is ₹23,955."

    def failing(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "m"}]})
        return httpx.Response(404, json={"error": "model not found"})

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    from backend.llm.base import LLMResponseError
    with pytest.raises(LLMResponseError):
        asyncio.run(provider.generate([{"role": "user", "content": "hi"}], options={"on_token": on_token}))


def test_suggestions():
    assert suggest("calculation", [{"tool": "finance_calc", "operation": "debt_rescue_plan"}])[0].startswith("What if I pay")
    assert len(suggest("general_qa", [])) == 3
    assert suggest("research", [{"tool": "research", "operation": None}]) == ["Compare it with other banks",
                                                                             "Which option is cheapest for me?"]
