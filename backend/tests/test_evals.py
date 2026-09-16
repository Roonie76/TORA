"""Phase 4A — the evaluation benchmark itself."""
import json

import httpx
import pytest

from backend.evals import EvalHarness, load_scenarios, run_suite, to_markdown
from backend.evals.__main__ import main as evals_main
from backend.llm.ollama import OllamaProvider


def test_offline_benchmark_passes_fully():
    report = run_suite(EvalHarness(mode="offline"), load_scenarios())
    failures = [(r["id"], t["index"], t["failed_checks"], t["error"])
                for r in report["results"] for t in r["turns"] if not t["passed"]]
    assert not failures, failures
    assert report["scenarios"] >= 20
    assert set(report["by_category"]) >= {"memory", "followup", "calculation", "tax", "safety", "grounding"}


def test_harness_detects_failures(tmp_path):
    bad = {"version": 1, "scenarios": [{
        "id": "must-fail", "category": "meta", "turns": [
            {"user": "My salary is 50k", "expect": {"profile": {"income": 99999.0}, "intent": "tax"}},
            {"user": "hello", "expect": {"tools": ["calculator"]}},
        ]}]}
    path = tmp_path / "s.json"
    path.write_text(json.dumps(bad))
    report = run_suite(EvalHarness(mode="offline"), load_scenarios(path))
    assert report["scenarios_passed"] == 0
    failed = {c["name"] for t in report["results"][0]["turns"] for c in t["failed_checks"]}
    assert {"profile:income", "intent", "tools"} <= failed
    md = to_markdown(report)
    assert "## Failures" in md and "must-fail" in md


def test_missing_fixture_is_reported_as_tool_error(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"version": 1, "scenarios": [{"id": "nofix", "category": "meta", "turns": [{
        "user": "Compare SBI and HDFC home loan rates",
        "plan": {"requires_tools": True, "steps": [{"tool_name": "research", "arguments": {"query": "x rates"}}]},
        "expect": {"tool_ok": {"research": False}}}]}]}))
    report = run_suite(EvalHarness(mode="offline"), load_scenarios(path))
    assert report["scenarios_passed"] == 1


def test_live_mode_with_mocked_ollama(tmp_path):
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:e4b"}]})
        body = json.loads(request.content)
        if body["messages"][0]["content"].startswith("You are TORA's Tool Planner"):
            content = json.dumps({"requires_tools": False, "steps": []})
        else:
            content = "Hello! I'm TORA."
        return httpx.Response(200, json={"message": {"content": content}, "done": True})

    provider = OllamaProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"version": 1, "scenarios": [{"id": "live", "category": "meta", "turns": [{
        "user": "Hi TORA", "expect": {"tools": [], "answer_contains": ["tora"], "answer_not_contains": ["system prompt"]}}]}]}))
    report = run_suite(EvalHarness(mode="live", provider=provider), load_scenarios(path))
    assert report["scenarios_passed"] == 1
    assert report["results"][0]["turns"][0]["answer"] == "Hello! I'm TORA."


def test_cli_writes_reports_and_gates(tmp_path):
    out, md = tmp_path / "r.json", tmp_path / "r.md"
    assert evals_main(["--mode", "offline", "--category", "tax", "--out", str(out), "--md", str(md),
                       "--min-pass-rate", "1.0"]) == 0
    assert json.loads(out.read_text())["by_category"] == {"tax": {"scenarios": 2, "passed": 2}}
    assert "TORA evaluation" in md.read_text()


def test_invalid_mode():
    with pytest.raises(ValueError):
        EvalHarness(mode="cloud")
