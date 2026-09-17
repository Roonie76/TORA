"""Phase 7: fast routing and complexity scaling."""
import asyncio

import pytest

from backend.agent.agent import ToraAgent, _case_note, _is_small_talk
from backend.llm.base import LLMProvider, LLMResponse
from backend.planner import Planner
from backend.planner.fast_path import Complexity, assess_complexity, fast_plan
from backend.state.intent import Intent, IntentResult
from backend.tools import CalculatorTool, FinanceCalcTool, TaxCalcTool, ToolExecutor, ToolRegistry


def args(message):
    plan = fast_plan(message)
    return None if plan is None else (plan.steps[0].tool_name, plan.steps[0].arguments)


@pytest.mark.parametrize("message, tool, expected", [
    ("Calculate 200000 * 0.36 / 12", "calculator", {"expression": "200000 * 0.36 / 12"}),
    ("what is 1,50,000 * 12", "calculator", {"expression": "150000 * 12"}),
    ("What is 20% of 60000?", "calculator", {"expression": "20.0 / 100 * 60000"}),
    ("What would the EMI be for a 20 lakh home loan at 8.5% for 20 years?", "finance_calc",
     {"operation": "emi", "params": {"principal": 2000000.0, "annual_rate": 8.5, "tenure_months": 240}}),
    ("EMI on 30 lakh loan at 8.4% for 25 yrs", "finance_calc",
     {"operation": "emi", "params": {"principal": 3000000.0, "annual_rate": 8.4, "tenure_months": 300}}),
    ("If I invest 10,000 a month for 10 years at 12%, what will I have?", "finance_calc",
     {"operation": "sip_future_value", "params": {"monthly_investment": 10000.0, "annual_return": 12.0, "years": 10.0}}),
    ("How much SIP do I need to build 1 crore in 20 years at 12%?", "finance_calc",
     {"operation": "required_sip", "params": {"target_amount": 10000000.0, "annual_return": 12.0, "years": 20.0}}),
    ("What will something costing 1 lakh today cost in 10 years at 6% inflation?", "finance_calc",
     {"operation": "inflation_adjust", "params": {"amount": 100000.0, "inflation_rate": 6.0, "years": 10.0,
                                                  "direction": "future_cost"}}),
    ("How much tax on a 12.75 lakh salary this year?", "tax_calc",
     {"operation": "compute_tax", "params": {"gross_salary": 1275000.0}}),
    ("My salary is 1.5 lakh a month. How much tax will I pay under the new regime?", "tax_calc",
     {"operation": "compute_tax", "params": {"gross_salary": 1800000.0, "regime": "new"}}),
    ("What was my tax for 2019-20 on a 10 lakh salary?", "tax_calc",
     {"operation": "compute_tax", "params": {"gross_salary": 1000000.0, "tax_year": "2019-20"}}),
    ("Which regime is better for a 15 lakh salary?", "tax_calc",
     {"operation": "compare_regimes", "params": {"gross_salary": 1500000.0}}),
])
def test_fast_plans(message, tool, expected):
    assert args(message) == (tool, expected)


@pytest.mark.parametrize("message", [
    "If I prepay 5000 extra every month on that loan, how much interest do I save?",   # needs context
    "What if I increase my SIP by 5000 for 15 years at 12%?",                         # what-if on memory
    "EMI for 5 lakh at 10% for 3 years and 7 lakh at 9% for 5 years?",                # two loans
    "What will my EMI be?",                                                            # missing inputs
    "Which tax regime is better for me: salary 18 lakh, 80C 1.5 lakh, 80D 25k?",      # deductions
    "Tax on 60 lakh income under the new regime",                                      # salary or other income?
    "20 lakh salary with big exemptions — which regime?",
    "I earn 60k, what's 10% of it?",
    "Hi TORA, how are you?",
    "EMI on a loan at 8% for 12 months",
    "x" * 500,
])
def test_unclear_requests_go_to_planner(message):
    assert fast_plan(message) is None


def test_fast_path_respects_available_tools():
    assert fast_plan("What is 20% of 60000?", available_tools={"finance_calc"}) is None


