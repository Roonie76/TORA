import unittest
from backend.context.extractor import FactExtractor, FactManager, parse_inr_amount
from backend.context.financial import FinancialProfile, FactStatus


class TestFactExtractor(unittest.TestCase):

    def test_parse_inr_amount(self):
        self.assertEqual(parse_inr_amount("75,000"), 75000.0)
        self.assertEqual(parse_inr_amount("₹82,000"), 82000.0)
        self.assertEqual(parse_inr_amount("82k"), 82000.0)
        self.assertEqual(parse_inr_amount("₹1.8 lakh"), 180000.0)
        self.assertEqual(parse_inr_amount("1.65 lakh"), 165000.0)
        self.assertEqual(parse_inr_amount("2.5L"), 250000.0)
        self.assertEqual(parse_inr_amount("₹4 lakh"), 400000.0)
        self.assertEqual(parse_inr_amount("6 lakh"), 600000.0)
        self.assertEqual(parse_inr_amount("1.5 crore"), 15000000.0)
        self.assertIsNone(parse_inr_amount(""))
        self.assertIsNone(parse_inr_amount("abc"))

    def test_extract_income_and_rent(self):
        msg = "I'm 27, living in Gurgaon, and I take home ₹75,000 per month."
        cands = FactExtractor.extract_candidate_facts(msg)
        names = [c["name"] for c in cands]
        self.assertIn("income", names)
        inc = next(c for c in cands if c["name"] == "income")
        self.assertEqual(inc["value"], 75000.0)

        msg2 = "My rent is ₹18,000 a month."
        cands2 = FactExtractor.extract_candidate_facts(msg2)
        rent = next(c for c in cands2 if c["name"] == "rent")
        self.assertEqual(rent["value"], 18000.0)

    def test_extract_expenses_and_loans(self):
        msg = "Food is ₹8,000, commute is ₹4,000, and I have a ₹12,000 monthly EMI for personal loan."
        cands = FactExtractor.extract_candidate_facts(msg)
        names = {c["name"]: c["value"] for c in cands}
        self.assertEqual(names.get("food"), 8000.0)
        self.assertEqual(names.get("commute"), 4000.0)
        self.assertEqual(names.get("personal_loan_emi"), 12000.0)

    def test_extract_credit_card_and_apr(self):
        msg = "I also have a credit card balance of ₹1.8 lakh at 36% annual interest."
        cands = FactExtractor.extract_candidate_facts(msg)
        names = {c["name"]: c["value"] for c in cands}
        self.assertEqual(names.get("credit_card_debt"), 180000.0)
        self.assertEqual(names.get("credit_card_apr"), 36.0)

    def test_extract_savings_and_assets(self):
        msg = "I have ₹35,000 in savings, ₹2.5 lakh in mutual funds, and ₹4 lakh in gold."
        cands = FactExtractor.extract_candidate_facts(msg)
        names = {c["name"]: c["value"] for c in cands}
        self.assertEqual(names.get("savings"), 35000.0)
        self.assertEqual(names.get("mutual_funds"), 250000.0)
        self.assertEqual(names.get("gold"), 400000.0)

    def test_extract_goal(self):
        msg = "I also want to buy a car in about two years for around ₹6 lakh."
        cands = FactExtractor.extract_candidate_facts(msg)
        goal = next((c for c in cands if c["name"] == "car_goal"), None)
        self.assertIsNotNone(goal)
        self.assertEqual(goal["value"], 600000.0)

    def test_extract_hypothetical_assumption(self):
        msg = "Would your answer change if my salary increases by another ₹10,000 next year?"
        cands = FactExtractor.extract_candidate_facts(msg)
        self.assertTrue(FactExtractor.is_hypothetical(msg))
        for c in cands:
            self.assertEqual(c["status"], FactStatus.HYPOTHETICAL.value)

    def test_fact_manager_updates_profile(self):
        profile = FinancialProfile()
        # Turn 1
        cands1 = FactExtractor.extract_candidate_facts("I take home ₹75,000 per month.")
        FactManager.apply_candidates(profile, cands1)
        self.assertEqual(profile.income.value, 75000.0)

        # Turn 31 correction
        cands2 = FactExtractor.extract_candidate_facts("Small correction: my salary just increased to ₹82,000 per month.")
        FactManager.apply_candidates(profile, cands2)
        self.assertEqual(profile.income.value, 82000.0)
        self.assertEqual(profile.income.previous_value, 75000.0)


if __name__ == "__main__":
    unittest.main()
