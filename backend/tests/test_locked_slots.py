"""Locked slots: the engine owns every figure in the answer."""
import os
import re

import pytest

from backend.answer.slots import (TRUSTED_TOOLS, build_slots, render, repair_instruction, slot_table,
                                  stray_numbers)
from backend.finance.debt import consolidation_check, debt_rescue_plan
from backend.finance.tax import compute_tax


class _Result:
    def __init__(self, output, tool_name="finance_calc", is_error=False):
        self.output, self.tool_name, self.is_error = output, tool_name, is_error


class _Ctx:
    def __init__(self, *results):
        self.results = list(results)

    def is_empty(self):
        return not self.results


DEBTS = [{"name": "Credit card", "balance": 120000, "apr": 40, "min_payment": 6000},
         {"name": "Personal loan", "balance": 250000, "apr": 15, "min_payment": 9000}]


@pytest.fixture
def rescue_slots():
    plan = debt_rescue_plan(debts=DEBTS, monthly_income=88000, essential_expenses=40000, current_savings=60000)
    return build_slots(_Ctx(_Result(plan)))


def test_slots_carry_the_engines_own_figures(rescue_slots):
    assert rescue_slots["debt_rescue_plan.total_debt"] == "₹3,70,000"
    assert rescue_slots["debt_rescue_plan.monthly_budget_for_debt"] == "₹48,000"
    assert rescue_slots["debt_rescue_plan.plan.months_to_debt_free"] == "9 months"
    # a rate reads as a percentage, a duration as months, an amount as rupees
    assert rescue_slots["debt_rescue_plan.snapshot.weighted_apr"].endswith("%")
    assert rescue_slots["debt_rescue_plan.what_if_extra.1.extra_per_month"].startswith("₹")


def test_placeholders_are_replaced_and_unknown_ones_reported(rescue_slots):
    text = ("Debt-free in {{debt_rescue_plan.plan.months_to_debt_free}} paying "
            "{{monthly_budget_for_debt}} a month. {{debt_rescue_plan.made_up}} is not a figure.")
    rendered, unknown = render(text, rescue_slots)
    assert "9 months" in rendered and "₹48,000" in rendered   # the short name resolves too
    assert unknown == ["debt_rescue_plan.made_up"]
    assert "{{" not in rendered


def test_a_rescaled_or_invented_figure_is_flagged(rescue_slots):
    answer = ("Your total debt is ₹37 Lakh, you pay ₹48,000 a month and save ₹99,999 "
              "in 9 months.")
    stray = stray_numbers(answer, rescue_slots)
    assert "₹37 Lakh" in stray and "₹99,999" in stray       # the live-run failures
    assert not [s for s in stray if "48,000" in s or s.startswith("9 ")]


def test_years_and_small_counts_are_not_treated_as_figures(rescue_slots):
    assert stray_numbers("In 2026 you have 2 debts and 1 plan; step 3 is the last.", rescue_slots) == []


def test_facts_the_user_gave_are_allowed(rescue_slots):
    answer = "On your ₹88,000 salary, after ₹22,000 rent, the plan takes 9 months."
    assert stray_numbers(answer, rescue_slots, extra_allowed=["₹22,000"]) == []


def test_comparison_rows_become_slots():
    out = consolidation_check(debts=DEBTS, new_rate=14, tenure_months=36, processing_fee_percent=2)
    slots = build_slots(_Ctx(_Result(out)))
    keys = [k for k in slots if "total_cost" in k]
    assert keys, slots.keys()
    assert any(slots[k].startswith("₹") for k in keys)


def test_tax_figures_are_offered_too():
    slots = build_slots(_Ctx(_Result(compute_tax(gross_salary=1375000), tool_name="tax_calc")))
    assert slots["compute_tax.total_tax"].startswith("₹")
    assert slots["compute_tax.given.gross_salary"] == "₹13,75,000"


def test_research_figures_never_reach_the_trusted_prompt():
    """Anything fetched from the web stays in the external-data block."""
    web = {"operation": "research", "summary": "SBI home loans start at 7.25%", "rates": {"sbi": 7.25}}
    assert build_slots(_Ctx(_Result(web, tool_name="research"))) == {}
    assert build_slots(_Ctx(_Result(web, tool_name="web_fetch"))) == {}
    assert "research" not in TRUSTED_TOOLS and "web_search" not in TRUSTED_TOOLS


def test_failed_tools_contribute_nothing():
    assert build_slots(_Ctx(_Result({"operation": "emi"}, is_error=True))) == {}
    assert build_slots(_Ctx()) == {}
    assert build_slots(None) == {}


def test_slot_table_and_repair_instruction_name_the_rules(rescue_slots):
    table = slot_table(rescue_slots)
    assert "{{debt_rescue_plan.total_debt}} = ₹3,70,000" in table
    assert "not ₹37 lakh" in table.lower()
    instruction = repair_instruction(["₹37 Lakh"], ["debt_rescue_plan.made_up"], rescue_slots)
    assert "₹37 Lakh" in instruction and "{{debt_rescue_plan.made_up}}" in instruction


