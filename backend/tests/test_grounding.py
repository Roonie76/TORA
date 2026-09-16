"""Phase 4B — numeric grounding verifier and agent integration."""
import asyncio

import pytest

from backend.agent.agent import ToraAgent
from backend.context import FactExtractor, FactManager, FinancialProfile, ToolContext
from backend.llm.base import LLMProvider, LLMResponse
from backend.planner import Planner
from backend.tools import FinanceCalcTool, ToolExecutor, ToolRegistry
from backend.verify import build_evidence, extract_claims, verify_answer


def profile_of(*messages):
    p = FinancialProfile()
    for m in messages:
        FactManager.apply_candidates(p, FactExtractor.extract_candidate_facts(m, profile=p))
    return p


class TestExtraction:
    def test_money_units_and_abbreviations(self):
        claims = extract_claims("Pay Rs. 5,000 now. Your corpus is ₹1.2 crore and EMI ₹17,356. Rate 8.5% p.a. here.")
        got = {(c.kind, c.value) for c in claims}
        assert ("money", 5000) in got and ("money", 12000000) in got and ("money", 17356) in got
        assert ("percent", 8.5) in got

    def test_small_amounts_ignored(self):
        assert extract_claims("It costs ₹50.") == []

    def test_labelled_examples(self):
        claims = extract_claims("For example, a ₹10 lakh loan at 9% has a higher EMI.")
        assert all(c.labelled for c in claims)

    def test_percent_not_double_counted_inside_money(self):
        assert [c.kind for c in extract_claims("₹5,000 is 10% of it")] == ["money", "percent"]


class TestVerification:
    def test_profile_and_derived_figures_supported(self):
        p = profile_of("My salary is 80,000 per month", "My rent is 20k")
        ev = build_evidence(user_texts=["How much do I have left?"], profile=p)
        r = verify_answer("After rent of ₹20,000 you keep ₹60,000 a month, i.e. ₹9,60,000 of salary a year "
                          "(₹10 lakh combined is not right).", ev)
        unsupported = {c.text for c in r.unsupported}
        assert unsupported == {"₹10 lakh"}

    def test_tool_output_rounding_supported(self):
        ctx = ToolContext().add_result(tool_name="finance_calc", call_id="f",
                                       output={"emi": 17356.46, "total_interest": 2165551.52})
        ev = build_evidence(user_texts=["EMI for 20 lakh?"], tool_context=ctx)
        r = verify_answer("EMI is ₹17,356; interest about ₹21.66 lakh on ₹20 lakh.", ev)
        assert r.ok and r.checked == 3

    def test_hallucinated_amount_flagged(self):
        ev = build_evidence(user_texts=["What's my EMI?"])
        r = verify_answer("Your EMI will be ₹23,450 per month.", ev)
        assert not r.ok and r.unsupported[0].value == 23450

    def test_percentages_soft_unless_strict(self):
        ctx = ToolContext().add_result(tool_name="research", call_id="r", output={
            "conclusions": [{"synthesized_statement": "SBI home loan starting from 7.25% p.a."}]})
        ev = build_evidence(user_texts=["compare rates"], tool_context=ctx)
        answer = "SBI starts at 7.25% while HDFC is 8.1%."
        assert verify_answer(answer, ev).ok
        strict = verify_answer(answer, ev, strict_percentages=True)
        assert [c.value for c in strict.unsupported] == [8.1]

    def test_failed_tool_output_is_not_evidence(self):
        ctx = ToolContext().add_result(tool_name="finance_calc", call_id="f", output="error 99999", is_error=True)
        ev = build_evidence(user_texts=["x"], tool_context=ctx)
        assert not verify_answer("It is ₹99,999.", ev).ok


class GroundingLLM(LLMProvider):
    def __init__(self, answers, plan=None):
        self.answers = list(answers)
        self.plan = plan
        self.calls = []

    @property
    def default_model(self):
        return "m"

    def resolve_model(self, requested=None, available=None):
        return "m"

    async def generate(self, messages, model=None, options=None):
        self.calls.append(messages)
        if messages[0]["content"].startswith("You are TORA's Tool Planner"):
            return LLMResponse(content=self.plan or '{"requires_tools": false, "steps": []}', model="m")
        return LLMResponse(content=self.answers.pop(0), model="m")

    async def list_models(self):
        return ["m"]

    async def health_check(self):
        return {}


