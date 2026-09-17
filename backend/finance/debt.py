"""
Phase 8 — debt-rescue calculations.

Deterministic helpers for people under debt stress: where they stand, whether
their income covers the minimums, the fastest affordable way out, what extra
payments or savings would change, whether a consolidation loan really helps and
how expensive paying only the credit-card minimum is.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .engine import FinanceInputError, _pos, _r, _rate, debt_payoff, emi, inr

MAX_MONTHS = 600
STRESS_BANDS = ((30.0, "manageable"), (40.0, "stretched"), (50.0, "high"), (float("inf"), "critical"))


def _items(debts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(debts, list) or not debts or len(debts) > 10:
        raise FinanceInputError("Provide between 1 and 10 debts as [{name, balance, apr, min_payment}].")
    out = []
    for idx, d in enumerate(debts):
        if not isinstance(d, dict):
            raise FinanceInputError("Each debt must be an object with name, balance, apr and min_payment.")
        out.append({
            "name": str(d.get("name") or f"debt_{idx + 1}")[:40],
            "balance": _pos("balance", d.get("balance")),
            "apr": _rate("apr", d.get("apr", 0), 60),
            "min_payment": _pos("min_payment", d.get("min_payment", 0), allow_zero=True),
        })
    return out


def _stress(dti: Optional[float]) -> Optional[str]:
    if dti is None:
        return None
    for limit, label in STRESS_BANDS:
        if dti <= limit:
            return label
    return "critical"


def _simulate_minimums(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pay exactly the minimums every month (freed-up minimums are NOT redirected)."""
    bal = {x["name"]: x["balance"] for x in items}
    interest = 0.0
    for month in range(1, MAX_MONTHS + 1):
        for x in items:
            b = bal[x["name"]]
            if b <= 0.005:
                continue
            i = b * x["apr"] / 1200
            interest += i
            b += i
            b -= min(x["min_payment"], b)
            if month > 1 and x["min_payment"] <= b * x["apr"] / 1200 + 1e-9 and b > 0.005:
                return None  # minimum never reduces this debt
            bal[x["name"]] = b
        if all(v <= 0.005 for v in bal.values()):
            return {"months": month, "total_interest": _r(interest)}
    return None


def debt_snapshot(debts: List[Dict[str, Any]], monthly_income: Optional[float] = None) -> Dict[str, Any]:
    items = _items(debts)
    income = _pos("monthly_income", monthly_income) if monthly_income else None
    total = sum(x["balance"] for x in items)
    mins = sum(x["min_payment"] for x in items)
    monthly_interest = sum(x["balance"] * x["apr"] / 1200 for x in items)
    weighted = sum(x["balance"] * x["apr"] for x in items) / total
    dti = 100 * mins / income if income else None
    costliest = max(items, key=lambda x: (x["apr"], x["balance"]))
    warnings = []
    for x in items:
        interest = x["balance"] * x["apr"] / 1200
        if x["min_payment"] and x["min_payment"] <= interest:
            warnings.append(f"{x['name']}: the minimum payment ({inr(x['min_payment'])}) does not even cover this "
                            f"month's interest ({inr(interest)}), so the balance will grow.")
        elif x["min_payment"] and x["min_payment"] < 1.5 * interest:
            warnings.append(f"{x['name']}: most of the {inr(x['min_payment'])} payment goes to interest "
                            f"({inr(interest)}); paying only this keeps you in debt for years.")
        if x["apr"] >= 30:
            warnings.append(f"{x['name']} costs {x['apr']:g}% a year — clear it first.")
    if income and mins > income:
        warnings.append("Minimum payments are higher than income.")
    result = {
        "operation": "debt_snapshot",
        "inputs": {"debts": debts, "monthly_income": income},
        "total_debt": _r(total),
        "total_minimum_payments": _r(mins),
        "monthly_interest_cost": _r(monthly_interest),
        "yearly_interest_cost": _r(monthly_interest * 12),
        "weighted_apr": _r(weighted),
        "debt_to_income_pct": _r(dti, 1) if dti is not None else None,
        "stress_level": _stress(dti),
        "costliest_debt": costliest["name"],
        "warnings": warnings,
        "summary": (f"Total debt {inr(total)} across {len(items)} accounts; minimums {inr(mins)}/month; about "
                    f"{inr(monthly_interest)} of interest is added each month (average {weighted:.1f}% a year)."
                    + (f" Minimums take {dti:.0f}% of income ({_stress(dti)})." if dti is not None else "")),
        "assumptions": ["Balances and rates as given; card minimums assumed fixed at the stated amount."],
    }
    return result


