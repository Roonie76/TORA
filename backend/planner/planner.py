import json
import re
from typing import Optional, List, Dict, Any

from ..llm.base import LLMProvider, LLMResponse
from ..tools.registry import ToolRegistry
from ..tools.base import ToolValidationError
from ..context.conversation import ConversationContext
from ..prompts.planner import get_planner_system_prompt
from .models import ToolPlan, ToolPlanStep, MAX_PLAN_STEPS


class Planner:
    """
    Planner abstraction responsible for analyzing user intent and producing
    a strongly typed, validated ToolPlan based on available tools in ToolRegistry.
    Completely decoupled from tool execution (does NOT execute tools).
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        tool_registry: ToolRegistry,
        max_steps: int = MAX_PLAN_STEPS,
    ):
        if not isinstance(tool_registry, ToolRegistry):
            raise TypeError(f"Expected ToolRegistry instance, got {type(tool_registry).__name__}")

        self.llm_provider = llm_provider
        self.tool_registry = tool_registry
        self.max_steps = max_steps

    def _extract_json_payload(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Extract JSON object from LLM response text, handling markdown fences
        and surrounding prose safely.
        """
        cleaned = text.strip()
        
        # 1. Direct JSON parse
        try:
            return json.loads(cleaned)
        except Exception:
            pass

        # 2. Markdown fence extraction (```json ... ``` or ``` ... ```)
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
        if fence_match:
            try:
                return json.loads(fence_match.group(1).strip())
            except Exception:
                pass

        # 3. Find first outer curly braces { ... }
        brace_match = re.search(r"\{[\s\S]*\}", cleaned)
        if brace_match:
            try:
                return json.loads(brace_match.group(0).strip())
            except Exception:
                pass

        return None

    def _validate_and_sanitize_plan(self, raw_plan: ToolPlan) -> ToolPlan:
        """
        Validate a ToolPlan against the ToolRegistry and argument schemas.
        Rejects plans with unknown tools, invalid schemas, or excessive steps.
        """
        if not raw_plan.requires_tools or not raw_plan.steps:
            return ToolPlan(
                requires_tools=False,
                steps=[],
                thought=raw_plan.thought,
            )

        # Enforce step count limit
        if len(raw_plan.steps) > self.max_steps:
            return ToolPlan(
                requires_tools=False,
                steps=[],
                thought=f"Plan rejected: step count ({len(raw_plan.steps)}) exceeds maximum limit of {self.max_steps}.",
            )

        validated_steps: List[ToolPlanStep] = []

        for idx, step in enumerate(raw_plan.steps):
            clean_name = step.tool_name.strip() if step.tool_name else ""
            
            # 1. Check if tool exists in registry
            if not self.tool_registry.has(clean_name):
                return ToolPlan(
                    requires_tools=False,
                    steps=[],
                    thought=f"Plan rejected: tool '{clean_name}' at step {idx + 1} is not registered in ToolRegistry.",
                )

            tool = self.tool_registry.get(clean_name)
            if tool is None:
                return ToolPlan(
                    requires_tools=False,
                    steps=[],
                    thought=f"Plan rejected: tool '{clean_name}' could not be resolved.",
                )

            # 2. Validate arguments against tool schema
            try:
                validated_args = tool.validate_args(step.arguments)
            except (ToolValidationError, Exception) as e:
                return ToolPlan(
                    requires_tools=False,
                    steps=[],
                    thought=f"Plan rejected: arguments for tool '{clean_name}' failed validation: {str(e)}",
                )

            validated_steps.append(
                ToolPlanStep(
                    tool_name=clean_name,
                    arguments=validated_args,
                    call_id=step.call_id or f"step_{idx + 1}",
                )
            )

        return ToolPlan(
            requires_tools=True,
            steps=validated_steps,
            thought=raw_plan.thought,
        )

    async def plan(
        self,
        message: str,
        context: Optional[ConversationContext] = None,
        model: Optional[str] = None,
        known_facts: str = "",
    ) -> ToolPlan:
        """
        Generate a validated ToolPlan for the provided user message.

        :param message: The user query to evaluate.
        :param context: Optional previous conversation turns for conversational grounding.
        :param model: Optional LLM model override.
        :return: Validated ToolPlan.
        """
        if not message or not message.strip():
            return ToolPlan(requires_tools=False, steps=[], thought="Empty user message.")

        # If no tools are registered, no planning is needed
        if len(self.tool_registry) == 0:
            return ToolPlan(requires_tools=False, steps=[], thought="No tools available in ToolRegistry.")

        # Build dynamic planner prompt with current tool schemas
        tool_schemas = self.tool_registry.get_schemas()
        planner_system_prompt = get_planner_system_prompt(tool_schemas, known_facts=known_facts)

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": planner_system_prompt},
        ]

        # Add recent conversation turns if present
        if context is not None and len(context) > 0:
            recent_turns = context.get_message_dicts(limit=6)
            for turn in recent_turns:
                messages.append(turn)

        messages.append({"role": "user", "content": message.strip()})

        try:
            llm_response: LLMResponse = await self.llm_provider.generate(
                messages=messages,
                model=model,
                options={"temperature": 0.0},
            )
        except Exception as e:
            return ToolPlan(
                requires_tools=False,
                steps=[],
                thought=f"Planning failed due to LLM provider error: {str(e)}",
            )

        payload = self._extract_json_payload(llm_response.content)
        if payload is None:
            return ToolPlan(
                requires_tools=False,
                steps=[],
                thought="LLM response did not contain valid JSON plan.",
            )

        try:
            raw_plan = ToolPlan.model_validate(payload)
        except Exception as e:
            return ToolPlan(
                requires_tools=False,
                steps=[],
                thought=f"Plan failed schema validation: {str(e)}",
            )

        return self._validate_and_sanitize_plan(raw_plan)
