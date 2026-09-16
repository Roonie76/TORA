import re
import logging
from typing import List, Any, Optional
from .financial import FinancialProfile, FactStatus

logger = logging.getLogger("tora.context.summarizer")

MAX_TOPIC_LINES: int = 6
MAX_UPDATE_LINES: int = 4
MAX_HYPOTHETICAL_LINES: int = 4
MAX_ASSISTANT_POINTS: int = 6
USER_EXCERPT_CHARS: int = 110
ASSISTANT_EXCERPT_CHARS: int = 170

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_HAS_FIGURE = re.compile(r"(?:₹|rs\.?\s?|inr\s?)\s*\d|\d+(?:\.\d+)?\s*%|\b\d[\d,]*(?:\.\d+)?\s*(?:k|l|lakh|lakhs|cr|crore)\b", re.IGNORECASE)
_UPDATE_CUES = re.compile(r"\b(?:actually|correction|now|increased|decreased|raise|hike|updated?|changed?)\b", re.IGNORECASE)


def _excerpt(text: str, limit: int) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


class ConversationSummarizer:
    """
    Deterministic structured conversation summarizer.

    Compresses older turns when the context budget is constrained. It only
    re-states text that actually appeared in the conversation (short excerpts),
    labelled by semantic status, so it can never introduce figures or advice that
    were not discussed. Verified figures live in the financial profile, which is
    always rendered separately.

    (The previous implementation emitted canned lines such as "Calculated
    comparative interest (10% Gold Loan vs 36% Credit Card)" whenever a keyword
    like "interest" appeared, injecting fabricated calculations into long chats.)
    """

    @classmethod
    def summarize_messages(
        cls,
        messages: List[Any],
        profile: Optional[FinancialProfile] = None,
    ) -> str:
        if not messages:
            return ""

        # Local import avoids a circular import (extractor imports financial).
        from .extractor import FactExtractor

        topics: List[str] = []
        updates: List[str] = []
        hypotheticals: List[str] = []
        assistant_points: List[str] = []

        for msg in messages:
            content = msg.content if hasattr(msg, "content") else msg.get("content", "")
            role = msg.role if hasattr(msg, "role") else msg.get("role", "")
            if not content or not content.strip():
                continue

            if role == "user":
                status = FactExtractor.classify_statement_status(content)
                excerpt = _excerpt(content, USER_EXCERPT_CHARS)
                if status in (FactStatus.HYPOTHETICAL.value, FactStatus.CONDITIONAL.value):
                    hypotheticals.append(f"[HYPOTHETICAL] User explored: \"{excerpt}\"")
                elif status == FactStatus.HISTORICAL.value or _UPDATE_CUES.search(content):
                    updates.append(f"User stated a change/past value: \"{excerpt}\"")
                else:
                    topics.append(f"User asked/said: \"{excerpt}\"")

            elif role == "assistant":
                for sentence in _SENTENCE_SPLIT.split(content):
                    if _HAS_FIGURE.search(sentence):
                        assistant_points.append(f"TORA said: \"{_excerpt(sentence, ASSISTANT_EXCERPT_CHARS)}\"")
                        break

        def _dedupe_tail(items: List[str], limit: int) -> List[str]:
            unique = list(dict.fromkeys(items))
            return unique[-limit:]

        lines = [
            "## Conversation History Summary",
            "(Condensed excerpts of earlier turns. Figures quoted here are what was said at the time; "
            "the User Verified Financial Profile is authoritative for current values.)",
        ]
        if topics:
            lines.append("### Key Discussion Topics")
            lines.extend(f"- {t}" for t in _dedupe_tail(topics, MAX_TOPIC_LINES))
        if updates:
            lines.append("### Historical State Changes Recorded")
            lines.extend(f"- {u}" for u in _dedupe_tail(updates, MAX_UPDATE_LINES))
        if assistant_points:
            lines.append("### Earlier Answers With Figures")
            lines.extend(f"- {a}" for a in _dedupe_tail(assistant_points, MAX_ASSISTANT_POINTS))
        if hypotheticals:
            lines.append("### Hypothetical Scenarios Discussed (Non-Current)")
            lines.extend(f"- {h}" for h in _dedupe_tail(hypotheticals, MAX_HYPOTHETICAL_LINES))

        return "\n".join(lines)
