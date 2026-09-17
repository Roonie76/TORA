"""Phase 8: debt-rescue calculations and routing."""
import asyncio

import pytest

from backend.finance.engine import FinanceInputError, OPERATIONS, run_operation
from backend.state.intent import Intent, IntentClassifier
from backend.tools import FinanceCalcTool

DEBTS = [
    {"name": "credit card", "balance": 155000, "apr": 42, "min_payment": 7750},
    {"name": "personal loan", "balance": 300000, "apr": 14, "min_payment": 10000},
    {"name": "car loan", "balance": 400000, "apr": 9, "min_payment": 9000},
]


def test_operations_registered_and_exposed():
    for op in ("debt_snapshot", "debt_rescue_plan", "consolidation_check", "minimum_due_trap"):
        assert op in OPERATIONS
        assert op in FinanceCalcTool().description


def test_snapshot():
    s = run_operation("debt_snapshot", {"debts": DEBTS, "monthly_income": 90000})
    assert s["total_debt"] == 855000 and s["total_minimum_payments"] == 26750
    assert s["monthly_interest_cost"] == pytest.approx(5425 + 3500 + 3000)
    assert s["debt_to_income_pct"] == 29.7 and s["stress_level"] == "manageable"
    assert s["costliest_debt"] == "credit card"
    assert any("42% a year" in w for w in s["warnings"])


def test_snapshot_flags_minimum_below_interest_and_stress():
    s = run_operation("debt_snapshot", {"debts": [{"name": "app loan", "balance": 50000, "apr": 60, "min_payment": 2000}],
                                        "monthly_income": 3500})
    assert any("does not even cover" in w for w in s["warnings"])
    assert s["stress_level"] == "critical"   # 2000 / 3500 = 57%

def test_stress_bands():
    from backend.finance.debt import _stress
    assert [_stress(v) for v in (20, 35, 45, 60)] == ["manageable", "stretched", "high", "critical"]


def test_rescue_plan_workable():
    r = run_operation("debt_rescue_plan", {"debts": DEBTS, "monthly_income": 90000, "essential_expenses": 45000,
                                           "current_savings": 80000})
    assert r["status"] == "workable" and r["monthly_budget_for_debt"] == 45000
    assert r["recommended_strategy"] == "avalanche"
    assert r["plan"]["months_to_debt_free"] == 22
    assert r["plan"]["payoff_order"][0]["name"] == "credit card"
    mins = r["comparison"]["minimums_only"]
    assert mins["months"] > r["plan"]["months_to_debt_free"] and mins["total_interest"] > r["plan"]["total_interest"]
    assert [w["extra_per_month"] for w in r["what_if_extra"]] == [2000, 5000]
    assert r["what_if_extra"][1]["interest_saved"] > r["what_if_extra"][0]["interest_saved"] > 0
    # keeps one month of essentials (45k) and uses the rest on the card
    assert r["savings_move"]["use_from_savings"] == 35000 and r["savings_move"]["pay_towards"] == "credit card"
    assert r["savings_move"]["interest_saved"] > 0
    assert any("savings" in a for a in r["actions"])


def test_rescue_plan_shortfall():
    r = run_operation("debt_rescue_plan", {"debts": DEBTS, "monthly_income": 60000, "essential_expenses": 40000})
    assert r["status"] == "shortfall" and r["monthly_shortfall"] == 6750
    assert any("restructuring" in a for a in r["actions"])
    assert any("app loan" in a or "new high-interest loan" in a for a in r["actions"])


def test_snowball_only_when_it_gives_a_real_quick_win():
    debts = [{"name": "card A", "balance": 20000, "apr": 36, "min_payment": 1000},
             {"name": "card B", "balance": 150000, "apr": 38, "min_payment": 6000}]
    r = run_operation("debt_rescue_plan", {"debts": debts, "monthly_income": 50000, "essential_expenses": 35000})
    assert r["recommended_strategy"] == "snowball"
    assert r["comparison"]["snowball"]["first_cleared"]["month"] < r["comparison"]["avalanche"]["first_cleared"]["month"]


def test_consolidation_check():
    r = run_operation("consolidation_check", {"debts": DEBTS[:2], "new_rate": 13, "tenure_months": 36,
                                              "processing_fee_percent": 2})
    assert r["fees"] == 9100 and r["verdict"] == "consolidate" and r["net_saving"] > 0
    bad = run_operation("consolidation_check", {"debts": [DEBTS[2]], "new_rate": 15, "tenure_months": 60})
    assert bad["verdict"] == "do_not_consolidate" and bad["net_saving"] < 0


