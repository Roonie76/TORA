import json
import os
import re
from typing import Optional, List, Dict, Any, Tuple

from ..llm.base import LLMProvider, LLMResponse
from ..tools.registry import ToolRegistry
from ..tools.base import ToolValidationError
from ..context.conversation import ConversationContext
from ..prompts.planner import get_planner_system_prompt
from .models import ToolPlan, ToolPlanStep, MAX_PLAN_STEPS

# Tools whose numeric params the planner often emits as strings ("20 lakh", "8.5%")
_NUMERIC_PARAM_TOOLS = {"finance_calc", "tax_calc"}
_TEXT_PARAM_KEYS = {"regime", "tax_year", "age_category", "strategy", "direction", "name", "operation"}
_NUMBER_LIKE = re.compile(r"^\s*(?:₹|rs\.?|inr)?\s*-?[\d,]*\.?\d+\s*(?:%|k|l|lakh|lakhs|lac|lacs|cr|crore|crores)?\s*$", re.IGNORECASE)


def _default_repairs() -> int:
    try:
        return max(0, int(os.getenv("TORA_PLANNER_REPAIRS", "1")))
    except ValueError:
        return 1


def _coerce_number(value: Any) -> Any:
    if not isinstance(value, str) or not _NUMBER_LIKE.match(value):
        return value
    from ..context.extractor import parse_inr_amount

    text = value.strip().rstrip("%").strip()
    parsed = parse_inr_amount(text)
    if parsed is None:
        return value
    if text.lstrip("₹").strip().startswith("-"):
        parsed = -parsed
    return int(parsed) if float(parsed).is_integer() else parsed


def _coerce_params(obj: Any, key: Optional[str] = None) -> Any:
    if isinstance(obj, dict):
        return {k: _coerce_params(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_params(v, key) for v in obj]
    if key in _TEXT_PARAM_KEYS:
        return obj
    return _coerce_number(obj)


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
        max_repairs: Optional[int] = None,
    ):
        if not isinstance(tool_registry, ToolRegistry):
            raise TypeError(f"Expected ToolRegistry instance, got {type(tool_registry).__name__}")

        self.llm_provider = llm_provider
        self.tool_registry = tool_registry
        self.max_steps = max_steps
        self.max_repairs = _default_repairs() if max_repairs is None else max(0, max_repairs)

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

    def _plan_schema(self) -> Dict[str, Any]:
        """JSON schema passed to providers that support constrained decoding (Ollama `format`)."""
        return {
            "type": "object",
            "properties": {
                "thought": {"type": "string"},
                "requires_tools": {"type": "boolean"},
                "steps": {
                    "type": "array",
                    "maxItems": self.max_steps,
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool_name": {"type": "string", "enum": self.tool_registry.list_names()},
                            "arguments": {"type": "object"},
                        },
                        "required": ["tool_name", "arguments"],
                    },
                },
            },
            "required": ["thought", "requires_tools", "steps"],
        }

    @staticmethod
    def _normalise_payload(payload: Any) -> Any:
        """Repair common small-model deviations before strict validation."""
        if not isinstance(payload, dict):
            return payload
        payload = dict(payload)
        if "steps" not in payload:
            for alt in ("tool_calls", "tools", "plan"):
                if isinstance(payload.get(alt), list):
                    payload["steps"] = payload.pop(alt)
                    break
        if "steps" not in payload and ("tool_name" in payload or "tool" in payload):
            payload = {"thought": payload.get("thought"), "requires_tools": True, "steps": [payload]}
        steps = payload.get("steps")
        if isinstance(steps, list):
            fixed = []
            for step in steps:
                if not isinstance(step, dict):
                    fixed.append(step)
                    continue
                step = dict(step)
                if "tool_name" not in step:
                    for alt in ("tool", "name", "function"):
                        if isinstance(step.get(alt), str):
                            step["tool_name"] = step.pop(alt)
                            break
                if "arguments" not in step:
                    for alt in ("args", "parameters", "input", "params"):
                        if alt in step:
                            step["arguments"] = step.pop(alt)
                            break
                args = step.get("arguments")
                if isinstance(args, str):
                    try:
                        step["arguments"] = json.loads(args)
                    except (ValueError, TypeError):
                        pass
                args = step.get("arguments")
                if step.get("tool_name") in _NUMERIC_PARAM_TOOLS and isinstance(args, dict):
                    args = dict(args)
                    if "params" not in args:
                        extra = {k: v for k, v in args.items() if k != "operation"}
                        args = {"operation": args.get("operation"), "params": extra}
                    if isinstance(args.get("params"), str):
                        try:
                            args["params"] = json.loads(args["params"])
                        except (ValueError, TypeError):
                            pass
                    args["params"] = _coerce_params(args.get("params"))
                    step["arguments"] = args
                fixed.append(step)
            payload["steps"] = fixed
            if "requires_tools" not in payload:
                payload["requires_tools"] = bool(fixed)
        return payload

    def _check_plan(self, raw_plan: ToolPlan) -> Tuple[ToolPlan, Optional[str]]:
        """Validate a plan; returns (plan, error_message_or_None)."""
        plan = self._validate_and_sanitize_plan(raw_plan)
        if raw_plan.requires_tools and raw_plan.steps and not plan.requires_tools:
            return plan, (plan.thought or "invalid plan").replace("Plan rejected: ", "")
        return plan, None

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

        conversation = list(messages)
        last_error = "no plan produced"
        for attempt in range(self.max_repairs + 1):
            try:
                llm_response: LLMResponse = await self.llm_provider.generate(
                    messages=conversation,
                    model=model,
                    options={"temperature": 0.0, "format": self._plan_schema()},
                )
            except Exception as e:
                return ToolPlan(
                    requires_tools=False,
                    steps=[],
                    thought=f"Planning failed due to LLM provider error: {str(e)}",
                )

            payload = self._normalise_payload(self._extract_json_payload(llm_response.content or ""))
            if payload is None:
                last_error = "LLM response did not contain valid JSON plan."
            else:
                try:
                    raw_plan = ToolPlan.model_validate(payload)
                except Exception as e:
                    last_error = f"Plan failed schema validation: {str(e)}"
                else:
                    plan, error = self._check_plan(raw_plan)
                    if error is None:
                        if attempt:
                            plan.thought = (plan.thought or "") + f" (repaired after {attempt} retry)"
                        return plan
                    last_error = error

            if attempt < self.max_repairs:
                conversation = list(messages) + [
                    {"role": "assistant", "content": (llm_response.content or "")[:2000]},
                    {"role": "user", "content": (
                        f"That plan was invalid: {last_error[:600]}\n"
                        "Reply with corrected JSON only. Use exactly the tool names and argument names from "
                        "'Available Registered Tools'. If no tool fits, reply "
                        '{"thought": "...", "requires_tools": false, "steps": []}.'
                    )},
                ]

        if last_error.startswith(("LLM response did not", "Plan failed schema")):
            return ToolPlan(requires_tools=False, steps=[], thought=last_error)
        return ToolPlan(requires_tools=False, steps=[], thought=f"Plan rejected: {last_error}")
