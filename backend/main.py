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
from .tools import (
    ToolRegistry, CalculatorTool, WebSearchTool, WebFetchTool, ResearchTool, FinanceCalcTool, TaxCalcTool,
    SpendsyDataTool, RulesLookupTool, ToolExecutor,
)
from .auth import (
    AuthError,
    AuthUnavailableError,
    GatewayAuthVerifier,
    Identity,
    auth_mode,
    extract_token,
    reset_current_identity,
    set_current_identity,
)
from .planner import Planner
from .agent import ToraAgent
from .state import SessionStore, is_valid_conversation_id
from .observability import TelemetryHub, conversation_ref, start_trace

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
telemetry = TelemetryHub(trace_file=os.getenv("TORA_TRACE_FILE") or None)
DEBUG_ENDPOINTS = os.getenv("TORA_DEBUG_ENDPOINTS", "").strip().lower() in ("1", "true", "yes")

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
tool_registry.register(TaxCalcTool())
tool_registry.register(RulesLookupTool())
# Only offered to the planner for signed-in users (see Planner._usable_tools).
tool_registry.register(SpendsyDataTool())

# Phase 6A: identity comes from the Spendsy auth service (TORA_AUTH_MODE / TORA_AUTH_URL).
auth_verifier = GatewayAuthVerifier()

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

# CORS: explicit origin allow-list (TORA_CORS_ORIGINS). Browsers send the Spendsy token as an
# Authorization header; cookies are only used when the Vite dev proxy makes calls same-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)


async def resolve_identity(http_request: Request, required: Optional[bool] = None) -> Optional[Identity]:
    """
    Map the request's Spendsy token to an Identity.
    off: always anonymous. optional: anonymous allowed, but a supplied token must be valid.
    required: a valid token is mandatory.
    """
    mode = auth_mode()
    if mode == "off":
        return None
    must = (mode == "required") if required is None else required
    token = extract_token(http_request.headers, http_request.cookies)
    if not token:
        if must:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Please sign in to use TORA.",
                                headers={"WWW-Authenticate": "Bearer"})
        return None
    try:
        return await auth_verifier.verify(token)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc),
                            headers={"WWW-Authenticate": "Bearer"})
    except AuthUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


def _user_id(identity: Optional[Identity]) -> Optional[str]:
    return identity.user_id if identity is not None else None


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
    grounding: Optional[Dict[str, Any]] = None


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
    """Traced entry point (metadata-only traces; see backend/observability)."""
    trace = start_trace(
        mode="stateless" if (request.messages is not None and request.conversation_id is None) else "stateful",
        conversation_ref=conversation_ref(request.conversation_id),
    )
    ctx_token = None
    try:
        identity = await resolve_identity(http_request)
        ctx_token = set_current_identity(identity)
        result = await _chat(request, http_request, identity)
    except HTTPException as exc:
        status_label = "rate_limited" if exc.status_code == 429 else ("client_error" if exc.status_code < 500 else "error")
        telemetry.finish(trace, status=status_label, http_status=exc.status_code,
                         error_type=None if exc.status_code < 500 else f"http_{exc.status_code}")
        raise
    except Exception as exc:
        telemetry.finish(trace, status="error", http_status=500, error_type=type(exc).__name__)
        raise
    finally:
        if ctx_token is not None:
            reset_current_identity(ctx_token)
    if result.conversation_id and trace.conversation_ref is None:
        trace.conversation_ref = conversation_ref(result.conversation_id)
    telemetry.finish(trace, status="ok", http_status=200)
    return result


