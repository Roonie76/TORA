"""
The general scenario engine: change several things at once and see what moves.

The per-engine what-ifs already here each answer one question with one variable
moved -- sip_change_impact, prepay_vs_invest, loan_tenure_choice. Real questions
are not shaped like that. "If I take the new job at 15k more but the rent goes up
6k and I clear the card, where am I?" moves three things and touches every metric
at once.

Two rules this follows, both of them the project's usual ones rather than
anything new here:

* No arithmetic is reimplemented. Each metric is computed by the engine function
  that already owns it, run twice -- once on the baseline, once on the changed
  figures. A second implementation of savings rate that drifted from the first
  would be a bug that unit tests pass straight through.

* A metric whose inputs are not known is absent, not zero and not guessed. If
  TORA does not know the expenses, the scenario says nothing about savings rate
  rather than reporting a confident 100%.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import engine

# The figures a scenario can move, and what each one feeds.
FIELDS = ("monthly_income", "monthly_expenses", "rent", "monthly_debt_payments", "savings")

# How a change may be expressed.
#   {"set": 60000}     -> becomes 60000
#   {"change": 10000}  -> 10000 more (negative for less)
#   {"pct": 10}        -> 10% more
CHANGE_KEYS = ("set", "change", "pct")


class ScenarioInputError(ValueError):
    """A scenario that cannot be read, as opposed to one that cannot be computed."""


def _clean_baseline(baseline: Any) -> Dict[str, float]:
    if not isinstance(baseline, dict):
        raise ScenarioInputError("'baseline' must be an object of figure -> amount.")
    out: Dict[str, float] = {}
    for key in FIELDS:
        value = baseline.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ScenarioInputError(f"'{key}' must be a number.")
        if number < 0:
            raise ScenarioInputError(f"'{key}' cannot be negative.")
        out[key] = number
    if not out:
        raise ScenarioInputError("A scenario needs at least one starting figure.")
    return out


def _apply(baseline: Dict[str, float], changes: Any) -> Tuple[Dict[str, float], List[Dict[str, Any]]]:
    """Return the changed figures and a record of what was moved."""
    if not isinstance(changes, dict) or not changes:
        raise ScenarioInputError("A scenario needs at least one change.")

    scenario = dict(baseline)
    moved: List[Dict[str, Any]] = []

    for key, spec in list(changes.items())[:20]:
        if key not in FIELDS:
            raise ScenarioInputError(f"'{key}' is not a figure a scenario can change.")
        if isinstance(spec, (int, float)):
            spec = {"set": float(spec)}
        if not isinstance(spec, dict):
            raise ScenarioInputError(f"Change to '{key}' must be a number or an object.")

        given = [k for k in CHANGE_KEYS if k in spec]
        if len(given) != 1:
            raise ScenarioInputError(
                f"Change to '{key}' needs exactly one of set, change or pct."
            )
        how = given[0]
        try:
            amount = float(spec[how])
        except (TypeError, ValueError):
            raise ScenarioInputError(f"Change to '{key}' must be a number.")

        # Changing a figure the baseline does not have would invent a starting
        # point: "spend 2000 less" on an unknown expense means nothing.
        if key not in baseline and how != "set":
            raise ScenarioInputError(
                f"'{key}' is not known, so it can only be set, not adjusted."
            )

        before = baseline.get(key)
        if how == "set":
            after = amount
        elif how == "change":
            after = float(before) + amount
        else:
            after = float(before) * (1 + amount / 100.0)

        if after < 0:
            raise ScenarioInputError(f"That change would take '{key}' below zero.")

        scenario[key] = round(after, 2)
        moved.append({"field": key, "how": how, "amount": amount,
                      "before": before, "after": scenario[key]})

    return scenario, moved


def _metrics(figures: Dict[str, float], emergency_months: float) -> Dict[str, Dict[str, Any]]:
    """
    Every metric whose inputs are present. An absent metric is absent, never zero.
    """
    income = figures.get("monthly_income")
    expenses = figures.get("monthly_expenses")
    rent = figures.get("rent")
    debt = figures.get("monthly_debt_payments")
    savings = figures.get("savings")

    # Rent is an expense; a scenario that moves rent has to move outgoings with
    # it, or "rent up 6k" would leave the savings rate untouched and look free.
    outgoings = None
    if expenses is not None:
        outgoings = expenses + (rent or 0.0)

    out: Dict[str, Dict[str, Any]] = {}

    if income is not None and outgoings is not None:
        out["savings_rate"] = engine.savings_rate(income, outgoings)
    if income is not None and debt is not None:
        out["debt_to_income"] = engine.debt_to_income(debt, income)
    if outgoings is not None:
        out["emergency_fund"] = engine.emergency_fund(
            outgoings, months=emergency_months, current_savings=savings or 0.0
        )
    if income is not None and outgoings is not None:
        left = income - outgoings - (debt or 0.0)
        out["monthly_left_over"] = {"amount": round(left, 2), "formatted": engine.inr(left)}

    return out


def _headline(name: str, metric: Dict[str, Any]) -> Optional[Tuple[str, float]]:
    """The one number from a metric that a comparison is about."""
    # These names are the engines' own output keys, not names invented here. A
    # mismatch fails silently -- the metric simply drops out of the comparison --
    # so the tests assert on the metric list rather than only on its contents.
    picks = {
        "savings_rate": "savings_rate_percent",
        "debt_to_income": "ratio_percent",
        "emergency_fund": "coverage_months",
        "monthly_left_over": "amount",
    }
    key = picks.get(name)
    if key is None:
        return None
    value = metric.get(key)
    if not isinstance(value, (int, float)):
        return None
    return key, float(value)


# Metrics where a bigger number is a better outcome.
_HIGHER_IS_BETTER = {"savings_rate": True, "debt_to_income": False,
                     "emergency_fund": True, "monthly_left_over": True}


def compare(baseline: Any, changes: Any, emergency_months: float = 6.0) -> Dict[str, Any]:
    """
    Run every applicable metric on the baseline and on the changed figures.

    Returns both sides plus the movement in each metric, so the answer can be
    rendered as a table of real numbers rather than written by a model.
    """
    base_figures = _clean_baseline(baseline)
    scenario_figures, moved = _apply(base_figures, changes)

    months = float(emergency_months) if emergency_months else 6.0
    if months <= 0:
        raise ScenarioInputError("'emergency_months' must be greater than zero.")

    before = _metrics(base_figures, months)
    after = _metrics(scenario_figures, months)

    comparison: List[Dict[str, Any]] = []
    for name in before:
        if name not in after:
            continue
        head_before = _headline(name, before[name])
        head_after = _headline(name, after[name])
        if head_before is None or head_after is None:
            continue
        _, b = head_before
        _, a = head_after
        delta = round(a - b, 2)
        better = None
        if delta != 0:
            better = (delta > 0) == _HIGHER_IS_BETTER.get(name, True)
        comparison.append({
            "metric": name,
            "before": b,
            "after": a,
            "change": delta,
            "improved": better,
        })

    return {
        "operation": "scenario_compare",
        "baseline": base_figures,
        "scenario": scenario_figures,
        "changed": moved,
        "metrics_before": before,
        "metrics_after": after,
        "comparison": comparison,
        # Said out loud because it is the limit of the answer: a scenario is
        # arithmetic on figures the user gave, not a forecast.
        "note": "These are the same calculations run on both sets of figures. "
                "They do not predict whether the change happens.",
    }
