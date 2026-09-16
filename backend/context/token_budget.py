import math
import logging
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger("tora.context.token_budget")

# Conservative character-to-token ratio for financial English text.
# 3.5 characters per token provides a safe overestimate (protects against context exhaustion).
CHARS_PER_TOKEN: float = 3.5
MESSAGE_OVERHEAD_TOKENS: int = 4

# Standard context window and generation reservation defaults
DEFAULT_NUM_CTX: int = 8192
DEFAULT_MAX_GENERATION_TOKENS: int = 2048
DEFAULT_SAFETY_MARGIN_TOKENS: int = 256


def estimate_tokens(text: Optional[str]) -> int:
    """
    Provider-independent conservative token estimator.
    
    Uses a 3.5 character-per-token ratio with rounding up to ensure
    we never underestimate token consumption on reasoning models (e.g. Gemma).
    """
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_message_tokens(msg: Dict[str, str]) -> int:
    """Estimate tokens consumed by a single chat message dict."""
    content = msg.get("content", "")
    role = msg.get("role", "")
    return estimate_tokens(content) + estimate_tokens(role) + MESSAGE_OVERHEAD_TOKENS


def estimate_messages_tokens(messages: List[Dict[str, str]]) -> int:
    """Estimate total tokens consumed by a list of chat message dicts."""
    return sum(estimate_message_tokens(m) for m in messages)


class TokenBudgetManager:
    """
    Provider-independent token budget manager.
    
    Ensures that LLM prompts fit strictly within the configured context window
    while prioritizing critical context (System instructions, Current user prompt,
    Financial Profile, Tool results) over raw historical turns.
    """

    def __init__(
        self,
        num_ctx: int = DEFAULT_NUM_CTX,
        max_generation_tokens: int = DEFAULT_MAX_GENERATION_TOKENS,
        safety_margin_tokens: int = DEFAULT_SAFETY_MARGIN_TOKENS,
    ):
        if num_ctx < 512:
            raise ValueError(f"num_ctx must be at least 512, got {num_ctx}")
        if max_generation_tokens >= num_ctx:
            raise ValueError(f"max_generation_tokens ({max_generation_tokens}) must be less than num_ctx ({num_ctx})")

        self.num_ctx = num_ctx
        self.max_generation_tokens = max_generation_tokens
        self.safety_margin_tokens = safety_margin_tokens

    @property
    def max_prompt_budget(self) -> int:
        """Total token budget available for input prompt messages."""
        return max(256, self.num_ctx - self.max_generation_tokens - self.safety_margin_tokens)

    def calculate_remaining_budget(
        self,
        system_tokens: int = 0,
        user_message_tokens: int = 0,
        financial_profile_tokens: int = 0,
        tool_tokens: int = 0,
        summary_tokens: int = 0,
    ) -> int:
        """
        Compute remaining token budget available for raw conversation history.
        """
        reserved = (
            system_tokens
            + user_message_tokens
            + financial_profile_tokens
            + tool_tokens
            + summary_tokens
        )
        return max(0, self.max_prompt_budget - reserved)

    def fit_history(
        self,
        history_messages: List[Any],
        available_budget_tokens: int,
    ) -> Tuple[List[Any], List[Any]]:
        """
        Select the most recent conversation messages that fit within available_budget_tokens.
        
        Preserves chronological ordering from newest backwards.
        Returns (fitted_messages, dropped_messages).
        """
        if not history_messages or available_budget_tokens <= 0:
            return [], list(history_messages)

        fitted: List[Any] = []
        consumed = 0

        # Traverse backwards from the most recent message
        for msg in reversed(history_messages):
            if hasattr(msg, "content") and hasattr(msg, "role"):
                msg_tokens = estimate_tokens(msg.content) + estimate_tokens(msg.role) + MESSAGE_OVERHEAD_TOKENS
            elif isinstance(msg, dict):
                msg_tokens = estimate_message_tokens(msg)
            else:
                msg_tokens = estimate_tokens(str(msg)) + MESSAGE_OVERHEAD_TOKENS

            if consumed + msg_tokens <= available_budget_tokens:
                fitted.append(msg)
                consumed += msg_tokens
            else:
                break

        # Re-reverse so messages are in original chronological order
        fitted.reverse()
        fitted_count = len(fitted)
        dropped = history_messages[: len(history_messages) - fitted_count]

        return fitted, dropped
