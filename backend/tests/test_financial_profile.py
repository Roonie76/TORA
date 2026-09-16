import unittest
from backend.context.financial import (
    FinancialProfile,
    FinancialFact,
    FactStatus,
)


class TestFinancialProfile(unittest.TestCase):

    def test_empty_profile(self):
        profile = FinancialProfile()
        self.assertTrue(profile.is_empty())
        self.assertEqual(profile.to_context_string(), "")
        self.assertIsNone(profile.get_fact("income"))

    def test_fact_creation_and_formatting(self):
        fact = FinancialFact(name="income", value=75000, period="monthly")
        self.assertEqual(fact.name, "income")
        self.assertEqual(fact.value, 75000)
        self.assertEqual(fact.format_value(), "₹75,000")
        self.assertEqual(fact.status, FactStatus.CURRENT.value)

        fact_lakh = FinancialFact(name="mutual_funds", value=250000)
        self.assertEqual(fact_lakh.format_value(), "₹2.5 Lakh")

        fact_apr = FinancialFact(name="apr", value=36, period="interest_rate")
        self.assertEqual(fact_apr.format_value(), "36%")

    def test_profile_set_and_update_income(self):
        profile = FinancialProfile()
        profile.set_fact("income", 75000, category="income")
        self.assertFalse(profile.is_empty())
        self.assertEqual(profile.income.value, 75000)
        self.assertIsNone(profile.income.previous_value)

        # Update salary
        profile.set_fact("income", 82000, category="income")
        self.assertEqual(profile.income.value, 82000)
        self.assertEqual(profile.income.previous_value, 75000)
        self.assertEqual(len(profile.history), 2)
        self.assertEqual(profile.history[1]["previous_value"], 75000)
        self.assertEqual(profile.history[1]["new_value"], 82000)

    def test_profile_set_and_update_rent(self):
        profile = FinancialProfile()
        profile.set_fact("rent", 18000, category="rent")
        self.assertEqual(profile.rent.value, 18000)

        # Update rent
        profile.set_fact("rent", 20000, category="rent")
        self.assertEqual(profile.rent.value, 20000)
        self.assertEqual(profile.rent.previous_value, 18000)

    def test_profile_set_and_update_debt(self):
        profile = FinancialProfile()
        profile.set_fact("credit_card", 180000, category="debt")
        self.assertEqual(profile.debts["credit_card"].value, 180000)

        # Update credit card balance
        profile.set_fact("credit_card", 165000, category="debt")
        self.assertEqual(profile.debts["credit_card"].value, 165000)
        self.assertEqual(profile.debts["credit_card"].previous_value, 180000)

    def test_profile_hypothetical_isolation(self):
        profile = FinancialProfile()
        profile.set_fact("income", 82000, category="income")
        # Add hypothetical future raise
        profile.set_fact("future_raise", 10000, category="assumption", notes="Next year hypothetical raise")
        
        # Real income remains 82000
        self.assertEqual(profile.income.value, 82000)
        self.assertEqual(len(profile.assumptions), 1)
        self.assertEqual(profile.assumptions[0].status, FactStatus.HYPOTHETICAL.value)
        self.assertEqual(profile.assumptions[0].value, 10000)

    def test_to_context_string_rendering(self):
        profile = FinancialProfile()
        profile.set_fact("income", 82000, category="income")
        profile.set_fact("rent", 20000, category="rent")
        profile.set_fact("food", 8000, category="expense")
        profile.set_fact("credit_card", 165000, category="debt")
        profile.set_fact("savings", 35000, category="savings")
        profile.set_fact("mutual_funds", 250000, category="investment")
        profile.set_fact("car_goal", 600000, category="goal")
        profile.set_fact("future_raise", 10000, category="assumption", notes="Next year")

        context_str = profile.to_context_string()
        self.assertIn("## User Verified Financial Profile", context_str)
        self.assertIn("Monthly Income: ₹82,000", context_str)
        self.assertIn("Monthly Rent: ₹20,000", context_str)
        self.assertIn("Expense (Food): ₹8,000", context_str)
        self.assertIn("Debt/Liability (Credit_card): ₹1.65 Lakh", context_str)
        self.assertIn("Liquid Savings: ₹35,000", context_str)
        self.assertIn("Asset/Investment (Mutual_funds): ₹2.5 Lakh", context_str)
        self.assertIn("Target Goal (Car_goal): ₹6 Lakh", context_str)
        self.assertIn("[HYPOTHETICAL] Future_raise: ₹10,000", context_str)


if __name__ == "__main__":
    unittest.main()
