"""
Locked slots — the engine owns every figure in the answer.

The live run showed the recurring failure is not choosing the wrong tool, it is
the model mishandling a correct tool result: relabelling a column, re-scaling
₹3,70,000 into "₹37 Lakh", or doing its own arithmetic beside the engine's.

So a turn with tool results now carries a slot table: a short name for every
figure the engine produced, already formatted. The answer model is told to write
`{{slot}}` instead of typing a number. `render` substitutes them afterwards, and
`stray_numbers` reports any figure the model typed that matches no slot, which
the agent uses to ask for one rewrite.

Nothing here can invent a value: slots are built only from tool output.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..finance.engine import inr

# Numbers a reply may contain without coming from a slot.
_ALLOWED_LITERAL = re.compile(
    r"^(?:"
    r"19\d{2}|20\d{2}"                       # years
    r"|20\d{2}\s*[-/]\s*\d{2}"               # tax years
    r"|[0-9]"                                # single digits: "one or two options", list numbering
    r"|1[0-2]"                               # months of the year, small counts
    r"|100|50|0"                             # percentages of the obvious kind
    r")$"
)
_NUMBER = re.compile(r"(?<![\w.])(?:₹\s*)?\d[\d,]*(?:\.\d+)?(?:\s*(?:lakhs?|lacs?|crores?|cr|k|%))?", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_.]+)\s*\}\}", re.IGNORECASE)
_UNIT = re.compile(r"\s*(lakhs?|lacs?|crores?|cr|k|%)$", re.IGNORECASE)

MONEY_HINTS = ("amount", "balance", "cost", "debt", "emi", "expense", "fee", "income", "interest",
               "payment", "principal", "rent", "salary", "saving", "surplus", "tax", "value", "worth",
               "corpus", "outflow", "budget", "due", "deduction", "rebate", "cess", "gain", "premium")
PERCENT_HINTS = ("rate", "apr", "percent", "pct", "yield", "return", "share")
MONTH_HINTS = ("month", "tenure")          # a duration, not "per_month" or "monthly"
NOT_MONTH = ("per_month", "monthly", "a_month", "month_payment")
SKIP_KEYS = ("operation", "inputs", "assumptions", "notes", "warnings", "law", "tax_year", "verified_on")
# Only deterministic, trusted producers may put figures in the system prompt. Anything fetched
# from the web stays in the external-data block, where it cannot be mistaken for TORA's own
# figures (an eval caught a researched rate reaching the trusted prompt through this path).
TRUSTED_TOOLS = ("finance_calc", "tax_calc", "calculator", "rules_lookup", "spendsy_data")
MAX_SLOTS = 28


def _looks_like(key: str, hints: Tuple[str, ...]) -> bool:
    lower = key.lower()
    return any(h in lower for h in hints)


def format_value(key: str, value: Any) -> Optional[str]:
    """The engine's value, written the way the user should see it (shared with answer.blocks)."""
    return _format(key, value)


def _format(key: str, value: Any) -> Optional[str]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() and any(ch.isdigit() for ch in value) else None
    if not isinstance(value, (int, float)):
        return None
    if _looks_like(key, PERCENT_HINTS):
        return f"{round(float(value), 2):g}%"
    if _looks_like(key, MONTH_HINTS) and not _looks_like(key, NOT_MONTH):
        months = int(round(float(value)))
        return f"{months} month{'s' if months != 1 else ''}"
    if _looks_like(key, MONEY_HINTS) or abs(float(value)) >= 1000:
        return inr(float(value))
    return f"{round(float(value), 2):g}"


def _walk(node: Any, prefix: str, out: Dict[str, str], depth: int = 0) -> None:
    if depth > 3 or len(out) >= MAX_SLOTS * 3:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key in SKIP_KEYS and depth == 0:
                continue
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                _walk(value, name, out, depth + 1)
            else:
                text = _format(str(key), value)
                if text:
                    out.setdefault(name, text)
    elif isinstance(node, list):
        for idx, item in enumerate(node[:6]):
            label = None
            if isinstance(item, dict):
                label = item.get("name") or item.get("item") or item.get("option") or item.get("label")
            name = f"{prefix}.{_slug(label)}" if label else f"{prefix}.{idx + 1}"
            _walk(item, name, out, depth + 1)


def _slug(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:34] or "item"


def build_slots(tool_context: Any) -> Dict[str, str]:
    """Every figure the engines produced this turn, formatted, keyed by a short name."""
    slots: Dict[str, str] = {}
    if tool_context is None or getattr(tool_context, "is_empty", lambda: True)():
        return slots
    for result in getattr(tool_context, "results", []):
        if getattr(result, "is_error", False) or not isinstance(result.output, dict):
            continue
        if result.tool_name not in TRUSTED_TOOLS:
            continue
        output = result.output
        prefix = _slug(output.get("operation") or result.tool_name)
        # Pre-formatted rupee figures come first: the engine already wrote them out.
        for key, value in (output.get("figures") or {}).items():
            if isinstance(value, str) and value.strip():
                slots[f"{prefix}.{_slug(key)}"] = value
        for row in output.get("comparison") or []:
            if not isinstance(row, dict):
                continue
            item = _slug(row.get("item") or row.get("option") or "row")
            for key, value in row.items():
                if key in ("item", "option") or not isinstance(value, str):
                    continue
                slots.setdefault(f"{prefix}.{item}.{_slug(key)}", value)
        _walk({k: v for k, v in output.items() if k not in ("figures", "comparison")}, prefix, slots)
        for key, value in (output.get("inputs") or {}).items():
            text = _format(str(key), value)
            if text:
                slots.setdefault(f"{prefix}.given.{_slug(key)}", text)
    return dict(list(slots.items())[: MAX_SLOTS * 3])


