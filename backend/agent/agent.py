import logging
import os
import re
from typing import Optional, List, Dict, Any, Union
from dataclasses import dataclass

from ..llm.base import (
    LLMProvider,
    LLMResponse,
    LLMResponseError,
    LLMProviderError,
)
from ..prompts.tora import TORA_SYSTEM_PROMPT
from ..planner.models import ToolPlan
from ..planner.planner import Planner
from ..planner.fast_path import Complexity, assess_complexity, fast_plan
from ..tools.executor import ToolExecutor
from ..state.intent import Intent, IntentClassifier, IntentResult
from ..state.conversation_state import ConversationState
from ..context.extractor import extract_memory_commands
from ..context.llm_extractor import propose_facts, should_try as should_try_llm_extraction
from ..observability import TraceTimer, current_trace
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


_SMALL_TALK = re.compile(
    r"^\s*(?:hi+|hey+|hello+|namaste|good\s+(?:morning|afternoon|evening|night)|thanks?(?:\s+you)?|thank\s+you|"
    r"ok(?:ay)?|cool|great|bye|see\s+you|who\s+are\s+you|what\s+can\s+you\s+do|how\s+are\s+you)"
    r"(?:[\s,!.?]+(?:tora|there|so\s+much|a\s+lot|how\s+are\s+you(?:\s+doing)?|doing|today))*[\s!.?]*$",
    re.IGNORECASE,
)


def _is_small_talk(message: str, intent: Any) -> bool:
    """Greetings and thanks never need tools; skipping the planner saves a full model call."""
    return intent.intent == Intent.GENERAL_QA and bool(_SMALL_TALK.match(message or ""))


def _complex_think() -> bool:
    return os.getenv("TORA_COMPLEX_THINK", "off").strip().lower() in ("1", "on", "true", "yes")


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
            candidate = fast_plan(message, intent, {t.name for t in self._planner._usable_tools()})
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
            planner_message = intent.resolved_query or message
            known_facts = ""
            if isinstance(active_profile, FinancialProfile) and not active_profile.is_empty():
                known_facts = "\n".join(
                    f"- {f.name}: {f.value} ({f.period or 'n/a'})" for f in active_profile.iter_current_facts()
                )
            planner_timer = TraceTimer()
            executed_plan = await self._planner.plan(
                message=planner_message,
                context=context,
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
                tool_result = await self._tool_executor.execute(
                    tool_name=step.tool_name,
                    arguments=step.arguments,
                    call_id=step.call_id or f"step_{step_idx + 1}",
                )
                effective_tool_context = self._tool_executor.to_tool_context(
                    tool_result=tool_result,
                    existing_context=effective_tool_context,
                )
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
        account_note = _account_note() + _case_note(complexity)
        if account_note:
            system_prompt = (system_prompt or self.default_system_prompt) + account_note
        messages = self._build_messages(
            user_message=message,
            context=context,
            user_context=user_context,
            financial_context=active_profile,
            knowledge_context=knowledge_context,
            tool_context=effective_tool_context,
            system_prompt=system_prompt,
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
        final_messages = messages
        try:
            llm_response: LLMResponse = await self.llm_provider.generate(
                messages=messages,
                model=model,
                options=options if options else None,
            )
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

        # 5. Numeric grounding verification (Phase 4B)
        content = llm_response.content
        grounding: Optional[Dict[str, Any]] = None
        mode = _grounding_mode()
        if mode != "off" and content:
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
