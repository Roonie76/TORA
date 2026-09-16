import unittest
from backend.context.summarizer import ConversationSummarizer
from backend.context.conversation import ConversationMessage
from backend.context.financial import FinancialProfile


class TestSummarizer(unittest.TestCase):

    def test_empty_messages_summary(self):
        summary = ConversationSummarizer.summarize_messages([])
        self.assertEqual(summary, "")

    def test_summarize_conversation_extracts_key_topics(self):
        messages = [
            ConversationMessage(role="user", content="I want to plan my budget with 50/30/20."),
            ConversationMessage(role="assistant", content="We will use the 50/30/20 framework."),
            ConversationMessage(role="user", content="I have credit card debt at 36% APR."),
            ConversationMessage(role="assistant", content="You must use the Debt Avalanche method for the 36% debt."),
            ConversationMessage(role="user", content="I want to save for a car in 2 years for ₹6 lakh."),
            ConversationMessage(role="assistant", content="You will need to save ₹25,000 per month for the car."),
        ]
        profile = FinancialProfile()
        profile.set_fact("income", 82000)
        profile.set_fact("credit_card", 165000)

        summary = ConversationSummarizer.summarize_messages(messages, profile)
        self.assertIn("## Conversation History Summary", summary)
        self.assertIn("budget", summary.lower())
        self.assertIn("debt", summary.lower())
        self.assertIn("car", summary.lower())
        self.assertIn("50/30/20", summary)
        self.assertIn("Debt Avalanche", summary)


    def test_summary_never_invents_calculations(self):
        """Keywords alone must not produce canned calculations or advice."""
        messages = [
            ConversationMessage(role="user", content="Tell me about interest on savings accounts."),
            ConversationMessage(role="assistant", content="Savings accounts pay interest quarterly."),
            ConversationMessage(role="user", content="Should I liquidate my mutual fund?"),
            ConversationMessage(role="assistant", content="That depends on your goals and the interest you pay on debts; I would liquidate only after reviewing exit loads."),
        ]
        summary = ConversationSummarizer.summarize_messages(messages)
        for invented in ("36%", "10% Gold Loan", "Debt Avalanche", "risk-free", "₹25,000", "6 Lakh"):
            self.assertNotIn(invented, summary)

    def test_assistant_figures_are_quoted_verbatim(self):
        messages = [
            ConversationMessage(role="user", content="What EMI for 20 lakh at 8.5% for 20 years?"),
            ConversationMessage(role="assistant", content="Here is the math. Your EMI is about ₹17,356 per month."),
        ]
        summary = ConversationSummarizer.summarize_messages(messages)
        self.assertIn("Your EMI is about ₹17,356 per month.", summary)


if __name__ == "__main__":
    unittest.main()
