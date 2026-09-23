"""
Presumptive taxation, sections 44AD and 44ADA.

This is the most dangerous kind of feature in TORA: a tax answer carries a legal
basis, so a wrong threshold ships as a wrong figure *with a citation attached*,
and the citation is exactly what makes it believable.

So the thresholds are not written in the engine. They live in the reviewed rules
library with their source URL and review date, and the engine reads the same row
the answer cites. The tests below check that arrangement as hard as they check
the arithmetic, because a limit that lives in two places will eventually differ
in two places.

Verified 2026-09-23 against incometaxindia.gov.in:
  44AD   8% of turnover, 6% on digital receipts; limit Rs 2 crore, Rs 3 crore
         where cash receipts are at most 5% of turnover.
  44ADA  50% of gross receipts; limit Rs 50 lakh, Rs 75 lakh where cash receipts
         are at most 5% (proviso inserted by Finance Act 2023, w.e.f. 1-4-2024).
"""

import pytest

from backend.finance.engine import FinanceInputError
from backend.finance.tax_extras import presumptive_income
from backend.knowledge import get_library

LAKH = 100000
CRORE = 10000000


class TestProfessionals44ADA:
    def test_half_of_gross_receipts_is_the_deemed_income(self):
        out = presumptive_income(receipts=40 * LAKH, kind="profession")
        assert out["eligible"] is True
        assert out["deemed_income"] == 20 * LAKH
        assert out["effective_rate_percent"] == 50

    def test_the_limit_is_fifty_lakh_when_cash_is_material(self):
        out = presumptive_income(receipts=60 * LAKH, kind="profession", cash_receipts=10 * LAKH)
        assert out["eligible"] is False
        assert out["limit_applied"] == 50 * LAKH

    def test_the_proviso_raises_it_to_seventy_five_lakh_on_low_cash(self):
        """Finance Act 2023, w.e.f. 1-4-2024. The whole point of the rule."""
        out = presumptive_income(receipts=70 * LAKH, kind="profession", cash_receipts=0)
        assert out["eligible"] is True
        assert out["limit_applied"] == 75 * LAKH
        assert out["deemed_income"] == 35 * LAKH

    def test_the_five_percent_boundary_is_inclusive(self):
        """Exactly 5% still qualifies: "does not exceed five per cent"."""
        at_limit = presumptive_income(receipts=70 * LAKH, kind="profession", cash_receipts=3.5 * LAKH)
        just_over = presumptive_income(receipts=70 * LAKH, kind="profession", cash_receipts=3.6 * LAKH)
        assert at_limit["low_cash_condition_met"] is True and at_limit["eligible"] is True
        assert just_over["low_cash_condition_met"] is False and just_over["eligible"] is False

    def test_above_the_limit_no_deemed_income_is_computed(self):
        """
        The scheme does not apply, so there is no figure. Computing 50% anyway
        would be a confident number for something the person cannot use.
        """
        out = presumptive_income(receipts=90 * LAKH, kind="profession")
        assert out["eligible"] is False
        assert out["deemed_income"] is None
        assert out["deemed_income_formatted"] is None

    def test_it_says_what_would_make_them_eligible(self):
        out = presumptive_income(receipts=60 * LAKH, kind="profession", cash_receipts=20 * LAKH)
        assert "5%" in out["summary"] or "5 %" in out["summary"]


class TestBusiness44AD:
    def test_digital_receipts_are_taxed_at_six_percent(self):
        out = presumptive_income(receipts=1 * CRORE, kind="business", cash_receipts=0)
        assert out["deemed_income"] == 6 * LAKH
        assert out["effective_rate_percent"] == 6

    def test_cash_receipts_are_taxed_at_eight_percent(self):
        out = presumptive_income(receipts=50 * LAKH, kind="business",
                                 cash_receipts=50 * LAKH, digital_receipts=0)
        assert out["deemed_income"] == 4 * LAKH
        assert out["effective_rate_percent"] == 8

    def test_a_mix_is_split_at_the_two_rates(self):
        out = presumptive_income(receipts=1 * CRORE, kind="business",
                                 cash_receipts=20 * LAKH, digital_receipts=80 * LAKH)
        assert out["deemed_income"] == pytest.approx(80 * LAKH * 0.06 + 20 * LAKH * 0.08)

    def test_the_limit_is_two_crore_then_three_on_low_cash(self):
        assert presumptive_income(receipts=2.5 * CRORE, kind="business",
                                  cash_receipts=0)["eligible"] is True
        blocked = presumptive_income(receipts=2.5 * CRORE, kind="business", cash_receipts=50 * LAKH)
        assert blocked["eligible"] is False
        assert blocked["limit_applied"] == 2 * CRORE

    def test_who_cannot_use_it_is_said_out_loud(self):
        """
        Eligibility is not only about size. An LLP or a commission agent under
        the limit still cannot use 44AD, and an answer that only checked the
        number would be wrong for them.
        """
        out = presumptive_income(receipts=50 * LAKH, kind="business")
        said = " ".join(out["assumptions"]).lower()
        assert "llp" in said and "commission" in said


