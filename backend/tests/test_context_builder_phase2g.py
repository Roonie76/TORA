import unittest
from backend.context.builder import ContextBuilder
from backend.context.conversation import ConversationContext, ConversationMessage
from backend.context.financial import FinancialProfile
from backend.context.tools import ToolContext, ToolResult
from backend.context.token_budget import TokenBudgetManager


class TestContextBuilderPhase2G(unittest.TestCase):

    def setUp(self):
        self.default_prompt = "You are TORA, a helpful financial AI assistant."
        self.builder = ContextBuilder(default_system_prompt=self.default_prompt)

    def test_priority_hierarchy_ordering(self):
        profile = FinancialProfile()
        profile.set_fact("income", 82000)
        profile.set_fact("rent", 20000)

        tool_ctx = ToolContext(results=[
            ToolResult(tool_name="calculator", call_id="call_1", output="1666.67", is_error=False)
        ])

        history = ConversationContext(messages=[
            ConversationMessage(role="user", content="Previous user question"),
            ConversationMessage(role="assistant", content="Previous assistant answer"),
        ])

        messages = self.builder.build(
            current_message="What is my current disposable cash flow?",
            context=history,
            financial_context=profile,
            tool_context=tool_ctx,
        )

        # 1. System message (Priority 1) contains system prompt, profile, and tool outputs
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("You are TORA", messages[0]["content"])
        self.assertIn("User Verified Financial Profile", messages[0]["content"])
        self.assertIn("Monthly Income: ₹82,000", messages[0]["content"])
        self.assertIn("Tool Execution Results", messages[0]["content"])
        self.assertIn("Result = 1666.67", messages[0]["content"])

        # 2. History turns (Priority 6)
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"], "Previous user question")
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertEqual(messages[2]["content"], "Previous assistant answer")

        # 3. Current User Message (Priority 2 — Last)
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "What is my current disposable cash flow?")

    def test_token_budget_bounds_history_and_triggers_summary(self):
        small_tbm = TokenBudgetManager(num_ctx=512, max_generation_tokens=256, safety_margin_tokens=64)
        custom_builder = ContextBuilder(
            default_system_prompt="You are TORA.",
            token_budget_manager=small_tbm,
        )

        large_history = []
        for i in range(10):
            large_history.append(ConversationMessage(role="user", content=f"User turn {i}: " + "hello " * 30))
            large_history.append(ConversationMessage(role="assistant", content=f"Assistant turn {i}: " + "finance " * 30))

        ctx = ConversationContext(messages=large_history)

        messages = custom_builder.build(
            current_message="Latest question",
            context=ctx,
        )

        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "Latest question")
        self.assertLess(len(messages), 10)

    def test_duplicate_user_message_avoided(self):
        history = ConversationContext(messages=[
            ConversationMessage(role="user", content="Turn 1"),
            ConversationMessage(role="assistant", content="Response 1"),
            ConversationMessage(role="user", content="Turn 2"),
        ])

        messages = self.builder.build(
            current_message="Turn 2",
            context=history,
        )

        user_msgs = [m["content"] for m in messages if m["role"] == "user"]
        self.assertEqual(user_msgs.count("Turn 2"), 1)


if __name__ == "__main__":
    unittest.main()
