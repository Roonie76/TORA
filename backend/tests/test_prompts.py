import unittest

from backend.prompts import TORA_SYSTEM_PROMPT, get_tora_system_prompt


class TestToraPrompt(unittest.TestCase):
    def test_prompt_exists_and_is_string(self):
        self.assertIsInstance(TORA_SYSTEM_PROMPT, str)
        self.assertTrue(len(TORA_SYSTEM_PROMPT.strip()) > 0)

    def test_get_tora_system_prompt_returns_same_constant(self):
        self.assertIs(get_tora_system_prompt(), TORA_SYSTEM_PROMPT)

    def test_prompt_has_general_and_finance_sections(self):
        """The prompt should define both general-purpose and finance capabilities.
        Rather than checking exact words (which breaks on rewording), we verify
        the structural sections exist."""
        self.assertIn("## Core Persona", TORA_SYSTEM_PROMPT)
        self.assertIn("## Specialization", TORA_SYSTEM_PROMPT)
        self.assertIn("## Context Discipline", TORA_SYSTEM_PROMPT)
        self.assertIn("## Accuracy", TORA_SYSTEM_PROMPT)

    def test_prompt_identifies_as_tora_for_spendsy(self):
        # This is the one identity assertion that should never change
        self.assertIn("TORA", TORA_SYSTEM_PROMPT)
        self.assertIn("Spendsy", TORA_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()


class TestPromptSlicing(unittest.TestCase):
    """Only the sections a turn can use are sent (Phase 15)."""

    def test_every_slice_keeps_identity_trust_and_confidentiality(self):
        from backend.prompts.tora import ALWAYS, slice_prompt

        for kwargs in ({}, {"intent": "general_qa"}, {"intent": "calculation", "tools_used": ["finance_calc"]},
                       {"intent": "research_followup", "has_history": True}):
            text = slice_prompt(**kwargs)
            self.assertTrue(text.startswith("You are TORA"))
            for section in ALWAYS:
                self.assertIn(f"## {section}", text)

    def test_slices_are_cut_from_the_full_prompt_and_are_smaller(self):
        from backend.prompts.tora import TORA_SYSTEM_PROMPT, slice_prompt

        small_talk = slice_prompt(intent="general_qa")
        self.assertLess(len(small_talk), len(TORA_SYSTEM_PROMPT) * 0.6)
        for line in small_talk.splitlines():
            if line.strip() and not line.startswith("## "):
                self.assertIn(line, TORA_SYSTEM_PROMPT)   # never written separately

    def test_sections_appear_only_when_the_turn_can_use_them(self):
        from backend.prompts.tora import slice_prompt

        plain = slice_prompt(intent="general_qa")
        self.assertNotIn("## Spendsy Records", plain)
        self.assertNotIn("## Uploaded Documents", plain)
        self.assertNotIn("## Citing the Law", plain)
        self.assertNotIn("## People Under Debt Stress", plain)

        self.assertIn("## Spendsy Records", slice_prompt(tools_used=["spendsy_data"]))
        self.assertIn("## Citing the Law", slice_prompt(tools_used=["rules_lookup"]))
        self.assertIn("## Tax Years", slice_prompt(tools_used=["tax_calc"]))
        self.assertIn("## Uploaded Documents", slice_prompt(has_documents=True))
        self.assertIn("## Conversation State", slice_prompt(has_history=True))

    def test_debt_stress_section_follows_the_case_not_just_the_tool(self):
        from backend.prompts.tora import slice_prompt

        by_tool = slice_prompt(intent="planning", tools_used=["finance_calc"], operations=["debt_rescue_plan"])
        self.assertIn("## People Under Debt Stress", by_tool)
        # someone in distress who triggered no tool still gets the section
        by_words = slice_prompt(intent="general_qa", debt_context=True)
        self.assertIn("## People Under Debt Stress", by_words)

    def test_section_order_matches_the_full_prompt(self):
        from backend.prompts.tora import SECTION_ORDER, slice_prompt

        text = slice_prompt(intent="planning",
                            tools_used=["finance_calc", "tax_calc", "spendsy_data", "rules_lookup"],
                            operations=["debt_rescue_plan"], has_documents=True, has_history=True)
        found = [t for t in SECTION_ORDER if f"## {t}" in text]
        positions = [text.index(f"## {t}") for t in found]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(found), len(SECTION_ORDER))      # everything, when everything applies


class TestStickySlices(unittest.TestCase):
    """The slice only grows within a conversation, so the KV cache prefix survives."""

    def test_a_later_turn_extends_the_earlier_prompt(self):
        from backend.prompts.tora import sections_for, slice_prompt

        turn1_sections = sections_for(intent="general_qa")
        turn1 = slice_prompt(sticky=turn1_sections)
        turn2_sections = sections_for(intent="calculation", tools_used=["tax_calc"])
        turn2 = slice_prompt(sticky=sorted(set(turn1_sections) | set(turn2_sections)))

        self.assertGreater(len(turn2), len(turn1))
        for section in turn1_sections:
            self.assertIn(f"## {section}", turn2)      # nothing is dropped between turns

    def test_repeating_a_turn_gives_a_byte_identical_prompt(self):
        from backend.prompts.tora import sections_for, slice_prompt

        sections = sections_for(intent="planning", tools_used=["finance_calc"], operations=["debt_rescue_plan"])
        self.assertEqual(slice_prompt(sticky=sections), slice_prompt(sticky=sections))


class TestPerTurnNotesGoLast(unittest.TestCase):
    """Per-turn notes must not sit in the stable part of the prompt."""

    def test_notes_are_appended_after_the_profile(self):
        from backend.context.builder import ContextBuilder
        from backend.context.financial import FinancialProfile

        profile = FinancialProfile()
        profile.set_fact(name="income", value=88000, category="income", period="monthly", turn=1)
        messages = ContextBuilder(default_system_prompt="STABLE PROMPT").build(
            current_message="hi", financial_context=profile,
                                          system_prompt="STABLE PROMPT", turn_notes="## Note\n- volatile")
        system = messages[0]["content"]
        self.assertTrue(system.startswith("STABLE PROMPT"))
        self.assertLess(system.index("88,000"), system.index("volatile"))