def debt_rescue_plan(
    debts: List[Dict[str, Any]],
    monthly_income: float,
    essential_expenses: float,
    current_savings: float = 0.0,
    buffer_months: float = 1.0,
    extra_payments: Optional[List[float]] = None,
) -> Dict[str, Any]:
    """Survival budget + fastest affordable way out + what-ifs."""
    items = _items(debts)
    income = _pos("monthly_income", monthly_income)
    essentials = _pos("essential_expenses", essential_expenses, allow_zero=True)
    savings = _pos("current_savings", current_savings, allow_zero=True)
    buffer = _pos("buffer_months", buffer_months, allow_zero=True)
    mins = sum(x["min_payment"] for x in items)
    free = income - essentials
    snapshot = debt_snapshot(debts, income)
    keep = buffer * essentials
    usable_savings = max(0.0, savings - keep)
    result: Dict[str, Any] = {
        "operation": "debt_rescue_plan",
        "inputs": {"debts": debts, "monthly_income": income, "essential_expenses": essentials,
                   "current_savings": savings, "buffer_months": buffer},
        "snapshot": {k: snapshot[k] for k in ("total_debt", "total_minimum_payments", "monthly_interest_cost",
                                              "weighted_apr", "debt_to_income_pct", "stress_level", "costliest_debt")},
        "warnings": snapshot["warnings"],
        "survival_budget": {
            "income": _r(income), "essentials": _r(essentials), "available_for_debt": _r(max(0.0, free)),
            "minimum_payments": _r(mins), "emergency_buffer_to_keep": _r(keep),
        },
    }
    if free < mins:
        shortfall = mins - max(0.0, free)
        result.update({
            "status": "shortfall",
            "monthly_shortfall": _r(shortfall),
            "actions": [
                f"Income after essentials ({inr(max(0.0, free))}) does not cover the minimums ({inr(mins)}); "
                f"the gap is {inr(shortfall)} a month.",
                "Contact the lenders before missing a payment and ask in writing about restructuring, a longer "
                "tenure or a lower EMI; keep records of every conversation.",
                "Do not take a new high-interest loan or app loan to pay existing EMIs.",
                "List non-essential spending and income options that could close the gap.",
                f"Keep at least {inr(keep)} for essentials; use savings above that only for the costliest debt "
                f"({snapshot['costliest_debt']}).",
                "If a legal notice or recovery pressure arrives, get professional help (lawyer, legal aid or a "
                "registered credit counsellor).",
            ],
            "summary": f"Income after essentials falls {inr(shortfall)} a month short of the minimum payments.",
        })
        return result

    budget = free
    avalanche = debt_payoff(debts, budget, "avalanche")
    snowball = debt_payoff(debts, budget, "snowball")
    minimum_only = _simulate_minimums(items)
    first_win = {s: plan["payoff_order"][0] for s, plan in (("avalanche", avalanche), ("snowball", snowball))}
    diff = snowball["total_interest"] - avalanche["total_interest"]
    quicker_win = first_win["snowball"]["month"] < first_win["avalanche"]["month"]
    recommended = "snowball" if (quicker_win and diff <= max(1000.0, 0.02 * avalanche["total_interest"])) else "avalanche"
    best = avalanche if recommended == "avalanche" else snowball

    what_if = []
    for extra in (extra_payments if extra_payments is not None else [2000, 5000]):
        extra = _pos("extra_payment", extra, allow_zero=True)
        if extra <= 0:
            continue
        p = debt_payoff(debts, budget + extra, recommended)
        what_if.append({
            "extra_per_month": _r(extra),
            "months_to_debt_free": p["months_to_debt_free"],
            "months_saved": best["months_to_debt_free"] - p["months_to_debt_free"],
            "interest_saved": _r(best["total_interest"] - p["total_interest"]),
        })

    savings_move = None
    if usable_savings > 0:
        target = max(items, key=lambda x: (x["apr"], -x["balance"]))
        lump = min(usable_savings, target["balance"])
        reduced = []
        for x in items:
            bal = x["balance"] - (lump if x is target else 0.0)
            if bal > 0.005:
                reduced.append({**x, "balance": bal})
        if reduced:
            after = debt_payoff(reduced, budget, recommended)
            months_after, interest_after = after["months_to_debt_free"], after["total_interest"]
        else:
            months_after, interest_after = 0, 0.0
        savings_move = {
            "use_from_savings": _r(lump),
            "pay_towards": target["name"],
            "months_to_debt_free": months_after,
            "interest_saved": _r(best["total_interest"] - interest_after),
            "savings_left": _r(savings - lump),
        }

    actions = [
        f"Keep essentials at about {inr(essentials)} and send {inr(budget)} a month to debts "
        f"(minimums {inr(mins)} + {inr(budget - mins)} extra).",
        f"Put every extra rupee on {best['payoff_order'][0]['name']} first "
        f"({'highest interest' if recommended == 'avalanche' else 'smallest balance — a quick win'}).",
    ]
    if savings_move:
        actions.append(f"Use {inr(savings_move['use_from_savings'])} of savings on {savings_move['pay_towards']} now; "
                       f"it saves about {inr(savings_move['interest_saved'])} and still leaves {inr(savings_move['savings_left'])}.")
    if what_if:
        w = what_if[0]
        if w["months_saved"] > 0:
            unit = "month" if w["months_saved"] == 1 else "months"
            actions.append(f"Finding {inr(w['extra_per_month'])} more a month clears everything {w['months_saved']} "
                           f"{unit} sooner and saves {inr(w['interest_saved'])}.")
    actions.append("Stop new card spending and avoid new loans until the costliest debt is gone.")

    result.update({
        "status": "workable",
        "monthly_budget_for_debt": _r(budget),
        "recommended_strategy": recommended,
        "plan": {"months_to_debt_free": best["months_to_debt_free"], "total_interest": best["total_interest"],
                 "payoff_order": best["payoff_order"]},
        "comparison": {
            "avalanche": {"months": avalanche["months_to_debt_free"], "total_interest": avalanche["total_interest"],
                          "first_cleared": first_win["avalanche"]},
            "snowball": {"months": snowball["months_to_debt_free"], "total_interest": snowball["total_interest"],
                         "first_cleared": first_win["snowball"]},
            "minimums_only": minimum_only or {"months": None, "total_interest": None,
                                              "note": "Paying only the minimums never clears at least one debt."},
        },
        "what_if_extra": what_if,
        "savings_move": savings_move,
        "actions": actions,
        "summary": (f"With {inr(budget)} a month for debts, the {recommended} method makes you debt-free in "
                    f"{best['months_to_debt_free']} months with about {inr(best['total_interest'])} interest."),
        "assumptions": ["Essential expenses and income stay as given; no new borrowing; rates unchanged.",
                        "All money left after essentials goes to debt — keep the emergency buffer separate."],
    })
    return result


