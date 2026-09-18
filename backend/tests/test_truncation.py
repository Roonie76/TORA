"""A length-capped answer should end at a sentence, not mid-word.

Live, on a debt plan with the block cap at 450 tokens, the answer ended
"...would get you debt-free one month sooner and save you ₹ …", which reads like a fault.
"""
from backend.llm.ollama import MAX_FRAGMENT_CHARS, end_cleanly

NOTE = "(Answer shortened — ask me to continue for more detail.)"


class TestEndingCleanly:
    def test_a_dangling_fragment_is_dropped(self):
        out = end_cleanly("One thing. Another thing. finding an extra ₹2,000 would save you ₹")
        assert out == "One thing. Another thing.\n\n" + NOTE

    def test_a_complete_answer_only_gains_the_note(self):
        assert end_cleanly("All done here.") == "All done here.\n\n" + NOTE

    def test_a_bullet_list_ends_on_its_last_complete_item(self):
        out = end_cleanly("- First point.\n- Second point.\n- Third one is cut off halfway thro")
        assert out.endswith(NOTE)
        assert "- Second point." in out and "halfway thro" not in out

    def test_a_long_unfinished_paragraph_is_kept_rather_than_gutted(self):
        # Cutting back to the last full stop would throw away more than it tidies.
        text = "Done. " + ("and it keeps going on and on without any punctuation at all " * 12)
        out = end_cleanly(text)
        assert "keeps going on and on" in out
        assert out.endswith("…\n\n" + NOTE)
        assert len(text) - len("Done.") > MAX_FRAGMENT_CHARS

    def test_empty_stays_empty(self):
        assert end_cleanly("") == ""
        assert end_cleanly("   ") == ""

    def test_a_closing_bracket_counts_as_an_ending(self):
        out = end_cleanly("The plan works (see the table above) but the next part is cut he")
        assert out == "The plan works (see the table above)\n\n" + NOTE
