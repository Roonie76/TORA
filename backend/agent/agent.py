import json
import logging
import os
import re
from typing import Optional, List, Dict, Any, Tuple, Union
from dataclasses import dataclass

from ..llm.base import (
    LLMProvider,
    LLMResponse,
    LLMResponseError,
    LLMProviderError,
)
from ..prompts.tora import TORA_SYSTEM_PROMPT, sections_for as prompt_sections_for, slice_prompt
from ..planner.models import ToolPlan
from ..planner.planner import Planner
from ..answer import slots as answer_slots
from ..planner.fast_path import Complexity, assess_complexity, document_plan, fast_plan, profile_plan
from ..planner.tool_filter import needs_no_tools
from ..tools.executor import ToolExecutor
from ..state.intent import Intent, IntentClassifier, IntentResult
from ..state.conversation_state import ConversationState
from ..context.extractor import extract_memory_commands
from ..context.llm_extractor import propose_facts, should_try as should_try_llm_extraction
from ..observability import TraceTimer, current_trace
from ..observability import progress
from ..verify import build_evidence, caveat_note, correction_instruction, verify_answer
from ..context import (
    ConversationContext,
    ConversationMessage,
    UserContext,
    FinancialProfile,
    FinancialContext,
    FactExtractor,
    FactManager,
    ConversationSummarizer,
    TokenBudgetManager,
    estimate_messages_tokens,
    KnowledgeContext,
    ToolContext,
    ContextBuilder,
    MAX_HISTORY_MESSAGES,
)

logger = logging.getLogger("tora.agent")

# Maximum sequential tool steps allowed per turn
MAX_TOOL_STEPS: int = 3
# Maximum automatic context-reduction retries on length overflow
MAX_OVERFLOW_RETRIES: int = 1
GROUNDING_MODES = ("off", "annotate", "regenerate")
STRICT_PERCENT_INTENTS = (Intent.RESEARCH, Intent.RESEARCH_FOLLOWUP, Intent.COMPARISON)


def _grounding_mode() -> str:
    mode = os.getenv("TORA_GROUNDING_MODE", "regenerate").strip().lower()
    return mode if mode in GROUNDING_MODES else "regenerate"




def _tool_summary(tool_result: Any) -> Optional[str]:
    """One short line for the UI chip (e.g. the engine's own summary)."""
    if not tool_result.success:
        return (tool_result.error or "failed")[:160]
    data = tool_result.data
    if isinstance(data, dict):
        if data.get("summary"):
            return str(data["summary"])[:220]
        if data.get("rules"):
            first = data["rules"][0]
            return f"{first.get('title')} — {first.get('citation')}"[:220]
        if "result" in data:
            return f"{data.get('expression', '')} = {data['result']}"[:220]
    return None

def _max_answer_tokens() -> int:
    """Optional cap on answer length (TORA_MAX_ANSWER_TOKENS); useful on CPU where
    generation runs at a few tokens per second. 0 / unset = no cap."""
    try:
        value = int(os.getenv("TORA_MAX_ANSWER_TOKENS", "0").strip() or 0)
    except ValueError:
        return 0
    return value if value >= 64 else 0



def _account_note() -> str:
    """Tell the answer model whether Spendsy records can exist for this user (Phase 6)."""
    from ..auth import auth_mode, current_identity

    if auth_mode() == "off":
        return ""
    if current_identity() is not None:
        return ("\n\n## Account\n- The user is signed in to Spendsy. Their recorded transactions are only what "
                "'spendsy_data' results show; if there is no such result, do not guess their spending.")
    return ("\n\n## Account\n- The user is not signed in, so you cannot see their Spendsy transactions. If they ask "
            "about their recorded spending, say they need to sign in to Spendsy, or offer to work with figures they share.")


# "forget my X" where nothing matching is stored: the answer must not claim a deletion.
_FORGET_REQUEST = re.compile(
    r"\b(?:forget|delete|remove|erase|wipe|clear)\b[^?.!]{0,40}\b(?:my|the|about\s+me)\b",
    re.IGNORECASE,
)


def _empty_memory_note(intent: Any, profile: Any) -> str:
    """Asked to recall something with nothing stored: say so, never invent a figure."""
    if getattr(intent, "intent", None) != Intent.MEMORY_RECALL:
        return ""
    if not isinstance(profile, FinancialProfile) or not profile.is_empty():
        return ""
    return ("\n\n## Memory\n- Nothing at all is recorded for this user yet. Say plainly that you do not have that "
            "figure and ask for it. Never state a number for a fact you were not given, not even as an example.")


