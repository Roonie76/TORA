import logging
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
from ..tools.executor import ToolExecutor
from ..state.intent import Intent, IntentClassifier, IntentResult
from ..state.conversation_state import ConversationState
from ..context.extractor import extract_memory_commands
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

        if (
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
            executed_plan = await self._planner.plan(
                message=planner_message,
                context=context,
                model=model,
                known_facts=known_facts,
            )
            logger.info(
                "Planner decision: requires_tools=%s, steps_count=%d",
                executed_plan.requires_tools,
                len(executed_plan.steps),
            )

            if executed_plan.requires_tools and executed_plan.steps:
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

        if conversation_state is not None:
            conversation_state.record_tool_results(effective_tool_context, query=intent.resolved_query or message)

        # 3. Build Initial Context
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
        if temperature is not None:
            options["temperature"] = temperature

        # Observability logging (metadata only, no private values)
        input_token_est = estimate_messages_tokens(messages)
        logger.info(
            "Context constructed: estimated_input_tokens=%d, message_count=%d, has_financial_profile=%s, has_tools=%s",
            input_token_est,
            len(messages),
            not active_profile.is_empty() if hasattr(active_profile, "is_empty") else False,
            not effective_tool_context.is_empty(),
        )

        # 4. LLM Generation with 1-Shot Context Overflow Recovery
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
                llm_response = await self.llm_provider.generate(
                    messages=reduced_messages,
                    model=model,
                    options=options if options else None,
                )
                logger.info("1-shot context reduction recovery succeeded.")
            else:
                raise

        logger.info("Agent response generated successfully with model '%s'.", llm_response.model)

        return AgentResponse(
            content=llm_response.content,
            model=llm_response.model,
            done=llm_response.done,
            raw=llm_response.raw,
            plan=executed_plan,
            tool_context=effective_tool_context if not effective_tool_context.is_empty() else None,
            financial_profile=active_profile if isinstance(active_profile, FinancialProfile) else None,
            intent=intent,
        )
