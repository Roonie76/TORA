import os
import time
import logging
from typing import Optional, List, Dict, Any
import httpx

from .base import (
    SearchProvider,
    SearchResult,
    SearchResponse,
    SearchError,
    SearchConnectionError,
    SearchTimeoutError,
    SearchResponseError,
    SearchProviderNotConfiguredError,
)

logger = logging.getLogger("tora.search.tavily")

TAVILY_API_URL: str = "https://api.tavily.com/search"


class TavilySearchProvider(SearchProvider):
    """
    Asynchronous Tavily search provider.
    Executes search requests against the Tavily AI search API.
    API keys are strictly safeguarded and never exposed in logs, results, or exceptions.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint_url: str = TAVILY_API_URL,
        timeout_seconds: float = 10.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._api_key = api_key or os.environ.get("TAVILY_API_KEY") or os.environ.get("SEARCH_API_KEY")
        self.endpoint_url = endpoint_url
        self.timeout_seconds = timeout_seconds
        self._client = client

    @property
    def name(self) -> str:
        return "tavily"

    async def search(
        self,
        query: str,
        max_results: int = 5,
        **kwargs: Any,
    ) -> SearchResponse:
        """
        Execute an asynchronous search request to Tavily.
        """
        if not self._api_key or not self._api_key.strip():
            raise SearchProviderNotConfiguredError(
                "Tavily search provider is not configured. Missing TAVILY_API_KEY or SEARCH_API_KEY."
            )

        if not query or not query.strip():
            raise ValueError("Search query cannot be empty.")

        clean_query = query.strip()
        limit = max(1, min(max_results, 10))

        payload: Dict[str, Any] = {
            "api_key": self._api_key,
            "query": clean_query,
            "max_results": limit,
            "search_depth": kwargs.get("search_depth", "basic"),
            "include_domains": kwargs.get("include_domains"),
            "exclude_domains": kwargs.get("exclude_domains"),
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        start_time = time.monotonic()
        logger.info(
            "Executing Tavily search: query_length=%d, max_results=%d",
            len(clean_query),
            limit,
        )

        try:
            if self._client is not None:
                response = await self._client.post(
                    self.endpoint_url,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(
                        self.endpoint_url,
                        json=payload,
                    )

            if response.status_code != 200:
                logger.warning("Tavily search returned non-200 status: %d", response.status_code)
                detail_msg = f"HTTP status {response.status_code}"
                raise SearchResponseError(
                    message=f"Tavily search failed with {detail_msg}",
                    status_code=response.status_code,
                    detail=response.text[:200] if response.text else None,
                )

            data = response.json()
            raw_results = data.get("results", [])
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)

            results: List[SearchResult] = []
            for idx, item in enumerate(raw_results[:limit]):
                title = item.get("title", "")
                url = item.get("url", "")
                snippet = item.get("content", "")
                domain = ""
                try:
                    import urllib.parse
                    domain = urllib.parse.urlparse(url).netloc
                except Exception:
                    pass

                if title or url:
                    results.append(
                        SearchResult(
                            title=title,
                            url=url,
                            snippet=snippet,
                            domain=domain,
                            rank=idx + 1,
                        )
                    )

            logger.info("Tavily search completed: returned %d results in %sms", len(results), duration_ms)

            return SearchResponse(
                query=clean_query,
                results=results,
                total=len(results),
                provider=self.name,
                metadata={"duration_ms": duration_ms, "status_code": response.status_code},
            )

        except httpx.TimeoutException as e:
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            logger.error("Tavily search timed out after %s seconds (%sms)", self.timeout_seconds, duration_ms)
            raise SearchTimeoutError(f"Tavily search timed out after {self.timeout_seconds} seconds.") from e

        except httpx.ConnectError as e:
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            logger.error("Tavily search connection failed (%sms)", duration_ms)
            raise SearchConnectionError("Unable to connect to Tavily search service.") from e

        except (SearchError, ValueError):
            raise

        except Exception as e:
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            logger.error("Unexpected error during Tavily search: %s (%sms)", str(e), duration_ms)
            raise SearchError(f"Unexpected search error: {str(e)}") from e
