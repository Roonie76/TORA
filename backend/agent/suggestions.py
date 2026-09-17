"""Follow-up suggestions shown under a reply (deterministic, based on what just happened)."""

from typing import Any, Dict, List, Optional

_BY_OPERATION = {
    "emi": ["What if I prepay ₹5,000 every month?", "Should I prepay or invest the extra money?",
            "Compare a 15-year and a 20-year tenure"],
    "amortization": ["Should I prepay or invest instead?", "How much faster with ₹10,000 extra a month?"],
    "sip_future_value": ["What if I increase it by 10% every year?", "How much SIP do I need for ₹1 crore?",
                         "What would that be worth after inflation?"],
    "sip_change_impact": ["How much SIP do I need for ₹1 crore?", "Check my financial health"],
    "required_sip": ["What if returns are only 10%?", "Plan my retirement"],
    "inflation_adjust": ["How much SIP do I need to beat that?", "Plan my retirement"],
    "debt_payoff": ["What if I pay ₹5,000 more a month?", "Is a consolidation loan worth it?"],
    "debt_rescue_plan": ["What if I pay ₹5,000 more a month?", "Is a consolidation loan worth it?",
                         "What are my rights if recovery agents harass me?"],
    "debt_snapshot": ["Make me a plan to get out of debt", "What if I only pay the minimum due?"],
    "consolidation_check": ["Make me a full debt rescue plan", "What if I only pay the minimum due?"],
    "minimum_due_trap": ["Make me a plan to get out of debt", "Is a balance transfer worth it?"],
    "prepay_vs_invest": ["What if my returns are only 9%?", "Should I pick a shorter tenure instead?"],
    "rent_vs_buy": ["What if prices grow 8% a year?", "How much should I save for the down payment?"],
    "loan_tenure_choice": ["Should I prepay or invest?", "What EMI can I afford?"],
    "financial_health_check": ["Help me build the emergency fund", "Make me a plan to clear the debt",
                               "How much life cover do I need?"],
    "emergency_fund": ["How long will it take to build it?", "Check my financial health"],
    "budget_plan": ["How can I cut my needs to 50%?", "Check my financial health"],
    "goal_plan": ["What if I can invest ₹5,000 more?", "Plan my retirement"],
    "retirement_plan": ["What if I retire at 55?", "What if returns are only 10%?"],
    "compute_tax": ["Compare the old and new regime", "How can I save more tax?", "When is my advance tax due?"],
    "compare_regimes": ["How can I save more tax?", "Which ITR form should I file?"],
    "tax_saving_finder": ["Which ITR form should I file?", "When is advance tax due?"],
    "hra_exemption": ["Which regime is better for me with this HRA?", "How can I save more tax?"],
    "capital_gains_tax": ["How can I save this tax with section 54 or 54EC?", "Which ITR form should I file?"],
    "advance_tax_plan": ["What interest do I pay if I'm late?", "How can I save more tax?"],
    "itr_form_choice": ["What is the last date to file?", "How can I save more tax?"],
    "spending_summary": ["Where can I cut spending?", "Help me plan my budget"],
}
_BY_INTENT = {
    "memory_update": ["Check my financial health", "Help me plan my budget"],
    "memory_recall": ["Check my financial health", "How much tax will I pay?"],
    "research": ["Compare it with other banks", "Which option is cheapest for me?"],
    "comparison": ["Which one suits me best?", "What will my EMI be?"],
    "general_qa": ["Check my financial health", "How much tax will I pay on my salary?", "Help me get out of debt"],
}


def suggest(intent: Optional[str], tool_results: List[Dict[str, Any]], limit: int = 3) -> List[str]:
    out: List[str] = []
    for r in tool_results:
        op = r.get("operation") or r.get("tool")
        for s in _BY_OPERATION.get(op, []):
            if s not in out:
                out.append(s)
        if r.get("tool") == "rules_lookup":
            out += [s for s in ("How does this affect my tax?", "What else can I claim?") if s not in out]
    if not out:
        out = list(_BY_INTENT.get(intent or "", _BY_INTENT["general_qa"]))
    return out[:limit]
