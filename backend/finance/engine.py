"""
Deterministic financial computation engine for TORA (Phase 3B).

All arithmetic for money questions happens here, never in the LLM. Functions are
pure, validate their inputs, round only at the edges, and return plain dicts that
include the inputs and the assumptions used so the model can explain them.

Conventions
- Rates are annual percentages (8.5 means 8.5% p.a.).
- Loans use monthly reducing balance (standard Indian bank EMI).
- SIPs assume investment at the start of each month with monthly compounding
  (the convention used by most Indian SIP calculators).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

MAX_MONTHS = 600  # 50 years
MAX_AMOUNT = 1e12


class FinanceInputError(ValueError):
    """Invalid input for a financial computation."""


def _r(x: float, places: int = 2) -> float:
    return round(float(x) + 0.0, places)


def _pos(name: str, value: float, allow_zero: bool = False) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise FinanceInputError(f"'{name}' must be a number.")
    if math.isnan(v) or math.isinf(v):
        raise FinanceInputError(f"'{name}' must be a finite number.")
    if v < 0 or (v == 0 and not allow_zero):
        raise FinanceInputError(f"'{name}' must be {'>= 0' if allow_zero else '> 0'}.")
    if v > MAX_AMOUNT:
        raise FinanceInputError(f"'{name}' is unrealistically large.")
    return v


def _rate(name: str, value: float, max_rate: float = 100.0) -> float:
    v = _pos(name, value, allow_zero=True)
    if v > max_rate:
        raise FinanceInputError(f"'{name}' must be between 0 and {max_rate}%.")
    return v


def _months(name: str, value: float) -> int:
    v = _pos(name, value)
    if v != int(v):
        raise FinanceInputError(f"'{name}' must be a whole number of months.")
    if v > MAX_MONTHS:
        raise FinanceInputError(f"'{name}' cannot exceed {MAX_MONTHS} months.")
    return int(v)


def _years(name: str, value: float) -> float:
    v = _pos(name, value)
    if v * 12 > MAX_MONTHS:
        raise FinanceInputError(f"'{name}' cannot exceed {MAX_MONTHS // 12} years.")
    return v


def inr(amount: float) -> str:
    """Indian digit grouping: 1234567.8 -> '₹12,34,568'."""
    neg = amount < 0
    n = int(round(abs(amount)))
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"{'-' if neg else ''}₹{s}"


# --------------------------------------------------------------------------- loans

def emi(principal: float, annual_rate: float, tenure_months: int) -> Dict[str, Any]:
    p = _pos("principal", principal)
    r_annual = _rate("annual_rate", annual_rate, 60)
    n = _months("tenure_months", tenure_months)
    i = r_annual / 1200
    payment = p / n if i == 0 else p * i * (1 + i) ** n / ((1 + i) ** n - 1)
    total = payment * n
    return {
        "operation": "emi",
        "inputs": {"principal": p, "annual_rate": r_annual, "tenure_months": n},
        "emi": _r(payment),
        "total_interest": _r(total - p),
        "total_payment": _r(total),
        "summary": f"EMI {inr(payment)}/month for {n} months; total interest {inr(total - p)}, total paid {inr(total)}.",
        "assumptions": ["Monthly reducing-balance EMI; processing fees, insurance and rate resets are not included."],
    }


def _simulate_loan(p: float, i: float, payment: float, extra_monthly: float,
                   lump_sums: Dict[int, float]) -> Dict[str, Any]:
    balance = p
    month = 0
    interest_total = 0.0
    yearly: List[Dict[str, Any]] = []
    y_int = y_prin = 0.0
    while balance > 0.005 and month < MAX_MONTHS:
        month += 1
        interest = balance * i
        principal_part = min(balance, payment + extra_monthly - interest)
        if principal_part <= 0:
            raise FinanceInputError("Payment does not cover the monthly interest; the loan would never close.")
        balance -= principal_part
        lump = min(balance, lump_sums.get(month, 0.0))
        balance -= lump
        interest_total += interest
        y_int += interest
        y_prin += principal_part + lump
        if month % 12 == 0 or balance <= 0.005:
            yearly.append({"year": math.ceil(month / 12), "interest_paid": _r(y_int),
                           "principal_paid": _r(y_prin), "closing_balance": _r(max(balance, 0))})
            y_int = y_prin = 0.0
    return {"months": month, "total_interest": interest_total, "yearly": yearly}


def amortization(principal: float, annual_rate: float, tenure_months: int,
                 extra_monthly: float = 0.0, lump_sum: Optional[float] = None,
                 lump_sum_month: Optional[int] = None) -> Dict[str, Any]:
    """Schedule (yearly) plus the effect of prepayments versus the baseline."""
    base = emi(principal, annual_rate, tenure_months)
    p, n = base["inputs"]["principal"], base["inputs"]["tenure_months"]
    i = base["inputs"]["annual_rate"] / 1200
    # Simulate with the unrounded EMI so the baseline closes exactly at the tenure.
    payment = p / n if i == 0 else p * i * (1 + i) ** n / ((1 + i) ** n - 1)
    extra = _pos("extra_monthly", extra_monthly, allow_zero=True)
    lumps: Dict[int, float] = {}
    if lump_sum:
        lm = _months("lump_sum_month", lump_sum_month or 12)
        lumps[lm] = _pos("lump_sum", lump_sum)
    baseline = _simulate_loan(p, i, payment, 0.0, {})
    scenario = _simulate_loan(p, i, payment, extra, lumps) if (extra or lumps) else baseline
    saved = baseline["total_interest"] - scenario["total_interest"]
    out = {
        "operation": "amortization",
        "inputs": {"principal": p, "annual_rate": base["inputs"]["annual_rate"], "tenure_months": n,
                   "extra_monthly": extra,
                   "lump_sum": next(iter(lumps.values()), 0.0),
                   "lump_sum_month": next(iter(lumps.keys()), None)},
        "emi": base["emi"],
        "months_to_close": scenario["months"],
        "total_interest": _r(scenario["total_interest"]),
        "baseline_total_interest": _r(baseline["total_interest"]),
        "interest_saved": _r(saved),
        "months_saved": baseline["months"] - scenario["months"],
        "yearly_schedule": scenario["yearly"][:30],
        "assumptions": base["assumptions"] + ["Prepayments reduce tenure (EMI unchanged); prepayment charges ignored."],
    }
    if extra or lumps:
        out["summary"] = (f"With prepayments the loan closes in {scenario['months']} months instead of "
                          f"{baseline['months']}, saving {inr(saved)} in interest.")
    else:
        out["summary"] = f"Loan closes in {scenario['months']} months with total interest {inr(scenario['total_interest'])}."
    return out


# --------------------------------------------------------------------- investing

def sip_future_value(monthly_investment: float, annual_return: float, years: float,
                     annual_step_up: float = 0.0) -> Dict[str, Any]:
    m = _pos("monthly_investment", monthly_investment)
    r = _rate("annual_return", annual_return, 50)
    y = _years("years", years)
    step = _rate("annual_step_up", annual_step_up, 100)
    i = r / 1200
    months = int(round(y * 12))
    value = invested = 0.0
    contribution = m
    for month in range(1, months + 1):
        value = (value + contribution) * (1 + i)
        invested += contribution
        if month % 12 == 0:
            contribution *= 1 + step / 100
    return {
        "operation": "sip_future_value",
        "inputs": {"monthly_investment": m, "annual_return": r, "years": y, "annual_step_up": step},
        "invested": _r(invested),
        "future_value": _r(value),
        "gains": _r(value - invested),
        "summary": f"Investing {inr(m)}/month for {y:g} years at {r:g}% p.a. grows to about {inr(value)} "
                   f"(invested {inr(invested)}, gains {inr(value - invested)}).",
        "assumptions": ["Constant annual return (market returns vary); monthly compounding; "
                        "investment at the start of each month; before tax and expense ratio."],
    }


def sip_change_impact(current_monthly: float, change: float, annual_return: float, years: float,
                      annual_step_up: float = 0.0) -> Dict[str, Any]:
    """What-if: effect of increasing (or reducing, negative change) a SIP."""
    cur = sip_future_value(current_monthly, annual_return, years, annual_step_up)
    new_amount = _pos("current_monthly", current_monthly) + float(change)
    if new_amount <= 0:
        raise FinanceInputError("The new SIP amount must be positive.")
    new = sip_future_value(new_amount, annual_return, years, annual_step_up)
    diff = new["future_value"] - cur["future_value"]
    return {
        "operation": "sip_change_impact",
        "inputs": {"current_monthly": current_monthly, "change": change, "annual_return": annual_return,
                   "years": years, "annual_step_up": annual_step_up},
        "current_future_value": cur["future_value"],
        "new_future_value": new["future_value"],
        "difference": _r(diff),
        "additional_invested": _r(new["invested"] - cur["invested"]),
        "summary": f"Changing the SIP from {inr(current_monthly)} to {inr(new_amount)} changes the {years:g}-year "
                   f"value from {inr(cur['future_value'])} to {inr(new['future_value'])} ({inr(diff)} difference).",
        "assumptions": cur["assumptions"],
    }


def required_sip(target_amount: float, annual_return: float, years: float) -> Dict[str, Any]:
    t = _pos("target_amount", target_amount)
    unit = sip_future_value(1.0, annual_return, years)["future_value"]
    monthly = t / unit
    return {
        "operation": "required_sip",
        "inputs": {"target_amount": t, "annual_return": annual_return, "years": years},
        "monthly_sip": _r(monthly),
        "summary": f"To reach {inr(t)} in {years:g} years at {annual_return:g}% p.a., invest about {inr(monthly)}/month.",
        "assumptions": sip_future_value(1.0, annual_return, years)["assumptions"],
    }


def lumpsum_future_value(principal: float, annual_return: float, years: float,
                         compounding_per_year: int = 1) -> Dict[str, Any]:
    p = _pos("principal", principal)
    r = _rate("annual_return", annual_return, 50)
    y = _years("years", years)
    k = int(_pos("compounding_per_year", compounding_per_year))
    if k not in (1, 2, 4, 12, 365):
        raise FinanceInputError("'compounding_per_year' must be one of 1, 2, 4, 12, 365.")
    value = p * (1 + r / 100 / k) ** (k * y)
    return {
        "operation": "compound_growth",
        "inputs": {"principal": p, "annual_return": r, "years": y, "compounding_per_year": k},
        "future_value": _r(value),
        "gains": _r(value - p),
        "effective_annual_rate": _r(((1 + r / 100 / k) ** k - 1) * 100, 3),
        "summary": f"{inr(p)} at {r:g}% p.a. (compounded {k}x/yr) becomes {inr(value)} in {y:g} years.",
        "assumptions": ["Constant rate; before tax (FD interest is taxable at slab rate)."],
    }


def inflation_adjust(amount: float, inflation_rate: float, years: float,
                     direction: str = "future_cost") -> Dict[str, Any]:
    a = _pos("amount", amount)
    r = _rate("inflation_rate", inflation_rate, 30)
    y = _years("years", years)
    factor = (1 + r / 100) ** y
    if direction == "future_cost":
        value = a * factor
        summary = f"Something costing {inr(a)} today would cost about {inr(value)} in {y:g} years at {r:g}% inflation."
    elif direction == "present_value":
        value = a / factor
        summary = f"{inr(a)} received in {y:g} years is worth about {inr(value)} in today's money at {r:g}% inflation."
    else:
        raise FinanceInputError("'direction' must be 'future_cost' or 'present_value'.")
    return {
        "operation": "inflation_adjust",
        "inputs": {"amount": a, "inflation_rate": r, "years": y, "direction": direction},
        "value": _r(value),
        "summary": summary,
        "assumptions": ["Constant average inflation rate."],
    }


# ---------------------------------------------------------------- debt & health

def debt_payoff(debts: List[Dict[str, Any]], monthly_budget: float, strategy: str = "avalanche") -> Dict[str, Any]:
    """Simulate paying several debts with a fixed monthly budget (avalanche or snowball)."""
    if not debts or len(debts) > 10:
        raise FinanceInputError("Provide between 1 and 10 debts.")
    if strategy not in ("avalanche", "snowball"):
        raise FinanceInputError("'strategy' must be 'avalanche' or 'snowball'.")
    budget = _pos("monthly_budget", monthly_budget)
    items = []
    for idx, d in enumerate(debts):
        items.append({
            "name": str(d.get("name") or f"debt_{idx + 1}")[:40],
            "balance": _pos("balance", d.get("balance")),
            "apr": _rate("apr", d.get("apr", 0), 60),
            "min_payment": _pos("min_payment", d.get("min_payment", 0), allow_zero=True),
        })
    if sum(x["min_payment"] for x in items) > budget:
        raise FinanceInputError("Monthly budget is lower than the sum of minimum payments.")
    order_key = (lambda x: (-x["apr"], x["balance"])) if strategy == "avalanche" else (lambda x: (x["balance"], -x["apr"]))
    month = 0
    total_interest = 0.0
    payoff_month: Dict[str, int] = {}
    while any(x["balance"] > 0.005 for x in items):
        month += 1
        if month > MAX_MONTHS:
            raise FinanceInputError("Debts would not be repaid within 50 years at this budget.")
        for x in items:
            if x["balance"] > 0.005:
                interest = x["balance"] * x["apr"] / 1200
                x["balance"] += interest
                total_interest += interest
        remaining = budget
        for x in items:
            if x["balance"] > 0.005:
                pay = min(x["min_payment"], x["balance"], remaining)
                x["balance"] -= pay
                remaining -= pay
        for x in sorted([x for x in items if x["balance"] > 0.005], key=order_key):
            pay = min(x["balance"], remaining)
            x["balance"] -= pay
            remaining -= pay
            if remaining <= 0:
                break
        for x in items:
            if x["balance"] <= 0.005 and x["name"] not in payoff_month:
                payoff_month[x["name"]] = month
        if month > 1 and remaining == budget:
            raise FinanceInputError("Budget does not reduce the debts.")
    order = sorted(payoff_month.items(), key=lambda kv: kv[1])
    return {
        "operation": "debt_payoff",
        "inputs": {"debts": debts, "monthly_budget": budget, "strategy": strategy},
        "months_to_debt_free": month,
        "total_interest": _r(total_interest),
        "payoff_order": [{"name": n, "month": m} for n, m in order],
        "summary": f"Using the {strategy} method with {inr(budget)}/month, all debts are cleared in {month} months "
                   f"with about {inr(total_interest)} total interest.",
        "assumptions": ["APR applied monthly on the outstanding balance; no new spending, fees or rate changes."],
    }


def emergency_fund(monthly_expenses: float, months: float = 6, current_savings: float = 0.0) -> Dict[str, Any]:
    e = _pos("monthly_expenses", monthly_expenses)
    m = _pos("months", months)
    s = _pos("current_savings", current_savings, allow_zero=True)
    target = e * m
    gap = max(0.0, target - s)
    return {
        "operation": "emergency_fund",
        "inputs": {"monthly_expenses": e, "months": m, "current_savings": s},
        "target": _r(target),
        "gap": _r(gap),
        "coverage_months": _r(s / e, 1),
        "summary": f"A {m:g}-month emergency fund is {inr(target)}; current savings cover {s / e:.1f} months"
                   + (f", gap {inr(gap)}." if gap else "."),
        "assumptions": ["3–6 months of essential expenses is a common guideline; 6+ for variable income."],
    }


def savings_rate(monthly_income: float, monthly_expenses: float) -> Dict[str, Any]:
    inc = _pos("monthly_income", monthly_income)
    exp = _pos("monthly_expenses", monthly_expenses, allow_zero=True)
    surplus = inc - exp
    rate = surplus / inc * 100
    return {
        "operation": "savings_rate",
        "inputs": {"monthly_income": inc, "monthly_expenses": exp},
        "monthly_surplus": _r(surplus),
        "savings_rate_percent": _r(rate, 1),
        "summary": f"Monthly surplus {inr(surplus)} — a savings rate of {rate:.1f}%.",
        "assumptions": ["Income is take-home; expenses include EMIs if listed."],
    }


def debt_to_income(monthly_debt_payments: float, monthly_income: float) -> Dict[str, Any]:
    d = _pos("monthly_debt_payments", monthly_debt_payments, allow_zero=True)
    inc = _pos("monthly_income", monthly_income)
    ratio = d / inc * 100
    band = "comfortable" if ratio <= 30 else ("stretched" if ratio <= 50 else "high")
    return {
        "operation": "debt_to_income",
        "inputs": {"monthly_debt_payments": d, "monthly_income": inc},
        "ratio_percent": _r(ratio, 1),
        "band": band,
        "summary": f"EMIs take {ratio:.1f}% of monthly income ({band}).",
        "assumptions": ["Bands are rough guides; lenders set their own FOIR limits (often 40–60% of net income)."],
    }


def net_worth(assets: Dict[str, float], liabilities: Dict[str, float]) -> Dict[str, Any]:
    if not isinstance(assets, dict) or not isinstance(liabilities, dict):
        raise FinanceInputError("'assets' and 'liabilities' must be objects of name -> amount.")
    a = {str(k)[:40]: _pos(f"assets.{k}", v, allow_zero=True) for k, v in list(assets.items())[:30]}
    l = {str(k)[:40]: _pos(f"liabilities.{k}", v, allow_zero=True) for k, v in list(liabilities.items())[:30]}
    total_a, total_l = sum(a.values()), sum(l.values())
    return {
        "operation": "net_worth",
        "inputs": {"assets": a, "liabilities": l},
        "total_assets": _r(total_a),
        "total_liabilities": _r(total_l),
        "net_worth": _r(total_a - total_l),
        "summary": f"Assets {inr(total_a)} − liabilities {inr(total_l)} = net worth {inr(total_a - total_l)}.",
        "assumptions": ["Values as provided by the user; market values fluctuate."],
    }


OPERATIONS = {
    "emi": emi,
    "amortization": amortization,
    "sip_future_value": sip_future_value,
    "sip_change_impact": sip_change_impact,
    "required_sip": required_sip,
    "compound_growth": lumpsum_future_value,
    "inflation_adjust": inflation_adjust,
    "debt_payoff": debt_payoff,
    "emergency_fund": emergency_fund,
    "savings_rate": savings_rate,
    "debt_to_income": debt_to_income,
    "net_worth": net_worth,
}

OPERATION_PARAMS = {
    "emi": "principal, annual_rate, tenure_months",
    "amortization": "principal, annual_rate, tenure_months, [extra_monthly], [lump_sum, lump_sum_month]",
    "sip_future_value": "monthly_investment, annual_return, years, [annual_step_up]",
    "sip_change_impact": "current_monthly, change, annual_return, years, [annual_step_up]",
    "required_sip": "target_amount, annual_return, years",
    "compound_growth": "principal, annual_return, years, [compounding_per_year: 1|2|4|12|365]",
    "inflation_adjust": "amount, inflation_rate, years, [direction: future_cost|present_value]",
    "debt_payoff": "debts: [{name, balance, apr, min_payment}], monthly_budget, [strategy: avalanche|snowball]",
    "emergency_fund": "monthly_expenses, [months], [current_savings]",
    "savings_rate": "monthly_income, monthly_expenses",
    "debt_to_income": "monthly_debt_payments, monthly_income",
    "net_worth": "assets: {name: amount}, liabilities: {name: amount}",
}


def run_operation(operation: str, params: Dict[str, Any]) -> Dict[str, Any]:
    fn = OPERATIONS.get(operation)
    if fn is None:
        raise FinanceInputError(f"Unknown operation '{operation}'. Valid: {', '.join(OPERATIONS)}.")
    if not isinstance(params, dict):
        raise FinanceInputError("'params' must be an object.")
    import inspect

    sig = inspect.signature(fn)
    unknown = set(params) - set(sig.parameters)
    if unknown:
        raise FinanceInputError(f"Unknown parameter(s) for {operation}: {', '.join(sorted(unknown))}. "
                                f"Expected: {OPERATION_PARAMS[operation]}.")
    missing = [n for n, p in sig.parameters.items() if p.default is inspect._empty and n not in params]
    if missing:
        raise FinanceInputError(f"Missing parameter(s) for {operation}: {', '.join(missing)}. "
                                f"Expected: {OPERATION_PARAMS[operation]}.")
    return fn(**params)
