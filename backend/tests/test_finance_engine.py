"""Phase 3B — deterministic financial engine and finance_calc tool."""
import asyncio

import pytest

from backend.context import ContextBuilder, ToolContext
from backend.finance import FinanceInputError, run_operation, inr
from backend.finance import engine as fe
from backend.tools import FinanceCalcTool, ToolRegistry, ToolExecutor
from backend.planner import Planner
from backend.agent.agent import ToraAgent
from backend.llm.base import LLMProvider, LLMResponse


class TestKnownValues:
    def test_emi_matches_bank_calculators(self):
        r = fe.emi(2000000, 8.5, 240)
        assert r["emi"] == pytest.approx(17356.46, abs=0.01)
        assert r["total_interest"] == pytest.approx(2165551.5, abs=1)

    def test_zero_rate_emi(self):
        assert fe.emi(120000, 0, 12)["emi"] == 10000

    def test_sip_future_value(self):
        r = fe.sip_future_value(10000, 12, 10)
        assert r["future_value"] == pytest.approx(2323390.76, abs=0.5)
        assert r["invested"] == 1200000

    def test_sip_step_up_increases_value(self):
        assert fe.sip_future_value(10000, 12, 10, 10)["future_value"] > fe.sip_future_value(10000, 12, 10)["future_value"]

    def test_required_sip_inverts_future_value(self):
        m = fe.required_sip(10000000, 12, 20)["monthly_sip"]
        assert fe.sip_future_value(m, 12, 20)["future_value"] == pytest.approx(10000000, rel=1e-5)

    def test_sip_change_impact(self):
        r = fe.sip_change_impact(10000, 5000, 12, 15)
        assert r["difference"] == pytest.approx(r["new_future_value"] - r["current_future_value"], abs=0.02)
        assert r["additional_invested"] == 900000

    def test_amortization_baseline_closes_on_time(self):
        r = fe.amortization(2000000, 8.5, 240)
        assert r["months_to_close"] == 240 and r["interest_saved"] == 0

    def test_prepayment_saves_interest(self):
        r = fe.amortization(2000000, 8.5, 240, extra_monthly=5000)
        assert r["months_to_close"] < 240 and r["interest_saved"] > 0
        assert sum(y["principal_paid"] for y in r["yearly_schedule"]) == pytest.approx(2000000, abs=5)

    def test_lump_sum_prepayment(self):
        r = fe.amortization(2000000, 8.5, 240, lump_sum=200000, lump_sum_month=12)
        assert r["months_saved"] > 0

    def test_compound_growth_quarterly(self):
        r = fe.lumpsum_future_value(100000, 7, 5, 4)
        assert r["future_value"] == pytest.approx(141478.0, abs=1)
        assert r["effective_annual_rate"] == pytest.approx(7.186, abs=0.001)

    def test_inflation(self):
        assert fe.inflation_adjust(100000, 6, 10)["value"] == pytest.approx(179084.77, abs=0.5)
        assert fe.inflation_adjust(179084.77, 6, 10, "present_value")["value"] == pytest.approx(100000, abs=0.5)

    def test_avalanche_beats_snowball_on_interest(self):
        debts = [
            {"name": "card", "balance": 155000, "apr": 42, "min_payment": 7750},
            {"name": "personal", "balance": 60000, "apr": 14, "min_payment": 3000},
        ]
        av = fe.debt_payoff([dict(d) for d in debts], 30000, "avalanche")
        sn = fe.debt_payoff([dict(d) for d in debts], 30000, "snowball")
        assert av["total_interest"] < sn["total_interest"]
        assert av["payoff_order"][0]["name"] == "card"
        assert sn["payoff_order"][0]["name"] == "personal"

    def test_health_metrics(self):
        assert fe.emergency_fund(40000, 6, 100000)["gap"] == 140000
        assert fe.savings_rate(100000, 70000)["savings_rate_percent"] == 30.0
        assert fe.debt_to_income(45000, 100000)["band"] == "stretched"
        assert fe.net_worth({"fd": 500000, "mf": 300000}, {"card": 150000})["net_worth"] == 650000

    def test_inr_grouping(self):
        assert inr(12345678.9) == "₹1,23,45,679"
        assert inr(100000) == "₹1,00,000"
        assert inr(-999) == "-₹999"


