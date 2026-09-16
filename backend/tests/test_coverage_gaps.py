"""
Test Coverage Gap Closures — Phase 2E Audit

Adds focused tests for the 12 shortcomings identified in the test suite audit.
Each test class is numbered and maps directly to one audit gap.

Does NOT modify production code.
Does NOT implement Phase 2F.
"""

import unittest
import json
from typing import Optional, List, Dict, Any
from unittest.mock import AsyncMock, patch, MagicMock

from backend.context import (
    ConversationContext,
    ContextBuilder,
    ToolContext,
    UserContext,
    FinancialContext,
    KnowledgeContext,
    MAX_HISTORY_MESSAGES,
)
from backend.context.tools import ToolResult as ContextToolResult
from backend.tools import (
    ToolResult,
    ToolRegistry,
    CalculatorTool,
    ToolExecutor,
)
from backend.planner import Planner, ToolPlan, ToolPlanStep, MAX_PLAN_STEPS
from backend.planner.planner import Planner as PlannerImpl
from backend.agent import ToraAgent, AgentResponse
from backend.llm.base import (
    LLMProvider,
    LLMResponse,
    LLMConnectionError,
    LLMTimeoutError,
    LLMProviderError,
)
from backend.prompts import TORA_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Deterministic Mock LLM Provider used across Planner tests (gaps 2, 4, 5, 6)
# ---------------------------------------------------------------------------

class MockLLM(LLMProvider):
    """Deterministic mock for planner coverage-gap tests."""

    def __init__(self, response_content: str = "{}", error: Optional[Exception] = None):
        self.response_content = response_content
        self.error = error
        self.last_messages: Optional[List[Dict[str, str]]] = None

    @property
    def default_model(self) -> str:
        return "mock-model"

    async def generate(self, messages, model=None, options=None):
        self.last_messages = messages
        if self.error:
            raise self.error
        return LLMResponse(content=self.response_content, model=model or self.default_model)

    async def list_models(self):
        return ["mock-model"]

    async def health_check(self):
        return {"status": "ok"}


# ===========================================================================
# GAP #1 — ContextBuilder: Tool Error Rendering
# ===========================================================================

class TestGap1_ContextBuilderToolErrorRendering(unittest.TestCase):
    """
    Gap #1 (HIGH): Verify ContextBuilder renders tool error results using
    the 'Error' status string rather than 'Result'.
    Tests builder.py L78: status_str = "Error" if res.is_error else "Result"
    """

    def setUp(self):
        self.builder = ContextBuilder(
            default_system_prompt="Default TORA prompt",
            max_history=4,
        )

    def test_error_tool_result_renders_error_marker(self):
        tool_ctx = ToolContext()
        tool_ctx = tool_ctx.add_result(
            tool_name="calculator",
            call_id="call-err-1",
            output="Division by zero",
            is_error=True,
        )

        messages = self.builder.build(
            current_message="What is 100/0?",
            tool_context=tool_ctx,
        )

        system_content = messages[0]["content"]
        # Verify the error marker is present (not "Result")
        self.assertIn("Error", system_content)
        self.assertIn("Division by zero", system_content)
        self.assertIn("calculator", system_content)
        self.assertIn("## Tool Execution Results", system_content)
        # Verify "Result" marker is NOT present for this error case
        self.assertNotIn(": Result =", system_content)
        self.assertIn(": Error =", system_content)

    def test_success_tool_result_renders_result_marker(self):
        """Complementary test: success uses 'Result' not 'Error'."""
        tool_ctx = ToolContext()
        tool_ctx = tool_ctx.add_result(
            tool_name="calculator",
            call_id="call-ok-1",
            output={"result": 12000},
            is_error=False,
        )

        messages = self.builder.build(
            current_message="What is 20% of 60000?",
            tool_context=tool_ctx,
        )

        system_content = messages[0]["content"]
        self.assertIn(": Result =", system_content)
        self.assertNotIn(": Error =", system_content)
        self.assertIn("12000", system_content)