def test_complexity_levels():
    assert assess_complexity("Hi").level == "simple"
    fast = fast_plan("EMI on 30 lakh loan at 8.4% for 25 yrs")
    assert assess_complexity("EMI on 30 lakh loan at 8.4% for 25 yrs", fast=fast).level == "simple"
    hard = ("I have 3 loans: personal 3 lakh at 14%, card 1.5 lakh at 42%, car 4 lakh at 9%. I can pay 35k a month. "
            "Should I consolidate or pay off the card first?")
    c = assess_complexity(hard)
    assert c.is_complex and "decision or strategy question" in c.reasons
    assert assess_complexity("Explain the new tax regime",
                             IntentResult(Intent.FINANCIAL_QA, [], None)).level == "standard"


def test_small_talk_and_case_note():
    general = IntentResult(Intent.GENERAL_QA, [], None)
    assert _is_small_talk("Hi TORA, how are you?", general)
    assert _is_small_talk("thanks a lot!", general)
    assert not _is_small_talk("Hi, what is an EMI?", general)
    assert not _is_small_talk("Hello", IntentResult(Intent.FINANCIAL_QA, [], None))
    assert _case_note(Complexity("standard", 1)) == ""
    note = _case_note(Complexity("complex", 5))
    assert "experienced chartered accountant" in note and "never claim to be a CA" in note


class Recorder(LLMProvider):
    def __init__(self):
        self.calls = []

    @property
    def default_model(self):
        return "base"

    def resolve_model(self, requested=None, available=None):
        return requested or "base"

    async def generate(self, messages, model=None, options=None):
        self.calls.append({"messages": messages, "model": model, "options": options or {}})
        if messages[0]["content"].startswith("You are TORA's Tool Planner"):
            return LLMResponse(content='{"thought": "none", "requires_tools": false, "steps": []}', model=model or "base")
        return LLMResponse(content="ok", model=model or "base")

    async def list_models(self):
        return ["base"]

    async def health_check(self):
        return {"connected": True}


def _agent(llm):
    registry = ToolRegistry()
    for tool in (CalculatorTool(), FinanceCalcTool(), TaxCalcTool()):
        registry.register(tool)
    return ToraAgent(llm_provider=llm, planner=Planner(llm_provider=llm, tool_registry=registry),
                     tool_executor=ToolExecutor(registry=registry))


def test_agent_fast_path_skips_planner_and_runs_tool(monkeypatch):
    monkeypatch.setenv("TORA_FAST_PATH", "on")
    llm = Recorder()
    resp = asyncio.run(_agent(llm).run(message="EMI on 30 lakh loan at 8.4% for 25 yrs"))
    assert len(llm.calls) == 1  # answer only
    assert resp.tool_context.results[0].output["emi"] == 23954.98
    assert resp.complexity.level == "simple"


def test_fast_path_can_be_disabled(monkeypatch):
    monkeypatch.setenv("TORA_FAST_PATH", "off")
    llm = Recorder()
    asyncio.run(_agent(llm).run(message="EMI on 30 lakh loan at 8.4% for 25 yrs"))
    assert llm.calls[0]["messages"][0]["content"].startswith("You are TORA's Tool Planner")


def test_complex_cases_scale_model_and_reasoning(monkeypatch):
    monkeypatch.setenv("TORA_COMPLEX_MODEL", "big-model")
    monkeypatch.setenv("TORA_COMPLEX_THINK", "on")
    llm = Recorder()
    msg = ("I have 3 loans: personal 3 lakh at 14%, card 1.5 lakh at 42%, car 4 lakh at 9%. I can pay 35k a month. "
           "Should I consolidate or pay off the card first? What's the best strategy?")
    resp = asyncio.run(_agent(llm).run(message=msg))
    assert resp.complexity.is_complex
    assert all(c["model"] == "big-model" for c in llm.calls)
    answer = llm.calls[-1]
    assert answer["options"].get("think") is True
    assert "## This Case" in answer["messages"][0]["content"]

    # simple turns keep the default model and no reasoning pass
    llm2 = Recorder()
    asyncio.run(_agent(llm2).run(message="Hi TORA"))
    assert llm2.calls[-1]["model"] is None and "think" not in llm2.calls[-1]["options"]