def make_agent(llm):
    registry = ToolRegistry()
    registry.register(FinanceCalcTool())
    return ToraAgent(llm_provider=llm, planner=Planner(llm_provider=llm, tool_registry=registry),
                     tool_executor=ToolExecutor(registry=registry, default_timeout_seconds=5))


EMI_PLAN = ('{"requires_tools": true, "steps": [{"tool_name": "finance_calc", "arguments": {"operation": "emi", '
            '"params": {"principal": 2000000, "annual_rate": 8.5, "tenure_months": 240}}}]}')


def test_grounded_answer_passes_untouched(monkeypatch):
    monkeypatch.setenv("TORA_GROUNDING_MODE", "regenerate")
    llm = GroundingLLM(["Your EMI is ₹17,356 per month for the ₹20 lakh loan."], plan=EMI_PLAN)
    r = asyncio.run(make_agent(llm).run("What is the EMI for a 20 lakh loan at 8.5% for 20 years?"))
    assert r.grounding["ok"] and r.grounding["action"] == "none"
    assert r.content == "Your EMI is ₹17,356 per month for the ₹20 lakh loan."


def test_hallucination_triggers_one_regeneration(monkeypatch):
    monkeypatch.setenv("TORA_GROUNDING_MODE", "regenerate")
    llm = GroundingLLM(["Your EMI is ₹18,900 per month.", "Your EMI is ₹17,356 per month."], plan=EMI_PLAN)
    r = asyncio.run(make_agent(llm).run("What is the EMI for a 20 lakh loan at 8.5% for 20 years?"))
    assert r.content == "Your EMI is ₹17,356 per month."
    assert r.grounding["action"] == "regenerated" and r.grounding["ok"]
    retry_system = llm.calls[-1][0]["content"]
    assert "## Grounding Correction" in retry_system and "₹18,900" in retry_system


def test_persistent_hallucination_is_annotated(monkeypatch):
    monkeypatch.setenv("TORA_GROUNDING_MODE", "regenerate")
    llm = GroundingLLM(["EMI ₹18,900.", "EMI ₹19,100."], plan=EMI_PLAN)
    r = asyncio.run(make_agent(llm).run("What is the EMI for a 20 lakh loan at 8.5% for 20 years?"))
    assert r.grounding["action"] == "regenerated+annotated"
    assert r.content.startswith("EMI ₹19,100.") and "could not be verified" in r.content


def test_annotate_mode_does_not_regenerate(monkeypatch):
    monkeypatch.setenv("TORA_GROUNDING_MODE", "annotate")
    llm = GroundingLLM(["Save ₹45,000 every month."])
    r = asyncio.run(make_agent(llm).run("How much should I save?"))
    answer_calls = [c for c in llm.calls if not c[0]["content"].startswith("You are TORA's Tool Planner")]
    assert len(answer_calls) == 1
    assert r.grounding["action"] == "annotated" and "₹45,000" in r.content.split("Note:")[1]


def test_off_mode(monkeypatch):
    monkeypatch.setenv("TORA_GROUNDING_MODE", "off")
    llm = GroundingLLM(["Save ₹45,000 every month."])
    r = asyncio.run(make_agent(llm).run("How much should I save?"))
    assert r.grounding is None and r.content == "Save ₹45,000 every month."


def test_labelled_illustration_is_allowed(monkeypatch):
    monkeypatch.setenv("TORA_GROUNDING_MODE", "regenerate")
    llm = GroundingLLM(["For example, a ₹10 lakh loan over 5 years costs more interest than a 3-year one."])
    r = asyncio.run(make_agent(llm).run("Does a longer tenure cost more interest?"))
    assert r.grounding["ok"] and r.grounding["labelled_assumptions"]
