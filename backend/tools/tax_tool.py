"""TORA deterministic income-tax tool (Phase 4C)."""

from typing import Any, Dict, Literal, Type

from pydantic import BaseModel, Field

from .base import BaseTool, ToolMetadata
from ..finance.engine import FinanceInputError
from ..finance.tax import TAX_OPERATIONS, TAX_PARAMS, TAX_RULES, current_tax_year

_ALLOWED = {"gross_salary", "other_income", "regime", "tax_year", "age_category", "deductions",
            "stcg_equity", "ltcg_equity", "ltcg_other"}


class TaxCalcInput(BaseModel):
    operation: Literal["compute_tax", "compare_regimes"] = Field(
        ..., description="compute_tax for one regime, compare_regimes for new vs old."
    )
    params: Dict[str, Any] = Field(default_factory=dict, description="Annual amounts in rupees.")


class TaxCalcTool(BaseTool):
    name: str = "tax_calc"
    description: str = (
        "Deterministic Indian income-tax calculator for resident individuals (tax years "
        + ", ".join(sorted(TAX_RULES))
        + "): slab tax, standard deduction, rebate with marginal relief, surcharge, cess, capital gains, "
        "and new-vs-old regime comparison. Amounts are ANNUAL rupees. Params: " + TAX_PARAMS
    )
    args_schema: Type[BaseModel] = TaxCalcInput

    def __init__(self):
        self.metadata = ToolMetadata(
            name=self.name, description=self.description, version="1.0.0",
            tags=["finance", "tax", "deterministic"], is_deterministic=True, requires_auth=False,
        )
        super().__init__()

    async def execute(self, operation: str, params: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        unknown = set(params) - _ALLOWED
        if unknown:
            raise FinanceInputError(f"Unknown parameter(s): {', '.join(sorted(unknown))}. Expected: {TAX_PARAMS}.")
        clean = dict(params)
        if operation == "compare_regimes":
            clean.pop("regime", None)
        clean.setdefault("tax_year", current_tax_year())
        return TAX_OPERATIONS[operation](**clean)
