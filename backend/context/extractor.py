import re
import logging
from typing import List, Dict, Any, Optional, Tuple
from .financial import FinancialProfile, FinancialFact, FactStatus

logger = logging.getLogger("tora.context.extractor")


def parse_inr_amount(text: str) -> Optional[float]:
    """
    Parse Indian numerical notation into float.
    Examples:
        '75,000' -> 75000.0
        '82k' -> 82000.0
        '1.8 lakh' -> 180000.0
        '1.65 lakh' -> 165000.0
        '6 lakh' -> 600000.0
        '2.5 crore' -> 25000000.0
    """
    if not text:
        return None

    clean = text.replace("₹", "").replace("Rs.", "").replace("Rs", "").replace("INR", "").strip().lower()

    # Match crore
    cr_match = re.search(r"\b([\d,.]+)\s*(?:crore|cr)\b", clean)
    if cr_match:
        try:
            num = float(cr_match.group(1).replace(",", ""))
            return num * 10000000.0
        except ValueError:
            pass

    # Match lakh / lac / L
    lakh_match = re.search(r"\b([\d,.]+)\s*(?:lakh|lakhs|lac|lacs|lpa|l)\b", clean)
    if lakh_match:
        try:
            num = float(lakh_match.group(1).replace(",", ""))
            return num * 100000.0
        except ValueError:
            pass

    # Match k (thousands)
    k_match = re.search(r"\b([\d,.]+)\s*k\b", clean)
    if k_match:
        try:
            num = float(k_match.group(1).replace(",", ""))
            return num * 1000.0
        except ValueError:
            pass

    # Match plain number with commas (ensuring it starts with a digit, not punctuation)
    num_match = re.search(r"\b\d[\d,]*(?:\.\d+)?", clean)
    if num_match:
        try:
            raw_str = num_match.group(0).replace(",", "")
            return float(raw_str)
        except ValueError:
            pass

    return None


_QUESTION_START = re.compile(
    r"^\s*(?:what|how|is|are|was|were|should|shall|can|could|would|will|does|do|did|which|why|when|where|who)\b",
    re.IGNORECASE,
)
_FIRST_PERSON = re.compile(r"\b(?:i|i'm|im|i've|my|me|mine)\b", re.IGNORECASE)
_ARITHMETIC = re.compile(r"\d\s*[*/+^x×÷-]\s*\d|\b(?:calculate|compute|evaluate)\b", re.IGNORECASE)
_CORRECTION_CUES = re.compile(
    r"\b(?:actually|correction|sorry|make\s+that|updated?|latest|now|it\s+is|it's|its|no)\b",
    re.IGNORECASE,
)


_OTHER_FINANCE_WORDS = re.compile(
    r"\b(?:mutual|mf|gold|sips?|car|house|home|savings?|emergency|food|groceries|commute|travel|fuel|fds?|"
    r"deposit|insurance|stocks?|shares?|ppf|epf|nps|tax)\b",
    re.IGNORECASE,
)
_ANNUAL_CUE = re.compile(r"\b(?:per\s*annum|p\.?\s?a\b|a\s*year|per\s*year|yearly|annual(?:ly)?|lpa|ctc|every\s*year)", re.IGNORECASE)
_MONTHLY_CUE = re.compile(r"\b(?:per\s*month|a\s*month|monthly|every\s*month|pm\b|/\s*month|in\s*hand)", re.IGNORECASE)
ANNUAL_SALARY_THRESHOLD = 500000.0


def _income_monthly(amount: float, clause: str):
    """
    Normalise a stated income to a monthly figure. Explicit cues win; otherwise an
    amount of ₹5 lakh or more is read the Indian way (annual package, e.g. "18 lakh salary").
    Returns (monthly_value, note_or_None).
    """
    annual = bool(_ANNUAL_CUE.search(clause))
    monthly = bool(_MONTHLY_CUE.search(clause))
    if annual and not monthly or (not monthly and not annual and amount >= ANNUAL_SALARY_THRESHOLD):
        from ..finance.engine import inr  # local import keeps context independent at import time
        return round(amount / 12, 2), f"stated as {inr(amount)} per year"
    return amount, None


