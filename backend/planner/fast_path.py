"""
Phase 7 — fast routing and complexity scaling.

`fast_plan` builds a tool plan straight from the message when the request is
unambiguous (all inputs present, one reading only). That skips the planner LLM
call, which is the slowest step on CPU (median ~76 s in the live run).
Anything unclear returns None and goes to the normal planner.

`assess_complexity` sizes the case (simple / standard / complex) so the agent
can scale effort: complex cases can use a stronger model, reasoning mode and
the option-comparison answer style.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..context.extractor import parse_inr_amount
from ..context.normalize import normalize_message
from .models import ToolPlan, ToolPlanStep

_UNIT = r"(?:lakhs?|lacs?|lac|crores?|cr|thousand|grand|k|l)\b"
_MONEY = re.compile(
    r"(?:₹|rs\.?|inr)?\s*(?<![\d.,])(\d(?:[\d,]*\d)?(?:\.\d+)?)(?![\d.])\s*(" + _UNIT + r")?(?!\s*(?:%|percent|years?|yrs?|months?|mos?\b))",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b|per\s*cent\b)", re.IGNORECASE)
_YEARS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b", re.IGNORECASE)
_MONTHS = re.compile(r"(\d+)\s*(?:months?|mos?)\b", re.IGNORECASE)
_TAX_YEAR = re.compile(r"\b(20\d{2})\s*[-–/]\s*(\d{2})\b")
_PURE_MATH = re.compile(r"^[\d\s.,+\-*/x×÷()^%]+$")
_FOLLOWUP_REF = re.compile(
    r"\b(?:that|this(?!\s+(?:year|month|fy|financial\s+year|tax\s+year))|same|it|those|above|previous|my\s+loan|the\s+loan)\b",
    re.IGNORECASE,
)
_WHAT_IF_WORDS = re.compile(r"\b(?:increase|decrease|raise|reduce|step[- ]?up|change|extra|prepay\w*|instead)\b", re.IGNORECASE)
_TAX_COMPLEX = re.compile(
    r"\b(?:80c|80d|80ccd|hra|home\s*loan|house\s*property|rent\s*paid|gains?|stcg|ltcg|other\s+income|interest\s+income|"
    r"business|freelanc|deduction|nps|senior|super\s*senior|surcharge|capital|rebate\s+for|bonus|arrears|"
    r"property|shares?|mutual|crypto|pension|agricultur|exempt|allowance|lta|claim|insurance|medical|donation|"
    r"80g|80e|deduct|invest|epf|ppf|elss|perquisite|esop|gratuity|leave\s+encash|foreign|nri|resident)\w*",
    re.IGNORECASE,
)
_MONTHLY_WORDS = re.compile(r"\b(?:a|per|every|each)\s+month\b|\bmonthly\b|\bpm\b|/\s*month", re.IGNORECASE)


def _money_values(text: str) -> List[float]:
    values = []
    for m in _MONEY.finditer(text):
        raw = m.group(1) + (" " + m.group(2) if m.group(2) else "")
        # skip parts of tax years like "2026-27" and percentages / periods handled elsewhere
        tail = text[m.end():m.end() + 3]
        if re.match(r"\s*[-–/]\s*\d{2}\b", tail) or re.search(r"\d\s*[-–/]\s*$", text[:m.start()]):
            continue
        amt = parse_inr_amount(raw)
        if amt is not None and amt > 0:
            values.append(amt)
    return values


def _one(values: List[float]) -> Optional[float]:
    uniq = sorted(set(values))
    return uniq[0] if len(uniq) == 1 else None


def _single(regex: re.Pattern, text: str) -> Optional[float]:
    found = {float(m.group(1)) for m in regex.finditer(text)}
    return found.pop() if len(found) == 1 else None


def _plan(tool: str, arguments: Dict[str, Any], why: str) -> ToolPlan:
    return ToolPlan(requires_tools=True, steps=[ToolPlanStep(tool_name=tool, arguments=arguments)],
                    thought=f"fast path: {why}")


def _finance(operation: str, params: Dict[str, Any]) -> ToolPlan:
    return _plan("finance_calc", {"operation": operation, "params": params}, operation)


def _strip_amount_words(text: str) -> str:
    """'200000 * 0.36 / 12' style check after removing lead-in words."""
    t = re.sub(r"^\s*(?:please\s+)?(?:calculate|compute|what\s+is|what's|whats|evaluate|solve)\s*:?\s*", "", text, flags=re.I)
    return t.rstrip(" ?.=")


def fast_plan(message: str, intent: Any = None, available_tools: Optional[set] = None) -> Optional[ToolPlan]:
    """Return a ready plan for clear-cut requests, or None to use the planner."""
    if not message or len(message) > 400:
        return None
    text = normalize_message(message.strip())
    lower = text.lower()
    tools = available_tools if available_tools is not None else {"calculator", "finance_calc", "tax_calc"}
    if _FOLLOWUP_REF.search(lower) and not re.search(r"\bwhat\s+is\b", lower[:10]):
        # "that loan", "the same amount" -> needs conversation context: planner
        return None

    # 1. Plain arithmetic: "Calculate 200000 * 0.36 / 12"
    expr = _strip_amount_words(text)
    if "calculator" in tools and _PURE_MATH.match(expr) and re.search(r"\d\s*[-+*/x×÷^]\s*\d", expr):
        cleaned = expr.replace(",", "").replace("×", "*").replace("÷", "/").replace("^", "**")
        cleaned = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "*", cleaned)
        if "%" not in cleaned:
            return _plan("calculator", {"expression": cleaned.strip()}, "arithmetic")

    # 2. "What is 20% of 60000?"
    m = re.fullmatch(r"(?:what\s+is\s+|what's\s+|calculate\s+)?(\d+(?:\.\d+)?)\s*%\s+of\s+(.+?)\s*\??", lower)
    if "calculator" in tools and m:
        base = _one(_money_values(m.group(2)))
        if base is not None and re.fullmatch(r"[₹rs.\s\d,]*(?:" + _UNIT + r")?\s*", m.group(2)):
            return _plan("calculator", {"expression": f"{float(m.group(1))} / 100 * {base:g}"}, "percent_of")

    percent = _single(_PERCENT, lower)
    years = _single(_YEARS, lower)
    months = _single(_MONTHS, lower)
    money = [v for v in _money_values(lower)]

    if "finance_calc" in tools and not _WHAT_IF_WORDS.search(lower):
        # 3. EMI
        if re.search(r"\bemis?\b", lower) and percent is not None and (years or months) and not (years and months):
            principal = _one([v for v in money if v >= 1000])
            if principal is not None:
                tenure = int(round(years * 12)) if years else int(months)
                return _finance("emi", {"principal": principal, "annual_rate": percent, "tenure_months": tenure})

        # 4. Required SIP for a target
        if (re.search(r"\b(?:how\s+much|what)\b.*\b(?:sip|invest|save)\b", lower)
                and re.search(r"\b(?:need|required|to\s+(?:build|reach|get|accumulate|have|make))\b", lower)
                and percent is not None and years is not None):
            target = _one(money)
            if target is not None and target >= 10000:
                return _finance("required_sip", {"target_amount": target, "annual_return": percent, "years": years})

        # 5. SIP future value
        if (re.search(r"\b(?:sip|invest\w*|put)\b", lower) and _MONTHLY_WORDS.search(lower)
                and re.search(r"\b(?:what\s+will|how\s+much\s+will|future\s+value|end\s+up|will\s+i\s+(?:have|get)|grow)\b", lower)
                and percent is not None and years is not None):
            monthly = _one(money)
            if monthly is not None:
                return _finance("sip_future_value", {"monthly_investment": monthly, "annual_return": percent, "years": years})

        # 6. Inflation
        if re.search(r"\binflation\b", lower) and percent is not None and years is not None:
            amount = _one(money)
            if amount is not None:
                direction = "present_value" if re.search(r"\b(?:worth\s+today|today'?s\s+(?:money|value)|present\s+value)\b", lower) \
                    else "future_cost"
                return _finance("inflation_adjust", {"amount": amount, "inflation_rate": percent, "years": years,
                                                     "direction": direction})

    # 7. Simple salary tax
    if "tax_calc" in tools and re.search(r"\b(?:tax|regime)\b", lower) and not _TAX_COMPLEX.search(lower) and percent is None:
        salary = _one([v for v in money if v >= 10000])
        # Only salary wording: bare "income" could be other income (no standard deduction) -> planner.
        if salary is not None and re.search(r"\b(?:salary|ctc|package|earn\w*|take[- ]?home)\b", lower):
            annual = salary * 12 if _MONTHLY_WORDS.search(lower) else salary
            if annual < 100000:
                return None  # too small to be an annual salary; let the planner ask
            params: Dict[str, Any] = {"gross_salary": round(annual, 2)}
            ty = _TAX_YEAR.search(lower)
            if ty:
                params["tax_year"] = f"{ty.group(1)}-{ty.group(2)}"
            compare = bool(re.search(r"\b(?:which|better|compare|comparison|vs|versus|both)\b.*\bregime|\bregime\b.*\b(?:better|compare)", lower))
            if compare:
                return _plan("tax_calc", {"operation": "compare_regimes", "params": params}, "compare_regimes")
            if re.search(r"\bold\s+regime\b", lower):
                params["regime"] = "old"
            elif re.search(r"\bnew\s+regime\b", lower):
                params["regime"] = "new"
            return _plan("tax_calc", {"operation": "compute_tax", "params": params}, "compute_tax")
    return None


# ── complexity ───────────────────────────────────────────────────────────────

_DECISION_WORDS = re.compile(
    r"\b(?:should\s+i|best|better|which\s+(?:one|option|is)|optimi[sz]e|strategy|afford|worth\s+it|"
    r"prioriti[sz]e|plan\s+my|what\s+do\s+i\s+do|help\s+me\s+(?:decide|plan|get\s+out|clear)|"
    r"trade[- ]?off|pros\s+and\s+cons|recommend|restructur|consolidat|refinanc|settle\w*|pay\s*off|payoff|"
    r"avalanche|snowball|debt[- ]free|retire\w*)\b",
    re.IGNORECASE,
)
_MULTI_ITEM = re.compile(r"\b(?:loans?|cards?|emis?|goals?|debts?|investments?|policies|properties)\b", re.IGNORECASE)


@dataclass
class Complexity:
    level: str                      # simple | standard | complex
    score: int
    reasons: List[str] = field(default_factory=list)

    @property
    def is_complex(self) -> bool:
        return self.level == "complex"


def assess_complexity(message: str, intent: Any = None, fast: Optional[ToolPlan] = None,
                      known_fact_count: int = 0) -> Complexity:
    """Size the case so effort can scale with it."""
    text = normalize_message(message or "")
    lower = text.lower()
    reasons: List[str] = []
    score = 0
    intent_value = getattr(getattr(intent, "intent", None), "value", None)
    if fast is not None:
        return Complexity("simple", 0, ["fast path"])
    if intent_value in ("memory_update", "memory_recall", "memory_delete", "clarification"):
        return Complexity("simple", 0, [intent_value])
    if _DECISION_WORDS.search(lower):
        score += 2
        reasons.append("decision or strategy question")
    amounts = len(_money_values(lower))
    if amounts >= 4:
        score += 2
        reasons.append(f"{amounts} amounts")
    elif amounts >= 2:
        score += 1
    items = len(_MULTI_ITEM.findall(lower))
    if items >= 3:
        score += 1
        reasons.append("several debts/goals/products")
    if len(text) > 280:
        score += 1
        reasons.append("long description")
    if intent_value in ("planning", "comparison", "what_if"):
        score += 1
        reasons.append(intent_value)
    if known_fact_count >= 5 and _DECISION_WORDS.search(lower):
        score += 1
        reasons.append("rich profile")
    level = "complex" if score >= 4 else ("standard" if score >= 1 or intent_value not in (None, "general_qa") else "simple")
    return Complexity(level, score, reasons)
