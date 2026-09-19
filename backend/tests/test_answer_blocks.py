"""The engine prints its own figures. Measured: 24% of every word the model typed was a table or a
figure list the engines had already computed, and retyping is where ₹3,70,000 became "₹37 Lakh"."""
import pytest

from backend.answer.blocks import block_note, render_block
from backend.answer.slots import build_slots
from backend.finance import debt, engine


class Result:
    is_error = False

    def __init__(self, name, output):
        self.tool_name, self.output = name, output


class Context:
    def __init__(self, results):
        self.results = results

    def is_empty(self):
        return not self.results


def block_for(output, tool="finance_calc"):
    ctx = Context([Result(tool, output)])
    return render_block(ctx, build_slots(ctx))


DEBTS = [{"name": "Credit card", "balance": 120000, "annual_rate": 40, "minimum_due": 6000},
         {"name": "Personal loan", "balance": 250000, "annual_rate": 15, "emi": 9000}]


class TestWhatGetsRendered:
    def test_a_single_calculation_becomes_a_figure_list(self):
        block = block_for(engine.emi(principal=2000000, annual_rate=8.5, tenure_months=240))
        assert "**EMI**" in block
        assert "- **EMI:** ₹17,356" in block
        assert "- **Total interest:**" in block

    def test_a_comparison_becomes_a_table(self):
        block = block_for(debt.debt_rescue_plan(debts=DEBTS, monthly_income=88000,
                                                essential_expenses=40000))
        assert "| **Avalanche** |" in block and "| **Snowball** |" in block
        assert len([l for l in block.splitlines() if l.startswith("|---")]) == 1

    def test_nothing_to_show_renders_nothing(self):
        assert render_block(Context([]), {}) == ""
        assert block_for({"operation": "noop", "notes": "nothing numeric"}) == ""

    def test_the_note_is_only_sent_when_there_is_a_block(self):
        assert block_note("") == ""
        assert "already displayed" in block_note("**EMI**\n- **EMI:** ₹1")


class TestWhatIsKeptOut:
    """Everything here was a real defect in the first version of this renderer."""

    def test_untrusted_tools_never_reach_the_block(self):
        # a researched rate must not be shown as one of TORA's own figures
        assert block_for({"operation": "search", "rate": 7.25}, tool="web_search") == ""

    def test_the_users_own_inputs_are_not_handed_back(self):
        block = block_for(debt.debt_rescue_plan(debts=DEBTS, monthly_income=88000,
                                                essential_expenses=40000))
        assert "**Income:** ₹88,000" not in block
        assert "**Essentials:** ₹40,000" not in block

    def test_the_same_figure_under_two_paths_appears_once(self):
        block = block_for(debt.debt_rescue_plan(debts=DEBTS, monthly_income=88000,
                                                essential_expenses=40000))
        assert block.count("**Total debt:**") == 1

    def test_a_summary_sentence_is_not_a_figure(self):
        block = block_for(engine.savings_rate(monthly_income=88000, monthly_expenses=68000))
        assert "Summary" not in block
        assert "surplus ₹20,000 —" not in block

    def test_raw_unformatted_numbers_never_appear(self):
        block = block_for(engine.emi(principal=2000000, annual_rate=8.5, tenure_months=240))
        assert "17356.46" not in block and "2165551" not in block


