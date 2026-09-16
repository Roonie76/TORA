"""
Regression tests for memory-integrity defects found in the Phase 2G re-audit:
- 'L' / 'cr' suffixes were ignored by entity regexes (1.65L parsed as ₹1.65 and dropped)
- questions and calculations overwrote stored facts via the context-entity fallback
- percentages in unrelated questions were stored as credit-card APR
"""
import pytest

from backend.context import FinancialProfile, FactExtractor, FactManager
from backend.context.financial import format_inr_large


def _replay(messages):
    profile = FinancialProfile()
    for msg in messages:
        FactManager.apply_candidates(
            profile, FactExtractor.extract_candidate_facts(msg, profile=profile)
        )
    return profile


@pytest.mark.parametrize(
    "message, expected",
    [
        ("My credit card balance is 1.65L", 165000.0),
        ("my credit card balance is 1.65 L", 165000.0),
        ("I earn 1.2L a month", 120000.0),
        ("My salary is 1 lac", 100000.0),
        ("I have 2 cr in mutual funds", 20000000.0),
    ],
)
def test_short_indian_unit_suffixes_are_parsed(message, expected):
    cands = FactExtractor.extract_candidate_facts(message)
    assert cands, message
    assert cands[0]["value"] == expected


@pytest.mark.parametrize(
    "follow_up",
    [
        "Calculate 200000 * 0.36 / 12",
        "Is 50k a good salary for Bangalore?",
        "What is 20% of 60000?",
        "Should I pay 5000 toward it?",
        "How much is 1.5 lakh divided by 12?",
    ],
)
def test_questions_and_calculations_never_overwrite_income(follow_up):
    profile = _replay(["My salary is 80000 per month", follow_up])
    assert profile.income.value == 80000.0
    assert profile.income.revisions == []


def test_calculation_after_debt_does_not_change_debt():
    profile = _replay(["My credit card balance is 1.65L", "Calculate 165000 * 0.42 / 12"])
    assert profile.debts["credit_card_debt"].value == 165000.0


def test_unrelated_percentage_is_not_stored_as_card_apr():
    profile = _replay(["What is 20% of 60000?", "My FD pays 7% interest"])
    assert "credit_card_apr" not in profile.debts


def test_card_apr_still_extracted_in_debt_context():
    cands = FactExtractor.extract_candidate_facts("My credit card charges 42% APR")
    names = {c["name"]: c["value"] for c in cands}
    assert names.get("credit_card_apr") == 42.0


def test_previous_credit_card_balance_is_recoverable():
    profile = _replay([
        "My credit card balance is 1.65L",
        "I paid some, my credit card balance is now 1.55L",
        "What was my previous credit card balance?",
    ])
    fact = profile.debts["credit_card_debt"]
    assert fact.value == 155000.0
    assert fact.get_previous_value() == 165000.0
    rendered = profile.to_context_string()
    assert "Previous was ₹1.65 Lakh" in rendered


def test_hypothetical_with_lakh_suffix_is_isolated():
    profile = _replay(["My salary is 80000 per month", "What if my salary were 1L?"])
    assert profile.income.value == 80000.0
    assert any(s.value == 100000.0 and s.status == "hypothetical" for s in profile.scenarios)


def test_first_person_correction_still_uses_context_entity():
    profile = FinancialProfile()
    FactManager.apply_candidates(profile, FactExtractor.extract_candidate_facts("My rent is 20k"))
    FactManager.apply_candidates(
        profile, FactExtractor.extract_candidate_facts("Sorry, it is 22k", profile=profile)
    )
    assert profile.rent.value == 22000.0


def test_crore_formatting():
    assert format_inr_large(20000000) == "₹2 Crore"
    assert format_inr_large(25000000) == "₹2.5 Crore"
    assert format_inr_large(165000) == "₹1.65 Lakh"