def _is_question(text: str) -> bool:
    """True when the text reads as a question rather than a statement."""
    stripped = text.strip()
    return stripped.endswith("?") or bool(_QUESTION_START.match(stripped))


def _allows_loose_amount(text: str) -> bool:
    """
    Decide whether a bare amount (no keyword directly attached) may be attributed to an
    entity inferred from the clause or from conversation history. Prevents questions,
    calculations and advice requests from overwriting stored facts
    (e.g. "Calculate 200000 * 0.36 / 12" must never become the user's salary).
    """
    if _is_question(text) or _ARITHMETIC.search(text):
        return False
    return bool(_FIRST_PERSON.search(text) or _CORRECTION_CUES.search(text))


_STRONG_CORRECTION = re.compile(
    r"\b(?:i\s+meant|typo|i\s+was\s+wrong|that\s+was\s+wrong|that'?s\s+wrong|my\s+mistake|"
    r"i\s+made\s+a\s+mistake|correction\s*:|wrongly|"
    r"not\s+(?:₹|rs\.?|inr)?\s*\d[\d,.]*\s*(?:k|l|lakhs?|lacs?|cr|crores?)?\b)",
    re.IGNORECASE,
)
# Wording that describes a real change over time (not a mistake), e.g.
# "Small correction: my salary just increased to 82k".
_CHANGE_OVER_TIME = re.compile(
    r"\b(?:increased|increase|raised|hike|hiked|went\s+up|went\s+down|reduced|decreased|dropped|"
    r"changed|revised|now|new|latest|this\s+month|promotion|raise)\b",
    re.IGNORECASE,
)
_DELETE_ALL = re.compile(
    r"\b(?:forget|delete|erase|clear|wipe|remove)\b\s+(?:all\s+(?:of\s+)?|everything|my\s+(?:memory|profile|data|details|info(?:rmation)?)\b|"
    r"what\s+you\s+know|all\s+my\s+(?:data|details|info(?:rmation)?|facts))",
    re.IGNORECASE,
)
_DELETE_ONE = re.compile(r"\b(?:forget|delete|erase|remove|clear|drop)\b\s+(?:about\s+)?(.+)", re.IGNORECASE)
_CLOSED_CUES = re.compile(
    r"\b(?:paid\s+off|fully\s+paid|closed|cleared\s+(?:off|it|the)|no\s+longer\s+(?:have|pay)|"
    r"(?:don'?t|do\s+not)\s+(?:have|pay)\b.*\banymore)\b",
    re.IGNORECASE,
)

# keyword -> fact names affected by delete / closure commands
ENTITY_FACT_NAMES = (
    (("salary", "income", "earning", "earnings"), ("income",)),
    (("rent",), ("rent",)),
    (("savings", "saving", "emergency fund"), ("savings",)),
    (("credit card", "card", "cc"), ("credit_card_debt", "credit_card_apr")),
    (("emi", "loan"), ("personal_loan_emi",)),
    (("mutual fund", "mutual funds", "mf"), ("mutual_funds",)),
    (("gold",), ("gold",)),
    (("car",), ("car_goal",)),
    (("food", "groceries"), ("food",)),
    (("commute", "travel", "transport", "fuel"), ("commute",)),
)


def _fact_names_in(text: str) -> List[str]:
    lower = text.lower()
    names: List[str] = []
    for keywords, fact_names in ENTITY_FACT_NAMES:
        if any(re.search(r"\b" + re.escape(k) + r"\b", lower) for k in keywords):
            for n in fact_names:
                if n not in names:
                    names.append(n)
    return names