class TestStrippingWhatTheModelRetyped:
    """Live: told the figures were already on screen, gemma4:e4b restated all eight and the
    answer grew 368 -> 443 words. The repetition is removed, not requested."""

    BLOCK = ("**Debt rescue plan**\n| | Months |\n|---|---|\n| **Avalanche** | 9 months |\n\n"
             "- **Total debt:** ₹3,70,000\n- **Monthly budget for debt:** ₹48,000\n")

    def test_a_retyped_label_and_figure_is_dropped(self):
        from backend.answer.blocks import strip_repeats
        out = strip_repeats("*   **Total Debt:** ₹3,70,000\n*   **Monthly Budget for Debt:** ₹48,000",
                            self.BLOCK)
        assert out == ""

    def test_prose_that_uses_a_figure_is_kept(self):
        from backend.answer.blocks import strip_repeats
        text = "By sending ₹48,000 a month you are debt-free in 9 months, which is the whole point."
        assert strip_repeats(text, self.BLOCK) == text

    def test_a_list_item_carrying_advice_is_kept(self):
        from backend.answer.blocks import strip_repeats
        text = "1.  **Attack the card first:** put every extra rupee there because it costs 40% a year."
        assert strip_repeats(text, self.BLOCK) == text

    def test_a_figure_the_block_does_not_have_is_kept(self):
        from backend.answer.blocks import strip_repeats
        text = "- **Processing fee:** ₹7,400"
        assert strip_repeats(text, self.BLOCK) == text

    def test_an_orphaned_introduction_goes_with_its_list(self):
        from backend.answer.blocks import strip_repeats
        out = strip_repeats("Here is the plan summary:\n\n- **Total debt:** ₹3,70,000\n", self.BLOCK)
        assert "plan summary" not in out

    def test_nothing_happens_without_a_block(self):
        from backend.answer.blocks import strip_repeats
        text = "- **Total debt:** ₹3,70,000"
        assert strip_repeats(text, "") == text


class TestItFitsAPhone:
    """Measured in a browser at 390px: the message column gives the table 312px. The row label
    plus two columns fits exactly; a third took it to 391px, where it scrolled but nothing said
    so, and the last column just looked cut off."""

    def test_the_table_is_never_wider_than_a_phone(self):
        block = block_for(debt.debt_rescue_plan(debts=DEBTS, monthly_income=88000,
                                                essential_expenses=40000))
        header = next(l for l in block.splitlines() if l.startswith("| |"))
        assert header.count("|") - 1 <= 3, f"row label + 2 columns at most, got {header}"

    def test_a_wide_engine_result_is_narrowed_not_dropped(self):
        block = block_for(debt.debt_rescue_plan(debts=DEBTS, monthly_income=88000,
                                                essential_expenses=40000))
        assert "| **Avalanche** |" in block and "| **Snowball** |" in block
        assert "Total debt" in block          # what the table cannot hold is in the list below


class TestNestedFiguresKeepTheirGroup:
    """From a live budget plan on localhost. The block read:

        - **Target:** ₹47,500      - **Actual:** ₹72,348
        - **Actual:** 76.2%        - **Target:** ₹28,500

    Target twice, Actual four times, nothing saying which bucket — the same relabelling the
    locked slots exist to stop, reintroduced by the block that replaced them.
    """

    @staticmethod
    def budget():
        return block_for(engine.budget_plan(monthly_income=95000, needs={"rent": 24000}, emis=48348))

    def test_every_label_is_unique(self):
        labels = [l.split(":**")[0] for l in self.budget().splitlines() if l.startswith("- **")]
        assert len(labels) == len(set(labels)), f"duplicate labels: {labels}"

    def test_a_figure_names_its_bucket(self):
        block = self.budget()
        assert "**Needs target:** ₹47,500" in block
        assert "**Wants target:** ₹28,500" in block
        assert "**Savings target:** ₹19,000" in block

    def test_a_percentage_is_not_the_same_label_as_an_amount(self):
        block = self.budget()
        assert "**Needs actual:** ₹72,348" in block
        assert "**Needs actual %:** 76.2%" in block

    def test_a_group_is_never_shown_half(self):
        """Listing Needs and Wants but stopping before Savings reads as "no savings bucket"."""
        block = self.budget()
        for bucket in ("Needs", "Wants", "Savings"):
            fields = [l for l in block.splitlines() if l.startswith(f"- **{bucket} ")]
            assert len(fields) == 4, f"{bucket} showed {len(fields)} of 4 figures"

    def test_a_flat_result_is_unchanged(self):
        block = block_for(engine.emi(principal=4000000, annual_rate=8.75, tenure_months=240))
        assert "- **EMI:** ₹35,348" in block
        assert "- **Total interest:** ₹44,83,623" in block