# ===========================================================================
# GAP #2 — Planner: LLM Provider Failure
# ===========================================================================

class TestGap2_PlannerLLMProviderCrash(unittest.IsolatedAsyncioTestCase):
    """
    Gap #2 (HIGH): Verify Planner catches LLM provider exceptions and
    returns a safe fallback ToolPlan without crashing.
    Tests planner.py L166-177: try/except around llm_provider.generate().
    """

    async def test_connection_error_returns_safe_fallback_plan(self):
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        mock_llm = MockLLM(error=LLMConnectionError("Ollama is down"))
        planner = Planner(llm_provider=mock_llm, tool_registry=registry)

        plan = await planner.plan("Calculate 2 + 2")

        self.assertIsInstance(plan, ToolPlan)
        self.assertFalse(plan.requires_tools)
        self.assertEqual(len(plan.steps), 0)
        self.assertIn("LLM provider error", plan.thought)
        self.assertIn("Ollama is down", plan.thought)

    async def test_timeout_error_returns_safe_fallback_plan(self):
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        mock_llm = MockLLM(error=LLMTimeoutError("Request timed out after 180s"))
        planner = Planner(llm_provider=mock_llm, tool_registry=registry)

        plan = await planner.plan("What is 20% of 60000?")

        self.assertIsInstance(plan, ToolPlan)
        self.assertFalse(plan.requires_tools)
        self.assertEqual(len(plan.steps), 0)
        self.assertIn("LLM provider error", plan.thought)

    async def test_generic_runtime_error_returns_safe_fallback_plan(self):
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        mock_llm = MockLLM(error=RuntimeError("Unexpected internal error"))
        planner = Planner(llm_provider=mock_llm, tool_registry=registry)

        plan = await planner.plan("Calculate something")

        self.assertFalse(plan.requires_tools)
        self.assertIn("LLM provider error", plan.thought)


# ===========================================================================
# GAP #3 — ToraAgent: Planner Without Executor
# ===========================================================================

class TestGap3_AgentPlannerWithoutExecutor(unittest.IsolatedAsyncioTestCase):
    """
    Gap #3 (HIGH): Verify that when ToraAgent has a Planner but no ToolExecutor,
    the tool planning loop is skipped entirely and normal LLM generation proceeds.
    Tests agent.py L153: `if self._planner is not None and self._tool_executor is not None`.
    """

    async def test_planner_only_agent_skips_tool_loop_and_generates_normally(self):
        mock_provider = AsyncMock(spec=LLMProvider)
        # Only the final synthesis call should happen (no planner call)
        mock_provider.generate.return_value = LLMResponse(
            content="Here is a normal response without tool execution.",
            model="test-model",
            done=True,
        )

        registry = ToolRegistry()
        registry.register(CalculatorTool())
        planner = Planner(llm_provider=mock_provider, tool_registry=registry)

        # Planner is set, but tool_executor is None
        tora_agent = ToraAgent(
            llm_provider=mock_provider,
            planner=planner,
            tool_executor=None,
        )

        response = await tora_agent.run(message="What is 20% of 60000?")

        # Agent should produce a response from direct LLM generation
        self.assertIsInstance(response, AgentResponse)
        self.assertEqual(response.content, "Here is a normal response without tool execution.")
        # No plan should be executed
        self.assertIsNone(response.plan)
        # No tool context should exist
        self.assertIsNone(response.tool_context)

        # Verify provider.generate was called exactly ONCE (synthesis only, no planner call)
        mock_provider.generate.assert_called_once()


# ===========================================================================
# GAP #4 — Planner: Pydantic Schema Validation Failure
# ===========================================================================

