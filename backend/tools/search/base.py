from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class SearchError(Exception):
    """Base exception for all search-related errors."""
    pass


class SearchConnectionError(SearchError):
    """Raised when unable to connect to the search service."""
    pass


class SearchTimeoutError(SearchError):
    """Raised when a search request times out."""
    pass


class SearchResponseError(SearchError):
    """Raised when the search service returns an HTTP or API error response."""
    def __init__(self, message: str, status_code: Optional[int] = None, detail: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class SearchProviderNotConfiguredError(SearchError):
    """Raised when a search provider is missing required credentials or configuration."""
    pass


class SearchResult(BaseModel):
    """
    Strongly typed, normalized search result representation.
    Decoupled from any vendor-specific response format.
    """
    title: str = Field(..., description="Title of the search result item.")
    url: str = Field(..., description="Destination URL of the result.")
    snippet: str = Field(default="", description="Text snippet or abstract from the search result.")
    domain: str = Field(default="", description="Extracted domain or source identifier.")
    rank: Optional[int] = Field(default=None, description="1-indexed ranking position in search results.")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "domain": self.domain,
            "rank": self.rank,
        }


class SearchResponse(BaseModel):
    """
    Strongly typed, normalized search response containing ordered results and metadata.
    """
    query: str = Field(..., description="The query string that was executed.")
    results: List[SearchResult] = Field(default_factory=list, description="Ordered list of search results.")
    total: Optional[int] = Field(default=None, description="Total matching results if reported by provider.")
    provider: str = Field(default="unknown", description="Identifier of the search provider that fulfilled the query.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional provider metadata (duration, etc.).")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "query": self.query,
            "result_count": len(self.results),
            "total": self.total,
            "provider": self.provider,
            "results": [r.to_dict() for r in self.results],
            "metadata": self.metadata,
        }


class SearchProvider(ABC):
    """
    Abstract Base Class for search service providers.
    All search provider implementations must inherit from this class
    and implement the asynchronous search() method.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier name for this search provider."""
        pass

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = 5,
        **kwargs: Any,
    ) -> SearchResponse:
        """
        Execute a search query asynchronously and return normalized results.

        :param query: The search query string.
        :param max_results: Maximum number of results to return (1-10).
        :param kwargs: Optional provider-specific parameters.
        :return: Strongly typed SearchResponse.
        :raises SearchError: On connection, timeout, HTTP, or configuration failures.
        """
        pass
