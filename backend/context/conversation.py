from enum import Enum
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field


class MessageRole(str, Enum):
    """
    Standard message roles supported by the conversational AI system.
    Designed so future roles (e.g. TOOL) can be added cleanly.
    """
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

    @classmethod
    def valid_client_roles(cls) -> set:
        """Roles that clients/frontend are allowed to supply in history."""
        return {cls.USER.value, cls.ASSISTANT.value}


@dataclass(frozen=True)
class ConversationMessage:
    """
    Strongly typed representation of a single conversation turn.
    Immutable to prevent unexpected mutations.
    """
    role: str
    content: str

    def __post_init__(self):
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("Message role must be a non-empty string.")
        
        valid_roles = {r.value for r in MessageRole}
        if self.role.strip().lower() not in valid_roles:
            raise ValueError(f"Invalid message role '{self.role}'. Supported roles: {sorted(valid_roles)}")
        
        if not isinstance(self.content, str):
            raise ValueError("Message content must be a string.")
        
        if not self.content.strip():
            raise ValueError("Message content cannot be empty or whitespace-only.")

    def to_dict(self) -> Dict[str, str]:
        """Return generic dictionary for LLM provider consumption."""
        return {
            "role": self.role.strip().lower(),
            "content": self.content.strip(),
        }


# Default limit for historical turns sent to the LLM (configurable)
# Note: Token-aware truncation and summarization will be added in a future phase.
MAX_HISTORY_MESSAGES: int = 20


class ConversationContext:
    """
    In-memory request-level conversation context container.
    Maintains an ordered list of user and assistant conversation messages.
    System prompts are kept separate and not stored in conversation history.
    """

    def __init__(self, messages: Optional[List[ConversationMessage]] = None):
        self._messages: List[ConversationMessage] = list(messages) if messages else []

    @property
    def messages(self) -> List[ConversationMessage]:
        """Return a copy of the ordered messages."""
        return list(self._messages)

    def add_message(self, role: str, content: str) -> ConversationMessage:
        """Add a validated message to the context."""
        msg = ConversationMessage(role=role, content=content)
        self._messages.append(msg)
        return msg

    def add_user_message(self, content: str) -> ConversationMessage:
        """Convenience helper to append a user message."""
        return self.add_message(role=MessageRole.USER.value, content=content)

    def add_assistant_message(self, content: str) -> ConversationMessage:
        """Convenience helper to append an assistant response."""
        return self.add_message(role=MessageRole.ASSISTANT.value, content=content)

    def get_messages(self, limit: Optional[int] = None) -> List[ConversationMessage]:
        """
        Get the ordered messages, optionally limited to the most recent N items.
        """
        if limit is not None and limit > 0:
            return self._messages[-limit:]
        return list(self._messages)

    def get_message_dicts(self, limit: Optional[int] = None) -> List[Dict[str, str]]:
        """Return messages formatted as dictionaries for LLM providers."""
        return [msg.to_dict() for msg in self.get_messages(limit=limit)]

    def clear(self) -> None:
        """Reset the conversation context."""
        self._messages.clear()

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self):
        return iter(self._messages)

    @classmethod
    def from_list(
        cls,
        raw_messages: List[Dict[str, Any]],
        allowed_roles: Optional[set] = None,
    ) -> "ConversationContext":
        """
        Construct a ConversationContext from a list of raw dictionaries.
        Validates structure and enforces role restrictions.
        """
        parsed: List[ConversationMessage] = []
        check_roles = allowed_roles or {r.value for r in MessageRole}

        for idx, item in enumerate(raw_messages):
            if not isinstance(item, dict):
                raise ValueError(f"Message at index {idx} must be a dictionary.")
            
            role = item.get("role")
            content = item.get("content")

            if not role or not isinstance(role, str):
                raise ValueError(f"Message at index {idx} has missing or invalid 'role'.")
            
            clean_role = role.strip().lower()
            if clean_role not in check_roles:
                raise ValueError(
                    f"Role '{role}' at index {idx} is not permitted. Allowed roles: {sorted(check_roles)}"
                )

            if content is None or not isinstance(content, str):
                raise ValueError(f"Message at index {idx} has missing or non-string 'content'.")

            if not content.strip():
                raise ValueError(f"Message at index {idx} has empty 'content'.")

            parsed.append(ConversationMessage(role=clean_role, content=content))

        return cls(messages=parsed)
