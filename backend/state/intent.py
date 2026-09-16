"""
Deterministic intent classification and follow-up resolution for TORA.

Runs before the LLM planner. It is cheap, testable and conversation-aware:
short follow-ups such as "What about HDFC?" or "Which one was cheaper?" are
resolved against the active topic kept in ConversationState.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .conversation_state import ConversationState


class Intent(str, Enum):
    GENERAL_QA = "general_qa"
    FINANCIAL_QA = "financial_qa"
    CALCULATION = "calculation"
    MEMORY_RECALL = "memory_recall"
    MEMORY_UPDATE = "memory_update"
    MEMORY_DELETE = "memory_delete"
    RESEARCH = "research"
    RESEARCH_FOLLOWUP = "research_followup"
    COMPARISON = "comparison"
    PLANNING = "planning"
    WHAT_IF = "what_if"
    CLARIFICATION = "clarification"


# Canonical entity name -> regex alternatives (word-bounded, case-insensitive)
ENTITY_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("SBI", r"sbi|state bank(?: of india)?"),
    ("HDFC Bank", r"hdfc(?: bank)?"),
    ("ICICI Bank", r"icici(?: bank)?"),
    ("Axis Bank", r"axis(?: bank)?"),
    ("Kotak Mahindra Bank", r"kotak(?: mahindra)?(?: bank)?"),
    ("Punjab National Bank", r"pnb|punjab national bank"),
    ("Bank of Baroda", r"bob|bank of baroda"),
    ("Canara Bank", r"canara(?: bank)?"),
    ("Union Bank of India", r"union bank(?: of india)?"),
    ("Bank of India", r"bank of india"),
    ("IDFC First Bank", r"idfc(?: first)?(?: bank)?"),
    ("Yes Bank", r"yes bank"),
    ("IndusInd Bank", r"indusind(?: bank)?"),
    ("Federal Bank", r"federal bank"),
    ("AU Small Finance Bank", r"au (?:small finance )?bank"),
    ("Bajaj Finance", r"bajaj(?: finance| finserv)?"),
    ("LIC Housing Finance", r"lic hfl|lic housing(?: finance)?"),
    ("LIC", r"lic"),
    ("Muthoot Finance", r"muthoot(?: finance)?"),
    ("Manappuram Finance", r"manappuram(?: finance)?"),
    ("Tata Capital", r"tata capital"),
    ("Post Office", r"post office|india post"),
    ("RBI", r"rbi|reserve bank(?: of india)?"),
    ("SEBI", r"sebi"),
    ("Income Tax Department", r"income tax department|incometax\.gov\.in|itd"),
    ("EPFO", r"epfo"),
)

# Canonical product label -> regex alternatives
PRODUCT_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("home loan", r"home loans?|housing loans?|mortgages?"),
    ("personal loan", r"personal loans?"),
    ("car loan", r"car loans?|auto loans?|vehicle loans?"),
    ("gold loan", r"gold loans?"),
    ("education loan", r"education loans?|student loans?"),
    ("loan against property", r"loans? against property|lap"),
    ("credit card", r"credit cards?|cc"),
    ("fixed deposit", r"fixed deposits?|fds?|term deposits?"),
    ("recurring deposit", r"recurring deposits?|rds?"),
    ("savings account", r"savings accounts?"),
    ("mutual fund", r"mutual funds?|mfs?"),
    ("SIP", r"sips?|systematic investment plans?"),
    ("PPF", r"ppf|public provident fund"),
    ("EPF", r"epf|provident fund"),
    ("NPS", r"nps|national pension (?:system|scheme)"),
    ("repo rate", r"repo rates?"),
    ("income tax", r"income tax|tax slabs?|tax regime|itr|80c|80d|tds"),
    ("GST", r"gst"),
    ("insurance", r"insurance|term plans?|health cover"),
    ("gold", r"gold rates?|gold prices?"),
    ("stocks", r"stocks?|shares?|equity|nifty|sensex"),
)

_ENTITY_RES = [(name, re.compile(r"(?<![\w.])(?:" + pat + r")(?![\w])", re.IGNORECASE)) for name, pat in ENTITY_PATTERNS]
_PRODUCT_RES = [(name, re.compile(r"(?<![\w])(?:" + pat + r")(?![\w])", re.IGNORECASE)) for name, pat in PRODUCT_PATTERNS]

_ARITHMETIC = re.compile(r"\d\s*[*/+^×÷x-]\s*\d|\b(?:calculate|compute|how much will|how much would|emi (?:for|on|of)|"
                         r"what is \d+(?:\.\d+)?\s*%|percent of|% of|amortization|amortisation|maturity (?:value|amount)|"
                         r"future value|corpus|payoff|pay off .* in)\b", re.IGNORECASE)
_CALC_TOPIC_WITH_NUMBER = re.compile(
    r"(?=.*\d)(?=.*\b(?:emi|sip|corpus|maturity|compound|amortization|prepay\w*|how much tax|tax on|tax payable|"
    r"regime is better|which regime|net worth|debt[- ]free|retire\w*|inflation|payoff|pay off)\b)", re.IGNORECASE | re.DOTALL)
_REQUEST_WORDS = re.compile(r"\b(?:help|plan|calculate|compute|compare|tell me|show|explain|suggest|advise|"
                            r"recommend|should|can you|could you|please|how|what|which|why)\b", re.IGNORECASE)
_WHAT_IF = re.compile(r"\b(?:what if|suppose|assuming|assume|imagine|hypothetically|let'?s say|if i (?:increase|decrease|"
                      r"invest|take|pay|prepay|earn|save|stop|start|switch)|if my)\b", re.IGNORECASE)
_COMPARISON = re.compile(r"\b(?:compare|comparison|vs\.?|versus|which is (?:better|cheaper|best)|better than|"
                         r"cheapest|lowest|highest|best)\b", re.IGNORECASE)
_CURRENT_INFO = re.compile(r"\b(?:current|currently|latest|today|now|this (?:week|month|year)|live|recent|"
                           r"rates?|interest rates?|charges?|fees?|circular|notification|announced)\b", re.IGNORECASE)
_MEMORY_RECALL = re.compile(r"\b(?:what (?:is|was|were|are) my|my (?:previous|original|old|last|current)|"
                            r"what did i (?:say|tell|mention)|remind me|do you remember|what do you know about me|"
                            r"how much (?:do|did) i (?:earn|make|pay|owe|have)|what'?s my)\b", re.IGNORECASE)
_MEMORY_FACT_WORDS = re.compile(r"\b(?:salary|income|earn|rent|savings?|emergency fund|balance|debt|loan|emi|card|"
                                r"investments?|mutual funds?|gold|goal|expenses?|profile|about me)\b", re.IGNORECASE)
_PLANNING = re.compile(r"\b(?:plan|budget|roadmap|strategy|allocate|allocation|goal|retire|retirement|"
                       r"emergency fund|how should i|help me (?:save|invest|pay))\b", re.IGNORECASE)
_PRONOUN_REF = re.compile(r"\b(?:it|that|those|these|them|they|this one|that one|which one|the first|the second|"
                          r"the third|the other|the same|previous one|last one|there)\b", re.IGNORECASE)
_FOLLOWUP_OPENERS = re.compile(r"^\s*(?:and|what about|how about|also|same for|and for|then|ok(?:ay)?,?|so)\b", re.IGNORECASE)
_CLARIFY = re.compile(r"^\s*(?:why\??|how\??|what\??|huh\??|explain(?: more| that)?\.?|elaborate\.?|"
                      r"can you explain(?: that| more)?\??|what do you mean\??|meaning\??|really\??)\s*$", re.IGNORECASE)
_FINANCE_WORDS = re.compile(r"\b(?:loan|emi|interest|tax|salary|income|invest|saving|savings|budget|debt|credit|"
                            r"deposit|fund|insurance|pension|rent|expense|money|rupee|inr|₹|bank|cibil|credit score|"
                            r"inflation|return|portfolio|asset|liabilit)", re.IGNORECASE)


def find_entities(text: str) -> List[str]:
    found: List[Tuple[int, str]] = []
    taken: List[Tuple[int, int]] = []
    for name, rx in _ENTITY_RES:
        for m in rx.finditer(text):
            span = m.span()
            if any(not (span[1] <= a or span[0] >= b) for a, b in taken):
                continue
            taken.append(span)
            found.append((span[0], name))
    ordered: List[str] = []
    for _, name in sorted(found):
        if name not in ordered:
            ordered.append(name)
    return ordered


def find_product(text: str) -> Optional[str]:
    best: Optional[Tuple[int, str]] = None
    for name, rx in _PRODUCT_RES:
        m = rx.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    return best[1] if best else None


@dataclass
class IntentResult:
    intent: Intent
    entities: List[str] = field(default_factory=list)
    product: Optional[str] = None
    is_followup: bool = False
    resolved_query: Optional[str] = None
    topic_key: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    @property
    def skips_planner(self) -> bool:
        return self.intent in (Intent.MEMORY_RECALL, Intent.MEMORY_DELETE, Intent.CLARIFICATION)

    def to_dict(self):
        return {
            "intent": self.intent.value,
            "entities": self.entities,
            "product": self.product,
            "is_followup": self.is_followup,
            "resolved_query": self.resolved_query,
            "topic_key": self.topic_key,
        }


class IntentClassifier:
    """Rule-based, conversation-aware intent classifier."""

    SHORT_FOLLOWUP_WORDS = 9

    @classmethod
    def classify(
        cls,
        message: str,
        state: Optional["ConversationState"] = None,
        memory_commands: Optional[list] = None,
        extracted_facts: Optional[list] = None,
    ) -> IntentResult:
        text = (message or "").strip()
        entities = find_entities(text)
        product = find_product(text)
        words = len(text.split())
        active = state.active_topic() if state is not None else None

        if memory_commands:
            if any(c.get("action") in ("delete", "clear") for c in memory_commands):
                return IntentResult(Intent.MEMORY_DELETE, entities, product)
            return IntentResult(Intent.MEMORY_UPDATE, entities, product)

        # Follow-up detection: short message leaning on earlier context
        refers_back = bool(_PRONOUN_REF.search(text)) or bool(_FOLLOWUP_OPENERS.search(text))
        is_short = words <= cls.SHORT_FOLLOWUP_WORDS
        is_followup = bool(
            active is not None
            and product is None
            and is_short
            and (refers_back or (entities and not _CURRENT_INFO.search(text) and words <= 5))
        )

        if _CLARIFY.match(text):
            return IntentResult(Intent.CLARIFICATION, entities, product, is_followup=active is not None,
                                topic_key=active.key if active else None)

        if _WHAT_IF.search(text):
            intent = Intent.WHAT_IF
        elif extracted_facts and "?" not in text and not _REQUEST_WORDS.search(text) and not _ARITHMETIC.search(text):
            intent = Intent.MEMORY_UPDATE
        elif _ARITHMETIC.search(text) or _CALC_TOPIC_WITH_NUMBER.match(text):
            intent = Intent.CALCULATION
        elif _MEMORY_RECALL.search(text) and _MEMORY_FACT_WORDS.search(text) and not entities:
            intent = Intent.MEMORY_RECALL
        elif is_followup and active is not None and active.has_research():
            intent = Intent.RESEARCH_FOLLOWUP
        elif _COMPARISON.search(text) and (len(entities) >= 2 or product or (is_followup and active is not None)):
            intent = Intent.COMPARISON
        elif (entities or product) and _CURRENT_INFO.search(text):
            intent = Intent.RESEARCH
        elif _PLANNING.search(text):
            intent = Intent.PLANNING
        elif extracted_facts and "?" not in text:
            intent = Intent.MEMORY_UPDATE
        elif _FINANCE_WORDS.search(text) or entities or product:
            intent = Intent.FINANCIAL_QA
        else:
            intent = Intent.GENERAL_QA

        result = IntentResult(intent, entities, product, is_followup=is_followup)

        if is_followup and active is not None:
            result.topic_key = active.key
            result.resolved_query = cls._resolve(text, entities, active)
            result.notes.append(
                f"Short follow-up interpreted within the active topic '{active.label}'."
            )
        elif product or entities:
            result.topic_key = None  # the state decides whether this opens/returns to a topic
        return result

    @staticmethod
    def _resolve(text: str, entities: List[str], topic) -> str:
        """Rewrite a short follow-up into a self-contained query for the planner/tools."""
        subject_entities = entities or list(topic.entities)
        subject = ", ".join(subject_entities) if subject_entities else ""
        parts = [p for p in (subject, topic.product or "", topic.aspect or "") if p]
        base = " ".join(parts).strip() or topic.label
        return f"{text.rstrip('?.! ')} — about {base}".strip()
