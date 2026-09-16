from typing import Optional, Dict, Any
from dataclasses import dataclass, field


@dataclass(frozen=True)
class UserContext:
    """
    Architectural boundary for non-sensitive user metadata (locale, currency, preferences).
    Does NOT contain financial data, database entities, or authentication tokens.
    """
    user_id: Optional[str] = None
    locale: str = "en-IN"
    currency: str = "INR"
    currency_symbol: str = "₹"
    preferences: Dict[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        """Return True if no user-specific context is provided."""
        return self.user_id is None and not self.preferences
