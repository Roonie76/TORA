"""Phase 3A — Memory 2.0: corrections, deletion, closure, provenance and persistence."""
import pytest

from backend.context import FactExtractor, FactManager, FinancialProfile
from backend.context.extractor import extract_memory_commands
from backend.context.financial import FactStatus


def replay(messages):
    profile = FinancialProfile()
    for turn, msg in enumerate(messages, start=1):
        FactManager.apply_candidates(
            profile, FactExtractor.extract_candidate_facts(msg, profile=profile), turn=turn
        )
    return profile


class TestCorrections:
    @pytest.mark.parametrize("fix", [
        "Actually it's 65k, not 60k",
        "Sorry, I meant 65000",
        "Typo — my salary is 65k",
        "My mistake, I earn 65k",
    ])
    def test_mistake_is_retracted_not_history(self, fix):
        p = replay(["I earn 60k", fix])
        assert p.income.value == 65000.0
        assert p.income.get_previous_value() is None
        assert p.income.get_original_value() == 65000.0
        assert p.income.get_retracted_values() == [60000.0]
        text = p.to_context_string()
        assert "Corrected Mistakes" in text
        assert "₹60,000 was stated by mistake" in text
        assert "Original: ₹60,000" not in text

    def test_change_over_time_is_history_not_mistake(self):
        p = replay(["I take home ₹75,000 per month.",
                    "Small correction: my salary just increased to ₹82,000 per month."])
        assert p.income.get_previous_value() == 75000.0
        assert p.income.get_retracted_values() == []

    def test_retracted_value_skipped_in_multi_hop_chain(self):
        p = replay([
            "My credit card balance is 1.8L",
            "My credit card balance is now 1.6L",
            "Sorry, I meant 1.65L, not 1.6L",
        ])
        cc = p.debts["credit_card_debt"]
        assert cc.value == 165000.0
        assert cc.get_previous_value() == 180000.0
        assert cc.get_provenance_chain() == [180000.0, 165000.0]
        assert cc.get_retracted_values() == [160000.0]

    def test_same_value_restated_adds_no_revision(self):
        p = replay(["My rent is 20k", "My rent is 20k"])
        assert p.rent.revisions == []


class TestDeletion:
    def test_forget_single_fact_scrubs_values(self):
        p = replay(["My rent is 20k", "My rent is now 22k", "My salary is 90k", "Please forget my rent"])
        assert p.rent is None
        assert p.income.value == 90000.0
        dumped = str(p.to_dict())
        assert "22000" not in dumped and "20000" not in dumped
        assert "Monthly Rent" not in p.to_context_string()

    def test_forget_credit_card_removes_balance_and_apr(self):
        p = replay(["My credit card balance is 1.2L at 42% APR", "delete my credit card details"])
        assert "credit_card_debt" not in p.debts
        assert "credit_card_apr" not in p.debts

    def test_forget_removes_hypotheticals_too(self):
        p = replay(["My salary is 80k", "What if my salary were 1L?", "forget my salary"])
        assert p.income is None
        assert not any(s.name == "income" for s in p.scenarios)

    @pytest.mark.parametrize("msg", ["forget everything", "Clear my profile", "delete all my data",
                                     "Please forget what you know"])
    def test_clear_all(self, msg):
        p = replay(["My salary is 80k", "My rent is 20k", msg])
        assert p.is_empty()
        assert p.to_context_string() == ""

    @pytest.mark.parametrize("msg", ["How do I delete my credit card?", "Can you forget my rent?",
                                     "Should I remove my gold from the locker?"])
    def test_questions_are_not_commands(self, msg):
        assert extract_memory_commands(msg) == []

    def test_facts_after_clear_are_kept(self):
        p = replay(["My salary is 80k", "forget everything", "My rent is 12k"])
        assert p.income is None and p.rent.value == 12000.0


class TestClosure:
    def test_paid_off_sets_zero_with_history(self):
        p = replay(["My credit card balance is 50k", "I paid off my credit card"])
        cc = p.debts["credit_card_debt"]
        assert cc.value == 0.0 and cc.get_previous_value() == 50000.0

    def test_paid_off_without_existing_debt_creates_nothing(self):
        p = replay(["I paid off my credit card"])
        assert p.is_empty()

    def test_no_longer_pay_rent(self):
        p = replay(["My rent is 18k", "I no longer pay rent, I moved in with my parents"])
        assert p.rent.value == 0.0


class TestProvenanceAndPersistence:
    def test_turn_numbers_recorded(self):
        p = replay(["I earn 60k", "hello", "My salary is now 70k"])
        assert p.income.source_turn == 3
        assert p.income.revisions[0].turn == 1

    def test_round_trip_serialization(self):
        p = replay([
            "I earn 60k", "Actually it's 65k, not 60k", "My credit card balance is 1.5L",
            "What if my salary were 1L?", "I used to pay 15k rent", "My rent is 20k",
        ])
        restored = FinancialProfile.from_dict(p.to_dict())
        assert restored.to_dict() == p.to_dict()
        assert restored.to_context_string() == p.to_context_string()
        assert restored.scenarios[0].status == FactStatus.HYPOTHETICAL.value

    def test_from_dict_handles_empty(self):
        assert FinancialProfile.from_dict(None).is_empty()
        assert FinancialProfile.from_dict({}).is_empty()
