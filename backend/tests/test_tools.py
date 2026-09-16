import unittest
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from backend.tools import (
    BaseTool,
    ToolMetadata,
    ToolResult,
    ToolError,
    ToolNotFoundError,
    ToolAlreadyRegisteredError,
    ToolValidationError,
    ToolExecutionError,
    ToolRegistry,
)


# Sample tool schemas for testing
class AddInput(BaseModel):
    a: float = Field(description="First number")
    b: float = Field(description="Second number")


class AddTool(BaseTool):
    name = "add"
    description = "Add two numbers together."
    args_schema = AddInput

    async def execute(self, a: float, b: float) -> float:
        return a + b


class FailingTool(BaseTool):
    name = "failing_tool"
    description = "A tool that throws an unexpected error."

    async def execute(self, **kwargs) -> Any:
        raise RuntimeError("Something went wrong internally")


class SimpleEchoTool(BaseTool):
    name = "echo"
    description = "Echo back parameters without schema."

    async def execute(self, **kwargs) -> Dict[str, Any]:
        return kwargs


class TestToolMetadata(unittest.TestCase):
    def test_metadata_creation_success(self):
        meta = ToolMetadata(
            name="tax_calc",
            description="Computes income tax",
            version="1.2.0",
            tags=["finance", "tax"],
            is_deterministic=True,
            requires_auth=True,
        )
        self.assertEqual(meta.name, "tax_calc")
        self.assertEqual(meta.description, "Computes income tax")
        self.assertEqual(meta.version, "1.2.0")
        self.assertEqual(meta.tags, ["finance", "tax"])
        self.assertTrue(meta.is_deterministic)
        self.assertTrue(meta.requires_auth)

    def test_metadata_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            ToolMetadata(name="", description="Valid desc")
        with self.assertRaises(ValueError):
            ToolMetadata(name="   ", description="Valid desc")

    def test_metadata_rejects_empty_description(self):
        with self.assertRaises(ValueError):
            ToolMetadata(name="calc", description="")
        with self.assertRaises(ValueError):
            ToolMetadata(name="calc", description="   ")


class TestToolResult(unittest.TestCase):
    def test_tool_result_success(self):
        res = ToolResult(
            tool_name="add",
            success=True,
            data=42.0,
            call_id="call-1",
        )
        self.assertEqual(res.tool_name, "add")
        self.assertTrue(res.success)
        self.assertEqual(res.data, 42.0)
        self.assertIsNone(res.error)
        self.assertEqual(res.call_id, "call-1")

        d = res.to_dict()
        self.assertEqual(d["tool_name"], "add")
        self.assertTrue(d["success"])
        self.assertEqual(d["data"], 42.0)

    def test_tool_result_failure(self):
        res = ToolResult(
            tool_name="add",
            success=False,
            error="Missing required argument 'b'",
            call_id="call-2",
        )
        self.assertFalse(res.success)
        self.assertIsNone(res.data)
        self.assertEqual(res.error, "Missing required argument 'b'")


class TestBaseTool(unittest.IsolatedAsyncioTestCase):
    def test_tool_schema_generation_with_pydantic_model(self):
        tool = AddTool()
        schema = tool.get_schema()
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "add")
        self.assertEqual(schema["function"]["description"], "Add two numbers together.")
        params = schema["function"]["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertIn("a", params["properties"])
        self.assertIn("b", params["properties"])
        self.assertIn("a", params["required"])
        self.assertIn("b", params["required"])

    def test_tool_schema_generation_without_pydantic_model(self):
        tool = SimpleEchoTool()
        schema = tool.get_schema()
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "echo")
        self.assertEqual(schema["function"]["parameters"], {"type": "object", "properties": {}})

    def test_tool_validation_success(self):
        tool = AddTool()
        validated = tool.validate_args({"a": 10, "b": 20})
        self.assertEqual(validated, {"a": 10.0, "b": 20.0})

    def test_tool_validation_failure_raises_tool_validation_error(self):
        tool = AddTool()
        with self.assertRaises(ToolValidationError) as ctx:
            tool.validate_args({"a": 10})  # missing 'b'
        self.assertIn("Validation failed", str(ctx.exception))

    async def test_tool_run_success(self):
        tool = AddTool()
        res = await tool.run(args={"a": 15.5, "b": 4.5}, call_id="call-xyz")
        self.assertTrue(res.success)
        self.assertEqual(res.data, 20.0)
        self.assertEqual(res.call_id, "call-xyz")

    async def test_tool_run_validation_error_captured(self):
        tool = AddTool()
        res = await tool.run(args={"a": "not-a-number"}, call_id="call-err")
        self.assertFalse(res.success)
        self.assertIn("Validation failed", res.error)
        self.assertEqual(res.call_id, "call-err")

    async def test_tool_run_execution_exception_captured(self):
        tool = FailingTool()
        res = await tool.run(args={}, call_id="call-fail")
        self.assertFalse(res.success)
        self.assertIn("Something went wrong internally", res.error)


