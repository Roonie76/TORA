import os
import time
import threading
from collections import defaultdict, deque
from typing import Optional, List, Dict, Any, Deque
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv

from .llm import (
    OllamaProvider,
    LLMConnectionError,
    LLMTimeoutError,
    LLMResponseError,
    LLMProviderError,
)
from .context import (
    ConversationContext,
    MessageRole,
    FinancialProfile,
    FactExtractor,
    FactManager,
)
from .tools import ToolRegistry, CalculatorTool, WebSearchTool, WebFetchTool, ResearchTool, FinanceCalcTool, ToolExecutor
from .planner import Planner
from .agent import ToraAgent
from .state import SessionStore, is_valid_conversation_id

load_dotenv()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


# Request limits (configurable via environment)
MAX_MESSAGE_CHARS: int = _env_int("TORA_MAX_MESSAGE_CHARS", 8000)
MAX_HISTORY_ITEMS: int = _env_int("TORA_MAX_HISTORY_ITEMS", 200)
MAX_MODEL_NAME_CHARS: int = 128
TOOL_TIMEOUT_SECONDS: float = _env_float("TORA_TOOL_TIMEOUT_SECONDS", 30.0)
RATE_LIMIT_PER_MINUTE: int = _env_int("TORA_RATE_LIMIT_PER_MINUTE", 30)
DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173"
CORS_ORIGINS: List[str] = [
    o.strip() for o in os.getenv("TORA_CORS_ORIGINS", DEFAULT_CORS_ORIGINS).split(",") if o.strip()
]


class SlidingWindowRateLimiter:
    """Small in-process per-client limiter (single-worker deployments)."""

    def __init__(self, limit_per_minute: int, window_seconds: float = 60.0):
        self.limit = limit_per_minute
        self.window = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


rate_limiter = SlidingWindowRateLimiter(RATE_LIMIT_PER_MINUTE)

DEFAULT_SESSION_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".data", "tora_sessions.db")
session_store = SessionStore(
    path=os.getenv("TORA_SESSION_DB", DEFAULT_SESSION_DB),
    ttl_days=_env_int("TORA_SESSION_TTL_DAYS", 30),
)

# Composition root: Instantiate tools, registry, executor, planner, provider, and agent
tool_registry = ToolRegistry()
tool_registry.register(CalculatorTool())
tool_registry.register(WebSearchTool())
tool_registry.register(WebFetchTool())
tool_registry.register(ResearchTool())
tool_registry.register(FinanceCalcTool())

tool_executor = ToolExecutor(registry=tool_registry, default_timeout_seconds=TOOL_TIMEOUT_SECONDS)
llm_provider = OllamaProvider()
planner = Planner(llm_provider=llm_provider, tool_registry=tool_registry)

agent = ToraAgent(
    llm_provider=llm_provider,
    planner=planner,
    tool_executor=tool_executor,
)

app = FastAPI(
    title="Spendsy TORA AI Backend",
    description="FastAPI gateway for Spendsy TORA AI",
    version="1.0.0"
)

# CORS: explicit origin allow-list (TORA_CORS_ORIGINS). The chat API uses no cookies,
# so credentials are not allowed; the Vite dev proxy makes browser calls same-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)


class MessageItem(BaseModel):
    role: str = Field(..., max_length=32)
    content: str = Field(..., max_length=MAX_MESSAGE_CHARS)


class ChatRequest(BaseModel):
    conversation_id: Optional[str] = Field(default=None, max_length=64)
    message: Optional[str] = Field(default=None, max_length=MAX_MESSAGE_CHARS)
    messages: Optional[List[MessageItem]] = Field(default=None, max_length=MAX_HISTORY_ITEMS)
    model: Optional[str] = Field(default=None, max_length=MAX_MODEL_NAME_CHARS, pattern=r"^[A-Za-z0-9._:/-]+$")
    temperature: Optional[float] = Field(default=0.7, ge=0.0, le=2.0)


class ChatResponse(BaseModel):
    response: str
    model: str
    done: bool = True
    conversation_id: Optional[str] = None
    intent: Optional[str] = None


@app.get("/")
@app.get("/api/health")
async def health_check():
    """Health check for FastAPI and LLM provider connectivity."""
    health_data = await agent.provider.health_check()
    return {
        "status": "ok",
        "service": "spendsy-fastapi-backend",
        "ollama": health_data,
    }


@app.get("/api/models")
async def list_models():
    """List available models from the LLM provider."""
    models = await agent.provider.list_models()
    return {"models": models, "default": agent.provider.default_model}