class TestBadInput:
    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(FinanceInputError, match="business.*profession"):
            presumptive_income(receipts=10 * LAKH, kind="charity")

    def test_cash_above_total_is_refused(self):
        with pytest.raises(FinanceInputError, match="cannot exceed"):
            presumptive_income(receipts=10 * LAKH, cash_receipts=20 * LAKH)

    @pytest.mark.parametrize("alias,expected", [
        ("44ADA", "profession"), ("professional", "profession"),
        ("44AD", "business"), ("trade", "business"),
    ])
    def test_the_section_number_works_as_the_kind(self, alias, expected):
        assert presumptive_income(receipts=10 * LAKH, kind=alias)["kind"] == expected


class TestTheFiguresLiveInOnePlaceOnly:
    """
    The part that matters more than the arithmetic. A threshold written in the
    engine as well as the library gives a limit two homes, and the one nobody
    updates is the one that answers.
    """

    def test_the_engine_reads_the_library_not_its_own_constants(self):
        lib = get_library()
        prof = lib.get("presumptive-44ada-profession").figures
        biz = lib.get("presumptive-44ad-business").figures

        out = presumptive_income(receipts=10 * LAKH, kind="profession")
        assert out["base_limit"] == prof["gross_receipts_limit"]
        assert out["low_cash_limit"] == prof["gross_receipts_limit_low_cash"]

        out = presumptive_income(receipts=10 * LAKH, kind="business")
        assert out["base_limit"] == biz["turnover_limit"]
        assert out["low_cash_limit"] == biz["turnover_limit_low_cash"]

    def test_changing_the_rule_changes_the_answer(self, monkeypatch):
        """If the engine had its own copy, this would pass while being wrong."""
        lib = get_library()
        rule = lib.get("presumptive-44ada-profession")
        monkeypatch.setitem(rule.figures, "gross_receipts_limit_low_cash", 90 * LAKH)
        assert presumptive_income(receipts=85 * LAKH, kind="profession")["eligible"] is True

    def test_the_rules_carry_a_source_and_a_review_date(self):
        for rule_id in ("presumptive-44ad-business", "presumptive-44ada-profession"):
            rule = get_library().get(rule_id)
            assert rule.source_url.startswith("https://www.incometaxindia.gov.in")
            assert rule.verified_on
            assert rule.tax_years == ["2025-26"]

    def test_no_citation_is_offered_for_the_new_act(self):
        """
        The Income-tax Act, 2025 renumbers these from tax year 2026-27 and that
        mapping was not verified against a primary source. Scoping the rule to
        2025-26 is the honest alternative to guessing a section number — and a
        guessed section number is worse than none, because it reads as authority.
        """
        rule = get_library().get("presumptive-44ada-profession")
        assert "2026-27" not in rule.citation
        assert "not been verified" in (rule.notes or "")


class TestThroughTheTool:
    @pytest.mark.asyncio
    async def test_the_answer_carries_its_legal_basis(self):
        """
        The direct-answer path refuses to serve a tax answer with no legal basis,
        so an operation without one silently costs a model call every time.
        """
        from backend.tools.tax_tool import TaxCalcTool

        out = await TaxCalcTool().execute(
            operation="presumptive_income", params={"receipts": 70 * LAKH, "kind": "profession"}
        )
        assert out["eligible"] is True
        basis = " ".join(out["legal_basis"])
        assert "44ADA" in basis and "1961" in basis

    @pytest.mark.asyncio
    async def test_business_cites_its_own_section(self):
        from backend.tools.tax_tool import TaxCalcTool

        out = await TaxCalcTool().execute(
            operation="presumptive_income", params={"receipts": 1 * CRORE, "kind": "business"}
        )
        assert "44AD" in " ".join(out["legal_basis"])

    def test_the_planner_is_offered_the_operation(self):
        from backend.tools.tax_tool import TaxCalcTool

        assert "presumptive_income" in str(TaxCalcTool().get_schema())