def extract_memory_commands(message: str) -> List[Dict[str, Any]]:
    """
    Detect explicit memory-management commands:
    - "forget everything" / "clear my profile"            -> {"action": "clear"}
    - "forget my rent" / "delete my credit card details"  -> {"action": "delete", "name": ...}
    - "I paid off my credit card" / "I no longer pay rent" -> current value 0 (closure)
    Questions ("How do I delete my card?") are never commands.
    """
    if not message or not message.strip() or _is_question(message):
        return []
    text = message.strip()
    if _DELETE_ALL.search(text):
        return [{"action": "clear"}]
    m = _DELETE_ONE.search(text)
    if m and re.search(r"\b(?:my|the|about)\b", m.group(0), re.IGNORECASE):
        names = _fact_names_in(m.group(1))
        if names:
            return [{"action": "delete", "name": n} for n in names]
    if _CLOSED_CUES.search(text) and _FIRST_PERSON.search(text):
        names = _fact_names_in(text)
        closable = [n for n in names if n in ("credit_card_debt", "personal_loan_emi", "rent")]
        return [
            {
                "name": n,
                "value": 0.0,
                "category": "rent" if n == "rent" else ("loan" if n == "personal_loan_emi" else "debt"),
                "period": "monthly" if n in ("rent", "personal_loan_emi") else "lump_sum",
                "status": FactStatus.CURRENT.value,
                "notes": text[:200],
                "closure": True,
            }
            for n in closable
        ]
    return []