@app.post("/api/chat", response_model=ChatResponse)
@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, http_request: Request):
    """
    Core Chat Endpoint:
    Validates request -> Builds Context -> ToraAgent -> LLMProvider -> Response
    """
    client_key = http_request.client.host if http_request.client else "unknown"
    if not rate_limiter.allow(client_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please wait a moment and try again.",
        )
    user_prompt: Optional[str] = request.message
    context: Optional[ConversationContext] = None

    # Process conversation history if provided
    if request.messages is not None:
        if not isinstance(request.messages, list):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="'messages' must be a list of message objects.",
            )

        raw_list: List[Dict[str, str]] = []
        for idx, item in enumerate(request.messages):
            clean_role = item.role.strip().lower() if item.role else ""
            if clean_role not in MessageRole.valid_client_roles():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid message role '{item.role}' at index {idx}. Only 'user' and 'assistant' roles are allowed in history.",
                )
            if not item.content or not item.content.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Message content at index {idx} cannot be empty.",
                )
            raw_list.append({"role": clean_role, "content": item.content.strip()})

        # If explicit 'message' is provided, 'messages' represents prior history
        if user_prompt and user_prompt.strip():
            try:
                context = ConversationContext.from_list(raw_list, allowed_roles=MessageRole.valid_client_roles())
            except ValueError as e:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        else:
            # If no separate 'message' field, extract the last user message from 'messages'
            if not raw_list:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Message history cannot be empty without a prompt.",
                )
            last_item = raw_list[-1]
            if last_item["role"] != MessageRole.USER.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="The last message in 'messages' must be from the 'user' role when no 'message' field is provided.",
                )
            user_prompt = last_item["content"]
            prior_history = raw_list[:-1]
            if prior_history:
                try:
                    context = ConversationContext.from_list(prior_history, allowed_roles=MessageRole.valid_client_roles())
                except ValueError as e:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # Validate that we have a non-empty user prompt
    if not user_prompt or not user_prompt.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message prompt cannot be empty."
        )

    try:
        # Stateful path: the server owns history, memory and conversation state.
        if request.conversation_id is not None or request.messages is None:
            return await _run_stateful_turn(request, user_prompt.strip())

        # Legacy stateless path (client supplies history): memory is rebuilt per request.
        financial_profile = FinancialProfile()
        if context:
            for msg in context.messages:
                if msg.role == MessageRole.USER.value:
                    cands = FactExtractor.extract_candidate_facts(msg.content, profile=financial_profile)
                    if cands:
                        FactManager.apply_candidates(financial_profile, cands)

        agent_response = await agent.run(
            message=user_prompt.strip(),
            context=context,
            financial_context=financial_profile,
            model=request.model,
            temperature=request.temperature,
        )
        return ChatResponse(
            response=agent_response.content,
            model=agent_response.model,
            done=agent_response.done,
            intent=_intent_value(agent_response),
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except LLMConnectionError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        )
    except LLMTimeoutError as e:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(e),
        )
    except LLMResponseError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=e.detail or str(e),
        )
    except LLMProviderError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"LLM provider error: {str(e)}",
        )


def _intent_value(agent_response) -> Optional[str]:
    intent = getattr(agent_response, "intent", None)
    return intent.intent.value if intent is not None else None


def _load_session_or_404(conversation_id: str):
    if not is_valid_conversation_id(conversation_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    session = session_store.get(conversation_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return session


async def _run_stateful_turn(request: ChatRequest, prompt: str) -> ChatResponse:
    """
    One turn of a server-side conversation. History, memory and topic state come from
    the store; client-supplied `messages` are ignored so earlier turns cannot be forged.
    Nothing is persisted unless the turn succeeds.
    """
    if request.conversation_id is None:
        session = session_store.create()
    else:
        session = _load_session_or_404(request.conversation_id)

    async with session_store.lock_for(session.id):
        if request.conversation_id is not None:
            session = _load_session_or_404(session.id)  # reload under the lock
        history = session.history()
        context = (
            ConversationContext.from_list(history, allowed_roles=MessageRole.valid_client_roles())
            if history else None
        )
        agent_response = await agent.run(
            message=prompt,
            context=context,
            financial_context=session.profile,
            model=request.model,
            temperature=request.temperature,
            conversation_state=session.state,
        )
        intent_value = _intent_value(agent_response)
        meta = {"intent": intent_value} if intent_value else None
        plan = getattr(agent_response, "plan", None)
        if plan is not None and plan.requires_tools:
            meta = dict(meta or {})
            meta["tools"] = [step.tool_name for step in plan.steps]
        session.append_exchange(prompt, agent_response.content, turn=session.state.turn_count, meta=meta)
        session_store.save(session)

    return ChatResponse(
        response=agent_response.content,
        model=agent_response.model,
        done=agent_response.done,
        conversation_id=session.id,
        intent=intent_value,
    )


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    """Transcript, remembered facts and topic state for one conversation."""
    session = _load_session_or_404(conversation_id)
    return {
        "conversation_id": session.id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "turns": session.turns,
        "memory": session.profile.to_dict(),
        "memory_summary": session.profile.to_context_string(),
        "state": session.state.to_dict(),
    }


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    """Delete a conversation and everything remembered in it."""
    if not is_valid_conversation_id(conversation_id) or not session_store.delete(conversation_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return {"deleted": True}


@app.delete("/api/conversations/{conversation_id}/memory")
async def clear_conversation_memory(conversation_id: str):
    """Forget all remembered financial facts but keep the transcript."""
    session = _load_session_or_404(conversation_id)
    async with session_store.lock_for(session.id):
        session = _load_session_or_404(conversation_id)
        session.profile.clear(turn=session.state.turn_count)
        session_store.save(session)
    return {"cleared": True}


if __name__ == "__main__":
    import uvicorn
    # Loopback by default; set TORA_HOST=0.0.0.0 explicitly to expose on the network.
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("TORA_HOST", "127.0.0.1"),
        port=_env_int("TORA_PORT", 8000),
        reload=True,
    )
