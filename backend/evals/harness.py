"""
TORA evaluation harness (Phase 4A).

Runs multi-turn scenarios through the real agent pipeline (memory, intent,
conversation state, planner, tools, context builder, grounding) and scores
explicit expectations per turn.

Modes
- offline: planner and answer LLM calls are scripted from the scenario, so the
  deterministic pipeline is checked exactly (runs in CI).
- live: a real Ollama model plans and answers; web tools still return scenario
  fixtures unless real_web=True. Use this to compare models and prompts.

Usage:
    python -m backend.evals --mode offline
    python -m backend.evals --mode live --model gemma4:e4b --out evals_live.json --md evals_live.md
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..agent.agent import ToraAgent
from ..auth import Identity, reset_current_identity, set_current_identity
from ..context.llm_extractor import EXTRACTOR_PREFIX
from ..context import ConversationContext, FinancialProfile, MessageRole
from ..llm.base import LLMProvider, LLMResponse
from ..planner import Planner
from ..state import ConversationState
from ..tools import (
    BaseTool,
    CalculatorTool,
    FinanceCalcTool,
    ResearchTool,
    SpendsyDataTool,
    TaxCalcTool,
    ToolExecutor,
    ToolRegistry,
    WebFetchTool,
    WebSearchTool,
)

SCENARIO_FILE = Path(__file__).with_name("scenarios.json")
PLANNER_PREFIX = "You are TORA's Tool Planner"


# --------------------------------------------------------------------------- stubs

class _NoopResearchProvider:
    async def multi_source_research(self, **kwargs):  # pragma: no cover - never used by fixtures
        raise RuntimeError("research fixture missing")


class FixtureTool(BaseTool):
    """Same name/schema as a real tool, but returns the scenario's fixture output."""

    def __init__(self, real: BaseTool, harness: "EvalHarness"):
        self.name = real.name
        self.description = real.description
        self.args_schema = real.args_schema
        self.metadata = real.metadata
        self._harness = harness
        super().__init__()

    async def execute(self, **kwargs: Any) -> Dict[str, Any]:
        fixtures = self._harness.current_fixtures
        if self.name not in fixtures:
            raise RuntimeError(f"No fixture for tool '{self.name}' in this turn.")
        return fixtures[self.name]


class ScriptedLLM(LLMProvider):
    def __init__(self, harness: "EvalHarness"):
        self._harness = harness

    @property
    def default_model(self) -> str:
        return "scripted"

    def resolve_model(self, requested=None, available=None):
        return "scripted"

    async def generate(self, messages, model=None, options=None) -> LLMResponse:
        turn = self._harness.current_turn
        self._harness.captured.append(messages)
        if messages and messages[0]["content"].startswith(PLANNER_PREFIX):
            plan = turn.get("plan") or {"thought": "no tools", "requires_tools": False, "steps": []}
            return LLMResponse(content=json.dumps(plan), model="scripted")
        if messages and messages[0]["content"].startswith(EXTRACTOR_PREFIX):
            self._harness.extraction_calls += 1
            return LLMResponse(content=json.dumps(turn.get("extraction") or {"facts": []}), model="scripted")
        answers = self._harness.pending_answers
        text = answers.pop(0) if answers else "OK."
        return LLMResponse(content=text, model="scripted")

    async def list_models(self):
        return ["scripted"]

    async def health_check(self):
        return {"connected": True}


# --------------------------------------------------------------------------- results

@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class TurnResult:
    index: int
    user: str
    checks: List[CheckResult] = field(default_factory=list)
    intent: Optional[str] = None
    tools: List[str] = field(default_factory=list)
    latency_ms: float = 0.0
    answer: str = ""
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)


