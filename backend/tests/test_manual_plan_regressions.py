"""Regressions found while writing the manual test plan (extraction + intent)."""
import pytest

from backend.context import FactExtractor, FactManager, FinancialProfile
from backend.state import ConversationState, Intent, IntentClassifier


def facts(message, profile=None):
    return {c["name"]: c["value"] for c in FactExtractor.extract_candidate_facts(message, profile=profile)
            if not c.get("action")}


def test_context_entity_not_reused_for_unrelated_assets():
    p = FinancialProfile()
    FactManager.apply_candidates(p, FactExtractor.extract_candidate_facts("My credit card balance is 1.55L"))
    assert facts("I have 2 cr in mutual funds", p) == {"mutual_funds": 20000000.0}


def test_personal_loan_amount_is_a_balance_not_an_emi():
    f = facts("I have a personal loan of 3 lakh at 14% with a minimum of 10000")
    assert f.get("personal_loan_balance") == 300000.0
    assert "personal_loan_emi" not in f
    assert facts("my personal loan EMI is 12k")["personal_loan_emi"] == 12000.0


@pytest.mark.parametrize("message, monthly", [
    ("Which regime is better for me with a salary of 18 lakh?", 150000.0),
    ("I earn 6 lakh per annum", 50000.0),
    ("My CTC is 24 LPA", 200000.0),
    ("My salary is 1.5 lakh a month", 150000.0),
    ("My take home is 95k", 95000.0),
    ("I earn 1.2L a month", 120000.0),
])
def test_income_normalised_to_monthly(message, monthly):
    assert facts(message)["income"] == monthly


def test_annual_origin_is_shown_in_profile():
    p = FinancialProfile()
    FactManager.apply_candidates(p, FactExtractor.extract_candidate_facts("My salary is 18 lakh per annum"))
    assert "Monthly Income: ₹1.5 Lakh (stated as ₹18,00,000 per year)" in p.to_context_string()


def test_other_income_and_gains_are_not_salary():
    assert facts("I have 6 lakh other income, 2 lakh short-term gains on shares. How much tax?") == {}


def test_sip_amount_remembered_but_not_its_change():
    assert facts("I invest 10k a month in a SIP") == {"sip_monthly": 10000.0}
    assert "sip_monthly" not in facts("What if I increase my SIP by 5000 for 15 years at 12%?")


@pytest.mark.parametrize("message, intent", [
    ("I invest 10k a month in a SIP", Intent.MEMORY_UPDATE),
    ("I paid some, my credit card balance is now 1.55L", Intent.MEMORY_UPDATE),
    ("Help me plan my budget: I earn 1 lakh, rent 30k", Intent.PLANNING),
    ("What will something costing 1 lakh today cost in 10 years at 6% inflation?", Intent.CALCULATION),
    ("Explain the new tax regime slabs", Intent.FINANCIAL_QA),
])
def test_intents(message, intent):
    extracted = [c for c in FactExtractor.extract_candidate_facts(message) if not c.get("action")]
    assert IntentClassifier.classify(message, ConversationState(), extracted_facts=extracted).intent == intent


def test_loan_balance_and_emi_in_one_message():
    assert facts("I have a personal loan of 3 lakh and my home loan EMI is 25k") == {
        "personal_loan_balance": 300000.0, "home_loan_emi": 25000.0}


@pytest.mark.parametrize("message, expected", [
    ("My home loan is 40 lakh and the EMI is 35k", {"home_loan_balance": 4000000.0, "home_loan_emi": 35000.0}),
    ("My car loan EMI is 12k", {"car_loan_emi": 12000.0}),
    ("I pay 15k EMI", {"personal_loan_emi": 15000.0}),
    ("I took a 5 lakh education loan", {"education_loan_balance": 500000.0}),
    ("personal loan emi 8000, car loan 4 lakh", {"personal_loan_emi": 8000.0, "car_loan_balance": 400000.0}),
])
def test_loan_types(message, expected):
    assert facts(message) == expected


def test_forget_and_close_typed_loans():
    from backend.context.extractor import extract_memory_commands
    assert {c["name"] for c in extract_memory_commands("forget my home loan")} == {"home_loan_emi", "home_loan_balance", "home_loan_rate"}
    closed = extract_memory_commands("I paid off my car loan")
    assert {c["name"] for c in closed} == {"car_loan_emi", "car_loan_balance", "car_loan_rate"}
    assert all(c["closure"] for c in closed if not c.get("action"))
    # the rate is removed rather than set to 0%
    assert [c["name"] for c in closed if c.get("action") == "delete"] == ["car_loan_rate"]


@pytest.mark.parametrize("message, expected", [
    ("What was my tax for 2019-20 on a 10 lakh salary?", {}),
    ("How much tax on a 12.75 lakh salary this year?", {}),
    ("My salary is 1.5 lakh a month. How much tax will I pay under the new regime?", {"income": 150000.0}),
])
def test_tax_questions_do_not_become_salary(message, expected):
    assert facts(message) == expected


def test_planner_prompt_passes_named_tax_years():
    from backend.prompts.planner import get_planner_system_prompt
    assert "pass it as 'tax_year' exactly" in get_planner_system_prompt([])


@pytest.mark.parametrize("message, expected", [
    ("forget my SIP", {"sip_monthly"}),
    ("please forget my sip", {"sip_monthly"}),
    ("delete my PPF", {"ppf"}),
    ("forget my fixed deposit", {"fixed_deposit"}),
    ("forget my essentials", {"essential_expenses"}),
    ("forget my insurance premium", {"insurance_premium"}),
    ("forget my stocks", {"stocks"}),
    ("forget my rent", {"rent"}),
])
def test_forget_covers_every_remembered_kind(message, expected):
    """TORA must not say it forgot something it still remembers."""
    from backend.context.extractor import extract_memory_commands
    names = {c["name"] for c in extract_memory_commands(message) if c.get("action") == "delete"}
    assert names == expected


@pytest.mark.parametrize("message, expected", [
    ("मेरी सैलरी 60 हजार है", {"income": 60000.0}),
    ("मेरा किराया २०००० है", {"rent": 20000.0}),
    ("मेरी आमदनी 1.2 लाख है", {"income": 120000.0}),
])
def test_hindi_in_devanagari_is_remembered(message, expected):
    """Live run: a Hindi salary message was answered with 'noted' but nothing was stored."""
    from backend.context.extractor import FactExtractor
    got = {c["name"]: c["value"] for c in FactExtractor.extract_candidate_facts(message)}
    assert got == expected
