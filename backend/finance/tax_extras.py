"""
Phase 12 — the rest of individual income tax: HRA, house property, capital gains
on a single sale, advance tax and interest, the right ITR form, and a finder for
missed deductions / a better regime.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from .engine import FinanceInputError, _pos, _r, inr
from .tax import TAX_RULES, compute_tax, current_tax_year

# Cost Inflation Index (base 2001-02 = 100), CBDT notifications.
CII = {
    "2001-02": 100, "2002-03": 105, "2003-04": 109, "2004-05": 113, "2005-06": 117, "2006-07": 122,
    "2007-08": 129, "2008-09": 137, "2009-10": 148, "2010-11": 167, "2011-12": 184, "2012-13": 200,
    "2013-14": 220, "2014-15": 240, "2015-16": 254, "2016-17": 264, "2017-18": 272, "2018-19": 280,
    "2019-20": 289, "2020-21": 301, "2021-22": 317, "2022-23": 331, "2023-24": 348, "2024-25": 363,
    "2025-26": 376, "2026-27": 384,
}
GRANDFATHER_CUTOFF = date(2024, 7, 23)   # property bought before this: 20% with indexation option
EQUITY_GRANDFATHER_DATE = date(2018, 1, 31)
METRO_CITIES = ("mumbai", "delhi", "new delhi", "kolkata", "chennai")


def _fy_of(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def _parse_date(name: str, value: Any) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise FinanceInputError(f"'{name}' must be a date like 2024-05-31.")


# ─────────────────────────────────────────────────────────────── HRA

def hra_exemption(basic_monthly: float, hra_received_monthly: float, rent_paid_monthly: float,
                  metro: Optional[bool] = None, city: Optional[str] = None, da_monthly: float = 0.0,
                  months: int = 12) -> Dict[str, Any]:
    basic = _pos("basic_monthly", basic_monthly)
    hra = _pos("hra_received_monthly", hra_received_monthly, allow_zero=True)
    rent = _pos("rent_paid_monthly", rent_paid_monthly, allow_zero=True)
    da = _pos("da_monthly", da_monthly, allow_zero=True)
    n = int(_pos("months", months))
    if n > 12:
        raise FinanceInputError("'months' cannot exceed 12.")
    if metro is None:
        metro = bool(city) and city.strip().lower() in METRO_CITIES
    salary = (basic + da) * n
    a = hra * n
    b = max(0.0, rent * n - 0.10 * salary)
    c = (0.50 if metro else 0.40) * salary
    exempt = min(a, b, c)
    limiting = ["HRA received", "rent minus 10% of salary", f"{'50' if metro else '40'}% of salary"][[a, b, c].index(exempt)]
    notes = []
    if rent * n > 100000:
        notes.append("Annual rent is above ₹1 lakh: the landlord's PAN is needed to claim this.")
    if rent == 0:
        notes.append("No rent paid, so no HRA exemption.")
    return {
        "operation": "hra_exemption",
        "inputs": {"basic_monthly": basic, "hra_received_monthly": hra, "rent_paid_monthly": rent,
                   "metro": metro, "da_monthly": da, "months": n},
        "hra_received": _r(a),
        "rent_minus_10pct_salary": _r(b),
        "salary_cap": _r(c),
        "exempt_hra": _r(exempt),
        "taxable_hra": _r(a - exempt),
        "limited_by": limiting,
        "notes": notes + ["HRA exemption is available only in the old regime."],
        "summary": f"Exempt HRA {inr(exempt)} a year (limited by {limiting}); taxable HRA {inr(a - exempt)}.",
        "assumptions": [f"{n} months of rent; {'metro' if metro else 'non-metro'} city rate."],
    }


# ─────────────────────────────────────────────────────────────── house property

def house_property_income(self_occupied: bool = True, annual_rent: float = 0.0, municipal_tax: float = 0.0,
                          home_loan_interest: float = 0.0, construction_completed_within_5_years: bool = True,
                          regime: str = "old") -> Dict[str, Any]:
    if regime not in ("old", "new"):
        raise FinanceInputError("'regime' must be 'new' or 'old'.")
    rent = _pos("annual_rent", annual_rent, allow_zero=True)
    mtax = _pos("municipal_tax", municipal_tax, allow_zero=True)
    interest = _pos("home_loan_interest", home_loan_interest, allow_zero=True)
    notes = []
    if self_occupied:
        nav = 0.0
        std = 0.0
        if regime == "new":
            allowed = 0.0
            notes.append("The new regime allows no interest deduction for a self-occupied house.")
        else:
            cap = 200000.0 if construction_completed_within_5_years else 30000.0
            allowed = min(interest, cap)
            if interest > cap:
                notes.append(f"Interest above {inr(cap)} is not deductible for a self-occupied house.")
    else:
        nav = max(0.0, rent - mtax)
        std = 0.30 * nav
        allowed = interest
    income = nav - std - allowed
    set_off = income
    carry_forward = 0.0
    if income < 0:
        if regime == "old":
            set_off = max(income, -200000.0)
            carry_forward = -(income - set_off)
            if carry_forward:
                notes.append(f"{inr(carry_forward)} of the loss can be carried forward for 8 years.")
        else:
            set_off = 0.0
            notes.append("In the new regime this loss cannot be set off against salary or other income.")
    return {
        "operation": "house_property_income",
        "inputs": {"self_occupied": self_occupied, "annual_rent": rent, "municipal_tax": mtax,
                   "home_loan_interest": interest, "regime": regime},
        "net_annual_value": _r(nav),
        "standard_deduction_30pct": _r(std),
        "interest_allowed": _r(allowed),
        "income_from_house_property": _r(income),
        "amount_to_use_in_tax": _r(set_off),
        "loss_carried_forward": _r(carry_forward),
        "notes": notes,
        "summary": (f"Income from house property: {inr(income)}; {inr(set_off)} counts in this year's tax "
                    f"({regime} regime). Pass it to compute_tax as house_property_income."),
        "assumptions": ["Single property; municipal taxes actually paid; interest on a loan for buying or building it."],
    }


# ─────────────────────────────────────────────────────────────── capital gains

def capital_gains_tax(asset_type: str, purchase_date: Any, sale_date: Any, purchase_cost: float,
                      sale_value: float, transfer_expenses: float = 0.0, improvement_cost: float = 0.0,
                      fmv_on_31_jan_2018: Optional[float] = None, other_ltcg_this_year: float = 0.0,
                      slab_rate_percent: float = 30.0) -> Dict[str, Any]:
    """Gain and tax on one sale (resident individual, sales on or after 23 July 2024)."""
    kinds = ("listed_equity", "equity_fund", "property", "gold", "unlisted_shares", "debt_fund")
    if asset_type not in kinds:
        raise FinanceInputError(f"'asset_type' must be one of {', '.join(kinds)}.")
    bought = _parse_date("purchase_date", purchase_date)
    sold = _parse_date("sale_date", sale_date)
    if sold <= bought:
        raise FinanceInputError("'sale_date' must be after 'purchase_date'.")
    if sold < GRANDFATHER_CUTOFF:
        raise FinanceInputError("Only sales on or after 23 July 2024 are supported.")
    cost = _pos("purchase_cost", purchase_cost)
    sale = _pos("sale_value", sale_value)
    exp = _pos("transfer_expenses", transfer_expenses, allow_zero=True)
    improve = _pos("improvement_cost", improvement_cost, allow_zero=True)
    other_ltcg = _pos("other_ltcg_this_year", other_ltcg_this_year, allow_zero=True)
    slab = _pos("slab_rate_percent", slab_rate_percent, allow_zero=True)
    months_held = (sold.year - bought.year) * 12 + (sold.month - bought.month) - (1 if sold.day < bought.day else 0)
    equity = asset_type in ("listed_equity", "equity_fund")
    long_term_after = 12 if equity else 24
    notes: List[str] = []
    if asset_type == "debt_fund" and bought >= date(2023, 4, 1):
        term = "short"
        notes.append("Debt funds bought on or after 1 April 2023 are always taxed at slab rates.")
    else:
        term = "long" if months_held > long_term_after else "short"
    cost_basis = cost
    if equity and term == "long" and bought <= EQUITY_GRANDFATHER_DATE and fmv_on_31_jan_2018:
        fmv = _pos("fmv_on_31_jan_2018", fmv_on_31_jan_2018)
        cost_basis = max(cost, min(fmv, sale))
        notes.append("Cost raised to the 31 Jan 2018 market value (grandfathering).")
    gain = sale - exp - cost_basis - improve
    options = []
    if gain <= 0:
        tax = 0.0
        rate_label = "no tax (capital loss)"
        notes.append(f"Capital loss of {inr(-gain)} — it can be set off against gains "
                     + ("(long-term losses only against long-term gains)." if term == "long" else "."))
        taxable = 0.0
    elif term == "short":
        if equity:
            rate, rate_label = 0.20, "20% (short-term, STT-paid)"
        else:
            rate, rate_label = slab / 100, f"your slab rate ({slab:g}%)"
        taxable = gain
        tax = taxable * rate
    else:
        if equity:
            exemption_left = max(0.0, 125000.0 - other_ltcg)
            taxable = max(0.0, gain - exemption_left)
            tax = taxable * 0.125
            rate_label = "12.5% above the ₹1.25 lakh yearly exemption"
        else:
            taxable = gain
            tax = gain * 0.125
            rate_label = "12.5% without indexation"
            if asset_type == "property" and bought < GRANDFATHER_CUTOFF:
                buy_cii = CII.get(_fy_of(bought) if bought >= date(2001, 4, 1) else "2001-02")
                sell_cii = CII.get(_fy_of(sold))
                if buy_cii and sell_cii:
                    indexed_gain = sale - exp - (cost + improve) * sell_cii / buy_cii
                    alt_tax = max(0.0, indexed_gain) * 0.20
                    options = [{"method": "12.5% without indexation", "gain": _r(gain), "tax": _r(tax)},
                               {"method": "20% with indexation", "indexed_gain": _r(indexed_gain),
                                "tax": _r(alt_tax), "cii_purchase": buy_cii, "cii_sale": sell_cii}]
                    if alt_tax < tax:
                        tax, taxable, rate_label = alt_tax, max(0.0, indexed_gain), "20% with indexation (lower)"
                    notes.append("Property bought before 23 July 2024: the lower of the two methods applies.")
                    if bought < date(2001, 4, 1):
                        notes.append("Bought before April 2001: cost should be the 1 April 2001 fair value (not modelled).")
                else:
                    notes.append("Cost Inflation Index not available for these years; indexation option not computed.")
            if asset_type == "property":
                notes.append("Sections 54/54EC/54F can exempt the gain if you reinvest in a house or specified bonds.")
    cess_tax = tax * 1.04
    return {
        "operation": "capital_gains_tax",
        "inputs": {"asset_type": asset_type, "purchase_date": bought.isoformat(), "sale_date": sold.isoformat(),
                   "purchase_cost": cost, "sale_value": sale, "transfer_expenses": exp,
                   "improvement_cost": improve, "other_ltcg_this_year": other_ltcg},
        "holding_months": months_held,
        "term": term,
        "capital_gain": _r(gain),
        "taxable_gain": _r(taxable),
        "rate": rate_label,
        "tax_before_cess": _r(tax),
        "tax_with_cess": _r(cess_tax),
        "options": options,
        "tax_year": _fy_of(sold),
        "notes": notes,
        "summary": (f"{term.title()}-term {'gain' if gain > 0 else 'loss'} of {inr(abs(gain))}; tax "
                    f"{inr(cess_tax)} incl. 4% cess at {rate_label}."),
        "assumptions": ["Resident individual; surcharge not included; STT paid on listed equity; "
                        "not a substitute for the Income Tax Department's calculation."],
    }


# ─────────────────────────────────────────────────────────────── advance tax

_INSTALMENTS = ((6, 15, 0.15), (9, 15, 0.45), (12, 15, 0.75), (3, 15, 1.00))


def advance_tax_plan(estimated_tax: float, tds_expected: float = 0.0, paid_so_far: float = 0.0,
                     tax_year: Optional[str] = None, today: Optional[Any] = None,
                     senior_without_business: bool = False, presumptive: bool = False) -> Dict[str, Any]:
    ty = tax_year or current_tax_year()
    try:
        start = int(ty[:4])
    except ValueError:
        raise FinanceInputError("'tax_year' must look like 2026-27.")
    now = _parse_date("today", today) if today else date.today()
    total = _pos("estimated_tax", estimated_tax, allow_zero=True)
    tds = _pos("tds_expected", tds_expected, allow_zero=True)
    paid = _pos("paid_so_far", paid_so_far, allow_zero=True)
    liability = max(0.0, total - tds)
    if liability < 10000 or senior_without_business:
        reason = ("tax after TDS is below ₹10,000" if liability < 10000 else
                  "resident senior citizens without business income are exempt")
        return {"operation": "advance_tax_plan", "tax_year": ty, "liability_after_tds": _r(liability),
                "required": False, "schedule": [], "summary": f"No advance tax needed: {reason}.",
                "assumptions": ["Pay any balance as self-assessment tax before filing."]}
    schedule = []
    for month, day, share in _INSTALMENTS:
        if presumptive and share < 1.0:
            continue
        due = date(start + (1 if month <= 3 else 0), month, day)
        cumulative = liability * share
        schedule.append({"due_date": due.isoformat(), "cumulative_percent": int(share * 100),
                         "cumulative_amount": _r(cumulative),
                         "status": "past" if due < now else "upcoming"})
    past = [s for s in schedule if s["status"] == "past"]
    shortfall = max(0.0, past[-1]["cumulative_amount"] - paid) if past else 0.0
    next_due = next((s for s in schedule if s["status"] == "upcoming"), None)
    pay_now = max(0.0, (next_due["cumulative_amount"] if next_due else liability) - paid)
    # 234C (estimate): 1% a month for 3 months on the shortfall at the latest missed date (1 month for 15 March)
    interest_234c = 0.0
    if past and shortfall:
        interest_234c = shortfall * 0.01 * (1 if past[-1]["cumulative_percent"] == 100 else 3)
    notes = []
    if paid < 0.9 * liability and now > date(start + 1, 3, 31):
        notes.append("Less than 90% was paid by 31 March, so interest under section 234B (1% a month) also applies.")
    return {
        "operation": "advance_tax_plan",
        "tax_year": ty,
        "liability_after_tds": _r(liability),
        "required": True,
        "paid_so_far": _r(paid),
        "schedule": schedule,
        "shortfall_now": _r(shortfall),
        "pay_by_next_due_date": _r(pay_now),
        "next_due_date": next_due["due_date"] if next_due else None,
        "estimated_interest_234c": _r(interest_234c),
        "notes": notes,
        "summary": (f"Advance tax needed: {inr(liability)} for {ty}. "
                    + (f"Pay {inr(pay_now)} by {next_due['due_date']}." if next_due else
                       f"All instalment dates have passed; pay the balance {inr(max(0.0, liability - paid))} now.")
                    + (f" You are {inr(shortfall)} behind, so some interest applies." if shortfall else "")),
        "assumptions": ["Estimated tax for the year as given; interest shown is an estimate."],
    }


# ─────────────────────────────────────────────────────────────── ITR form

def itr_form_choice(total_income: float, has_salary: bool = True, house_properties: int = 0,
                    has_business_income: bool = False, presumptive_business: bool = False,
                    ltcg_112a: float = 0.0, other_capital_gains: float = 0.0, agricultural_income: float = 0.0,
                    is_director: bool = False, unlisted_shares: bool = False, foreign_assets: bool = False,
                    resident: bool = True, crypto_income: bool = False,
                    carried_forward_losses: bool = False) -> Dict[str, Any]:
    income = _pos("total_income", total_income, allow_zero=True)
    hp = int(_pos("house_properties", house_properties, allow_zero=True))
    reasons_not_itr1: List[str] = []
    if not resident:
        reasons_not_itr1.append("not a resident")
    if income > 5000000:
        reasons_not_itr1.append("total income above ₹50 lakh")
    if hp > 2:
        reasons_not_itr1.append("more than two house properties")
    if has_business_income:
        reasons_not_itr1.append("business or professional income")
    if other_capital_gains > 0 or ltcg_112a > 125000 or (ltcg_112a and carried_forward_losses):
        reasons_not_itr1.append("capital gains beyond ₹1.25 lakh of listed-equity LTCG")
    if agricultural_income > 5000:
        reasons_not_itr1.append("agricultural income above ₹5,000")
    if is_director:
        reasons_not_itr1.append("director in a company")
    if unlisted_shares:
        reasons_not_itr1.append("unlisted shares")
    if foreign_assets:
        reasons_not_itr1.append("foreign assets or income")
    if crypto_income:
        reasons_not_itr1.append("crypto / virtual digital asset income")
    if not reasons_not_itr1:
        form = "ITR-1"
    elif has_business_income:
        itr4_ok = (presumptive_business and resident and income <= 5000000 and hp <= 2 and not is_director
                   and not unlisted_shares and not foreign_assets and not crypto_income and other_capital_gains == 0
                   and ltcg_112a <= 125000 and agricultural_income <= 5000)
        form = "ITR-4" if itr4_ok else "ITR-3"
    else:
        form = "ITR-2"
    return {
        "operation": "itr_form_choice",
        "recommended_form": form,
        "why_not_itr1": reasons_not_itr1,
        "summary": f"Use {form}." + (f" Not ITR-1 because: {', '.join(reasons_not_itr1)}." if reasons_not_itr1 else
                                    " You meet all ITR-1 (Sahaj) conditions."),
        "assumptions": ["Rules for returns filed in 2026 (income of 2025-26); check the e-filing portal's form "
                        "selector before filing."],
    }


# ─────────────────────────────────────────────────────────────── missed savings

def tax_saving_finder(gross_salary: float, tax_year: Optional[str] = None, age_category: str = "normal",
                      section_80c: float = 0.0, section_80d_self: float = 0.0, section_80d_parents: float = 0.0,
                      section_80ccd_1b: float = 0.0, home_loan_interest: float = 0.0,
                      rent_paid_monthly: float = 0.0, basic_monthly: float = 0.0, hra_received_monthly: float = 0.0,
                      metro: bool = False, savings_interest: float = 0.0, other_income: float = 0.0,
                      current_regime: Optional[str] = None, hra_exempt_annual: float = 0.0) -> Dict[str, Any]:
    """Which regime is cheaper now, and what unused deductions could change that."""
    ty = tax_year or current_tax_year()
    if ty not in TAX_RULES:
        raise FinanceInputError(f"Tax rules for {ty} are not available.")
    caps = TAX_RULES[ty]["deduction_caps"]
    senior = age_category != "normal"
    hra_exempt = _pos("hra_exempt_annual", hra_exempt_annual, allow_zero=True)
    if not hra_exempt and rent_paid_monthly and basic_monthly and hra_received_monthly:
        hra_exempt = hra_exemption(basic_monthly, hra_received_monthly, rent_paid_monthly, metro=metro)["exempt_hra"]
    current = {"section_80c": section_80c, "section_80d_self": section_80d_self,
               "section_80d_parents": section_80d_parents, "section_80ccd_1b": section_80ccd_1b,
               "home_loan_interest": home_loan_interest, "savings_interest": savings_interest}
    base = dict(gross_salary=gross_salary, other_income=other_income, tax_year=ty, age_category=age_category)
    new_tax = compute_tax(regime="new", **base)["total_tax"]
    old_now = compute_tax(regime="old", deductions=current, hra_exempt=hra_exempt, **base)["total_tax"]
    opportunities = []

    def try_add(key: str, label: str, headroom: float, how: str):
        if headroom <= 0:
            return
        trial = dict(current)
        trial[key] = trial.get(key, 0.0) + headroom
        t = compute_tax(regime="old", deductions=trial, hra_exempt=hra_exempt, **base)["total_tax"]
        saving = old_now - t
        if saving > 0:
            opportunities.append({"deduction": label, "unused_limit": _r(headroom), "old_regime_tax_saving": _r(saving),
                                  "how": how})

    cap_80d = caps["section_80d_self"][1 if senior else 0]
    try_add("section_80c", "80C (now s.123)", caps["section_80c"] - min(section_80c, caps["section_80c"]),
            "PPF, ELSS, EPF top-up, life cover premium, children's tuition, home-loan principal")
    try_add("section_80ccd_1b", "NPS own contribution 80CCD(1B) (now s.124)",
            caps["section_80ccd_1b"] - min(section_80ccd_1b, caps["section_80ccd_1b"]), "Invest in NPS Tier-I")
    try_add("section_80d_self", "Health insurance 80D (now s.126)", cap_80d - min(section_80d_self, cap_80d),
            "Buy a family health policy (also protects your finances)")
    try_add("section_80d_parents", "Parents' health insurance 80D", 25000 - min(section_80d_parents, 25000),
            "Pay your parents' health premium (limit ₹50,000 if they are senior citizens)")
    full = {k: v for k, v in current.items()}
    full["section_80c"] = max(section_80c, caps["section_80c"])
    full["section_80ccd_1b"] = max(section_80ccd_1b, caps["section_80ccd_1b"])
    full["section_80d_self"] = max(section_80d_self, cap_80d)
    old_full = compute_tax(regime="old", deductions=full, hra_exempt=hra_exempt, **base)["total_tax"]
    better_now = "new" if new_tax <= old_now else "old"
    better_if_maxed = "new" if new_tax <= old_full else "old"
    tips = []
    if rent_paid_monthly and not hra_received_monthly:
        tips.append("You pay rent but get no HRA: ask your employer about HRA, or check the old-regime rent "
                    "deduction (s.80GG) with a tax professional.")
    if current_regime and current_regime != better_now:
        tips.append(f"You are on the {current_regime} regime; the {better_now} regime is cheaper for you by "
                    f"{inr(abs(new_tax - old_now))} this year.")
    return {
        "operation": "tax_saving_finder",
        "tax_year": ty,
        "new_regime_tax": _r(new_tax),
        "old_regime_tax_now": _r(old_now),
        "old_regime_tax_if_all_limits_used": _r(old_full),
        "hra_exempt_used": _r(hra_exempt),
        "better_regime_now": better_now,
        "better_regime_if_limits_used": better_if_maxed,
        "opportunities": sorted(opportunities, key=lambda o: -o["old_regime_tax_saving"]),
        "tips": tips,
        "summary": (f"Now: new regime {inr(new_tax)} vs old {inr(old_now)} — {better_now} is cheaper. "
                    f"Using every listed deduction would bring the old regime to {inr(old_full)}"
                    + (", still more than the new regime." if better_if_maxed == "new" else
                       f", which beats the new regime by {inr(new_tax - old_full)}.")),
        "assumptions": ["Savings shown are for the old regime; the new regime ignores these deductions.",
                        "Professional tax and other small deductions are not included.",
                        "Only invest for tax if the product also suits your goals."],
    }


EXTRA_TAX_OPERATIONS = {
    "hra_exemption": hra_exemption,
    "house_property_income": house_property_income,
    "capital_gains_tax": capital_gains_tax,
    "advance_tax_plan": advance_tax_plan,
    "itr_form_choice": itr_form_choice,
    "tax_saving_finder": tax_saving_finder,
}
EXTRA_TAX_PARAMS = {
    "hra_exemption": "basic_monthly, hra_received_monthly, rent_paid_monthly, [metro] or [city], [da_monthly], [months]",
    "house_property_income": "[self_occupied], [annual_rent], [municipal_tax], [home_loan_interest], "
                             "[construction_completed_within_5_years], [regime]",
    "capital_gains_tax": "asset_type: listed_equity|equity_fund|property|gold|unlisted_shares|debt_fund, purchase_date, "
                         "sale_date (YYYY-MM-DD), purchase_cost, sale_value, [transfer_expenses], [improvement_cost], "
                         "[fmv_on_31_jan_2018], [other_ltcg_this_year], [slab_rate_percent]",
    "advance_tax_plan": "estimated_tax, [tds_expected], [paid_so_far], [tax_year], [senior_without_business], [presumptive]",
    "itr_form_choice": "total_income, [has_salary], [house_properties], [has_business_income], [presumptive_business], "
                       "[ltcg_112a], [other_capital_gains], [agricultural_income], [is_director], [unlisted_shares], "
                       "[foreign_assets], [resident], [crypto_income]",
    "tax_saving_finder": "gross_salary, [tax_year], [age_category], [section_80c], [section_80d_self], "
                         "[section_80d_parents], [section_80ccd_1b], [home_loan_interest], [rent_paid_monthly], "
                         "[basic_monthly], [hra_received_monthly], [metro], [savings_interest], [other_income], "
                         "[current_regime], [hra_exempt_annual]",
}


def _register() -> None:
    from . import tax

    tax.TAX_OPERATIONS.update(EXTRA_TAX_OPERATIONS)


_register()


# ── presumptive taxation (sections 44AD / 44ADA) ─────────────────────────────

PRESUMPTIVE_RULE_IDS = {
    "business": "presumptive-44ad-business",
    "profession": "presumptive-44ada-profession",
}


def _presumptive_figures(kind: str) -> Dict[str, Any]:
    """
    The thresholds, read from the reviewed rules library rather than written here.

    Hardcoding them would give two places for a limit to live, and the one nobody
    updates is the one that answers. The library already owns the citation, the
    source URL and the staleness check, so the engine reads the same row the
    answer cites — they cannot drift apart.
    """
    from ..knowledge import get_library

    rule = get_library().get(PRESUMPTIVE_RULE_IDS[kind])
    if rule is None or not rule.figures:
        raise FinanceInputError(f"No reviewed rule found for presumptive {kind} income.")
    return dict(rule.figures)


def presumptive_income(receipts: float, kind: str = "profession", cash_receipts: float = 0.0,
                       digital_receipts: Optional[float] = None,
                       tax_year: Optional[str] = None) -> Dict[str, Any]:
    """
    Whether a small business or professional may declare income presumptively,
    and what that income would be.

    Answers the eligibility question first, because it is the one that decides
    everything after it: above the limit the scheme simply does not apply, and a
    deemed profit computed anyway would be a confident number for a scheme the
    person cannot use.
    """
    kind = str(kind or "profession").strip().lower()
    if kind in ("professional", "profession", "44ada"):
        kind = "profession"
    elif kind in ("business", "trade", "44ad"):
        kind = "business"
    else:
        raise FinanceInputError("'kind' must be either 'business' or 'profession'.")

    total = _pos("receipts", receipts)
    cash = _pos("cash_receipts", cash_receipts, allow_zero=True)
    if cash > total:
        raise FinanceInputError("'cash_receipts' cannot exceed total receipts.")

    f = _presumptive_figures(kind)
    threshold_pct = float(f["low_cash_threshold_percent"])
    cash_share = (cash / total * 100.0) if total else 0.0
    low_cash = cash_share <= threshold_pct

    if kind == "business":
        base_limit = float(f["turnover_limit"])
        high_limit = float(f["turnover_limit_low_cash"])
        label, section = "turnover", "44AD"
    else:
        base_limit = float(f["gross_receipts_limit"])
        high_limit = float(f["gross_receipts_limit_low_cash"])
        label, section = "gross receipts", "44ADA"

    limit = high_limit if low_cash else base_limit
    eligible = total <= limit

    # For a business the rate depends on how the money arrived; for a profession
    # it is a flat 50%.
    if kind == "business":
        digital = total - cash if digital_receipts is None else _pos("digital_receipts", digital_receipts, allow_zero=True)
        digital = min(max(digital, 0.0), total)
        cash_part = total - digital
        pct_digital = float(f["deemed_profit_percent_digital"])
        pct_cash = float(f["deemed_profit_percent"])
        deemed = digital * pct_digital / 100.0 + cash_part * pct_cash / 100.0
        rate_note = (f"{pct_digital:g}% on {inr(digital)} received digitally and "
                     f"{pct_cash:g}% on {inr(cash_part)} received in cash")
        effective_pct = round(deemed / total * 100.0, 2) if total else 0.0
    else:
        pct = float(f["deemed_profit_percent"])
        deemed = total * pct / 100.0
        rate_note = f"{pct:g}% of gross receipts"
        effective_pct = pct

    if eligible:
        summary = (f"You can declare income under section {section}: {rate_note}, "
                   f"so {inr(deemed)} would be your taxable business income. "
                   f"No books of account and no audit.")
    else:
        summary = (f"Section {section} does not apply — your {label} of {inr(total)} is above the "
                   f"{inr(limit)} limit" +
                   (f" (the higher limit applies because cash is {cash_share:.1f}% of receipts)."
                    if low_cash else
                    f". Keeping cash receipts at or below {threshold_pct:g}% would raise the limit to "
                    f"{inr(high_limit)}."))

    return {
        "operation": "presumptive_income",
        "kind": kind,
        "section": section,
        "inputs": {"receipts": total, "cash_receipts": cash, "tax_year": tax_year},
        "eligible": eligible,
        "limit_applied": limit,
        "base_limit": base_limit,
        "low_cash_limit": high_limit,
        "cash_share_percent": round(cash_share, 2),
        "low_cash_condition_met": low_cash,
        "deemed_income": round(deemed, 2) if eligible else None,
        "deemed_income_formatted": inr(deemed) if eligible else None,
        "effective_rate_percent": effective_pct if eligible else None,
        "summary": summary,
        "assumptions": [
            "Presumptive income is the whole taxable business income; no further expense deduction is allowed.",
            "Eligibility also depends on who you are: section 44AD is not available to a non-resident, an LLP, "
            "an agency business, or income by way of commission or brokerage.",
        ],
    }


EXTRA_TAX_OPERATIONS["presumptive_income"] = presumptive_income
EXTRA_TAX_PARAMS["presumptive_income"] = (
    "receipts, [kind: business|profession], [cash_receipts], [digital_receipts], [tax_year]"
)
