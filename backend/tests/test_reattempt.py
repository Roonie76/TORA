"""
Autonomous re-research.

A research turn used to answer from whatever the first search returned. If that
was one undated blog post, the answer rested on one undated blog post.

The risk in fixing that is not missing a retry — it is retrying badly: looping,
burning seconds on a query that cannot improve, or replacing a good answer with a
cleaner-looking but less relevant one. That is what most of these tests are about.
"""

import pytest

from backend.research import reattempt
from backend.research.reattempt import assess, is_better, note, quality


def result(status="CORROBORATED", confidence="HIGH", official=True,
           conclusions=1, conflicts=False, success=True, error=None):
    return {
        "overall_status": status,
        "overall_confidence": confidence,
        "has_conflicts": conflicts,
        "conclusions": [{"claim": "x"}] * conclusions,
        "sources": [{"domain": "rbi.org.in", "is_official": official}],
        "success": success,
        "error": error,
    }


class TestWhenItIsSatisfied:
    def test_good_evidence_is_left_alone(self):
        assert assess(result(), "repo rate").sufficient is True

    def test_a_failed_run_is_never_retried(self):
        """
        A broken provider or blocked network is not thin evidence. A different
        question costs the same seconds and cannot fix it.
        """
        verdict = assess(result(success=False, error="search provider unavailable"), "repo rate")
        assert verdict.sufficient is True
        assert verdict.retry_query is None
        assert "cannot fix" in " ".join(verdict.reasons)


class TestWhenItGoesAgain:
    def test_no_official_source_aims_the_next_query_at_one(self):
        """A summary of an RBI circular is not the circular."""
        verdict = assess(result(official=False), "repo rate")
        assert verdict.sufficient is False
        assert "no official source" in verdict.reasons
        assert "rbi.org.in" in verdict.retry_query

    def test_disagreeing_sources_get_the_year_pinned(self):
        """Two sources disagreeing is often two different years."""
        verdict = assess(result(conflicts=True), "80C limit", year=2026)
        assert "sources disagree" in verdict.reasons
        assert "2026" in verdict.retry_query

    def test_nothing_concluded_broadens_the_question(self):
        verdict = assess(result(conclusions=0), "gold loan rate for salaried borrowers in Chennai")
        assert "nothing was concluded" in verdict.reasons
        assert verdict.retry_query == "gold loan rate"
        assert len(verdict.retry_query) < len("gold loan rate for salaried borrowers in Chennai")

    @pytest.mark.parametrize("status", ["UNVERIFIED", "INSUFFICIENT_EVIDENCE"])
    def test_weak_status_is_a_reason(self, status):
        assert assess(result(status=status), "repo rate").sufficient is False

    @pytest.mark.parametrize("confidence", ["LOW", "VERY_LOW"])
    def test_weak_confidence_is_a_reason(self, confidence):
        assert assess(result(confidence=confidence), "repo rate").sufficient is False

    def test_it_admits_when_there_is_no_better_question(self):
        """
        Thin evidence from an official source with nothing to reformulate: going
        again would be the same search twice. It says so instead.
        """
        verdict = assess(result(status="UNVERIFIED", official=True, conclusions=1), "repo rate")
        assert verdict.sufficient is False
        assert verdict.retry_query is None
        assert "no better question to ask" in verdict.reasons


class TestTheHintDoesNotCompound:
    def test_a_second_pass_does_not_stack_hints_on_top_of_each_other(self):
        """
        Without stripping, a query would grow the site: hint once per attempt
        and eventually exceed the 300-character query limit.
        """
        first = assess(result(official=False), "repo rate").retry_query
        second = assess(result(official=False), first).retry_query
        assert second.count("rbi.org.in") == 1
        assert len(second) == len(first)


class TestABetterResultWins:
    def test_a_primary_source_beats_an_unverified_one(self):
        assert is_better(result(status="VERIFIED_PRIMARY"), result(status="UNVERIFIED"))

    def test_higher_confidence_breaks_a_status_tie(self):
        assert is_better(result(confidence="VERY_HIGH"), result(confidence="MEDIUM"))

    def test_an_official_source_breaks_a_confidence_tie(self):
        assert is_better(result(official=True), result(official=False))

    def test_a_tie_keeps_the_original(self):
        """Equal is not better. The first answer stands."""
        assert is_better(result(), result()) is False

    def test_an_unknown_status_sorts_last_rather_than_crashing(self):
        assert quality(result(status="SOMETHING_NEW"))[0] >= len(reattempt.STATUS_RANK) - 1
        assert is_better(result(status="CORROBORATED"), result(status="SOMETHING_NEW"))


