import unittest
import math

from backend.tools import (
    CalculatorTool,
    CalculatorInput,
    safe_eval,
    ToolRegistry,
    ToolResult,
)


class TestSafeEvalArithmetic(unittest.TestCase):
    def test_basic_addition(self):
        self.assertEqual(safe_eval("2 + 2"), 4)
        self.assertEqual(safe_eval("15.5 + 4.5"), 20)

    def test_basic_subtraction(self):
        self.assertEqual(safe_eval("10 - 3"), 7)
        self.assertEqual(safe_eval("5 - 12"), -7)

    def test_basic_multiplication(self):
        self.assertEqual(safe_eval("4 * 5"), 20)
        self.assertEqual(safe_eval("2.5 * 4"), 10)

    def test_basic_division(self):
        self.assertEqual(safe_eval("20 / 4"), 5)
        self.assertEqual(safe_eval("7 / 2"), 3.5)

    def test_floor_division(self):
        self.assertEqual(safe_eval("20 // 6"), 3)

    def test_modulo(self):
        self.assertEqual(safe_eval("10 % 3"), 1)
        self.assertEqual(safe_eval("14 % 5"), 4)

    def test_exponentiation(self):
        self.assertEqual(safe_eval("2 ** 3"), 8)
        self.assertEqual(safe_eval("3 ** 4"), 81)
        self.assertEqual(safe_eval("2 ** 10"), 1024)

    def test_parentheses_precedence(self):
        self.assertEqual(safe_eval("(2 + 3) * 4"), 20)
        self.assertEqual(safe_eval("2 + (3 * 4)"), 14)
        self.assertEqual(safe_eval("100 / (2 + 3)"), 20)
        self.assertEqual(safe_eval("((5 + 5) * (3 - 1)) / 4"), 5)

    def test_unary_operators(self):
        self.assertEqual(safe_eval("-5 + 10"), 5)
        self.assertEqual(safe_eval("+15 - 5"), 10)
        self.assertEqual(safe_eval("-(3 + 2)"), -5)
        self.assertEqual(safe_eval("-(-5)"), 5)

    def test_financial_style_calculations(self):
        self.assertEqual(safe_eval("60000 * 0.20"), 12000)
        self.assertEqual(safe_eval("200000 * 0.36 / 12"), 6000)
        self.assertEqual(safe_eval("50000 - 15000 - 10000"), 25000)
        self.assertEqual(safe_eval("(15000 / 50000) * 100"), 30)
        self.assertEqual(safe_eval("150000 + 50000 + 25000"), 225000)


