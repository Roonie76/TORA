"""Phase 4C — versioned income-tax engine and tax_calc tool."""
import asyncio
from datetime import date

import pytest

from backend.context import ContextBuilder, ToolContext
from backend.finance import FinanceInputError
from backend.finance.tax import TAX_RULES, compare_regimes, compute_tax, current_tax_year, slab_tax
from backend.tools import TaxCalcTool


@pytest.mark.parametrize("today, expected", [
    (date(2026, 9, 16), "2026-27"), (date(2026, 3, 31), "2025-26"), (date(2026, 4, 1), "2026-27"),
])
def test_current_tax_year(today, expected):
    assert current_tax_year(today) == expected


@pytest.mark.parametrize("ty", ["2025-26", "2026-27"])
class TestNewRegime:
    def test_salary_12_75_lakh_is_tax_free(self, ty):
        r = compute_tax(gross_salary=1275000, tax_year=ty)
        assert r["taxable_normal_income"] == 1200000
        assert r["slab_tax"] == 60000 and r["rebate"] == 60000 and r["total_tax"] == 0

    def test_marginal_relief_just_above_12_lakh(self, ty):
        r = compute_tax(gross_salary=1285000, tax_year=ty)
        assert r["rebate"] == 0
        assert r["rebate_marginal_relief"] == 51500
        assert r["total_tax"] == 10400          # 10,000 + 4% cess

    def test_marginal_relief_ends_near_12_7_lakh(self, ty):
        assert compute_tax(other_income=1270000, tax_year=ty)["rebate_marginal_relief"] == 500
        assert compute_tax(other_income=1271000, tax_year=ty)["rebate_marginal_relief"] == 0

    def test_13_lakh_taxable(self, ty):
        assert compute_tax(gross_salary=1375000, tax_year=ty)["total_tax"] == 78000

    def test_surcharge_and_its_marginal_relief(self, ty):
        r60 = compute_tax(other_income=6000000, tax_year=ty)
        assert r60["surcharge"] == 138000 and r60["total_tax"] == 1578720
        r505 = compute_tax(other_income=5050000, tax_year=ty)
        assert r505["surcharge_marginal_relief"] == 74500 and r505["total_tax"] == 1175200

    def test_new_regime_surcharge_capped_at_25_percent(self, ty):
        r = compute_tax(other_income=60000000, tax_year=ty)
        assert r["surcharge"] == pytest.approx(r["slab_tax"] * 0.25, abs=1)

    def test_old_regime_deductions_ignored(self, ty):
        r = compute_tax(gross_salary=1500000, deductions={"section_80c": 150000}, tax_year=ty)
        assert r["deductions_applied"] == {}
        assert any("ignored" in n for n in r["notes"])


class TestOldRegime:
    def test_salary_10_lakh_with_80c(self):
        r = compute_tax(gross_salary=1000000, regime="old", deductions={"section_80c": 150000})
        assert r["taxable_normal_income"] == 800000
        assert r["total_tax"] == 75400

    def test_deduction_caps(self):
        r = compute_tax(gross_salary=2000000, regime="old", deductions={
            "section_80c": 400000, "section_80d_self": 60000, "home_loan_interest": 350000})
        assert r["deductions_applied"] == {"section_80c": 150000, "section_80d_self": 25000,
                                           "home_loan_interest": 200000}

    def test_senior_80d_cap_higher(self):
        r = compute_tax(gross_salary=2000000, regime="old", age_category="senior",
                        deductions={"section_80d_self": 60000})
        assert r["deductions_applied"]["section_80d_self"] == 50000

    def test_rebate_at_5_lakh_without_marginal_relief(self):
        assert compute_tax(other_income=500000, regime="old")["total_tax"] == 0
        r = compute_tax(other_income=510000, regime="old")
        assert r["rebate"] == 0 and r["rebate_marginal_relief"] == 0
        assert r["total_tax"] == pytest.approx((12500 + 2000) * 1.04)

    @pytest.mark.parametrize("age, income, tax", [
        ("senior", 300000, 0), ("senior", 600000, (10000 + 20000) * 1.04),
        ("super_senior", 700000, 40000 * 1.04),
    ])
    def test_age_slabs(self, age, income, tax):
        assert compute_tax(other_income=income, regime="old", age_category=age)["total_tax"] == pytest.approx(tax)

    def test_old_regime_surcharge_37_percent_above_5_crore(self):
        r = compute_tax(other_income=60000000, regime="old")
        assert r["surcharge"] == pytest.approx(r["slab_tax"] * 0.37, abs=1)


