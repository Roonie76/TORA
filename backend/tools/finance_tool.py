"""TORA deterministic financial calculator tool (Phase 3B)."""

from typing import Any, Dict, Literal, Type

from pydantic import BaseModel, Field

from .base import BaseTool, ToolMetadata
from ..finance.engine import OPERATION_PARAMS, run_operation

OperationName = Literal[
    "emi", "amortization", "sip_future_value", "sip_change_impact", "required_sip",
    "compound_growth", "inflation_adjust", "debt_payoff", "emergency_fund",
    "savings_rate", "debt_to_income", "net_worth", "budget_plan", "goal_plan", "retirement_plan",
    "debt_snapshot", "debt_rescue_plan", "consolidation_check", "minimum_due_trap",
]


class FinanceCalcInput(BaseModel):
    operation: OperationName = Field(..., description="Which financial computation to run.")
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Named numeric inputs for the operation. Rates are annual percentages (8.5 = 8.5%).",
    )


_PARAM_DOC = "; ".join(f"{op}({args})" for op, args in OPERATION_PARAMS.items())


class FinanceCalcTool(BaseTool):
    name: str = "finance_calc"
    description: str = (
        "Deterministic personal-finance calculator. Use for EMIs, loan prepayment/amortization, SIP and "
        "lump-sum growth, SIP what-if changes, required SIP for a goal, inflation, debt payoff plans, "
        "emergency fund, savings rate, debt-to-income, net worth, 50/30/20 budget plans, multi-goal plans and "
        "retirement corpus planning. Operations and params: " + _PARAM_DOC
    )
    args_schema: Type[BaseModel] = FinanceCalcInput

    def __init__(self):
        self.metadata = ToolMetadata(
            name=self.name,
            description=self.description,
            version="1.0.0",
            tags=["finance", "calculation", "deterministic"],
            is_deterministic=True,
            requires_auth=False,
        )
        super().__init__()

    async def execute(self, operation: str, params: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        return run_operation(operation, params)
