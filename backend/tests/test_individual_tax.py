"""Phase 12: HRA, house property, capital gains, advance tax, ITR form and tax-saving finder."""
import asyncio

import pytest

from backend.finance.engine import FinanceInputError
from backend.finance.tax import compute_tax
from backend.finance.tax_extras import (
    CII,
    advance_tax_plan,
    capital_gains_tax,
    hra_exemption,
    house_property_income,
    itr_form_choice,
    tax_saving_finder,
)
from backend.tools import TaxCalcTool


def test_hra_rules():
    r = hra_exemption(50000, 20000, 25000, city="Mumbai")
    assert (r["hra_received"], r["rent_minus_10pct_salary"], r["salary_cap"]) == (240000, 240000, 300000)
    assert r["exempt_hra"] == 240000 and r["taxable_hra"] == 0
    r2 = hra_exemption(50000, 20000, 15000, metro=False)
    assert r2["exempt_hra"] == 120000 and r2["limited_by"] == "rent minus 10% of salary"
    assert any("PAN" in n for n in r2["notes"])
    r3 = hra_exemption(30000, 25000, 40000, city="Pune")
    assert r3["exempt_hra"] == 144000 and r3["limited_by"] == "40% of salary"
    assert hra_exemption(50000, 20000, 0)["exempt_hra"] == 0
    with pytest.raises(FinanceInputError):
        hra_exemption(50000, 20000, 10000, months=13)


def test_house_property():
    let_out = house_property_income(self_occupied=False, annual_rent=300000, municipal_tax=10000,
                                    home_loan_interest=450000)
    assert let_out["standard_deduction_30pct"] == 87000 and let_out["income_from_house_property"] == -247000
    assert let_out["amount_to_use_in_tax"] == -200000 and let_out["loss_carried_forward"] == 47000
    new = house_property_income(self_occupied=False, annual_rent=300000, home_loan_interest=450000, regime="new")
    assert new["amount_to_use_in_tax"] == 0
    sop = house_property_income(home_loan_interest=320000)
    assert sop["interest_allowed"] == 200000 and sop["amount_to_use_in_tax"] == -200000
    assert house_property_income(home_loan_interest=320000, regime="new")["interest_allowed"] == 0
    assert house_property_income(home_loan_interest=320000,
                                 construction_completed_within_5_years=False)["interest_allowed"] == 30000


def test_compute_tax_uses_hra_and_house_property():
    r = compute_tax(gross_salary=1000000, regime="old", hra_exempt=120000, house_property_income=-250000,
                    tax_year="2026-27")
    assert r["taxable_normal_income"] == 630000 and r["total_tax"] == 40040
    assert any("limited to ₹2,00,000" in n for n in r["notes"])
    n = compute_tax(gross_salary=1000000, regime="new", hra_exempt=120000, house_property_income=-250000,
                    tax_year="2026-27")
    assert n["taxable_normal_income"] == 925000 and len(n["notes"]) == 2
    rent_income = compute_tax(gross_salary=1000000, house_property_income=203000, tax_year="2026-27")
    assert rent_income["taxable_normal_income"] == 1128000


def test_capital_gains_property_indexation_choice():
    r = capital_gains_tax("property", "2010-06-01", "2026-08-01", 3000000, 9000000)
    assert r["term"] == "long" and r["capital_gain"] == 6000000
    methods = {o["method"]: o for o in r["options"]}
    assert methods["20% with indexation"]["cii_purchase"] == CII["2010-11"] == 167
    assert methods["20% with indexation"]["cii_sale"] == 384
    assert r["tax_before_cess"] == pytest.approx(methods["20% with indexation"]["tax"])
    assert r["tax_with_cess"] == pytest.approx(437174, abs=1)
    recent = capital_gains_tax("property", "2024-08-01", "2026-09-01", 5000000, 6000000)
    assert recent["options"] == [] and recent["tax_before_cess"] == 125000


