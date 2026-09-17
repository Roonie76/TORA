"""Phase 10: rules library with citations."""
import asyncio
import json
from datetime import date

import pytest

from backend.context import ContextBuilder, ToolContext
from backend.knowledge import RulesLibrary, get_library
from backend.knowledge.__main__ import main as knowledge_cli
from backend.planner.fast_path import fast_plan
from backend.tools import RulesLookupTool, TaxCalcTool


def ids(query, tax_year=None):
    return [r["id"] for r in get_library().search(query, tax_year)]


def test_library_is_valid_and_matches_the_tax_engine():
    lib = get_library()
    assert len(lib.rules) >= 30
    assert lib.check(today=date(2026, 9, 17)) == []
    assert knowledge_cli(["check"]) == 0


def test_staleness_and_drift_are_reported(tmp_path):
    from backend.knowledge.library import RULES_PATH
    data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    for r in data["rules"]:
        if r["id"] == "80c":
            r["figures"]["limit"] = 200000
        if r["id"] == "hra":
            r["verified_on"] = "2024-01-01"
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(data))
    problems = RulesLibrary.load(path).check(today=date(2026, 9, 17))
    assert any("80c.limit" in p for p in problems)
    assert any(p.startswith("hra: last verified") for p in problems)


@pytest.mark.parametrize("query, expected", [
    ("What is the 80C limit?", "80c"),
    ("HRA exemption rules", "hra"),
    ("recovery agent calling me at night", "rbi-recovery"),
    ("section 156", "rebate"),
    ("home loan interest deduction", "home-loan-interest"),
    ("is FD interest taxed TDS", "tds-interest"),
    ("LTCG on mutual funds", "ltcg-equity"),
    ("freelancer tax", "presumptive"),
    ("how to complain against my bank", "rbi-ombudsman"),
    ("prepayment penalty on home loan", "rbi-prepayment"),
    ("last date to file ITR", "itr-due-dates"),
])
def test_search_finds_the_right_rule(query, expected):
    assert ids(query)[0] == expected


def test_tax_year_filter_and_citations():
    lib = get_library()
    r26 = lib.search("80C limit", "2026-27")[0]
    r25 = lib.search("80C limit", "2025-26")[0]
    assert "Section 123" in r26["citation"] and "earlier s.80C" in r26["citation"]
    assert r25["citation"].startswith("Section 80C")
    assert "itr-due-dates" not in ids("last date to file ITR", "2026-27")
    assert lib.search("zzzz qqqq") == []
    unverified = lib.get("standard-deduction").citation_for("2026-27")
    assert "confirm the new section number" in unverified


def test_tool_and_rendering():
    res = asyncio.run(RulesLookupTool().run({"query": "80C limit", "tax_year": "2026-27"}))
    assert res.success and res.data["found"] and res.data["rules"][0]["figures"]["limit"] == 150000
    ctx = ToolContext().add_result(tool_name="rules_lookup", call_id="r", output=res.data)
    system = ContextBuilder(default_system_prompt="S").build(current_message="q", tool_context=ctx)[0]["content"]
    assert "Section 123" in system and "Verified: 2026-09-17" in system
    bad = asyncio.run(RulesLookupTool().run({"query": "80C", "tax_year": "FY26"}))
    assert not bad.success


def test_tax_results_carry_legal_basis():
    res = asyncio.run(TaxCalcTool().run({"operation": "compute_tax", "params": {
        "gross_salary": 1000000, "regime": "old", "deductions": {"section_80c": 150000}, "tax_year": "2026-27"}}))
    basis = " | ".join(res.data["legal_basis"])
    assert "Section 156" in basis and "Section 123" in basis and "Old regime" in basis


@pytest.mark.parametrize("message, tool", [
    ("What is the 80C limit?", "rules_lookup"),
    ("Last date to file ITR?", "rules_lookup"),
    ("Can a recovery agent call me at 10 pm?", "rules_lookup"),
    ("What is the HRA exemption rule for 2025-26?", "rules_lookup"),
    ("What is the limit on my credit card?", None),
    ("How much tax on 12 lakh salary?", "tax_calc"),
])
def test_rule_questions_are_fast_routed(message, tool):
    plan = fast_plan(message)
    assert (plan.steps[0].tool_name if plan else None) == tool


def test_prompts_require_citations():
    from backend.prompts.planner import get_planner_system_prompt
    from backend.prompts.tora import TORA_SYSTEM_PROMPT
    assert "rules_lookup" in get_planner_system_prompt([])
    assert "Citing the Law" in TORA_SYSTEM_PROMPT
