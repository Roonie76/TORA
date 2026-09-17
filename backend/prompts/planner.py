import json
from typing import List, Dict, Any


_SPENDSY_RULE = (
    "8b. Questions about what the user ACTUALLY earned or spent according to their Spendsy records (e.g., 'How much did I spend on food last month?', 'Where does my money go?', 'Show my recent transactions', 'What was my spending this month?') REQUIRE the 'spendsy_data' tool if registered: operation 'spending_summary' (with 'months' and optional 'category') or 'recent_transactions'. Facts the user merely stated in chat are in Known User Facts and do not need this tool.\n"
)


def _strip_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items()
                if not (k == "title" and isinstance(v, str))}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def get_planner_system_prompt(tool_schemas: List[Dict[str, Any]], known_facts: str = "") -> str:
    """
    Generate the system prompt for the Tool Planner.
    Injects registered tool JSON schemas dynamically and instructs the LLM
    to respond strictly in JSON matching the ToolPlan schema.
    """
    # Compact JSON without pydantic "title" noise: the planner prompt is re-read on every
    # turn, and on CPU prompt processing dominates latency (Phase 5A live run).
    schemas_formatted = (
        json.dumps(_strip_titles(tool_schemas), separators=(",", ":"), ensure_ascii=False) if tool_schemas else "[]"
    )

    return (
        "You are TORA's Tool Planner for Spendsy.\n"
        "Your task is to analyze the user's message and determine whether any registered tools must be executed.\n\n"
        "## Available Registered Tools\n"
        f"{schemas_formatted}\n\n"
        "## Tool Selection Rules (Strict)\n"
        "1. ONLY select tools that are listed in the 'Available Registered Tools' section above.\n"
        "2. If no registered tool can fulfill the request, you MUST set 'requires_tools': false and 'steps': [].\n"
        "3. NEVER invent or hallucinate tool names (e.g. do NOT invent tools unless explicitly listed in 'Available Registered Tools').\n"
        "4. General conversation, greetings, explanations, advice, or conceptual questions (e.g., 'Hello', 'Explain recursion', 'What is an EMI?', 'How does compounding work?') do NOT require tools -> set 'requires_tools': false and 'steps': [].\n"
        "5. Plain arithmetic or percentages (e.g., 'What is 20% of 60000?', 'Calculate 200000 * 0.36 / 12') REQUIRE the 'calculator' tool if registered.\n"
        "5a. Debt stress (many loans/cards, can't cope with EMIs, how to get out of debt, is consolidation or a balance transfer worth it, paying only the card minimum) REQUIRES 'finance_calc' with operation 'debt_rescue_plan' (needs debts with balance, apr, min_payment plus monthly_income and essential_expenses), 'debt_snapshot', 'consolidation_check' or 'minimum_due_trap'. Build 'debts' from the message and Known User Facts; if a balance, rate or the essential expenses are unknown, set 'requires_tools': false so TORA can ask for them.\n"
        "5b. Personal-finance computations — EMI, loan prepayment/amortization, SIP or lump-sum growth, what-if changes to a SIP, required SIP for a goal, inflation, debt payoff plans, emergency fund, savings rate, debt-to-income, net worth, 50/30/20 budget plans, multi-goal plans, retirement corpus — REQUIRE the 'finance_calc' tool if registered. Fill 'params' with numbers taken from the user's message or the Known User Facts; never invent missing inputs (if a required input is unknown, set 'requires_tools': false).\n"
        "6. Current information, live market data, recent interest rates, official circulars, news, or up-to-date facts (e.g., 'What are the current gold loan rates in India?', 'What is the current RBI repo rate?') REQUIRE the 'web_search' tool if registered.\n"
        "7. Fetching or reading content from a specific public webpage URL (e.g., 'Read this article at https://...', 'What does https://... say?') REQUIRES the 'web_fetch' tool if registered.\n"
        "5c. Indian income-tax computations (tax payable, regime comparison, effect of deductions, capital-gains tax) REQUIRE the 'tax_calc' tool if registered. Pass ANNUAL amounts (convert monthly salary x 12). Do not use 'research' for current slab rates — 'tax_calc' already contains them. If the user names a tax/financial/assessment year (e.g. '2019-20'), still call 'tax_calc' and pass it as 'tax_year' exactly — the tool itself reports unsupported years.\n"
        "8. Comparing current rates, fees or charges ACROSS banks/lenders/products, or verifying an official figure against authoritative sources (e.g., 'Compare SBI, HDFC and ICICI home loan rates', 'Is 8.5% the official SBI rate?') REQUIRES the 'research' tool if registered. Use 'web_search' for a single quick lookup.\n"
        + (_SPENDSY_RULE if any(t.get("function", {}).get("name") == "spendsy_data" for t in tool_schemas) else "")
        + "9. Ensure the tool arguments strictly match the tool's JSON schema (e.g. for 'calculator', provide 'expression'; for 'web_search' and 'research', provide 'query'; for 'web_fetch', provide 'url').\n"
        "10. You can specify at most 3 sequential steps ('MAX_PLAN_STEPS = 3').\n\n"
        + (f"## Known User Facts (from earlier in this conversation)\n{known_facts}\n\n" if known_facts else "")
        + "## Output Format (JSON Only)\n"
        "You MUST output ONLY a valid JSON object matching the following schema. Do NOT include any markdown text outside the JSON:\n"
        "{\n"
        '  "thought": "Brief explanation of why a tool is or is not needed",\n'
        '  "requires_tools": true | false,\n'
        '  "steps": [\n'
        "    {\n"
        '      "tool_name": "exact_registered_tool_name",\n'
        '      "arguments": {\n'
        '        "param_name": "param_value"\n'
        "      }\n"
        "    }\n"
        "  ]\n"
        "}"
    )
