"""Phase 6B: read-only access to the signed-in user's Spendsy data."""

from .spendsy import (
    SpendsyClient,
    SpendsyDataError,
    SpendsyUnavailableError,
    category_breakdown,
    monthly_totals,
    normalise_transaction,
    spending_summary,
)

__all__ = [
    "SpendsyClient",
    "SpendsyDataError",
    "SpendsyUnavailableError",
    "category_breakdown",
    "monthly_totals",
    "normalise_transaction",
    "spending_summary",
]
