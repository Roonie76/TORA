"""
Model-assisted fact extraction (Phase 5E).

The rule-based extractor is precise but misses everyday phrasing ("meri salary
80 hazaar hai", "I pull in about 90 grand a month after tax", "FD of 3 lakh at
SBI"). When the rules find nothing in a message that clearly states personal
money facts, the LLM proposes facts as constrained JSON — and every proposal is
checked by rules before it can touch memory:

- the fact name must be on an allow-list;
- the quoted evidence must appear verbatim in the user's message and parse to
  the proposed value (±1%), so the model cannot invent numbers;
- hypothetical/conditional wording always wins over a "current" label;
- questions without a first-person statement never create facts.

TORA_LLM_EXTRACTION = auto (default) | off
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from .extractor import (
    FactExtractor,
    _FIRST_PERSON,
    _income_monthly,
    _is_question,
    parse_inr_amount,
)
from .financial import FactStatus

logger = logging.getLogger("tora.context.llm_extractor")

EXTRACTOR_PREFIX = "You are TORA's Fact Extractor"

# name -> (category, default period)
ALLOWED_FACTS: Dict[str, tuple] = {
    "income": ("income", "monthly"),
    "rent": ("rent", "monthly"),
    "savings": ("savings", "lump_sum"),
    "credit_card_debt": ("debt", "lump_sum"),
    "credit_card_apr": ("debt", "interest_rate"),
    "personal_loan_emi": ("loan", "monthly"),
    "personal_loan_balance": ("loan", "lump_sum"),
    "home_loan_emi": ("loan", "monthly"),
    "home_loan_balance": ("loan", "lump_sum"),
    "car_loan_emi": ("loan", "monthly"),
    "car_loan_balance": ("loan", "lump_sum"),
    "education_loan_balance": ("loan", "lump_sum"),
    "gold_loan_emi": ("loan", "monthly"),
    "gold_loan_balance": ("loan", "lump_sum"),
    "education_loan_emi": ("loan", "monthly"),
    "sip_monthly": ("investment", "monthly"),
    "mutual_funds": ("investment", "lump_sum"),
    "fixed_deposit": ("investment", "lump_sum"),
    "stocks": ("investment", "lump_sum"),
    "gold": ("investment", "lump_sum"),
    "ppf": ("investment", "lump_sum"),
    "epf": ("investment", "lump_sum"),
    "food": ("expense", "monthly"),
    "commute": ("expense", "monthly"),
    "utilities": ("expense", "monthly"),
    "insurance_premium": ("expense", "annual"),
    "car_goal": ("goal", "lump_sum"),
    "house_goal": ("goal", "lump_sum"),
}

_MONEYISH = re.compile(r"\d|\b(?:hazaar|hazar|thousand|lakh|lac|crore|grand)\b", re.IGNORECASE)
_FINANCE_HINT = re.compile(
    r"\b(?:salary|income|earn|kamata|kamati|kamai|paisa|paise|rent|kiraya|emi|loan|debt|card|fd|deposit|sip|"
    r"mutual|fund|stock|share|gold|ppf|epf|save|saving|bachat|invest|spend|kharcha|expense|premium|insurance|"
    r"take[- ]?home|in[- ]?hand|ctc|package|bonus|grand|rupees?|rs|inr|₹|pull(?:s|ing)? in|bring(?:s|ing)? in|"
    r"make|making|jobs?|freelanc|stipend|pension|amount)",
    re.IGNORECASE,
)
_HINGLISH_FIRST_PERSON = re.compile(r"\b(?:mera|meri|mere|main|mai|mujhe|hum|humara|hamara)\b", re.IGNORECASE)
_WORD_NUMBERS = {"hazaar": 1_000, "hazar": 1_000, "thousand": 1_000, "grand": 1_000}


def extraction_mode() -> str:
    mode = os.getenv("TORA_LLM_EXTRACTION", "auto").strip().lower()
    return mode if mode in ("auto", "off") else "auto"


def should_try(message: str, rule_candidates: List[Dict[str, Any]]) -> bool:
    """Only call the model when rules found nothing but the message looks like a personal money statement."""
    if extraction_mode() == "off" or rule_candidates:
        return False
    text = message or ""
    if len(text) > 1200 or not _MONEYISH.search(text) or not _FINANCE_HINT.search(text):
        return False
    first_person = bool(_FIRST_PERSON.search(text) or _HINGLISH_FIRST_PERSON.search(text))
    if not first_person:
        return False
    if re.search(r"\d\s*[*/+^×÷]\s*\d|\bcalculate\b", text, re.IGNORECASE):
        return False
    if FactExtractor.classify_statement_status(text) != FactStatus.CURRENT.value:
        # what-if / future wording: not worth an extra model call for scenario-only facts
        return False
    return True


def _evidence_amount(evidence: str) -> Optional[float]:
    text = evidence.lower()
    for word, mult in _WORD_NUMBERS.items():
        m = re.search(r"(\d+(?:\.\d+)?)\s*" + word, text)
        if m:
            return float(m.group(1)) * mult
    return parse_inr_amount(evidence)


def build_messages(message: str) -> List[Dict[str, str]]:
    names = ", ".join(ALLOWED_FACTS)
    system = (
        f"{EXTRACTOR_PREFIX}. Extract only facts the user states about their OWN finances in the message "
        "(English, Hindi or Hinglish). Ignore questions, examples and other people's numbers.\n"
        f"Allowed names: {names}.\n"
        "For each fact return: name, value (a plain number in rupees; 1 lakh = 100000, 1 crore = 10000000, "
        "80 hazaar = 80000; for percentages the number itself), status (current | hypothetical | historical), "
        "and evidence — the exact words from the message that contain the amount, copied verbatim.\n"
        'Reply with JSON only: {"facts": [...]} — or {"facts": []} if there are none.'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": message}]


SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": list(ALLOWED_FACTS)},
                    "value": {"type": "number"},
                    "status": {"type": "string", "enum": ["current", "hypothetical", "historical"]},
                    "evidence": {"type": "string"},
                },
                "required": ["name", "value", "status", "evidence"],
            },
        }
    },
    "required": ["facts"],
}


def validate_proposals(message: str, payload: Any) -> List[Dict[str, Any]]:
    """Turn model proposals into extractor candidates, dropping anything unverifiable."""
    if not isinstance(payload, dict) or not isinstance(payload.get("facts"), list):
        return []
    lower_msg = (message or "").lower()
    rule_status = FactExtractor.classify_statement_status(message or "")
    if _is_question(message or "") and not (_FIRST_PERSON.search(message or "") or _HINGLISH_FIRST_PERSON.search(message or "")):
        return []
    accepted: List[Dict[str, Any]] = []
    seen = set()
    for fact in payload["facts"][:6]:
        if not isinstance(fact, dict):
            continue
        name = str(fact.get("name", "")).strip().lower()
        evidence = str(fact.get("evidence", "")).strip()
        if name not in ALLOWED_FACTS or name in seen or not evidence:
            continue
        if evidence.lower() not in lower_msg:
            logger.info("LLM fact '%s' rejected: evidence not in message", name)
            continue
        try:
            value = float(fact.get("value"))
        except (TypeError, ValueError):
            continue
        category, period = ALLOWED_FACTS[name]
        if period == "interest_rate":
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", evidence)
            parsed = float(m.group(1)) if m else None
            if parsed is None or abs(parsed - value) > 0.01 or not (0 < value <= 60):
                continue
        else:
            parsed = _evidence_amount(evidence)
            if parsed is None or value <= 0 or abs(parsed - value) > max(1.0, 0.01 * value):
                logger.info("LLM fact '%s' rejected: evidence parses to %s, proposed %s", name, parsed, value)
                continue
        status = str(fact.get("status", "current")).lower()
        if status not in ("current", "hypothetical", "historical"):
            continue
        if rule_status in (FactStatus.HYPOTHETICAL.value, FactStatus.CONDITIONAL.value):
            status = rule_status
        notes = f"{evidence} (model-assisted)"
        if name == "income":
            value, income_note = _income_monthly(value, lower_msg)
            if income_note:
                notes += f" ({income_note})"
        seen.add(name)
        accepted.append({
            "name": name,
            "value": value,
            "category": category,
            "period": period,
            "status": status,
            "notes": notes,
            "source": "model_assisted",
        })
    return accepted


async def propose_facts(provider, message: str, model: Optional[str] = None) -> List[Dict[str, Any]]:
    from .extractor import FactExtractor as _FE  # noqa: F401  (keeps import order explicit)

    try:
        response = await provider.generate(
            messages=build_messages(message),
            model=model,
            options={"temperature": 0.0, "format": SCHEMA},
        )
    except Exception as exc:
        logger.warning("Model-assisted extraction failed: %s", exc)
        return []
    text = (response.content or "").strip()
    try:
        payload = json.loads(text)
    except ValueError:
        m = re.search(r"\{[\s\S]*\}", text)
        try:
            payload = json.loads(m.group(0)) if m else None
        except ValueError:
            payload = None
    return validate_proposals(message, payload)
