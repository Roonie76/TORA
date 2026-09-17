"""Phase 5E — model-assisted fact extraction guarded by rules."""
import asyncio
import json

import pytest

from backend.agent.agent import ToraAgent
from backend.context import FactExtractor, FinancialProfile
from backend.context.llm_extractor import EXTRACTOR_PREFIX, SCHEMA, should_try, validate_proposals
from backend.llm.base import LLMProvider, LLMResponse


def f(name, value, evidence, status="current"):
    return {"name": name, "value": value, "evidence": evidence, "status": status}


def test_rules_now_cover_common_hinglish():
    got = {c["name"]: c["value"] for c in FactExtractor.extract_candidate_facts("meri salary 80 hazaar hai")}
    assert got == {"income": 80000.0}


class TestShouldTry:
    @pytest.mark.parametrize("msg", [
        "meri salary 80 hazaar hai",
        "I pull in about 90 grand a month after tax",
        "I have an FD of 3 lakh at SBI",
    ])
    def test_tries_when_rules_miss_a_money_statement(self, msg):
        assert should_try(msg, [])

    @pytest.mark.parametrize("msg", [
        "What is 20% of 60000?",           # no first person
        "Calculate 5000 * 12 for me",       # arithmetic
        "What if I invest 5000 more?",      # hypothetical
        "Hello, I am new here",             # no money
        "My salary is 80k",                 # would already be caught (rules non-empty below)
    ])
    def test_skips(self, msg):
        rules = FactExtractor.extract_candidate_facts(msg)
        assert not should_try(msg, rules)

    def test_off_switch(self, monkeypatch):
        monkeypatch.setenv("TORA_LLM_EXTRACTION", "off")
        assert not should_try("meri salary 80 hazaar hai", [])


class TestValidation:
    def test_hinglish_hazaar_accepted(self):
        out = validate_proposals("meri salary 80 hazaar hai", {"facts": [f("income", 80000, "80 hazaar")]})
        assert out[0]["name"] == "income" and out[0]["value"] == 80000
        assert out[0]["source"] == "model_assisted"

    def test_grand_and_lakh(self):
        out = validate_proposals("I pull in about 90 grand a month and have an FD of 3 lakh",
                                 {"facts": [f("income", 90000, "90 grand"), f("fixed_deposit", 300000, "3 lakh")]})
        assert {(x["name"], x["value"]) for x in out} == {("income", 90000), ("fixed_deposit", 300000)}

    def test_evidence_must_be_in_message(self):
        assert validate_proposals("I have an FD at SBI worth a lot", {"facts": [f("fixed_deposit", 500000, "5 lakh")]}) == []

    def test_value_must_match_evidence(self):
        assert validate_proposals("I have an FD of 3 lakh", {"facts": [f("fixed_deposit", 3000000, "3 lakh")]}) == []

    def test_unknown_names_rejected(self):
        assert validate_proposals("My crypto is 2 lakh", {"facts": [f("crypto", 200000, "2 lakh")]}) == []

    def test_hypothetical_wording_wins(self):
        out = validate_proposals("If I had an FD of 3 lakh, what would I earn?",
                                 {"facts": [f("fixed_deposit", 300000, "3 lakh", "current")]})
        assert out[0]["status"] == "hypothetical"

    def test_questions_without_first_person_rejected(self):
        assert validate_proposals("Is an FD of 3 lakh enough?", {"facts": [f("fixed_deposit", 300000, "3 lakh")]}) == []

    def test_annual_income_normalised(self):
        out = validate_proposals("My package works out to 18 lakh per annum",
                                 {"facts": [f("income", 1800000, "18 lakh")]})
        assert out[0]["value"] == 150000.0 and "per year" in out[0]["notes"]

    def test_rate_facts(self):
        ok = validate_proposals("my card charges 42% a year", {"facts": [f("credit_card_apr", 42, "42%")]})
        assert ok[0]["value"] == 42
        assert validate_proposals("my card charges 42% a year", {"facts": [f("credit_card_apr", 24, "42%")]}) == []

    def test_malformed_payloads(self):
        for payload in (None, [], {"facts": "x"}, {"facts": [None, {"name": "income"}]}):
            assert validate_proposals("I earn 5 lakh", payload) == []

    def test_schema_enumerates_names(self):
        assert "income" in SCHEMA["properties"]["facts"]["items"]["properties"]["name"]["enum"]


class ExtractLLM(LLMProvider):
    def __init__(self, facts):
        self.facts = facts
        self.calls = []

    @property
    def default_model(self):
        return "m"

    def resolve_model(self, requested=None, available=None):
        return "m"

    async def generate(self, messages, model=None, options=None):
        self.calls.append(messages[0]["content"][:30])
        if messages[0]["content"].startswith(EXTRACTOR_PREFIX):
            assert options["format"]["required"] == ["facts"]
            return LLMResponse(content=json.dumps({"facts": self.facts}), model="m")
        return LLMResponse(content="Noted.", model="m")

    async def list_models(self):
        return ["m"]

    async def health_check(self):
        return {}


def test_agent_applies_verified_model_facts():
    llm = ExtractLLM([f("income", 90000, "90 grand"), f("rent", 99999, "99999")])
    profile = FinancialProfile()
    resp = asyncio.run(ToraAgent(llm_provider=llm).run("I pull in about 90 grand a month", financial_context=profile))
    assert profile.income.value == 90000 and profile.income.source == "model_assisted"
    assert profile.rent is None                          # evidence not in the message
    assert resp.intent.intent.value == "memory_update"


def test_agent_skips_model_when_rules_succeed():
    llm = ExtractLLM([])
    asyncio.run(ToraAgent(llm_provider=llm).run("My salary is 80k per month", financial_context=FinancialProfile()))
    assert not any(c.startswith(EXTRACTOR_PREFIX[:30]) for c in llm.calls)


T5_MESSAGE = ("I have 6 lakh other income, 2 lakh short-term gains on shares and "
              "3 lakh long-term gains on equity funds. How much tax do I pay?")


def test_tax_questions_skip_model_extraction():
    assert not should_try(T5_MESSAGE, [])


def test_gains_and_other_income_are_not_salary_or_holdings():
    payload = {"facts": [
        {"name": "income", "value": 600000, "status": "current", "evidence": "6 lakh"},
        {"name": "stocks", "value": 200000, "status": "current", "evidence": "2 lakh"},
        {"name": "mutual_funds", "value": 300000, "status": "current", "evidence": "3 lakh"},
    ]}
    msg = "I have 6 lakh other income, 2 lakh short-term gains on shares and 3 lakh long-term gains on equity funds"
    assert validate_proposals(msg, payload) == []
    ok = validate_proposals("I have 3 lakh in equity funds", {"facts": [
        {"name": "mutual_funds", "value": 300000, "status": "current", "evidence": "3 lakh"}]})
    assert [c["name"] for c in ok] == ["mutual_funds"]