class FactExtractor:
    """
    Deterministic rule-based extractor for user financial facts.
    Classifies statements into semantic statuses: CURRENT, HISTORICAL, HYPOTHETICAL, CONDITIONAL, ESTIMATE.
    """

    HYPOTHETICAL_TRIGGERS = (
        "if i ", "if my ", "what if ", "suppose i ", "suppose my ", "assume i ",
        "assume my ", "assuming ", "would your answer change if", "hypothetically",
        "if we assume", "imagine i ", "imagine my ", "let's say ", "lets say ",
        "for a scenario where", "in a scenario where", "say my salary", "say my income",
        "if i got a ", "if i get a ", "if my income were", "if my salary were",
        "if my salary was", "if my income was",
    )

    CONDITIONAL_FUTURE_TRIGGERS = (
        "next year i expect", "i expect my", "will probably increase",
        "will probably be", "might increase to", "planning to earn",
        "aiming for a salary of", "expected to be",
    )

    HISTORICAL_TRIGGERS = (
        "i used to", "used to earn", "used to pay", "previously i",
        "previously my", "originally my", "originally i", "earlier i",
        "earlier my", "before my raise", "old salary", "old rent",
    )

    ESTIMATE_TRIGGERS = (
        "i think i spend", "i guess", "roughly", "approx", "approximately",
        "around ₹", "around rs", "around ",
    )

    @classmethod
    def classify_statement_status(cls, text: str) -> str:
        """Classify the semantic status of a message or clause."""
        lower = text.lower().strip()
        if any(trig in lower for trig in cls.HYPOTHETICAL_TRIGGERS):
            return FactStatus.HYPOTHETICAL.value
        if any(trig in lower for trig in cls.CONDITIONAL_FUTURE_TRIGGERS):
            return FactStatus.CONDITIONAL.value
        if any(trig in lower for trig in cls.HISTORICAL_TRIGGERS):
            return FactStatus.HISTORICAL.value
        return FactStatus.CURRENT.value

    @classmethod
    def is_hypothetical(cls, text: str) -> bool:
        """Check if statement is hypothetical."""
        return cls.classify_statement_status(text) in (
            FactStatus.HYPOTHETICAL.value,
            FactStatus.CONDITIONAL.value,
        )

    @classmethod
    def extract_candidate_facts(cls, message: str, profile: Optional[FinancialProfile] = None) -> List[Dict[str, Any]]:
        """
        Scan a user message and extract candidate financial facts with semantic status.
        Handles same-turn corrections (e.g. '₹75k. Actually ₹82k') and explicit past vs present.
        """
        candidates: List[Dict[str, Any]] = []
        if not message or not message.strip():
            return candidates

        commands = extract_memory_commands(message)
        if commands:
            return commands

        raw_lower = message.lower()

        # Check for system injection markers
        if "system instruction:" in raw_lower or "ignore previous instructions" in raw_lower:
            clean_text = re.sub(r"(?:system instruction:|ignore previous instructions)[\s\S]*", "", message, flags=re.IGNORECASE)
        else:
            clean_text = message

        lower = clean_text.lower()
        overall_status = cls.classify_statement_status(clean_text)

        # Questions without any first-person assertion never create CURRENT facts
        # (e.g. "Is 50k a good salary?" or "What is 20% of 60000?").
        if (
            overall_status == FactStatus.CURRENT.value
            and _is_question(clean_text)
            and not _FIRST_PERSON.search(clean_text)
        ):
            return candidates

        # Context-aware default entity from profile history if message is an isolated correction
        context_entity = None
        if profile and profile.history:
            last_name = profile.history[-1].get("name", "")
            if "income" in last_name or "salary" in last_name:
                context_entity = "income"
            elif "rent" in last_name:
                context_entity = "rent"
            elif "card" in last_name or "debt" in last_name or "balance" in last_name:
                context_entity = "debt"
            elif "loan" in last_name or "emi" in last_name:
                context_entity = "loan"

        # Helper to extract facts for a given sub-clause
        def parse_clause(clause_text: str, clause_status: str, inherited_entity: Optional[str] = None) -> List[str]:
            c_lower = clause_text.lower()
            detected_entities = []

            # 1. Detect entity clues in clause
            if any(w in c_lower for w in ("salary", "income", "take home", "earn", "earning", "ctc", "package")):
                detected_entities.append("income")
            if any(w in c_lower for w in ("rent", "housing")):
                detected_entities.append("rent")
            if any(w in c_lower for w in ("card", "debt", "balance", "statement", "credit", "cc")):
                detected_entities.append("debt")
            if any(w in c_lower for w in ("emi", "personal loan", "loan")):
                detected_entities.append("loan")

            fallback_entity = (
                context_entity
                if _CORRECTION_CUES.search(clause_text) and not _OTHER_FINANCE_WORDS.search(clause_text)
                else None
            )
            effective_entity = inherited_entity or (detected_entities[0] if detected_entities else fallback_entity)

            # 2. Income / Salary
            inc_match = re.search(
                r"(?:(?<!other )(?<!rental )(?<!interest )(?<!dividend )(?<!side )\b(?:salary|income|take\s*home|earn|earning|earned|made|make|ctc|package)\b[^\d\n]{0,35}?(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?(?:\s*per\s*month|\s*/\s*month|\s*pm)?)|(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)\s*(?:salary|income|take\s*home))",
                c_lower,
            )
            if (
                not inc_match
                and effective_entity == "income"
                and _allows_loose_amount(clause_text)
                and not re.search(r"\b(?:other|rental|interest|dividend|side)\s+income|\bgains?\b", c_lower)
            ):
                amt = parse_inr_amount(c_lower)
                if amt and amt >= 5000:
                    monthly_amt, income_note = _income_monthly(amt, c_lower)
                    candidates.append({
                        "name": "income",
                        "value": monthly_amt,
                        "category": "income",
                        "period": "monthly",
                        "status": clause_status,
                        "notes": clause_text.strip() + (f" ({income_note})" if income_note else ""),
                    })
                    if "income" not in detected_entities:
                        detected_entities.append("income")
            elif inc_match:
                val_str = inc_match.group(1) or inc_match.group(2)
                amt = parse_inr_amount(val_str)
                if amt and amt >= 5000:
                    monthly_amt, income_note = _income_monthly(amt, c_lower)
                    candidates.append({
                        "name": "income",
                        "value": monthly_amt,
                        "category": "income",
                        "period": "monthly",
                        "status": clause_status,
                        "notes": clause_text.strip() + (f" ({income_note})" if income_note else ""),
                    })
                    if "income" not in detected_entities:
                        detected_entities.append("income")

            # 3. Rent
            rent_match = re.search(
                r"(?:\b(?:rent)\b[^\d\n]{0,35}?(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?(?:\s*per\s*month|\s*/\s*month|\s*pm)?)|(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)\s*(?:rent))",
                c_lower,
            )
            if not rent_match and effective_entity == "rent" and _allows_loose_amount(clause_text):
                amt = parse_inr_amount(c_lower)
                if amt and 1000 <= amt <= 500000:
                    candidates.append({
                        "name": "rent",
                        "value": amt,
                        "category": "rent",
                        "period": "monthly",
                        "status": clause_status,
                        "notes": clause_text.strip(),
                    })
                    if "rent" not in detected_entities:
                        detected_entities.append("rent")
            elif rent_match:
                val_str = rent_match.group(1) or rent_match.group(2)
                amt = parse_inr_amount(val_str)
                if amt and 1000 <= amt <= 500000:
                    candidates.append({
                        "name": "rent",
                        "value": amt,
                        "category": "rent",
                        "period": "monthly",
                        "status": clause_status,
                        "notes": clause_text.strip(),
                    })
                    if "rent" not in detected_entities:
                        detected_entities.append("rent")

            # 4. Food / Groceries
            food_match = re.search(
                r"\b(?:food|groceries)\b[^\d\n]{0,30}?(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)",
                c_lower,
            )
            if food_match:
                amt = parse_inr_amount(food_match.group(1))
                if amt and 500 <= amt <= 100000:
                    candidates.append({
                        "name": "food",
                        "value": amt,
                        "category": "expense",
                        "period": "monthly",
                        "status": clause_status,
                    })
                    if "food" not in detected_entities:
                        detected_entities.append("food")

            # 5. Commute / Travel
            commute_match = re.search(
                r"\b(?:commute|transport|travel|fuel)\b[^\d\n]{0,30}?(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)",
                c_lower,
            )
            if commute_match:
                amt = parse_inr_amount(commute_match.group(1))
                if amt and 200 <= amt <= 100000:
                    candidates.append({
                        "name": "commute",
                        "value": amt,
                        "category": "expense",
                        "period": "monthly",
                        "status": clause_status,
                    })
                    if "commute" not in detected_entities:
                        detected_entities.append("commute")

            # 6. Personal Loan / EMI
            emi_matches = list(re.finditer(
                r"(?:\b(?:personal\s*loan|loan\s*emi|monthly\s*emi|emi)\b[^\d\n]{0,35}?(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)|(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)\s*(?:monthly\s*emi|personal\s*loan|emi))",
                c_lower,
            ))
            seen_loan_names = set()
            for emi_match in emi_matches:
                val_str = emi_match.group(1) or emi_match.group(2)
                amt = parse_inr_amount(val_str)
                is_emi = bool(re.search(r"\bemis?\b|per\s*month|a\s*month|monthly", emi_match.group(0)))
                if is_emi and amt and 1000 <= amt <= 1000000:
                    name, period = "personal_loan_emi", "monthly"
                elif not is_emi and amt and amt >= 10000:
                    # "a personal loan of 3 lakh" is an outstanding balance, not a monthly EMI
                    name, period = "personal_loan_balance", "lump_sum"
                else:
                    continue
                if name in seen_loan_names:
                    continue
                seen_loan_names.add(name)
                candidates.append({
                    "name": name,
                    "value": amt,
                    "category": "loan",
                    "period": period,
                    "status": clause_status,
                })
                if "loan" not in detected_entities:
                    detected_entities.append("loan")

            # 7. Credit Card Debt / Balance
            cc_match = re.search(
                r"\b(?:credit\s*card|cc\s*debt|cc\s*balance|card\s*balance|outstanding\s*balance|card\s*statement|statement|balance)\b[^\d\n]{0,50}?(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)",
                c_lower,
            )
            if not cc_match and effective_entity == "debt" and _allows_loose_amount(clause_text):
                amt = parse_inr_amount(c_lower)
                if amt and amt >= 1000:
                    candidates.append({
                        "name": "credit_card_debt",
                        "value": amt,
                        "category": "debt",
                        "period": "lump_sum",
                        "status": clause_status,
                    })
                    if "debt" not in detected_entities:
                        detected_entities.append("debt")
            elif cc_match:
                amt = parse_inr_amount(cc_match.group(1))
                if amt and amt >= 1000:
                    candidates.append({
                        "name": "credit_card_debt",
                        "value": amt,
                        "category": "debt",
                        "period": "lump_sum",
                        "status": clause_status,
                    })
                    if "debt" not in detected_entities:
                        detected_entities.append("debt")

            # 8. Credit Card APR / Interest rate
            apr_match = re.search(
                r"(\d+(?:\.\d+)?)\s*%\s*(?:apr|annual|interest|p\.a\.|per\s*annum)?",
                c_lower,
            )
            if apr_match and ("apr" in c_lower or "debt" in detected_entities):
                try:
                    rate = float(apr_match.group(1))
                    if 5 <= rate <= 60:
                        candidates.append({
                            "name": "credit_card_apr",
                            "value": rate,
                            "category": "debt",
                            "period": "interest_rate",
                            "status": clause_status,
                        })
                        if "debt" not in detected_entities:
                            detected_entities.append("debt")
                except ValueError:
                    pass

            # 9. Savings / Emergency Fund
            savings_match = re.search(
                r"(?:\b(?:savings|emergency\s*fund|bank\s*balance)\b[^\d\n]{0,35}?(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)|(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)\s*(?:in\s*savings|in\s*bank\s*savings|savings|emergency\s*fund))",
                c_lower,
            )
            if savings_match:
                val_str = savings_match.group(1) or savings_match.group(2)
                amt = parse_inr_amount(val_str)
                if amt and amt >= 1000:
                    candidates.append({
                        "name": "savings",
                        "value": amt,
                        "category": "savings",
                        "period": "lump_sum",
                        "status": clause_status,
                    })
                    if "savings" not in detected_entities:
                        detected_entities.append("savings")

            # 9b. Monthly SIP amount (not the size of a change like "increase my SIP by 5000")
            sip_match = re.search(
                r"(?:\b(?:sips?|systematic\s*investment\s*plans?)\b[^\d\n]{0,30}?(₹?\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:lakhs|lakh|lacs|lac|l|k)\b)?)"
                r"|(₹?\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:lakhs|lakh|lacs|lac|l|k)\b)?)[^\d\n]{0,25}?\b(?:sips?|systematic\s*investment\s*plans?)\b)",
                c_lower,
            )
            if sip_match and not re.search(r"\b(?:increase|decrease|raise|reduce|by|step[- ]?up|top[- ]?up)\b", c_lower):
                amt = parse_inr_amount(sip_match.group(1) or sip_match.group(2))
                if amt and 100 <= amt <= 1000000:
                    candidates.append({
                        "name": "sip_monthly",
                        "value": amt,
                        "category": "investment",
                        "period": "monthly",
                        "status": clause_status,
                    })
                    if "investment" not in detected_entities:
                        detected_entities.append("investment")

            # 10. Mutual Funds
            mf_match = re.search(
                r"(?:\b(?:mutual\s*funds|mf)\b[^\d\n]{0,35}?(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)|(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)\s*(?:in\s*mutual\s*funds|in\s*mf|mutual\s*funds))",
                c_lower,
            )
            if mf_match:
                val_str = mf_match.group(1) or mf_match.group(2)
                amt = parse_inr_amount(val_str)
                if amt and amt >= 1000:
                    candidates.append({
                        "name": "mutual_funds",
                        "value": amt,
                        "category": "investment",
                        "period": "lump_sum",
                        "status": clause_status,
                    })
                    if "investment" not in detected_entities:
                        detected_entities.append("investment")

            # 11. Gold
            gold_match = re.search(
                r"(?:\b(?:gold|gold\s*worth)\b[^\d\n]{0,35}?(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)|(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)\s*(?:in\s*gold|worth\s*of\s*gold|gold))",
                c_lower,
            )
            if gold_match:
                val_str = gold_match.group(1) or gold_match.group(2)
                amt = parse_inr_amount(val_str)
                if amt and amt >= 1000:
                    candidates.append({
                        "name": "gold",
                        "value": amt,
                        "category": "investment",
                        "period": "lump_sum",
                        "status": clause_status,
                    })
                    if "investment" not in detected_entities:
                        detected_entities.append("investment")

            # 12. Car Goal / Major Goal
            goal_match = re.search(
                r"\b(?:car|vehicle|buy\s*a\s*car|car\s*goal)\b[^\d\n]{0,45}?(₹?\s*\d[\d,]*(?:\.\d+)?\s*(?:\s*(?:lakhs|lakh|lacs|lac|lpa|crores|crore|cr|l|k)\b)?)",
                c_lower,
            )
            if goal_match:
                amt = parse_inr_amount(goal_match.group(1))
                if amt and amt >= 50000:
                    candidates.append({
                        "name": "car_goal",
                        "value": amt,
                        "category": "goal",
                        "period": "lump_sum",
                        "status": clause_status,
                    })
                    if "goal" not in detected_entities:
                        detected_entities.append("goal")

            return detected_entities

        # Check for same-turn multi-clause statements (e.g. "I used to earn 75k, but now earn 82k")
        if "used to" in lower and ("now" in lower or "actually" in lower or "but" in lower):
            parts = [p.strip() for p in re.split(r"\b(?:but now|now|actually)\b", clean_text, flags=re.IGNORECASE) if p.strip()]
            if len(parts) >= 2:
                e1_list = parse_clause(parts[0], FactStatus.HISTORICAL.value)
                inherited = e1_list[0] if e1_list else None
                parse_clause(parts[1], FactStatus.CURRENT.value, inherited_entity=inherited)
                return candidates

        # Check for same-turn corrections (e.g. "My salary is ₹75,000. Actually, it is ₹82,000.")
        if ". actually" in lower or ", actually" in lower:
            parts = [p.strip() for p in re.split(r"(?:[.,;]\s*actually\b)", clean_text, flags=re.IGNORECASE) if p.strip()]
            if len(parts) >= 2:
                e1_list = parse_clause(parts[0], FactStatus.HISTORICAL.value)
                inherited = e1_list[0] if e1_list else None
                parse_clause(parts[1], FactStatus.CURRENT.value, inherited_entity=inherited)
                return candidates

        # Default parse whole message with overall status
        parse_clause(clean_text, overall_status)
        if _STRONG_CORRECTION.search(clean_text) and not _CHANGE_OVER_TIME.search(clean_text):
            for cand in candidates:
                if cand.get("status") == FactStatus.CURRENT.value:
                    cand["correction"] = True
        return candidates


class FactManager:
    """
    Application-level manager to validate candidate facts and safely apply them to FinancialProfile.
    """

    @classmethod
    def apply_candidates(
        cls,
        profile: FinancialProfile,
        candidates: List[Dict[str, Any]],
        turn: Optional[int] = None,
    ) -> List[FinancialFact]:
        """Apply extracted candidate facts (and memory commands) to profile."""
        applied = []
        for cand in candidates:
            action = cand.get("action")
            if action == "clear":
                profile.clear(turn=turn)
                continue
            if action == "delete":
                profile.delete_fact(cand["name"], turn=turn)
                continue
            if cand.get("closure"):
                existing = profile.get_fact(cand["name"])
                if existing is None or existing.status != FactStatus.CURRENT.value:
                    continue  # nothing to close
            fact = profile.set_fact(
                name=cand["name"],
                value=cand["value"],
                category=cand.get("category", "general"),
                period=cand.get("period"),
                status=cand.get("status", FactStatus.CURRENT.value),
                source=cand.get("source", "user"),
                notes=cand.get("notes"),
                turn=turn,
                is_correction=bool(cand.get("correction")),
            )
            applied.append(fact)
        return applied
