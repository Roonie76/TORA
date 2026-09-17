"""
Training-data hooks (Phase 13) — OFF unless TORA_TRAINING_LOG is set.

When enabled, each successful turn is appended as one JSON line with identifiers
masked (PAN, account numbers, emails, phones) and the conversation id replaced by
a short hash. Users can rate answers (POST /api/feedback); ratings and corrected
answers are logged next to the turns. `python -m backend.observability.training_log
export` joins them into fine-tuning records (SFT for good answers, preference
pairs where a correction exists).

Only enable this with the users' consent and a data-retention policy.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .trace import conversation_ref

_lock = threading.Lock()
MAX_TOOL_CHARS = 4000


# Words people naturally use to switch the log off. Without this, TORA_TRAINING_LOG="off"
# would be taken as a file name and a file called "off" would quietly collect turns.
DISABLED_VALUES = {"", "0", "off", "no", "false", "none", "disabled"}


def log_path() -> Optional[str]:
    path = os.getenv("TORA_TRAINING_LOG", "").strip()
    return None if path.lower() in DISABLED_VALUES else path


def _mask(text: Optional[str]) -> str:
    from ..documents import mask_identifiers

    return mask_identifiers(text or "")


def _append(record: Dict[str, Any]) -> None:
    path = log_path()
    if not path:
        return
    record["ts"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def record_turn(conversation_id: Optional[str], turn: Optional[int], message: str, response: Any,
                history: Optional[List[Dict[str, str]]] = None) -> None:
    if not log_path():
        return
    plan = getattr(response, "plan", None)
    tools = []
    ctx = getattr(response, "tool_context", None)
    for r in (ctx.results if ctx else []):
        out = json.dumps(r.output, ensure_ascii=False, default=str)
        tools.append({"tool": r.tool_name, "ok": not r.is_error, "output": _mask(out[:MAX_TOOL_CHARS])})
    intent = getattr(response, "intent", None)
    complexity = getattr(response, "complexity", None)
    _append({
        "type": "turn",
        "conversation": conversation_ref(conversation_id) if conversation_id else None,
        "turn": turn,
        "history": [{"role": h["role"], "content": _mask(h["content"])[:2000]} for h in (history or [])[-6:]],
        "message": _mask(message),
        "intent": intent.intent.value if intent is not None else None,
        "complexity": getattr(complexity, "level", None),
        "plan": [{"tool": s.tool_name, "arguments": s.arguments} for s in (plan.steps if plan else [])],
        "plan_source": ("fast_path" if plan is not None and (plan.thought or "").startswith("fast path")
                        else ("planner" if plan is not None else None)),
        "tools": tools,
        "answer": _mask(getattr(response, "content", "")),
        "grounding": getattr(response, "grounding", None) and {
            "action": response.grounding.get("action"), "checked": response.grounding.get("checked")},
        "model": getattr(response, "model", None),
    })


def record_feedback(conversation_id: str, turn: int, rating: str, comment: Optional[str] = None,
                    better_answer: Optional[str] = None) -> None:
    _append({
        "type": "feedback",
        "conversation": conversation_ref(conversation_id),
        "turn": turn,
        "rating": rating,
        "comment": _mask(comment)[:1000] if comment else None,
        "better_answer": _mask(better_answer)[:4000] if better_answer else None,
    })


def export(path: str, out_path: str, only_rated: bool = False) -> Dict[str, int]:
    turns: Dict[tuple, Dict[str, Any]] = {}
    feedback: Dict[tuple, Dict[str, Any]] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            key = (rec.get("conversation"), rec.get("turn"))
            if rec.get("type") == "turn":
                turns[key] = rec
            elif rec.get("type") == "feedback":
                feedback[key] = rec
    counts = {"sft": 0, "preference": 0, "skipped": 0}
    with open(out_path, "w", encoding="utf-8") as out:
        for key, t in turns.items():
            fb = feedback.get(key)
            if only_rated and fb is None:
                counts["skipped"] += 1
                continue
            messages = t["history"] + [{"role": "user", "content": t["message"]}]
            base = {"messages": messages, "plan": t["plan"], "tools": t["tools"], "intent": t["intent"]}
            if fb and fb.get("better_answer"):
                out.write(json.dumps({**base, "kind": "preference", "chosen": fb["better_answer"],
                                      "rejected": t["answer"]}, ensure_ascii=False) + "\n")
                counts["preference"] += 1
            elif fb is None or fb.get("rating") == "up":
                if t.get("grounding") and t["grounding"].get("action") not in (None, "none"):
                    counts["skipped"] += 1   # only clean, verified answers become SFT targets
                    continue
                out.write(json.dumps({**base, "kind": "sft", "answer": t["answer"],
                                      "rated": fb is not None}, ensure_ascii=False) + "\n")
                counts["sft"] += 1
            else:
                counts["skipped"] += 1
    return counts


def _main(argv: List[str]) -> int:
    if len(argv) >= 2 and argv[0] == "export":
        src = os.getenv("TORA_TRAINING_LOG") or (argv[3] if len(argv) > 3 else "")
        out = argv[1]
        only = "--only-rated" in argv
        path = next((a for a in argv[2:] if not a.startswith("--")), src)
        if not path:
            print("Usage: python -m backend.observability.training_log export OUT.jsonl [LOG.jsonl] [--only-rated]")
            return 2
        print(json.dumps(export(path, out, only_rated=only)))
        return 0
    print("Usage: python -m backend.observability.training_log export OUT.jsonl [LOG.jsonl] [--only-rated]")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
