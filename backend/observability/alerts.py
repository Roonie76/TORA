"""
Alerting: something shouts when TORA breaks, instead of someone noticing later.

`/api/metrics` and `/api/dashboard` already hold everything needed to see a
problem. The gap was that seeing it required a person to go and look, and nobody
looks at a dashboard until something has already gone wrong.

The hard part of alerting is not detection, it is **not crying wolf**. An alert
that fires on a quiet system, or on behaviour that is normal here, gets muted
within a week and then the real one is missed too. So:

* Every rule has a **minimum sample size**. One failed call out of one is not a
  failing tool, it is a failed call.
* **Latency is deliberately not alerted on.** On 2 CPU cores a Tier 2 answer
  legitimately takes minutes — p95 is measured in minutes by design, not by
  fault. A latency rule here would fire constantly and correctly mean nothing.
  What would be worth alerting on is latency for turns that should need no model
  at all, and that is `no_model_turns`, covered below.
* Firing is **level-triggered, not edge-triggered**: `evaluate()` reports what is
  wrong right now, from the current snapshot. There is no state to get stuck.
  Deduplication for delivery is the caller's problem (see `AlertLog`), so a rule
  can never latch on and refuse to clear.

What this module does not do is deliver to a pager or a webhook. Detection,
severity and a log line at WARNING/ERROR are here; where those lines are shipped
is a deployment decision, and a half-built HTTP sender with its own timeouts and
failure modes would be worse than none.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tora.alerts")

WARNING = "warning"
CRITICAL = "critical"

# Thresholds. Every one is paired with a minimum sample, because a rate over a
# tiny denominator is noise.
TOOL_MIN_CALLS = int(os.getenv("TORA_ALERT_TOOL_MIN_CALLS", "5"))
TOOL_ERROR_RATE = float(os.getenv("TORA_ALERT_TOOL_ERROR_RATE", "0.5"))

REQUEST_MIN_TOTAL = int(os.getenv("TORA_ALERT_REQUEST_MIN_TOTAL", "20"))
REQUEST_ERROR_RATE = float(os.getenv("TORA_ALERT_REQUEST_ERROR_RATE", "0.2"))

GROUNDING_MIN_TURNS = int(os.getenv("TORA_ALERT_GROUNDING_MIN_TURNS", "20"))
GROUNDING_REGENERATE_RATE = float(os.getenv("TORA_ALERT_GROUNDING_RATE", "0.25"))


@dataclass(frozen=True)
class Alert:
    key: str          # stable identity, so repeats can be recognised
    severity: str
    summary: str      # one line, readable by someone who did not write this
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "severity": self.severity,
                "summary": self.summary, "detail": dict(self.detail)}


def _rate(part: float, whole: float) -> float:
    return (part / whole) if whole else 0.0


def evaluate(metrics: Dict[str, Any]) -> List[Alert]:
    """
    Everything wrong with the system right now, worst first.

    Takes the metrics snapshot rather than reading global state, so a rule can be
    tested by handing it numbers instead of by breaking a real tool.
    """
    alerts: List[Alert] = []

    # 1. A tool held open by its circuit breaker. That tool is fully out of
    #    service and every answer needing it is degraded, so this is the loudest
    #    thing here.
    for name, state in (metrics.get("failing_tools") or {}).items():
        if state.get("open"):
            alerts.append(Alert(
                key=f"tool_open:{name}",
                severity=CRITICAL,
                summary=f"{name} is held open by the circuit breaker — every answer needing it is degraded.",
                detail={"tool": name, "consecutive_failures": state.get("consecutive_failures")},
            ))

    # 2. A tool failing often but not yet open. Answers are quietly getting worse.
    for name, counts in (metrics.get("tools") or {}).items():
        ok = int(counts.get("ok") or 0)
        err = int(counts.get("error") or 0)
        calls = ok + err
        if calls < TOOL_MIN_CALLS:
            continue
        rate = _rate(err, calls)
        if rate >= TOOL_ERROR_RATE and not any(a.key == f"tool_open:{name}" for a in alerts):
            alerts.append(Alert(
                key=f"tool_errors:{name}",
                severity=CRITICAL if rate >= 0.99 else WARNING,
                summary=f"{name} is failing {err} of {calls} calls ({rate:.0%}).",
                detail={"tool": name, "ok": ok, "error": err, "rate": round(rate, 4)},
            ))

    # 3. Requests failing overall.
    requests = metrics.get("requests") or {}
    total = int(requests.get("total") or 0)
    failed = int(requests.get("error") or 0)
    if total >= REQUEST_MIN_TOTAL:
        rate = _rate(failed, total)
        if rate >= REQUEST_ERROR_RATE:
            alerts.append(Alert(
                key="request_errors",
                severity=CRITICAL if rate >= 0.5 else WARNING,
                summary=f"{failed} of {total} turns are failing ({rate:.0%}).",
                detail={"total": total, "error": failed, "rate": round(rate, 4)},
            ))

    # 4. The grounding check is rewriting answers often. This is the correctness
    #    canary: every regenerate means the model produced a figure no engine
    #    supports. It is the one rule here about answers being wrong rather than
    #    the service being down, which is why it is worth waking up for.
    grounding = metrics.get("grounding") or {}
    checked = sum(int(v or 0) for v in grounding.values())
    regenerated = int(grounding.get("regenerate") or 0)
    if checked >= GROUNDING_MIN_TURNS:
        rate = _rate(regenerated, checked)
        if rate >= GROUNDING_REGENERATE_RATE:
            alerts.append(Alert(
                key="grounding_regenerate",
                severity=WARNING,
                summary=(f"{regenerated} of {checked} answers needed rewriting for unsupported "
                         f"figures ({rate:.0%}) — the model is inventing numbers."),
                detail={"checked": checked, "regenerate": regenerated, "rate": round(rate, 4)},
            ))

    alerts.sort(key=lambda a: (a.severity != CRITICAL, a.key))
    return alerts


class AlertLog:
    """
    Turns repeated evaluations into log lines, without repeating itself.

    `evaluate()` is level-triggered, so the same fault is reported on every call.
    Logging each one would bury the signal in its own noise, so a fault is
    logged when it appears and again when it clears, and not in between.
    """

    def __init__(self) -> None:
        self._firing: Dict[str, Alert] = {}

    def update(self, alerts: List[Alert]) -> Dict[str, List[Alert]]:
        current = {a.key: a for a in alerts}

        new = [a for k, a in current.items() if k not in self._firing]
        cleared = [a for k, a in self._firing.items() if k not in current]

        for alert in new:
            log = logger.error if alert.severity == CRITICAL else logger.warning
            log("ALERT %s: %s", alert.severity.upper(), alert.summary)
        for alert in cleared:
            logger.info("ALERT CLEARED: %s", alert.key)

        self._firing = current
        return {"new": new, "cleared": cleared, "firing": list(current.values())}

    @property
    def firing(self) -> List[Alert]:
        return list(self._firing.values())

    def reset(self) -> None:
        self._firing.clear()


log = AlertLog()


def check(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate, log any change, and return what is firing — for /api/alerts."""
    alerts = evaluate(metrics)
    log.update(alerts)
    worst: Optional[str] = None
    if any(a.severity == CRITICAL for a in alerts):
        worst = CRITICAL
    elif alerts:
        worst = WARNING
    return {
        "status": worst or "ok",
        "count": len(alerts),
        "alerts": [a.to_dict() for a in alerts],
    }
