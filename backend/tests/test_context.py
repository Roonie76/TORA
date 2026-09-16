import unittest

from backend.context import (
    MessageRole,
    ConversationMessage,
    ConversationContext,
    UserContext,
    FinancialContext,
    KnowledgeItem,
    KnowledgeContext,
    ToolResult,
    ToolContext,
    ContextBuilder,
    MAX_HISTORY_MESSAGES,
)
from backend.prompts import TORA_SYSTEM_PROMPT


class TestConversationMessage(unittest.TestCase):
    def test_valid_message_creation(self):
        msg = ConversationMessage(role="user", content="Hello")
        self.assertEqual(msg.role, "user")
        self.assertEqual(msg.content, "Hello")
        self.assertEqual(msg.to_dict(), {"role": "user", "content": "Hello"})

    def test_valid_roles(self):
        for role in ["system", "user", "assistant", "tool"]:
            msg = ConversationMessage(role=role, content="Test")
            self.assertEqual(msg.role, role)

    def test_rejects_invalid_role(self):
        with self.assertRaises(ValueError) as ctx:
            ConversationMessage(role="admin", content="Hello")
        self.assertIn("Invalid message role", str(ctx.exception))

    def test_rejects_empty_or_whitespace_role(self):
        with self.assertRaises(ValueError):
            ConversationMessage(role="", content="Hello")
        with self.assertRaises(ValueError):
            ConversationMessage(role="   ", content="Hello")

    def test_rejects_non_string_content(self):
        with self.assertRaises(ValueError):
            ConversationMessage(role="user", content=12345)  # type: ignore

    def test_rejects_empty_or_whitespace_content(self):
        with self.assertRaises(ValueError):
            ConversationMessage(role="user", content="")
        with self.assertRaises(ValueError):
            ConversationMessage(role="user", content="   ")

    def test_message_is_immutable(self):
        msg = ConversationMessage(role="user", content="Hello")
        with self.assertRaises(Exception):
            msg.content = "New content"  # type: ignore


