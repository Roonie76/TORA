"""A parameter the model set to null is one it did not have, not a bad number.

Live on localhost: "I also have a car loan of 6 lakh at 11% with an EMI of 13,000" ran a health
check that failed with "'high_interest_debt' must be a number" — the model had emitted null for
an optional field whose default is 0. Five minutes and two model calls to produce an error.
"""
import asyncio

import pytest

from backend.tools.finance_tool import FinanceCalcTool, _drop_unset_optionals

BASE = {"monthly_income": 95000, "monthly_expenses": 24000}


def run(params, operation="financial_health_check"):
    return asyncio.run(FinanceCalcTool().run(args={"operation": operation, "params": params}))


class TestNullOptionals:
    @pytest.mark.parametrize("value", [None, "", "none", "null", "N/A", "unknown", "-"])
    def test_an_unset_optional_is_treated_as_omitted(self, value):
        result = run({**BASE, "high_interest_debt": value})
        assert result.success, result.error
        assert result.data["score"] == run(BASE).data["score"]

    def test_a_real_zero_is_still_a_zero(self):
        assert run({**BASE, "high_interest_debt": 0}).success

    def test_a_real_value_still_counts(self):
        with_debt = run({**BASE, "high_interest_debt": 500000})
        assert with_debt.success
        assert with_debt.data["score"] != run(BASE).data["score"]

    def test_a_null_required_parameter_still_fails(self):
        """Silently defaulting a required input would invent an answer out of nothing."""
        result = run({"monthly_income": None, "monthly_expenses": 24000})
        assert not result.success and "monthly_income" in result.error

    def test_an_unknown_operation_is_left_alone(self):
        assert _drop_unset_optionals("no_such_operation", {"a": None}) == {"a": None}

    def test_a_non_dict_is_left_alone(self):
        assert _drop_unset_optionals("financial_health_check", None) is None

    def test_a_string_that_is_not_blank_is_untouched(self):
        kept = _drop_unset_optionals("financial_health_check", {"high_interest_debt": "50000"})
        assert kept == {"high_interest_debt": "50000"}
