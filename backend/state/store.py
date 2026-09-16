"""
Server-side conversation sessions for TORA.

A session is addressed by an unguessable, server-generated conversation id
(capability token). It stores the transcript, the user's financial memory
(FinancialProfile) and the ConversationState. The server — not the client —
is now the source of truth for history and memory, so a client can no longer
forge earlier turns to plant "verified" facts.

Storage is SQLite (stdlib) with a single guarded connection; ":memory:" is
supported for tests.
"""

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..context.financial import FinancialProfile
from .conversation_state import ConversationState

CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{24,64}$")
MAX_STORED_TURNS = 400
DEFAULT_TTL_DAYS = 30


def new_conversation_id() -> str:
    return secrets.token_urlsafe(24)


def is_valid_conversation_id(value: Optional[str]) -> bool:
    return bool(value) and bool(CONVERSATION_ID_RE.match(value))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ConversationSession:
    id: str
    turns: List[Dict[str, Any]] = field(default_factory=list)
    profile: FinancialProfile = field(default_factory=FinancialProfile)
    state: ConversationState = field(default_factory=ConversationState)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def history(self) -> List[Dict[str, str]]:
        return [{"role": t["role"], "content": t["content"]} for t in self.turns]

    def append_exchange(self, user_text: str, assistant_text: str, turn: int, meta: Optional[Dict[str, Any]] = None) -> None:
        ts = _now_iso()
        self.turns.append({"role": "user", "content": user_text, "turn": turn, "ts": ts})
        entry = {"role": "assistant", "content": assistant_text, "turn": turn, "ts": ts}
        if meta:
            entry["meta"] = meta
        self.turns.append(entry)
        if len(self.turns) > MAX_STORED_TURNS:
            self.turns = self.turns[-MAX_STORED_TURNS:]
        self.updated_at = ts

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id,
            "turns": self.turns,
            "profile": self.profile.to_dict(),
            "state": self.state.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }, ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "ConversationSession":
        d = json.loads(raw)
        return cls(
            id=d["id"],
            turns=d.get("turns", []),
            profile=FinancialProfile.from_dict(d.get("profile")),
            state=ConversationState.from_dict(d.get("state")),
            created_at=d.get("created_at", _now_iso()),
            updated_at=d.get("updated_at", _now_iso()),
        )


class SessionStore:
    """SQLite-backed session store."""

    def __init__(self, path: str, ttl_days: int = DEFAULT_TTL_DAYS):
        self.path = path
        self.ttl_seconds = max(0, ttl_days) * 86400
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._async_locks: Dict[str, Any] = {}
        self._saves = 0
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                " id TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL)"
            )
            self._conn.commit()
        self.purge_expired()

    def create(self) -> ConversationSession:
        session = ConversationSession(id=new_conversation_id())
        self.save(session)
        return session

    def get(self, conversation_id: str) -> Optional[ConversationSession]:
        if not is_valid_conversation_id(conversation_id):
            return None
        with self._lock:
            row = self._conn.execute("SELECT data, updated FROM sessions WHERE id = ?", (conversation_id,)).fetchone()
        if row is None:
            return None
        if self.ttl_seconds and time.time() - row[1] > self.ttl_seconds:
            self.delete(conversation_id)
            return None
        return ConversationSession.from_json(row[0])

    def save(self, session: ConversationSession) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, data, updated) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated = excluded.updated",
                (session.id, session.to_json(), time.time()),
            )
            self._conn.commit()
            self._saves += 1
        if self._saves % 100 == 0:
            self.purge_expired()

    def delete(self, conversation_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE id = ?", (conversation_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def purge_expired(self) -> int:
        if not self.ttl_seconds:
            return 0
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE updated < ?", (time.time() - self.ttl_seconds,))
            self._conn.commit()
            return cur.rowcount

    def lock_for(self, conversation_id: str):
        """Per-conversation asyncio lock so concurrent turns cannot interleave."""
        import asyncio

        lock = self._async_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._async_locks[conversation_id] = lock
            if len(self._async_locks) > 5000:
                for key in list(self._async_locks)[:1000]:
                    if not self._async_locks[key].locked():
                        del self._async_locks[key]
        return lock

    def close(self) -> None:
        with self._lock:
            self._conn.close()
