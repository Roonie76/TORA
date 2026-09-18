import time
import asyncio
from typing import Optional, Dict, Any

from .base import ToolResult
from .registry import ToolRegistry
from ..context.tools import ToolContext
from .resilience import CircuitBreaker, is_transient, retries


class ToolExecutor:
    """
    Executes pre-selected tools safely against a ToolRegistry.
    Provider-agnostic execution layer responsible for:
    1. Tool name validation & lookup in ToolRegistry
    2. Input argument schema validation (via registered Tool)
    3. Safe async execution with optional timeout control
    4. Execution duration tracking using monotonic timing
    5. Structured error containment without tracebacks or implementation leaks
    6. Normalizing outputs into ToolResult and bridgeable ToolContext
    """

    def __init__(
        self,
        registry: ToolRegistry,
        default_timeout_seconds: Optional[float] = None,
    ):
        if not isinstance(registry, ToolRegistry):
            raise TypeError(f"Expected ToolRegistry instance, got {type(registry).__name__}")
        self.registry = registry
        self.default_timeout_seconds = default_timeout_seconds
        # Shared across calls for the life of the process: a tool that is down stays known to be
        # down, so the next turn does not pay its timeout again.
        self.breaker = CircuitBreaker()

    async def execute(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        call_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> ToolResult:
        """Run a tool, retrying a transient failure once and refusing a tool that keeps failing.

        A timeout or a refused connection gets another attempt; bad arguments or an unknown
        operation do not, because the second attempt has the same answer waiting. A tool that has
        failed repeatedly is skipped outright until its cooldown passes — on a box where a turn
        costs minutes, paying a dead source's timeout every turn is the expensive mistake.
        """
        clean_name = tool_name.strip() if isinstance(tool_name, str) else str(tool_name)
        if self.breaker.is_open(clean_name):
            wait = int(self.breaker.seconds_remaining(clean_name))
            return ToolResult(
                tool_name=clean_name,
                success=False,
                error=(f"'{clean_name}' has failed repeatedly and is being skipped for another "
                       f"{wait}s. Answer without it and say the source was unavailable."),
                call_id=call_id,
                metadata={"circuit_open": True, "retry_after_seconds": wait},
            )

        attempts = retries() + 1
        result = await self._attempt(clean_name, arguments, call_id, timeout_seconds)
        tried = 1
        while not result.success and tried < attempts and is_transient(result.error):
            result = await self._attempt(clean_name, arguments, call_id, timeout_seconds)
            tried += 1

        if result.success:
            self.breaker.record_success(clean_name)
        else:
            self.breaker.record_failure(clean_name)
        if tried > 1:
            meta = dict(result.metadata or {})
            meta["attempts"] = tried
            result = ToolResult(tool_name=result.tool_name, success=result.success, data=result.data,
                                error=result.error, call_id=result.call_id, metadata=meta)
        return result

    async def _attempt(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        call_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> ToolResult:
        """
        One attempt: name validation, argument validation, timeout protection, error containment.

        :param tool_name: Name of the tool registered in ToolRegistry.
        :param arguments: Dictionary of input parameters for the tool.
        :param call_id: Optional tracking identifier for this tool call.
        :param timeout_seconds: Optional per-call timeout override in seconds.
        :return: Structured ToolResult.
        """
        if not isinstance(tool_name, str) or not tool_name.strip():
            return ToolResult(
                tool_name=str(tool_name),
                success=False,
                error="Tool name must be a non-empty string.",
                call_id=call_id,
            )

        clean_name = tool_name.strip()
        tool = self.registry.get(clean_name)
        if tool is None:
            return ToolResult(
                tool_name=clean_name,
                success=False,
                error=f"Tool '{clean_name}' not found in registry. Available tools: {self.registry.list_names()}",
                call_id=call_id,
            )

        timeout = timeout_seconds if timeout_seconds is not None else self.default_timeout_seconds
        start_time = time.monotonic()

        try:
            if timeout is not None and timeout > 0:
                result = await asyncio.wait_for(
                    tool.run(args=arguments, call_id=call_id),
                    timeout=timeout,
                )
            else:
                result = await tool.run(args=arguments, call_id=call_id)
        except asyncio.TimeoutError:
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            return ToolResult(
                tool_name=clean_name,
                success=False,
                error=f"Tool '{clean_name}' execution timed out after {timeout} seconds.",
                call_id=call_id,
                metadata={"duration_ms": duration_ms, "timeout_seconds": timeout},
            )
        except Exception as e:
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            return ToolResult(
                tool_name=clean_name,
                success=False,
                error=f"Unexpected error executing tool '{clean_name}': {str(e)}",
                call_id=call_id,
                metadata={"duration_ms": duration_ms},
            )

        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        meta = dict(result.metadata)
        meta["duration_ms"] = duration_ms

        return ToolResult(
            tool_name=result.tool_name,
            success=result.success,
            data=result.data,
            error=result.error,
            call_id=result.call_id,
            metadata=meta,
        )

    def to_tool_context(
        self,
        tool_result: ToolResult,
        existing_context: Optional[ToolContext] = None,
    ) -> ToolContext:
        """
        Bridge a ToolResult into a clean ToolContext container
        without polluting conversation history.
        """
        ctx = existing_context or ToolContext()
        return ctx.add_result(
            tool_name=tool_result.tool_name,
            call_id=tool_result.call_id or "",
            output=tool_result.data if tool_result.success else tool_result.error,
            is_error=not tool_result.success,
            metadata=tool_result.metadata,
        )