def slot_table(slots: Dict[str, str], limit: int = MAX_SLOTS) -> str:
    """The list the answer model sees: name, value, and the rule for using it."""
    if not slots:
        return ""
    lines = [f"- {{{{{name}}}}} = {value}" for name, value in list(slots.items())[:limit]]
    return (
        "\n\n## Figures For This Answer\n"
        "Every figure below was produced by the engines that just ran. Write the placeholder, exactly as shown "
        "including the braces, wherever the figure belongs in your sentence — it is replaced with the value "
        "before the user sees it.\n"
        + "\n".join(lines)
        + "\n- Do not write any other number, and do not re-scale or re-format these (₹3,70,000 is not ₹37 lakh). "
        "If a figure you need is not listed, say what you would need instead of estimating it."
    )


# The slot value already carries its unit ("9 months", "\u20b915,000"), so a model that writes
# "{{...}} months" or "\u20b9{{...}}" produces "9 months months" and "\u20b9\u20b915,000" after
# substitution. Seen live in a debt plan, so it is cleaned up rather than left to the prompt.
_DOUBLE_RUPEE = re.compile(r"\u20b9\s*\u20b9+")
_DOUBLE_UNIT = re.compile(
    r"\b(months?|years?|weeks?|days?|lakhs?|lacs?|crores?)(\*{0,2})\s+\1\b(\(s\))?",
    re.IGNORECASE,
)


def _tidy(text: str) -> str:
    text = _DOUBLE_RUPEE.sub("\u20b9", text)
    for _ in range(2):                       # "month month month" needs a second pass
        cleaned = _DOUBLE_UNIT.sub(r"\1\2", text)
        if cleaned == text:
            break
        text = cleaned
    return text


def render(text: str, slots: Dict[str, str]) -> Tuple[str, List[str]]:
    """Substitute {{slot}} placeholders; return the text and any unknown names."""
    unknown: List[str] = []

    def replace(match: re.Match) -> str:
        name = match.group(1).lower()
        if name in slots:
            return slots[name]
        for candidate, value in slots.items():          # tolerate a missing prefix
            if candidate.endswith("." + name) or candidate.split(".")[-1] == name:
                return value
        unknown.append(name)
        return ""

    return _tidy(_PLACEHOLDER.sub(replace, text or "")), unknown


def _number_key(token: str) -> Optional[float]:
    cleaned = token.replace("₹", "").replace(",", "").strip()
    unit = _UNIT.search(cleaned)
    if unit:
        cleaned = _UNIT.sub("", cleaned).strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    suffix = (unit.group(1).lower() if unit else "")
    if suffix.startswith(("lakh", "lac")):
        value *= 100000
    elif suffix.startswith(("crore", "cr")):
        value *= 10000000
    elif suffix == "k":
        value *= 1000
    return value


def _matches(value: float, allowed: set) -> bool:
    """Rounding drift is not an invented figure: ₹2,550 for the engine's ₹2,551 is the same number.
    The window stays tight (0.1%) so a re-scaled figure — ₹37 lakh for ₹3,70,000 — is still caught."""
    for candidate in allowed:
        if abs(value - candidate) <= max(1.0, abs(candidate) * 0.001):
            return True
    return False


def stray_numbers(text: str, slots: Dict[str, str], extra_allowed: Optional[List[str]] = None) -> List[str]:
    """Figures in the answer that match no slot value — what the model typed itself."""
    allowed_values = set()
    for value in list(slots.values()) + list(extra_allowed or []):
        key = _number_key(value)
        if key is not None:
            allowed_values.add(round(key, 2))
        for token in _NUMBER.findall(value):
            key = _number_key(token)
            if key is not None:
                allowed_values.add(round(key, 2))
    stray: List[str] = []
    for token in _NUMBER.findall(text or ""):
        cleaned = token.strip()
        bare = cleaned.replace("₹", "").replace(",", "").strip()
        if _ALLOWED_LITERAL.match(bare):
            continue
        value = _number_key(cleaned)
        if value is None or _matches(value, allowed_values):
            continue
        if cleaned not in stray:
            stray.append(cleaned)
    return stray


def repair_instruction(stray: List[str], unknown: List[str], slots: Dict[str, str]) -> str:
    parts = []
    if stray:
        parts.append("these figures are not in the list and must not appear: " + ", ".join(stray[:8]))
    if unknown:
        parts.append("these placeholders do not exist: " + ", ".join(f"{{{{{u}}}}}" for u in unknown[:8]))
    names = ", ".join(f"{{{{{n}}}}}" for n in list(slots)[:MAX_SLOTS])
    return (
        "Locked figures: rewrite the answer using only the placeholders provided — " + "; ".join(parts) + ". "
        "Available placeholders: " + names + ". Keep the same advice and structure; replace every number with its "
        "placeholder, and drop any figure you cannot support."
    )
