"""Tier 0: an unambiguous calculation is answered from the engine's own summary, with no model
call. The gate matters more than the feature — a question that wanted judgement, or a tax answer
that must carry its legal basis, has to fall through to the model."""
import asyncio

import pytest

from backend.answer.blocks import render_block
from backend.answer.direct import direct_answer
from backend.answer.slots import build_slots
from backend.finance import debt, engine
from backend.state.intent import IntentClassifier


class Result:
    is_error = False

    def __init__(self, name, output):
        self.tool_name, self.output = name, output


class Context:
    def __init__(self, results):
        self.results = results

    def is_empty(self):
        return not self.results


def answer_for(message, output, tool="finance_calc", **kw):
    ctx = Context([Result(tool, output)])
    block = render_block(ctx, build_slots(ctx))
    return direct_answer(message, IntentClassifier.classify(message), ctx, block, **kw)


EMI = engine.emi(principal=2000000, annual_rate=8.5, tenure_months=240)


class TestAnsweredWithoutAModel:
    def test_an_unambiguous_calculation_is_answered_outright(self):
        out = answer_for("What would the EMI be for a 20 lakh home loan at 8.5% for 20 years?", EMI)
        assert out is not None
        assert "₹17,356" in out                       # the figure block
        assert "EMI ₹17,356/month for 240 months" in out   # the engine's own sentence

    def test_the_engines_warnings_are_carried(self):
        out = answer_for("SIP of 10k for 15 years at 12%?",
                         dict(engine.sip_future_value(monthly_investment=10000, annual_return=12, years=15),
                              warnings=["Returns are not guaranteed."]))
        assert out is not None and "Returns are not guaranteed." in out


class TestWhatMustFallThroughToTheModel:
    """Each of these was caught by the eval suite when the gate was looser."""

    @pytest.mark.parametrize("message", [
        "Should I take a 20 lakh home loan at 8.5% for 20 years?",
        "Which tax regime is better?",
        "Prepay or invest? 30 lakh home loan at 8.5%, 10k spare a month.",
        "Am I on the right regime? How can I save tax?",
        "Where do I stand?",
        "Why is the EMI on 20 lakh at 8.5% for 20 years so high?",
        "What is the difference between an EMI and a pre-EMI?",
    ])
    def test_a_question_wanting_judgement_is_left_to_the_model(self, message):
        assert answer_for(message, EMI) is None

    def test_tax_answers_keep_the_model_because_they_must_cite_the_law(self):
        # eval rules-tax-basis: "Tax results carry their legal basis" (Section 156). The engine
        # summary does not carry it, so a fast tax answer would be a worse one.
        assert answer_for("Tax on a 13.75 lakh salary?", {"operation": "compute_tax",
                                                          "summary": "Tax is ₹1,00,000."},
                          tool="tax_calc") is None

    def test_a_comparison_engine_needs_the_recommendation_written(self):
        plan = debt.debt_rescue_plan(
            debts=[{"name": "Card", "balance": 120000, "annual_rate": 40, "minimum_due": 6000}],
            monthly_income=88000, essential_expenses=40000)
        assert answer_for("How fast can I clear my card debt?", plan) is None

    def test_an_uploaded_document_is_left_to_the_model(self):
        assert answer_for("EMI on 20 lakh at 8.5% for 20 years?", EMI, has_documents=True) is None

    def test_two_tools_are_left_to_the_model(self):
        ctx = Context([Result("finance_calc", EMI), Result("calculator", {"summary": "x"})])
        assert direct_answer("EMI on 20 lakh at 8.5% for 20 years?",
                             IntentClassifier.classify("EMI on 20 lakh at 8.5% for 20 years?"),
                             ctx, "") is None

    def test_a_failed_tool_is_left_to_the_model(self):
        failed = Result("finance_calc", EMI)
        failed.is_error = True
        assert direct_answer("EMI on 20 lakh at 8.5% for 20 years?",
                             IntentClassifier.classify("EMI on 20 lakh at 8.5% for 20 years?"),
                             Context([failed]), "") is None

    def test_an_engine_with_no_summary_is_left_to_the_model(self):
        assert answer_for("EMI on 20 lakh at 8.5% for 20 years?",
                          {"operation": "emi", "emi": 17356.46}) is None


class TestIntentIsNotTheGate:
    """The classifier reads some single sums as "planning". The operation list, not the intent,
    is what decides — so a sum stays fast and a real plan still gets written."""

    def test_a_single_sum_read_as_planning_is_still_answered_outright(self):
        out = answer_for("How much do I need to invest monthly to reach 5 lakh in 3 years at 12%?",
                         engine.required_sip(target_amount=500000, annual_return=12, years=3))
        assert out is not None and "₹11,492/month" in out

    @pytest.mark.parametrize("operation", [
        "retirement_plan", "budget_plan", "goal_plan", "debt_payoff", "financial_health_check",
    ])
    def test_a_real_plan_is_still_written_by_the_model(self, operation):
        assert answer_for("Plan this for me in 3 years",
                          {"operation": operation, "summary": "A plan."}) is None