class TestValidation:
    @pytest.mark.parametrize("op, params", [
        ("emi", {"principal": -1, "annual_rate": 8, "tenure_months": 12}),
        ("emi", {"principal": 100000, "annual_rate": 80, "tenure_months": 12}),
        ("emi", {"principal": 100000, "annual_rate": 8, "tenure_months": 12.5}),
        ("emi", {"principal": 100000, "annual_rate": 8, "tenure_months": 1000}),
        ("emi", {"principal": "abc", "annual_rate": 8, "tenure_months": 12}),
        ("emi", {"principal": float("nan"), "annual_rate": 8, "tenure_months": 12}),
        ("emi", {"principal": 100000, "annual_rate": 8}),
        ("emi", {"principal": 100000, "annual_rate": 8, "tenure_months": 12, "hack": 1}),
        ("sip_future_value", {"monthly_investment": 1000, "annual_return": 12, "years": 80}),
        ("compound_growth", {"principal": 1000, "annual_return": 7, "years": 5, "compounding_per_year": 3}),
        ("debt_payoff", {"debts": [{"balance": 100000, "apr": 36, "min_payment": 5000}], "monthly_budget": 1000}),
        ("debt_payoff", {"debts": [{"balance": 100000, "apr": 60, "min_payment": 0}], "monthly_budget": 4000}),
        ("inflation_adjust", {"amount": 1, "inflation_rate": 5, "years": 1, "direction": "sideways"}),
        ("sip_change_impact", {"current_monthly": 5000, "change": -6000, "annual_return": 12, "years": 5}),
        ("teleport", {}),
    ])
    def test_invalid_inputs_raise(self, op, params):
        with pytest.raises(FinanceInputError):
            run_operation(op, params)

    def test_interest_only_payment_rejected_in_amortization(self):
        with pytest.raises(FinanceInputError):
            fe._simulate_loan(100000, 0.01, 500, 0, {})


class TestTool:
    def test_tool_success_and_errors_are_structured(self):
        tool = FinanceCalcTool()
        ok = asyncio.run(tool.run({"operation": "emi", "params": {"principal": 500000, "annual_rate": 10, "tenure_months": 60}}))
        assert ok.success and ok.data["emi"] == pytest.approx(10623.52, abs=0.01)
        bad = asyncio.run(tool.run({"operation": "emi", "params": {"principal": 500000}}))
        assert not bad.success and "Missing parameter" in bad.error
        bad_op = asyncio.run(tool.run({"operation": "rm -rf", "params": {}}))
        assert not bad_op.success

    def test_rendered_compactly_in_system_message(self):
        tool = FinanceCalcTool()
        res = asyncio.run(tool.run({"operation": "amortization", "params": {
            "principal": 2000000, "annual_rate": 8.5, "tenure_months": 240, "extra_monthly": 5000}}))
        ctx = ToolContext().add_result(tool_name="finance_calc", call_id="f", output=res.data)
        msgs = ContextBuilder(default_system_prompt="S").build(current_message="q", tool_context=ctx)
        system = msgs[0]["content"]
        assert "finance_calc' (amortization)" in system and "interest_saved=" in system
        assert "use these exact figures" in system
        assert len(msgs) == 2  # deterministic output: no external-data message
        assert system.count("closing_balance") <= 3


class _LLM(LLMProvider):
    def __init__(self):
        self.calls = []

    @property
    def default_model(self):
        return "m"

    def resolve_model(self, requested=None, available=None):
        return "m"

    async def generate(self, messages, model=None, options=None):
        self.calls.append(messages)
        if len(self.calls) == 1:
            return LLMResponse(model="m", content=(
                '{"thought": "sip what-if", "requires_tools": true, "steps": [{"tool_name": "finance_calc", '
                '"arguments": {"operation": "sip_change_impact", "params": {"current_monthly": 10000, '
                '"change": 5000, "annual_return": 12, "years": 15}}}]}'))
        return LLMResponse(model="m", content="done")

    async def list_models(self):
        return ["m"]

    async def health_check(self):
        return {}


def test_agent_what_if_runs_deterministic_engine(monkeypatch):
    # Tier 0 would answer this from the engine with no model call; this test is about what
    # the model does with the result, so it takes the model path deliberately.
    monkeypatch.setenv("TORA_DIRECT_ANSWER", "off")
    registry = ToolRegistry()
    registry.register(FinanceCalcTool())
    llm = _LLM()
    agent = ToraAgent(llm_provider=llm, planner=Planner(llm_provider=llm, tool_registry=registry),
                      tool_executor=ToolExecutor(registry=registry, default_timeout_seconds=5))
    resp = asyncio.run(agent.run("What happens if I increase my SIP of 10k by 5000 for 15 years at 12%?"))
    assert resp.intent.intent.value == "what_if"
    assert resp.tool_context.results[0].tool_name == "finance_calc"
    assert "₹25,22,880" in llm.calls[1][0]["content"]
