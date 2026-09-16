import os

# Tests never touch the on-disk session database.
os.environ.setdefault("TORA_SESSION_DB", ":memory:")

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
