"""
Identity for TORA requests (Phase 6A).

TORA does not issue accounts. It trusts the Spendsy auth service: the browser's
access token (``Authorization: Bearer`` or the ``access_token`` cookie) is checked
by calling ``{TORA_AUTH_URL}/me``. A successful answer yields an ``Identity``; the
token itself is kept only in memory for the duration of the request so tools can
read the user's own Spendsy data on their behalf.

Modes (``TORA_AUTH_MODE``):
  off       – ignore tokens; every request is anonymous (default, backwards compatible)
  optional  – anonymous allowed; a *supplied* token must be valid
  required  – every chat / conversation request needs a valid token
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple

import httpx

logger = logging.getLogger(__name__)

AUTH_MODES = ("off", "optional", "required")
DEFAULT_AUTH_URL = "http://localhost:8080/auth"
MAX_TOKEN_CHARS = 4096
_USER_ID_KEYS = ("id", "uid", "user_id", "userId", "sub")


class AuthError(Exception):
    """The supplied credentials were rejected."""


class AuthUnavailableError(Exception):
    """The auth service could not be reached or answered unexpectedly."""


@dataclass(frozen=True)
class Identity:
    user_id: str
    username: Optional[str] = None
    token: str = field(default="", repr=False, compare=False)

    @property
    def is_authenticated(self) -> bool:
        return bool(self.user_id)


def auth_mode() -> str:
    mode = os.getenv("TORA_AUTH_MODE", "off").strip().lower()
    return mode if mode in AUTH_MODES else "off"


def extract_token(headers: Mapping[str, str], cookies: Mapping[str, str]) -> Optional[str]:
    """Bearer header first, then the Spendsy ``access_token`` cookie."""
    raw = headers.get("authorization") or headers.get("Authorization") or ""
    token = ""
    if raw:
        scheme, _, value = raw.strip().partition(" ")
        token = value.strip() if scheme.lower() == "bearer" else ""
    if not token:
        token = (cookies.get("access_token") or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
    if not token or len(token) > MAX_TOKEN_CHARS or any(ch.isspace() for ch in token):
        return None
    return token


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _user_from_payload(payload: Any) -> Tuple[str, Optional[str]]:
    """Accept {id,...}, {user:{...}} and the Spendsy envelope {ok, data:{...}}."""
    if isinstance(payload, dict):
        for key in ("data", "user"):
            inner = payload.get(key)
            if isinstance(inner, dict) and any(k in inner for k in _USER_ID_KEYS + ("user",)):
                return _user_from_payload(inner)
        for key in _USER_ID_KEYS:
            value = payload.get(key)
            if value not in (None, ""):
                username = payload.get("username") or payload.get("name") or payload.get("email")
                return str(value), (str(username) if username else None)
    raise AuthUnavailableError("Auth service response did not contain a user id.")


class AuthVerifier(Protocol):
    async def verify(self, token: str) -> Identity: ...


class GatewayAuthVerifier:
    """Checks tokens against the Spendsy auth service, with a short positive cache."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        cache_seconds: Optional[float] = None,
        timeout_seconds: float = 5.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.base_url = (base_url or os.getenv("TORA_AUTH_URL") or DEFAULT_AUTH_URL).rstrip("/")
        if cache_seconds is None:
            try:
                cache_seconds = float(os.getenv("TORA_AUTH_CACHE_SECONDS", "60"))
            except ValueError:
                cache_seconds = 60.0
        self.cache_seconds = max(0.0, cache_seconds)
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        self._cache: Dict[str, Tuple[float, str, Optional[str]]] = {}
        self._lock = threading.Lock()

    def _cached(self, key: str) -> Optional[Tuple[str, Optional[str]]]:
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1], hit[2]
            if hit:
                self._cache.pop(key, None)
        return None

    def _remember(self, key: str, user_id: str, username: Optional[str]) -> None:
        if not self.cache_seconds:
            return
        with self._lock:
            if len(self._cache) > 10000:
                self._cache.clear()
            self._cache[key] = (time.monotonic() + self.cache_seconds, user_id, username)

    def forget(self, token: str) -> None:
        with self._lock:
            self._cache.pop(_token_key(token), None)

    async def verify(self, token: str) -> Identity:
        key = _token_key(token)
        cached = self._cached(key)
        if cached:
            return Identity(user_id=cached[0], username=cached[1], token=token)
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, transport=self._transport, follow_redirects=False
            ) as client:
                resp = await client.get(f"{self.base_url}/me", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            logger.warning("Auth service unreachable: %s", type(exc).__name__)
            raise AuthUnavailableError("Sign-in service is unavailable.") from exc
        if resp.status_code in (401, 403):
            raise AuthError("Your session has expired. Please sign in again.")
        if resp.status_code != 200:
            raise AuthUnavailableError(f"Sign-in service returned HTTP {resp.status_code}.")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise AuthUnavailableError("Sign-in service returned invalid JSON.") from exc
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise AuthError("Your session has expired. Please sign in again.")
        user_id, username = _user_from_payload(payload)
        self._remember(key, user_id, username)
        return Identity(user_id=user_id, username=username, token=token)


class StaticAuthVerifier:
    """Test / offline verifier: a fixed token → user map."""

    def __init__(self, users: Dict[str, str]):
        self.users = dict(users)

    async def verify(self, token: str) -> Identity:
        user_id = self.users.get(token)
        if not user_id:
            raise AuthError("Your session has expired. Please sign in again.")
        return Identity(user_id=user_id, username=user_id, token=token)


_current: contextvars.ContextVar[Optional[Identity]] = contextvars.ContextVar("tora_identity", default=None)


def current_identity() -> Optional[Identity]:
    return _current.get()


def set_current_identity(identity: Optional[Identity]) -> contextvars.Token:
    return _current.set(identity)


def reset_current_identity(token: contextvars.Token) -> None:
    _current.reset(token)