class TestToolRegistry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.add_tool = AddTool()
        self.echo_tool = SimpleEchoTool()

    def test_register_and_lookup_tool(self):
        self.registry.register(self.add_tool)
        self.assertEqual(len(self.registry), 1)
        self.assertTrue(self.registry.has("add"))
        self.assertIn("add", self.registry)
        self.assertIs(self.registry.get("add"), self.add_tool)

    def test_register_rejects_non_basetool(self):
        with self.assertRaises(TypeError):
            self.registry.register("not-a-tool")  # type: ignore

    def test_register_duplicate_name_raises_error(self):
        self.registry.register(self.add_tool)
        with self.assertRaises(ToolAlreadyRegisteredError):
            self.registry.register(self.add_tool)

    def test_register_duplicate_with_overwrite(self):
        self.registry.register(self.add_tool)
        new_add_tool = AddTool()
        self.registry.register(new_add_tool, overwrite=True)
        self.assertIs(self.registry.get("add"), new_add_tool)

    def test_unregister_tool(self):
        self.registry.register(self.add_tool)
        removed = self.registry.unregister("add")
        self.assertIs(removed, self.add_tool)
        self.assertFalse(self.registry.has("add"))
        self.assertEqual(len(self.registry), 0)

    def test_get_or_raise(self):
        self.registry.register(self.add_tool)
        self.assertIs(self.registry.get_or_raise("add"), self.add_tool)
        with self.assertRaises(ToolNotFoundError):
            self.registry.get_or_raise("non_existent")

    def test_list_tools_and_names(self):
        self.registry.register(self.add_tool)
        self.registry.register(self.echo_tool)
        self.assertEqual(set(self.registry.list_names()), {"add", "echo"})
        self.assertEqual(len(self.registry.list_tools()), 2)

    def test_get_schemas_for_all_registered_tools(self):
        self.registry.register(self.add_tool)
        self.registry.register(self.echo_tool)
        schemas = self.registry.get_schemas()
        self.assertEqual(len(schemas), 2)
        names = [s["function"]["name"] for s in schemas]
        self.assertIn("add", names)
        self.assertIn("echo", names)

    async def test_execute_success(self):
        self.registry.register(self.add_tool)
        res = await self.registry.execute("add", args={"a": 10, "b": 32}, call_id="c-123")
        self.assertTrue(res.success)
        self.assertEqual(res.data, 42.0)
        self.assertEqual(res.call_id, "c-123")

    async def test_execute_non_existent_tool_returns_failure_result(self):
        res = await self.registry.execute("non_existent", args={}, call_id="c-404")
        self.assertFalse(res.success)
        self.assertIn("not found in registry", res.error)

    async def test_execute_validation_failure(self):
        self.registry.register(self.add_tool)
        res = await self.registry.execute("add", args={"a": 10}, call_id="c-bad")
        self.assertFalse(res.success)
        self.assertIn("Validation failed", res.error)

    def test_clear_registry(self):
        self.registry.register(self.add_tool)
        self.registry.clear()
        self.assertEqual(len(self.registry), 0)


if __name__ == "__main__":
    unittest.main()
