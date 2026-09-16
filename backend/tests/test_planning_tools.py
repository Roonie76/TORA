"""Phase 4E — deterministic planning operations."""
import pytest

from backend.finance import FinanceInputError, run_operation
from backend.finance import engine as fe


class TestBudgetPlan:
    def test_buckets_and_actions(self):
        r = fe.budget_plan(100000, needs={"rent": 30000, "groceries": 12000},
                           wants={"dining": 15000, "shopping": 10000}, emis=20000,
                           current_savings_per_month=5000)
        buckets = {b["bucket"]: b for b in r["buckets"]}
        assert buckets["needs"]["actual"] == 62000 and buckets["needs"]["gap"] == 12000
        assert buckets["wants"]["actual"] == 25000
        assert buckets["savings"]["actual"] == 13000  # 5000 + 8000 unallocated
        assert any("Needs" in a for a in r["actions"])
        assert any("short of 20%" in a for a in r["actions"])

    def test_overspending_flagged(self):
        r = fe.budget_plan(50000, needs={"rent": 40000}, wants={"travel": 20000})
        assert r["unallocated"] == -10000
        assert any("exceeds income" in a for a in r["actions"])

    def test_high_emi_flagged(self):
        r = fe.budget_plan(100000, emis=45000)
        assert any("EMIs alone" in a for a in r["actions"])


class TestGoalPlan:
    GOALS = [
        {"name": "house down payment", "target": 2500000, "years": 7, "current_saved": 300000},
        {"name": "car", "target": 800000, "years": 3},
    ]

    def test_required_monthly_matches_required_sip(self):
        r = fe.goal_plan([{"name": "car", "target": 800000, "years": 3}], 10)
        assert r["goals"][0]["required_monthly"] == pytest.approx(
            fe.required_sip(800000, 10, 3)["monthly_sip"], abs=0.01)

    def test_existing_savings_reduce_requirement(self):
        with_savings = fe.goal_plan([{"name": "h", "target": 2500000, "years": 7, "current_saved": 300000}])
        without = fe.goal_plan([{"name": "h", "target": 2500000, "years": 7}])
        assert with_savings["goals"][0]["required_monthly"] < without["goals"][0]["required_monthly"]

    def test_capacity_funds_nearest_deadline_first(self):
        r = fe.goal_plan([dict(g) for g in self.GOALS], 10, 30000)
        assert [g["name"] for g in r["goals"]] == ["car", "house down payment"]
        car, house = r["goals"]
        assert car["status"] == "on_track"
        assert house["status"] == "short" and house["shortfall"] > 0
        assert car["funded_monthly"] + house["funded_monthly"] == pytest.approx(30000, abs=0.02)

    def test_enough_capacity(self):
        r = fe.goal_plan([dict(g) for g in self.GOALS], 10, 50000)
        assert all(g["status"] == "on_track" for g in r["goals"])
        assert "on track" in r["summary"]

    def test_already_funded_goal_needs_nothing(self):
        r = fe.goal_plan([{"name": "trip", "target": 100000, "years": 2, "current_saved": 200000}])
        assert r["goals"][0]["required_monthly"] == 0


class TestRetirementPlan:
    def test_known_projection(self):
        r = fe.retirement_plan(30, 60, 50000, current_savings=500000)
        assert r["monthly_expenses_at_retirement"] == pytest.approx(287174.56, abs=1)
        assert r["corpus_needed"] == pytest.approx(77148478, rel=1e-4)
        sip_value = fe.sip_future_value(r["required_monthly_sip"], 11, 30)["future_value"]
        assert sip_value + r["current_savings_future_value"] == pytest.approx(r["corpus_needed"], rel=1e-5)

    def test_zero_real_return(self):
        r = fe.retirement_plan(40, 60, 10000, inflation_rate=6, return_after=6, life_expectancy=80)
        assert r["corpus_needed"] == pytest.approx(r["monthly_expenses_at_retirement"] * 12 * 20, rel=1e-6)

    @pytest.mark.parametrize("args", [
        (60, 55, 50000), (30, 60, 50000, 6, 11, 7, 58), (10, 60, 50000),
    ])
    def test_invalid_ages(self, args):
        with pytest.raises(FinanceInputError):
            fe.retirement_plan(*args)


def test_operations_registered():
    for op in ("budget_plan", "goal_plan", "retirement_plan"):
        assert op in fe.OPERATIONS and op in fe.OPERATION_PARAMS
    r = run_operation("goal_plan", {"goals": [{"name": "a", "target": 100000, "years": 1}]})
    assert r["total_required_monthly"] > 0
