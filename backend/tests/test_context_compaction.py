"""
Tests for ContextBuilder compact web search rendering.

Verifies that web search results are rendered compactly (title/source/snippet only)
to preserve LLM context-window budget, while calculator and error rendering remain
unchanged. Also verifies search text stays in data position (not system instruction),
and that full URLs are retained in the structured ToolResult.
"""

import pytest
from backend.context.builder import ContextBuilder
from backend.context.tools import ToolContext, ToolResult


def _get_external_content(builder: ContextBuilder, tool_context: ToolContext) -> str:
    """Build messages and return the untrusted external-data message content."""
    messages = builder.build(current_message="test query", tool_context=tool_context)
    data_msgs = [m for m in messages if m["role"] == "user" and m["content"].startswith("<external_data")]
    assert len(data_msgs) == 1
    assert '<external_data trust="untrusted">' not in messages[0]["content"]
    return data_msgs[0]["content"]


def _get_full_prompt(builder: ContextBuilder, tool_context: ToolContext) -> str:
    messages = builder.build(current_message="test query", tool_context=tool_context)
    return "\n".join(m["content"] for m in messages)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = "You are a helpful assistant."

# Realistic web search output with large tracking URLs
MOCK_SEARCH_OUTPUT = {
    "query": "current home loan interest rates all banks India",
    "result_count": 3,
    "provider": "duckduckgo",
    "results": [
        {
            "title": "Home Loan Interest Rates - Compare All Banks",
            "url": "https://www.example.com/home-loan?ad_domain=tatacapital.com&ad_provider_id=12345&ad_type=serp_listing&cmpgn=v2_serp_dd_bk&gclid=abc123def456ghi789jkl012mno345pqr678stu901vwx234yz567",
            "snippet": "Compare home loan interest rates from SBI, HDFC, ICICI and other major banks. Rates starting from 8.25% p.a.",
            "domain": "example.com",
            "rank": 1,
        },
        {
            "title": "Latest Home Loan Rates 2025 - NoBroker",
            "url": "https://www.nobroker.in/home-loan-rates?utm_source=duckduckgo&utm_medium=cpc&utm_campaign=home_loan_rates_2025&tracking_id=xyzzy_987654321_abcdefg",
            "snippet": "Check the latest home loan rates. SBI offers 8.25%, HDFC 8.35%, ICICI 8.40%.",
            "domain": "nobroker.in",
            "rank": 2,
        },
        {
            "title": "Best Home Loan Interest Rates in India",
            "url": "https://www.bankbazaar.com/home-loan.html",
            "snippet": "Find the best home loan interest rates from top banks and NBFCs in India.",
            "domain": "bankbazaar.com",
            "rank": 3,
        },
    ],
}

MOCK_CALCULATOR_OUTPUT = {"result": 20000}


def _build_search_context() -> ToolContext:
    """Create a ToolContext with web_search results."""
    return ToolContext(results=[
        ToolResult(
            tool_name="web_search",
            call_id="step_1",
            output=MOCK_SEARCH_OUTPUT,
            is_error=False,
        )
    ])


def _build_calculator_context() -> ToolContext:
    """Create a ToolContext with calculator results."""
    return ToolContext(results=[
        ToolResult(
            tool_name="calculator",
            call_id="step_1",
            output=MOCK_CALCULATOR_OUTPUT,
            is_error=False,
        )
    ])


def _build_error_context() -> ToolContext:
    """Create a ToolContext with an error result."""
    return ToolContext(results=[
        ToolResult(
            tool_name="calculator",
            call_id="step_1",
            output="Division by zero",
            is_error=True,
        )
    ])


