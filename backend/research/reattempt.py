"""
Autonomous re-research: notice the evidence is thin and go again.

Until now a research turn answered from whatever the first search returned. If
that was one blog post with no date, the answer was built on one blog post with
no date — and the only signal was a confidence field nobody downstream acted on.

Three rules shape this, and they are the whole design:

**A second attempt must ask a different question.** Re-running the same query
returns the same pages; the retry is not a retry, it is a *reformulation* aimed
at whatever was missing — an official source, a date, a narrower claim. If there
is no sensible reformulation, there is no retry.

**The budget is one extra attempt by default.** Each research run is several
seconds of real network and parsing on a machine with two cores. Re-researching
until satisfied would turn a slow turn into an unbounded one, so the cap is hard
and counted, never "until good enough".

**A retry may never make the answer worse.** The second result is kept only if it
is actually better by status and confidence; otherwise the first stands. A
reformulated query can easily drift off-topic and come back cleaner but less
relevant, and quietly preferring it would be a regression that reads as an
improvement.

What this does not do is decide the evidence is good when it is not. If both
attempts come back thin, the result still says so — the point is to have tried,
not to manufacture confidence.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# How many extra attempts are allowed. One, because each costs seconds.
MAX_REATTEMPTS = int(os.getenv("TORA_RESEARCH_REATTEMPTS", "1"))

# Ordered best to worst. Used only for "is this better than that", never shown.
STATUS_RANK = [
    "VERIFIED_PRIMARY",
    "CORROBORATED",
    "VERIFIED_SECONDARY",
    "STALE",
    "CONFLICTING",
    "UNVERIFIED",
    "INSUFFICIENT_EVIDENCE",
]
CONFIDENCE_RANK = ["VERY_HIGH", "HIGH", "MEDIUM", "LOW", "VERY_LOW"]

WEAK_STATUSES = {"UNVERIFIED", "INSUFFICIENT_EVIDENCE"}
WEAK_CONFIDENCE = {"LOW", "VERY_LOW"}

# Words that make a query aim at a primary source rather than a summary of one.
_OFFICIAL_HINT = "official circular site:rbi.org.in OR site:incometax.gov.in OR site:sebi.gov.in"


def _rank(value: Optional[str], order: List[str]) -> int:
    """Position in the ordering; unknown values sort last."""
    try:
        return order.index(str(value))
    except ValueError:
        return len(order)


def quality(result: Dict[str, Any]) -> tuple:
    """A sortable quality key. Lower is better, matching the rank lists."""
    return (
        _rank(result.get("overall_status"), STATUS_RANK),
        _rank(result.get("overall_confidence"), CONFIDENCE_RANK),
        -len([s for s in (result.get("sources") or []) if s.get("is_official")]),
        -len(result.get("conclusions") or []),
    )


def is_better(candidate: Dict[str, Any], incumbent: Dict[str, Any]) -> bool:
    """Strictly better, so a tie keeps the original and the first answer wins."""
    return quality(candidate) < quality(incumbent)


@dataclass
class Assessment:
    """Whether a research result is good enough, and what to ask instead."""
    sufficient: bool
    reasons: List[str] = field(default_factory=list)
    retry_query: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"sufficient": self.sufficient, "reasons": list(self.reasons),
                "retry_query": self.retry_query}


def _has_official_source(result: Dict[str, Any]) -> bool:
    return any(s.get("is_official") for s in (result.get("sources") or []))


def _strip_hint(query: str) -> str:
    """Remove a hint this module added, so attempts do not compound."""
    return re.sub(re.escape(_OFFICIAL_HINT), "", query).strip()


def assess(result: Dict[str, Any], query: str, year: Optional[int] = None) -> Assessment:
    """
    Decide whether to go again, and with what.

    Takes the tool's own result dict rather than provider internals, so a rule
    can be tested by handing it a shape instead of by running a real search.
    """
    # A failed run is not thin evidence, it is a broken provider or a blocked
    # network. Asking it a different question will not help and costs the same
    # seconds, so this is the one case that never retries.
    if result.get("success") is False or result.get("error"):
        return Assessment(sufficient=True, reasons=["research failed; a different query cannot fix that"])

    reasons: List[str] = []
    status = str(result.get("overall_status") or "")
    confidence = str(result.get("overall_confidence") or "")
    conclusions = result.get("conclusions") or []

    if not conclusions:
        reasons.append("nothing was concluded")
    if status in WEAK_STATUSES:
        reasons.append(f"evidence is {status.replace('_', ' ').lower()}")
    if confidence in WEAK_CONFIDENCE:
        reasons.append(f"confidence is {confidence.replace('_', ' ').lower()}")
    if result.get("has_conflicts"):
        reasons.append("sources disagree")
    if not _has_official_source(result):
        reasons.append("no official source")

    if not reasons:
        return Assessment(sufficient=True)

    base = _strip_hint(query)

    # One reformulation, chosen by what is actually missing. Ordered by which
    # weakness a different search can most plausibly fix.
    retry: Optional[str] = None
    if not _has_official_source(result):
        # A summary of a rule is not the rule. Aim at the body that issued it.
        retry = f"{base} {_OFFICIAL_HINT}".strip()
    elif result.get("has_conflicts") or status == "STALE":
        # Disagreement is often two sources from different years. Pin the year.
        retry = f"{base} latest {year}".strip() if year else f"{base} latest".strip()
    elif not conclusions:
        # Nothing came back at all: the query may be too specific to match.
        broadened = re.sub(r"\b(?:for|in|with|under)\b.*$", "", base).strip()
        retry = broadened if broadened and broadened != base else None

    if retry is None:
        reasons.append("no better question to ask")

    return Assessment(sufficient=False, reasons=reasons, retry_query=retry)


def note(reasons: List[str], attempts: int, improved: bool) -> str:
    """One line for the answer, saying what happened and what it means."""
    if attempts <= 1:
        return ""
    why = "; ".join(reasons[:2])
    if improved:
        return f"Searched again ({why}) and found better sources."
    return f"Searched again ({why}) but found nothing better, so this rests on limited evidence."
