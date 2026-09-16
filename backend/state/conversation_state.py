"""
Conversation state for TORA: topics, entities and research already gathered.

Keeps structured memory of *what the conversation is about* (distinct from the
user's financial profile) so that follow-ups ("those sources", "what about HDFC?")
and returning to an earlier topic ("back to the home loan comparison") resolve
against real, previously fetched data instead of the model's recollection.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .intent import Intent, IntentResult, find_entities, find_product

MAX_TOPICS = 12
MAX_RESEARCH_PER_TOPIC = 3
MAX_CONCLUSIONS_PER_RECORD = 8
MAX_WEB_RESULTS_PER_TOPIC = 5
MAX_CALCULATIONS = 6
TRUSTED_RENDER_CHARS = 1400
EXTERNAL_RENDER_CHARS = 2600

_ASPECT_PATTERNS = (
    ("interest rates", re.compile(r"\b(?:interest )?rates?\b|\binterest\b", re.I)),
    ("fees and charges", re.compile(r"\b(?:fees?|charges?|processing)\b", re.I)),
    ("eligibility", re.compile(r"\beligib", re.I)),
    ("tax rules", re.compile(r"\b(?:slabs?|deductions?|regime)\b", re.I)),
    ("returns", re.compile(r"\breturns?\b", re.I)),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _aspect_of(text: str) -> Optional[str]:
    for label, rx in _ASPECT_PATTERNS:
        if rx.search(text):
            return label
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "general"


@dataclass
class ResearchRecord:
    query: str
    turn: int
    tool: str
    conclusions: List[Dict[str, Any]] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    status: Optional[str] = None
    retrieved_at: str = field(default_factory=_now)

    def entities(self) -> List[str]:
        names: List[str] = []
        for c in self.conclusions:
            for n in find_entities(" ".join(str(c.get(k) or "") for k in ("entity", "statement"))):
                if n not in names:
                    names.append(n)
        return names

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ResearchRecord":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


@dataclass
class TopicState:
    key: str
    label: str
    product: Optional[str] = None
    aspect: Optional[str] = None
    entities: List[str] = field(default_factory=list)
    first_turn: int = 0
    last_turn: int = 0
    research: List[ResearchRecord] = field(default_factory=list)
    web_results: List[Dict[str, Any]] = field(default_factory=list)

    def has_research(self) -> bool:
        return bool(self.research or self.web_results)

    def covers_entities(self, names: List[str]) -> bool:
        """True when stored research already mentions every requested entity."""
        if not names:
            return self.has_research()
        known = set()
        for r in self.research:
            known.update(r.entities())
        for w in self.web_results:
            known.update(find_entities(f"{w.get('title', '')} {w.get('snippet', '')} {w.get('domain', '')}"))
        return all(n in known for n in names)

    def add_entities(self, names: List[str]) -> None:
        for n in names:
            if n not in self.entities:
                self.entities.append(n)

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["research"] = [r.to_dict() for r in self.research]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TopicState":
        t = cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d and k != "research"})
        t.research = [ResearchRecord.from_dict(r) for r in d.get("research", [])]
        t.entities = list(t.entities or [])
        t.web_results = list(t.web_results or [])
        return t


@dataclass
class ConversationState:
    turn_count: int = 0
    active_topic_key: Optional[str] = None
    topics: Dict[str, TopicState] = field(default_factory=dict)
    calculations: List[Dict[str, Any]] = field(default_factory=list)
    last_intent: Optional[str] = None
    last_resolution: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ topics
    def active_topic(self) -> Optional[TopicState]:
        if self.active_topic_key:
            return self.topics.get(self.active_topic_key)
        return None

    def begin_turn(self) -> int:
        self.turn_count += 1
        return self.turn_count

    def _find_topic(self, product: Optional[str], entities: List[str]) -> Optional[TopicState]:
        if product:
            matches = [t for t in self.topics.values() if t.product == product]
            if matches:
                return max(matches, key=lambda t: t.last_turn)
        return None

    def observe(self, message: str, intent: IntentResult) -> Optional[TopicState]:
        """Update topic tracking from the user's message and its classified intent."""
        turn = self.turn_count
        self.last_intent = intent.intent.value
        self.last_resolution = intent.to_dict()

        if intent.intent in (Intent.MEMORY_RECALL, Intent.MEMORY_UPDATE, Intent.MEMORY_DELETE):
            # Talking about the user's own profile never switches the research topic.
            return self.active_topic()

        if intent.is_followup and self.active_topic() is not None:
            topic = self.active_topic()
            topic.add_entities(intent.entities)
            topic.last_turn = turn
            return topic

        product = intent.product or find_product(message)
        if product is None:
            # Entity-only mention while a topic is active (e.g. "SBI's rate?")
            active = self.active_topic()
            if active is not None and intent.entities and intent.intent in (
                Intent.RESEARCH, Intent.COMPARISON, Intent.RESEARCH_FOLLOWUP, Intent.FINANCIAL_QA
            ):
                active.add_entities(intent.entities)
                active.last_turn = turn
                return active
            return None

        topic = self._find_topic(product, intent.entities)
        if topic is None:
            aspect = _aspect_of(message)
            label = f"{product} {aspect}" if aspect else product
            key = _slug(product)
            topic = TopicState(key=key, label=label, product=product, aspect=aspect,
                               first_turn=turn, last_turn=turn)
            self.topics[key] = topic
            self._evict_topics()
        else:
            aspect = _aspect_of(message)
            if aspect and not topic.aspect:
                topic.aspect = aspect
                topic.label = f"{topic.product} {aspect}"
        topic.add_entities(intent.entities)
        topic.last_turn = turn
        self.active_topic_key = topic.key
        return topic

    def _evict_topics(self) -> None:
        if len(self.topics) <= MAX_TOPICS:
            return
        for key, _ in sorted(self.topics.items(), key=lambda kv: kv[1].last_turn)[: len(self.topics) - MAX_TOPICS]:
            if key != self.active_topic_key:
                del self.topics[key]

    # ------------------------------------------------------------ tool results
    def record_tool_results(self, tool_context, query: str) -> None:
        """Persist research/web/calculator results against the active topic."""
        if tool_context is None or tool_context.is_empty():
            return
        turn = self.turn_count
        topic = self.active_topic()
        for res in tool_context.results:
            if res.is_error or not isinstance(res.output, dict):
                continue
            out = res.output
            if res.tool_name == "calculator":
                self.calculations.append({
                    "turn": turn,
                    "expression": out.get("expression"),
                    "result": out.get("result"),
                })
                self.calculations = self.calculations[-MAX_CALCULATIONS:]
            elif res.tool_name in ("finance_calc", "tax_calc"):
                self.calculations.append({
                    "turn": turn,
                    "expression": out.get("operation"),
                    "result": out.get("summary") or out.get("result"),
                })
                self.calculations = self.calculations[-MAX_CALCULATIONS:]
            elif "conclusions" in out:
                if topic is None:
                    topic = self._topic_from_text(out.get("query") or query)
                rec = ResearchRecord(
                    query=out.get("query") or query,
                    turn=turn,
                    tool=res.tool_name,
                    status=out.get("overall_status"),
                    conclusions=[
                        {
                            "entity": c.get("entity"),
                            "statement": c.get("synthesized_statement"),
                            "qualifiers": c.get("qualifiers") or [],
                            "confidence": c.get("confidence"),
                            "urls": (c.get("provenance_urls") or [])[:2],
                            "effective_date": c.get("effective_date"),
                        }
                        for c in (out.get("conclusions") or [])[:MAX_CONCLUSIONS_PER_RECORD]
                    ],
                    sources=[
                        {"domain": s.get("domain"), "authority": s.get("authority_level"),
                         "official": s.get("is_official")}
                        for s in (out.get("sources") or [])[:6]
                    ],
                    conflicts=[c.get("reason", "") for c in (out.get("conflicts") or [])][:4],
                )
                topic.research.append(rec)
                topic.research = topic.research[-MAX_RESEARCH_PER_TOPIC:]
                topic.add_entities(rec.entities())
            elif res.tool_name == "web_search" and out.get("results"):
                if topic is None:
                    topic = self._topic_from_text(out.get("query") or query)
                items = [
                    {"turn": turn, "title": r.get("title"), "domain": r.get("domain"),
                     "snippet": (r.get("snippet") or "")[:240], "url": r.get("url")}
                    for r in out["results"][:MAX_WEB_RESULTS_PER_TOPIC]
                ]
                topic.web_results = (topic.web_results + items)[-MAX_WEB_RESULTS_PER_TOPIC:]
                for it in items:
                    topic.add_entities(find_entities(f"{it['title']} {it['snippet']}"))

    def _topic_from_text(self, text: str) -> TopicState:
        product = find_product(text) or "general research"
        topic = self._find_topic(product, [])
        if topic is None:
            aspect = _aspect_of(text)
            topic = TopicState(key=_slug(product), label=f"{product} {aspect}" if aspect else product,
                               product=product if product != "general research" else None, aspect=aspect,
                               first_turn=self.turn_count, last_turn=self.turn_count)
            self.topics[topic.key] = topic
        self.active_topic_key = topic.key
        return topic

    # --------------------------------------------------------------- rendering
    def render_trusted(self) -> str:
        """Structural state (no third-party text) — safe for the system message."""
        lines: List[str] = []
        active = self.active_topic()
        if active is not None:
            ents = f" — entities: {', '.join(active.entities)}" if active.entities else ""
            lines.append(f"- Active topic: {active.label} (since turn {active.first_turn}){ents}")
            if active.has_research():
                lines.append(
                    f"- Research for this topic was gathered earlier (latest turn "
                    f"{max([r.turn for r in active.research] + [w.get('turn', 0) for w in active.web_results])}); "
                    "it is attached as previously gathered external data. Resolve references such as "
                    "'those sources', 'that bank' or 'the first one' against it."
                )
        others = [t for k, t in sorted(self.topics.items(), key=lambda kv: -kv[1].last_turn) if k != self.active_topic_key]
        if others:
            lines.append("- Other topics discussed: " + "; ".join(
                f"{t.label} (turn {t.last_turn}{', has research' if t.has_research() else ''})" for t in others[:5]
            ))
        if self.last_resolution and self.last_resolution.get("is_followup") and self.last_resolution.get("resolved_query"):
            lines.append(f"- The latest message is a follow-up; interpreted as: \"{self.last_resolution['resolved_query']}\"")
        if self.calculations:
            lines.append("- Recent deterministic calculations:")
            for c in self.calculations[-3:]:
                lines.append(f"  - turn {c['turn']}: {c.get('expression')} = {c.get('result')}")
        if not lines:
            return ""
        text = "## Conversation State\n" + "\n".join(lines)
        return text[:TRUSTED_RENDER_CHARS]

    def render_external(self, exclude_turn: Optional[int] = None) -> str:
        """Previously gathered third-party research for the active topic (untrusted)."""
        active = self.active_topic()
        if active is None or not active.has_research():
            return ""
        lines = [f"- Previously gathered research — topic: {active.label}"]
        for rec in active.research:
            if exclude_turn is not None and rec.turn == exclude_turn:
                continue
            lines.append(f"  Research from turn {rec.turn} for \"{rec.query}\" (status: {rec.status}):")
            for i, c in enumerate(rec.conclusions, start=1):
                qual = f" [qualifiers: {', '.join(c['qualifiers'])}]" if c.get("qualifiers") else ""
                src = f" — sources: {', '.join(c['urls'])}" if c.get("urls") else ""
                lines.append(f"    {i}. {c.get('statement')}{qual} (confidence: {c.get('confidence')}){src}")
            if rec.sources:
                lines.append("    Sources checked: " + ", ".join(
                    f"{s.get('domain')} ({s.get('authority')}{', official' if s.get('official') else ''})" for s in rec.sources
                ))
            for cf in rec.conflicts:
                lines.append(f"    Conflict: {cf}")
        web = [w for w in active.web_results if exclude_turn is None or w.get("turn") != exclude_turn]
        if web:
            lines.append("  Earlier search results:")
            for i, w in enumerate(web, start=1):
                lines.append(f"    {i}. {w.get('title')} — {w.get('domain')}: {w.get('snippet')}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)[:EXTERNAL_RENDER_CHARS]

    # ----------------------------------------------------------- serialization
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "turn_count": self.turn_count,
            "active_topic_key": self.active_topic_key,
            "topics": {k: t.to_dict() for k, t in self.topics.items()},
            "calculations": list(self.calculations),
            "last_intent": self.last_intent,
            "last_resolution": self.last_resolution,
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "ConversationState":
        if not d:
            return cls()
        return cls(
            turn_count=d.get("turn_count", 0),
            active_topic_key=d.get("active_topic_key"),
            topics={k: TopicState.from_dict(v) for k, v in (d.get("topics") or {}).items()},
            calculations=list(d.get("calculations") or []),
            last_intent=d.get("last_intent"),
            last_resolution=d.get("last_resolution"),
        )
