"""
Tier 0: the turns that need no model at all.

Prefill runs at 39 tokens/sec and decode at 4.1 on this box, and neither is moving
without different hardware. So the way to make TORA fast is not a faster model — it
is not calling one.

All 31 engine operations already write their own summary sentence, and they are
better written than the model's:

    EMI ₹44,986/month for 240 months; total interest ₹57,96,711, total paid ₹1,07,96,711.
    To reach ₹5,00,000 in 3 years at 12% p.a., invest about ₹11,492/month.

For an unambiguous calculation that is the whole answer. Put the figure block above it
and the engine's own warnings below, and the turn is finished before a model would have
finished reading the prompt.

The gate is deliberately narrow: one trusted engine, no errors, a summary present, and a
question that asks what a number *is* rather than what to *do* about it. Anything asking
for judgement — "should I", "is it worth it", "which is better" — is exactly what the
model is for and goes down the normal path.
"""
from __future__ import annotations

import re
from typing import Any, List, Optional

# Engines whose output is deterministic and whose wording is the engine author's, not a model's.
#
# tax_calc was held back until this answer could carry a tax result's legal basis — the eval
# "Tax results carry their legal basis" requires every tax answer to cite the section it rests on
# (scenario rules-tax-basis, "Section 156"), and a tax answer without it is a worse answer
# delivered faster. tax_calc already returns those citations from the reviewed rules library, so
# they are rendered below and it now qualifies.
DIRECT_TOOLS = ("finance_calc", "tax_calc")
# "planning" is here because the classifier reads "invest monthly to reach 5 lakh in 3 years" as
# planning when it is a single sum. The operation list below, not the intent, is what keeps a
# genuine plan away from this path.
DIRECT_INTENTS = ("calculation", "financial_qa", "what_if", "planning")
# Operations that exist to weigh one option against another. The figures are only half the
# answer; the recommendation is the other half, and that is the model's job.
COMPARISON_OPERATIONS = ("compare_regimes", "prepay_vs_invest", "rent_vs_buy", "loan_tenure_choice",
                         "consolidation_check", "tax_saving_finder", "financial_health_check",
                         "itr_form_choice", "budget_plan", "debt_rescue_plan", "retirement_plan",
                         "goal_plan", "debt_payoff", "minimum_due_trap", "debt_snapshot")

# A question about what to do needs judgement, and judgement is what the model is for.
_WANTS_JUDGEMENT = re.compile(
    r"\b(?:should\s+i|should\s+we|worth\s+it|worth\s+doing|is\s+it\s+wise|am\s+i\s+on\s+the\s+right|"
    r"(?:is|are|which|what|whose)\s+\w*\s*better|better\s+(?:to|option|choice|for\s+me)|best\s+(?:option|way|for\s+me)|"
    r"which\s+(?:is|one|should|regime|option)|\w+\s+or\s+(?:invest|prepay|buy|rent|save)\b|"
    r"recommend\w*|advice|advise|suggest\w*|what\s+do\s+you\s+think|where\s+do\s+i\s+stand|"
    r"help\s+me\s+decide|do\s+you\s+think|opinion|pros\s+and\s+cons|explain\s+why|why\s+(?:is|does|should))\b",
    re.IGNORECASE,
)
# An open question wants prose, not a figure.
_WANTS_PROSE = re.compile(r"^\s*(?:why|how\s+does|how\s+do|what\s+is\s+the\s+difference|explain|tell\s+me\s+about)\b",
                          re.IGNORECASE)
MAX_WARNINGS = 3
MAX_BASIS = 6


def _intent_name(intent: Any) -> str:
    inner = getattr(intent, "intent", intent)
    return str(getattr(inner, "value", inner) or "")


def _sole_result(tool_context: Any) -> Optional[Any]:
    """The one trusted engine result this turn, or None if it is not that simple."""
    results = [r for r in getattr(tool_context, "results", []) or []]
    if len(results) != 1:
        return None
    result = results[0]
    if getattr(result, "is_error", False) or not isinstance(getattr(result, "output", None), dict):
        return None
    if result.tool_name not in DIRECT_TOOLS:
        return None
    return result


def direct_answer(message: str, intent: Any, tool_context: Any, block: str,
                  has_documents: bool = False) -> Optional[str]:
    """The finished answer when no model is needed, otherwise None."""
    if has_documents or tool_context is None or getattr(tool_context, "is_empty", lambda: True)():
        return None
    if _intent_name(intent) not in DIRECT_INTENTS:
        return None
    text = message or ""
    if _WANTS_JUDGEMENT.search(text) or _WANTS_PROSE.match(text.strip()):
        return None
    if text.count("?") > 1:
        return None                      # two questions; the second one is rarely a figure
    result = _sole_result(tool_context)
    if result is None:
        return None
    if str(result.output.get("operation") or "") in COMPARISON_OPERATIONS:
        return None
    summary = str(result.output.get("summary") or "").strip()
    if not summary:
        return None
    basis = [str(b).strip() for b in (result.output.get("legal_basis") or []) if str(b).strip()]
    if result.tool_name == "tax_calc" and not basis:
        # The rule this rests on is not optional in a tax answer. If the engine did not give one,
        # the model writes the answer and the prompt's own citation rules apply.
        return None

    parts: List[str] = []
    if block:
        parts.append(block.rstrip())
    parts.append(summary)
    warnings = [str(w).strip() for w in (result.output.get("warnings") or []) if str(w).strip()]
    if warnings:
        parts.append("\n".join(f"- {w}" for w in warnings[:MAX_WARNINGS]))
    # A tax figure without the rule it rests on is half an answer. These come from the reviewed
    # rules library, carry the tax year, and are the reason tax could not take this path before.
    if basis:
        parts.append("**Legal basis**\n" + "\n".join(f"- {b}" for b in basis[:MAX_BASIS]))
    parts.append("Ask if you want this broken down, or run with different numbers.")
    return "\n\n".join(parts).strip()
