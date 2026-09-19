"""
The engine prints its own figures; the model writes only the judgement.

Measured across 61 live answers: 24% of every word the model typed was a table, a
figure list or a heading — content the engines had already computed and labelled.
On the long answers it was 35-62%. At 4.1 tokens/sec of decode that is 40-66
seconds per turn spent retyping a data structure.

Retyping is also where figures go wrong: a live debt plan turned ₹3,70,000 into
"₹37 Lakh", and another relabelled an interest column as total cost. Those are
transcription errors, and code does not make them.

So a turn whose tools produced figures gets a block rendered here, streamed ahead
of the model's text (at 4.1 tok/s the numbers would otherwise arrive a minute
late), and the model is told not to repeat it.

The figures come from the locked slots, not from raw tool output: slots are
already filtered to trusted tools, already formatted (₹3,70,000, 9 months, 40%),
and already the whitelist the grounding check uses. Building the block from
anything else would let the two disagree.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .slots import TRUSTED_TOOLS, format_value

MAX_ROWS = 8
# The row label plus two columns is what fits a 390px phone (312px of room, measured in a
# browser). With three the table ran to 391px: it scrolled, but nothing on screen said so, so
# the last column simply looked cut off — worse than not showing it. TORA's users are on
# phones, so the phone wins; what does not fit goes in the figure list under the table.
MAX_COLUMNS = 2
MAX_PER_SECTION = 14   # a three-bucket budget is 12 figures plus what is left over
MAX_SECTIONS = 3
# Container names that say nothing about the figure underneath them.
GENERIC_GROUPS = frozenset({"buckets", "items", "rows", "results", "values", "plan", "detail"})
# Acronyms the generic title-case would mangle.
TITLES = {
    "emi": "EMI", "sip_future_value": "SIP growth", "required_sip": "Monthly SIP needed",
    "hra_exemption": "HRA exemption", "itr_form_choice": "ITR form", "debt_to_income": "Debt-to-income",
    "compute_tax": "Tax", "compare_regimes": "Old regime vs new", "capital_gains_tax": "Capital gains tax",
    "advance_tax_plan": "Advance tax", "ltcg": "LTCG", "stcg": "STCG",
}


def _label(key: str) -> str:
    # "actual_percent" must not collapse to "Actual": the budget block then showed "Actual:
    # ₹72,348" and "Actual: 76.2%" as if they were the same field. It becomes "Actual %".
    text = re.sub(r"\s+(percent|pct)$", " %", re.sub(r"[_\-]+", " ", str(key)).strip())
    return TITLES.get(str(key), text[:1].upper() + text[1:] if text else text)


def _field_label(field: str) -> str:
    """A nested figure keeps the group it belongs to, so "Needs target" is not just "Target".

    Live on a budget plan the block listed Target twice and Actual four times with nothing saying
    which bucket each belonged to — the same relabelling problem the locked slots exist to stop.
    """
    parts = [p for p in str(field).split(".") if p]
    if len(parts) >= 2 and not parts[-2].isdigit() and parts[-2] not in GENERIC_GROUPS:
        return f"{_label(parts[-2])} {_label(parts[-1]).lower()}"
    return _label(parts[-1]) if parts else ""


def _rows(comparison: Any) -> List[Dict[str, Any]]:
    """Engines shape `comparison` two ways: a list of rows, or a dict keyed by the row's name."""
    if isinstance(comparison, list):
        return [r for r in comparison if isinstance(r, dict)]
    if isinstance(comparison, dict):
        return [dict(row, item=row.get("item") or row.get("option") or name)
                for name, row in comparison.items() if isinstance(row, dict)]
    return []


def _cell(key: str, value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):                      # {"name": "Credit card", "month": 3}
        name = value.get("name") or value.get("item")
        month = value.get("month") or value.get("months")
        if name and month is not None:
            return f"{name} ({format_value('months', month)})"
        return None
    return format_value(key, value)


def _comparison_table(comparison: Any) -> List[str]:
    """The engine's own comparison rows, with the engine's own column labels."""
    rows = _rows(comparison)[:MAX_ROWS]
    cells = [{k: _cell(k, v) for k, v in row.items() if k not in ("item", "option")} for row in rows]
    columns: List[str] = []
    for row in cells:
        for key, value in row.items():
            if value is not None and key not in columns:
                columns.append(key)
    columns = columns[:MAX_COLUMNS]
    if not columns or len(rows) < 2:
        return []
    out = ["| | " + " | ".join(_label(c) for c in columns) + " |",
           "|---|" + "|".join("---" for _ in columns) + "|"]
    for row, cell in zip(rows, cells):
        name = _label(str(row.get("item") or row.get("option") or "").strip()) or "\u2014"
        out.append("| **" + name + "** | " + " | ".join(cell.get(c) or "\u2014" for c in columns) + " |")
    return out