def _negative_amount_note(message: str) -> str:
    """A negative figure is a typo or a misunderstanding; nothing was stored, so don't confirm it."""
    from ..context.extractor import negated_amounts

    if not negated_amounts(message or ""):
        return ""
    return ("\n\n## Figures\n- The message contains a negative amount, so nothing was recorded from it. Ask the "
            "user what they meant (for example an expense, a loss, or a typo) instead of confirming the figure or "
            "treating it as a positive number.")


def _forget_note(message: str, memory_commands: List[Dict[str, Any]], profile: Any = None) -> str:
    """Nothing to delete — either the words matched no fact, or the named facts were never stored."""
    if not _FORGET_REQUEST.search(message or ""):
        return ""
    targets = [c["name"] for c in memory_commands if c.get("action") == "delete" and c.get("name")]
    if any(c.get("action") == "clear" for c in memory_commands):
        return ""
    if targets and isinstance(profile, FinancialProfile):
        stored = {f.name for f in profile.iter_current_facts()}
        if any(name in stored for name in targets):
            return ""
    elif targets:
        return ""
    return ("\n\n## Memory\n- The user asked you to forget something, but nothing matching it is stored. Say you "
            "have nothing recorded for it and ask what exactly to remove. Never claim to have deleted or removed "
            "anything.")


_SMALL_TALK = re.compile(
    r"^\s*(?:hi+|hey+|hello+|namaste|good\s+(?:morning|afternoon|evening|night)|thanks?(?:\s+you)?|thank\s+you|"
    r"ok(?:ay)?|cool|great|bye|see\s+you|who\s+are\s+you|what\s+can\s+you\s+do|how\s+are\s+you)"
    r"(?:[\s,!.?]+(?:tora|there|so\s+much|a\s+lot|how\s+are\s+you(?:\s+doing)?|doing|today))*[\s!.?]*$",
    re.IGNORECASE,
)


# "Hi TORA! I'm Ravi, 32, working in Pune." A greeting plus a short introduction.
_INTRODUCTION = re.compile(
    r"^\s*(?:(?:hi+|hey+|hello+|namaste)(?:[\s,!.]+tora)?[\s,!.]+)?"
    r"(?:i'?m|i\s+am|my\s+name\s+is|this\s+is)\s+[a-z]+(?:\s+[a-z]+)?"
    r"(?:(?:[\s,.]+|\s+and\s+)(?:\d{2}(?:\s*(?:years?|yrs?)(?:\s+old)?)?"
    r"|(?:i\s+)?(?:am\s+)?(?:work(?:ing)?|live|living|based|from|staying)(?:\s+(?:in|at|as|from|for))?\s+[a-z][a-z .&-]{0,40}?))*"
    r"[\s!.]*$",
    re.IGNORECASE,
)
_FINANCE_WORDS = re.compile(
    r"\b(?:debts?|loans?|emis?|tax|salary|income|earn|invest\w*|sips?|cards?|money|rent|sav\w*|budget|"
    r"stress\w*|broke|lakh|crore|spend\w*|bills?|dues?|insurance|funds?|stocks?)\b|₹|\d{3,}",
    re.IGNORECASE,
)


def _is_small_talk(message: str, intent: Any) -> bool:
    """Greetings, thanks and introductions never need tools; skipping the planner saves a full model call."""
    if intent.intent != Intent.GENERAL_QA:
        return False
    text = message or ""
    if _SMALL_TALK.match(text):
        return True
    return len(text) <= 120 and not _FINANCE_WORDS.search(text) and bool(_INTRODUCTION.match(text))


def _complex_think() -> bool:
    return os.getenv("TORA_COMPLEX_THINK", "off").strip().lower() in ("1", "on", "true", "yes")


_DEBT_WORDS = re.compile(r"\b(?:debt|debts|loan|loans|emi|card|cards|overdue|default|collection|recovery|"
                         r"minimum\s+due|interest|repay\w*|borrow\w*)\b", re.IGNORECASE)


def _prompt_slicing_enabled() -> bool:
    """Phase 15: send only the prompt sections a turn can use (TORA_PROMPT_SLICING=off to disable)."""
    return os.getenv("TORA_PROMPT_SLICING", "on").strip().lower() not in ("0", "off", "false", "no")