async def _chat(request: ChatRequest, http_request: Request, identity: Optional[Identity] = None):
    """
    Core Chat Endpoint:
    Validates request -> Builds Context -> ToraAgent -> LLMProvider -> Response
    """
    client_key = http_request.client.host if http_request.client else "unknown"
    if identity is not None:
        client_key = f"user:{identity.user_id}"
    if not rate_limiter.allow(client_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please wait a moment and try again.",
            headers={"Retry-After": str(int(rate_limiter.window))},
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
            return await _run_stateful_turn(request, user_prompt.strip(), identity)

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
            grounding=_grounding_summary(agent_response),
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


def _grounding_summary(agent_response) -> Optional[Dict[str, Any]]:
    g = getattr(agent_response, "grounding", None)
    if not g:
        return None
    return {
        "action": g.get("action"),
        "checked": g.get("checked"),
        "unsupported": [c["text"] for c in g.get("unsupported", [])],
    }


def _intent_value(agent_response) -> Optional[str]:
    intent = getattr(agent_response, "intent", None)
    return intent.intent.value if intent is not None else None


def _load_session_or_404(conversation_id: str, identity: Optional[Identity] = None):
    """Unknown ids and other users' conversations look identical (404) so ids can't be probed."""
    if not is_valid_conversation_id(conversation_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    session = session_store.get(conversation_id)
    if session is None or not session.can_access(_user_id(identity)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return session


async def _run_stateful_turn(request: ChatRequest, prompt: str, identity: Optional[Identity] = None) -> ChatResponse:
    """
    One turn of a server-side conversation. History, memory and topic state come from
    the store; client-supplied `messages` are ignored so earlier turns cannot be forged.
    Nothing is persisted unless the turn succeeds.
    """
    owner = _user_id(identity)
    if request.conversation_id is None:
        session = session_store.create(owner_id=owner)
    else:
        session = _load_session_or_404(request.conversation_id, identity)

    lock_key = f"user:{session.owner_id}" if session.owner_id else session.id
    async with session_store.lock_for(lock_key):
        if request.conversation_id is not None:
            session = _load_session_or_404(session.id, identity)  # reload under the lock
        if session.owner_id:
            # Account memory is shared by all of the user's conversations; use the latest copy.
            saved = session_store.get_user_profile(session.owner_id)
            if saved is not None:
                session.profile = saved
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
        grounding = _grounding_summary(agent_response)
        if grounding:
            meta = dict(meta or {})
            meta["grounding"] = grounding
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
        grounding=grounding,
    )


@app.get("/api/metrics")
async def metrics():
    """Aggregate, content-free TORA metrics since process start."""
    return telemetry.snapshot()


@app.get("/api/traces")
async def traces(limit: int = 50):
    """Recent per-turn traces (metadata only). Enabled with TORA_DEBUG_ENDPOINTS=1."""
    if not DEBUG_ENDPOINTS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    return {"traces": telemetry.recent(limit)}


@app.get("/api/me")
async def me(http_request: Request):
    """Who TORA thinks is calling, and whether account features are on."""
    identity = await resolve_identity(http_request, required=False)
    return {
        "auth_mode": auth_mode(),
        "signed_in": identity is not None,
        "user_id": _user_id(identity),
        "username": identity.username if identity else None,
        "features": {
            "account_memory": identity is not None,
            "spendsy_data": identity is not None,
        },
    }


async def _require_user(http_request: Request) -> Identity:
    if auth_mode() == "off":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Accounts are not enabled.")
    identity = await resolve_identity(http_request, required=True)
    assert identity is not None
    return identity


@app.get("/api/conversations")
async def list_conversations(http_request: Request, limit: int = 50):
    """The signed-in user's conversations, newest first."""
    identity = await _require_user(http_request)
    return {"conversations": session_store.list_for_owner(identity.user_id, limit=limit)}


@app.get("/api/me/memory")
async def get_account_memory(http_request: Request):
    """Financial facts TORA remembers for this account (shared by all its conversations)."""
    identity = await _require_user(http_request)
    profile = session_store.get_user_profile(identity.user_id) or FinancialProfile()
    return {"memory": profile.to_dict(), "memory_summary": profile.to_context_string()}


@app.delete("/api/me/memory")
async def clear_account_memory(http_request: Request):
    """Forget every remembered financial fact for this account (transcripts are kept)."""
    identity = await _require_user(http_request)
    async with session_store.lock_for(f"user:{identity.user_id}"):
        profile = session_store.get_user_profile(identity.user_id) or FinancialProfile()
        profile.clear()
        session_store.save_user_profile(identity.user_id, profile)
    return {"cleared": True}


@app.delete("/api/me/data")
async def delete_account_data(http_request: Request):
    """Delete all of this account's TORA conversations and remembered facts."""
    identity = await _require_user(http_request)
    async with session_store.lock_for(f"user:{identity.user_id}"):
        removed = session_store.delete_user_data(identity.user_id)
    return {"deleted": True, "conversations_deleted": removed}


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, http_request: Request):
    """Transcript, remembered facts and topic state for one conversation."""
    identity = await resolve_identity(http_request)
    session = _load_session_or_404(conversation_id, identity)
    if session.owner_id:
        session.profile = session_store.get_user_profile(session.owner_id) or session.profile
    return {
        "conversation_id": session.id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "turns": session.turns,
        "memory": session.profile.to_dict(),
        "memory_summary": session.profile.to_context_string(),
        "state": session.state.to_dict(),
        "owned": session.owner_id is not None,
    }


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, http_request: Request):
    """Delete a conversation (for signed-in users, account memory is kept; see /api/me/memory)."""
    identity = await resolve_identity(http_request)
    _load_session_or_404(conversation_id, identity)
    if not session_store.delete(conversation_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return {"deleted": True}


@app.delete("/api/conversations/{conversation_id}/memory")
async def clear_conversation_memory(conversation_id: str, http_request: Request):
    """Forget all remembered financial facts but keep the transcript.
    For an account-owned conversation this clears the account memory."""
    identity = await resolve_identity(http_request)
    session = _load_session_or_404(conversation_id, identity)
    lock_key = f"user:{session.owner_id}" if session.owner_id else session.id
    async with session_store.lock_for(lock_key):
        session = _load_session_or_404(conversation_id, identity)
        if session.owner_id:
            session.profile = session_store.get_user_profile(session.owner_id) or session.profile
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
