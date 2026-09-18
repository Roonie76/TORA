"""A flaky tool should not lose a turn, and a dead tool should not cost every turn.

On this box a turn costs minutes, so a tool timeout is paid in the user's time. One retry for a
transient failure; none for a failure that will repeat; and a tool that keeps failing is skipped
until it has had time to recover.
"""
import asyncio

import pytest

from backend.tools.base import BaseTool, ToolResult
from backend.tools.executor import ToolExecutor
from backend.tools.registry import ToolRegistry
from backend.tools.resilience import CircuitBreaker, is_transient


class Flaky(BaseTool):
    """Fails for the first `fail_times` calls, then succeeds."""

    name = "flaky"
    description = "test tool"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, fail_times=1, error="Connection refused"):
        super().__init__()
        self.fail_times, self.error, self.calls = fail_times, error, 0

    async def execute(self, **kwargs):          # unused: this tool overrides run() directly
        return {}

    async def run(self, args=None, call_id=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            return ToolResult(tool_name=self.name, success=False, error=self.error, call_id=call_id)
        return ToolResult(tool_name=self.name, success=True, data={"ok": True}, call_id=call_id)


def executor_for(tool):
    registry = ToolRegistry()
    registry.register(tool)
    return ToolExecutor(registry=registry)


class TestRetries:
    def test_a_transient_failure_is_tried_once_more(self):
        tool = Flaky(fail_times=1, error="Tool execution timed out after 30 seconds.")
        result = asyncio.run(executor_for(tool).execute("flaky"))
        assert result.success and tool.calls == 2
        assert result.metadata["attempts"] == 2

    def test_a_permanent_failure_is_not_retried(self):
        tool = Flaky(fail_times=1, error="missing required parameter principal")
        result = asyncio.run(executor_for(tool).execute("flaky"))
        assert not result.success and tool.calls == 1

    def test_retries_are_bounded(self, monkeypatch):
        monkeypatch.setenv("TORA_TOOL_RETRIES", "1")
        tool = Flaky(fail_times=99, error="Connection refused")
        result = asyncio.run(executor_for(tool).execute("flaky"))
        assert not result.success and tool.calls == 2

    def test_retries_can_be_turned_off(self, monkeypatch):
        monkeypatch.setenv("TORA_TOOL_RETRIES", "0")
        tool = Flaky(fail_times=99, error="Connection refused")
        asyncio.run(executor_for(tool).execute("flaky"))
        assert tool.calls == 1


class TestTransientClassification:
    @pytest.mark.parametrize("error", [
        "Tool execution timed out after 30 seconds.", "Connection refused",
        "HTTP 503 from upstream", "rate limit exceeded (429)", "network unreachable",
    ])
    def test_worth_another_try(self, error):
        assert is_transient(error)

    @pytest.mark.parametrize("error", [
        "Tool 'x' not found in registry. Available tools: []", "invalid operation: foo",
        "missing required parameter principal", "HTTP 404 Not Found", "not signed in", "",
    ])
    def test_not_worth_another_try(self, error):
        assert not is_transient(error)


class TestCircuitBreaker:
    def test_it_opens_after_repeated_failures_and_says_so(self, monkeypatch):
        monkeypatch.setenv("TORA_TOOL_RETRIES", "0")
        monkeypatch.setenv("TORA_TOOL_BREAKER_FAILURES", "2")
        tool = Flaky(fail_times=99, error="Connection refused")
        ex = executor_for(tool)
        for _ in range(2):
            asyncio.run(ex.execute("flaky"))
        assert tool.calls == 2
        blocked = asyncio.run(ex.execute("flaky"))
        assert not blocked.success and blocked.metadata["circuit_open"] is True
        assert "source was unavailable" in blocked.error
        assert tool.calls == 2, "an open breaker must not call the tool at all"

    def test_it_closes_again_after_the_cooldown(self, monkeypatch):
        monkeypatch.setenv("TORA_TOOL_RETRIES", "0")
        monkeypatch.setenv("TORA_TOOL_BREAKER_FAILURES", "2")
        monkeypatch.setenv("TORA_TOOL_BREAKER_SECONDS", "60")
        now = [1000.0]
        breaker = CircuitBreaker(clock=lambda: now[0])
        breaker.record_failure("flaky")
        breaker.record_failure("flaky")
        assert breaker.is_open("flaky")
        now[0] += 61
        assert not breaker.is_open("flaky")

    def test_a_success_clears_the_count(self, monkeypatch):
        monkeypatch.setenv("TORA_TOOL_BREAKER_FAILURES", "2")
        breaker = CircuitBreaker()
        breaker.record_failure("flaky")
        breaker.record_success("flaky")
        breaker.record_failure("flaky")
        assert not breaker.is_open("flaky"), "failures either side of a success are not consecutive"

    def test_one_tool_failing_does_not_block_another(self, monkeypatch):
        monkeypatch.setenv("TORA_TOOL_BREAKER_FAILURES", "1")
        breaker = CircuitBreaker()
        breaker.record_failure("research")
        assert breaker.is_open("research") and not breaker.is_open("finance_calc")

    def test_the_snapshot_names_what_is_failing(self, monkeypatch):
        monkeypatch.setenv("TORA_TOOL_BREAKER_FAILURES", "1")
        breaker = CircuitBreaker()
        breaker.record_failure("research")
        assert breaker.snapshot() == {"research": {"consecutive_failures": 1, "open": True}}


def test_failing_tools_are_visible_in_metrics(monkeypatch):
    """A tool quietly failing every turn just makes the answers worse; /api/metrics names it."""
    monkeypatch.setenv("TORA_TOOL_BREAKER_FAILURES", "2")
    from fastapi.testclient import TestClient

    from backend.main import app, tool_executor

    tool_executor.breaker.record_success("research")          # start from clean
    with TestClient(app) as client:
        assert "failing_tools" not in client.get("/api/metrics").json()
        tool_executor.breaker.record_failure("research")
        tool_executor.breaker.record_failure("research")
        body = client.get("/api/metrics").json()
        assert body["failing_tools"]["research"] == {"consecutive_failures": 2, "open": True}
    tool_executor.breaker.record_success("research")          # leave no state behind
