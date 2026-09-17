"""
Read-only client for the Spendsy finance service plus deterministic summaries.

Only GET requests are made, always with the signed-in user's own token, so TORA
can never see another account's data or change anything. Transaction text
(descriptions, merchants, categories) may come from parsed bank statements and
is treated as untrusted by the context builder.
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

DEFAULT_FINANCE_URL = "http://localhost:8080/finance"
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_TEXT = 60
_INCOME_TYPES = {"income", "credit", "cr"}
_EXPENSE_TYPES = {"expense", "debit", "dr"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SpendsyDataError(Exception):
    """The request could not be served (not signed in, bad token, ...)."""


class SpendsyUnavailableError(SpendsyDataError):
    """The finance service could not be reached."""


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload and ("ok" in payload or "message" in payload):
        return payload["data"]
    return payload


def _items_and_cursor(payload: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    data = _unwrap(payload)
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)], None
    if isinstance(data, dict):
        for key in ("items", "transactions", "results", "data"):
            if isinstance(data.get(key), list):
                cursor = data.get("next_cursor") or data.get("nextCursor") or data.get("cursor")
                return [d for d in data[key] if isinstance(d, dict)], (str(cursor) if cursor else None)
    return [], None


def _clean_text(value: Any) -> str:
    text = _CONTROL.sub(" ", str(value or "")).strip()
    return " ".join(text.split())[:MAX_TEXT]


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, (int, float)) and value > 0:
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    for candidate in (raw, raw.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            pass
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(raw[:20], fmt).date()
        except ValueError:
            continue
    return None


def normalise_transaction(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return {date, amount, kind, category, description} or None if unusable."""
    try:
        amount = float(str(raw.get("amount", "")).replace(",", "").replace("₹", "").strip())
    except ValueError:
        return None
    if amount != amount or abs(amount) > 1e12:  # NaN / absurd
        return None
    kind = str(raw.get("type") or raw.get("transaction_type") or "").strip().lower()
    if raw.get("is_transfer") or kind == "transfer":
        kind = "transfer"
    elif kind in _INCOME_TYPES:
        kind = "income"
    elif kind in _EXPENSE_TYPES:
        kind = "expense"
    elif amount < 0:
        kind = "expense"
    else:
        return None
    when = _parse_date(raw.get("date") or raw.get("transaction_date") or raw.get("created_at"))
    if when is None:
        return None
    return {
        "date": when.isoformat(),
        "amount": round(abs(amount), 2),
        "kind": kind,
        "category": _clean_text(raw.get("category") or "Uncategorised") or "Uncategorised",
        "description": _clean_text(raw.get("description") or raw.get("merchant") or raw.get("note") or raw.get("title")),
    }


def _month_key(d: str) -> str:
    return d[:7]


def _recent_months(today: date, months: int) -> List[str]:
    keys = []
    y, m = today.year, today.month
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(keys))


def monthly_totals(txns: Iterable[Dict[str, Any]], months: List[str]) -> List[Dict[str, Any]]:
    buckets = {k: {"month": k, "income": 0.0, "expenses": 0.0, "count": 0} for k in months}
    for t in txns:
        b = buckets.get(_month_key(t["date"]))
        if b is None or t["kind"] == "transfer":
            continue
        b["income" if t["kind"] == "income" else "expenses"] += t["amount"]
        b["count"] += 1
    out = []
    for k in months:
        b = buckets[k]
        b["income"], b["expenses"] = round(b["income"], 2), round(b["expenses"], 2)
        b["net"] = round(b["income"] - b["expenses"], 2)
        out.append(b)
    return out


def category_breakdown(txns: Iterable[Dict[str, Any]], months: List[str], top: int = 8,
                       complete_months: Optional[int] = None) -> List[Dict[str, Any]]:
    """Expense totals per category; monthly_average divides by complete months only."""
    wanted = set(months)
    divisor = max(1, complete_months if complete_months else len(months))
    totals: Dict[str, float] = defaultdict(float)
    counts: Dict[str, int] = defaultdict(int)
    for t in txns:
        if t["kind"] != "expense" or _month_key(t["date"]) not in wanted:
            continue
        totals[t["category"]] += t["amount"]
        counts[t["category"]] += 1
    grand = sum(totals.values())
    rows = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    out = []
    for name, amt in rows[:top]:
        out.append({
            "category": name,
            "total": round(amt, 2),
            "monthly_average": round(amt / divisor, 2),
            "share_pct": round(100 * amt / grand, 1) if grand else 0.0,
            "count": counts[name],
        })
    rest = sum(a for _, a in rows[top:])
    if rest:
        out.append({"category": "Other", "total": round(rest, 2),
                    "monthly_average": round(rest / divisor, 2),
                    "share_pct": round(100 * rest / grand, 1), "count": sum(counts[n] for n, _ in rows[top:])})
    return out