def _locked_slots_enabled() -> bool:
    """Phase 15: the engine owns every figure in the answer (TORA_LOCKED_SLOTS=off to disable)."""
    return os.getenv("TORA_LOCKED_SLOTS", "on").strip().lower() not in ("0", "off", "false", "no")


def _fast_path_enabled() -> bool:
    return os.getenv("TORA_FAST_PATH", "on").strip().lower() not in ("0", "off", "false", "no")


def _case_note(complexity: Optional[Complexity]) -> str:
    """Scale the answer style with the case (Phase 7)."""
    if complexity is None or not complexity.is_complex:
        return ""
    return ("\n\n## This Case\n- This is a multi-factor decision. Work the way an experienced chartered accountant would "
            "(but never claim to be a CA or a professional): state the key facts you are "
            "using, compare two or three realistic options with their rupee impact (use only tool figures or clearly "
            "labelled assumptions), recommend one with the reason, name the main risk, and list what the user should "
            "confirm. Ask for any missing fact that would change the recommendation.")

@dataclass

class AgentResponse:
    content: str
    model: str
    done: bool = True
    raw: Optional[Dict[str, Any]] = None
    plan: Optional[ToolPlan] = None
    tool_context: Optional[ToolContext] = None
    financial_profile: Optional[FinancialProfile] = None
    intent: Optional[IntentResult] = None
    grounding: Optional[Dict[str, Any]] = None
    complexity: Optional[Any] = None


