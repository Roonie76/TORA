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
