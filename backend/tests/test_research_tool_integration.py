"""
End-to-end tests for the research pipeline as used by the live agent.

Before the re-audit, CompositeResearchProvider.multi_source_research had no tests,
was not registered as a tool, and its failed-source fallback raised NameError
(VerificationStatus / ConfidenceLevel were not imported).
"""
import asyncio
from typing import Any, Dict

import pytest

import backend.main as main_module
from backend.agent.agent import ToraAgent
from backend.context import ContextBuilder, ToolContext
from backend.llm.base import LLMProvider, LLMResponse
from backend.planner import Planner
from backend.research.provider import CompositeResearchProvider
from backend.tools import ResearchTool, ToolExecutor, ToolRegistry
from backend.tools.search.base import SearchProvider, SearchResponse, SearchResult, SearchError
from backend.tools.web_fetch.models import FetchConnectionError, FetchResponse, FetchResult
from backend.tools.web_fetch.provider.base import FetchProvider

PAGES: Dict[str, Dict[str, str]] = {
    "https://sbi.co.in/web/interest-rates/home-loan": {
        "title": "SBI Home Loan Interest Rates",
        "content": "SBI home loan interest rates start from 7.25% p.a. onwards for salaried borrowers.",
    },
    "https://www.hdfcbank.com/personal/borrow/home-loan/rates": {
        "title": "HDFC Bank Home Loan Rates",
        "content": "HDFC Bank home loan interest rates starting from 7.90% p.a.",
    },
}


class FakeSearch(SearchProvider):
    def __init__(self, urls, fail=False):
        self.urls = urls
        self.fail = fail

    @property
    def name(self) -> str:
        return "fake"

    async def search(self, query: str, max_results: int = 5, **kwargs: Any) -> SearchResponse:
        if self.fail:
            raise SearchError("HTTP 202 throttled")
        return SearchResponse(
            query=query,
            provider="fake",
            results=[
                SearchResult(title=f"Result {i}", url=u, snippet="home loan rates", rank=i + 1)
                for i, u in enumerate(self.urls[:max_results])
            ],
        )


class FakeFetch(FetchProvider):
    def __init__(self):
        self.calls = []

    @property
    def name(self) -> str:
        return "fake_fetch"

    async def fetch(self, url: str, max_chars: int = 3000, **kwargs: Any) -> FetchResponse:
        self.calls.append(url)
        page = PAGES.get(url)
        if page is None:
            raise FetchConnectionError(f"cannot reach {url}")
        domain = url.split("/")[2]
        return FetchResponse(
            url=url,
            provider="fake_fetch",
            result=FetchResult(url=url, final_url=url, domain=domain, title=page["title"], content=page["content"]),
        )


def _tool(urls, fail=False):
    fetch = FakeFetch()
    provider = CompositeResearchProvider(search_provider=FakeSearch(urls, fail=fail), fetch_provider=fetch)
    return ResearchTool(provider=provider), fetch


def test_research_tool_registered_in_live_app():
    assert main_module.tool_registry.has("research")
    assert "research" in main_module.planner.tool_registry.list_names()


def test_research_tool_returns_sourced_conclusions():
    tool, fetch = _tool(list(PAGES))
    result = asyncio.run(tool.run({"query": "SBI vs HDFC home loan interest rates"}))
    assert result.success, result.error
    out = result.data
    assert out["conclusions"], out
    text = " ".join(c["synthesized_statement"] for c in out["conclusions"])
    assert "7.25" in text and "7.90" in text
    urls = {u for c in out["conclusions"] for u in c["provenance_urls"]}
    assert any("sbi.co.in" in u for u in urls)
    assert {s["domain"] for s in out["sources"]} >= {"sbi.co.in", "www.hdfcbank.com"} or len(out["sources"]) >= 2
    assert sorted(fetch.calls) == sorted(PAGES)


def test_failed_source_is_reported_not_crashing():
    urls = list(PAGES) + ["https://unreachable.example.com/rates"]
    tool, _ = _tool(urls)
    result = asyncio.run(tool.run({"query": "home loan rates", "max_sources": 3}))
    assert result.success, result.error
    assert result.data["conclusions"]


def test_search_throttling_returns_structured_error():
    tool, _ = _tool(list(PAGES), fail=True)
    result = asyncio.run(tool.run({"query": "home loan rates"}))
    assert not result.success
    assert "throttled" in result.error


@pytest.mark.parametrize("args", [{"query": ""}, {"query": "ok"}, {"query": "x" * 400}, {"query": "rates", "max_sources": 9}])
def test_research_input_validation(args):
    tool, _ = _tool(list(PAGES))
    result = asyncio.run(tool.run(args))
    assert not result.success


def test_research_output_rendered_as_untrusted_data():
    tool, _ = _tool(list(PAGES))
    result = asyncio.run(tool.run({"query": "SBI vs HDFC home loan interest rates"}))
    ctx = ToolContext().add_result(tool_name="research", call_id="r1", output=result.data)
    messages = ContextBuilder(default_system_prompt="You are TORA.").build(current_message="compare", tool_context=ctx)
    assert "7.25" not in messages[0]["content"]
    data = messages[-2]["content"]
    assert data.startswith("<external_data")
    assert "Multi-Source Research Synthesis" in data
    assert "7.25" in data and "Sources checked:" in data


def test_empty_research_tells_model_not_to_invent_figures():
    ctx = ToolContext().add_result(
        tool_name="research", call_id="r1",
        output={"query": "q", "conclusions": [], "sources": [], "overall_status": "insufficient_evidence"},
    )
    data = ContextBuilder(default_system_prompt="S").build(current_message="q", tool_context=ctx)[-2]["content"]
    assert "No verifiable claims" in data


class _PlannerLLM(LLMProvider):
    """First call = planner JSON selecting research; second = final answer."""

    def __init__(self):
        self.calls = []

    @property
    def default_model(self):
        return "m"

    def resolve_model(self, requested=None, available=None):
        return "m"

    async def generate(self, messages, model=None, options=None):
        self.calls.append(messages)
        if len(self.calls) == 1:
            return LLMResponse(
                content='{"thought": "compare banks", "requires_tools": true, "steps": '
                        '[{"tool_name": "research", "arguments": {"query": "SBI vs HDFC home loan rates"}}]}',
                model="m",
            )
        return LLMResponse(content="SBI starts from 7.25% p.a.", model="m")

    async def list_models(self):
        return ["m"]

    async def health_check(self):
        return {}


def test_agent_end_to_end_with_research_tool():
    tool, _ = _tool(list(PAGES))
    registry = ToolRegistry()
    registry.register(tool)
    llm = _PlannerLLM()
    agent = ToraAgent(
        llm_provider=llm,
        planner=Planner(llm_provider=llm, tool_registry=registry),
        tool_executor=ToolExecutor(registry=registry, default_timeout_seconds=10),
    )
    response = asyncio.run(agent.run("Compare SBI and HDFC home loan rates"))
    assert response.plan.steps[0].tool_name == "research"
    final_messages = llm.calls[1]
    assert "research" in final_messages[0]["content"] or "Tool Execution Results" in final_messages[0]["content"]
    assert "7.25" in final_messages[-2]["content"]
    assert "research" in main_module.planner.tool_registry.list_names()