def spending_summary(
    txns: List[Dict[str, Any]],
    months: int = 3,
    today: Optional[date] = None,
    category: Optional[str] = None,
    include_current_month: bool = True,
) -> Dict[str, Any]:
    """Deterministic summary of normalised transactions over the last N calendar months."""
    months = max(1, min(int(months), 24))
    today = today or date.today()
    keys = _recent_months(today, months)
    if not include_current_month:
        keys = _recent_months(today.replace(day=1) - timedelta(days=1), months)
    pool = txns
    if category:
        needle = category.strip().lower()
        pool = [t for t in txns if needle in t["category"].lower() or (t["description"] and needle in t["description"].lower())]
    per_month = monthly_totals(pool, keys)
    total_income = round(sum(m["income"] for m in per_month), 2)
    total_expenses = round(sum(m["expenses"] for m in per_month), 2)
    complete = [m for m in per_month if m["month"] != today.strftime("%Y-%m")] or per_month
    avg_exp = round(sum(m["expenses"] for m in complete) / len(complete), 2)
    avg_inc = round(sum(m["income"] for m in complete) / len(complete), 2)
    result = {
        "operation": "spending_summary",
        "period": {"from_month": keys[0], "to_month": keys[-1], "months": len(keys),
                   "current_month_partial": keys[-1] == today.strftime("%Y-%m")},
        "filter_category": category or None,
        "total_income": total_income,
        "total_expenses": total_expenses,
        "net": round(total_income - total_expenses, 2),
        "average_monthly_income": avg_inc,
        "average_monthly_expenses": avg_exp,
        "complete_months_averaged": len(complete),
        "savings_rate_pct": round(100 * (total_income - total_expenses) / total_income, 1) if total_income else None,
        "by_month": per_month,
        "transactions_considered": sum(m["count"] for m in per_month),
    }
    if not category:
        result["top_categories"] = category_breakdown(pool, keys, complete_months=len(complete))
    return result


class SpendsyClient:
    """GET-only client for the Spendsy finance service, scoped to one user token."""

    def __init__(
        self,
        token: str,
        base_url: Optional[str] = None,
        timeout_seconds: float = 10.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        if not token:
            raise SpendsyDataError("Sign in to Spendsy so TORA can read your transactions.")
        self.base_url = (base_url or os.getenv("TORA_FINANCE_URL") or DEFAULT_FINANCE_URL).rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport

    async def _get(self, client: httpx.AsyncClient, path: str, params: Dict[str, Any]) -> Any:
        try:
            resp = await client.get(f"{self.base_url}{path}", params=params,
                                    headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as exc:
            raise SpendsyUnavailableError("The Spendsy finance service is unavailable right now.") from exc
        if resp.status_code in (401, 403):
            raise SpendsyDataError("Your Spendsy session has expired. Please sign in again.")
        if resp.status_code != 200:
            raise SpendsyUnavailableError(f"The Spendsy finance service returned HTTP {resp.status_code}.")
        try:
            return resp.json()
        except ValueError as exc:
            raise SpendsyUnavailableError("The Spendsy finance service returned invalid data.") from exc

    async def transactions(self, since: Optional[date] = None, search: Optional[str] = None) -> Tuple[List[Dict[str, Any]], bool]:
        """Normalised transactions (newest first from the API), stopping once older than `since`.
        Returns (transactions, truncated)."""
        out: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        truncated = False
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport, follow_redirects=False) as client:
            for page in range(MAX_PAGES):
                params: Dict[str, Any] = {"limit": PAGE_SIZE}
                if cursor:
                    params["cursor"] = cursor
                if search:
                    params["search"] = search[:80]
                items, cursor = _items_and_cursor(await self._get(client, "/transactions", params))
                oldest: Optional[str] = None
                for raw in items:
                    t = normalise_transaction(raw)
                    if t is None:
                        continue
                    oldest = t["date"] if oldest is None or t["date"] < oldest else oldest
                    if since is None or t["date"] >= since.isoformat():
                        out.append(t)
                if not cursor or not items or (since is not None and oldest is not None and oldest < since.isoformat()):
                    break
                if page == MAX_PAGES - 1:
                    truncated = True
        return out, truncated
