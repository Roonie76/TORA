"""
Background turns: accept a slow question, answer it without holding the connection.

On this hardware a Tier 2 question (open-ended advice, no engine behind it) costs
minutes, almost all of it decode at 4.1 tokens/sec. `/api/chat/stream` answers
those by holding an HTTP connection open for the whole turn, and its docstring is
honest about the consequence: closing the connection stops the turn and nothing is
saved. A phone that sleeps, a proxy with an idle timeout, a switched tab or a lift
with no signal therefore throws away minutes of work that was nearly finished.

Here the turn is accepted, given an id, and run in the background. It finishes and
is saved whether or not anyone is listening, and a client may attach late, detach,
and attach again without missing anything.

What a late joiner needs is the *state* of the answer, not the history of how it
was typed, so the buffer is not a log of every event:

  - token events are coalesced into one running string, and a `replace` event
    (the regenerate-after-grounding path) resets it, exactly as it does in the UI
  - every other event -- stage, tool, complexity -- is kept in order

So attaching replays one snapshot plus a short list, rather than thousands of
token events, and a turn's memory cost is bounded by the length of its answer
rather than by how long anyone stayed connected.

Nothing here is a queue or a worker pool: turns run in the event loop that
accepted them, which is the same place `/api/chat/stream` already runs them.
This changes who waits, not how the work is done.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# A turn's answer is the only thing that grows without bound, so it is capped.
# 60k characters is far beyond any answer TORA writes (the longest measured is a
# debt plan at ~2.5k) and still small enough that a full registry cannot exhaust
# memory on an 8 GB box.
MAX_ANSWER_CHARS = int(os.getenv("TORA_TURN_MAX_ANSWER_CHARS", "60000"))

# Non-token events are small and few (a dozen or so per turn); the cap only
# exists so a pathological tool loop cannot grow the buffer forever.
MAX_EVENTS = int(os.getenv("TORA_TURN_MAX_EVENTS", "400"))

# How long a finished turn stays readable. Long enough that a user who closed
# their laptop mid-answer can come back for it; short enough to bound memory.
TURN_TTL_SECONDS = float(os.getenv("TORA_TURN_TTL_SECONDS", "1800"))

# Hard ceiling on retained turns, oldest finished evicted first.
MAX_TURNS = int(os.getenv("TORA_TURN_MAX_RETAINED", "200"))

RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"

TERMINAL = (DONE, ERROR, CANCELLED)


@dataclass
class Turn:
    """One background turn. Owned by `owner_key`; nobody else may read it."""

    turn_id: str
    owner_key: str
    conversation_id: Optional[str] = None
    status: str = RUNNING
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    # The answer as it stands, rebuilt from token/replace events.
    text: str = ""
    truncated: bool = False

    # Everything that is not a token, in order.
    events: List[Dict[str, Any]] = field(default_factory=list)
    dropped_events: int = 0

    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None

    _listeners: List[asyncio.Queue] = field(default_factory=list, repr=False)
    _task: Optional[asyncio.Task] = field(default=None, repr=False)

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL

    def snapshot(self, include_result: bool = True) -> Dict[str, Any]:
        """Everything a client needs to render this turn right now."""
        data: Dict[str, Any] = {
            "turn_id": self.turn_id,
            "status": self.status,
            "conversation_id": self.conversation_id,
            "text": self.text,
            "events": list(self.events),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round((self.finished_at or time.time()) - self.created_at, 3),
        }
        if self.truncated:
            data["truncated"] = True
        if self.dropped_events:
            data["dropped_events"] = self.dropped_events
        if include_result and self.result is not None:
            data["result"] = self.result
        if self.error is not None:
            data["error"] = self.error
        return data


class TurnRegistry:
    """
    In-process registry of background turns.

    Deliberately in-process and not durable: a restart loses running turns, which
    is the same guarantee `/api/chat/stream` already gives, and the conversation
    itself is persisted by the normal path once the turn completes. Making turns
    survive a restart needs a real queue, and that is a different decision from
    this one.
    """

    def __init__(self) -> None:
        self._turns: Dict[str, Turn] = {}

    # -- lifecycle ---------------------------------------------------------

    def create(self, owner_key: str, conversation_id: Optional[str] = None) -> Turn:
        self._evict()
        turn = Turn(turn_id=uuid.uuid4().hex, owner_key=owner_key, conversation_id=conversation_id)
        self._turns[turn.turn_id] = turn
        return turn

    def get(self, turn_id: str, owner_key: str) -> Optional[Turn]:
        """
        Fetch a turn, but only for its owner.

        Returns None both for "no such turn" and "not yours", so a caller cannot
        use this to discover that someone else's turn id exists.
        """
        turn = self._turns.get(turn_id)
        if turn is None or turn.owner_key != owner_key:
            return None
        return turn

    def publish(self, turn: Turn, event: Dict[str, Any]) -> None:
        """Fold one progress event into the turn's state and fan it out live."""
        kind = event.get("type")

        if kind == "token":
            text = event.get("text") or ""
            if text:
                room = MAX_ANSWER_CHARS - len(turn.text)
                if room > 0:
                    turn.text += text[:room]
                if len(text) > max(room, 0):
                    turn.truncated = True
        elif kind == "replace":
            # The grounding path rewrites the whole answer; the UI replaces, so
            # the buffer must replace too or a late joiner sees both versions.
            turn.text = (event.get("text") or "")[:MAX_ANSWER_CHARS]
            turn.truncated = len(event.get("text") or "") > MAX_ANSWER_CHARS
            turn.events.append(event)
        else:
            if len(turn.events) < MAX_EVENTS:
                turn.events.append(event)
            else:
                turn.dropped_events += 1

        self._fan_out(turn, event)

    def finish(self, turn: Turn, result: Dict[str, Any]) -> None:
        turn.status = DONE
        turn.result = result
        turn.finished_at = time.time()
        answer = result.get("response")
        if isinstance(answer, str) and answer:
            # The saved answer is authoritative over anything reconstructed
            # from tokens -- it is what the conversation actually stored.
            turn.text = answer[:MAX_ANSWER_CHARS]
        self._fan_out(turn, {"type": "final", **result}, close=True)

    def fail(self, turn: Turn, status: int, detail: str) -> None:
        turn.status = ERROR
        turn.error = {"status": status, "detail": detail}
        turn.finished_at = time.time()
        self._fan_out(turn, {"type": "error", "status": status, "detail": detail}, close=True)

    def cancel(self, turn: Turn) -> bool:
        """Stop a running turn. Returns False if it had already finished."""
        if turn.finished:
            return False
        if turn._task is not None and not turn._task.done():
            turn._task.cancel()
        turn.status = CANCELLED
        turn.finished_at = time.time()
        self._fan_out(turn, {"type": "cancelled", "turn_id": turn.turn_id}, close=True)
        return True

    # -- listeners ---------------------------------------------------------

    def attach(self, turn: Turn) -> asyncio.Queue:
        """
        Subscribe to a turn's live events.

        The caller is responsible for replaying `turn.snapshot()` first; this
        only carries what happens from now on.
        """
        queue: asyncio.Queue = asyncio.Queue()
        if turn.finished:
            queue.put_nowait(None)
        else:
            turn._listeners.append(queue)
        return queue

    def detach(self, turn: Turn, queue: asyncio.Queue) -> None:
        try:
            turn._listeners.remove(queue)
        except ValueError:
            pass

    def _fan_out(self, turn: Turn, event: Dict[str, Any], close: bool = False) -> None:
        for queue in list(turn._listeners):
            try:
                queue.put_nowait(event)
                if close:
                    queue.put_nowait(None)
            except asyncio.QueueFull:  # pragma: no cover - unbounded queues
                pass
        if close:
            turn._listeners.clear()

    # -- housekeeping ------------------------------------------------------

    def _evict(self) -> None:
        now = time.time()
        stale = [
            t.turn_id
            for t in self._turns.values()
            if t.finished and t.finished_at is not None and (now - t.finished_at) > TURN_TTL_SECONDS
        ]
        for turn_id in stale:
            self._turns.pop(turn_id, None)

        if len(self._turns) <= MAX_TURNS:
            return
        # Over the ceiling: drop finished turns oldest-first. A running turn is
        # never evicted -- losing one would mean throwing away work in progress,
        # which is the exact failure this module exists to prevent.
        finished = sorted(
            (t for t in self._turns.values() if t.finished),
            key=lambda t: t.finished_at or t.created_at,
        )
        for turn in finished:
            if len(self._turns) <= MAX_TURNS:
                break
            self._turns.pop(turn.turn_id, None)

    def stats(self) -> Dict[str, int]:
        running = sum(1 for t in self._turns.values() if t.status == RUNNING)
        return {
            "retained": len(self._turns),
            "running": running,
            "finished": len(self._turns) - running,
        }

    def clear(self) -> None:
        """Test hook: drop everything."""
        self._turns.clear()


class TurnSink:
    """
    A progress sink that writes straight into a turn.

    `progress.emit` only ever calls `put_nowait`, so this stands in for the
    asyncio.Queue the streaming endpoint installs. Folding events into the turn
    synchronously -- rather than through a second queue drained by another task --
    means there is no window in which the turn can be marked finished while
    events it produced are still in flight behind it.
    """

    __slots__ = ("_registry", "_turn")

    def __init__(self, registry: TurnRegistry, turn: Turn) -> None:
        self._registry = registry
        self._turn = turn

    def put_nowait(self, event: Dict[str, Any]) -> None:
        if not self._turn.finished:
            self._registry.publish(self._turn, event)


registry = TurnRegistry()