class TestTheNote:
    def test_a_single_attempt_says_nothing(self):
        assert note(["no official source"], attempts=1, improved=False) == ""

    def test_it_says_when_going_again_helped(self):
        assert "found better sources" in note(["no official source"], 2, True)

    def test_it_says_when_going_again_did_not_help(self):
        """
        The honest outcome. Having tried and failed is not the same as having
        found something, and the answer should not read as though it were.
        """
        said = note(["no official source"], 2, False)
        assert "limited evidence" in said


# ---------------------------------------------------------------------------
# Through the tool: the budget, and never looping.
# ---------------------------------------------------------------------------


class FakeSynthesis:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return dict(self._data)


class FakeProvider:
    """Returns a scripted result per call and records the queries it was asked."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.queries = []

    async def multi_source_research(self, query, max_sources, max_chars_per_source):
        self.queries.append(query)
        data = self._scripted[min(len(self.queries) - 1, len(self._scripted) - 1)]
        return FakeSynthesis({**data, "query": query, "sources_evaluated": data.get("sources", [])})


def tool(scripted):
    from backend.tools.research_tool import ResearchTool

    provider = FakeProvider(scripted)
    return ResearchTool(provider=provider), provider


@pytest.mark.asyncio
class TestThroughTheTool:
    async def test_good_evidence_costs_exactly_one_search(self):
        t, provider = tool([result()])
        out = await t.execute(query="repo rate")
        assert len(provider.queries) == 1
        assert out["attempts"] == 1
        assert "reattempt_note" not in out

    async def test_thin_evidence_triggers_one_different_search(self):
        weak = result(official=False, status="UNVERIFIED", confidence="LOW")
        strong = result(status="VERIFIED_PRIMARY", confidence="HIGH", official=True)
        t, provider = tool([weak, strong])
        out = await t.execute(query="repo rate")
        assert len(provider.queries) == 2
        assert provider.queries[0] != provider.queries[1]  # a reformulation, not a repeat
        assert out["attempts"] == 2
        assert out["overall_status"] == "VERIFIED_PRIMARY"
        assert "found better sources" in out["reattempt_note"]

    async def test_a_worse_second_result_is_discarded(self):
        """
        A reformulated query can drift and come back cleaner but less relevant.
        Preferring it would be a regression that reads as an improvement.
        """
        first = result(official=False, status="VERIFIED_SECONDARY", confidence="MEDIUM")
        worse = result(official=False, status="INSUFFICIENT_EVIDENCE", confidence="VERY_LOW", conclusions=0)
        t, provider = tool([first, worse])
        out = await t.execute(query="repo rate")
        assert out["overall_status"] == "VERIFIED_SECONDARY"
        assert out["attempts"] == 2
        assert "nothing better" in out["reattempt_note"]

    async def test_it_never_loops_however_bad_the_evidence(self):
        """The hard budget. Every attempt is seconds of real network."""
        hopeless = result(official=False, status="UNVERIFIED", confidence="VERY_LOW", conclusions=0)
        t, provider = tool([hopeless])
        out = await t.execute(query="repo rate")
        assert len(provider.queries) <= 1 + reattempt.MAX_REATTEMPTS
        assert out["attempts"] <= 1 + reattempt.MAX_REATTEMPTS

    async def test_a_failed_search_is_not_retried(self):
        t, provider = tool([result(success=False, error="provider down")])
        out = await t.execute(query="repo rate")
        assert len(provider.queries) == 1
        assert out["attempts"] == 1

    async def test_the_budget_can_be_turned_off(self, monkeypatch):
        monkeypatch.setattr(reattempt, "MAX_REATTEMPTS", 0)
        t, provider = tool([result(official=False, status="UNVERIFIED")])
        out = await t.execute(query="repo rate")
        assert len(provider.queries) == 1
        assert out["attempts"] == 1