class ToraAgent:
    """
    Orchestration layer for TORA (Spendsy's Personal AI Assistant).
    Coordinates token-aware context assembly, structured financial profile tracking,
    fact extraction, conversation summarization, autonomous tool planning,
    deterministic execution, and 1-shot context overflow recovery.
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        default_system_prompt: Optional[str] = None,
        max_history_messages: int = MAX_HISTORY_MESSAGES,
        planner: Optional[Planner] = None,
        tool_executor: Optional[ToolExecutor] = None,
        max_tool_steps: int = MAX_TOOL_STEPS,
        token_budget_manager: Optional[TokenBudgetManager] = None,
    ):
        self.llm_provider = llm_provider
        self.default_system_prompt = default_system_prompt or TORA_SYSTEM_PROMPT
        self.max_history_messages = max_history_messages
        self._planner = planner
        self._tool_executor = tool_executor
        self.max_tool_steps = max_tool_steps
        self.token_budget_manager = token_budget_manager or TokenBudgetManager()
        self.context_builder = ContextBuilder(
            default_system_prompt=self.default_system_prompt,
            max_history=self.max_history_messages,
            token_budget_manager=self.token_budget_manager,
        )
        self.global_financial_profile = FinancialProfile()

    @property
    def provider(self) -> LLMProvider:
        """Access the underlying LLMProvider."""
        return self.llm_provider

    @property
    def planner(self) -> Optional[Planner]:
        """Access the configured Planner instance (if any)."""
        return self._planner

    @property
    def tool_executor(self) -> Optional[ToolExecutor]:
        """Access the configured ToolExecutor instance (if any)."""
        return self._tool_executor

    async def plan(
        self,
        message: str,
        context: Optional[ConversationContext] = None,
        model: Optional[str] = None,
    ) -> Optional[ToolPlan]:
        """Generate a validated ToolPlan using the configured Planner (if available)."""
        if self._planner is None:
            return None
        return await self._planner.plan(message=message, context=context, model=model)

    def _build_messages(
        self,
        user_message: str,
        context: Optional[ConversationContext] = None,
        user_context: Optional[UserContext] = None,
        financial_context: Optional[Union[FinancialProfile, FinancialContext]] = None,
        knowledge_context: Optional[KnowledgeContext] = None,
        tool_context: Optional[ToolContext] = None,
        system_prompt: Optional[str] = None,
        summary: Optional[str] = None,
        conversation_state: Optional[ConversationState] = None,
        current_turn: Optional[int] = None,
        turn_notes: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Construct the complete LLM message payload using the ContextBuilder."""
        return self.context_builder.build(
            current_message=user_message,
            context=context,
            user_context=user_context,
            financial_context=financial_context,
            knowledge_context=knowledge_context,
            tool_context=tool_context,
            system_prompt=system_prompt,
            turn_notes=turn_notes,
            summary=summary,
            conversation_state=conversation_state,
            current_turn=current_turn,
        )

    async def run(
        self,
        message: str,
        context: Optional[ConversationContext] = None,
        user_context: Optional[UserContext] = None,
        financial_context: Optional[Union[FinancialProfile, FinancialContext]] = None,
        knowledge_context: Optional[KnowledgeContext] = None,
        tool_context: Optional[ToolContext] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        conversation_state: Optional[ConversationState] = None,
    ) -> AgentResponse:
        """
        Execute an agent turn with fact extraction, token-aware context assembly,
        autonomous tool planning, deterministic execution, and 1-shot context overflow recovery.
        """
        if not message or not message.strip():
            raise ValueError("Message cannot be empty.")

        # 1. Fact Extraction: Extract candidate facts and update active profile
        # A fresh request-scoped profile is used when none is supplied. The agent is a
        # process-wide singleton, so falling back to shared state would leak one user's
        # facts into another user's conversation.
        active_profile = financial_context if financial_context is not None else FinancialProfile()
        turn: Optional[int] = conversation_state.begin_turn() if conversation_state is not None else None
        candidates: List[Dict[str, Any]] = []
        if isinstance(active_profile, FinancialProfile):
            candidates = FactExtractor.extract_candidate_facts(message, profile=active_profile)
            if candidates:
                FactManager.apply_candidates(active_profile, candidates, turn=turn)
                logger.info(
                    "Fact extraction processed %d candidate facts (Profile facts: %d).",
                    len(candidates),
                    len(active_profile.history),
                )

        # 1a. Model-assisted extraction when the rules found nothing in a money statement
        model_facts: List[Dict[str, Any]] = []
        if isinstance(active_profile, FinancialProfile) and should_try_llm_extraction(message, candidates):
            model_facts = await propose_facts(self.llm_provider, message, model=model)
            if model_facts:
                FactManager.apply_candidates(active_profile, model_facts, turn=turn)
                candidates = list(model_facts)
                logger.info("Model-assisted extraction accepted %d fact(s).", len(model_facts))

        # 1b. Intent classification + topic tracking (conversation-aware)
        memory_commands = [c for c in candidates if c.get("action") or c.get("closure")]
        if not memory_commands and not candidates:
            memory_commands = extract_memory_commands(message)
        intent = IntentClassifier.classify(
            message,
            state=conversation_state,
            memory_commands=memory_commands,
            extracted_facts=[c for c in candidates if not c.get("action")],
        )
        if conversation_state is not None:
            conversation_state.observe(message, intent)
        trace = current_trace()
        progress.emit("stage", stage="remembering" if intent.intent in (
            Intent.MEMORY_UPDATE, Intent.MEMORY_DELETE) else "understanding", intent=intent.intent.value)
        if trace is not None:
            trace.intent = intent.intent.value
            trace.is_followup = intent.is_followup
            trace.turn = turn
            trace.model_facts = len(model_facts)
        logger.info(
            "Intent=%s followup=%s entities=%s topic=%s",
            intent.intent.value, intent.is_followup, intent.entities,
            conversation_state.active_topic_key if conversation_state is not None else None,
        )

        executed_plan: Optional[ToolPlan] = None
        effective_tool_context = tool_context or ToolContext()

        # 2. Autonomous Tool Planning & Execution Loop
        skip_planner = intent.skips_planner or (
            intent.intent == Intent.MEMORY_UPDATE and "?" not in message
        )
        if (
            intent.intent == Intent.RESEARCH_FOLLOWUP
            and conversation_state is not None
            and conversation_state.active_topic() is not None
            and conversation_state.active_topic().covers_entities(intent.entities)
        ):
            # Stored research already answers this follow-up; no new web calls.
            skip_planner = True

        if not skip_planner and _is_small_talk(message, intent):
            skip_planner = True

        # A turn that only reads back or records what TORA already knows needs no engine, and a
        # planner call costs ~95s of prompt reading on CPU. 19 of 35 live planner calls returned
        # "no tools needed" before this gate existed.
        if not skip_planner and needs_no_tools(
            message, intent, has_documents=bool(conversation_state is not None and conversation_state.documents)
        ):
            skip_planner = True
            if trace is not None:
                trace.planner["skipped_no_tools"] = True

        # Phase 7: clear-cut requests get a direct plan; effort scales with the case.
        fast: Optional[ToolPlan] = None
        if (
            not skip_planner
            and self._planner is not None
            and tool_context is None
            and _fast_path_enabled()
            and not intent.is_followup
            and intent.intent in (Intent.CALCULATION, Intent.FINANCIAL_QA, Intent.WHAT_IF,
                                  Intent.PLANNING, Intent.GENERAL_QA)
        ):
            usable = {t.name for t in self._planner._usable_tools()}
            candidate = fast_plan(message, intent, usable)
            if candidate is None:
                # "How do I get out of debt?" — build the plan from what TORA already knows,
                # so the months and interest come from the engine, not the model's arithmetic.
                candidate = profile_plan(message, intent, active_profile, usable)
            if candidate is None and conversation_state is not None and "tax_calc" in usable:
                candidate = document_plan(message, conversation_state.documents)
            if candidate is not None:
                checked = self._planner._validate_and_sanitize_plan(candidate)
                if checked.requires_tools and checked.steps:
                    fast = checked
        fact_count = (
            sum(1 for _ in active_profile.iter_current_facts())
            if isinstance(active_profile, FinancialProfile) else 0
        )
        complexity = assess_complexity(message, intent, fast=fast, known_fact_count=fact_count)
        if trace is not None:
            trace.complexity = {"level": complexity.level, "score": complexity.score}
        effort_model = model
        if complexity.is_complex and not model and os.getenv("TORA_COMPLEX_MODEL", "").strip():
            effort_model = os.getenv("TORA_COMPLEX_MODEL").strip()

        progress.emit("complexity", level=complexity.level)
        if fast is not None and self._tool_executor is not None:
            executed_plan = fast
            if trace is not None:
                trace.planner = {
                    "used": False,
                    "fast_path": True,
                    "requires_tools": True,
                    "steps": [st.tool_name for st in fast.steps],
                    "rewritten_query": False,
                }
        elif (
            self._planner is not None
            and self._tool_executor is not None
            and tool_context is None
            and not skip_planner
        ):
            logger.info("Initiating tool planning for user message.")
            progress.emit("stage", stage="planning")
            planner_message = intent.resolved_query or message
            known_facts = ""
            if isinstance(active_profile, FinancialProfile) and not active_profile.is_empty():
                known_facts = "\n".join(
                    f"- {f.name}: {f.value} ({f.period or 'n/a'})" for f in active_profile.iter_current_facts()
                )
            if conversation_state is not None and conversation_state.documents:
                doc_lines = []
                for d in conversation_state.documents[-2:]:
                    figures = {k: v for k, v in (d.get("summary") or {}).items()
                               if isinstance(v, (int, float, str)) and v not in (None, "")}
                    doc_lines.append(f"- uploaded {d.get('doc_type')}: {json.dumps(figures, ensure_ascii=False)[:600]}")
                known_facts = "\n".join(filter(None, [known_facts] + doc_lines))
            planner_timer = TraceTimer()
            executed_plan = await self._planner.plan(
                message=planner_message,
                context=context,
                intent=intent,
                model=effort_model,
                known_facts=known_facts,
            )
            if trace is not None:
                trace.planner = {
                    "used": True,
                    "ms": planner_timer.ms,
                    "requires_tools": executed_plan.requires_tools,
                    "steps": [st.tool_name for st in executed_plan.steps],
                    "rewritten_query": planner_message != message,
                }
            logger.info(
                "Planner decision: requires_tools=%s, steps_count=%d",
                executed_plan.requires_tools,
                len(executed_plan.steps),
            )

        if (
            executed_plan is not None and self._tool_executor is not None
            and executed_plan.requires_tools and executed_plan.steps
        ):
            steps_to_execute = executed_plan.steps[: self.max_tool_steps]
            if len(executed_plan.steps) > self.max_tool_steps:
                logger.warning(
                    "Plan steps (%d) exceeded MAX_TOOL_STEPS (%d); capping execution.",
                    len(executed_plan.steps),
                    self.max_tool_steps,
                )

            for step_idx, step in enumerate(steps_to_execute):
                logger.info("Executing tool step %d/%d: %s", step_idx + 1, len(steps_to_execute), step.tool_name)
                progress.emit("stage", stage=progress.tool_stage(step.tool_name))
                progress.emit("tool", status="running", name=step.tool_name,
                              label=progress.TOOL_LABELS.get(step.tool_name, step.tool_name),
                              operation=(step.arguments or {}).get("operation"))
                tool_result = await self._tool_executor.execute(
                    tool_name=step.tool_name,
                    arguments=step.arguments,
                    call_id=step.call_id or f"step_{step_idx + 1}",
                )
                effective_tool_context = self._tool_executor.to_tool_context(
                    tool_result=tool_result,
                    existing_context=effective_tool_context,
                )
                progress.emit("tool", status="done" if tool_result.success else "failed", name=step.tool_name,
                              label=progress.TOOL_LABELS.get(step.tool_name, step.tool_name),
                              operation=(step.arguments or {}).get("operation"),
                              summary=_tool_summary(tool_result))
                if trace is not None:
                    trace.tools.append({
                        "name": tool_result.tool_name,
                        "ok": tool_result.success,
                        "ms": tool_result.metadata.get("duration_ms"),
                        "error": None if tool_result.success else (tool_result.error or "")[:120],
                    })

        if conversation_state is not None:
            conversation_state.record_tool_results(effective_tool_context, query=intent.resolved_query or message)

        # 3. Build Initial Context
        locked_slots: Dict[str, str] = {}
        if _locked_slots_enabled():
            locked_slots = answer_slots.build_slots(effective_tool_context)

        tools_used, operations = [], []
        for result in (effective_tool_context.results if effective_tool_context else []):
            tools_used.append(result.tool_name)
            if isinstance(result.output, dict) and result.output.get("operation"):
                operations.append(str(result.output["operation"]))
        if system_prompt is None and _prompt_slicing_enabled():
            # Only the sections this turn can use: cheaper on CPU, and fewer competing rules for a
            # small model to hold at once. The slice is sticky per conversation — it only grows —
            # so the prompt stays byte-identical and the model's KV cache survives between turns.
            sticky = list(conversation_state.prompt_sections) if conversation_state is not None else []
            needed = prompt_sections_for(
                intent=intent.intent.value if hasattr(intent.intent, "value") else str(intent.intent),
                tools_used=tools_used,
                operations=operations,
                has_documents=bool(conversation_state is not None and conversation_state.documents),
                has_history=bool((context and context.messages) or (turn or 0) > 1),
                debt_context=bool(_DEBT_WORDS.search(message or "")),
            )
            combined = sorted(set(sticky) | set(needed))
            if conversation_state is not None:
                conversation_state.prompt_sections = combined
            system_prompt = slice_prompt(sticky=combined)
            if trace is not None:
                trace.prompt_sections = len(combined)
        account_note = (_account_note() + _case_note(complexity) + _forget_note(message, memory_commands, active_profile)
                        + _negative_amount_note(message) + _empty_memory_note(intent, active_profile)
                        + answer_slots.slot_table(locked_slots))
        # account_note is per-turn: it is passed as turn_notes so it lands after the stable
        # prompt instead of changing it (see ContextBuilder.build on prefix reuse).
        messages = self._build_messages(
            user_message=message,
            context=context,
            user_context=user_context,
            financial_context=active_profile,
            knowledge_context=knowledge_context,
            tool_context=effective_tool_context,
            system_prompt=system_prompt,
            turn_notes=account_note,
            conversation_state=conversation_state,
            current_turn=turn,
        )

        options: Dict[str, Any] = {}
        max_answer_tokens = _max_answer_tokens()
        if max_answer_tokens:
            options["num_predict"] = max_answer_tokens
        if temperature is not None:
            options["temperature"] = temperature
        if complexity.is_complex and _complex_think():
            options["think"] = True  # deeper reasoning only where the case needs it
        model = effort_model

        # Observability logging (metadata only, no private values)
        input_token_est = estimate_messages_tokens(messages)
        if trace is not None:
            trace.prompt_tokens_estimate = input_token_est
        logger.info(
            "Context constructed: estimated_input_tokens=%d, message_count=%d, has_financial_profile=%s, has_tools=%s",
            input_token_est,
            len(messages),
            not active_profile.is_empty() if hasattr(active_profile, "is_empty") else False,
            not effective_tool_context.is_empty(),
        )

        # 4. LLM Generation with 1-Shot Context Overflow Recovery
        streamed_parts: List[str] = []
        final_messages = messages
        try:
            progress.emit("stage", stage="writing")
            answer_options = dict(options)
            streamed = False
            if progress.active():
                async def _forward(text: str) -> None:
                    nonlocal streamed
                    streamed = True
                    streamed_parts.append(text)
                    await progress.emit_token(text)

                answer_options["on_token"] = _forward
            llm_response: LLMResponse = await self.llm_provider.generate(
                messages=messages,
                model=model,
                options=answer_options if answer_options else None,
            )
            if progress.active() and not streamed and llm_response.content:
                streamed_parts.append(llm_response.content)
                await progress.emit_token(llm_response.content)  # providers without streaming
        except LLMResponseError as e:
            detail_text = str(e.detail or "").lower()
            message_text = str(e).lower()
            # Only genuine context-window exhaustion triggers the reduction retry; a bare
            # "length" substring (e.g. "Content-Length") must not.
            is_length_overflow = (
                "done_reason=length" in detail_text
                or "context length" in message_text
                or "context window" in message_text
                or "context length" in detail_text
            )
            if is_length_overflow:
                logger.warning(
                    "Context-window exhaustion (done_reason=length) detected. Initiating 1-shot context reduction..."
                )
                # Controlled context reduction: compact history to most recent 4 messages + summary
                reduced_messages = []
                if context and len(context) > 4:
                    older_msgs = context.get_messages()[:-4]
                    summary_text = ConversationSummarizer.summarize_messages(
                        messages=older_msgs,
                        profile=active_profile if isinstance(active_profile, FinancialProfile) else None,
                    )
                    compact_context = ConversationContext(messages=context.get_messages()[-4:])
                    reduced_messages = self._build_messages(
                        user_message=message,
                        context=compact_context,
                        user_context=user_context,
                        financial_context=active_profile,
                        knowledge_context=knowledge_context,
                        tool_context=effective_tool_context,
                        system_prompt=system_prompt,
                        summary=summary_text,
                        conversation_state=conversation_state,
                        current_turn=turn,
                    )
                else:
                    # If context was already short, drop tool context if large
                    reduced_messages = self._build_messages(
                        user_message=message,
                        context=context,
                        user_context=user_context,
                        financial_context=active_profile,
                        knowledge_context=knowledge_context,
                        tool_context=None,
                        system_prompt=system_prompt,
                        conversation_state=conversation_state,
                        current_turn=turn,
                    )

                logger.info(
                    "Retrying generation with reduced context (%d messages, ~%d tokens).",
                    len(reduced_messages),
                    estimate_messages_tokens(reduced_messages),
                )
                # Retry generation ONCE
                if trace is not None:
                    trace.overflow_retry = True
                final_messages = reduced_messages
                llm_response = await self.llm_provider.generate(
                    messages=reduced_messages,
                    model=model,
                    options=options if options else None,
                )
                logger.info("1-shot context reduction recovery succeeded.")
            else:
                raise

        logger.info("Agent response generated successfully with model '%s'.", llm_response.model)

        # 5a. Locked slots (Phase 15): placeholders become the engine's own figures, and a
        # number the model typed that matches no slot earns one rewrite.
        content = llm_response.content
        slots_report: Optional[Dict[str, Any]] = None
        if locked_slots and content:
            content, slots_report = await self._apply_slots(
                content=content,
                slots=locked_slots,
                messages=final_messages,
                model=model,
                options=options,
                profile=active_profile,
            )
            if trace is not None:
                trace.slots = slots_report

        # 5b. Numeric grounding verification (Phase 4B)
        grounding: Optional[Dict[str, Any]] = None
        mode = _grounding_mode()
        if mode != "off" and content:
            progress.emit("stage", stage="checking")
            content, grounding = await self._ground(
                content=content,
                message=message,
                context=context,
                profile=active_profile,
                tool_context=effective_tool_context,
                conversation_state=conversation_state,
                intent=intent,
                messages=final_messages,
                model=model,
                options=options,
                mode=mode,
            )

        if progress.active() and content != "".join(streamed_parts):
            # The checked / recovered answer differs from what was streamed: show the final text.
            progress.emit("replace", text=content, reason=(grounding or {}).get("action") or "final")

        if trace is not None and grounding is not None:
            trace.grounding = {
                "checked": grounding.get("checked"),
                "unsupported": len(grounding.get("unsupported", [])),
                "action": grounding.get("action"),
            }

        return AgentResponse(
            content=content,
            model=llm_response.model,
            done=llm_response.done,
            raw=llm_response.raw,
            plan=executed_plan,
            tool_context=effective_tool_context if not effective_tool_context.is_empty() else None,
            financial_profile=active_profile if isinstance(active_profile, FinancialProfile) else None,
            intent=intent,
            grounding=grounding,
            complexity=complexity,
        )

    async def _apply_slots(
        self,
        content: str,
        slots: Dict[str, str],
        messages: List[Dict[str, str]],
        model: Optional[str],
        options: Dict[str, Any],
        profile: Any = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Replace {{slot}} with the engine's figures; ask for one rewrite if a figure was invented."""
        rendered, unknown = answer_slots.render(content, slots)
        known_extra = [f.format_value() for f in profile.iter_current_facts()] if isinstance(profile, FinancialProfile) else []
        stray = answer_slots.stray_numbers(rendered, slots, extra_allowed=known_extra)
        report: Dict[str, Any] = {
            "offered": len(slots),
            "used": len(answer_slots._PLACEHOLDER.findall(content)),
            "unknown": unknown,
            "stray": stray,
            "action": "none",
        }
        if not stray and not unknown:
            return rendered, report

        logger.info("Locked slots: %d stray figure(s) %s, %d unknown placeholder(s) — rewriting once.",
                    len(stray), stray[:4], len(unknown))
        corrected = [dict(m) for m in messages]
        corrected[0] = {
            "role": corrected[0]["role"],
            "content": corrected[0]["content"] + "\n\n## Locked Figures Correction\n"
            + answer_slots.repair_instruction(stray, unknown, slots),
        }
        try:
            retry = await self.llm_provider.generate(messages=corrected, model=model,
                                                     options=options if options else None)
        except Exception as exc:  # noqa: BLE001 — keep the first answer if the rewrite fails
            logger.warning("Locked-slot rewrite failed: %s", exc)
            report["action"] = "rewrite_failed"
            return rendered, report
        if not retry.content:
            report["action"] = "rewrite_empty"
            return rendered, report
        second, second_unknown = answer_slots.render(retry.content, slots)
        second_stray = answer_slots.stray_numbers(second, slots, extra_allowed=known_extra)
        if len(second_stray) + len(second_unknown) < len(stray) + len(unknown):
            report.update({"action": "rewritten", "stray_after": second_stray, "unknown_after": second_unknown})
            return second, report
        # The rewrite was no better: keep the first answer and let grounding annotate it.
        report["action"] = "kept_first"
        return rendered, report

    async def _ground(
        self,
        content: str,
        message: str,
        context: Optional[ConversationContext],
        profile: Any,
        tool_context: ToolContext,
        conversation_state: Optional[ConversationState],
        intent: IntentResult,
        messages: List[Dict[str, str]],
        model: Optional[str],
        options: Dict[str, Any],
        mode: str,
    ):
        """Verify figures in the answer; regenerate once and/or annotate when unsupported."""
        user_texts = [message]
        other_texts: List[str] = []
        if context is not None:
            for m in context.messages:
                (user_texts if m.role == "user" else other_texts).append(m.content)
        if conversation_state is not None:
            other_texts.append(conversation_state.render_trusted())
            other_texts.append(conversation_state.render_external())
        evidence = build_evidence(
            user_texts=user_texts,
            profile=profile if isinstance(profile, FinancialProfile) else None,
            tool_context=tool_context,
            other_texts=other_texts,
        )
        strict = intent.intent in STRICT_PERCENT_INTENTS
        report = verify_answer(content, evidence, strict_percentages=strict)
        action = "none"
        if not report.ok and mode == "regenerate":
            corrected = [dict(m) for m in messages]
            corrected[0] = {
                "role": corrected[0]["role"],
                "content": corrected[0]["content"] + "\n\n## Grounding Correction\n" + correction_instruction(report),
            }
            try:
                retry = await self.llm_provider.generate(
                    messages=corrected, model=model, options=options if options else None
                )
                if retry.content:
                    second = verify_answer(retry.content, evidence, strict_percentages=strict)
                    logger.info("Grounding regeneration: before=%d unsupported, after=%d",
                                len(report.unsupported), len(second.unsupported))
                    content, report, action = retry.content, second, "regenerated"
            except Exception as exc:  # keep the first draft, annotate below
                logger.warning("Grounding regeneration failed: %s", exc)
        if not report.ok:
            content = content + caveat_note(report)
            action = "annotated" if action == "none" else "regenerated+annotated"
        result = report.to_dict()
        result["action"] = action
        result["mode"] = mode
        return content, result
