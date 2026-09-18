"""Phase 4D — traces and metrics."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.llm.ollama import OllamaProvider
from backend.observability import TelemetryHub, TurnTrace, start_trace, current_trace
from backend.tests.test_sessions_api import ScriptedLLM, chat


@pytest.fixture
def llm(monkeypatch):
    fake = ScriptedLLM()
    monkeypatch.setattr(main_module.agent, "llm_provider", fake)
    monkeypatch.setattr(main_module.planner, "llm_provider", fake)
    return fake


@pytest.fixture
def client(monkeypatch):
    main_module.telemetry.reset()
    monkeypatch.setattr(main_module, "DEBUG_ENDPOINTS", True)
    return TestClient(main_module.app)


EMI_PLAN = {"thought": "emi", "requires_tools": True, "steps": [{"tool_name": "finance_calc", "arguments": {
    "operation": "emi", "params": {"principal": 2000000, "annual_rate": 8.5, "tenure_months": 240}}}]}


def test_trace_captures_pipeline_without_content(client, llm, monkeypatch):
    # Tier 0 would answer this from the engine with no model call; this test is about what
    # the model does with the result, so it takes the model path deliberately.
    monkeypatch.setenv("TORA_DIRECT_ANSWER", "off")
    llm.plans.append(EMI_PLAN)
    secret = "My salary is 1,23,456 per month. What is the EMI on a 20 lakh loan at 8.5% for 20 years?"
    cid = chat(client, secret).json()["conversation_id"]
    traces = client.get("/api/traces").json()["traces"]
    t = traces[-1]
    assert t["status"] == "ok" and t["http_status"] == 200 and t["mode"] == "stateful"
    assert t["intent"] == "calculation" and t["turn"] == 1
    assert t["planner"]["used"] and t["planner"]["steps"] == ["finance_calc"]
    assert t["tools"][0]["name"] == "finance_calc" and t["tools"][0]["ok"] and t["tools"][0]["ms"] >= 0
    assert t["prompt_tokens_estimate"] > 0
    assert t["grounding"]["action"] in ("none", "annotated", "regenerated", "regenerated+annotated")
    assert t["total_ms"] >= 0
    dumped = json.dumps(t)
    assert "123456" not in dumped and "1,23,456" not in dumped and "salary" not in dumped
    assert cid not in dumped and t["conversation_ref"] and len(t["conversation_ref"]) == 12


def test_metrics_aggregate(client, llm):
    cid = chat(client, "My rent is 20k").json()["conversation_id"]
    chat(client, "What is my rent?", cid)
    chat(client, "hi", "x" * 30)  # unknown conversation -> 404
    m = client.get("/api/metrics").json()
    assert m["requests"]["ok"] == 2 and m["requests"]["client_error"] == 1
    assert m["success_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert m["intents"]["memory_recall"] == 1
    assert m["planner_skipped"] >= 1
    assert m["latency_ms"]["samples"] == 2 and m["latency_ms"]["p50"] is not None


def test_llm_failure_counted_as_error(client, llm):
    llm.fail_next_answer = True
    assert chat(client, "hello").status_code == 503
    m = client.get("/api/metrics").json()
    assert m["requests"]["error"] == 1 and m["errors"]["http_503"] == 1


def test_rate_limit_counted(client, llm, monkeypatch):
    monkeypatch.setattr(main_module.rate_limiter, "limit", 1)
    chat(client, "a")
    chat(client, "b")
    assert client.get("/api/metrics").json()["requests"]["rate_limited"] == 1


def test_traces_endpoint_disabled_by_default(monkeypatch):
    monkeypatch.setattr(main_module, "DEBUG_ENDPOINTS", False)
    assert TestClient(main_module.app).get("/api/traces").status_code == 404


def test_trace_file_sink(tmp_path):
    path = tmp_path / "traces" / "t.jsonl"
    hub = TelemetryHub(trace_file=str(path))
    trace = start_trace(mode="stateless")
    trace.intent = "general_qa"
    hub.finish(trace)
    hub.finish(start_trace(), status="error", http_status=500, error_type="Boom")
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [l["status"] for l in lines] == ["ok", "error"]
    assert current_trace() is None


def test_ollama_provider_records_token_usage():
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:e4b"}]})
        return httpx.Response(200, json={"message": {"content": "hi"}, "done": True,
                                         "prompt_eval_count": 120, "eval_count": 30})

    import asyncio

    async def scenario():
        trace = start_trace()
        provider = OllamaProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await provider.generate([{"role": "user", "content": "x"}])
        await provider.generate([{"role": "user", "content": "y"}])
        return trace

    trace = asyncio.run(scenario())
    assert trace.llm["calls"] == 2
    assert trace.llm["prompt_tokens"] == 240 and trace.llm["completion_tokens"] == 60
    assert trace.llm["model"] == "gemma4:e4b"


def test_percentiles():
    hub = TelemetryHub()
    for ms in range(1, 101):
        t = TurnTrace()
        hub.finish(t)
        hub.latency[-1] = float(ms)
    snap = hub.snapshot()
    assert snap["latency_ms"]["p50"] in (50.0, 51.0) and snap["latency_ms"]["p95"] in (95.0, 96.0)
