"""
Phase 9 — CA-style option evaluation.

Each operation lays out the realistic options for a common decision, values
every option on the same basis (net worth at the same date, after tax where it
matters), recommends one, explains why, measures how close the call is
(break-even and confidence) and lists what the user should confirm.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .engine import FinanceInputError, _months, _pos, _r, _rate, inr

EQUITY_LTCG_EXEMPTION = 125000.0  # yearly exemption for listed equity / equity funds (tax year 2026-27)


def _monthly_rate(annual: float) -> float:
    return annual / 1200.0


def _emi(principal: float, annual_rate: float, months: int) -> float:
    i = _monthly_rate(annual_rate)
    if i == 0:
        return principal / months
    return principal * i * (1 + i) ** months / ((1 + i) ** months - 1)


def _loan_path(balance: float, annual_rate: float, payment: float, extra: float, lump: float,
               horizon: int) -> List[Dict[str, float]]:
    """Month-by-month loan; returns rows with interest paid and cash paid."""
    i = _monthly_rate(annual_rate)
    bal = max(0.0, balance - lump)
    rows = []
    for _ in range(horizon):
        if bal <= 0.005:
            rows.append({"interest": 0.0, "paid": 0.0, "balance": 0.0})
            continue
        interest = bal * i
        pay = min(bal + interest, payment + extra)
        bal = bal + interest - pay
        rows.append({"interest": interest, "paid": pay, "balance": max(0.0, bal)})
    return rows


def _after_tax(value: float, invested: float, tax_rate: float, exemption: float) -> float:
    gain = max(0.0, value - invested)
    taxable = max(0.0, gain - exemption)
    return value - taxable * tax_rate / 100.0


def _confidence(margin_pct: float, spread_points: float) -> str:
    """How firm the recommendation is: size of the win and how far the key assumption is from break-even."""
    if margin_pct >= 15 and spread_points >= 3:
        return "high"
    if margin_pct >= 5 and spread_points >= 1.5:
        return "medium"
    return "low"


def _bisect(fn: Callable[[float], float], lo: float, hi: float, iters: int = 60) -> Optional[float]:
    f_lo, f_hi = fn(lo), fn(hi)
    if f_lo == 0:
        return lo
    if f_lo * f_hi > 0:
        return None
    for _ in range(iters):
        mid = (lo + hi) / 2
        f_mid = fn(mid)
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


# ─────────────────────────────────────────────────────────────── prepay vs invest

def prepay_vs_invest(
    loan_balance: float,
    loan_rate: float,
    remaining_months: int,
    amount: float,
    amount_type: str = "monthly",
    expected_return: float = 12.0,
    return_tax_rate: float = 12.5,
    loan_tax_benefit_rate: float = 0.0,
    emergency_fund_ok: bool = True,
) -> Dict[str, Any]:
    """Use spare money to prepay a loan, invest it, or split it — valued at the original loan end date."""
    bal = _pos("loan_balance", loan_balance)
    rate = _rate("loan_rate", loan_rate, 60)
    n = _months("remaining_months", remaining_months)
    amt = _pos("amount", amount)
    if amount_type not in ("monthly", "lump_sum"):
        raise FinanceInputError("'amount_type' must be 'monthly' or 'lump_sum'.")
    ret = _rate("expected_return", expected_return, 30)
    tax = _rate("return_tax_rate", return_tax_rate, 45)
    benefit = _rate("loan_tax_benefit_rate", loan_tax_benefit_rate, 45)
    payment = _emi(bal, rate, n)
    monthly = amount_type == "monthly"
    exemption_total = EQUITY_LTCG_EXEMPTION if tax <= 15 else 0.0

    def value(share_prepay: float, r: float) -> Dict[str, float]:
        extra = amt * share_prepay if monthly else 0.0
        lump = amt * share_prepay if not monthly else 0.0
        path = _loan_path(bal, rate, payment, extra, lump, n)
        base_budget = payment + (amt if monthly else 0.0)
        i = _monthly_rate(r)
        corpus = amt * (1 - share_prepay) if not monthly else 0.0
        invested = corpus
        interest_paid = 0.0
        closed_at = None
        for m, row in enumerate(path, start=1):
            interest_paid += row["interest"]
            if closed_at is None and row["balance"] <= 0.005:
                closed_at = m
            spare = base_budget - row["paid"]  # money not needed for the loan this month
            corpus = corpus * (1 + i) + spare
            invested += spare
        # tax benefit on interest (e.g. home-loan interest deduction) lowers the real cost
        interest_after_benefit = interest_paid * (1 - benefit / 100.0)
        net_corpus = _after_tax(corpus, invested, tax, exemption_total)
        return {"corpus": net_corpus, "interest": interest_paid, "interest_after_benefit": interest_after_benefit,
                "closed_at": closed_at or n,
                "net_worth": net_corpus + (interest_paid - interest_after_benefit)}

    options = []
    for label, share in (("prepay", 1.0), ("invest", 0.0), ("split_50_50", 0.5)):
        v = value(share, ret)
        options.append({
            "option": label,
            "net_worth_at_loan_end": _r(v["net_worth"]),
            "loan_interest_paid": _r(v["interest"]),
            "loan_closes_in_months": v["closed_at"],
            "investment_value_after_tax": _r(v["corpus"]),
        })
    by_name = {o["option"]: o for o in options}
    best = max(options, key=lambda o: o["net_worth_at_loan_end"])
    effective_loan_rate = rate * (1 - benefit / 100.0)
    post_tax_return = ret * (1 - tax / 100.0)
    spread = abs(post_tax_return - effective_loan_rate)
    diff = by_name["invest"]["net_worth_at_loan_end"] - by_name["prepay"]["net_worth_at_loan_end"]
    base = max(1.0, abs(by_name["prepay"]["net_worth_at_loan_end"]))
    margin = 100 * abs(diff) / base
    breakeven = _bisect(lambda r: value(0.0, r)["net_worth"] - value(1.0, r)["net_worth"], 0.0, 30.0)
    confidence = _confidence(margin, spread)
    # A close call goes to the guaranteed option; no emergency fund means keep liquidity.
    if not emergency_fund_ok:
        recommended = "build_emergency_fund_first"
        reason = "Without an emergency fund, keep this money liquid first; prepayment cannot be withdrawn."
    elif confidence == "low" and best["option"] != "prepay":
        recommended = "split_50_50" if by_name["split_50_50"]["net_worth_at_loan_end"] >= by_name["prepay"]["net_worth_at_loan_end"] else "prepay"
        reason = (f"Investing is only slightly ahead and depends on earning {ret:g}% every year; "
                  "splitting keeps most of the upside with less risk.")
    else:
        recommended = best["option"]
        reason = ("Prepaying gives a guaranteed return equal to the loan rate, which beats the expected "
                  f"after-tax investment return ({post_tax_return:.1f}% vs {effective_loan_rate:.1f}%)."
                  if recommended == "prepay" else
                  f"The expected after-tax return ({post_tax_return:.1f}%) is well above the loan's effective "
                  f"cost ({effective_loan_rate:.1f}%).")
    return {
        "operation": "prepay_vs_invest",
        "inputs": {"loan_balance": bal, "loan_rate": rate, "remaining_months": n, "amount": amt,
                   "amount_type": amount_type, "expected_return": ret, "return_tax_rate": tax,
                   "loan_tax_benefit_rate": benefit, "emergency_fund_ok": emergency_fund_ok},
        "current_emi": _r(payment),
        "options": options,
        "recommended": recommended,
        "reason": reason,
        "confidence": confidence,
        "invest_minus_prepay": _r(diff),
        "breakeven_return": _r(breakeven, 2) if breakeven is not None else None,
        "effective_loan_rate": _r(effective_loan_rate),
        "post_tax_expected_return": _r(post_tax_return),
        "what_to_confirm": [
            "Prepayment charges on this loan (floating-rate home loans usually have none).",
            "Whether the loan interest is claimed as a tax deduction and in which regime.",
            "That an emergency fund of 3–6 months is already in place.",
            f"That {ret:g}% a year is a realistic long-term return for how the money would be invested.",
        ],
        "summary": (f"Valued at the original loan end ({n} months): prepay {inr(by_name['prepay']['net_worth_at_loan_end'])}, "
                    f"invest {inr(by_name['invest']['net_worth_at_loan_end'])}, split "
                    f"{inr(by_name['split_50_50']['net_worth_at_loan_end'])}. Recommended: {recommended.replace('_', ' ')} "
                    f"({confidence} confidence)."
                    + (f" Investing only wins if returns stay above {breakeven:.1f}% a year." if breakeven else "")),
        "assumptions": [
            "Loan EMI stays the same and prepayments shorten the tenure.",
            f"Investments earn a steady {ret:g}% a year, taxed at {tax:g}% on gains"
            + (f" above {inr(EQUITY_LTCG_EXEMPTION)}" if exemption_total else "") + " when sold at the end.",
            "After the loan closes, the freed EMI is invested at the same return.",
        ],
    }


# ─────────────────────────────────────────────────────────────── rent vs buy

def _simulate_rent_buy(price, rent0, lrate, horizon, dp_pct, tenure, appr, rent_up, inv, costs_pct, maint_pct, tax):
    """Equal-outflow comparison; `appr` may be negative (falling prices)."""
    down = price * dp_pct / 100
    upfront = down + price * costs_pct / 100
    loan = price - down
    emi_amt = _emi(loan, lrate, tenure) if loan > 0 else 0.0
    i_inv = _monthly_rate(inv)
    bal, rent, value = loan, rent0, price
    renter, renter_in = upfront, upfront   # the renter invests what the buyer paid up front
    owner = owner_in = 0.0
    total_rent, total_owner = 0.0, upfront
    for m in range(1, horizon + 1):
        interest = bal * _monthly_rate(lrate)
        pay = min(bal + interest, emi_amt) if bal > 0.005 else 0.0
        bal = max(0.0, bal + interest - pay)
        owner_cost = pay + value * maint_pct / 1200
        total_owner += owner_cost
        total_rent += rent
        renter *= 1 + i_inv
        owner *= 1 + i_inv
        if owner_cost > rent:
            renter += owner_cost - rent
            renter_in += owner_cost - rent
        else:
            owner += rent - owner_cost
            owner_in += rent - owner_cost
        if m % 12 == 0:
            rent *= 1 + rent_up / 100
            value *= 1 + appr / 100
    value *= (1 + appr / 100) ** ((horizon % 12) / 12)
    buy_nw = value - bal + _after_tax(owner, owner_in, tax, EQUITY_LTCG_EXEMPTION)
    rent_nw = _after_tax(renter, renter_in, tax, EQUITY_LTCG_EXEMPTION)
    return {"buy": buy_nw, "rent": rent_nw, "home_value": value, "loan_left": bal, "emi": emi_amt,
            "upfront": upfront, "total_rent": total_rent, "total_owner": total_owner}


def rent_vs_buy(
    property_price: float,
    monthly_rent: float,
    loan_rate: float,
    years: float = 20,
    down_payment_percent: float = 20.0,
    loan_tenure_years: Optional[float] = None,
    appreciation: float = 5.0,
    rent_increase: float = 5.0,
    investment_return: float = 10.0,
    buying_costs_percent: float = 7.0,
    maintenance_percent: float = 0.5,
    return_tax_rate: float = 12.5,
) -> Dict[str, Any]:
    """Net worth after `years` for buying with a loan vs renting and investing the difference."""
    price = _pos("property_price", property_price)
    rent0 = _pos("monthly_rent", monthly_rent)
    lrate = _rate("loan_rate", loan_rate, 30)
    yrs = _pos("years", years)
    horizon = int(round(yrs * 12))
    if horizon > 600:
        raise FinanceInputError("'years' cannot exceed 50.")
    dp_pct = _rate("down_payment_percent", down_payment_percent, 100)
    tenure_years = loan_tenure_years if loan_tenure_years is not None else min(yrs, 30)
    tenure = int(round(_pos("loan_tenure_years", tenure_years) * 12))
    appr = _rate("appreciation", appreciation, 30)
    rent_up = _rate("rent_increase", rent_increase, 30)
    inv = _rate("investment_return", investment_return, 30)
    costs_pct = _rate("buying_costs_percent", buying_costs_percent, 20)
    maint_pct = _rate("maintenance_percent", maintenance_percent, 10)
    tax = _rate("return_tax_rate", return_tax_rate, 45)
    args = (price, rent0, lrate, horizon, dp_pct, tenure)
    rest = (rent_up, inv, costs_pct, maint_pct, tax)
    sim = _simulate_rent_buy(*args, appr, *rest)
    diff = sim["buy"] - sim["rent"]
    recommended = "buy" if diff > 0 else "rent_and_invest"
    margin = 100 * abs(diff) / max(1.0, min(abs(sim["buy"]), abs(sim["rent"])))
    breakeven = _bisect(lambda a: (lambda r: r["buy"] - r["rent"])(_simulate_rent_buy(*args, a, *rest)), -10.0, 25.0)
    confidence = _confidence(margin, abs(appr - breakeven) if breakeven is not None else 5.0)
    return {
        "operation": "rent_vs_buy",
        "inputs": {"property_price": price, "monthly_rent": rent0, "loan_rate": lrate, "years": yrs,
                   "down_payment_percent": dp_pct, "loan_tenure_years": tenure / 12, "appreciation": appr,
                   "rent_increase": rent_up, "investment_return": inv, "buying_costs_percent": costs_pct,
                   "maintenance_percent": maint_pct, "return_tax_rate": tax},
        "upfront_cash_needed": _r(sim["upfront"]),
        "emi": _r(sim["emi"]),
        "first_month_cost_gap": _r(sim["emi"] + price * maint_pct / 1200 - rent0),
        "price_to_annual_rent": _r(price / (rent0 * 12), 1),
        "options": [
            {"option": "buy", "net_worth": _r(sim["buy"]), "home_value": _r(sim["home_value"]),
             "loan_left": _r(sim["loan_left"]), "total_paid": _r(sim["total_owner"])},
            {"option": "rent_and_invest", "net_worth": _r(sim["rent"]), "total_rent_paid": _r(sim["total_rent"])},
        ],
        "recommended": recommended,
        "difference": _r(diff),
        "confidence": confidence,
        "breakeven_appreciation": _r(breakeven, 2) if breakeven is not None else None,
        "reason": (f"After {yrs:g} years buying leaves you {inr(abs(diff))} "
                   f"{'better' if diff > 0 else 'worse'} off than renting and investing the difference."),
        "what_to_confirm": [
            "Realistic price growth for this exact locality (see the break-even growth rate).",
            "How long you will really stay — selling early adds another round of costs.",
            "Stamp duty, registration and brokerage in your state.",
            "That total EMIs stay under about 40% of take-home pay.",
            "Home-loan tax benefits (mainly under the old regime) are not included.",
        ],
        "summary": (f"Buy: net worth {inr(sim['buy'])} after {yrs:g} years; rent and invest: {inr(sim['rent'])}. "
                    f"Recommended: {recommended.replace('_', ' ')} ({confidence} confidence)."
                    + (f" Buying wins only if prices grow faster than {breakeven:.1f}% a year."
                       if breakeven is not None else "")),
        "assumptions": [
            f"Prices grow {appr:g}% and rent {rent_up:g}% a year; investments earn {inv:g}% a year.",
            f"Buying costs {costs_pct:g}% up front; maintenance {maint_pct:g}% of value a year; no selling costs.",
            "Whoever has the lower monthly cost invests the difference.",
        ],
    }


# ─────────────────────────────────────────────────────────────── loan tenure

def loan_tenure_choice(principal: float, loan_rate: float, short_years: float, long_years: float,
                       investment_return: float = 10.0, return_tax_rate: float = 12.5) -> Dict[str, Any]:
    """Shorter tenure (higher EMI) vs longer tenure while investing the EMI difference."""
    p = _pos("principal", principal)
    rate = _rate("loan_rate", loan_rate, 60)
    s = int(round(_pos("short_years", short_years) * 12))
    l = int(round(_pos("long_years", long_years) * 12))
    if s >= l:
        raise FinanceInputError("'short_years' must be less than 'long_years'.")
    inv = _rate("investment_return", investment_return, 30)
    tax = _rate("return_tax_rate", return_tax_rate, 45)
    emi_s, emi_l = _emi(p, rate, s), _emi(p, rate, l)
    i = _monthly_rate(inv)
    a = b = a_in = b_in = 0.0
    for m in range(1, l + 1):
        a *= 1 + i
        b *= 1 + i
        if m > s:  # short loan closed: invest the EMI it used to cost
            a += emi_s
            a_in += emi_s
        b += emi_s - emi_l  # long loan: invest the EMI difference every month
        b_in += emi_s - emi_l
    a_net = _after_tax(a, a_in, tax, EQUITY_LTCG_EXEMPTION)
    b_net = _after_tax(b, b_in, tax, EQUITY_LTCG_EXEMPTION)
    int_s, int_l = emi_s * s - p, emi_l * l - p
    diff = a_net - b_net
    margin = 100 * abs(diff) / max(1.0, min(a_net, b_net))
    confidence = _confidence(margin, abs(inv * (1 - tax / 100) - rate))
    recommended = "shorter_tenure" if diff >= 0 or confidence == "low" else "longer_tenure_and_invest"
    return {
        "operation": "loan_tenure_choice",
        "inputs": {"principal": p, "loan_rate": rate, "short_years": short_years, "long_years": long_years,
                   "investment_return": inv, "return_tax_rate": tax},
        "options": [
            {"option": "shorter_tenure", "emi": _r(emi_s), "total_interest": _r(int_s), "net_worth_at_end": _r(a_net)},
            {"option": "longer_tenure_and_invest", "emi": _r(emi_l), "total_interest": _r(int_l),
             "net_worth_at_end": _r(b_net)},
        ],
        "recommended": recommended,
        "confidence": confidence,
        "difference": _r(diff),
        "reason": ("The shorter tenure saves " + inr(int_l - int_s) + " of interest"
                   + (" and ends with more wealth." if diff >= 0 else
                      "; the longer tenure only wins if the investments reliably earn "
                      f"{inv:g}%, so the gap is too small to justify the risk." if recommended == "shorter_tenure" else
                      "; the longer tenure plus disciplined investing ends with more wealth.")),
        "what_to_confirm": ["That the higher EMI stays under about 40% of take-home pay.",
                            "That the EMI difference would really be invested every month."],
        "summary": (f"{short_years:g}-year EMI {inr(emi_s)} vs {long_years:g}-year EMI {inr(emi_l)}. After {long_years:g} "
                    f"years: shorter tenure {inr(a_net)}, longer + invest {inr(b_net)}. Recommended: "
                    f"{recommended.replace('_', ' ')} ({confidence} confidence)."),
        "assumptions": [f"Investments earn {inv:g}% a year, taxed at {tax:g}% on gains at the end.",
                        "Both options spend the same amount each month (the shorter EMI)."],
    }


# ─────────────────────────────────────────────────────────────── health check

def financial_health_check(
    monthly_income: float,
    monthly_expenses: float,
    emergency_savings: float = 0.0,
    monthly_emis: float = 0.0,
    high_interest_debt: float = 0.0,
    monthly_investments: float = 0.0,
    life_cover: Optional[float] = None,
    health_cover: Optional[float] = None,
    dependents: int = 0,
    age: Optional[int] = None,
) -> Dict[str, Any]:
    """Overall check-up with a score and the order in which to fix things."""
    inc = _pos("monthly_income", monthly_income)
    exp = _pos("monthly_expenses", monthly_expenses, allow_zero=True)
    ef = _pos("emergency_savings", emergency_savings, allow_zero=True)
    emis = _pos("monthly_emis", monthly_emis, allow_zero=True)
    hid = _pos("high_interest_debt", high_interest_debt, allow_zero=True)
    invest = _pos("monthly_investments", monthly_investments, allow_zero=True)
    deps = int(_pos("dependents", dependents, allow_zero=True))
    essentials = exp + emis
    areas = []
    actions = []

    def area(name, score, status, detail):
        areas.append({"area": name, "score": score, "status": status, "detail": detail})

    months_cover = ef / essentials if essentials else 0.0
    target_months = 6 if deps else 3
    s = min(20, round(20 * months_cover / target_months))
    area("emergency_fund", s, "ok" if months_cover >= target_months else "weak",
         f"{months_cover:.1f} months of expenses saved (target {target_months}).")
    if months_cover < target_months:
        actions.append((1, f"Build the emergency fund to {inr(target_months * essentials)} "
                           f"(gap {inr(max(0.0, target_months * essentials - ef))})."))

    area("high_interest_debt", 20 if hid == 0 else max(0, 20 - round(20 * hid / max(inc * 6, 1))),
         "ok" if hid == 0 else "weak",
         "No high-interest debt." if hid == 0 else f"{inr(hid)} of costly debt (cards, personal or app loans).")
    if hid:
        actions.append((2, f"Clear the {inr(hid)} of high-interest debt before new investing (use debt_rescue_plan)."))

    dti = 100 * emis / inc
    area("emi_burden", 20 if dti <= 30 else (12 if dti <= 40 else (6 if dti <= 50 else 0)),
         "ok" if dti <= 30 else ("stretched" if dti <= 40 else "weak"), f"EMIs take {dti:.0f}% of income.")
    if dti > 40:
        actions.append((2, f"EMIs take {dti:.0f}% of income; avoid new loans and look at restructuring the costliest ones."))

    surplus = inc - exp - emis
    rate = 100 * invest / inc
    area("saving_and_investing", min(20, round(rate)), "ok" if rate >= 15 else "weak",
         f"Investing {rate:.0f}% of income; {inr(max(0.0, surplus - invest))} a month is unallocated.")
    if rate < 15 and surplus > invest:
        actions.append((4, f"Invest regularly: aim for 15–20% of income ({inr(0.15 * inc)}+ a month) once the steps above are done."))

    ins_score = 20
    ins_detail = []
    if deps:
        need = inc * 12 * 10
        if life_cover is None:
            ins_score -= 10
            ins_detail.append("life cover not given")
            actions.append((3, f"Check term life cover — a common rule is about 10× annual income ({inr(need)})."))
        elif life_cover < need:
            ins_score -= 10
            ins_detail.append(f"life cover {inr(life_cover)} below about {inr(need)}")
            actions.append((3, f"Increase term life cover towards {inr(need)} (you have {inr(life_cover)})."))
    if health_cover is None:
        ins_score -= 5
        ins_detail.append("health cover not given")
        actions.append((3, "Make sure the family has its own health insurance, not only employer cover."))
    elif health_cover < 500000:
        ins_score -= 5
        ins_detail.append(f"health cover {inr(health_cover)} is low")
        actions.append((3, "Consider health cover of at least ₹5–10 lakh for the family."))
    area("insurance", max(0, ins_score), "ok" if ins_score == 20 else "weak",
         "; ".join(ins_detail) or "Adequate cover reported.")

    total = sum(a["score"] for a in areas)
    grade = "strong" if total >= 80 else ("fair" if total >= 55 else "needs attention")
    ordered = [text for _, text in sorted(actions, key=lambda x: x[0])]
    return {
        "operation": "financial_health_check",
        "inputs": {"monthly_income": inc, "monthly_expenses": exp, "emergency_savings": ef, "monthly_emis": emis,
                   "high_interest_debt": hid, "monthly_investments": invest, "life_cover": life_cover,
                   "health_cover": health_cover, "dependents": deps, "age": age},
        "score": total,
        "grade": grade,
        "areas": areas,
        "monthly_surplus": _r(surplus),
        "priorities": ordered,
        "summary": f"Financial health score {total}/100 ({grade}). "
                   + (f"First priority: {ordered[0]}" if ordered else "No urgent gaps found."),
        "assumptions": ["Rules of thumb: 3–6 months emergency fund, EMIs under 30–40% of income, "
                        "10× income life cover with dependents, 15–20% of income invested."],
    }


ADVISOR_OPERATIONS = {
    "prepay_vs_invest": prepay_vs_invest,
    "rent_vs_buy": rent_vs_buy,
    "loan_tenure_choice": loan_tenure_choice,
    "financial_health_check": financial_health_check,
}
ADVISOR_PARAMS = {
    "prepay_vs_invest": "loan_balance, loan_rate, remaining_months, amount, [amount_type: monthly|lump_sum], "
                        "[expected_return], [return_tax_rate], [loan_tax_benefit_rate], [emergency_fund_ok]",
    "rent_vs_buy": "property_price, monthly_rent, loan_rate, [years], [down_payment_percent], [loan_tenure_years], "
                   "[appreciation], [rent_increase], [investment_return], [buying_costs_percent], [maintenance_percent]",
    "loan_tenure_choice": "principal, loan_rate, short_years, long_years, [investment_return], [return_tax_rate]",
    "financial_health_check": "monthly_income, monthly_expenses, [emergency_savings], [monthly_emis], "
                              "[high_interest_debt], [monthly_investments], [life_cover], [health_cover], "
                              "[dependents], [age]",
}


def _register() -> None:
    from . import engine

    engine.OPERATIONS.update(ADVISOR_OPERATIONS)
    engine.OPERATION_PARAMS.update(ADVISOR_PARAMS)


_register()
