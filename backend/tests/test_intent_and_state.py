"""Phase 3A — intent classification, follow-up resolution, topic pinning and research memory."""
import pytest

from backend.context import ContextBuilder, ToolContext
from backend.state import ConversationState, Intent, IntentClassifier, find_entities, find_product


def turn(state, message):
    state.begin_turn()
    result = IntentClassifier.classify(message, state)
    state.observe(message, result)
    return result


def research_output(query, entities_rates):
    return {
        "query": query,
        "overall_status": "VERIFIED_PRIMARY",
        "conclusions": [
            {"entity": e, "synthesized_statement": f"{e} official rate for Home Loan is starting from {r}% p.a.",
             "qualifiers": ["starting from"], "confidence": "HIGH", "provenance_urls": [f"https://{d}/rates"]}
            for e, r, d in entities_rates
        ],
        "sources": [{"domain": d, "authority_level": "HIGH", "is_official": True} for _, _, d in entities_rates],
        "conflicts": [],
    }


def add_research(state, query, rows):
    ctx = ToolContext().add_result(tool_name="research", call_id="r", output=research_output(query, rows))
    state.record_tool_results(ctx, query)


class TestEntityAndProduct:
    def test_entities_in_order_without_duplicates(self):
        assert find_entities("Compare SBI, HDFC and ICICI Bank, then State Bank again") == ["SBI", "HDFC Bank", "ICICI Bank"]

    def test_lic_housing_not_double_counted(self):
        assert find_entities("LIC Housing Finance rates") == ["LIC Housing Finance"]

    @pytest.mark.parametrize("text, product", [
        ("best FD rates", "fixed deposit"), ("home loan EMI", "home loan"),
        ("new tax regime slabs", "income tax"), ("hello there", None),
    ])
    def test_products(self, text, product):
        assert find_product(text) == product

    def test_no_false_entity_inside_words(self):
        assert find_entities("the bobcat ate a public sandwich") == []


class TestIntent:
    @pytest.mark.parametrize("message, intent", [
        ("Compare SBI and HDFC home loan rates", Intent.COMPARISON),
        ("What is the current SBI FD rate?", Intent.RESEARCH),
        ("What is 20% of 60000?", Intent.CALCULATION),
        ("What would the EMI for 20 lakh at 8.5% for 20 years be?", Intent.CALCULATION),
        ("What if I increase my SIP by 5000?", Intent.WHAT_IF),
        ("What was my previous credit card balance?", Intent.MEMORY_RECALL),
        ("Help me plan my budget", Intent.PLANNING),
        ("How does compounding work in a PPF?", Intent.FINANCIAL_QA),
        ("Write a haiku about rain", Intent.GENERAL_QA),
        ("why?", Intent.CLARIFICATION),
    ])
    def test_classification(self, message, intent):
        assert IntentClassifier.classify(message, ConversationState()).intent == intent

    def test_memory_commands_take_precedence(self):
        r = IntentClassifier.classify("forget my rent", None, memory_commands=[{"action": "delete", "name": "rent"}])
        assert r.intent == Intent.MEMORY_DELETE and r.skips_planner

    def test_whats_my_tax_is_not_memory_recall(self):
        assert IntentClassifier.classify("What's my tax under the new regime?").intent != Intent.MEMORY_RECALL


class TestFollowUps:
    def setup_method(self):
        self.state = ConversationState()
        turn(self.state, "Compare SBI, HDFC and ICICI home loan interest rates")
        add_research(self.state, "SBI HDFC ICICI home loan rates",
                     [("SBI", 7.25, "sbi.co.in"), ("HDFC Bank", 7.9, "www.hdfcbank.com")])

    @pytest.mark.parametrize("message", [
        "Which of those sources was the primary official bank?",
        "What about HDFC?",
        "and SBI?",
        "Which one was cheaper?",
    ])
    def test_short_followups_resolve_to_active_topic(self, message):
        r = turn(self.state, message)
        assert r.is_followup
        assert r.intent == Intent.RESEARCH_FOLLOWUP
        assert "home loan" in r.resolved_query
        assert self.state.active_topic_key == "home-loan"

    def test_followup_entity_is_named_in_resolution(self):
        r = turn(self.state, "What about Axis?")
        assert "Axis Bank home loan" in r.resolved_query

    def test_stored_research_covers_known_but_not_new_entities(self):
        topic = self.state.active_topic()
        assert topic.covers_entities(["SBI"])
        assert topic.covers_entities(["HDFC Bank"])
        assert not topic.covers_entities(["Axis Bank"])

    def test_explicit_new_question_is_not_followup(self):
        r = turn(self.state, "What are the current FD rates at Axis Bank for 1 year?")
        assert not r.is_followup
        assert self.state.active_topic_key == "fixed-deposit"