def _group(slots: Dict[str, str]) -> Dict[str, List[tuple]]:
    """Slot names are `operation.field`, `operation.a.b` when nested, `operation.given.field` for
    the user's own inputs. Shallower fields are the headline results, so they come first."""
    grouped: Dict[str, List[tuple]] = {}
    givens: Dict[str, set] = {}
    for name, value in slots.items():
        head, _, rest = name.partition(".")
        if not rest:
            continue
        if rest.startswith("given."):
            givens.setdefault(head, set()).add(value)
            continue
        grouped.setdefault(head, []).append((rest.count("."), rest, value))
    out: Dict[str, List[tuple]] = {}
    for head, fields in grouped.items():
        given = givens.get(head, set())
        seen: set = set()
        rows: List[tuple] = []
        for _, field, value in sorted(fields, key=lambda f: f[0]):
            leaf = field.split(".")[-1]
            if leaf == "summary" or len(value) > 40 or value.endswith("."):
                continue                      # a sentence, not a figure
            if value in given:
                continue                      # the user's own input, handed back
            label = _label(leaf)
            if (label, value) in seen:
                continue                      # the same figure under a second path
            seen.add((label, value))
            rows.append((field, value))
        out[head] = rows
    return out


def render_block(tool_context: Any, slots: Dict[str, str]) -> str:
    """The figures this turn produced, ready to show. Empty when there is nothing worth showing."""
    if not slots or tool_context is None or getattr(tool_context, "is_empty", lambda: True)():
        return ""
    comparisons: Dict[str, Any] = {}
    for result in getattr(tool_context, "results", []):
        if getattr(result, "is_error", False) or not isinstance(result.output, dict):
            continue
        if result.tool_name not in TRUSTED_TOOLS:
            continue
        if result.output.get("comparison"):
            comparisons[str(result.output.get("operation") or result.tool_name)] = result.output["comparison"]

    sections: List[str] = []
    for operation, fields in list(_group(slots).items())[:MAX_SECTIONS]:
        table = _comparison_table(comparisons.get(operation)) if operation in comparisons else []
        # A figure the table already shows would only be repeated by the list below it.
        shown = {cell.strip() for line in table for cell in line.split("|")}
        body = list(table)
        # Truncating mid-group is its own kind of wrong: a budget that lists Needs and Wants but
        # stops before Savings reads as though there is no savings bucket. Groups go in whole.
        groups: Dict[str, List[str]] = {}
        for field, value in fields:
            if value in shown:
                continue
            parts = [p for p in field.split(".") if p]
            key = parts[-2] if len(parts) >= 2 and not parts[-2].isdigit() else ""
            groups.setdefault(key, []).append(f"- **{_field_label(field)}:** {value}")
        lines: List[str] = []
        for group_lines in groups.values():
            if lines and len(lines) + len(group_lines) > MAX_PER_SECTION:
                break
            lines += group_lines
        if lines:
            if body:
                body.append("")
            body += lines[:max(MAX_PER_SECTION, len(lines))]
        if not body:
            continue
        sections.append(f"**{_label(operation)}**")
        sections += body
        sections.append("")
    if not sections:
        return ""
    return "\n".join(sections).rstrip() + "\n\n"


def block_note(block: str) -> str:
    """What the model is told about a block that is already on the user's screen."""
    if not block:
        return ""
    return (
        "\n\n## Figures Already Shown\n"
        "The figures this turn produced are already displayed to the user, immediately above where "
        "your answer will appear. Do not repeat them, do not restate them as a list or a table, and "
        "do not introduce them (“here is the summary”, “I ran the numbers”). Write only what the "
        "figures cannot say for themselves: what they mean for this person, what to do first, and "
        "what to watch out for. A figure may be named again only where the sentence would be "
        "meaningless without it."
    )


# Measured live: told plainly that the figures were already on screen, gemma4:e4b restated all
# eight of them anyway and the answer grew 368 -> 443 words. Prompt rules are advisory on this
# model, so the repetition is removed rather than requested. Only label-shaped lines go: a list
# item or table row whose numbers all came from the block and which carries no advice of its own.
_LIST_LINE = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|\|)")
_NUMBER = re.compile(r"(?:₹\s*)?\d[\d,]*(?:\.\d+)?(?:\s*(?:lakhs?|lacs?|crores?|cr|k|%|months?|years?))?",
                     re.IGNORECASE)
_WORDS = re.compile(r"[A-Za-z]{2,}")
MAX_LABEL_WORDS = 8


def _values(text: str) -> set:
    from .slots import _number_key
    found = set()
    for token in _NUMBER.findall(text or ""):
        key = _number_key(re.sub(r"\s*(months?|years?)$", "", token, flags=re.IGNORECASE))
        if key is not None:
            found.add(round(key, 2))
    return found


def strip_repeats(content: str, block: str) -> str:
    """Drop the lines where the model simply retyped the block."""
    if not block or not content:
        return content
    known = _values(block)
    if not known:
        return content
    kept: List[str] = []
    for line in content.splitlines():
        if not _LIST_LINE.match(line):
            kept.append(line)
            continue
        numbers = _values(line)
        words = len(_WORDS.findall(re.sub(r"\*\*.*?\*\*", " ", line)))
        if numbers and numbers <= known and words <= MAX_LABEL_WORDS:
            continue                       # a label and a figure the user can already see
        kept.append(line)
    text = "\n".join(kept)
    # An introduction whose list has just gone ("Here is the plan summary:") is now dangling.
    text = re.sub(r"(?m)^[^\n]{0,80}:[ \t]*\n(?=\s*(?:\n|$))", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