def test_minimum_due_trap():
    r = run_operation("minimum_due_trap", {"balance": 155000, "apr": 42})
    assert r["first_minimum_due"] == 7750
    assert r["minimum_only_months"] > 10 * 12
    assert r["minimum_only_interest"] > r["fixed_payment_interest"]
    stuck = run_operation("minimum_due_trap", {"balance": 100000, "apr": 48, "min_percent": 2, "min_floor": 0})
    assert stuck["minimum_only_months"] is None and "never" in stuck["summary"]


@pytest.mark.parametrize("params", [
    {"debts": [], "monthly_income": 1, "essential_expenses": 1},
    {"debts": "card", "monthly_income": 1, "essential_expenses": 1},
    {"debts": [{"name": "x", "balance": -5, "apr": 10, "min_payment": 1}], "monthly_income": 1, "essential_expenses": 1},
    {"debts": DEBTS},
])
def test_invalid_input(params):
    with pytest.raises(FinanceInputError):
        run_operation("debt_rescue_plan", params)


def test_tool_runs_debt_plan():
    res = asyncio.run(FinanceCalcTool().run({"operation": "debt_rescue_plan", "params": {
        "debts": DEBTS, "monthly_income": 90000, "essential_expenses": 45000}}))
    assert res.success and res.data["status"] == "workable"


@pytest.mark.parametrize("message", [
    "I can't manage my EMIs anymore, how do I get out of debt?",
    "Is a balance transfer worth it for my 1.5 lakh card?",
    "I have too many loans, help",
])
def test_debt_stress_routes_to_planning(message):
    assert IntentClassifier.classify(message, None, [], []).intent == Intent.PLANNING


def test_prompts_cover_debt_stress():
    from backend.prompts.planner import get_planner_system_prompt
    from backend.prompts.tora import TORA_SYSTEM_PROMPT
    assert "debt_rescue_plan" in get_planner_system_prompt([])
    assert "Integrated Ombudsman" in TORA_SYSTEM_PROMPT and "14416" in TORA_SYSTEM_PROMPT


def test_consolidation_comparison_rows_are_consistent():
    """The answer copies these rows, so they must add up on their own."""
    from backend.finance.debt import consolidation_check

    out = consolidation_check(
        debts=[{"name": "credit card", "balance": 120000, "apr": 40, "min_payment": 6000},
               {"name": "personal loan", "balance": 250000, "apr": 15, "min_payment": 9000}],
        new_rate=14, tenure_months=36, processing_fee_percent=2,
    )
    rows = {r["item"]: r for r in out["comparison"]}
    assert set(rows) == {"Total debt", "Monthly outflow compared", "Months to clear", "Interest", "Fees",
                         "Total cost (interest + fees)"}
    money = lambda text: float(text.replace("₹", "").replace(",", ""))
    assert money(rows["Total debt"]["current"]) == 370000
    assert rows["Monthly outflow compared"]["current"] == rows["Monthly outflow compared"]["consolidation"]
    assert money(rows["Interest"]["current"]) == round(out["current_plan_interest"])
    assert money(rows["Interest"]["consolidation"]) == round(out["new_loan_interest_same_outflow"])
    assert money(rows["Fees"]["consolidation"]) == round(out["fees"])
    # total cost = interest + fees on the new loan, and interest alone on the current debts
    assert money(rows["Total cost (interest + fees)"]["consolidation"]) == round(out["new_total_cost"])
    assert abs((money(rows["Total cost (interest + fees)"]["current"])
                - money(rows["Total cost (interest + fees)"]["consolidation"])) - out["net_saving"]) <= 1
    assert int(rows["Months to clear"]["consolidation"]) == out["new_loan_months_same_outflow"]


def test_rescue_plan_writes_out_its_rupee_figures():
    """A live run turned ₹3,70,000 into '₹37 Lakh'; the answer now copies these strings."""
    from backend.finance.debt import debt_rescue_plan

    out = debt_rescue_plan(
        debts=[{"name": "Credit card", "balance": 120000, "apr": 40, "min_payment": 6000},
               {"name": "Personal loan", "balance": 250000, "apr": 15, "min_payment": 9000}],
        monthly_income=88000, essential_expenses=40000, current_savings=60000,
    )
    figures = out["figures"]
    assert figures["total_debt"] == "₹3,70,000"
    assert figures["minimum_payments"] == "₹15,000"
    assert figures["monthly_budget_for_debt"] == "₹48,000"
    assert figures["extra_over_minimums"] == "₹33,000"
    assert figures["income"] == "₹88,000" and figures["essentials"] == "₹40,000"
    # every figure is the engine's own number, written in the Indian system
    assert figures["interest_on_this_plan"].startswith("₹")
    assert figures["interest_if_minimums_only"] == "₹1,40,074"
