import re
import html
import time
import logging
import urllib.parse
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
)

logger = logging.getLogger("tora.search.duckduckgo")

DEFAULT_DDG_URL: str = "https://html.duckduckgo.com/html/"
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class DuckDuckGoSearchProvider(SearchProvider):
    """
    Asynchronous DuckDuckGo search provider.
    Executes searches against DuckDuckGo's public HTML interface and extracts
    strongly typed, normalized search results without requiring API keys.
    """

    def __init__(
        self,
        endpoint_url: str = DEFAULT_DDG_URL,
        timeout_seconds: float = 10.0,
        user_agent: str = DEFAULT_USER_AGENT,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.endpoint_url = endpoint_url
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self._client = client

    @property
    def name(self) -> str:
        return "duckduckgo"

    def _extract_actual_url(self, raw_url: str) -> str:
        """Resolve DuckDuckGo redirect wrapper (uddg=...) to canonical URL."""
        if not raw_url:
            return ""
        if "uddg=" in raw_url:
            match = re.search(r"uddg=([^&]+)", raw_url)
            if match:
                return urllib.parse.unquote(match.group(1))
        elif raw_url.startswith("//"):
            return "https:" + raw_url
        return raw_url

    def _clean_html_text(self, raw_html: str) -> str:
        """Strip HTML tags and unescape HTML entities into plain text."""
        if not raw_html:
            return ""
        no_tags = re.sub(r"<[^>]+>", "", raw_html)
        return html.unescape(no_tags).strip()

    def _extract_domain(self, url: str) -> str:
        """Extract domain netloc safely from a URL."""
        try:
            parsed = urllib.parse.urlparse(url)
            return parsed.netloc or ""
        except Exception:
            return ""

    def _parse_html_results(self, content: str, max_results: int) -> List[SearchResult]:
        """
        Parse DuckDuckGo HTML output into normalized SearchResult items.
        Extracts titles, links, snippets, and domains with order preserved.
        """
        results: List[SearchResult] = []

        link_matches = list(re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', content))
        snippet_matches = list(re.finditer(r'<a[^>]+class="result__snippet"[^>]*>([\s\S]*?)</a>', content))

        for idx, lm in enumerate(link_matches):
            if len(results) >= max_results:
                break

            raw_url = lm.group(1)
            raw_title = lm.group(2)

            canonical_url = self._extract_actual_url(raw_url)
            clean_title = self._clean_html_text(raw_title)

            # Pair each result with the snippet located between this link and the next one.
            # Index-based pairing misattributes snippets when a result has no snippet.
            clean_snippet = ""
            next_start = link_matches[idx + 1].start() if idx + 1 < len(link_matches) else len(content)
            for sm in snippet_matches:
                if lm.end() <= sm.start() < next_start:
                    clean_snippet = self._clean_html_text(sm.group(1))
                    break

            domain = self._extract_domain(canonical_url)

            if canonical_url and clean_title:
                results.append(
                    SearchResult(
                        title=clean_title,
                        url=canonical_url,
                        snippet=clean_snippet,
                        domain=domain,
                        rank=len(results) + 1,
                    )
                )

        return results

    async def search(
        self,
        query: str,
        max_results: int = 5,
        **kwargs: Any,
    ) -> SearchResponse:
        """
        Execute an asynchronous search request to DuckDuckGo and return normalized results.
        """
        if not query or not query.strip():
            raise ValueError("Search query cannot be empty.")

        clean_query = query.strip()
        limit = max(1, min(max_results, 10))

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        data = {"q": clean_query, "b": ""}

        start_time = time.monotonic()
        logger.info(
            "Executing DuckDuckGo search: query_length=%d, max_results=%d",
            len(clean_query),
            limit,
        )

        try:
            if self._client is not None:
                response = await self._client.post(
                    self.endpoint_url,
                    data=data,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                    response = await client.post(
                        self.endpoint_url,
                        data=data,
                        headers=headers,
                    )

            if response.status_code != 200:
                logger.warning("DuckDuckGo returned non-200 status: %d", response.status_code)
                raise SearchResponseError(
                    message=f"DuckDuckGo search failed with HTTP status {response.status_code}",
                    status_code=response.status_code,
                    detail=response.text[:500] if response.text else None,
                )

            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            results = self._parse_html_results(response.text, max_results=limit)
            logger.info("DuckDuckGo search completed: returned %d results in %sms", len(results), duration_ms)

            return SearchResponse(
                query=clean_query,
                results=results,
                total=len(results),
                provider=self.name,
                metadata={"duration_ms": duration_ms, "status_code": response.status_code},
            )

        except httpx.TimeoutException as e:
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            logger.error("DuckDuckGo search timed out after %s seconds (%sms)", self.timeout_seconds, duration_ms)
            raise SearchTimeoutError(f"DuckDuckGo search timed out after {self.timeout_seconds} seconds.") from e

        except httpx.ConnectError as e:
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            logger.error("DuckDuckGo search connection failed (%sms)", duration_ms)
            raise SearchConnectionError("Unable to connect to DuckDuckGo search service.") from e

        except (SearchError, ValueError):
            raise

        except Exception as e:
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            logger.error("Unexpected error during DuckDuckGo search: %s (%sms)", str(e), duration_ms)
            raise SearchError(f"Unexpected search error: {str(e)}") from e
