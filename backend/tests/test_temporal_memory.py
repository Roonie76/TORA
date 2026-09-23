"""
Temporal memory: "what was my rent in March?"

The revision chains needed to answer this already existed; what was missing was a
way to ask them a question about a point in time, and an honest account of what
they can and cannot support.

The distinction the tests keep returning to: a timestamp records when TORA
*learned* a value, not when it became true. So the question that can be answered
is "what did you have on file in March", and anything stronger than that would be
a figure presented with more confidence than it has -- the failure this project
keeps working against.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.context.financial import (
    FactRevision,
    FactStatus,
    FinancialFact,
    FinancialProfile,
)


def at(day: int, month: int = 1, year: int = 2026) -> str:
    return datetime(year, month, day, 12, 0, tzinfo=timezone.utc).isoformat()


def rent_with_history() -> FinancialFact:
    """₹12,000 until 10 Mar, ₹15,000 until 10 Jun, ₹18,000 since."""
    fact = FinancialFact(name="rent", value=18000, period="monthly", updated_at=at(10, 6))
    fact.revisions = [
        FactRevision(value=12000, timestamp=at(10, 3)),
        FactRevision(value=15000, timestamp=at(10, 6)),
    ]
    return fact


class TestTheTimeline:
    def test_every_true_value_gets_a_window(self):
        entries = rent_with_history().timeline()
        assert [e["value"] for e in entries] == [12000, 15000, 18000]
        # the oldest has no start: the fact existed before we first recorded it
        assert entries[0]["from"] is None
        assert entries[0]["to"] == at(10, 3)
        assert entries[1]["from"] == at(10, 3)
        assert entries[-1]["to"] is None  # still current

    def test_a_fact_with_no_history_is_one_open_window(self):
        fact = FinancialFact(name="rent", value=18000, updated_at=at(1, 1))
        entries = fact.timeline()
        assert len(entries) == 1
        assert entries[0]["from"] == at(1, 1) and entries[0]["to"] is None


class TestAskingAboutAPointInTime:
    @pytest.mark.parametrize(
        "when,expected",
        [
            (at(15, 4), 15000),   # between the two changes
            (at(1, 7), 18000),    # after the last change
            (at(10, 6), 18000),   # exactly on a boundary: the new value has started
        ],
    )
    def test_it_returns_the_value_on_file_then(self, when, expected):
        answer = rent_with_history().value_as_of(when)
        assert answer.known is True
        assert answer.value == expected
        assert answer.exact is True

    def test_asking_before_anything_was_recorded_is_flagged_not_guessed(self):
        """
        January predates the first thing we know. ₹12,000 is the oldest value we
        hold, but we never watched it be true in January -- so it is returned
        with exact=False rather than stated as fact.
        """
        answer = rent_with_history().value_as_of(at(5, 1))
        assert answer.known is True
        assert answer.value == 12000
        assert answer.exact is False
        assert answer.reason == "before_first_record"

    def test_an_unreadable_time_is_not_silently_treated_as_now(self):
        answer = rent_with_history().value_as_of("last Tuesday-ish")
        assert answer.known is False
        assert answer.reason == "unreadable_time"

    def test_a_naive_timestamp_still_compares(self):
        """Older stored timestamps have no timezone; they must not raise mid-walk."""
        fact = FinancialFact(name="rent", value=18000, updated_at="2026-01-01T12:00:00")
        fact.revisions = [FactRevision(value=12000, timestamp="2026-03-10T12:00:00")]
        assert fact.value_as_of(at(1, 2)).value == 12000
        assert fact.value_as_of(at(1, 4)).value == 18000


class TestRetractedValuesNeverComeBack:
    def test_a_corrected_value_never_answers_a_question_about_the_past(self):
        """
        The user said ₹20,000 was wrong. Answering "what was my rent in March?"
        with ₹20,000 would hand back a figure they have already corrected --
        worse than saying nothing, because it looks like memory working.
        """
        fact = FinancialFact(name="rent", value=25000, updated_at=at(1, 6))
        fact.revisions = [
            FactRevision(value=20000, status=FactStatus.RETRACTED.value, timestamp=at(10, 3)),
        ]
        answer = fact.value_as_of(at(1, 3))
        assert answer.value != 20000
        assert answer.value == 25000
        assert [e["value"] for e in fact.timeline()] == [25000]

    def test_the_value_a_retraction_replaced_covers_that_period(self):
        fact = FinancialFact(name="rent", value=25000, updated_at=at(1, 6))
        fact.revisions = [
            FactRevision(value=12000, timestamp=at(10, 3)),
            FactRevision(value=20000, status=FactStatus.RETRACTED.value, timestamp=at(1, 6)),
        ]
        # April sits in what would have been the retracted value's window.
        assert fact.value_as_of(at(15, 4)).value == 25000
        assert fact.value_as_of(at(1, 2)).value == 12000


class TestThroughTheProfile:
    def test_it_answers_for_a_named_fact(self):
        profile = FinancialProfile(rent=rent_with_history())
        assert profile.fact_as_of("rent", at(15, 4)).value == 15000

    def test_a_fact_it_has_never_held_says_so_distinctly(self):
        """
        "I don't know your rent" and "I didn't know it in March, but it's
        ₹18,000 now" are different answers. Only one of them is useful, so the
        caller has to be able to tell them apart.
        """
        profile = FinancialProfile()
        answer = profile.fact_as_of("rent", at(15, 4))
        assert answer.known is False
        assert answer.reason == "no_such_fact"

        held = FinancialProfile(rent=FinancialFact(name="rent", value=18000, updated_at=at(1, 9)))
        later = held.fact_as_of("rent", at(15, 4))
        assert later.known is True and later.exact is False

    def test_income_answers_under_either_name(self):
        profile = FinancialProfile(income=FinancialFact(name="income", value=50000, updated_at=at(1, 1)))
        assert profile.fact_as_of("salary", at(1, 6)).value == 50000

    def test_the_answer_serialises_for_an_api_response(self):
        answer = rent_with_history().value_as_of(at(15, 4))
        data = answer.to_dict()
        assert data["value"] == 15000 and data["known"] is True and data["exact"] is True


class TestItDoesNotClaimToKnowWhenThingsChanged:
    def test_learning_late_is_reported_as_learning_late(self):
        """
        A rent that changed in March but was only mentioned in June is recorded
        in June. Asked about April, TORA must not claim the new figure was true
        then -- it reports what it had on file, which was the old one.
        """
        fact = FinancialFact(name="rent", value=18000, updated_at=at(20, 6))
        fact.revisions = [FactRevision(value=12000, timestamp=at(20, 6))]
        april = fact.value_as_of(at(15, 4))
        assert april.value == 12000
        assert april.effective_to == at(20, 6)


# ---------------------------------------------------------------------------
# The question, end to end: "what was my rent in March?"
# ---------------------------------------------------------------------------

from backend.answer.temporal import parse_when, temporal_recall  # noqa: E402

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


class TestReadingTheDate:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("what was my rent in March", (2026, 3)),
            ("what was my rent in march 2025", (2025, 3)),
            ("how much was my salary back in Jan", (2026, 1)),
            ("what did I pay in rent during Feb 2024", (2024, 2)),
            ("what was my rent last month", (2026, 8)),
        ],
    )
    def test_it_reads_the_month(self, text, expected):
        moment, _ = parse_when(text, now=NOW)
        assert (moment.year, moment.month) == expected

    def test_a_month_still_to_come_this_year_means_last_year(self):
        """
        Asked in September, "in December" cannot mean this December -- that is
        three months away. Reading it forwards would answer a question about the
        future with a figure from the past.
        """
        moment, label = parse_when("what was my rent in December", now=NOW)
        assert (moment.year, moment.month) == (2025, 12)
        assert label == "December 2025"

    def test_no_date_is_not_a_temporal_question(self):
        assert parse_when("what is my rent", now=NOW) is None


class TestTheAnswer:
    def test_it_answers_from_the_revision_chain(self):
        profile = FinancialProfile(rent=rent_with_history())
        said = temporal_recall("what was my rent in April", profile, now=NOW)
        assert "April 2026" in said and "15,000" in said

    def test_it_says_when_it_cannot_evidence_that_far_back(self):
        """
        The value exists but nothing records it for January. Returning ₹12,000
        as though it were remembered would read like memory working.
        """
        profile = FinancialProfile(rent=rent_with_history())
        said = temporal_recall("what was my rent in January", profile, now=NOW)
        assert "don't have anything recorded" in said
        assert "January 2026" in said

    def test_a_fact_it_has_never_held(self):
        said = temporal_recall("what was my rent in April", FinancialProfile(), now=NOW)
        assert "don't have your rent on file" in said

    def test_a_corrected_figure_is_never_read_back_as_history(self):
        fact = FinancialFact(name="rent", value=25000, updated_at=at(1, 6))
        fact.revisions = [FactRevision(value=20000, status=FactStatus.RETRACTED.value, timestamp=at(10, 3))]
        said = temporal_recall("what was my rent in March", FinancialProfile(rent=fact), now=NOW)
        assert "20,000" not in said

    @pytest.mark.parametrize(
        "text",
        [
            "what is my rent",              # present tense, not temporal
            "what was my rent",             # no date
            "what was the weather in March",  # no fact of ours named
            "should I move in March",       # not a question about a value
        ],
    )
    def test_it_declines_anything_it_cannot_answer_squarely(self, text):
        """Returning None hands the turn back to the normal path, which is the
        right outcome -- a half-guess here is worse than the ordinary recall."""
        profile = FinancialProfile(rent=rent_with_history())
        assert temporal_recall(text, profile, now=NOW) is None
