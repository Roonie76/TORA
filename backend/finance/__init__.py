from .engine import (
    FinanceInputError,
    OPERATIONS,
    OPERATION_PARAMS,
    run_operation,
    inr,
)
from . import debt  # noqa: E402,F401  (registers the Phase 8 debt-rescue operations)
from . import advisor  # noqa: E402,F401  (registers the Phase 9 option-evaluation operations)

__all__ = ["FinanceInputError", "OPERATIONS", "OPERATION_PARAMS", "run_operation", "inr"]
