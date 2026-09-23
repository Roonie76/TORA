"""
Tier 0 for questions about the past: "what was my rent in March?"

The revision chains hold the answer, so no model is needed and none is called --
at 4.1 tokens/sec of decode the cheapest model call is the one that does not
happen. What this module mostly does is refuse to overclaim.

TORA's timestamps say when it *learned* a value, not when that value became true
in the world. A rent that changed in March and was mentioned in June is recorded
in June. So the honest answer to "what was my rent in March" is what TORA had on
file then, and where it cannot evidence that date it says so in the sentence
rather than quietly returning its newest figure -- which would read exactly like
memory working, and be wrong.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# "what was my rent", "how much was my salary", "what did I pay in rent"
_PAST_QUESTION = re.compile(
    r"\b(?:what|how\s+much)\s+(?:was|were|did)\b",
    re.IGNORECASE,
)

# Only a question about a *point in time* belongs here. "What was my rent?"
# without a date is ordinary recall and the existing path answers it.
_IN_MONTH = re.compile(
    r"\b(?:in|during|back\s+in|as\s+of|for)\s+"
    r"(?P<month>" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")"
    r"(?:\s+(?P<year>(?:19|20)\d{2}))?\b",
    re.IGNORECASE,
)

_LAST_MONTH = re.compile(r"\blast\s+month\b", re.IGNORECASE)

# The facts worth asking about in the past tense, and how to say them.
FACT_WORDS = (
    ("rent", ("rent",)),
    ("income", ("salary", "income", "pay", "ctc")),
    ("savings", ("savings", "saved", "emergency fund")),
)

_LABELS = {"rent": "rent", "income": "salary", "savings": "savings"}


def _resolve_month(month: int, year: Optional[int], now: datetime) -> datetime:
    """
    A bare month name means the most recent one that has already happened.

    "In March" asked in September 2026 means March 2026. Asked in February 2026
    it means March 2025 -- reading it as a month seven months away would answer a
    question about the future with a figure from the past.
    """
    if year is not None:
        return datetime(year, month, 15, 12, 0, tzinfo=timezone.utc)
    candidate = datetime(now.year, month, 15, 12, 0, tzinfo=timezone.utc)
    if candidate > now:
        candidate = datetime(now.year - 1, month, 15, 12, 0, tzinfo=timezone.utc)
    return candidate


def parse_when(message: str, now: Optional[datetime] = None) -> Optional[Tuple[datetime, str]]:
    """The point in time a message asks about, and how to name it back."""
    now = now or datetime.now(timezone.utc)
    text = message or ""

    match = _IN_MONTH.search(text)
    if match:
        month = MONTHS[match.group("month").lower()]
        year = int(match.group("year")) if match.group("year") else None
        moment = _resolve_month(month, year, now)
        return moment, moment.strftime("%B %Y")

    if _LAST_MONTH.search(text):
        year, month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        moment = datetime(year, month, 15, 12, 0, tzinfo=timezone.utc)
        return moment, moment.strftime("%B %Y")

    return None


def _named_fact(message: str) -> Optional[str]:
    lower = (message or "").lower()
    for name, words in FACT_WORDS:
        if any(re.search(rf"\b{re.escape(w)}\b", lower) for w in words):
            return name
    return None


def _format(value: Any, fact: Any) -> str:
    try:
        return fact.format_value(value)
    except Exception:
        return str(value)


def _how_sure(fact: Any) -> str:
    """
    A caveat on where a remembered figure came from, or "" when there is none.

    Rendered here in code rather than asked of the model, for the reason the
    whole answer path exists: a prompt rule to "mention when you are unsure" is
    advisory, and the turns where it would matter most are exactly the turns a
    small model drops it.

    Age is deliberately excluded. Confidence counts a year-old fact as weaker
    because it may have changed since — but the question here is *about the
    past*, so "you told me this a year ago" is not a caveat, it is the point.
    Repeating it would train the reader to ignore the caveats that do matter.
    """
    if fact is None:
        return ""
    try:
        confidence = fact.confidence()
    except Exception:
        return ""
    reasons = [r for r in confidence.reasons if "confirmed" not in r]
    if not reasons:
        return ""
    return f" ({reasons[0].capitalize()}.)"


def temporal_recall(message: str, profile: Any, now: Optional[datetime] = None) -> Optional[str]:
    """
    Answer a point-in-time question from memory, or return None to let the
    normal path handle it.

    None is returned for anything this cannot answer squarely: no date, no fact
    named, no such fact. Guessing here would be worse than the ordinary recall
    path, which at least knows what it does not know.
    """
    if profile is None or not message:
        return None
    if not _PAST_QUESTION.search(message):
        return None

    when = parse_when(message, now=now)
    if when is None:
        return None
    moment, label = when

    name = _named_fact(message)
    if name is None:
        return None

    try:
        answer = profile.fact_as_of(name, moment)
    except Exception:
        return None

    noun = _LABELS.get(name, name)

    if not answer.known:
        if answer.reason == "no_such_fact":
            return f"I don't have your {noun} on file, so I can't say what it was in {label}."
        return None

    fact = profile.get_fact(name)
    shown = _format(answer.value, fact)
    caveat = _how_sure(fact)

    if answer.exact:
        return f"In {label} your {noun} was {shown}.{caveat}"

    # We hold the value but cannot evidence it for that date. Say which is which,
    # rather than presenting the newest figure as a memory of that month.
    return (
        f"I don't have anything recorded for your {noun} as far back as {label}. "
        f"The earliest I have is {shown}.{caveat}"
    )