def consolidation_check(
    debts: List[Dict[str, Any]],
    new_rate: float,
    tenure_months: int,
    processing_fee_percent: float = 0.0,
    other_fees: float = 0.0,
) -> Dict[str, Any]:
    """Would one consolidation / balance-transfer loan beat the current debts?"""
    items = _items(debts)
    total = sum(x["balance"] for x in items)
    fee = total * _rate("processing_fee_percent", processing_fee_percent, 10) / 100 + \
        _pos("other_fees", other_fees, allow_zero=True)
    new = emi(total, new_rate, tenure_months)
    mins = sum(x["min_payment"] for x in items)
    budget = max(new["emi"], mins)
    current = debt_payoff(debts, budget, "avalanche")
    # Compare at the same monthly outflow: any amount above the new EMI prepays the new loan.
    same_outflow = debt_payoff([{"name": "consolidation loan", "balance": total, "apr": new_rate,
                                 "min_payment": new["emi"]}], budget, "avalanche")
    new_cost = same_outflow["total_interest"] + fee
    saving = current["total_interest"] - new_cost
    weighted = sum(x["balance"] * x["apr"] for x in items) / total
    verdict = "consolidate" if saving > max(2000.0, 0.03 * total) else ("marginal" if saving > 0 else "do_not_consolidate")
    notes = []
    if new["emi"] > mins:
        notes.append(f"The new EMI ({inr(new['emi'])}) is higher than today's minimums ({inr(mins)}).")
    if new["emi"] < mins:
        notes.append(f"The new EMI ({inr(new['emi'])}) is lower than today's minimums ({inr(mins)}); the comparison "
                     f"assumes you keep paying {inr(budget)} a month. Paying only the new EMI costs "
                     f"{inr(new['total_interest'] + fee)} over {tenure_months} months.")
    notes.append("Consolidation only works if the cleared cards are not used again.")
    return {
        "operation": "consolidation_check",
        "inputs": {"debts": debts, "new_rate": new_rate, "tenure_months": tenure_months,
                   "processing_fee_percent": processing_fee_percent, "other_fees": other_fees},
        "total_debt": _r(total),
        "current_weighted_apr": _r(weighted),
        "new_emi": new["emi"],
        "new_loan_interest": new["total_interest"],
        "new_loan_interest_same_outflow": same_outflow["total_interest"],
        "new_loan_months_same_outflow": same_outflow["months_to_debt_free"],
        "fees": _r(fee),
        "new_total_cost": _r(new_cost),
        "current_plan_interest": current["total_interest"],
        "current_plan_months": current["months_to_debt_free"],
        "net_saving": _r(saving),
        # Ready-made rows so the answer never has to relabel a figure.
        "comparison": [
            {"item": "Total debt", "current": inr(total), "consolidation": inr(total)},
            {"item": "Monthly outflow compared", "current": inr(budget), "consolidation": inr(budget)},
            {"item": "Months to clear", "current": f"{current['months_to_debt_free']}",
             "consolidation": f"{same_outflow['months_to_debt_free']}"},
            {"item": "Interest", "current": inr(current["total_interest"]),
             "consolidation": inr(same_outflow["total_interest"])},
            {"item": "Fees", "current": inr(0), "consolidation": inr(fee)},
            {"item": "Total cost (interest + fees)", "current": inr(current["total_interest"]),
             "consolidation": inr(new_cost)},
        ],
        "verdict": verdict,
        "notes": notes,
        "summary": (f"Paying {inr(budget)} a month, a {new_rate:g}% consolidation loan costs {inr(new_cost)} "
                    f"(interest + fees) vs {inr(current['total_interest'])} on the current debts — "
                    + (f"saves {inr(saving)}." if saving > 0 else f"costs {inr(-saving)} more.")),
        "assumptions": ["Same monthly outflow compared in both cases; fees paid up front; rates fixed."],
    }


