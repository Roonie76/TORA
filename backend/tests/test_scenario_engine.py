"""
The general scenario engine: several things move at once.

The existing what-ifs each move one variable. Real questions do not: "the new job
pays 15k more, but rent goes up 6k and the card is cleared" moves three and
touches every metric. These tests pin the two rules that keep it honest -- no
metric is reimplemented here, and a metric whose inputs are unknown is absent
rather than zero.
"""

import pytest

from backend.finance import engine
from backend.finance.scenario import ScenarioInputError, compare

BASE = {
    "monthly_income": 60000,
    "monthly_expenses": 20000,
    "rent": 18000,
    "monthly_debt_payments": 12000,
    "savings": 100000,
}


def metric_names(result):
    return {c["metric"] for c in result["comparison"]}


def headline(result, name):
    return next(c for c in result["comparison"] if c["metric"] == name)


class TestMovingSeveralThingsAtOnce:
    def test_it_reports_every_metric_on_both_sides(self):
        out = compare(BASE, {
            "monthly_income": {"change": 15000},
            "rent": {"change": 6000},
            "monthly_debt_payments": {"set": 4000},
        })
        assert metric_names(out) == {
            "savings_rate", "debt_to_income", "emergency_fund", "monthly_left_over",
        }
        assert out["scenario"]["monthly_income"] == 75000
        assert out["scenario"]["rent"] == 24000
        assert out["scenario"]["monthly_debt_payments"] == 4000
        # the baseline is untouched
        assert out["baseline"]["monthly_income"] == 60000

    def test_a_raise_that_is_eaten_by_rent_shows_where_it_went(self):
        """
        The point of moving several things at once: the raise improves the
        savings rate while the emergency fund gets *worse*, because a higher
        rent raises the target it is measured against. Moving one variable at a
        time never shows that.
        """
        out = compare(BASE, {"monthly_income": {"change": 15000}, "rent": {"change": 6000}})
        assert headline(out, "savings_rate")["improved"] is True
        assert headline(out, "emergency_fund")["improved"] is False

    def test_rent_counts_as_an_outgoing(self):
        """
        If rent did not feed the expense side, "rent up 6k" would leave the
        savings rate untouched and read as free.
        """
        out = compare(BASE, {"rent": {"change": 6000}})
        assert headline(out, "savings_rate")["change"] < 0

    def test_lower_is_better_for_debt(self):
        out = compare(BASE, {"monthly_debt_payments": {"change": -6000}})
        row = headline(out, "debt_to_income")
        assert row["change"] < 0 and row["improved"] is True

    def test_an_unchanged_metric_is_marked_neither_way(self):
        out = compare(BASE, {"savings": {"change": 50000}})
        assert headline(out, "savings_rate")["change"] == 0
        assert headline(out, "savings_rate")["improved"] is None


class TestHowChangesAreExpressed:
    @pytest.mark.parametrize(
        "spec,expected",
        [
            ({"set": 75000}, 75000),
            ({"change": 15000}, 75000),
            ({"pct": 25}, 75000),
            (75000, 75000),  # a bare number means "set"
        ],
    )
    def test_set_change_and_percent(self, spec, expected):
        out = compare(BASE, {"monthly_income": spec})
        assert out["scenario"]["monthly_income"] == expected

    def test_what_moved_is_recorded(self):
        out = compare(BASE, {"rent": {"change": 6000}})
        assert out["changed"] == [
            {"field": "rent", "how": "change", "amount": 6000, "before": 18000, "after": 24000}
        ]

    @pytest.mark.parametrize(
        "changes,message",
        [
            ({"rent": {"set": 1, "pct": 2}}, "exactly one"),
            ({"holiday_budget": {"set": 5000}}, "not a figure"),
            ({"rent": {"change": -50000}}, "below zero"),
            ({}, "at least one change"),
        ],
    )
    def test_it_refuses_what_it_cannot_read(self, changes, message):
        with pytest.raises(ScenarioInputError, match=message):
            compare(BASE, changes)

    def test_an_unknown_figure_can_be_set_but_not_adjusted(self):
        """
        "Spend 2000 less" on an expense TORA does not know would have to invent
        the starting point, and every metric downstream would inherit it.
        """
        thin = {"monthly_income": 60000}
        with pytest.raises(ScenarioInputError, match="only be set"):
            compare(thin, {"monthly_expenses": {"change": -2000}})
        assert compare(thin, {"monthly_expenses": {"set": 25000}})["scenario"]["monthly_expenses"] == 25000


class TestUnknownInputsStayUnknown:
    def test_a_metric_without_its_inputs_is_absent_not_zero(self):
        """Not knowing the expenses must not be reported as a 100% savings rate."""
        out = compare({"monthly_income": 60000}, {"monthly_income": {"change": 10000}})
        assert "savings_rate" not in metric_names(out)
        assert "emergency_fund" not in metric_names(out)

    def test_debt_to_income_needs_both_sides(self):
        out = compare({"monthly_income": 60000, "monthly_debt_payments": 12000},
                      {"monthly_debt_payments": {"set": 6000}})
        assert "debt_to_income" in metric_names(out)
        assert "savings_rate" not in metric_names(out)

    def test_an_empty_baseline_is_refused(self):
        with pytest.raises(ScenarioInputError, match="at least one starting figure"):
            compare({}, {"rent": {"set": 1000}})


class TestItDoesNotReimplementAnyArithmetic:
    def test_each_metric_matches_the_engine_that_owns_it(self):
        """
        A second implementation of savings rate that drifted from the first
        would be a bug unit tests pass straight through, so the scenario's
        numbers are checked against the engine functions directly.
        """
        out = compare(BASE, {"monthly_income": {"change": 15000}})
        outgoings = BASE["monthly_expenses"] + BASE["rent"]

        assert out["metrics_before"]["savings_rate"] == engine.savings_rate(60000, outgoings)
        assert out["metrics_after"]["savings_rate"] == engine.savings_rate(75000, outgoings)
        assert out["metrics_before"]["debt_to_income"] == engine.debt_to_income(12000, 60000)
        assert out["metrics_before"]["emergency_fund"] == engine.emergency_fund(
            outgoings, months=6.0, current_savings=100000
        )


class TestThroughTheToolInterface:
    def test_it_runs_as_a_finance_calc_operation(self):
        out = engine.run_operation("scenario_compare", {
            "baseline": BASE,
            "changes": {"rent": {"change": 6000}},
        })
        assert out["operation"] == "scenario_compare"
        assert headline(out, "savings_rate")["change"] < 0

    def test_a_bad_scenario_fails_the_way_every_other_bad_parameter_does(self):
        """
        The planner has a repair loop for FinanceInputError. A scenario-specific
        exception escaping here would fail the turn instead of being retried.
        """
        with pytest.raises(engine.FinanceInputError):
            engine.run_operation("scenario_compare", {"baseline": BASE, "changes": {"nope": 1}})

    def test_it_is_offered_to_the_planner(self):
        """The operation is useless if the planner is never shown it."""
        from backend.tools.finance_tool import FinanceCalcTool

        tool = FinanceCalcTool()
        assert "scenario_compare" in str(tool.get_schema())
        assert "scenario_compare" in engine.OPERATION_PARAMS

    def test_the_answer_says_what_it_is_not(self):
        out = compare(BASE, {"rent": {"change": 6000}})
        assert "do not predict" in out["note"]
