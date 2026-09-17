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
    r"(?:₹|rs\.?|inr)?\s*(?<![\d.,\w])(\d(?:[\d,]*\d)?(?:\.\d+)?)(?![\d.])(?:\s*(" + _UNIT + r")|(?![a-z]))"
    r"(?!\s*(?:%|percent|years?|yrs?|months?|mos?\b|am\b|pm\b|a\.m|p\.m|days?\b|hours?\b))",
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
_RULE_QUESTION = re.compile(
    r"\b(?:limit|maximum\s+deduction|deadline|last\s+date|due\s+date|which\s+section|under\s+(?:which|what)\s+section|"
    r"section\s*\d+[a-z]*|eligib\w*|exemption\s+rules?|rules?\s+(?:for|on|about)|is\s+\w+\s+(?:allowed|taxable|exempt)|"
    r"can\s+(?:a\s+)?recovery\s+agents?|ombudsman|complain\w*\s+(?:against|about))\b",
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


def _labelled_amount(text: str, label: str) -> Optional[float]:
    """Amount written right after (or right before) a label, e.g. 'basic is 50k' / '25k rent'."""
    m = re.search(r"\b" + label + r"\b(?:\s+(?:is|of|=|:|was|about|around|received))*\s*((?:₹|rs\.?\s*)?\d[\d,]*(?:\.\d+)?\s*(?:" + _UNIT + r")?)", text)
    if not m:
        m = re.search(r"((?:₹|rs\.?\s*)?\d[\d,]*(?:\.\d+)?\s*(?:" + _UNIT + r")?)\s+(?:as\s+|in\s+|of\s+)?" + label + r"\b", text)
    if not m:
        return None
    values = _money_values(m.group(1))
    return values[0] if len(values) == 1 else None


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
    tools = available_tools if available_tools is not None else {"calculator", "finance_calc", "tax_calc", "rules_lookup"}
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

    # 7a. HRA exemption: basic, HRA and rent all given
    if "tax_calc" in tools and re.search(r"\b(?:hra|house\s+rent\s+allowance)\b", lower) and re.search(r"\bexempt", lower):
        basic = _labelled_amount(lower, r"basic(?:\s+salary|\s+pay)?")
        hra_amt = _labelled_amount(lower, r"(?:hra|house\s+rent\s+allowance)")
        rent = _labelled_amount(lower, r"(?:rent(?:\s+paid)?|pay\s+(?:a\s+)?rent\s+of|pay)")
        if basic and hra_amt and rent:
            params: Dict[str, Any] = {"basic_monthly": basic, "hra_received_monthly": hra_amt, "rent_paid_monthly": rent}
            if re.search(r"\b(?:mumbai|delhi|kolkata|chennai)\b", lower):
                params["metro"] = True
            elif re.search(r"\bnon[- ]?metro\b", lower):
                params["metro"] = False
            elif re.search(r"\bmetro\b", lower):
                params["metro"] = True
            else:
                params["metro"] = False
            if re.search(r"\b(?:a|per)\s+year\b|\bannual\w*\b|\byearly\b", lower):
                return None  # yearly figures: let the planner convert
            return _plan("tax_calc", {"operation": "hra_exemption", "params": params}, "hra_exemption")

    # 7. Rule / limit / deadline questions -> reviewed rules library
    if ("rules_lookup" in tools and _RULE_QUESTION.search(lower) and not _money_values(lower)
            and not re.search(r"\b(?:credit|card)\s+limit|limit\s+on\s+my\b", lower)
            and percent is None and not re.search(r"\b(?:calculate|compute|how\s+much\s+tax|my\s+tax)\b", lower)):
        args: Dict[str, Any] = {"query": text[:200]}
        ty = _TAX_YEAR.search(lower)
        if ty:
            args["tax_year"] = f"{ty.group(1)}-{ty.group(2)}"
        return _plan("rules_lookup", args, "rules_lookup")

    # 8. Simple salary tax
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
    r"avalanche|snowball|debt[- ]free|retire\w*|right\s+(?:regime|choice|option|move)|save\s+(?:more\s+)?tax|"
    r"tax\s+planning|minimi[sz]e|where\s+do\s+i\s+stand|right\s+tax\s+regime|get\s+out\s+of\s+debt|can'?t\s+manage|drowning)\b",
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
        if re.search(r"\b(?:or|vs\.?|versus)\b", lower):
            score += 1
            reasons.append("choosing between alternatives")
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


# ── document-driven plans ────────────────────────────────────────────────────

_TAX_REVIEW = re.compile(
    r"\b(?:right\s+(?:tax\s+)?regime|which\s+regime|regime\s+(?:is\s+)?better|save\s+(?:more\s+)?tax|tax\s+saving|"
    r"reduce\s+(?:my\s+)?tax|unused\s+deduction|missed\s+deduction|claim\s+more)\b",
    re.IGNORECASE,
)


def document_plan(message: str, documents: List[Dict[str, Any]]) -> Optional[ToolPlan]:
    """Use the latest Form 16 for regime / tax-saving questions (no need to re-type the figures)."""
    if not documents or not _TAX_REVIEW.search(normalize_message(message or "")):
        return None
    if any(v >= 1000 for v in _money_values(message.lower())):
        return None  # the user gave new figures; let the planner combine them
    form16 = next((d for d in reversed(documents) if d.get("doc_type") == "form16"), None)
    s = (form16 or {}).get("summary") or {}
    if not s.get("gross_salary"):
        return None
    params: Dict[str, Any] = {"gross_salary": s["gross_salary"]}
    for src, dst in (("section_80c", "section_80c"), ("section_80d", "section_80d_self"),
                     ("section_80ccd_1b", "section_80ccd_1b"), ("hra_exempt", "hra_exempt_annual")):
        if s.get(src):
            params[dst] = s[src]
    if s.get("regime") in ("old", "new"):
        params["current_regime"] = s["regime"]
    if s.get("tax_year"):
        params["tax_year"] = s["tax_year"]
    return _plan("tax_calc", {"operation": "tax_saving_finder", "params": params}, "form16 tax review")

# --- Debt rescue from remembered facts -------------------------------------------------
# "How do I get out of debt?" is the case TORA exists for. Left to the planner, the model
# often answers with its own arithmetic and ignores interest, so route it to the engine.
_DEBT_RESCUE = re.compile(
    r"\b(?:get|getting|come|coming|dig|climb|way)\s+out\s+of\s+(?:this\s+|my\s+|the\s+)?debts?\b"
    r"|\bdebt[-\s]?free\b"
    r"|\b(?:clear|repay|pay\s*off|pay\s*down|get\s*rid\s*of|tackle|attack)\s+(?:all\s+)?(?:my|these|those|the)\s+"
    r"(?:debts?|loans?\s+and\s+cards?|credit\s*cards?\s+and\s+loans?)\b"
    r"|\b(?:debt|repayment|payoff|pay[-\s]?off)\s+(?:rescue\s+)?plan\b"
    r"|\bplan\s+to\s+(?:clear|repay|pay\s*off)\s+(?:my\s+)?debts?\b"
    r"|\bhow\s+do\s+i\s+(?:escape|survive)\s+(?:this\s+)?debt\b",
    re.IGNORECASE,
)
# Typical card minimum in India when the user hasn't said: 5% of the balance. Interest
# rates are never assumed — without them the plan would understate the cost of waiting.
MIN_DUE_FRACTION = 0.05
DEBT_LABELS = {
    "credit_card": "Credit card",
    "personal_loan": "Personal loan",
    "home_loan": "Home loan",
    "car_loan": "Car loan",
    "education_loan": "Education loan",
    "gold_loan": "Gold loan",
}


def _facts(profile: Any) -> Dict[str, float]:
    """name -> current value, for a FinancialProfile (or anything with iter_current_facts)."""
    out: Dict[str, float] = {}
    if profile is None or not hasattr(profile, "iter_current_facts"):
        return out
    for fact in profile.iter_current_facts():
        try:
            value = float(fact.value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[fact.name] = value
    return out


def _debts_from_facts(facts: Dict[str, float]) -> Optional[List[Dict[str, Any]]]:
    """Every known debt, or None when one of them is missing a figure the engine needs."""
    debts: List[Dict[str, Any]] = []
    balance = facts.get("credit_card_debt")
    if balance:
        if not facts.get("credit_card_apr"):
            return None
        debts.append({
            "name": DEBT_LABELS["credit_card"],
            "balance": balance,
            "apr": facts["credit_card_apr"],
            "min_payment": facts.get("credit_card_min_due") or round(balance * MIN_DUE_FRACTION),
        })
    for kind in ("personal_loan", "car_loan", "education_loan", "gold_loan", "home_loan"):
        balance = facts.get(f"{kind}_balance")
        emi = facts.get(f"{kind}_emi")
        rate = facts.get(f"{kind}_rate")
        if not balance and not emi:
            continue
        if not balance or not emi or not rate:
            return None  # leaving a real debt out would make the plan look better than it is
        debts.append({"name": DEBT_LABELS[kind], "balance": balance, "apr": rate, "min_payment": emi})
    return debts


def _essentials(facts: Dict[str, float]) -> Optional[float]:
    if facts.get("essential_expenses"):
        return facts["essential_expenses"]
    parts = [facts[k] for k in ("rent", "food", "commute", "utilities") if facts.get(k)]
    return sum(parts) if len(parts) >= 2 else None


def profile_plan(message: str, intent: Any, profile: Any, available_tools: Optional[set] = None) -> Optional[ToolPlan]:
    """A debt-rescue plan built from what TORA already knows, so the figures come from the engine."""
    tools = available_tools if available_tools is not None else {"finance_calc"}
    if "finance_calc" not in tools or not message or not _DEBT_RESCUE.search(normalize_message(message)):
        return None
    facts = _facts(profile)
    income = facts.get("income")
    debts = _debts_from_facts(facts)
    essentials = _essentials(facts)
    if not income or not debts or essentials is None or essentials >= income:
        return None  # ask the user instead of inventing the missing half
    params: Dict[str, Any] = {
        "debts": debts,
        "monthly_income": income,
        "essential_expenses": essentials,
    }
    if facts.get("savings"):
        params["current_savings"] = facts["savings"]
    return _finance("debt_rescue_plan", params)
