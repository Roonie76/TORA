"""TORA deterministic financial calculator tool (Phase 3B)."""

from typing import Any, Dict, Literal, Optional, Type

from pydantic import BaseModel, Field

from .base import BaseTool, ToolMetadata
from ..finance.engine import OPERATION_PARAMS, run_operation

OperationName = Literal[
    "emi", "amortization", "sip_future_value", "sip_change_impact", "required_sip",
    "compound_growth", "inflation_adjust", "debt_payoff", "emergency_fund",
    "savings_rate", "debt_to_income", "net_worth", "budget_plan", "goal_plan", "retirement_plan",
    "debt_snapshot", "debt_rescue_plan", "consolidation_check", "minimum_due_trap",
    "prepay_vs_invest", "rent_vs_buy", "loan_tenure_choice", "financial_health_check",
    "scenario_compare",
]


class FinanceCalcInput(BaseModel):
    operation: OperationName = Field(..., description="Which financial computation to run.")
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Named numeric inputs for the operation. Rates are annual percentages (8.5 = 8.5%).",
    )


_PARAM_DOC = "; ".join(f"{op}({args})" for op, args in OPERATION_PARAMS.items())


def _drop_unset_optionals(operation: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """A parameter the model set to null is one it did not have, not a bad number.

    Seen live: a health check on a real conversation failed with "'high_interest_debt' must be a
    number" after the model emitted null for an optional field it had no value for. The turn cost
    five minutes and two model calls to produce an error, when the parameter has a default of 0
    and omitting it would have worked. A null for a REQUIRED parameter is left alone, so it still
    fails as a missing parameter rather than silently becoming a default.
    """
    import inspect

    from ..finance.engine import OPERATIONS

    fn = OPERATIONS.get(operation)
    if fn is None or not isinstance(params, dict):
        return params
    optional = {name for name, p in inspect.signature(fn).parameters.items()
                if p.default is not inspect.Parameter.empty}
    blank = ("", "none", "null", "n/a", "na", "unknown", "-")
    return {k: v for k, v in params.items()
            if not (k in optional and (v is None or (isinstance(v, str) and v.strip().lower() in blank)))}


class FinanceCalcTool(BaseTool):
    name: str = "finance_calc"
    description: str = (
        "Deterministic personal-finance calculator. Use for EMIs, loan prepayment/amortization, SIP and "
        "lump-sum growth, SIP what-if changes, required SIP for a goal, inflation, debt payoff plans, "
        "emergency fund, savings rate, debt-to-income, net worth, 50/30/20 budget plans, multi-goal plans, "
        "retirement corpus planning, debt rescue (snapshot, rescue plan, consolidation, minimum-due trap) and "
        "option comparisons (prepay vs invest, rent vs buy, loan tenure, financial health check). "
        "Operations and params: " + _PARAM_DOC
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

    def check_arguments(self, args: Dict[str, Any]) -> Optional[str]:
        """Unknown or missing parameter names, reported at planning time so the planner can repair them."""
        import inspect

        from ..finance.engine import OPERATIONS

        fn = OPERATIONS.get(args.get("operation"))
        params = args.get("params") or {}
        if fn is None:
            return None
        sig = inspect.signature(fn)
        unknown = set(params) - set(sig.parameters)
        if unknown:
            return (f"Unknown parameter(s) for {args['operation']}: {', '.join(sorted(unknown))}. "
                    f"Expected: {OPERATION_PARAMS[args['operation']]}.")
        missing = [n for n, p in sig.parameters.items() if p.default is inspect._empty and n not in params]
        if missing:
            return f"Missing parameter(s) for {args['operation']}: {', '.join(missing)}."
        return None

    async def execute(self, operation: str, params: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        return run_operation(operation, _drop_unset_optionals(operation, params))