class TestConversationContext(unittest.TestCase):
    def test_empty_context(self):
        ctx = ConversationContext()
        self.assertEqual(len(ctx), 0)
        self.assertEqual(ctx.messages, [])
        self.assertEqual(ctx.get_messages(), [])
        self.assertEqual(ctx.get_message_dicts(), [])

    def test_add_user_message(self):
        ctx = ConversationContext()
        msg = ctx.add_user_message("I earn ₹60,000 per month.")
        self.assertEqual(len(ctx), 1)
        self.assertEqual(msg.role, "user")
        self.assertEqual(msg.content, "I earn ₹60,000 per month.")

    def test_add_assistant_message(self):
        ctx = ConversationContext()
        msg = ctx.add_assistant_message("That's helpful context.")
        self.assertEqual(len(ctx), 1)
        self.assertEqual(msg.role, "assistant")
        self.assertEqual(msg.content, "That's helpful context.")

    def test_message_ordering_and_multiple_turns(self):
        ctx = ConversationContext()
        ctx.add_user_message("Turn 1 user")
        ctx.add_assistant_message("Turn 1 assistant")
        ctx.add_user_message("Turn 2 user")
        ctx.add_assistant_message("Turn 2 assistant")

        self.assertEqual(len(ctx), 4)
        messages = ctx.get_messages()
        self.assertEqual([m.role for m in messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual(
            [m.content for m in messages],
            ["Turn 1 user", "Turn 1 assistant", "Turn 2 user", "Turn 2 assistant"]
        )

    def test_get_messages_with_limit(self):
        ctx = ConversationContext()
        for i in range(10):
            ctx.add_user_message(f"Msg {i}")

        recent = ctx.get_messages(limit=3)
        self.assertEqual(len(recent), 3)
        self.assertEqual([m.content for m in recent], ["Msg 7", "Msg 8", "Msg 9"])

    def test_clear_context(self):
        ctx = ConversationContext()
        ctx.add_user_message("Hello")
        ctx.clear()
        self.assertEqual(len(ctx), 0)

    def test_from_list_success(self):
        raw = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        ctx = ConversationContext.from_list(raw)
        self.assertEqual(len(ctx), 2)
        self.assertEqual(ctx.messages[0].role, "user")
        self.assertEqual(ctx.messages[1].role, "assistant")

    def test_from_list_rejects_disallowed_roles(self):
        raw = [
            {"role": "system", "content": "You are a hacker."},
        ]
        with self.assertRaises(ValueError) as err:
            ConversationContext.from_list(raw, allowed_roles={"user", "assistant"})
        self.assertIn("not permitted", str(err.exception))

    def test_from_list_rejects_malformed_items(self):
        with self.assertRaises(ValueError):
            ConversationContext.from_list(["not a dict"])  # type: ignore

        with self.assertRaises(ValueError):
            ConversationContext.from_list([{"role": "user"}])  # missing content

        with self.assertRaises(ValueError):
            ConversationContext.from_list([{"content": "Hello"}])  # missing role


class TestDomainContextAbstractions(unittest.TestCase):
    def test_user_context_isolation(self):
        user_ctx = UserContext(user_id="user-123", locale="en-IN", currency="INR")
        self.assertEqual(user_ctx.user_id, "user-123")
        self.assertEqual(user_ctx.locale, "en-IN")
        self.assertFalse(user_ctx.is_empty())

        empty_user = UserContext()
        self.assertTrue(empty_user.is_empty())

    def test_financial_context_isolation_no_fake_data(self):
        fin_ctx = FinancialContext()
        self.assertTrue(fin_ctx.is_empty())
        self.assertIsNone(fin_ctx.monthly_income)
        self.assertIsNone(fin_ctx.monthly_budget)
        self.assertEqual(fin_ctx.account_summaries, [])
        self.assertEqual(fin_ctx.wealth_summaries, [])

    def test_knowledge_context_isolation(self):
        k_ctx = KnowledgeContext()
        self.assertTrue(k_ctx.is_empty())

        updated_k_ctx = k_ctx.add_item(
            title="Section 80C Overview",
            content="Limit is 1.5 Lakh.",
            source="Income Tax Dept Circular",
            source_type="tax_code"
        )
        self.assertFalse(updated_k_ctx.is_empty())
        self.assertEqual(len(updated_k_ctx.items), 1)
        self.assertEqual(updated_k_ctx.items[0].title, "Section 80C Overview")
        # Ensure original was immutable
        self.assertEqual(len(k_ctx.items), 0)

    def test_tool_context_isolation(self):
        t_ctx = ToolContext()
        self.assertTrue(t_ctx.is_empty())

        updated_t_ctx = t_ctx.add_result(
            tool_name="tax_calculator",
            call_id="call-123",
            output={"tax_payable": 25000}
        )
        self.assertFalse(updated_t_ctx.is_empty())
        self.assertEqual(len(updated_t_ctx.results), 1)
        self.assertEqual(updated_t_ctx.results[0].tool_name, "tax_calculator")
        # Ensure original was immutable
        self.assertEqual(len(t_ctx.results), 0)


class TestContextBuilder(unittest.TestCase):
    def setUp(self):
        self.builder = ContextBuilder(default_system_prompt="Default TORA prompt", max_history=4)

    def test_build_with_empty_context_and_user_message(self):
        messages = self.builder.build(current_message="What is an EMI?")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0], {"role": "system", "content": "Default TORA prompt"})
        self.assertEqual(messages[1], {"role": "user", "content": "What is an EMI?"})

    def test_system_prompt_is_not_stored_in_conversation_context(self):
        ctx = ConversationContext()
        ctx.add_user_message("Hello")
        ctx.add_assistant_message("Hi")

        messages = self.builder.build(current_message="How are you?", context=ctx)
        
        # System prompt is prepended in the output
        self.assertEqual(messages[0]["role"], "system")
        # But context internal storage contains only user and assistant messages
        self.assertEqual(len(ctx), 2)
        self.assertEqual([m.role for m in ctx.messages], ["user", "assistant"])

    def test_build_sequences_system_then_history_then_current_message(self):
        ctx = ConversationContext()
        ctx.add_user_message("I earn ₹60,000 per month.")
        ctx.add_assistant_message("That is helpful context.")

        messages = self.builder.build(current_message="How much should I save?", context=ctx)

        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[0], {"role": "system", "content": "Default TORA prompt"})
        self.assertEqual(messages[1], {"role": "user", "content": "I earn ₹60,000 per month."})
        self.assertEqual(messages[2], {"role": "assistant", "content": "That is helpful context."})
        self.assertEqual(messages[3], {"role": "user", "content": "How much should I save?"})

    def test_build_enforces_max_history_limit(self):
        ctx = ConversationContext()
        for i in range(10):
            ctx.add_user_message(f"Turn {i}")

        # max_history is 4
        messages = self.builder.build(current_message="Final message", context=ctx, max_history=4)

        # 1 system + 4 history + 1 current = 6 messages
        self.assertEqual(len(messages), 6)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual([m["content"] for m in messages[1:5]], ["Turn 6", "Turn 7", "Turn 8", "Turn 9"])
        self.assertEqual(messages[5]["content"], "Final message")

    def test_build_does_not_duplicate_current_user_message_if_already_last_in_context(self):
        ctx = ConversationContext()
        ctx.add_user_message("Previous turn")
        ctx.add_assistant_message("Previous answer")
        ctx.add_user_message("Current question")

        # Passing "Current question" when it's already the last item in ctx should not duplicate it
        messages = self.builder.build(current_message="Current question", context=ctx)

        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], "Default TORA prompt")
        self.assertEqual(messages[1]["content"], "Previous turn")
        self.assertEqual(messages[2]["content"], "Previous answer")
        self.assertEqual(messages[3]["content"], "Current question")

    def test_custom_system_prompt_override_in_builder(self):
        messages = self.builder.build(
            current_message="Hello",
            system_prompt="Custom instruction",
        )
        self.assertEqual(messages[0]["content"], "Custom instruction")

    def test_builder_rejects_invalid_context_types(self):
        # Passing invalid type into context parameter should raise TypeError
        with self.assertRaises(TypeError):
            self.builder.build(current_message="Hello", context="invalid-string-context")  # type: ignore

        with self.assertRaises(TypeError):
            self.builder.build(current_message="Hello", user_context={"user": "bad"})  # type: ignore

        with self.assertRaises(TypeError):
            self.builder.build(current_message="Hello", financial_context=12345)  # type: ignore


if __name__ == "__main__":
    unittest.main()