@pytest.mark.parametrize("message, expected", [
    ("My basic is 50k, HRA 20k and I pay 25k rent in Mumbai. How much HRA is exempt?",
     {"basic_monthly": 50000.0, "hra_received_monthly": 20000.0, "rent_paid_monthly": 25000.0, "metro": True}),
    ("Basic salary 40,000, HRA 16,000, rent paid 18,000 in Pune — HRA exemption?",
     {"basic_monthly": 40000.0, "hra_received_monthly": 16000.0, "rent_paid_monthly": 18000.0, "metro": False}),
])
def test_hra_fast_path(message, expected):
    plan = fast_plan(message)
    assert plan.steps[0].arguments == {"operation": "hra_exemption", "params": expected}


def test_hra_yearly_figures_go_to_planner():
    assert fast_plan("basic 6 lakh a year, hra 2.4 lakh, rent 3 lakh a year, how much hra exempt") is None


def test_introductions_skip_the_planner():
    general = IntentResult(Intent.GENERAL_QA, [], None)
    for text in (
        "Hi TORA! I'm Ravi, 32, working in Pune.",
        "hello, my name is Asha Rao",
        "I am Priya, 28 years old and I live in Chennai",
        "Hey I'm Arjun from Bangalore",
    ):
        assert _is_small_talk(text, general), text
    for text in (
        "Hi, I'm Ravi and I have 2 lakh of debt",
        "I'm stressed about my loans",
        "hi I'm in debt",
        "I'm Ravi, I earn 50000",
        "I am confused, what is an ELSS fund?",
        "Hi I'm Ravi. Should I prepay my loan?",
    ):
        assert not _is_small_talk(text, general), text


def _profile_with(**facts):
    from backend.context.financial import FinancialProfile
    profile = FinancialProfile()
    categories = {"income": ("income", "monthly"), "essential_expenses": ("expense", "monthly"),
                  "savings": ("savings", "lump_sum"), "credit_card_debt": ("debt", "lump_sum"),
                  "credit_card_apr": ("debt", "interest_rate"), "credit_card_min_due": ("debt", "monthly"),
                  "personal_loan_balance": ("loan", "lump_sum"), "personal_loan_emi": ("loan", "monthly"),
                  "personal_loan_rate": ("loan", "interest_rate"), "rent": ("rent", "monthly"),
                  "food": ("expense", "monthly")}
    for name, value in facts.items():
        category, period = categories[name]
        profile.set_fact(name=name, value=value, category=category, period=period, turn=1)
    return profile


DEBT_FACTS = dict(income=88000, essential_expenses=40000, credit_card_debt=120000, credit_card_apr=40,
                  credit_card_min_due=6000, personal_loan_balance=250000, personal_loan_emi=9000,
                  personal_loan_rate=15, savings=60000)


def test_debt_rescue_uses_remembered_facts():
    from backend.planner.fast_path import profile_plan
    plan = profile_plan("I'm stressed about these debts, how do I get out of debt?", None,
                        _profile_with(**DEBT_FACTS), {"finance_calc"})
    assert plan is not None and plan.steps[0].tool_name == "finance_calc"
    params = plan.steps[0].arguments["params"]
    assert plan.steps[0].arguments["operation"] == "debt_rescue_plan"
    assert params["monthly_income"] == 88000 and params["essential_expenses"] == 40000
    assert params["current_savings"] == 60000
    assert {d["name"]: (d["balance"], d["apr"], d["min_payment"]) for d in params["debts"]} == {
        "Credit card": (120000, 40, 6000),
        "Personal loan": (250000, 15, 9000),
    }


def test_debt_rescue_falls_back_to_a_typical_card_minimum():
    from backend.planner.fast_path import profile_plan
    facts = {k: v for k, v in DEBT_FACTS.items() if k != "credit_card_min_due"}
    plan = profile_plan("make me a plan to get out of debt", None, _profile_with(**facts), {"finance_calc"})
    card = next(d for d in plan.steps[0].arguments["params"]["debts"] if d["name"] == "Credit card")
    assert card["min_payment"] == 6000  # 5% of the balance


