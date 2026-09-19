"""The planner is expensive (~95s of prompt reading on CPU): only call it when it can help,
and only show it the tools that could plausibly be used."""
import pytest

from backend.planner.tool_filter import candidate_tools, filter_schemas, needs_no_tools
from backend.state.intent import Intent, IntentClassifier, IntentResult

SCHEMAS = [{"function": {"name": n}} for n in
           ("calculator", "web_search", "web_fetch", "research", "finance_calc", "tax_calc", "rules_lookup")]


def ir(intent):
    return IntentResult(intent, [], None)


@pytest.mark.parametrize("message, intent", [
    ("What is my salary?", Intent.MEMORY_RECALL),
    ("And my rent?", Intent.CLARIFICATION),
    ("why?", Intent.CLARIFICATION),
    ("forget my SIP", Intent.MEMORY_DELETE),
    ("my salary is 88k", Intent.MEMORY_UPDATE),
    ("I have a credit card balance of 1.2 lakh at 40% with 6000 minimum due", Intent.MEMORY_UPDATE),
])
def test_memory_turns_skip_the_planner(message, intent):
    assert needs_no_tools(message, ir(intent)) is True


@pytest.mark.parametrize("message, intent", [
    ("what did I spend on food last month?", Intent.MEMORY_RECALL),     # needs the records
    ("how much tax do I owe?", Intent.MEMORY_RECALL),                   # needs the tax engine
    ("what will I have if I invest 5000 for 10 years?", Intent.MEMORY_RECALL),
    ("EMI on 30 lakh at 9% for 20 years", Intent.CALCULATION),
    ("How do I get out of debt?", Intent.PLANNING),
    ("Compare SBI and HDFC home loan rates", Intent.COMPARISON),
])
def test_turns_that_need_an_engine_still_reach_the_planner(message, intent):
    assert needs_no_tools(message, ir(intent)) is False


def test_an_uploaded_document_always_reaches_the_planner():
    assert needs_no_tools("what is my salary?", ir(Intent.MEMORY_RECALL), has_documents=True) is False


@pytest.mark.parametrize("message, intent, expected", [
    ("EMI on 30 lakh at 9% for 20 years", Intent.CALCULATION, {"finance_calc", "calculator"}),
    # finance_calc is deliberately absent: the wording names tax, so the intent does not get
    # to add the largest schema in the set on top of it.
    ("How much tax on 13.75 lakh?", Intent.CALCULATION, {"tax_calc", "rules_lookup", "calculator"}),
    ("what is the 80C limit?", Intent.FINANCIAL_QA, {"rules_lookup", "tax_calc", "calculator"}),
    ("Compare SBI and HDFC home loan rates", Intent.COMPARISON,
     {"research", "web_search", "web_fetch", "finance_calc", "calculator"}),
])
def test_only_plausible_tools_are_offered(message, intent, expected):
    assert candidate_tools(message, ir(intent), available={s["function"]["name"] for s in SCHEMAS}) == expected


def test_an_unrecognised_request_still_sees_everything():
    """Being slow is better than being wrong: when the wording gives nothing away, send them all."""
    chosen = candidate_tools("hmm, interesting", ir(Intent.GENERAL_QA))
    assert chosen == set()
    assert filter_schemas(SCHEMAS, chosen) == SCHEMAS


def test_filtering_shrinks_the_planner_prompt():
    from backend.prompts.planner import get_planner_system_prompt

    everything = get_planner_system_prompt(SCHEMAS)
    narrowed = get_planner_system_prompt(
        filter_schemas([{"function": {"name": n}} for n in ("calculator", "finance_calc")],
                       {"calculator", "finance_calc"}))
    assert len(narrowed) < len(everything)


class TestBareRecallFollowups:
    """"And my rent?" is the commonest follow-up there is. Classified as financial_qa it cost a
    53-second planner call on the live box that came back "no tools needed"."""

    @pytest.mark.parametrize("message", [
        "And my rent?", "my rent?", "what about my salary", "And my EMI?", "and my goal?",
        "ok, and my savings?", "what is my emergency fund",
    ])
    def test_reads_a_fact_back_without_the_planner(self, message):
        intent = IntentClassifier.classify(message)
        assert intent.intent is Intent.MEMORY_RECALL
        assert needs_no_tools(message, intent) is True

    @pytest.mark.parametrize("message", [
        "my rent is 22000",                    # a statement, not a question
        "should i pay my loan?",               # asks for a judgement
        "my home loan options?",               # asks for options
        "what about my tax for this year",     # the tax engine owns this
        "and my spending?",                    # the records tool owns this
        "and my friend?",                      # not a stored fact at all
        "what are the current rates on my FD?",  # wants live data
    ])
    def test_leaves_everything_else_to_the_planner(self, message):
        intent = IntentClassifier.classify(message)
        assert needs_no_tools(message, intent) is False


class TestIntentDoesNotOverruleTheWording:
    """A capital-gains question is intent "calculation". Adding finance_calc on that alone put its
    930-token schema — the largest of the eight — in front of the planner on a question plainly
    about tax, which at 39 tokens/sec of prefill is ~24 seconds for a tool it would never pick."""

    ALL = {"calculator", "web_search", "web_fetch", "research", "finance_calc", "tax_calc",
           "rules_lookup", "spendsy_data"}

    @pytest.mark.parametrize("message", [
        "I bought a flat in June 2010 for 30 lakh and sold it in August 2026 for 90 lakh. What is my tax?",
        "My tax this year will be about 1.5 lakh, TDS 40k, I've paid 10k advance tax. What do I pay and when?",
        "How much tax on a 13.75 lakh salary?",
    ])
    def test_a_tax_question_does_not_carry_the_finance_schema(self, message):
        keep = candidate_tools(message, IntentClassifier.classify(message), available=self.ALL)
        assert "tax_calc" in keep
        assert "finance_calc" not in keep

    @pytest.mark.parametrize("message", [
        "EMI on a 30 lakh loan at 8.4% for 25 years?",
        "How much do I need to invest monthly to reach 5 lakh in 3 years at 12%?",
    ])
    def test_a_money_question_still_carries_it(self, message):
        keep = candidate_tools(message, IntentClassifier.classify(message), available=self.ALL)
        assert "finance_calc" in keep

    def test_wording_that_gives_nothing_away_still_gets_the_default(self):
        """When nothing in the message names an engine, the intent is all there is to go on."""
        keep = candidate_tools("Calculate 18% of 2,35,000",
                               IntentClassifier.classify("Calculate 18% of 2,35,000"), available=self.ALL)
        assert {"calculator", "finance_calc"} <= keep

    def test_a_question_naming_both_keeps_both(self):
        message = "Should I prepay my home loan or put it in an ELSS to save tax?"
        keep = candidate_tools(message, IntentClassifier.classify(message), available=self.ALL)
        assert "finance_calc" in keep and "tax_calc" in keep