class TestGap4_PlannerPydanticValidationFailure(unittest.IsolatedAsyncioTestCase):
    """
    Gap #4 (MEDIUM): Verify Planner catches ToolPlan.model_validate() failures
    when JSON is valid but doesn't match the Pydantic schema.
    Tests planner.py L187-194.
    """

    async def test_invalid_requires_tools_type_returns_safe_fallback(self):
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        mock_llm = MockLLM(response_content=json.dumps({
            "thought": "hi",
            "requires_tools": {"invalid": "nested_dict"},  # dict instead of bool
            "steps": [],
        }))
        planner = Planner(llm_provider=mock_llm, tool_registry=registry)

        plan = await planner.plan("Calculate something")

        self.assertIsInstance(plan, ToolPlan)
        self.assertFalse(plan.requires_tools)
        self.assertEqual(len(plan.steps), 0)
        self.assertIn("schema validation", plan.thought)

    async def test_invalid_steps_type_returns_safe_fallback(self):
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        mock_llm = MockLLM(response_content=json.dumps({
            "thought": "attempt",
            "requires_tools": True,
            "steps": "not-a-list",  # string instead of list
        }))
        planner = Planner(llm_provider=mock_llm, tool_registry=registry)

        plan = await planner.plan("Calculate something")

        self.assertFalse(plan.requires_tools)
        self.assertIn("schema validation", plan.thought)


# ===========================================================================
# GAP #5 — Planner: Empty Message Short-Circuit
# ===========================================================================

class TestGap5_PlannerEmptyMessage(unittest.IsolatedAsyncioTestCase):
    """
    Gap #5 (MEDIUM): Verify planner.plan("") and planner.plan("   ")
    return the documented empty-plan behavior without calling the LLM.
    Tests planner.py L143-144.
    """

    def setUp(self):
        self.registry = ToolRegistry()
        self.registry.register(CalculatorTool())
        self.mock_llm = MockLLM()
        self.planner = Planner(llm_provider=self.mock_llm, tool_registry=self.registry)

    async def test_empty_string_returns_empty_plan(self):
        plan = await self.planner.plan("")

        self.assertIsInstance(plan, ToolPlan)
        self.assertFalse(plan.requires_tools)
        self.assertEqual(len(plan.steps), 0)
        self.assertEqual(plan.thought, "Empty user message.")
        # LLM should NOT have been called
        self.assertIsNone(self.mock_llm.last_messages)

    async def test_whitespace_only_returns_empty_plan(self):
        plan = await self.planner.plan("    ")

        self.assertFalse(plan.requires_tools)
        self.assertEqual(plan.thought, "Empty user message.")
        self.assertIsNone(self.mock_llm.last_messages)


# ===========================================================================
# GAP #6 — Planner: Raw Brace JSON Extraction (3rd strategy)
# ===========================================================================

class TestGap6_PlannerBraceExtractionFallback(unittest.IsolatedAsyncioTestCase):
    """
    Gap #6 (MEDIUM): Verify the 3rd JSON extraction strategy in
    _extract_json_payload: finding JSON embedded in surrounding prose
    using raw brace matching.
    Tests planner.py L54-61.
    """

    async def test_json_embedded_in_prose_extracted_correctly(self):
        registry = ToolRegistry()
        registry.register(CalculatorTool())
        embedded_json = json.dumps({
            "thought": "Need to calculate 2+2.",
            "requires_tools": True,
            "steps": [
                {"tool_name": "calculator", "arguments": {"expression": "2 + 2"}}
            ]
        })
        # Wrap valid JSON in surrounding prose (not a markdown fence)
        prose_response = f"Let me analyze this request.\n{embedded_json}\nThat should do it."
        mock_llm = MockLLM(response_content=prose_response)
        planner = Planner(llm_provider=mock_llm, tool_registry=registry)

        plan = await planner.plan("Calculate 2+2")

        self.assertTrue(plan.requires_tools)
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].tool_name, "calculator")
        self.assertEqual(plan.steps[0].arguments["expression"], "2 + 2")


# ===========================================================================
# GAP #7 — ContextBuilder: Multiple Tool Results
# ===========================================================================

