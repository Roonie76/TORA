import unittest
from backend.context.token_budget import (
    TokenBudgetManager,
    estimate_tokens,
    estimate_message_tokens,
    estimate_messages_tokens,
    CHARS_PER_TOKEN,
    MESSAGE_OVERHEAD_TOKENS,
    DEFAULT_NUM_CTX,
    DEFAULT_MAX_GENERATION_TOKENS,
)
from backend.context.conversation import ConversationMessage


class TestTokenBudget(unittest.TestCase):

    def test_token_estimation_basic(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens(None), 0)
        # 35 chars / 3.5 = 10 tokens
        self.assertEqual(estimate_tokens("a" * 35), 10)
        # 36 chars / 3.5 = 10.28 -> 11 tokens (conservative ceiling)
        self.assertEqual(estimate_tokens("a" * 36), 11)

    def test_message_tokens_estimation(self):
        msg = {"role": "user", "content": "Hello TORA"}
        # 'Hello TORA' is 10 chars -> 3 tokens; 'user' is 4 chars -> 2 tokens; overhead = 4 -> 9 tokens
        tokens = estimate_message_tokens(msg)
        self.assertGreater(tokens, 0)
        self.assertEqual(tokens, estimate_tokens("Hello TORA") + estimate_tokens("user") + MESSAGE_OVERHEAD_TOKENS)

    def test_messages_tokens_estimation(self):
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi! How can I help you?"},
        ]
        total = estimate_messages_tokens(msgs)
        expected = estimate_message_tokens(msgs[0]) + estimate_message_tokens(msgs[1])
        self.assertEqual(total, expected)

    def test_token_budget_manager_initialization(self):
        tbm = TokenBudgetManager(num_ctx=8192, max_generation_tokens=2048, safety_margin_tokens=256)
        self.assertEqual(tbm.num_ctx, 8192)
        self.assertEqual(tbm.max_prompt_budget, 8192 - 2048 - 256)

        with self.assertRaises(ValueError):
            TokenBudgetManager(num_ctx=256)  # Below min 512

        with self.assertRaises(ValueError):
            TokenBudgetManager(num_ctx=1024, max_generation_tokens=1024)

    def test_calculate_remaining_budget(self):
        tbm = TokenBudgetManager(num_ctx=4096, max_generation_tokens=1024, safety_margin_tokens=256)
        # max_prompt_budget = 4096 - 1024 - 256 = 2816
        rem = tbm.calculate_remaining_budget(
            system_tokens=500,
            user_message_tokens=100,
            financial_profile_tokens=200,
            tool_tokens=300,
            summary_tokens=100,
        )
        self.assertEqual(rem, 2816 - (500 + 100 + 200 + 300 + 100))

    def test_fit_history_small_history_all_fit(self):
        tbm = TokenBudgetManager(num_ctx=8192)
        history = [
            ConversationMessage(role="user", content="Turn 1"),
            ConversationMessage(role="assistant", content="Response 1"),
        ]
        fitted, dropped = tbm.fit_history(history, available_budget_tokens=1000)
        self.assertEqual(len(fitted), 2)
        self.assertEqual(len(dropped), 0)
        self.assertEqual(fitted[0].content, "Turn 1")
        self.assertEqual(fitted[1].content, "Response 1")

    def test_fit_history_large_history_truncated_chronological(self):
        tbm = TokenBudgetManager(num_ctx=8192)
        history = [
            ConversationMessage(role="user", content="Turn 1: " + "a" * 1000),
            ConversationMessage(role="assistant", content="Response 1: " + "a" * 1000),
            ConversationMessage(role="user", content="Turn 2: " + "a" * 100),
            ConversationMessage(role="assistant", content="Response 2: " + "a" * 100),
        ]
        # Each large turn is ~300 tokens; small turn is ~40 tokens
        # Give budget of 150 tokens -> only Turn 2 and Response 2 should fit
        fitted, dropped = tbm.fit_history(history, available_budget_tokens=150)
        self.assertEqual(len(fitted), 2)
        self.assertEqual(len(dropped), 2)
        self.assertEqual(fitted[0].content, history[2].content)
        self.assertEqual(fitted[1].content, history[3].content)
        self.assertEqual(dropped[0].content, history[0].content)
        self.assertEqual(dropped[1].content, history[1].content)

    def test_fit_history_zero_budget(self):
        tbm = TokenBudgetManager(num_ctx=8192)
        history = [ConversationMessage(role="user", content="Turn 1")]
        fitted, dropped = tbm.fit_history(history, available_budget_tokens=0)
        self.assertEqual(fitted, [])
        self.assertEqual(len(dropped), 1)


if __name__ == "__main__":
    unittest.main()
