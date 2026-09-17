"""Phase 5D — planner structured output, normalisation and repair retry."""
import asyncio
import json

import httpx
import pytest

from backend.llm.base import LLMProvider, LLMResponse
from backend.llm.ollama import OllamaProvider
from backend.planner import Planner
from backend.tools import CalculatorTool, FinanceCalcTool, TaxCalcTool, ToolRegistry


class QueueLLM(LLMProvider):
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    @property
    def default_model(self):
        return "m"

    def resolve_model(self, requested=None, available=None):
        return "m"

    async def generate(self, messages, model=None, options=None):
        self.calls.append({"messages": messages, "options": options})
        reply = self.replies.pop(0)
        return LLMResponse(content=reply if isinstance(reply, str) else json.dumps(reply), model="m")

    async def list_models(self):
        return ["m"]

    async def health_check(self):
        return {}


def planner_with(replies, repairs=1):
    registry = ToolRegistry()
    for tool in (CalculatorTool(), FinanceCalcTool(), TaxCalcTool()):
        registry.register(tool)
    llm = QueueLLM(replies)
    return Planner(llm_provider=llm, tool_registry=registry, max_repairs=repairs), llm


def plan(planner, message="What is the EMI on 20 lakh at 8.5% for 20 years?"):
    return asyncio.run(planner.plan(message=message))


def test_schema_constrains_tool_names():
    p, llm = planner_with([{"thought": "t", "requires_tools": False, "steps": []}])
    plan(p)
    schema = llm.calls[0]["options"]["format"]
    assert schema["properties"]["steps"]["items"]["properties"]["tool_name"]["enum"] == \
        ["calculator", "finance_calc", "tax_calc"]
    assert schema["properties"]["steps"]["maxItems"] == 3
    assert llm.calls[0]["options"]["temperature"] == 0.0


def test_flat_params_and_indian_number_strings_are_normalised():
    p, _ = planner_with([{
        "thought": "emi", "requires_tools": True,
        "steps": [{"tool": "finance_calc", "args": json.dumps(
            {"operation": "emi", "principal": "20 lakh", "annual_rate": "8.5%", "tenure_months": "240"})}],
    }])
    result = plan(p)
    assert result.requires_tools
    assert result.steps[0].arguments == {
        "operation": "emi", "params": {"principal": 2000000, "annual_rate": 8.5, "tenure_months": 240}}


def test_text_params_are_not_coerced():
    p, _ = planner_with([{"requires_tools": True, "steps": [{"tool_name": "tax_calc", "arguments": {
        "operation": "compute_tax", "params": {"gross_salary": "18,00,000", "tax_year": "2026-27", "regime": "new"}}}]}])
    result = plan(p, "tax on 18 lakh")
    assert result.steps[0].arguments["params"] == {"gross_salary": 1800000, "tax_year": "2026-27", "regime": "new"}


def test_single_step_object_is_wrapped():
    p, _ = planner_with([{"tool_name": "calculator", "arguments": {"expression": "2+2"}}])
    result = plan(p, "2+2?")
    assert result.requires_tools and result.steps[0].tool_name == "calculator"


def test_invalid_plan_is_repaired_once():
    p, llm = planner_with([
        {"requires_tools": True, "steps": [{"tool_name": "emi_calculator", "arguments": {}}]},
        {"requires_tools": True, "steps": [{"tool_name": "finance_calc", "arguments": {
            "operation": "emi", "params": {"principal": 2000000, "annual_rate": 8.5, "tenure_months": 240}}}]},
    ])
    result = plan(p)
    assert result.requires_tools and result.steps[0].tool_name == "finance_calc"
    assert "repaired" in result.thought
    repair_turn = llm.calls[1]["messages"][-1]["content"]
    assert "That plan was invalid" in repair_turn and "emi_calculator" in repair_turn


def test_non_json_is_repaired():
    p, _ = planner_with(["Sure! I will use the calculator.",
                         {"requires_tools": True, "steps": [{"tool_name": "calculator", "arguments": {"expression": "1+1"}}]}])
    assert plan(p, "1+1").requires_tools


def test_gives_up_after_repairs():
    p, llm = planner_with(["nope", "still nope"], repairs=1)
    result = plan(p, "1+1")
    assert not result.requires_tools and len(llm.calls) == 2
    assert "valid JSON" in result.thought


def test_repairs_can_be_disabled():
    p, llm = planner_with([{"requires_tools": True, "steps": [{"tool_name": "bogus", "arguments": {}}]}], repairs=0)
    result = plan(p)
    assert not result.requires_tools and len(llm.calls) == 1
    assert result.thought.startswith("Plan rejected")


def test_ollama_sends_format_field():
    bodies = []

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:e4b"}]})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "{}"}, "done": True})

    async def scenario():
        provider = OllamaProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await provider.generate([{"role": "user", "content": "x"}], options={"format": {"type": "object"}})
        await provider.generate([{"role": "user", "content": "y"}])

    asyncio.run(scenario())
    assert bodies[0]["format"] == {"type": "object"}
    assert "format" not in bodies[1]
    assert "format" not in bodies[0]["options"]


def test_llm_timeout_from_env(monkeypatch):
    monkeypatch.setenv("TORA_LLM_TIMEOUT_SECONDS", "600")
    assert OllamaProvider().timeout == 600.0


def test_wrong_parameter_names_trigger_a_repair():
    import asyncio
    import json
    from backend.llm.base import LLMProvider, LLMResponse
    from backend.planner import Planner
    from backend.tools import TaxCalcTool, ToolRegistry

    class TwoPlans(LLMProvider):
        def __init__(self):
            self.calls = []
            self.plans = [
                {"thought": "x", "requires_tools": True, "steps": [
                    {"tool_name": "tax_calc", "arguments": {"operation": "hra_exemption",
                                                            "params": {"basic": 50000, "hra": 20000, "rent": 25000}}}]},
                {"thought": "fixed", "requires_tools": True, "steps": [
                    {"tool_name": "tax_calc", "arguments": {"operation": "hra_exemption",
                                                            "params": {"basic_monthly": 50000, "hra_received_monthly": 20000,
                                                                       "rent_paid_monthly": 25000}}}]},
            ]

        @property
        def default_model(self):
            return "m"

        def resolve_model(self, requested=None, available=None):
            return "m"

        async def generate(self, messages, model=None, options=None):
            self.calls.append(messages)
            return LLMResponse(content=json.dumps(self.plans.pop(0)), model="m")

        async def list_models(self):
            return ["m"]

        async def health_check(self):
            return {}

    registry = ToolRegistry()
    registry.register(TaxCalcTool())
    llm = TwoPlans()
    plan = asyncio.run(Planner(llm_provider=llm, tool_registry=registry, max_repairs=1).plan("hra?"))
    assert plan.requires_tools and plan.steps[0].arguments["params"]["basic_monthly"] == 50000
    assert "Unknown parameter(s) for hra_exemption" in llm.calls[1][-1]["content"]
