"""TORA tool: the signed-in user's own Spendsy transactions (Phase 6B)."""

from datetime import date, timedelta
from typing import Any, Literal, Optional, Type

from pydantic import BaseModel, Field

from .base import BaseTool, ToolMetadata
from ..auth import current_identity
from ..integrations.spendsy import SpendsyClient, SpendsyDataError, spending_summary

RECENT_LIMIT_MAX = 25


class SpendsyDataInput(BaseModel):
    operation: Literal["spending_summary", "recent_transactions"] = Field(
        ...,
        description="spending_summary: income/expenses/categories per month; "
                    "recent_transactions: the latest transactions (optionally filtered).",
    )
    months: int = Field(default=3, ge=1, le=24, description="Calendar months to cover, including the current one.")
    category: Optional[str] = Field(default=None, max_length=40,
                                    description="Optional category or merchant filter, e.g. 'food', 'swiggy'.")
    limit: int = Field(default=10, ge=1, le=RECENT_LIMIT_MAX)


class SpendsyDataTool(BaseTool):
    name: str = "spendsy_data"
    description: str = (
        "Read the signed-in user's OWN Spendsy transactions (read-only). Use for questions about what they "
        "actually earned or spent: 'how much did I spend on food last month', 'where does my money go', "
        "'my spending this month', 'recent transactions'. Returns deterministic totals per month and per "
        "category. Fails with a sign-in message when the user is not signed in."
    )
    args_schema: Type[BaseModel] = SpendsyDataInput

    def __init__(self, base_url: Optional[str] = None, transport: Any = None, today: Optional[date] = None):
        self.metadata = ToolMetadata(
            name=self.name, description=self.description, version="1.0.0",
            tags=["finance", "user-data", "read-only"], is_deterministic=True, requires_auth=True,
        )
        self._base_url = base_url
        self._transport = transport
        self._today = today
        super().__init__()

    async def execute(self, operation: str, months: int = 3, category: Optional[str] = None,
                      limit: int = 10, **kwargs: Any) -> Any:
        identity = current_identity()
        if identity is None or not identity.token:
            raise SpendsyDataError("The user is not signed in to Spendsy, so their transactions are not available.")
        today = self._today or date.today()
        client = SpendsyClient(identity.token, base_url=self._base_url, transport=self._transport)
        start_month = today.replace(day=1)
        for _ in range(months - 1):
            start_month = (start_month - timedelta(days=1)).replace(day=1)
        txns, truncated = await client.transactions(since=start_month)
        if operation == "recent_transactions":
            pool = txns
            if category:
                needle = category.lower()
                pool = [t for t in txns if needle in t["category"].lower() or needle in t["description"].lower()]
            pool = sorted(pool, key=lambda t: t["date"], reverse=True)[:limit]
            return {"operation": operation, "since": start_month.isoformat(), "count": len(pool),
                    "transactions": pool, "truncated": truncated}
        result = spending_summary(txns, months=months, today=today, category=category)
        result["truncated"] = truncated
        return result