def test_agent_rewrites_an_invented_figure_once(monkeypatch):
    """End to end: a bad first draft is rewritten from the slots, and the trace records it."""
    import asyncio

    from backend.agent.agent import ToraAgent
    from backend.llm.base import LLMResponse
    from backend.tests.test_sessions_api import ScriptedLLM

    monkeypatch.setenv("TORA_LOCKED_SLOTS", "on")
    monkeypatch.setenv("TORA_FAST_PATH", "on")
    monkeypatch.setenv("TORA_GROUNDING_MODE", "off")

    import backend.main as main_module

    fake = ScriptedLLM()
    drafts = iter([
        "Your EMI is ₹99,999 a month.",                                   # invented
        "Your EMI is {{emi.emi}} a month.",                                    # rewritten from the slot
    ])

    async def generate(messages, model=None, options=None):
        if messages[0]["content"].startswith("You are TORA's Tool Planner"):
            return LLMResponse(content='{"thought":"x","requires_tools":false,"steps":[]}', model="fake")
        return LLMResponse(content=next(drafts), model="fake")

    monkeypatch.setattr(fake, "generate", generate)
    agent = ToraAgent(llm_provider=fake, planner=main_module.planner, tool_executor=main_module.tool_executor)
    monkeypatch.setattr(main_module.planner, "llm_provider", fake)

    response = asyncio.run(agent.run("EMI on 30 lakh at 8.4% for 25 years"))
    assert "99,999" not in response.content
    assert "₹23,955" in response.content


def test_rounding_drift_is_not_a_stray_figure(rescue_slots):
    """A live turn spent a rewrite on ₹2,550 against the engine's ₹2,551."""
    saved = rescue_slots["debt_rescue_plan.what_if_extra.2.interest_saved"]
    assert saved == "₹2,551"
    assert stray_numbers("You save ₹2,550 in interest.", rescue_slots) == []
    # but a re-scaled or invented figure is still caught
    assert stray_numbers("Your debt is ₹37 Lakh.", rescue_slots) == ["₹37 Lakh"]
    assert stray_numbers("You save ₹2,900 in interest.", rescue_slots) == ["₹2,900"]


class TestUnitDuplication:
    """Seen live: the slot value carries its own unit, so "{{x}} months" rendered as
    "9 months months" and "₹{{y}}" as "₹₹15,000" in a debt rescue plan."""

    def test_repeated_unit_after_a_slot_is_dropped(self):
        slots = {"plan.timeline": "9 months", "plan.sooner": "1 month"}
        text = "Cleared in **{{plan.timeline}}** months, or **{{plan.sooner}}** month(s) sooner."
        out, unknown = render(text, slots)
        assert out == "Cleared in **9 months**, or **1 month** sooner."
        assert unknown == []

    def test_repeated_rupee_sign_is_dropped(self):
        out, _ = render("Never below ₹{{plan.min}}.", {"plan.min": "₹15,000"})
        assert out == "Never below ₹15,000."

    def test_a_genuine_repeat_across_a_sentence_is_left_alone(self):
        out, _ = render("{{a}} now. Months matter.", {"a": "9 months"})
        assert out == "9 months now. Months matter."

    def test_different_units_are_not_merged(self):
        out, _ = render("{{a}} years away.", {"a": "9 months"})
        assert out == "9 months years away."


class TestRowNaming:
    """Engines name a row with whatever word fits — "name", "item", "bucket" (budget_plan),
    "area" (financial_health_check). Missing one numbers the rows 1, 2, 3 and the figure loses
    what it was about: live, that produced "Score: 55 / Score: 20 / Score: 15"."""

    def test_the_known_names_are_used(self):
        from backend.answer.slots import _row_label
        for key in ("name", "item", "option", "label", "bucket", "area", "category"):
            assert _row_label({key: "needs", "score": 1}) == "needs"

    def test_an_unknown_key_falls_back_to_the_first_short_string(self):
        from backend.answer.slots import _row_label
        assert _row_label({"pillar": "liquidity", "score": 3}) == "liquidity"

    def test_a_sentence_is_not_a_row_name(self):
        from backend.answer.slots import _row_label
        row = {"detail": "0.0 months of expenses saved (target 3).", "score": 0}
        assert _row_label(row) is None

    def test_a_row_with_no_string_at_all_has_no_name(self):
        from backend.answer.slots import _row_label
        assert _row_label({"score": 3, "target": 10}) is None

    def test_health_check_areas_are_named_in_the_slots(self):
        from backend.answer.slots import build_slots
        from backend.finance import advisor

        class Result:
            is_error = False
            tool_name = "finance_calc"

            def __init__(self, output):
                self.output = output

        class Context:
            def __init__(self, r):
                self.results = r

            def is_empty(self):
                return not self.results

        out = advisor.financial_health_check(monthly_income=95000, monthly_expenses=24000,
                                             monthly_emis=13000)
        names = list(build_slots(Context([Result(out)])))
        assert not any(re.search(r"\.\d+\.", n) for n in names), f"numbered rows remain: {names}"
