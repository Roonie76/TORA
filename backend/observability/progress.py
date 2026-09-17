"""
Live progress events for streaming replies (chat UI).

A request that wants live updates installs a sink (an asyncio.Queue) for the
duration of the turn; deep code calls `emit(...)` without knowing whether anyone
is listening. Events are small dicts: {"type": "stage"|"tool"|"token"|..., ...}.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any, Dict, Optional

_sink: contextvars.ContextVar[Optional[asyncio.Queue]] = contextvars.ContextVar("tora_progress", default=None)

STAGE_LABELS = {
    "understanding": "Understanding your question",
    "remembering": "Updating what I know about you",
    "planning": "Working out what to calculate",
    "calculating": "Running the numbers",
    "researching": "Checking sources",
    "reading_rules": "Looking up the rules",
    "reading_records": "Reading your Spendsy records",
    "writing": "Writing the answer",
    "checking": "Double-checking the figures",
}

TOOL_LABELS = {
    "calculator": "Calculator",
    "finance_calc": "Finance engine",
    "tax_calc": "Tax engine",
    "rules_lookup": "Rules library",
    "spendsy_data": "Spendsy records",
    "research": "Multi-source research",
    "web_search": "Web search",
    "web_fetch": "Web page",
}


def install(queue: Optional[asyncio.Queue]) -> contextvars.Token:
    return _sink.set(queue)


def uninstall(token: contextvars.Token) -> None:
    _sink.reset(token)


def active() -> bool:
    return _sink.get() is not None


def emit(event_type: str, **data: Any) -> None:
    queue = _sink.get()
    if queue is None:
        return
    event: Dict[str, Any] = {"type": event_type, **data}
    if event_type == "stage" and "label" not in event:
        event["label"] = STAGE_LABELS.get(data.get("stage", ""), data.get("stage", ""))
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:  # pragma: no cover - unbounded queues by default
        pass


async def emit_token(text: str) -> None:
    if text:
        emit("token", text=text)


def tool_stage(tool_name: str) -> str:
    return {"rules_lookup": "reading_rules", "spendsy_data": "reading_records", "research": "researching",
            "web_search": "researching", "web_fetch": "researching"}.get(tool_name, "calculating")
