"""
Per-fact confidence.

Not every remembered fact deserves equal weight. A figure read out of a Form 16
is not the same kind of thing as one the model inferred from a sentence, and a
salary confirmed last week is not the same as one mentioned a year ago.

The ordering here is not invented. It follows how each source has actually failed
in this project: model-assisted extraction is the one that stored a home-loan EMI
as a personal loan and an example salary as the user's own, so it is trusted
least. The score exists to carry that history forward after everyone has
forgotten it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.context.financial import (
    FactStatus,
    FinancialFact,
    FinancialProfile,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def fact(source="user", status=FactStatus.CURRENT.value, days_old=1, value=18000):
    return FinancialFact(
        name="rent", value=value, source=source, status=status,
        updated_at=(NOW - timedelta(days=days_old)).isoformat(),
    )


class TestWhereItCameFrom:
    def test_a_document_outranks_a_typed_statement_outranks_the_model(self):
        doc = fact(source="document").confidence(now=NOW).score
        user = fact(source="user").confidence(now=NOW).score
        model = fact(source="model_assisted").confidence(now=NOW).score
        assert doc > user > model

    def test_a_model_inferred_fact_says_so_in_words(self):
        """
        The score is for sorting; the reason is what a person can act on. "I
        inferred this rather than being told it" is the caveat that matters.
        """
        c = fact(source="model_assisted").confidence(now=NOW)
        assert c.band == "medium"
        assert "inferred" in " ".join(c.reasons)

    def test_a_clean_typed_fact_needs_no_caveat(self):
        c = fact(source="user").confidence(now=NOW)
        assert c.band == "high"
        assert c.reasons == []

    def test_an_unrecognised_source_is_neither_trusted_nor_dismissed(self):
        c = fact(source="imported_from_somewhere").confidence(now=NOW)
        assert 0.5 < c.score < 0.8
        assert "not one TORA rates" in " ".join(c.reasons)


class TestHowItWasSaid:
    @pytest.mark.parametrize(
        "status", [FactStatus.ESTIMATE.value, FactStatus.AMBIGUOUS.value, FactStatus.UNKNOWN.value]
    )
    def test_a_hedged_fact_scores_below_a_stated_one(self, status):
        assert fact(status=status).confidence(now=NOW).score < fact().confidence(now=NOW).score

    def test_the_status_is_named_in_the_reasons(self):
        c = fact(status=FactStatus.ESTIMATE.value).confidence(now=NOW)
        assert "recorded as estimate" in c.reasons

    def test_a_retracted_value_carries_no_confidence_at_all(self):
        """The user said it was never true. There is no weight left to give it."""
        assert fact(status=FactStatus.RETRACTED.value).confidence(now=NOW).score == 0.0


class TestAge:
    def test_something_said_this_week_is_not_penalised(self):
        assert fact(days_old=3).confidence(now=NOW).score == fact(days_old=1).confidence(now=NOW).score

    def test_a_quarter_old_fact_is_still_unpenalised(self):
        assert fact(days_old=89).confidence(now=NOW).reasons == []

    def test_confidence_decays_between_a_quarter_and_a_year(self):
        scores = [fact(days_old=d).confidence(now=NOW).score for d in (90, 180, 270, 365)]
        assert scores == sorted(scores, reverse=True)
        assert len(set(scores)) == 4  # actually decaying, not a step

    def test_it_stops_decaying_after_a_year(self):
        """
        A two-year-old salary is not twice as wrong as a one-year-old one. The
        taper has a floor so old facts stay usable rather than decaying to zero
        and silently dropping out of answers.
        """
        assert fact(days_old=400).confidence(now=NOW).score == fact(days_old=2000).confidence(now=NOW).score

    def test_an_old_fact_says_how_old(self):
        assert "13 months ago" in " ".join(fact(days_old=400).confidence(now=NOW).reasons)

    def test_a_missing_timestamp_is_not_treated_as_infinitely_old(self):
        f = FinancialFact(name="rent", value=18000)
        f.updated_at = None
        assert f.confidence(now=NOW).band == "high"

    def test_an_unreadable_timestamp_does_not_raise(self):
        f = fact()
        f.updated_at = "sometime last spring"
        assert f.confidence(now=NOW).score > 0

    def test_a_clock_skewed_future_timestamp_does_not_inflate_confidence(self):
        f = fact(days_old=-30)
        assert f.confidence(now=NOW).score <= fact(days_old=1).confidence(now=NOW).score


class TestTheBandsAreUsable:
    def test_the_worst_case_still_lands_in_low(self):
        c = fact(source="model_assisted", status=FactStatus.AMBIGUOUS.value, days_old=500).confidence(now=NOW)
        assert c.band == "low"
        assert len(c.reasons) == 3  # source, status and age all explained

    def test_bands_follow_the_score(self):
        for f in (fact(source="document"), fact(source="model_assisted"),
                  fact(source="model_assisted", days_old=500)):
            c = f.confidence(now=NOW)
            expected = "high" if c.score >= 0.8 else ("medium" if c.score >= 0.55 else "low")
            assert c.band == expected


class TestItChangesNothingItShouldNot:
    def test_confidence_is_derived_not_stored(self):
        """
        A stored score would go stale the moment the fact aged and would need
        migrating whenever the reasoning changed. Nothing is written, so no
        existing memory can be corrupted by adding this.
        """
        f = fact()
        assert not hasattr(f, "confidence_score")
        assert "confidence" not in {fld for fld in f.__dataclass_fields__}

    def test_it_appears_in_the_serialised_fact(self):
        data = fact(source="document").to_dict()
        assert data["confidence"]["band"] == "high"
        assert "read from a document" in data["confidence"]["reasons"]

    def test_a_fact_round_trips_through_storage_unchanged(self):
        """to_dict grew a key; from_dict must not choke on it."""
        original = fact(source="document", days_old=200)
        restored = FinancialFact.from_dict(original.to_dict())
        assert restored.value == original.value
        assert restored.source == original.source
        assert restored.updated_at == original.updated_at
        assert restored.confidence(now=NOW).score == original.confidence(now=NOW).score

    def test_the_profile_still_serialises(self):
        profile = FinancialProfile(rent=fact(source="model_assisted"))
        data = profile.to_dict()
        assert data["rent"]["confidence"]["band"] == "medium"


# ---------------------------------------------------------------------------
# Confidence reaching the answer — rendered in code, not asked of the model.
# ---------------------------------------------------------------------------

from backend.answer.temporal import temporal_recall  # noqa: E402


def profile_with(source="user", days_old=250, status=FactStatus.CURRENT.value):
    """
    250 days old by default, so March 2026 falls *inside* the window the fact has
    been on file. A fact recorded yesterday genuinely was not on file in March,
    and would correctly take the "I can't evidence that date" branch instead.
    """
    return FinancialProfile(rent=FinancialFact(
        name="rent", value=18000, source=source, status=status,
        updated_at=(NOW - timedelta(days=days_old)).isoformat(),
    ))


class TestTheAnswerSaysHowSureItIs:
    def test_a_model_inferred_figure_is_flagged_in_the_sentence(self):
        said = temporal_recall("what was my rent in March", profile_with(source="model_assisted"), now=NOW)
        assert "₹18,000" in said
        assert "Inferred from what you wrote" in said

    def test_a_clean_fact_carries_no_caveat(self):
        """
        A caveat on every answer is a caveat on none. Only say it when it is
        actually true of this figure.
        """
        said = temporal_recall("what was my rent in March", profile_with(source="user"), now=NOW)
        assert said == "In March 2026 your rent was ₹18,000."

    def test_a_hedged_fact_is_flagged(self):
        said = temporal_recall("what was my rent in March",
                               profile_with(status=FactStatus.ESTIMATE.value), now=NOW)
        assert "estimate" in said.lower()

    def test_age_is_never_the_caveat_on_a_question_about_the_past(self):
        """
        Confidence counts a year-old fact as weaker because it may have changed.
        But this question *is* about the past, so "you told me this a year ago"
        is not a caveat — it is the point. Saying it here would train the reader
        to skip the caveats that do matter.
        """
        said = temporal_recall("what was my rent in March",
                               profile_with(source="user", days_old=400), now=NOW)
        assert "month" not in said.lower().replace("march", "")
        assert said == "In March 2026 your rent was ₹18,000."

    def test_the_caveat_survives_the_could_not_evidence_that_date_answer(self):
        said = temporal_recall("what was my rent in January",
                               profile_with(source="model_assisted", days_old=30), now=NOW)
        assert "don't have anything recorded" in said
        assert "Inferred" in said

    def test_a_broken_fact_object_does_not_take_the_answer_down(self):
        """The caveat is a nicety; the figure is the answer. Never lose the figure."""
        class Odd(FinancialFact):
            def confidence(self, now=None):
                raise RuntimeError("boom")

        profile = FinancialProfile(rent=Odd(name="rent", value=18000,
                                            updated_at=(NOW - timedelta(days=250)).isoformat()))
        said = temporal_recall("what was my rent in March", profile, now=NOW)
        assert "₹18,000" in said
