import json
import os
import time
import logging
import httpx
from typing import Optional, List, Dict, Any

from ..observability import record_llm_usage
from .base import (
    LLMProvider,
    LLMResponse,
    LLMProviderError,
    LLMConnectionError,
    LLMTimeoutError,
    LLMResponseError,
)

logger = logging.getLogger("tora.llm.ollama")

# Default context window size for Ollama models.
# Can be overridden via TORA_LLM_NUM_CTX environment variable or constructor parameter.
DEFAULT_NUM_CTX: int = 8192
MIN_NUM_CTX: int = 512


def _resolve_num_ctx(explicit: Optional[int] = None) -> int:
    """
    Resolve the context window size from explicit parameter, environment variable,
    or default. Invalid values fall back to DEFAULT_NUM_CTX with a warning.
    """
    if explicit is not None:
        if isinstance(explicit, int) and explicit >= MIN_NUM_CTX:
            return explicit
        logger.warning(
            "Invalid explicit num_ctx=%r (must be int >= %d). Using default %d.",
            explicit, MIN_NUM_CTX, DEFAULT_NUM_CTX,
        )
        return DEFAULT_NUM_CTX

    env_val = os.getenv("TORA_LLM_NUM_CTX", "")
    if env_val.strip():
        try:
            parsed = int(env_val.strip())
            if parsed >= MIN_NUM_CTX:
                return parsed
            logger.warning(
                "TORA_LLM_NUM_CTX=%s is below minimum %d. Using default %d.",
                env_val, MIN_NUM_CTX, DEFAULT_NUM_CTX,
            )
            return DEFAULT_NUM_CTX
        except (ValueError, TypeError):
            logger.warning(
                "Invalid TORA_LLM_NUM_CTX='%s' (not an integer). Using default %d.",
                env_val, DEFAULT_NUM_CTX,
            )
            return DEFAULT_NUM_CTX

    return DEFAULT_NUM_CTX


def _think_setting() -> Optional[bool]:
    """
    TORA_LLM_THINK: "false" (default) asks reasoning models (e.g. gemma4, qwen3) not to emit a
    hidden thinking pass — on CPU that pass costs minutes per turn and TORA's tools already do the
    maths. "true" enables it; "auto" leaves the model's default.
    """
    raw = os.getenv("TORA_LLM_THINK", "false").strip().lower()
    if raw in ("auto", "default", ""):
        return None
    return raw in ("1", "true", "yes", "on")


def normalize_ollama_host(raw_host: Optional[str]) -> str:
    """Normalize OLLAMA_HOST string to a valid HTTP URL."""
    if not raw_host or raw_host.strip() in ("0.0.0.0", "0.0.0.0:11434", "127.0.0.1", "localhost"):
        return "http://127.0.0.1:11434"
    host_str = raw_host.strip()
    if not host_str.startswith("http://") and not host_str.startswith("https://"):
        return f"http://{host_str}"
    return host_str