class TestTopicPinning:
    def test_return_to_earlier_topic_restores_its_research(self):
        state = ConversationState()
        turn(state, "Compare SBI and HDFC home loan interest rates")
        add_research(state, "home loan rates", [("SBI", 7.25, "sbi.co.in")])
        turn(state, "What are the best FD rates now?")
        turn(state, "Explain the new tax regime slabs")
        assert state.active_topic_key == "income-tax"
        turn(state, "Back to the home loan comparison — which was cheapest?")
        assert state.active_topic_key == "home-loan"
        assert "7.25" in state.render_external()
        trusted = state.render_trusted()
        assert "Active topic: home loan interest rates" in trusted
        assert "fixed deposit" in trusted and "income tax" in trusted

    def test_memory_questions_do_not_switch_topic(self):
        state = ConversationState()
        turn(state, "Compare SBI and HDFC home loan rates")
        turn(state, "What was my previous credit card balance?")
        assert state.active_topic_key == "home-loan"

    def test_topic_eviction_keeps_active(self):
        state = ConversationState()
        products = ["home loan", "personal loan", "car loan", "gold loan", "education loan", "credit card",
                    "FD", "RD", "savings account", "mutual fund", "SIP", "PPF", "NPS", "GST"]
        for p in products:
            turn(state, f"Tell me about {p} rates")
        assert len(state.topics) <= 12
        assert state.active_topic_key in state.topics


class TestRenderingAndIsolation:
    def test_research_recall_goes_to_external_block_not_system(self):
        state = ConversationState()
        turn(state, "Compare SBI and HDFC home loan rates")
        add_research(state, "home loan rates", [("SBI", 7.25, "sbi.co.in")])
        turn(state, "What about SBI?")
        messages = ContextBuilder(default_system_prompt="S").build(
            current_message="What about SBI?", conversation_state=state, current_turn=state.turn_count
        )
        system = messages[0]["content"]
        assert "## Conversation State" in system
        assert "7.25" not in system
        assert messages[-2]["content"].startswith("<external_data")
        assert "7.25" in messages[-2]["content"]

    def test_same_turn_research_not_duplicated(self):
        state = ConversationState()
        turn(state, "Compare SBI and HDFC home loan rates")
        add_research(state, "home loan rates", [("SBI", 7.25, "sbi.co.in")])
        assert state.render_external(exclude_turn=state.turn_count) == ""

    def test_web_search_results_remembered(self):
        state = ConversationState()
        turn(state, "Current gold loan rates?")
        ctx = ToolContext().add_result(tool_name="web_search", call_id="w", output={
            "query": "gold loan rates", "results": [
                {"title": "Muthoot gold loan 9.5%", "domain": "muthootfinance.com", "snippet": "from 9.5% p.a.",
                 "url": "https://muthootfinance.com"}]})
        state.record_tool_results(ctx, "gold loan rates")
        assert state.active_topic().covers_entities(["Muthoot Finance"])
        turn(state, "and Manappuram?")
        assert "Muthoot gold loan 9.5%" in state.render_external()

    def test_calculations_recorded_and_rendered(self):
        state = ConversationState()
        turn(state, "What is 20% of 60000?")
        ctx = ToolContext().add_result(tool_name="calculator", call_id="c",
                                       output={"expression": "0.2*60000", "result": 12000})
        state.record_tool_results(ctx, "")
        assert "0.2*60000 = 12000" in state.render_trusted()

    def test_round_trip(self):
        state = ConversationState()
        turn(state, "Compare SBI and HDFC home loan rates")
        add_research(state, "home loan rates", [("SBI", 7.25, "sbi.co.in")])
        turn(state, "What about HDFC?")
        restored = ConversationState.from_dict(state.to_dict())
        assert restored.to_dict() == state.to_dict()
        assert restored.render_external() == state.render_external()

    def test_render_is_bounded(self):
        state = ConversationState()
        turn(state, "Compare home loan rates")
        for i in range(10):
            add_research(state, f"q{i}", [(f"SBI", 7 + i / 10, "sbi.co.in")] * 8)
        assert len(state.render_external()) <= 2600
        assert len(state.active_topic().research) == 3