def test_capital_gains_equity_and_others():
    lt = capital_gains_tax("equity_fund", "2023-01-01", "2026-08-01", 300000, 600000)
    assert lt["taxable_gain"] == 175000 and lt["tax_with_cess"] == 22750
    lt2 = capital_gains_tax("equity_fund", "2023-01-01", "2026-08-01", 300000, 600000, other_ltcg_this_year=125000)
    assert lt2["taxable_gain"] == 300000
    st = capital_gains_tax("listed_equity", "2026-01-01", "2026-06-01", 100000, 150000)
    assert st["term"] == "short" and st["tax_before_cess"] == 10000
    loss = capital_gains_tax("listed_equity", "2026-01-01", "2026-08-01", 300000, 250000)
    assert loss["capital_gain"] == -50000 and loss["tax_with_cess"] == 0
    gf = capital_gains_tax("listed_equity", "2016-01-01", "2026-08-01", 100000, 500000, fmv_on_31_jan_2018=300000)
    assert gf["capital_gain"] == 200000
    debt = capital_gains_tax("debt_fund", "2023-06-01", "2026-08-01", 100000, 130000, slab_rate_percent=20)
    assert debt["term"] == "short" and debt["tax_before_cess"] == 6000
    gold = capital_gains_tax("gold", "2023-01-01", "2026-08-01", 100000, 200000)
    assert gold["term"] == "long" and gold["tax_before_cess"] == 12500
    with pytest.raises(FinanceInputError):
        capital_gains_tax("car", "2020-01-01", "2026-01-01", 1, 2)
    with pytest.raises(FinanceInputError):
        capital_gains_tax("gold", "2026-01-01", "2025-01-01", 1, 2)
    with pytest.raises(FinanceInputError):
        capital_gains_tax("gold", "2020-01-01", "2024-01-01", 1, 2)


def test_advance_tax():
    r = advance_tax_plan(150000, tds_expected=40000, paid_so_far=10000, tax_year="2026-27", today="2026-09-17")
    assert r["required"] and r["liability_after_tds"] == 110000
    assert [s["cumulative_amount"] for s in r["schedule"]] == [16500, 49500, 82500, 110000]
    assert r["shortfall_now"] == 39500 and r["pay_by_next_due_date"] == 72500
    assert r["next_due_date"] == "2026-12-15" and r["estimated_interest_234c"] == 1185
    assert not advance_tax_plan(45000, tds_expected=40000, tax_year="2026-27")["required"]
    assert not advance_tax_plan(90000, senior_without_business=True, tax_year="2026-27")["required"]
    pres = advance_tax_plan(90000, presumptive=True, tax_year="2026-27", today="2026-09-17")
    assert [s["due_date"] for s in pres["schedule"]] == ["2027-03-15"] and pres["shortfall_now"] == 0


def test_itr_form_choice():
    assert itr_form_choice(900000)["recommended_form"] == "ITR-1"
    assert itr_form_choice(900000, house_properties=2, ltcg_112a=100000)["recommended_form"] == "ITR-1"
    assert itr_form_choice(900000, ltcg_112a=200000)["recommended_form"] == "ITR-2"
    assert itr_form_choice(6000000)["recommended_form"] == "ITR-2"
    assert itr_form_choice(900000, has_business_income=True, presumptive_business=True)["recommended_form"] == "ITR-4"
    assert itr_form_choice(900000, has_business_income=True)["recommended_form"] == "ITR-3"
    assert itr_form_choice(900000, is_director=True)["why_not_itr1"] == ["director in a company"]


def test_tax_saving_finder():
    r = tax_saving_finder(1500000, section_80c=50000, rent_paid_monthly=30000, basic_monthly=60000,
                          hra_received_monthly=25000, metro=True, tax_year="2026-27")
    assert r["new_regime_tax"] == 97500 and r["better_regime_now"] == "new"
    assert r["opportunities"][0]["deduction"].startswith("80C") and r["opportunities"][0]["unused_limit"] == 100000
    assert r["old_regime_tax_if_all_limits_used"] < r["old_regime_tax_now"]
    heavy = tax_saving_finder(1200000, section_80c=150000, section_80d_self=25000, section_80ccd_1b=50000,
                              home_loan_interest=200000, rent_paid_monthly=0, tax_year="2026-27",
                              current_regime="old")
    assert heavy["better_regime_now"] == "new" and any("regime is cheaper" in t for t in heavy["tips"])
    assert [o["deduction"] for o in heavy["opportunities"]] == ["Parents' health insurance 80D"]
    tip = tax_saving_finder(1000000, rent_paid_monthly=20000, tax_year="2026-27")
    assert any("80GG" in t for t in tip["tips"])


def test_tool_dispatch_and_validation():
    t = TaxCalcTool()
    ok = asyncio.run(t.run({"operation": "itr_form_choice", "params": {"total_income": 900000}}))
    assert ok.success and ok.data["recommended_form"] == "ITR-1"
    missing = asyncio.run(t.run({"operation": "hra_exemption", "params": {"basic_monthly": 50000}}))
    assert not missing.success and "Missing parameter" in missing.error
    unknown = asyncio.run(t.run({"operation": "advance_tax_plan", "params": {"estimated_tax": 1, "foo": 2}}))
    assert not unknown.success and "Unknown parameter" in unknown.error
    assert "capital_gains_tax" in t.description