def _get_system_content(builder: ContextBuilder, tool_context: ToolContext) -> str:
    """Build messages and return the system prompt content."""
    messages = builder.build(
        current_message="test query",
        tool_context=tool_context,
    )
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWebSearchCompactRendering:
    """Tests for compact web search result rendering in ContextBuilder."""

    def test_web_search_compact_rendering(self):
        """Web search results render in compact format with title/source/snippet."""
        builder = ContextBuilder(default_system_prompt=SYSTEM_PROMPT)
        content = _get_external_content(builder, _build_search_context())

        # Should contain the compact header
        assert "Found 3 results" in content
        # Should contain title, source, snippet for each result
        assert "Home Loan Interest Rates - Compare All Banks" in content
        assert "Source: example.com" in content
        assert "Rates starting from 8.25% p.a." in content

    def test_tracking_urls_not_in_prompt(self):
        """Full tracking URLs with query parameters do not appear in the system prompt."""
        builder = ContextBuilder(default_system_prompt=SYSTEM_PROMPT)
        content = _get_full_prompt(builder, _build_search_context())

        # Tracking URL fragments must not appear
        assert "ad_domain=tatacapital" not in content
        assert "ad_provider_id" not in content
        assert "utm_source=duckduckgo" not in content
        assert "tracking_id=xyzzy" not in content
        assert "gclid=" not in content
        # Full URLs must not appear
        assert "https://www.example.com/home-loan?" not in content
        assert "https://www.nobroker.in/home-loan-rates?" not in content

    def test_title_preserved_in_compact(self):
        """All search result titles appear in the rendered output."""
        builder = ContextBuilder(default_system_prompt=SYSTEM_PROMPT)
        content = _get_external_content(builder, _build_search_context())

        assert "Home Loan Interest Rates - Compare All Banks" in content
        assert "Latest Home Loan Rates 2025 - NoBroker" in content
        assert "Best Home Loan Interest Rates in India" in content

    def test_snippet_preserved_in_compact(self):
        """All search result snippets appear in the rendered output."""
        builder = ContextBuilder(default_system_prompt=SYSTEM_PROMPT)
        content = _get_external_content(builder, _build_search_context())

        assert "SBI offers 8.25%, HDFC 8.35%, ICICI 8.40%" in content
        assert "Find the best home loan interest rates" in content

    def test_domain_preserved_in_compact(self):
        """Domain/source info appears in the rendered output."""
        builder = ContextBuilder(default_system_prompt=SYSTEM_PROMPT)
        content = _get_external_content(builder, _build_search_context())

        assert "Source: example.com" in content
        assert "Source: nobroker.in" in content
        assert "Source: bankbazaar.com" in content

    def test_multiple_results_rendered(self):
        """All search results (not just the first) are rendered."""
        builder = ContextBuilder(default_system_prompt=SYSTEM_PROMPT)
        content = _get_external_content(builder, _build_search_context())

        # All three numbered items should appear
        assert "1. Home Loan Interest Rates" in content
        assert "2. Latest Home Loan Rates" in content
        assert "3. Best Home Loan Interest Rates" in content

    def test_structured_data_retains_urls(self):
        """The original ToolResult still contains the full URLs — compaction is rendering-only."""
        ctx = _build_search_context()
        result = ctx.results[0]
        raw_results = result.output["results"]

        # Verify full tracking URLs are still in the underlying data
        assert "ad_domain=tatacapital" in raw_results[0]["url"]
        assert "utm_source=duckduckgo" in raw_results[1]["url"]
        assert "https://www.bankbazaar.com/home-loan.html" == raw_results[2]["url"]

    def test_search_text_stays_in_data_position(self):
        """Search snippets never enter the system message; they sit in the delimited data message."""
        builder = ContextBuilder(default_system_prompt=SYSTEM_PROMPT)
        messages = builder.build(current_message="test query", tool_context=_build_search_context())

        system_content = messages[0]["content"]
        assert "SBI offers 8.25%" not in system_content
        assert "## Tool Execution Results" in system_content

        data_msg = messages[-2]
        assert data_msg["role"] == "user"
        assert data_msg["content"].startswith("<external_data")
        assert "SBI offers 8.25%" in data_msg["content"]
        assert messages[-1] == {"role": "user", "content": "test query"}

    def test_calculator_rendering_unchanged(self):
        """Calculator tool rendering uses the existing raw format."""
        builder = ContextBuilder(default_system_prompt=SYSTEM_PROMPT)
        content = _get_system_content(builder, _build_calculator_context())

        # Calculator should use the old-style rendering
        assert "- Tool 'calculator': Result = {'result': 20000}" in content
        # Should contain deterministic trust language
        assert "use these exact figures" in content

    def test_error_rendering_unchanged(self):
        """Error tool rendering uses the existing error format."""
        builder = ContextBuilder(default_system_prompt=SYSTEM_PROMPT)
        content = _get_system_content(builder, _build_error_context())

        assert "- Tool 'calculator': Error = Division by zero" in content

    def test_section_header_is_neutral(self):
        """Section header says 'Tool Execution Results', not 'Verified'."""
        builder = ContextBuilder(default_system_prompt=SYSTEM_PROMPT)
        content = _get_system_content(builder, _build_search_context())

        assert "## Tool Execution Results" in content
        assert "Verified Tool Execution Results" not in content

    def test_compact_vs_raw_size_reduction(self):
        """Compact rendering is significantly smaller than raw dict rendering."""
        builder = ContextBuilder(default_system_prompt=SYSTEM_PROMPT)
        compact_content = _get_system_content(builder, _build_search_context())

        # Compare against what the raw dict representation would be
        raw_repr = str(MOCK_SEARCH_OUTPUT)
        compact_tool_section = compact_content[compact_content.find("## Tool Execution Results"):]

        # The compact section should be substantially smaller than the raw dict repr
        assert len(compact_tool_section) < len(raw_repr), (
            f"Compact ({len(compact_tool_section)} chars) should be smaller than raw ({len(raw_repr)} chars)"
        )

    def test_search_trust_language(self):
        """Web search results section contains external-data trust warning."""
        builder = ContextBuilder(default_system_prompt=SYSTEM_PROMPT)
        content = _get_system_content(builder, _build_search_context())

        assert "external sources" in content
        assert "not been independently verified" in content
        # Should NOT contain the deterministic-only language
        assert "use these exact figures" not in content

    def test_mixed_tools_trust_language(self):
        """Mixed calculator + search results produce both trust statements."""
        ctx = ToolContext(results=[
            ToolResult(
                tool_name="calculator",
                call_id="step_1",
                output={"result": 5000},
                is_error=False,
            ),
            ToolResult(
                tool_name="web_search",
                call_id="step_2",
                output=MOCK_SEARCH_OUTPUT,
                is_error=False,
            ),
        ])
        builder = ContextBuilder(default_system_prompt=SYSTEM_PROMPT)
        content = _get_system_content(builder, ctx)

        # Both trust statements should be present
        assert "use these exact figures" in content
        assert "not been independently verified" in content
