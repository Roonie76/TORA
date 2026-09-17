"""
Versioned Indian individual income-tax engine (Phase 4C).

Rules are data, keyed by financial year / tax year ("2026-27" = 1 Apr 2026 – 31 Mar 2027).
Sources checked Sept 2026: Union Budget 2026-27 made no change to slabs, rebate,
standard deduction or surcharge; the Income Tax Act, 2025 applies from 1 Apr 2026
(rebate now s.156, new regime s.202). Capital-gains rates: STCG on STT-paid listed
equity 20%, LTCG on listed equity 12.5% above ₹1.25 lakh, other LTCG 12.5% (no indexation).

Scope: resident individuals. Not modelled: HUFs/firms, agricultural income, AMT,
set-off of losses, property indexation grandfathering, crypto/VDA, lottery income.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .engine import FinanceInputError, _pos, _r, inr

INF = float("inf")

_NEW_REGIME_SLABS_FY25 = [(400000, 0.0), (800000, 0.05), (1200000, 0.10), (1600000, 0.15),
                          (2000000, 0.20), (2400000, 0.25), (INF, 0.30)]
_OLD_SLABS = [(250000, 0.0), (500000, 0.05), (1000000, 0.20), (INF, 0.30)]
_OLD_SLABS_SENIOR = [(300000, 0.0), (500000, 0.05), (1000000, 0.20), (INF, 0.30)]
_OLD_SLABS_SUPER_SENIOR = [(500000, 0.0), (1000000, 0.20), (INF, 0.30)]
_SURCHARGE = [(5000000, 0.10), (10000000, 0.15), (20000000, 0.25), (50000000, 0.37)]

_COMMON_RULES = {
    "cess": 0.04,
    "surcharge": _SURCHARGE,            # (income above, rate)
    "surcharge_cap_new_regime": 0.25,
    "surcharge_cap_capital_gains": 0.15,  # on STCG/LTCG on listed equity
    "capital_gains": {"stcg_equity": 0.20, "ltcg_equity": 0.125, "ltcg_equity_exemption": 125000,
                      "ltcg_other": 0.125},
    "new": {
        "slabs": {"normal": _NEW_REGIME_SLABS_FY25, "senior": _NEW_REGIME_SLABS_FY25,
                  "super_senior": _NEW_REGIME_SLABS_FY25},
        "standard_deduction": 75000,
        "rebate_limit": 1200000,
        "rebate_max": 60000,
        "rebate_marginal_relief": True,
        "deductions_allowed": {"employer_nps"},
    },
    "old": {
        "slabs": {"normal": _OLD_SLABS, "senior": _OLD_SLABS_SENIOR, "super_senior": _OLD_SLABS_SUPER_SENIOR},
        "standard_deduction": 50000,
        "rebate_limit": 500000,
        "rebate_max": 12500,
        "rebate_marginal_relief": False,
        "deductions_allowed": {"section_80c", "section_80d_self", "section_80d_parents", "section_80ccd_1b",
                               "home_loan_interest", "savings_interest", "employer_nps", "other"},
    },
    "deduction_caps": {
        "section_80c": 150000,
        "section_80ccd_1b": 50000,
        "home_loan_interest": 200000,       # self-occupied property
        "section_80d_self": (25000, 50000),   # (below 60, senior)
        "section_80d_parents": 50000,       # caller passes eligible amount; max 50k if parents senior
        "savings_interest": (10000, 50000),   # 80TTA / 80TTB(senior)
    },
}

TAX_RULES: Dict[str, Dict[str, Any]] = {
    "2025-26": {**_COMMON_RULES, "act": "Income-tax Act, 1961",
                "rebate_section": "87A", "filing_due_date": "2026-07-31"},
    "2026-27": {**_COMMON_RULES, "act": "Income Tax Act, 2025",
                "rebate_section": "156", "filing_due_date": "2027-07-31 (expected; confirm when notified)"},
}

AGE_CATEGORIES = ("normal", "senior", "super_senior")


def current_tax_year(today: Optional[date] = None) -> str:
    d = today or date.today()
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def _rules(tax_year: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    ty = tax_year or current_tax_year()
    if ty not in TAX_RULES:
        raise FinanceInputError(
            f"Tax rules for {ty} are not available. Supported: {', '.join(sorted(TAX_RULES))}."
        )
    return ty, TAX_RULES[ty]


def slab_tax(income: float, slabs: List[Tuple[float, float]]) -> Tuple[float, List[Dict[str, Any]]]:
    tax = 0.0
    lower = 0.0
    rows = []
    for upper, rate in slabs:
        if income <= lower:
            break
        portion = min(income, upper) - lower
        t = portion * rate
        tax += t
        if portion > 0:
            rows.append({"from": lower, "to": None if upper == INF else upper, "rate_percent": rate * 100,
                         "taxable_amount": _r(portion), "tax": _r(t)})
        lower = upper
    return tax, rows


def _surcharge_rate(total_income: float, table, cap: float) -> float:
    rate = 0.0
    for above, r in table:
        if total_income > above:
            rate = r
    return min(rate, cap)


def _threshold_below(total_income: float, table) -> Optional[float]:
    t = None
    for above, _ in table:
        if total_income > above:
            t = above
    return t


def compute_tax(
    gross_salary: float = 0.0,
    other_income: float = 0.0,
    regime: str = "new",
    tax_year: Optional[str] = None,
    age_category: str = "normal",
    deductions: Optional[Dict[str, float]] = None,
    stcg_equity: float = 0.0,
    ltcg_equity: float = 0.0,
    ltcg_other: float = 0.0,
    hra_exempt: float = 0.0,
    house_property_income: float = 0.0,
    _return_internal: bool = False,
) -> Dict[str, Any]:
    """Tax for a resident individual for one tax year and regime."""
    if regime not in ("new", "old"):
        raise FinanceInputError("'regime' must be 'new' or 'old'.")
    if age_category not in AGE_CATEGORIES:
        raise FinanceInputError(f"'age_category' must be one of {', '.join(AGE_CATEGORIES)}.")
    ty, rules = _rules(tax_year)
    reg = rules[regime]
    salary = _pos("gross_salary", gross_salary, allow_zero=True)
    other = _pos("other_income", other_income, allow_zero=True)
    stcg = _pos("stcg_equity", stcg_equity, allow_zero=True)
    ltcg_eq = _pos("ltcg_equity", ltcg_equity, allow_zero=True)
    ltcg_ot = _pos("ltcg_other", ltcg_other, allow_zero=True)
    hra = _pos("hra_exempt", hra_exempt, allow_zero=True)
    try:
        hp = float(house_property_income or 0.0)
    except (TypeError, ValueError):
        raise FinanceInputError("'house_property_income' must be a number (negative for a loss).")
    if salary + other + stcg + ltcg_eq + ltcg_ot + max(hp, 0.0) <= 0:
        raise FinanceInputError("Provide at least one income amount.")

    notes: List[str] = []
    if hra:
        if regime == "old":
            salary = max(0.0, salary - min(hra, salary))
        else:
            notes.append("HRA exemption is not available in the new regime and was ignored.")
    if hp < 0:
        if regime == "old":
            if hp < -200000:
                notes.append("House-property loss set off against other income is limited to ₹2,00,000; "
                             "the rest can be carried forward.")
            hp = max(hp, -200000.0)
        else:
            notes.append("A house-property loss cannot be set off against other income in the new regime.")
            hp = 0.0
    # 1. Normal (slab-rate) income
    std = min(reg["standard_deduction"], salary) if salary else 0.0
    caps = rules["deduction_caps"]
    senior = age_category != "normal"
    applied: Dict[str, float] = {}
    ignored: List[str] = []
    for key, raw in (deductions or {}).items():
        amount = _pos(f"deductions.{key}", raw, allow_zero=True)
        if key not in reg["deductions_allowed"]:
            if amount:
                ignored.append(key)
            continue
        cap = caps.get(key)
        if isinstance(cap, tuple):
            cap = cap[1] if senior else cap[0]
        applied[key] = min(amount, cap) if cap is not None else amount
    if ignored:
        notes.append(f"Not allowed under the {regime} regime and ignored: {', '.join(sorted(ignored))}.")
    total_deductions = sum(applied.values())
    normal_income = max(0.0, salary - std + other + hp - total_deductions)

    # 2. Special-rate income; unused basic exemption can absorb it (residents)
    cg = rules["capital_gains"]
    ltcg_eq_taxable = max(0.0, ltcg_eq - cg["ltcg_equity_exemption"])
    basic_exemption = reg["slabs"][age_category][0][0]
    shortfall = max(0.0, basic_exemption - normal_income)
    stcg_taxable = stcg
    for name in ("stcg", "ltcg_eq", "ltcg_ot"):
        if shortfall <= 0:
            break
        if name == "stcg":
            use = min(shortfall, stcg_taxable); stcg_taxable -= use
        elif name == "ltcg_eq":
            use = min(shortfall, ltcg_eq_taxable); ltcg_eq_taxable -= use
        else:
            use = min(shortfall, ltcg_ot); ltcg_ot -= use
        shortfall -= use
    special_tax = (stcg_taxable * cg["stcg_equity"] + ltcg_eq_taxable * cg["ltcg_equity"]
                   + ltcg_ot * cg["ltcg_other"])
    equity_cg_tax = stcg_taxable * cg["stcg_equity"] + ltcg_eq_taxable * cg["ltcg_equity"]
    total_income = normal_income + stcg + max(0.0, ltcg_eq - cg["ltcg_equity_exemption"]) + ltcg_other

    # 3. Slab tax and rebate (rebate never offsets special-rate tax)
    slabs = reg["slabs"][age_category]
    normal_tax, breakdown = slab_tax(normal_income, slabs)
    rebate = 0.0
    marginal_relief_rebate = 0.0
    if total_income <= reg["rebate_limit"]:
        rebate = min(normal_tax, reg["rebate_max"])
    elif reg["rebate_marginal_relief"]:
        excess = total_income - reg["rebate_limit"]
        if normal_tax > excess:
            marginal_relief_rebate = normal_tax - excess
    tax_after_rebate = normal_tax - rebate - marginal_relief_rebate
    base_tax = tax_after_rebate + special_tax

    # 4. Surcharge (capped for equity capital gains and under the new regime) + marginal relief
    table = rules["surcharge"]
    cap = rules["surcharge_cap_new_regime"] if regime == "new" else 1.0
    rate = _surcharge_rate(total_income, table, cap)
    cg_rate = min(rate, rules["surcharge_cap_capital_gains"])
    surcharge = (base_tax - equity_cg_tax) * rate + equity_cg_tax * cg_rate
    surcharge_relief = 0.0
    threshold = _threshold_below(total_income, table)
    if surcharge > 0 and threshold is not None:
        # Tax (incl. surcharge) at the threshold, using the same income mix scaled to the threshold
        scale = threshold / total_income
        at_thr = compute_tax(
            gross_salary=salary * scale, other_income=other * scale, regime=regime, tax_year=ty,
            age_category=age_category, deductions={k: v * scale for k, v in applied.items()},
            stcg_equity=stcg * scale, ltcg_equity=ltcg_eq * scale if ltcg_eq else 0.0,
            ltcg_other=ltcg_other * scale, hra_exempt=0.0, house_property_income=hp * scale,
            _return_internal=True,
        ) if scale < 1 else None
        if at_thr is not None:
            limit = at_thr["_tax_with_surcharge"] + (total_income - threshold)
            if base_tax + surcharge > limit:
                surcharge_relief = base_tax + surcharge - limit
    tax_with_surcharge = base_tax + surcharge - surcharge_relief
    cess = tax_with_surcharge * rules["cess"]
    total_tax = tax_with_surcharge + cess

    if _return_internal:
        return {"_tax_with_surcharge": tax_with_surcharge}

    gross_total = _pos("gross_salary", gross_salary, allow_zero=True) + other + max(hp, 0.0) + stcg + ltcg_eq + ltcg_other
    result = {
        "operation": "compute_tax",
        "tax_year": ty,
        "law": rules["act"],
        "regime": regime,
        "inputs": {"gross_salary": salary, "other_income": other, "age_category": age_category,
                   "deductions": deductions or {}, "stcg_equity": stcg, "ltcg_equity": ltcg_eq,
                   "ltcg_other": ltcg_other, "hra_exempt": hra, "house_property_income": hp},
        "standard_deduction": _r(std),
        "deductions_applied": {k: _r(v) for k, v in applied.items()},
        "taxable_normal_income": _r(normal_income),
        "total_income": _r(total_income),
        "slab_breakdown": breakdown,
        "slab_tax": _r(normal_tax),
        "rebate": _r(rebate),
        "rebate_section": rules["rebate_section"],
        "rebate_marginal_relief": _r(marginal_relief_rebate),
        "capital_gains_tax": _r(special_tax),
        "surcharge": _r(surcharge),
        "surcharge_marginal_relief": _r(surcharge_relief),
        "cess": _r(cess),
        "total_tax": _r(total_tax),
        "effective_rate_percent": _r(total_tax / gross_total * 100 if gross_total else 0, 2),
        "monthly_tax": _r(total_tax / 12),
        "notes": notes,
    }
    result["summary"] = (
        f"Tax year {ty}, {regime} regime: taxable income {inr(total_income)}, total tax {inr(total_tax)} "
        f"(about {inr(total_tax / 12)}/month, effective {result['effective_rate_percent']}% of gross income)."
    )
    result["assumptions"] = [
        "Resident individual; deductions as provided (capped at statutory limits).",
        "Rebate and marginal relief apply only to slab-rate income, not to capital gains.",
        "Not tax advice — confirm with the Income Tax Department's calculator or a tax professional.",
    ]
    return result


def compare_regimes(**kwargs: Any) -> Dict[str, Any]:
    kwargs.pop("regime", None)
    new = compute_tax(regime="new", **kwargs)
    old = compute_tax(regime="old", **kwargs)
    better = "new" if new["total_tax"] <= old["total_tax"] else "old"
    saving = abs(new["total_tax"] - old["total_tax"])
    return {
        "operation": "compare_regimes",
        "tax_year": new["tax_year"],
        "inputs": new["inputs"],
        "new_regime_tax": new["total_tax"],
        "old_regime_tax": old["total_tax"],
        "better_regime": better,
        "saving": _r(saving),
        "new_regime": {k: new[k] for k in ("taxable_normal_income", "slab_tax", "rebate", "rebate_marginal_relief",
                                           "surcharge", "cess", "total_tax")},
        "old_regime": {k: old[k] for k in ("taxable_normal_income", "deductions_applied", "slab_tax", "rebate",
                                           "surcharge", "cess", "total_tax")},
        "notes": new["notes"] + old["notes"],
        "summary": (f"Tax year {new['tax_year']}: new regime {inr(new['total_tax'])} vs old regime "
                    f"{inr(old['total_tax'])} — the {better} regime saves {inr(saving)}."),
        "assumptions": new["assumptions"],
    }


TAX_OPERATIONS = {"compute_tax": compute_tax, "compare_regimes": compare_regimes}
TAX_PARAMS = ("gross_salary, [other_income], [regime: new|old], [tax_year e.g. 2026-27], "
              "[age_category: normal|senior|super_senior], [deductions: {section_80c, section_80d_self, "
              "section_80d_parents, section_80ccd_1b, home_loan_interest, savings_interest, employer_nps}], "
              "[stcg_equity], [ltcg_equity], [ltcg_other]")