def minimum_due_trap(balance: float, apr: float, min_percent: float = 5.0, min_floor: float = 200.0) -> Dict[str, Any]:
    """Cost of paying only the credit-card minimum due vs a fixed payment."""
    b0 = _pos("balance", balance)
    r = _rate("apr", apr, 60) / 1200
    pct = _rate("min_percent", min_percent, 100) / 100
    floor = _pos("min_floor", min_floor, allow_zero=True)
    b, interest, month = b0, 0.0, 0
    first_min = max(b0 * pct, floor)
    while b > 0.5:
        month += 1
        if month > MAX_MONTHS:
            break
        i = b * r
        interest += i
        b += i
        pay = min(b, max(b * pct, floor))
        if pay <= i + 1e-9:
            month = None
            break
        b -= pay
    try:
        fixed = debt_payoff([{"name": "card", "balance": b0, "apr": apr, "min_payment": 0}], first_min, "avalanche")
    except FinanceInputError:
        fixed = {"months_to_debt_free": None, "total_interest": None}
    trapped = month is None or month > MAX_MONTHS
    return {
        "operation": "minimum_due_trap",
        "inputs": {"balance": b0, "apr": apr, "min_percent": min_percent, "min_floor": floor},
        "first_minimum_due": _r(first_min),
        "minimum_only_months": None if trapped else month,
        "minimum_only_interest": None if trapped else _r(interest),
        "fixed_payment_months": fixed["months_to_debt_free"],
        "fixed_payment_interest": fixed["total_interest"],
        "summary": ("Paying only the minimum never clears this balance — the interest is larger than the payment."
                    if trapped else
                    f"Paying only the minimum takes {month} months ({month / 12:.1f} years) and about {inr(interest)} "
                    f"interest; paying a fixed {inr(first_min)} every month clears it in "
                    f"{fixed['months_to_debt_free']} months with {inr(fixed['total_interest'])} interest."),
        "assumptions": [f"Minimum due = {min_percent:g}% of the balance (at least {inr(floor)}); no new spending or fees."],
    }


DEBT_OPERATIONS = {
    "debt_snapshot": debt_snapshot,
    "debt_rescue_plan": debt_rescue_plan,
    "consolidation_check": consolidation_check,
    "minimum_due_trap": minimum_due_trap,
}
DEBT_PARAMS = {
    "debt_snapshot": "debts: [{name, balance, apr, min_payment}], [monthly_income]",
    "debt_rescue_plan": "debts: [{name, balance, apr, min_payment}], monthly_income, essential_expenses, "
                        "[current_savings], [buffer_months], [extra_payments: [amounts]]",
    "consolidation_check": "debts: [{name, balance, apr, min_payment}], new_rate, tenure_months, "
                           "[processing_fee_percent], [other_fees]",
    "minimum_due_trap": "balance, apr, [min_percent], [min_floor]",
}


def _register() -> None:
    from . import engine

    engine.OPERATIONS.update(DEBT_OPERATIONS)
    engine.OPERATION_PARAMS.update(DEBT_PARAMS)


_register()
