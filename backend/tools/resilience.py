"""
One flaky call should not lose a turn — and a tool that is down should not cost every turn.

On this box a turn already costs minutes, so a tool failure is expensive twice over: the answer
is worse, and the user waited for it. But the two failure shapes want opposite handling.

A *transient* failure — a timeout, a refused connection, a 502 from a bank's site — is worth one
more try; the second attempt usually works. A *permanent* one — bad arguments, an unknown
operation, a tool that is not registered — will fail identically forever, and retrying it just
spends the user's time to reach the same answer.

And when a tool keeps failing, retrying makes things worse: every turn pays the timeout again.
So after a few consecutive failures the breaker opens and calls fail immediately, with the answer
saying the source was unavailable rather than silently going without it. One success closes it.

Only read-only tools are retried, which is all of them: nothing here writes, so a second attempt
cannot double an effect.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

# Worth trying again: the call did not get a real answer.
_TRANSIENT = re.compile(
    r"timed?\s*out|timeout|connection\s+(?:reset|refused|aborted|error)|temporarily\s+unavailable|"
    r"service\s+unavailable|bad\s+gateway|gateway\s+time|too\s+many\s+requests|rate\s*limit|"
    r"\b(?:429|500|502|503|504)\b|network|unreachable|ssl|eof\s+occurred|broken\s+pipe",
    re.IGNORECASE,
)
# Never worth trying again: the call was answered, and the answer was "no".
_PERMANENT = re.compile(
    r"not\s+found\s+in\s+registry|must\s+be\s+a\s+non-empty|unknown\s+operation|invalid\s+|"
    r"missing\s+required|validation|unsupported|not\s+signed\s+in|unauthor|forbidden|\b(?:400|401|403|404|422)\b",
    re.IGNORECASE,
)


def is_transient(error: Optional[str]) -> bool:
    """True when trying the same call again might reasonably produce a different result."""
    text = str(error or "")
    if not text.strip():
        return False
    if _PERMANENT.search(text):
        return False
    return bool(_TRANSIENT.search(text))


def retries() -> int:
    """Extra attempts after the first (TORA_TOOL_RETRIES). Kept at 1: on a box where a turn takes
    minutes, a second timeout costs more than the missing source usually does."""
    try:
        return max(0, min(3, int(os.getenv("TORA_TOOL_RETRIES", "1").strip() or 0)))
    except ValueError:
        return 1


def breaker_threshold() -> int:
    try:
        return max(1, int(os.getenv("TORA_TOOL_BREAKER_FAILURES", "3").strip() or 3))
    except ValueError:
        return 3


def breaker_cooldown() -> float:
    try:
        return max(0.0, float(os.getenv("TORA_TOOL_BREAKER_SECONDS", "120").strip() or 120))
    except ValueError:
        return 120.0


@dataclass
class _State:
    consecutive_failures: int = 0
    opened_at: Optional[float] = None


@dataclass
class CircuitBreaker:
    """Per-tool. Opens after repeated consecutive failures, closes on the first success after."""

    clock: object = time.monotonic
    _tools: Dict[str, _State] = field(default_factory=dict)

    def _state(self, tool: str) -> _State:
        return self._tools.setdefault(tool, _State())

    def is_open(self, tool: str) -> bool:
        state = self._state(tool)
        if state.opened_at is None:
            return False
        if self.clock() - state.opened_at >= breaker_cooldown():
            state.opened_at = None               # cooled down: let one call through
            state.consecutive_failures = 0
            return False
        return True

    def record_success(self, tool: str) -> None:
        self._tools[tool] = _State()

    def record_failure(self, tool: str) -> None:
        state = self._state(tool)
        state.consecutive_failures += 1
        if state.consecutive_failures >= breaker_threshold() and state.opened_at is None:
            state.opened_at = self.clock()

    def seconds_remaining(self, tool: str) -> float:
        state = self._state(tool)
        if state.opened_at is None:
            return 0.0
        return max(0.0, breaker_cooldown() - (self.clock() - state.opened_at))

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        """For /api/metrics: which tools are failing, and which are being held open."""
        return {
            name: {"consecutive_failures": s.consecutive_failures, "open": s.opened_at is not None}
            for name, s in self._tools.items() if s.consecutive_failures or s.opened_at is not None
        }
