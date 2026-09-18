"""
Which tools could possibly help this turn — and whether any could.

Measured on the live box: prefill runs at ~39 tokens/sec, and the planner prompt carries every
tool's schema (~3,700 tokens, of which finance_calc alone is 930). So a planner call costs
~95 seconds of reading before the model writes a word, and 19 of 35 live planner calls returned
"no tools needed" — pure cost.

This module answers two questions before that call is made:
  * `needs_no_tools` — can this turn be answered from memory and the conversation alone?
  * `candidate_tools` — which schemas are worth sending?

Both are deliberately conservative: when the wording is unclear, every tool goes in and the
planner decides. Getting this wrong costs a wrong answer; getting it slow costs seconds.
"""
from __future__ import annotations

import re
from typing import Any, List, Optional, Set

from ..context.normalize import normalize_message

# A turn that only reads back what TORA already knows needs no engine at all.
MEMORY_ONLY_INTENTS = ("memory_recall", "memory_update", "memory_delete", "clarification")

# ... unless the user is also asking for something to be worked out. Entity words alone don't
# count: "forget my SIP" names a fact to delete, it does not ask for a projection.
_CALC_REQUEST = re.compile(
    r"\b(?:calculate|compute|work\s+out|figure\s+out|project\w*|simulate|estimate|"
    r"compare|better|worth\s+it|afford|should\s+i|what\s+if|how\s+long\s+(?:will|would|until)|"
    r"plan\s+(?:my|for|to)|what\s+will\s+i\s+(?:have|pay|owe))\b",
    re.IGNORECASE,
)
# Tools whose subject matter a memory question can still need (records, live data, the rulebook).
_BLOCKS_MEMORY_ONLY = {"spendsy_data", "research", "web_search", "web_fetch", "rules_lookup", "tax_calc"}
_HAS_FIGURE = re.compile(r"(?<![\w.])\d{3,}|\b\d+(?:\.\d+)?\s*(?:k|lakhs?|lacs?|crores?|cr|%)\b", re.IGNORECASE)

_SIGNALS = (
    ("finance_calc", r"\b(?:emi|loan|loans|interest|prepay\w*|amorti\w*|sip|invest\w*|corpus|retire\w*|"
                     r"goal|budget|emergency|debt|debts|card|payoff|repay\w*|net\s*worth|savings?\s*rate|"
                     r"afford|surplus|consolidat\w*|rent\s+vs\s+buy|down\s*payment|inflation)\b"),
    ("tax_calc", r"\b(?:tax|taxes|taxable|regime|80\s*[cd]|80ccd|hra|deduction|rebate|cess|tds|itr|"
                 r"capital\s+gains?|ltcg|stcg|advance\s+tax|form\s*16|salary\s+slip)\b"),
    ("rules_lookup", r"\b(?:rule|rules|limit|limits|section|allowed|eligib\w*|deadline|due\s+date|penalt\w*|"
                     r"rbi|sebi|law|legal|recovery\s+agent|harass\w*|ombudsman|grievance|regulation)\b"),
    ("calculator", r"\b(?:calculate|compute|percent|percentage|%|plus|minus|times|divided|sum|total)\b|[-+*/=]"),
    ("spendsy_data", r"\b(?:spent|spend|spending|transactions?|statement|last\s+month|this\s+month|"
                     r"category|categories|my\s+expenses|where\s+is\s+my\s+money)\b"),
    ("research", r"\b(?:current|latest|today|now|live|market|rates?\s+(?:at|of|for)|compare\s+\w+\s+and|"
                 r"best\s+(?:bank|rate|fd|card)|news|announc\w*|which\s+bank)\b"),
    ("web_search", r"\b(?:search|google|look\s+up\s+online|find\s+online)\b"),
    ("web_fetch", r"https?://|\bwww\.|\b(?:this\s+(?:page|link|url)|read\s+this)\b"),
)
_COMPILED = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _SIGNALS]

# Where a tool is chosen, these ride along: the planner often needs arithmetic beside an engine,
# and a rules answer usually accompanies a tax one.
_COMPANIONS = {
    "finance_calc": ("calculator",),
    "tax_calc": ("calculator", "rules_lookup"),
    "research": ("web_search", "web_fetch"),
    "web_search": ("web_fetch",),
}


def needs_no_tools(message: str, intent: Any = None, has_documents: bool = False) -> bool:
    """True when the turn is answerable from memory and history — so the planner can be skipped."""
    name = getattr(getattr(intent, "intent", intent), "value", None) or str(getattr(intent, "intent", intent) or "")
    if name not in MEMORY_ONLY_INTENTS or has_documents:
        return False
    text = normalize_message(message or "")
    if _CALC_REQUEST.search(text):
        return False
    signals = {tool for tool, rx in _COMPILED if rx.search(text)}
    if signals & _BLOCKS_MEMORY_ONLY:
        return False
    if name in ("memory_recall", "clarification") and _HAS_FIGURE.search(text):
        return False          # a figure in a recall usually means "work this out with it"
    return True


def candidate_tools(message: str, intent: Any = None, available: Optional[Set[str]] = None) -> Set[str]:
    """The tools worth putting in front of the planner. Empty means 'no idea' — send them all."""
    text = normalize_message(message or "")
    chosen: Set[str] = set()
    for name, rx in _COMPILED:
        if rx.search(text):
            chosen.add(name)
            chosen.update(_COMPANIONS.get(name, ()))
    name = getattr(getattr(intent, "intent", intent), "value", None) or ""
    if name in ("research", "comparison", "research_followup"):
        chosen.update(("research", "web_search", "web_fetch"))
    if name in ("calculation", "what_if", "planning"):
        chosen.update(("calculator", "finance_calc"))
    if available is not None:
        chosen &= set(available)
    return chosen


def filter_schemas(schemas: List[dict], keep: Set[str]) -> List[dict]:
    """Tool schemas for the planner prompt, narrowed to the candidates (all of them if unsure)."""
    if not keep:
        return schemas
    narrowed = [s for s in schemas if (s.get("function", {}).get("name") or s.get("name")) in keep]
    return narrowed or schemas