class TestGap7_ContextBuilderMultipleToolResults(unittest.TestCase):
    """
    Gap #7 (MEDIUM): Verify ContextBuilder correctly renders multiple
    tool results from a single ToolContext, preserving order and keeping
    them in the system message (not user/assistant messages).
    """

    def setUp(self):
        self.builder = ContextBuilder(
            default_system_prompt="Default TORA prompt",
            max_history=4,
        )

    def test_two_results_both_rendered_in_system_message(self):
        tool_ctx = ToolContext()
        tool_ctx = tool_ctx.add_result(
            tool_name="calculator",
            call_id="call-1",
            output={"result": 12000},
            is_error=False,
        )
        tool_ctx = tool_ctx.add_result(
            tool_name="calculator",
            call_id="call-2",
            output={"result": 6000},
            is_error=False,
        )

        messages = self.builder.build(
            current_message="Give me both results.",
            tool_context=tool_ctx,
        )

        # System message must be first
        self.assertEqual(messages[0]["role"], "system")
        system_content = messages[0]["content"]

        # Both results must appear in the system message
        self.assertIn("12000", system_content)
        self.assertIn("6000", system_content)
        self.assertIn("## Tool Execution Results", system_content)

        # Verify order is preserved (12000 appears before 6000)
        pos_12000 = system_content.index("12000")
        pos_6000 = system_content.index("6000")
        self.assertLess(pos_12000, pos_6000)

        # Verify neither result leaked into a user or assistant message
        for msg in messages[1:]:
            self.assertNotIn("## Tool Execution Results", msg["content"])

    def test_mixed_success_and_error_results(self):
        tool_ctx = ToolContext()
        tool_ctx = tool_ctx.add_result(
            tool_name="calculator",
            call_id="call-ok",
            output={"result": 12000},
            is_error=False,
        )
        tool_ctx = tool_ctx.add_result(
            tool_name="calculator",
            call_id="call-err",
            output="Division by zero",
            is_error=True,
        )

        messages = self.builder.build(
            current_message="Both calculations please.",
            tool_context=tool_ctx,
        )

        system_content = messages[0]["content"]
        self.assertIn(": Result =", system_content)
        self.assertIn(": Error =", system_content)
        self.assertIn("12000", system_content)
        self.assertIn("Division by zero", system_content)


# ===========================================================================
# GAP #8 — FastAPI /api/chat Edge Cases
# ===========================================================================

