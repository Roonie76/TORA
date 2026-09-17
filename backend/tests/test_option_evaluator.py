"""Phase 9: CA-style option evaluation."""
import asyncio

import pytest

from backend.context import ContextBuilder, ToolContext
from backend.finance.engine import FinanceInputError, run_operation
from backend.tools import FinanceCalcTool

HOME = {"loan_balance": 3000000, "loan_rate": 8.5, "remaining_months": 180, "amount": 10000}


def names(result):
    return {o["option"]: o for o in result["options"]}


def test_prepay_vs_invest_prefers_investing_when_returns_clearly_higher():
    r = run_operation("prepay_vs_invest", HOME)
    o = names(r)
    assert set(o) == {"prepay", "invest", "split_50_50"}
    assert o["prepay"]["loan_closes_in_months"] < 180 == o["invest"]["loan_closes_in_months"]
    assert o["prepay"]["loan_interest_paid"] < o["split_50_50"]["loan_interest_paid"] < o["invest"]["loan_interest_paid"]
    assert r["recommended"] == "invest"
    assert 8.5 < r["breakeven_return"] < 12
    assert r["what_to_confirm"]


def test_prepay_wins_for_expensive_loans():
    r = run_operation("prepay_vs_invest", {"loan_balance": 500000, "loan_rate": 16, "remaining_months": 36,
                                           "amount": 5000, "expected_return": 11})
    assert r["recommended"] == "prepay"
    assert names(r)["prepay"]["net_worth_at_loan_end"] > names(r)["invest"]["net_worth_at_loan_end"]


def test_close_call_prefers_safety_and_no_emergency_fund_keeps_liquidity():
    close = run_operation("prepay_vs_invest", {**HOME, "expected_return": 10})
    assert close["confidence"] == "low" and close["recommended"] in ("prepay", "split_50_50")
    no_buffer = run_operation("prepay_vs_invest", {**HOME, "emergency_fund_ok": False})
    assert no_buffer["recommended"] == "build_emergency_fund_first"


def test_breakeven_is_consistent():
    r = run_operation("prepay_vs_invest", HOME)
    at = run_operation("prepay_vs_invest", {**HOME, "expected_return": r["breakeven_return"]})
    assert abs(at["invest_minus_prepay"]) < 2000


def test_home_loan_tax_benefit_lowers_effective_rate():
    r = run_operation("prepay_vs_invest", {**HOME, "loan_tax_benefit_rate": 30})
    assert r["effective_loan_rate"] == pytest.approx(5.95)


def test_rent_vs_buy():
    r = run_operation("rent_vs_buy", {"property_price": 8000000, "monthly_rent": 25000, "loan_rate": 8.5, "years": 20})
    assert r["upfront_cash_needed"] == 2160000
    assert r["recommended"] == "rent_and_invest"
    assert r["breakeven_appreciation"] > 5
    hot = run_operation("rent_vs_buy", {"property_price": 8000000, "monthly_rent": 25000, "loan_rate": 8.5,
                                        "years": 20, "appreciation": r["breakeven_appreciation"] + 2})
    assert hot["recommended"] == "buy"
    cheap = run_operation("rent_vs_buy", {"property_price": 4000000, "monthly_rent": 30000, "loan_rate": 8.5})
    assert cheap["recommended"] == "buy"


def test_loan_tenure_choice():
    r = run_operation("loan_tenure_choice", {"principal": 5000000, "loan_rate": 8.5, "short_years": 15,
                                             "long_years": 25})
    o = names(r)
    assert o["shorter_tenure"]["emi"] > o["longer_tenure_and_invest"]["emi"]
    assert o["shorter_tenure"]["total_interest"] < o["longer_tenure_and_invest"]["total_interest"]
    assert r["recommended"] == "shorter_tenure"   # thin return spread -> safer choice
    rich = run_operation("loan_tenure_choice", {"principal": 5000000, "loan_rate": 7, "short_years": 10,
                                                "long_years": 25, "investment_return": 14})
    assert rich["recommended"] == "longer_tenure_and_invest"
    with pytest.raises(FinanceInputError):
        run_operation("loan_tenure_choice", {"principal": 1, "loan_rate": 8, "short_years": 20, "long_years": 10})


def test_health_check_orders_priorities():
    r = run_operation("financial_health_check", {"monthly_income": 80000, "monthly_expenses": 40000,
                                                 "emergency_savings": 50000, "monthly_emis": 15000,
                                                 "high_interest_debt": 100000, "monthly_investments": 5000,
                                                 "dependents": 2})
    assert r["grade"] == "needs attention"
    assert r["priorities"][0].startswith("Build the emergency fund to ₹3,30,000")
    assert "high-interest debt" in r["priorities"][1]
    healthy = run_operation("financial_health_check", {"monthly_income": 150000, "monthly_expenses": 50000,
                                                       "emergency_savings": 400000, "monthly_investments": 40000,
                                                       "life_cover": 20000000, "health_cover": 1000000,
                                                       "dependents": 1})
    assert healthy["grade"] == "strong" and healthy["priorities"] == []


def test_tool_and_rendering():
    res = asyncio.run(FinanceCalcTool().run({"operation": "prepay_vs_invest", "params": HOME}))
    assert res.success
    ctx = ToolContext().add_result(tool_name="finance_calc", call_id="a", output=res.data)
    system = ContextBuilder(default_system_prompt="S").build(current_message="q", tool_context=ctx)[0]["content"]
    assert "recommended: invest" in system and "what_to_confirm" in system and "breakeven_return" in system


def test_prompts_mention_evaluators():
    from backend.prompts.planner import get_planner_system_prompt
    from backend.prompts.tora import TORA_SYSTEM_PROMPT
    assert "prepay_vs_invest" in get_planner_system_prompt([])
    assert "Comparing Options" in TORA_SYSTEM_PROMPT