class TestCapitalGains:
    def test_rebate_does_not_offset_capital_gains(self):
        r = compute_tax(other_income=600000, stcg_equity=200000, ltcg_equity=300000)
        assert r["total_income"] == 975000
        assert r["rebate"] == 10000
        assert r["capital_gains_tax"] == 61875
        assert r["total_tax"] == 64350

    def test_ltcg_exemption_and_unused_basic_exemption(self):
        r = compute_tax(ltcg_equity=500000)
        # 5L - 1.25L exempt = 3.75L, fully absorbed by the ₹4L basic exemption
        assert r["capital_gains_tax"] == 0 and r["total_tax"] == 0

    def test_equity_gains_surcharge_capped_at_15_percent(self):
        r = compute_tax(other_income=10000000, stcg_equity=30000000)
        normal_part = r["slab_tax"]
        expected = normal_part * 0.25 + 30000000 * 0.20 * 0.15
        assert r["surcharge"] == pytest.approx(expected, rel=1e-6)


class TestComparisonAndValidation:
    def test_compare_regimes(self):
        r = compare_regimes(gross_salary=1800000, deductions={
            "section_80c": 150000, "section_80d_self": 25000, "home_loan_interest": 200000})
        assert r["new_regime_tax"] == 150800 and r["old_regime_tax"] == 234000
        assert r["better_regime"] == "new" and r["saving"] == 83200

    def test_large_exemptions_can_favour_old_regime(self):
        r = compare_regimes(gross_salary=2000000, deductions={
            "section_80c": 150000, "section_80d_self": 25000, "home_loan_interest": 200000,
            "section_80ccd_1b": 50000, "section_80d_parents": 50000, "other": 600000})
        assert r["old_regime_tax"] == 91000 and r["new_regime_tax"] == 192400
        assert r["better_regime"] == "old"

    @pytest.mark.parametrize("kwargs", [
        {"gross_salary": 1000000, "tax_year": "2019-20"},
        {"gross_salary": 1000000, "regime": "flat"},
        {"gross_salary": 1000000, "age_category": "teen"},
        {"gross_salary": -5},
        {},
    ])
    def test_invalid(self, kwargs):
        with pytest.raises(FinanceInputError):
            compute_tax(**kwargs)

    def test_slab_breakdown_sums(self):
        tax, rows = slab_tax(1500000, TAX_RULES["2026-27"]["new"]["slabs"]["normal"])
        assert tax == sum(r["tax"] for r in rows) == 105000

    def test_2026_27_uses_new_act_labels(self):
        assert compute_tax(gross_salary=1, tax_year="2026-27")["rebate_section"] == "156"
        assert compute_tax(gross_salary=1, tax_year="2025-26")["rebate_section"] == "87A"


class TestTool:
    def test_tool_defaults_to_current_tax_year(self):
        r = asyncio.run(TaxCalcTool().run({"operation": "compute_tax", "params": {"gross_salary": 1500000}}))
        assert r.success and r.data["tax_year"] == current_tax_year()

    def test_tool_rejects_unknown_params(self):
        r = asyncio.run(TaxCalcTool().run({"operation": "compute_tax", "params": {"bonus_points": 1}}))
        assert not r.success and "Unknown parameter" in r.error

    def test_compare_ignores_regime_param(self):
        r = asyncio.run(TaxCalcTool().run({"operation": "compare_regimes",
                                           "params": {"gross_salary": 1500000, "regime": "old"}}))
        assert r.success and r.data["better_regime"] in ("new", "old")

    def test_rendered_in_system_message(self):
        r = asyncio.run(TaxCalcTool().run({"operation": "compare_regimes", "params": {"gross_salary": 1800000}}))
        ctx = ToolContext().add_result(tool_name="tax_calc", call_id="t", output=r.data)
        msgs = ContextBuilder(default_system_prompt="S").build(current_message="q", tool_context=ctx)
        assert "tax_calc' (compare_regimes)" in msgs[0]["content"]
        assert "new_regime:" in msgs[0]["content"]
        assert len(msgs) == 2


def test_tax_tool_accepts_flat_deductions_from_the_planner():
    """Live run T3: the model passed 80C / 80D / home-loan interest at the top level."""
    import asyncio
    from backend.tools import TaxCalcTool

    res = asyncio.run(TaxCalcTool().run({"operation": "compare_regimes", "params": {
        "gross_salary": 1800000, "section_80c": 150000, "section_80d_self": 25000,
        "home_loan_interest": 200000, "tax_year": "2026-27"}}))
    assert res.success, res.error
    assert res.data["new_regime"]["total_tax"] == 150800
    assert res.data["old_regime"]["total_tax"] == 234000

    alias = asyncio.run(TaxCalcTool().run({"operation": "compute_tax", "params": {
        "gross_salary": 1000000, "regime": "old", "deductions": [{"section": "80C", "amount": 150000}],
        "tax_year": "2026-27"}}))
    assert alias.success and alias.data["total_tax"] == 75400