@dataclass
class ScenarioResult:
    id: str
    category: str
    description: str
    turns: List[TurnResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(t.passed for t in self.turns)


# --------------------------------------------------------------------------- harness

class EvalHarness:
    def __init__(self, mode: str = "offline", provider: Optional[LLMProvider] = None,
                 real_web: bool = False, grounding_mode: str = "regenerate"):
        if mode not in ("offline", "live"):
            raise ValueError("mode must be 'offline' or 'live'")
        self.mode = mode
        self.real_web = real_web
        self.grounding_mode = grounding_mode
        self.current_turn: Dict[str, Any] = {}
        self.current_fixtures: Dict[str, Any] = {}
        self.pending_answers: List[str] = []
        self.captured: List[List[Dict[str, str]]] = []
        self.extraction_calls = 0
        if provider is None:
            if mode == "offline":
                provider = ScriptedLLM(self)
            else:
                from ..llm import OllamaProvider
                provider = OllamaProvider()
        self.provider = provider

    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        registry.register(FinanceCalcTool())
        registry.register(TaxCalcTool())
        # Spendsy records always come from fixtures (there is no live Spendsy service in evals).
        registry.register(FixtureTool(SpendsyDataTool(), self))
        web_tools = [WebSearchTool(), WebFetchTool(), ResearchTool(provider=_NoopResearchProvider())]
        for tool in web_tools:
            registry.register(tool if self.real_web and self.mode == "live" else FixtureTool(tool, self))
        return registry

    def run_scenario(self, scenario: Dict[str, Any]) -> ScenarioResult:
        import asyncio

        return asyncio.run(self._run_scenario(scenario))

    async def _run_scenario(self, scenario: Dict[str, Any]) -> ScenarioResult:
        previous_mode = os.environ.get("TORA_GROUNDING_MODE")
        os.environ["TORA_GROUNDING_MODE"] = self.grounding_mode
        try:
            return await self._run_scenario_inner(scenario)
        finally:
            if previous_mode is None:
                os.environ.pop("TORA_GROUNDING_MODE", None)
            else:
                os.environ["TORA_GROUNDING_MODE"] = previous_mode

    async def _run_scenario_inner(self, scenario: Dict[str, Any]) -> ScenarioResult:
        identity = Identity(user_id="eval-user", token="eval-token") if scenario.get("signed_in") else None
        token = set_current_identity(identity)
        previous_fast = os.environ.get("TORA_FAST_PATH")
        # Production default is on; scenarios that exercise the LLM planner itself opt out.
        os.environ["TORA_FAST_PATH"] = "off" if scenario.get("fast_path") is False else "on"
        try:
            return await self._run_turns(scenario)
        finally:
            reset_current_identity(token)
            if previous_fast is None:
                os.environ.pop("TORA_FAST_PATH", None)
            else:
                os.environ["TORA_FAST_PATH"] = previous_fast

    async def _run_turns(self, scenario: Dict[str, Any]) -> ScenarioResult:
        registry = self._registry()
        agent = ToraAgent(
            llm_provider=self.provider,
            planner=Planner(llm_provider=self.provider, tool_registry=registry),
            tool_executor=ToolExecutor(registry=registry, default_timeout_seconds=30),
        )
        profile = FinancialProfile()
        state = ConversationState()
        history: List[Dict[str, str]] = []
        result = ScenarioResult(scenario["id"], scenario.get("category", "general"), scenario.get("description", ""))

        for idx, turn in enumerate(scenario["turns"], start=1):
            self.current_turn = turn
            self.current_fixtures = turn.get("fixtures", {})
            answers = turn.get("answers") or ([turn["answer"]] if turn.get("answer") else [])
            self.pending_answers = list(answers)
            self.captured = []
            self.extraction_calls = 0
            tr = TurnResult(index=idx, user=turn["user"])
            started = time.monotonic()
            try:
                context = (ConversationContext.from_list(history, allowed_roles=MessageRole.valid_client_roles())
                           if history else None)
                response = await agent.run(
                    message=turn["user"], context=context, financial_context=profile,
                    conversation_state=state, model=turn.get("model"),
                )
            except Exception as exc:  # scored as a failed turn
                tr.error = f"{type(exc).__name__}: {exc}"
                tr.latency_ms = round((time.monotonic() - started) * 1000, 1)
                result.turns.append(tr)
                continue
            tr.latency_ms = round((time.monotonic() - started) * 1000, 1)
            tr.answer = response.content
            tr.intent = response.intent.intent.value if response.intent else None
            tr.tools = [r.tool_name for r in (response.tool_context.results if response.tool_context else [])]
            answer_msgs = [m for m in self.captured
                           if not m[0]["content"].startswith((PLANNER_PREFIX, EXTRACTOR_PREFIX))] \
                if self.mode == "offline" else []
            final_prompt = answer_msgs[0] if answer_msgs else None
            planner_called = any(m[0]["content"].startswith(PLANNER_PREFIX) for m in self.captured) \
                if self.mode == "offline" else (response.plan is not None)
            tr.checks = self._check(turn.get("expect", {}), response, profile, state, final_prompt, planner_called)
            if "model_extraction" in turn.get("expect", {}) and self.mode == "offline":
                want = turn["expect"]["model_extraction"]
                tr.checks.append(CheckResult("model_extraction", (self.extraction_calls > 0) == want,
                                             f"calls={self.extraction_calls}"))
            history.append({"role": "user", "content": turn["user"]})
            history.append({"role": "assistant", "content": response.content})
            result.turns.append(tr)
        return result

    # ------------------------------------------------------------------ checks
    def _check(self, expect, response, profile, state, prompt, planner_called) -> List[CheckResult]:
        checks: List[CheckResult] = []
        add = lambda name, ok, detail="": checks.append(CheckResult(name, bool(ok), detail))  # noqa: E731
        live = self.mode == "live"
        intent = response.intent.intent.value if response.intent else None
        tools = [r.tool_name for r in (response.tool_context.results if response.tool_context else [])]

        if "intent" in expect:
            allowed = expect["intent"] if isinstance(expect["intent"], list) else [expect["intent"]]
            add("intent", intent in allowed, f"got {intent}, expected {allowed}")
        if "followup" in expect:
            add("followup", response.intent.is_followup == expect["followup"], f"got {response.intent.is_followup}")
        if "resolved_contains" in expect:
            rq = response.intent.resolved_query or ""
            add("resolved_query", expect["resolved_contains"].lower() in rq.lower(), f"got {rq!r}")
        if "tools" in expect:
            add("tools", tools == expect["tools"], f"got {tools}, expected {expect['tools']}")
        if "tools_include" in expect:
            add("tools_include", all(t in tools for t in expect["tools_include"]), f"got {tools}")
        if "fast_path" in expect:
            got_fast = bool(response.plan is not None and (response.plan.thought or "").startswith("fast path"))
            add("fast_path", got_fast == expect["fast_path"], f"got {got_fast}")
        if "complexity" in expect:
            got_level = getattr(getattr(response, "complexity", None), "level", None)
            add("complexity", got_level == expect["complexity"], f"got {got_level}")
        if "planner_called" in expect:
            add("planner_called", planner_called == expect["planner_called"], f"got {planner_called}")
        if "tool_ok" in expect:
            oks = {r.tool_name: not r.is_error for r in (response.tool_context.results if response.tool_context else [])}
            for name, want in expect["tool_ok"].items():
                add(f"tool_ok:{name}", oks.get(name) == want, f"got {oks.get(name)}")
        if "tool_output" in expect and response.tool_context:
            outputs = {r.tool_name: r.output for r in response.tool_context.results}
            for path, want in expect["tool_output"].items():
                tool_name, *keys = path.split(".")
                value: Any = outputs.get(tool_name)
                for k in keys:
                    value = value.get(k) if isinstance(value, dict) else None
                ok = (abs(float(value) - float(want)) <= max(0.01, abs(float(want)) * 1e-4)
                      if isinstance(want, (int, float)) and isinstance(value, (int, float)) else value == want)
                add(f"tool_output:{path}", ok, f"got {value}, expected {want}")
        for name, want in (expect.get("profile") or {}).items():
            fact = profile.get_fact(name)
            got = None if fact is None or fact.status != "current" else fact.value
            add(f"profile:{name}", got == want, f"got {got}, expected {want}")
        for name, want in (expect.get("previous") or {}).items():
            fact = profile.get_fact(name)
            got = fact.get_previous_value() if fact else None
            add(f"previous:{name}", got == want, f"got {got}, expected {want}")
        for name, want in (expect.get("retracted") or {}).items():
            fact = profile.get_fact(name)
            got = fact.get_retracted_values() if fact else []
            add(f"retracted:{name}", got == want, f"got {got}")
        if "scenario_count" in expect:
            add("scenario_count", len(profile.scenarios) == expect["scenario_count"], f"got {len(profile.scenarios)}")
        if "topic" in expect:
            add("topic", state.active_topic_key == expect["topic"], f"got {state.active_topic_key}")
        if prompt is not None:
            system = prompt[0]["content"]
            external = "\n".join(m["content"] for m in prompt if m["content"].startswith("<external_data"))
            for s in expect.get("system_contains", []):
                add(f"system_contains:{s[:30]}", s in system)
            for s in expect.get("system_not_contains", []):
                add(f"system_not_contains:{s[:30]}", s not in system)
            for s in expect.get("external_contains", []):
                add(f"external_contains:{s[:30]}", s in external)
            for s in expect.get("prompt_not_contains", []):
                add(f"prompt_not_contains:{s[:30]}", all(s not in m["content"] for m in prompt))
        elif not live and any(k in expect for k in ("system_contains", "external_contains")):
            add("prompt_captured", False, "no answer prompt captured")
        if "grounding_action" in expect:
            got = (response.grounding or {}).get("action")
            allowed = expect["grounding_action"] if isinstance(expect["grounding_action"], list) else [expect["grounding_action"]]
            add("grounding_action", got in allowed, f"got {got}")
        if live or expect.get("check_answer_offline"):
            for s in expect.get("answer_contains", []):
                add(f"answer_contains:{s[:30]}", s.lower() in response.content.lower())
            for s in expect.get("answer_not_contains", []):
                add(f"answer_not_contains:{s[:30]}", s.lower() not in response.content.lower())
            for pattern in expect.get("answer_matches", []):
                add(f"answer_matches:{pattern[:30]}", re.search(pattern, response.content, re.I) is not None)
        return checks


# --------------------------------------------------------------------------- suite

def load_scenarios(path: Optional[Path] = None, category: Optional[str] = None) -> List[Dict[str, Any]]:
    data = json.loads(Path(path or SCENARIO_FILE).read_text(encoding="utf-8"))
    scenarios = data["scenarios"]
    if category:
        scenarios = [s for s in scenarios if s.get("category") == category]
    return scenarios


def run_suite(harness: EvalHarness, scenarios: List[Dict[str, Any]]) -> Dict[str, Any]:
    results = [harness.run_scenario(s) for s in scenarios]
    by_cat: Dict[str, Dict[str, int]] = {}
    checks_total = checks_passed = 0
    latencies: List[float] = []
    for r in results:
        cat = by_cat.setdefault(r.category, {"scenarios": 0, "passed": 0})
        cat["scenarios"] += 1
        cat["passed"] += int(r.passed)
        for t in r.turns:
            latencies.append(t.latency_ms)
            checks_total += len(t.checks) + (1 if t.error else 0)
            checks_passed += sum(c.passed for c in t.checks)
    passed = sum(r.passed for r in results)
    return {
        "mode": harness.mode,
        "model": getattr(harness.provider, "default_model", None),
        "scenarios": len(results),
        "scenarios_passed": passed,
        "scenario_pass_rate": round(passed / len(results), 4) if results else None,
        "checks": checks_total,
        "checks_passed": checks_passed,
        "check_pass_rate": round(checks_passed / checks_total, 4) if checks_total else None,
        "latency_ms": {"mean": round(sum(latencies) / len(latencies), 1) if latencies else None,
                       "max": max(latencies) if latencies else None},
        "by_category": by_cat,
        "results": [
            {
                "id": r.id, "category": r.category, "passed": r.passed, "description": r.description,
                "turns": [
                    {"index": t.index, "user": t.user, "passed": t.passed, "intent": t.intent, "tools": t.tools,
                     "latency_ms": t.latency_ms, "error": t.error,
                     "failed_checks": [{"name": c.name, "detail": c.detail} for c in t.checks if not c.passed],
                     "answer": t.answer if harness.mode == "live" else None}
                    for t in r.turns
                ],
            }
            for r in results
        ],
    }


def to_markdown(report: Dict[str, Any]) -> str:
    lines = [
        f"# TORA evaluation — {report['mode']} ({report.get('model')})",
        "",
        f"Scenarios: **{report['scenarios_passed']}/{report['scenarios']}** passed · "
        f"checks: **{report['checks_passed']}/{report['checks']}** · "
        f"mean turn latency {report['latency_ms']['mean']} ms",
        "",
        "| Category | Passed |",
        "|---|---|",
    ]
    for cat, v in sorted(report["by_category"].items()):
        lines.append(f"| {cat} | {v['passed']}/{v['scenarios']} |")
    failures = [r for r in report["results"] if not r["passed"]]
    if failures:
        lines += ["", "## Failures", ""]
        for r in failures:
            lines.append(f"### {r['id']} ({r['category']})")
            for t in r["turns"]:
                if t["passed"]:
                    continue
                lines.append(f"- Turn {t['index']} — “{t['user']}” (intent {t['intent']}, tools {t['tools']})")
                if t["error"]:
                    lines.append(f"  - error: {t['error']}")
                for c in t["failed_checks"]:
                    lines.append(f"  - {c['name']}: {c['detail']}")
    return "\n".join(lines) + "\n"
