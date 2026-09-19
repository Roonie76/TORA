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

    def test_a_tax_result_with_no_legal_basis_is_left_to_the_model(self):
        # eval rules-tax-basis: "Tax results carry their legal basis". A tax answer without the
        # rule it rests on is half an answer, so the engine does not get to give one.
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


def test_direct_grounding_reports_a_count_not_a_flag(monkeypatch):
    """The UI renders this value as "N figures checked". A bool showed the user
    "true figures checked" — caught by a browser screenshot, not by any unit test."""
    import asyncio

    monkeypatch.setenv("TORA_FAST_PATH", "on")
    monkeypatch.setenv("TORA_LOCKED_SLOTS", "on")
    from backend.tests.test_fast_path import Recorder, _agent

    llm = Recorder()
    r = asyncio.run(_agent(llm).run(message="EMI on 30 lakh loan at 8.4% for 25 yrs"))
    assert r.model == "engine", "tier 0 should have answered this"
    checked = r.grounding["checked"]
    assert isinstance(checked, int) and not isinstance(checked, bool), f"got {checked!r}"
    assert checked > 0


class TestNoModelCallSlipsThrough:
    """Measured live: an EMI question took 0.03s, but "how much do I need to invest monthly to
    reach 5 lakh in 3 years at 12%?" took 20.3s — a fact-extraction call that returned nothing,
    on a turn the engine answers by itself. The numbers in a sum are inputs, not facts."""

    def test_a_self_contained_sum_is_not_mined_for_facts(self):
        from backend.context.llm_extractor import should_try
        message = "How much do I need to invest monthly to reach 5 lakh in 3 years at 12%?"
        assert should_try(message, [], calculation_planned=True) is False

    def test_a_statement_about_the_user_still_is(self, monkeypatch):
        from backend.context.llm_extractor import should_try
        monkeypatch.setenv("TORA_LLM_EXTRACTION", "on")
        message = "I take home about 88 thousand a month after tax and my rent is 22 thousand."
        assert should_try(message, [], calculation_planned=False) is True

    def test_the_whole_turn_makes_no_model_call(self, monkeypatch):
        import asyncio

        monkeypatch.setenv("TORA_FAST_PATH", "on")
        monkeypatch.setenv("TORA_LOCKED_SLOTS", "on")
        monkeypatch.setenv("TORA_LLM_EXTRACTION", "on")
        from backend.tests.test_fast_path import Recorder, _agent

        llm = Recorder()
        r = asyncio.run(_agent(llm).run(
            message="How much do I need to invest monthly to reach 5 lakh in 3 years at 12%?"))
        assert r.model == "engine"
        assert llm.calls == [], f"expected no model call, got {len(llm.calls)}"


class TestTaxJoinsTierZero:
    """Tax was held back because an answer must cite the rule it rests on and the engine summary
    does not carry that. tax_calc already returns those citations from the reviewed rules
    library, so the direct answer renders them and the model is no longer needed for a plain
    tax question."""

    @staticmethod
    def tax(operation="compute_tax", **params):
        import asyncio

        from backend.tools.tax_tool import TaxCalcTool
        return asyncio.run(TaxCalcTool().run(args={"operation": operation, "params": params})).data

    def test_a_plain_tax_question_is_answered_outright(self):
        out = answer_for("How much tax on a 13.75 lakh salary?", self.tax(gross_salary=1375000),
                         tool="tax_calc")
        assert out is not None
        assert "₹78,000" in out
        assert "Tax year 2026-27" in out          # never silently answers for an unnamed year

    def test_the_legal_basis_is_in_the_answer(self):
        out = answer_for("Tax on 13.75 lakh?", self.tax(gross_salary=1375000), tool="tax_calc")
        assert "**Legal basis**" in out
        assert "Section 156" in out               # eval rules-tax-basis, now asserted on the answer

    def test_choosing_a_regime_is_still_the_models_job(self):
        assert answer_for("Which tax regime is better for me?",
                          self.tax("compare_regimes", gross_salary=1375000), tool="tax_calc") is None

    def test_a_capital_gains_answer_keeps_the_method_it_used(self):
        out = answer_for(
            "I bought a flat in June 2010 for 30 lakh and sold it in August 2026 for 90 lakh. What is my tax?",
            self.tax("capital_gains_tax", asset_type="property", purchase_date="2010-06-01",
                     sale_date="2026-08-01", purchase_cost=3000000, sale_value=9000000),
            tool="tax_calc")
        assert out is not None
        assert "with indexation" in out and "4,37,174" in out
        assert "| **12.5% without indexation** |" in out    # the two methods compared as a table

    def test_advance_tax_says_what_to_pay_when_and_how_far_behind(self):
        out = answer_for("My tax this year will be about 1.5 lakh, TDS 40k, I've paid 10k advance tax. "
                         "What do I pay and when?",
                         self.tax("advance_tax_plan", estimated_tax=150000, tds_expected=40000,
                                  paid_so_far=10000, tax_year="2026-27", today="2026-09-17"),
                         tool="tax_calc")
        assert out is not None
        for needed in ("72,500", "2026-12-15", "behind"):
            assert needed in out, f"missing {needed!r}"
