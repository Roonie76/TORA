import logging
from typing import Optional, Dict, Any, Type
from pydantic import BaseModel, Field, field_validator

from .base import BaseTool, ToolMetadata, ToolResult, ToolError
from .search.base import SearchProvider, SearchError
from .search.factory import get_search_provider

logger = logging.getLogger("tora.tools.web_search")

MAX_QUERY_LENGTH: int = 300
DEFAULT_MAX_RESULTS: int = 5
MAX_SEARCH_RESULTS: int = 10


class WebSearchInput(BaseModel):
    """
    Input schema for the web search tool.
    Validates query string format, non-emptiness, and bounds max_results.
    """
    query: str = Field(
        ...,
        min_length=1,
        max_length=MAX_QUERY_LENGTH,
        description="The specific search query to look up on the public internet.",
    )
    max_results: int = Field(
        default=DEFAULT_MAX_RESULTS,
        ge=1,
        le=MAX_SEARCH_RESULTS,
        description="Maximum number of search results to return (1 to 10).",
    )

    @field_validator("query")
    @classmethod
    def validate_query_content(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Search query cannot be empty or only whitespace.")
        return cleaned


class WebSearchTool(BaseTool):
    """
    TORA Web Search Tool.
    Enables TORA to retrieve current information, official bank/RBI guidelines,
    live interest rates, tax notifications, and market updates from the public internet.

    Outputs structured, normalized search results without fetching full web page bodies.
    All external text is treated strictly as untrusted data.
    """

    name: str = "web_search"
    description: str = (
        "Search the public web for current information, live loan/FD/gold rates, "
        "tax circulars, RBI guidelines, financial news, and up-to-date facts."
    )
    args_schema: Type[BaseModel] = WebSearchInput

    def __init__(self, provider: Optional[SearchProvider] = None):
        self.metadata = ToolMetadata(
            name=self.name,
            description=self.description,
            version="1.0.0",
            tags=["web", "search", "live_data", "finance"],
            is_deterministic=False,
            requires_auth=False,
        )
        super().__init__()
        self._provider = provider or get_search_provider()

    @property
    def provider(self) -> SearchProvider:
        """Access the underlying SearchProvider."""
        return self._provider

    async def execute(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Execute web search via the configured SearchProvider.

        :param query: Cleaned search query string.
        :param max_results: Bounded result count limit.
        :return: Structured dictionary of search results.
        """
        limit = max(1, min(max_results, MAX_SEARCH_RESULTS))
        logger.info(
            "WebSearchTool executing query via provider '%s' (query_len=%d, max_results=%d)",
            self._provider.name,
            len(query),
            limit,
        )

        response = await self._provider.search(query=query, max_results=limit, **kwargs)

        return {
            "query": response.query,
            "result_count": len(response.results),
            "provider": response.provider,
            "results": [r.to_dict() for r in response.results],
        }
