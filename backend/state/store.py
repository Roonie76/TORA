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
    owner_id: Optional[str] = None  # Spendsy user id (Phase 6A); None = anonymous

    def can_access(self, user_id: Optional[str]) -> bool:
        """Owned conversations are private to their owner; anonymous ones are id-addressed."""
        return self.owner_id is None or self.owner_id == user_id

    def title(self) -> str:
        for t in self.turns:
            if t.get("role") == "user":
                text = " ".join(str(t.get("content", "")).split())
                return text[:60] + ("…" if len(text) > 60 else "")
        return "New conversation"

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
            "owner_id": self.owner_id,
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
            owner_id=d.get("owner_id"),
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
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(sessions)")}
            if "owner_id" not in cols:  # Phase 6A migration
                self._conn.execute("ALTER TABLE sessions ADD COLUMN owner_id TEXT")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_id, updated)")
            # Per-account financial memory shared by all of a user's conversations.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS user_profiles ("
                " user_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL)"
            )
            self._conn.commit()
        self.purge_expired()

    def create(self, owner_id: Optional[str] = None) -> ConversationSession:
        session = ConversationSession(id=new_conversation_id(), owner_id=owner_id)
        if owner_id:
            profile = self.get_user_profile(owner_id)
            if profile is not None:
                session.profile = profile
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
                "INSERT INTO sessions (id, data, updated, owner_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated = excluded.updated, "
                "owner_id = excluded.owner_id",
                (session.id, session.to_json(), time.time(), session.owner_id),
            )
            if session.owner_id:
                self._conn.execute(
                    "INSERT INTO user_profiles (user_id, data, updated) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated = excluded.updated",
                    (session.owner_id, json.dumps(session.profile.to_dict(), ensure_ascii=False, default=str), time.time()),
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

    # ── Per-user data (Phase 6A) ──────────────────────────────────────────
    def list_for_owner(self, owner_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._lock:
            rows = self._conn.execute(
                "SELECT data, updated FROM sessions WHERE owner_id = ? ORDER BY updated DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        out = []
        for raw, updated in rows:
            if self.ttl_seconds and time.time() - updated > self.ttl_seconds:
                continue
            session = ConversationSession.from_json(raw)
            out.append({
                "conversation_id": session.id,
                "title": session.title(),
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "turns": len(session.turns) // 2,
            })
        return out

    def get_user_profile(self, user_id: str) -> Optional[FinancialProfile]:
        with self._lock:
            row = self._conn.execute("SELECT data FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
        return FinancialProfile.from_dict(json.loads(row[0])) if row else None

    def save_user_profile(self, user_id: str, profile: FinancialProfile) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO user_profiles (user_id, data, updated) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated = excluded.updated",
                (user_id, json.dumps(profile.to_dict(), ensure_ascii=False, default=str), time.time()),
            )
            self._conn.commit()

    def delete_user_data(self, user_id: str) -> int:
        """Delete every conversation and the saved memory of one account."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE owner_id = ?", (user_id,))
            self._conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
            self._conn.commit()
            return cur.rowcount

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