class OllamaProvider(LLMProvider):
    """
    Ollama LLM Provider implementation.
    Encapsulates host normalization, Ollama /api/chat payload formatting,
    model discovery via /api/tags, and structured error handling.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        default_model: Optional[str] = None,
        timeout: Optional[float] = None,
        client: Optional[httpx.AsyncClient] = None,
        num_ctx: Optional[int] = None,
    ):
        raw_host = host if host is not None else os.getenv("OLLAMA_HOST", "")
        self.host = normalize_ollama_host(raw_host)
        self._default_model = default_model or os.getenv("OLLAMA_MODEL", "gemma4:e4b")
        if timeout is None:
            try:
                timeout = float(os.getenv("TORA_LLM_TIMEOUT_SECONDS", "180"))
            except ValueError:
                timeout = 180.0
        self.timeout = timeout
        self._client = client
        self._num_ctx = _resolve_num_ctx(num_ctx)
        self._no_think_models: set = set()
        self._models_cache: Optional[List[str]] = None
        self._models_cache_at: float = 0.0
        try:
            self._models_cache_ttl = max(0.0, float(os.getenv("TORA_MODEL_CACHE_SECONDS", "60")))
        except ValueError:
            self._models_cache_ttl = 60.0

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def num_ctx(self) -> int:
        """The configured context window size for this provider."""
        return self._num_ctx

    def resolve_model(self, requested_model: Optional[str], available_models: List[str]) -> str:
        """Find the best matching installed model in Ollama."""
        target = requested_model or self.default_model
        if not available_models:
            return target

        if target in available_models:
            return target

        req_clean = target.lower().replace("-", "").replace(".", "").replace(":", "")
        for m in available_models:
            m_clean = m.lower().replace("-", "").replace(".", "").replace(":", "")
            if req_clean in m_clean or m_clean in req_clean:
                return m

        logger.warning(
            "Requested model '%s' is not installed; falling back to '%s'.", target, available_models[0]
        )
        return available_models[0]

    async def _available_models_cached(self) -> List[str]:
        """
        Installed-model list used for model resolution on every generate() call.
        Cached briefly so each chat turn does not add extra /api/tags round-trips
        (planner + answer previously cost four HTTP calls per turn).
        """
        now = time.monotonic()
        if self._models_cache and (now - self._models_cache_at) < self._models_cache_ttl:
            return self._models_cache
        models = await self.list_models()
        if models:
            self._models_cache = models
            self._models_cache_at = now
        return models

    async def list_models(self) -> List[str]:
        """Fetch list of available model tags from Ollama."""
        try:
            if self._client:
                res = await self._client.get(f"{self.host}/api/tags")
                if res.status_code == 200:
                    data = res.json()
                    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
                return []

            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(f"{self.host}/api/tags")
                if res.status_code == 200:
                    data = res.json()
                    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
                return []
        except Exception:
            return []

    async def health_check(self) -> Dict[str, Any]:
        """Check Ollama connectivity and list available models."""
        connected = False
        installed_models: List[str] = []
        try:
            if self._client:
                res = await self._client.get(f"{self.host}/api/tags")
                if res.status_code == 200:
                    connected = True
                    data = res.json()
                    installed_models = [m.get("name") for m in data.get("models", []) if m.get("name")]
            else:
                async with httpx.AsyncClient(timeout=4.0) as client:
                    res = await client.get(f"{self.host}/api/tags")
                    if res.status_code == 200:
                        connected = True
                        data = res.json()
                        installed_models = [m.get("name") for m in data.get("models", []) if m.get("name")]
        except Exception:
            connected = False

        return {
            "host": self.host,
            "connected": connected,
            "models": installed_models,
            "default_model": self.default_model,
        }

    async def _post_chat(self, payload: Dict[str, Any]) -> httpx.Response:
        if self._client:
            return await self._client.post(f"{self.host}/api/chat", json=payload)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await client.post(f"{self.host}/api/chat", json=payload)

    async def _generate_streaming(self, payload: Dict[str, Any], target_model: str, opts: Dict[str, Any],
                                  on_token) -> LLMResponse:
        """Same as generate() but forwards visible text chunks to `on_token` as they arrive."""
        payload = dict(payload, stream=True)
        started = time.monotonic()
        parts: List[str] = []
        final: Dict[str, Any] = {}

        async def run(client: httpx.AsyncClient, body: Dict[str, Any]) -> Optional[str]:
            async with client.stream("POST", f"{self.host}/api/chat", json=body) as response:
                if response.status_code != 200:
                    return (await response.aread()).decode("utf-8", "replace")
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        raise LLMResponseError(message=f"Ollama error: {chunk['error']}", detail=chunk["error"])
                    text = (chunk.get("message") or {}).get("content") or ""
                    if text:
                        parts.append(text)
                        await on_token(text)
                    if chunk.get("done"):
                        final.update(chunk)
                return None

        try:
            if self._client:
                error = await run(self._client, payload)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    error = await run(client, payload)
                    if error is not None and "think" in payload and "think" in error.lower():
                        self._no_think_models.add(target_model)
                        payload.pop("think", None)
                        error = await run(client, payload)
            if error is not None and self._client and "think" in payload and "think" in error.lower():
                self._no_think_models.add(target_model)
                payload.pop("think", None)
                error = await run(self._client, payload)
            if error is not None:
                raise LLMResponseError(message=f"Ollama error: {error}. Ensure model '{target_model}' is pulled.",
                                       detail=error)
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(f"Ollama request timed out after {self.timeout}s: {e}") from e
        except httpx.ConnectError as e:
            raise LLMConnectionError(
                f"Could not connect to Ollama at {self.host}. Make sure Ollama is running (`ollama serve`)."
            ) from e
        except (LLMResponseError, LLMProviderError):
            raise
        except Exception as e:
            raise LLMProviderError(f"Error communicating with Ollama: {str(e)}") from e

        content = "".join(parts)
        done_reason = final.get("done_reason", "")
        if not content and done_reason == "length":
            raise LLMResponseError(message="LLM response was truncated due to context length limits.",
                                   detail="done_reason=length")
        if content and done_reason == "length" and opts.get("num_predict"):
            tail = " …\n\n(Answer shortened — ask me to continue for more detail.)"
            content = content.rstrip() + tail
            await on_token(tail)
        record_llm_usage((time.monotonic() - started) * 1000, final, target_model)
        return LLMResponse(content=content, model=target_model, done=True, raw=final)

    async def generate(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        """
        Send chat request to Ollama /api/chat.
        """
        available = await self._available_models_cached()
        target_model = self.resolve_model(model, available)

        # Standard default options using configured context window
        opts: Dict[str, Any] = {
            "temperature": 0.7,
            "num_ctx": self._num_ctx,
        }
        if options:
            if "temperature" in options and options["temperature"] is not None:
                opts["temperature"] = options["temperature"]
            if "num_ctx" in options and options["num_ctx"] is not None:
                opts["num_ctx"] = options["num_ctx"]
            if options.get("num_predict"):
                opts["num_predict"] = int(options["num_predict"])

        payload = {
            "model": target_model,
            "messages": messages,
            "stream": False,
            "options": opts,
        }
        if options and options.get("format"):
            # Constrained decoding: a JSON schema (or "json") understood by Ollama >= 0.5
            payload["format"] = options["format"]

        think = _think_setting()
        if options and isinstance(options.get("think"), bool):
            think = options["think"]
        if think is not None and target_model not in self._no_think_models:
            payload["think"] = think

        on_token = options.get("on_token") if options else None
        if callable(on_token) and not payload.get("format"):
            return await self._generate_streaming(payload, target_model, opts, on_token)

        started = time.monotonic()
        try:
            response = await self._post_chat(payload)
            if response.status_code != 200 and "think" in payload and "think" in response.text.lower():
                # Model / server without thinking support: remember and retry without the flag.
                self._no_think_models.add(target_model)
                payload.pop("think", None)
                response = await self._post_chat(payload)

            if response.status_code != 200:
                error_body = response.text
                raise LLMResponseError(
                    message=f"Ollama error ({response.status_code}): {error_body}. Ensure model '{target_model}' is pulled.",
                    status_code=response.status_code,
                    detail=error_body,
                )

            data = response.json()
            content = data.get("message", {}).get("content", "")

            # Detect truncated/empty responses from context-window exhaustion.
            # When a thinking model consumes all remaining tokens on reasoning
            # and produces no visible output, done_reason will be "length".
            done_reason = data.get("done_reason", "")
            if not content and done_reason == "length":
                raise LLMResponseError(
                    message=(
                        "LLM response was truncated due to context length limits. "
                        "The model could not generate a response within the available token budget."
                    ),
                    status_code=None,
                    detail="done_reason=length",
                )

            if content and done_reason == "length" and opts.get("num_predict") and not payload.get("format"):
                # Answer hit TORA_MAX_ANSWER_TOKENS: make the cut visible instead of ending mid-thought.
                content = content.rstrip() + " …\n\n(Answer shortened — ask me to continue for more detail.)"

            record_llm_usage((time.monotonic() - started) * 1000, data, target_model)
            return LLMResponse(
                content=content,
                model=target_model,
                done=data.get("done", True),
                raw=data,
            )

        except httpx.TimeoutException as e:
            raise LLMTimeoutError(f"Ollama request timed out after {self.timeout}s: {e}") from e
        except httpx.ConnectError as e:
            raise LLMConnectionError(
                f"Could not connect to Ollama at {self.host}. Make sure Ollama is running (`ollama serve`)."
            ) from e
        except LLMResponseError:
            raise
        except Exception as e:
            raise LLMProviderError(f"Error communicating with Ollama: {str(e)}") from e