class TestGap8_ChatRouteEdgeCases(unittest.TestCase):
    """
    Gap #8 (MEDIUM): Four missing FastAPI /api/chat edge cases.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        from backend.main import app, agent
        self.client = TestClient(app)
        self.agent = agent

    def test_8a_messages_with_empty_content_returns_400(self):
        """Message item with empty content at a specific index → 400."""
        response = self.client.post(
            "/api/chat",
            json={
                "messages": [
                    {"role": "user", "content": "First message"},
                    {"role": "assistant", "content": ""},  # empty content
                    {"role": "user", "content": "Follow-up"},
                ]
            }
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be empty", response.json()["detail"])

    def test_8b_messages_last_is_assistant_no_message_field_returns_400(self):
        """messages non-empty, last message is assistant, no separate message field → 400."""
        response = self.client.post(
            "/api/chat",
            json={
                "messages": [
                    {"role": "user", "content": "Hello"},
                    {"role": "assistant", "content": "Hi there."},
                ]
            }
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("must be from the 'user' role", response.json()["detail"])

    def test_8c_empty_messages_list_no_message_field_returns_400(self):
        """messages=[] and no message field → 400."""
        response = self.client.post(
            "/api/chat",
            json={"messages": []}
        )
        self.assertEqual(response.status_code, 400)

    def test_8d_model_and_temperature_reach_agent(self):
        """model and temperature supplied in request reach agent.run()."""
        from backend.main import agent
        with patch.object(agent, "run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = AgentResponse(
                content="Response with custom params.",
                model="llama3.2:3b",
                done=True,
            )

            response = self.client.post(
                "/api/chat",
                json={
                    "message": "Hello",
                    "model": "llama3.2:3b",
                    "temperature": 0.3,
                }
            )

            self.assertEqual(response.status_code, 200)
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args[1]
            self.assertEqual(call_kwargs["model"], "llama3.2:3b")
            self.assertEqual(call_kwargs["temperature"], 0.3)


# Remove the broken test_8d_model_and_temperature_propagated_to_agent
# that used patch.object(None, ...) incorrectly
# The actual test is test_8d_model_and_temperature_reach_agent above


# ===========================================================================
# GAP #9 — ToolExecutor: Existing ToolContext Accumulation
# ===========================================================================

class TestGap9_ToolContextAccumulation(unittest.TestCase):
    """
    Gap #9 (MEDIUM): Verify to_tool_context appends to an existing
    ToolContext and that the original context remains immutable.
    """

    def setUp(self):
        self.registry = ToolRegistry()
        self.registry.register(CalculatorTool())
        self.executor = ToolExecutor(registry=self.registry)

    def test_accumulation_with_existing_context(self):
        # Create an initial ToolContext with one result
        initial_ctx = ToolContext()
        initial_ctx = initial_ctx.add_result(
            tool_name="calculator",
            call_id="prior-call-1",
            output={"result": 100},
            is_error=False,
        )
        self.assertEqual(len(initial_ctx.results), 1)

        # Bridge a new ToolResult with the existing context
        new_result = ToolResult(
            tool_name="calculator",
            success=True,
            data={"result": 200},
            call_id="new-call-2",
            metadata={"duration_ms": 1.0},
        )
        accumulated_ctx = self.executor.to_tool_context(
            tool_result=new_result,
            existing_context=initial_ctx,
        )

        # Accumulated context should have both results
        self.assertEqual(len(accumulated_ctx.results), 2)
        self.assertEqual(accumulated_ctx.results[0].tool_name, "calculator")
        self.assertEqual(accumulated_ctx.results[0].call_id, "prior-call-1")
        self.assertEqual(accumulated_ctx.results[0].output, {"result": 100})
        self.assertEqual(accumulated_ctx.results[1].call_id, "new-call-2")
        self.assertEqual(accumulated_ctx.results[1].output, {"result": 200})

        # Original context must be unchanged (immutability)
        self.assertEqual(len(initial_ctx.results), 1)
        self.assertEqual(initial_ctx.results[0].call_id, "prior-call-1")

    def test_accumulation_preserves_error_results(self):
        initial_ctx = ToolContext()
        initial_ctx = initial_ctx.add_result(
            tool_name="calculator",
            call_id="ok-call",
            output={"result": 42},
            is_error=False,
        )

        error_result = ToolResult(
            tool_name="calculator",
            success=False,
            error="Division by zero",
            call_id="err-call",
        )
        accumulated_ctx = self.executor.to_tool_context(
            tool_result=error_result,
            existing_context=initial_ctx,
        )

        self.assertEqual(len(accumulated_ctx.results), 2)
        self.assertFalse(accumulated_ctx.results[0].is_error)
        self.assertTrue(accumulated_ctx.results[1].is_error)
        self.assertEqual(accumulated_ctx.results[1].output, "Division by zero")


# ===========================================================================
# GAP #10 — ToolPlanStep / ToolPlan Pydantic Model Tests
# ===========================================================================

class TestGap10_ToolPlanModels(unittest.TestCase):
    """
    Gap #10 (LOW): Direct unit tests for ToolPlanStep and ToolPlan Pydantic
    models — construction, defaults, validation behavior.
    Tests the CURRENT contract without inventing stricter rules.
    """

    def test_tool_plan_step_normal_construction(self):
        step = ToolPlanStep(
            tool_name="calculator",
            arguments={"expression": "2 + 2"},
            call_id="step_1",
        )
        self.assertEqual(step.tool_name, "calculator")
        self.assertEqual(step.arguments, {"expression": "2 + 2"})
        self.assertEqual(step.call_id, "step_1")

    def test_tool_plan_step_default_call_id_is_none(self):
        step = ToolPlanStep(tool_name="calculator", arguments={})
        self.assertIsNone(step.call_id)

    def test_tool_plan_step_default_arguments_is_empty_dict(self):
        step = ToolPlanStep(tool_name="test_tool")
        self.assertEqual(step.arguments, {})

    def test_tool_plan_step_empty_tool_name_allowed_at_model_level(self):
        """
        Documents that Pydantic ToolPlanStep does NOT reject empty tool_name.
        Rejection happens at the Planner/Registry validation layer instead.
        """
        step = ToolPlanStep(tool_name="", arguments={})
        self.assertEqual(step.tool_name, "")

    def test_tool_plan_default_requires_tools_is_false(self):
        plan = ToolPlan()
        self.assertFalse(plan.requires_tools)
        self.assertEqual(plan.steps, [])
        self.assertIsNone(plan.thought)

    def test_tool_plan_with_multiple_steps(self):
        steps = [
            ToolPlanStep(tool_name="calculator", arguments={"expression": "1+1"}),
            ToolPlanStep(tool_name="calculator", arguments={"expression": "2+2"}),
            ToolPlanStep(tool_name="calculator", arguments={"expression": "3+3"}),
        ]
        plan = ToolPlan(requires_tools=True, steps=steps, thought="Three calcs")

        self.assertTrue(plan.requires_tools)
        self.assertEqual(len(plan.steps), 3)
        self.assertEqual(plan.thought, "Three calcs")

    def test_tool_plan_step_rejects_invalid_tool_name_type(self):
        """Pydantic should reject non-string tool_name."""
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            ToolPlanStep(tool_name=12345, arguments={})

    def test_tool_plan_rejects_invalid_requires_tools_type(self):
        """Pydantic should reject non-bool requires_tools (e.g. dictionary or list)."""
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            ToolPlan(requires_tools={"not": "a_bool"}, steps=[])

    def test_tool_plan_rejects_invalid_steps_type(self):
        """Pydantic should reject non-list steps."""
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            ToolPlan(requires_tools=True, steps="not-a-list")

    def test_max_plan_steps_constant(self):
        self.assertEqual(MAX_PLAN_STEPS, 3)


# ===========================================================================
# GAP #11 — GET / Root Endpoint
# ===========================================================================

class TestGap11_RootEndpoint(unittest.TestCase):
    """
    Gap #11 (LOW): Verify GET / returns the same health data as /api/health.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        from backend.main import app, agent
        self.client = TestClient(app)
        self.agent = agent

    def test_root_endpoint_returns_health_data(self):
        with patch.object(
            self.agent.llm_provider, "health_check", new_callable=AsyncMock
        ) as mock_health:
            mock_health.return_value = {
                "host": "http://127.0.0.1:11434",
                "connected": True,
                "models": ["gemma4:e4b"],
                "default_model": "gemma4:e4b",
            }

            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["service"], "spendsy-fastapi-backend")
            self.assertTrue(data["ollama"]["connected"])

    def test_root_and_health_endpoints_share_same_behavior(self):
        with patch.object(
            self.agent.llm_provider, "health_check", new_callable=AsyncMock
        ) as mock_health:
            mock_health.return_value = {
                "host": "http://127.0.0.1:11434",
                "connected": True,
                "models": ["gemma4:e4b"],
                "default_model": "gemma4:e4b",
            }

            root_response = self.client.get("/")
            health_response = self.client.get("/api/health")
            self.assertEqual(root_response.status_code, health_response.status_code)
            self.assertEqual(root_response.json(), health_response.json())


if __name__ == "__main__":
    unittest.main()
