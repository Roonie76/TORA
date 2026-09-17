"""Phase 5B: Hinglish / typo normalisation and benchmark coverage gates."""
import json
from collections import Counter
from pathlib import Path

import pytest

from backend.context.extractor import FactExtractor, FactManager
from backend.context.financial import FinancialProfile
from backend.context.normalize import normalize_message
from backend.state.intent import Intent, IntentClassifier

SCENARIOS = Path(__file__).resolve().parents[1] / "evals" / "scenarios.json"


@pytest.mark.parametrize("raw, expected_fragment", [
    ("meri salry 80k hai", "my salary 80k"),
    ("main 90 hazaar kamata hoon", "i 90 thousand"),
    ("mera kiraya 20k hai", "my rent 20k"),
    ("my ballance on credt card is 30k", "balance on credit card"),
    ("salary bhool jao", "forget"),
])
def test_normalize_message(raw, expected_fragment):
    assert expected_fragment in normalize_message(raw)


def test_normalize_leaves_english_untouched():
    msg = "My salary is 82k and rent is 20k"
    assert normalize_message(msg).lower() == msg.lower()


def test_main_only_rewritten_before_amount():
    # "main" as an English word must survive
    assert "main" in normalize_message("the main reason is rent")


def _profile_after(msg):
    profile = FinancialProfile()
    FactManager.apply_candidates(profile, FactExtractor.extract_candidate_facts(msg, profile), turn=1)
    return profile


def test_hinglish_salary_extracted():
    assert _profile_after("meri salary 80k hai").get_fact("income").value == 80000


def test_used_to_sets_previous_value():
    fact = _profile_after("I used to earn 60k but now I earn 72k").get_fact("income")
    assert fact.value == 72000 and fact.get_previous_value() == 60000


def test_amount_before_groceries_and_saved():
    assert _profile_after("I spend 15k on groceries").get_fact("food").value == 15000
    assert _profile_after("I have 2 lakh saved").get_fact("savings").value == 200000


@pytest.mark.parametrize("msg", ["Tax on 60 lakh income", "Super senior with 7 lakh income under old regime"])
def test_impersonal_calc_requests_do_not_store_facts(msg):
    assert _profile_after(msg).get_fact("income") is None


@pytest.mark.parametrize("msg, intent", [
    ("I earn 1 lakh and spend 65k a month. What is my savings rate?", Intent.CALCULATION),
    ("My EMIs are 45k and income 1 lakh — what's my debt to income ratio?", Intent.CALCULATION),
    ("My car loan EMI is 12k", Intent.MEMORY_UPDATE),
    ("My emergency fund is 1.5L", Intent.MEMORY_UPDATE),
])
def test_calc_vs_memory_intent(msg, intent):
    facts = FactExtractor.extract_candidate_facts(msg, FinancialProfile())
    assert IntentClassifier.classify(msg, None, [], facts).intent == intent


def test_benchmark_is_large_and_diverse():
    data = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    scenarios = data if isinstance(data, list) else data["scenarios"]
    assert len(scenarios) >= 100
    ids = [s["id"] for s in scenarios]
    assert len(ids) == len(set(ids)), "scenario ids must be unique"
    cats = Counter(s["category"] for s in scenarios)
    for required in ("memory", "language", "calculation", "tax", "planning", "safety", "tool_selection"):
        assert cats[required] >= 3, f"too few {required} scenarios"
    assert max(len(s["turns"]) for s in scenarios) >= 10, "need at least one long conversation"