def test_debt_rescue_adds_up_essentials_when_not_stated():
    from backend.planner.fast_path import profile_plan
    facts = {k: v for k, v in DEBT_FACTS.items() if k != "essential_expenses"}
    plan = profile_plan("how do I become debt free?", None, _profile_with(rent=22000, food=9000, **facts),
                        {"finance_calc"})
    assert plan.steps[0].arguments["params"]["essential_expenses"] == 31000


def test_debt_rescue_defers_to_the_planner_when_facts_are_missing():
    from backend.planner.fast_path import profile_plan
    message = "how do I get out of debt?"
    assert profile_plan(message, None, _profile_with(income=88000, essential_expenses=40000), {"finance_calc"}) is None
    no_income = {k: v for k, v in DEBT_FACTS.items() if k != "income"}
    assert profile_plan(message, None, _profile_with(**no_income), {"finance_calc"}) is None
    no_essentials = {k: v for k, v in DEBT_FACTS.items() if k != "essential_expenses"}
    assert profile_plan(message, None, _profile_with(**no_essentials), {"finance_calc"}) is None
    # a debt missing its interest rate goes to the planner: dropping it would flatter the plan,
    # and guessing the rate would understate what the debt costs
    no_loan_rate = {k: v for k, v in DEBT_FACTS.items() if k != "personal_loan_rate"}
    assert profile_plan(message, None, _profile_with(**no_loan_rate), {"finance_calc"}) is None
    no_card_apr = {k: v for k, v in DEBT_FACTS.items() if k != "credit_card_apr"}
    assert profile_plan(message, None, _profile_with(**no_card_apr), {"finance_calc"}) is None
    # and the tool has to be available
    assert profile_plan(message, None, _profile_with(**DEBT_FACTS), {"calculator"}) is None


def test_debt_rescue_ignores_unrelated_questions():
    from backend.planner.fast_path import profile_plan
    profile = _profile_with(**DEBT_FACTS)
    for message in ("what is my total debt?", "should I take a consolidation loan?",
                    "what is 5% of 1.2 lakh?", "how much tax will I pay?"):
        assert profile_plan(message, None, profile, {"finance_calc"}) is None, message


def test_tax_on_my_salary_uses_the_remembered_income():
    from backend.planner.fast_path import profile_plan
    profile = _profile_with(income=88000)
    plan = profile_plan("How much tax will I pay this year on my salary?", None, profile, {"tax_calc", "finance_calc"})
    assert plan.steps[0].tool_name == "tax_calc"
    assert plan.steps[0].arguments == {"operation": "compute_tax", "params": {"gross_salary": 1056000.0}}
    compare = profile_plan("Which regime is better for me?", None, profile, {"tax_calc"})
    assert compare.steps[0].arguments["operation"] == "compare_regimes"


def test_tax_from_memory_defers_when_the_case_is_not_plain_salary():
    from backend.planner.fast_path import profile_plan
    profile = _profile_with(income=88000)
    for message in (
        "How much tax on a 12 lakh salary?",          # figures in the message: the normal fast path
        "How much tax will I pay with my 1.5 lakh 80C?",  # deductions: planner
        "What tax did I pay in 2019-20?",             # another year: planner
        "What is my HRA exemption?",                  # its own calculation
        "What tax do I pay on my capital gains?",     # not salary
        "What is my rent?",                           # not a tax question
    ):
        assert profile_plan(message, None, profile, {"tax_calc", "finance_calc"}) is None, message
    assert profile_plan("what is my tax?", None, _profile_with(rent=20000), {"tax_calc"}) is None


@pytest.mark.parametrize("message", [
    "ok thanks. what's 18% of 2,35,000?",
    "thanks! what is 18% of 2,35,000?",
    "great, what's 18% of 2,35,000?",
    "Also, what's 18% of 2,35,000?",
])
def test_a_lead_in_does_not_push_a_clear_question_to_the_planner(message):
    """Live run: 'ok thanks. what's 18% of 2,35,000?' called the planner and a web search."""
    from backend.planner.fast_path import fast_plan
    plan = fast_plan(message, IntentResult(Intent.CALCULATION, [], None),
                     {"calculator", "finance_calc", "tax_calc", "rules_lookup"})
    assert plan is not None and plan.steps[0].tool_name == "calculator"
    assert plan.steps[0].arguments["expression"] == "18.0 / 100 * 235000"
