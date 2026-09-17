"""
Per-turn tracing and in-process metrics for TORA (Phase 4D).

Traces contain metadata only — never message text, figures or facts — so they
can be logged safely: intent, planner decision, tools and their latency, LLM
call count/latency/token usage, grounding outcome and final status.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections import Counter, defaultdict, deque
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

_current: ContextVar[Optional["TurnTrace"]] = ContextVar("tora_current_trace", default=None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def conversation_ref(conversation_id: Optional[str]) -> Optional[str]:
    if not conversation_id:
        return None
    return hashlib.sha256(conversation_id.encode()).hexdigest()[:12]


@dataclass
class TurnTrace:
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: str = field(default_factory=_now_iso)
    endpoint: str = "chat"
    mode: str = "stateless"
    conversation_ref: Optional[str] = None
    turn: Optional[int] = None
    intent: Optional[str] = None
    is_followup: bool = False
    planner: Dict[str, Any] = field(default_factory=lambda: {"used": False})
    tools: List[Dict[str, Any]] = field(default_factory=list)
    llm: Dict[str, Any] = field(default_factory=lambda: {
        "calls": 0, "ms": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "model": None})
    prompt_tokens_estimate: Optional[int] = None
    model_facts: int = 0
    overflow_retry: bool = False
    grounding: Optional[Dict[str, Any]] = None
    status: str = "in_progress"
    http_status: Optional[int] = None
    error_type: Optional[str] = None
    total_ms: Optional[float] = None
    _t0: float = field(default_factory=time.monotonic, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("_t0", None)
        return d


def current_trace() -> Optional[TurnTrace]:
    return _current.get()


def start_trace(**fields: Any) -> TurnTrace:
    trace = TurnTrace(**fields)
    _current.set(trace)
    return trace


def record_llm_usage(ms: float, raw: Optional[Dict[str, Any]], model: Optional[str] = None) -> None:
    """Called by LLM providers for every completion (planner, answer, retries)."""
    trace = _current.get()
    if trace is None:
        return
    trace.llm["calls"] += 1
    trace.llm["ms"] = round(trace.llm["ms"] + ms, 2)
    if model:
        trace.llm["model"] = model
    if isinstance(raw, dict):
        trace.llm["prompt_tokens"] += int(raw.get("prompt_eval_count") or 0)
        trace.llm["completion_tokens"] += int(raw.get("eval_count") or 0)


class TraceTimer:
    def __init__(self) -> None:
        self.t0 = time.monotonic()

    @property
    def ms(self) -> float:
        return round((time.monotonic() - self.t0) * 1000, 2)


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1)))))
    return round(ordered[k], 2)


class TelemetryHub:
    """Collects finished traces: ring buffer, optional JSONL file, aggregate metrics."""

    def __init__(self, trace_file: Optional[str] = None, buffer_size: int = 200):
        self.trace_file = trace_file
        self._lock = threading.Lock()
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=buffer_size)
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.started_at = _now_iso()
            self.requests = Counter()
            self.intents = Counter()
            self.tools: Dict[str, Counter] = defaultdict(Counter)
            self.tool_ms: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=500))
            self.grounding = Counter()
            self.errors = Counter()
            self.latency: Deque[float] = deque(maxlen=1000)
            self.llm_calls = 0
            self.prompt_tokens = 0
            self.completion_tokens = 0
            self.overflow_retries = 0
            self.planner_skipped = 0
            self._recent.clear()

    def finish(self, trace: TurnTrace, status: str = "ok", http_status: int = 200,
               error_type: Optional[str] = None) -> Dict[str, Any]:
        trace.status = status
        trace.http_status = http_status
        trace.error_type = error_type
        trace.total_ms = round((time.monotonic() - trace._t0) * 1000, 2)
        data = trace.to_dict()
        with self._lock:
            self.requests[status] += 1
            if trace.intent:
                self.intents[trace.intent] += 1
            for t in trace.tools:
                self.tools[t["name"]]["ok" if t.get("ok") else "error"] += 1
                if t.get("ms") is not None:
                    self.tool_ms[t["name"]].append(t["ms"])
            if trace.grounding:
                self.grounding[trace.grounding.get("action", "none")] += 1
            if error_type:
                self.errors[error_type] += 1
            if status == "ok":
                self.latency.append(trace.total_ms)
            self.llm_calls += trace.llm["calls"]
            self.prompt_tokens += trace.llm["prompt_tokens"]
            self.completion_tokens += trace.llm["completion_tokens"]
            self.overflow_retries += int(trace.overflow_retry)
            if trace.intent and not trace.planner.get("used"):
                self.planner_skipped += 1
            self._recent.append(data)
            if self.trace_file:
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(self.trace_file)), exist_ok=True)
                    with open(self.trace_file, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(data, ensure_ascii=False) + "\n")
                except OSError:
                    pass
        _current.set(None)
        return data

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._recent)[-max(1, min(limit, len(self._recent) or 1)):]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            total = sum(self.requests.values())
            lat = list(self.latency)
            return {
                "since": self.started_at,
                "requests": {"total": total, **dict(self.requests)},
                "success_rate": round(self.requests.get("ok", 0) / total, 4) if total else None,
                "latency_ms": {"p50": _percentile(lat, 50), "p95": _percentile(lat, 95),
                               "max": max(lat) if lat else None, "samples": len(lat)},
                "intents": dict(self.intents),
                "planner_skipped": self.planner_skipped,
                "tools": {
                    name: {"ok": c.get("ok", 0), "error": c.get("error", 0),
                           "p50_ms": _percentile(list(self.tool_ms[name]), 50),
                           "p95_ms": _percentile(list(self.tool_ms[name]), 95)}
                    for name, c in self.tools.items()
                },
                "llm": {"calls": self.llm_calls, "prompt_tokens": self.prompt_tokens,
                        "completion_tokens": self.completion_tokens, "overflow_retries": self.overflow_retries},
                "grounding": dict(self.grounding),
                "errors": dict(self.errors),
            }
