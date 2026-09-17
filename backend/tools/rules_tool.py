"""TORA tool: official rules with citations from the curated library (Phase 10)."""

from typing import Any, Dict, Optional, Type

from pydantic import BaseModel, Field

from .base import BaseTool, ToolMetadata
from ..knowledge import get_library


class RulesLookupInput(BaseModel):
    query: str = Field(..., min_length=2, max_length=200,
                       description="What rule to look up, e.g. '80C limit', 'HRA exemption', 'ITR due date'.")
    tax_year: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$",
                                    description="Only if the user named a tax/financial year, e.g. '2026-27'.")
    limit: int = Field(default=3, ge=1, le=5)


class RulesLookupTool(BaseTool):
    name: str = "rules_lookup"
    description: str = (
        "Look up official Indian tax, RBI and consumer-protection rules from TORA's reviewed library: limits, "
        "rates, eligibility, due dates, borrower rights and the exact legal section (Income-tax Act 2025 and the "
        "earlier 1961 section). Use for 'what is the limit / rule / deadline / which section' questions instead of "
        "web search. Does not calculate tax — use tax_calc for amounts."
    )
    args_schema: Type[BaseModel] = RulesLookupInput

    def __init__(self):
        self.metadata = ToolMetadata(name=self.name, description=self.description, version="1.0.0",
                                     tags=["knowledge", "law", "deterministic"], is_deterministic=True)
        super().__init__()

    async def execute(self, query: str, tax_year: Optional[str] = None, limit: int = 3, **kwargs: Any) -> Dict[str, Any]:
        lib = get_library()
        results = lib.search(query, tax_year=tax_year, limit=limit)
        return {
            "operation": "rules_lookup",
            "query": query,
            "tax_year": tax_year,
            "library_version": lib.version,
            "rules": results,
            "found": bool(results),
        }
