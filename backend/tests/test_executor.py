import unittest
import asyncio
from typing import Dict, Any

from backend.tools import (
    BaseTool,
    ToolMetadata,
    ToolResult,
    ToolRegistry,
    CalculatorTool,
    ToolExecutor,
)
from backend.context.tools import ToolContext


class SlowMockTool(BaseTool):
    name = "slow_tool"
    description = "A mock tool that takes time to complete."

    async def execute(self, delay: float = 0.5) -> str:
        await asyncio.sleep(delay)
        return "completed"


class CrashingMockTool(BaseTool):
    name = "crash_tool"
    description = "A mock tool that raises an unexpected internal runtime error."

    async def execute(self, **kwargs) -> Any:
        raise RuntimeError("Internal DB connection dropped unexpectedly")


class TestToolExecutor(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.calc_tool = CalculatorTool()
        self.registry.register(self.calc_tool)
        self.executor = ToolExecutor(registry=self.registry)

    def test_executor_initialization_type_guard(self):
        with self.assertRaises(TypeError):
            ToolExecutor(registry="not-a-registry")  # type: ignore

    async def test_execute_calculator_success_60k(self):
        result = await self.executor.execute(
            tool_name="calculator",
            arguments={"expression": "60000 * 0.20"},
            call_id="call_001",
        )
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.success)
        self.assertEqual(result.tool_name, "calculator")
        self.assertEqual(result.data["result"], 12000)
        self.assertEqual(result.call_id, "call_001")
        self.assertIsNone(result.error)
        self.assertIn("duration_ms", result.metadata)
        self.assertGreaterEqual(result.metadata["duration_ms"], 0)

    async def test_execute_calculator_success_multi_op(self):
        result = await self.executor.execute(
            tool_name="calculator",
            arguments={"expression": "200000 * 0.36 / 12"},
            call_id="call_002",
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["result"], 6000)

    async def test_execute_unknown_tool_returns_structured_failure(self):
        result = await self.executor.execute(
            tool_name="nonexistent_tool",
            arguments={},
            call_id="call_404",
        )
        self.assertFalse(result.success)
        self.assertIsNone(result.data)
        self.assertIn("not found in registry", result.error)
        self.assertEqual(result.call_id, "call_404")

    async def test_execute_invalid_tool_name_formats(self):
        res1 = await self.executor.execute(tool_name="")
        self.assertFalse(res1.success)
        self.assertIn("non-empty", res1.error)

        res2 = await self.executor.execute(tool_name="   ")
        self.assertFalse(res2.success)
        self.assertIn("non-empty", res2.error)

    async def test_execute_invalid_calculator_arguments_missing_field(self):
        result = await self.executor.execute(
            tool_name="calculator",
            arguments={},  # missing 'expression'
            call_id="call_bad_args",
        )
        self.assertFalse(result.success)
        self.assertIn("Validation failed", result.error)
        self.assertIn("validation_errors", result.metadata)

    async def test_execute_invalid_calculator_arguments_empty_string(self):
        result = await self.executor.execute(
            tool_name="calculator",
            arguments={"expression": "   "},
            call_id="call_empty_expr",
        )
        self.assertFalse(result.success)
        self.assertIn("Validation failed", result.error)

    async def test_execute_tool_execution_failure_handled(self):
        # Calculation that fails at runtime (e.g. zero division)
        result = await self.executor.execute(
            tool_name="calculator",
            arguments={"expression": "100 / 0"},
            call_id="call_div_zero",
        )
        self.assertFalse(result.success)
        self.assertIn("Division by zero", result.error)

    async def test_execute_unexpected_tool_crash_contained(self):
        crash_tool = CrashingMockTool()
        self.registry.register(crash_tool)

        result = await self.executor.execute(
            tool_name="crash_tool",
            arguments={},
            call_id="call_crash",
        )
        self.assertFalse(result.success)
        self.assertIn("Internal DB connection dropped", result.error)
        # Traceback should not be directly in error
        self.assertNotIn("Traceback", result.error)

    async def test_execute_timeout_handling(self):
        slow_tool = SlowMockTool()
        self.registry.register(slow_tool)

        result = await self.executor.execute(
            tool_name="slow_tool",
            arguments={"delay": 0.5},
            call_id="call_timeout",
            timeout_seconds=0.05,
        )
        self.assertFalse(result.success)
        self.assertIn("timed out after 0.05 seconds", result.error)
        self.assertIn("duration_ms", result.metadata)

    def test_to_tool_context_bridge_success(self):
        tool_result = ToolResult(
            tool_name="calculator",
            success=True,
            data={"result": 12000},
            call_id="call_ctx_1",
            metadata={"duration_ms": 1.5},
        )
        context = self.executor.to_tool_context(tool_result)
        self.assertIsInstance(context, ToolContext)
        self.assertEqual(len(context.results), 1)
        res_item = context.results[0]
        self.assertEqual(res_item.tool_name, "calculator")
        self.assertEqual(res_item.call_id, "call_ctx_1")
        self.assertEqual(res_item.output, {"result": 12000})
        self.assertFalse(res_item.is_error)

    def test_to_tool_context_bridge_failure(self):
        tool_result = ToolResult(
            tool_name="calculator",
            success=False,
            error="Division by zero",
            call_id="call_ctx_err",
        )
        context = self.executor.to_tool_context(tool_result)
        self.assertEqual(len(context.results), 1)
        res_item = context.results[0]
        self.assertEqual(res_item.output, "Division by zero")
        self.assertTrue(res_item.is_error)


if __name__ == "__main__":
    unittest.main()