class TestSafeEvalValidationAndLimits(unittest.TestCase):
    def test_rejects_empty_and_whitespace(self):
        with self.assertRaises(ValueError):
            safe_eval("")
        with self.assertRaises(ValueError):
            safe_eval("   ")

    def test_rejects_non_string(self):
        with self.assertRaises(ValueError):
            safe_eval(123)  # type: ignore

    def test_rejects_excessively_long_expression(self):
        long_expr = "1 + " * 300 + "1"
        with self.assertRaises(ValueError) as ctx:
            safe_eval(long_expr)
        self.assertIn("exceeds limit", str(ctx.exception))

    def test_rejects_syntax_errors(self):
        with self.assertRaises(ValueError):
            safe_eval("2 + * 3")
        with self.assertRaises(ValueError):
            safe_eval("(5 + 2")
        with self.assertRaises(ValueError):
            safe_eval("+")
        with self.assertRaises(ValueError):
            safe_eval("* 5")

    def test_division_by_zero(self):
        with self.assertRaises(ZeroDivisionError):
            safe_eval("20 / 0")
        with self.assertRaises(ZeroDivisionError):
            safe_eval("10 // 0")

    def test_modulo_by_zero(self):
        with self.assertRaises(ZeroDivisionError):
            safe_eval("10 % 0")

    def test_zero_to_negative_power(self):
        with self.assertRaises(ZeroDivisionError):
            safe_eval("0 ** -2")

    def test_exponent_safety_limit(self):
        with self.assertRaises(ValueError) as ctx:
            safe_eval("2 ** 1001")
        self.assertIn("exceeds maximum allowed safety limit", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            safe_eval("999999999999 ** 999999999999")
        self.assertIn("safety limit", str(ctx.exception))

    def test_complex_number_rejection(self):
        with self.assertRaises(ValueError) as ctx:
            safe_eval("(-4) ** 0.5")
        self.assertIn("Complex/imaginary", str(ctx.exception))

    def test_deep_expression_recursion_limit(self):
        # Build 35 levels of nested binary operations: 1 + 1 + 1 ...
        deep_expr = "1 + " * 35 + "1"
        with self.assertRaises(ValueError) as ctx:
            safe_eval(deep_expr)
        self.assertIn("depth/complexity", str(ctx.exception))



class TestSafeEvalSecurity(unittest.TestCase):
    def test_rejects_eval_and_exec(self):
        with self.assertRaises(ValueError):
            safe_eval("eval('2 + 2')")
        with self.assertRaises(ValueError):
            safe_eval("exec('x = 5')")

    def test_rejects_imports_and_builtins(self):
        with self.assertRaises(ValueError):
            safe_eval("__import__('os').system('ls')")
        with self.assertRaises(ValueError):
            safe_eval("open('/etc/passwd')")
        with self.assertRaises(ValueError):
            safe_eval("globals()")
        with self.assertRaises(ValueError):
            safe_eval("locals()")

    def test_rejects_attribute_access(self):
        with self.assertRaises(ValueError):
            safe_eval("(1).__class__.__bases__")
        with self.assertRaises(ValueError):
            safe_eval("os.system('calc')")

    def test_rejects_comprehensions(self):
        with self.assertRaises(ValueError):
            safe_eval("[x for x in [1, 2]]")
        with self.assertRaises(ValueError):
            safe_eval("{x: x for x in [1, 2]}")

    def test_rejects_lambdas_and_assignments(self):
        with self.assertRaises(ValueError):
            safe_eval("lambda x: x + 1")
        with self.assertRaises(ValueError):
            safe_eval("x = 5")

    def test_rejects_booleans(self):
        with self.assertRaises(ValueError) as ctx:
            safe_eval("True + 1")
        self.assertIn("Boolean literals are not allowed", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            safe_eval("False * 10")
        self.assertIn("Boolean literals are not allowed", str(ctx.exception))

    def test_rejects_strings_and_data_structures(self):
        with self.assertRaises(ValueError):
            safe_eval("'hello' + 'world'")
        with self.assertRaises(ValueError):
            safe_eval("[1, 2, 3]")
        with self.assertRaises(ValueError):
            safe_eval("{'a': 1}")

    def test_rejects_function_calls(self):
        with self.assertRaises(ValueError):
            safe_eval("abs(-5)")
        with self.assertRaises(ValueError):
            safe_eval("max(10, 20)")
        with self.assertRaises(ValueError):
            safe_eval("pow(2, 3)")


class TestCalculatorToolIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tool = CalculatorTool()
        self.registry = ToolRegistry()

    def test_tool_metadata_and_properties(self):
        self.assertEqual(self.tool.name, "calculator")
        self.assertTrue(self.tool.metadata.is_deterministic)
        self.assertIn("math", self.tool.metadata.tags)
        self.assertFalse(self.tool.metadata.requires_auth)

    def test_schema_generation(self):
        schema = self.tool.get_schema()
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "calculator")
        self.assertIn("expression", schema["function"]["parameters"]["properties"])
        self.assertIn("expression", schema["function"]["parameters"]["required"])

    async def test_direct_tool_run_success(self):
        res = await self.tool.run(
            args={"expression": "60000 * 0.20"},
            call_id="call-calc-1",
        )
        self.assertIsInstance(res, ToolResult)
        self.assertTrue(res.success)
        self.assertEqual(res.tool_name, "calculator")
        self.assertEqual(res.data["expression"], "60000 * 0.20")
        self.assertEqual(res.data["result"], 12000)
        self.assertIsNone(res.error)
        self.assertEqual(res.call_id, "call-calc-1")

    async def test_direct_tool_run_division_by_zero(self):
        res = await self.tool.run(
            args={"expression": "100 / 0"},
            call_id="call-calc-2",
        )
        self.assertFalse(res.success)
        self.assertIn("Division by zero", res.error)
        self.assertEqual(res.call_id, "call-calc-2")

    async def test_direct_tool_run_malformed_input(self):
        res = await self.tool.run(
            args={"expression": "2 + * 3"},
            call_id="call-calc-3",
        )
        self.assertFalse(res.success)
        self.assertIn("Invalid mathematical expression syntax", res.error)

    async def test_direct_tool_run_empty_validation_error(self):
        res = await self.tool.run(
            args={"expression": "   "},
            call_id="call-calc-4",
        )
        self.assertFalse(res.success)
        self.assertIn("Validation failed", res.error)

    async def test_registry_registration_and_execution(self):
        self.registry.register(self.tool)
        self.assertTrue(self.registry.has("calculator"))
        self.assertIn("calculator", self.registry.list_names())

        # Execute through registry
        res = await self.registry.execute(
            name="calculator",
            args={"expression": "200000 * 0.36 / 12"},
            call_id="call-reg-1",
        )
        self.assertTrue(res.success)
        self.assertEqual(res.data["result"], 6000)
        self.assertEqual(res.call_id, "call-reg-1")

    async def test_registry_execution_security_containment(self):
        self.registry.register(self.tool)

        # Attempt forbidden call through registry
        res = await self.registry.execute(
            name="calculator",
            args={"expression": "__import__('os').system('echo pwned')"},
            call_id="call-attack",
        )
        self.assertFalse(res.success)
        self.assertIn("forbidden", res.error)


if __name__ == "__main__":
    unittest.main()
