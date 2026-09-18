import os

# Tests never touch the on-disk session database.
os.environ.setdefault("TORA_SESSION_DB", ":memory:")
# Most agent tests script the planner's reply; the Phase 7 fast path has its own tests.
os.environ.setdefault("TORA_FAST_PATH", "off")
# Locked slots spend a model call on a rewrite, which tests that script a fixed sequence of
# answers would mis-count; test_locked_slots.py and the offline evals exercise it turned on.
os.environ.setdefault("TORA_LOCKED_SLOTS", "off")

import pytest


@pytest.fixture(autouse=True)
def _reset_chat_rate_limiter():
    """Keep the per-client chat rate limiter from leaking state between tests."""
    try:
        from backend.main import rate_limiter
    except Exception:  # pragma: no cover - main import failures surface in their own tests
        yield
        return
    rate_limiter.reset()
    yield
    rate_limiter.reset()
