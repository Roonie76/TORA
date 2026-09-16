import logging
from typing import Optional, Dict, Any, Type
from pydantic import BaseModel, Field, field_validator

from ..base import BaseTool, ToolMetadata, ToolResult, ToolError
from .models import FetchError
from .provider.base import FetchProvider
from .provider.factory import get_fetch_provider
from .extractor import DEFAULT_MAX_CHARS, HARD_MAX_CHARS

logger = logging.getLogger("tora.tools.web_fetch")

MAX_URL_LENGTH: int = 2048


class WebFetchInput(BaseModel):
    """
    Input schema for the web fetch tool.
    Validates URL string format, non-emptiness, length limits, and bounds max_chars.
    """
    url: str = Field(
        ...,
        min_length=1,
        max_length=MAX_URL_LENGTH,
        description="The specific public HTTP or HTTPS webpage URL to fetch and read.",
    )
    max_chars: Optional[int] = Field(
        default=DEFAULT_MAX_CHARS,
        ge=100,
        le=HARD_MAX_CHARS,
        description=f"Maximum plain text characters to extract (100 to {HARD_MAX_CHARS}, default {DEFAULT_MAX_CHARS}).",
    )

    @field_validator("url")
    @classmethod
    def validate_url_content(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("URL cannot be empty or only whitespace.")
        if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
            raise ValueError("URL must start with 'http://' or 'https://'.")
        return cleaned


class WebFetchTool(BaseTool):
    """
    TORA Web Fetch Tool.
    Safely retrieves and extracts readable text from specific public web pages
    (e.g., official RBI guidelines, bank interest rate tables, government circulars, articles)
    returned from WebSearchTool or provided directly by the user.

    Features SSRF protection, redirect verification, response size caps,
    Content-Type validation, and prompt injection isolation.
    """

    name: str = "web_fetch"
    description: str = (
        "Fetch and extract readable plain text content from a specific public webpage URL (HTTP/HTTPS). "
        "Use this when you need to read the details of an article, guideline, or webpage from a specific URL."
    )
    args_schema: Type[BaseModel] = WebFetchInput

    def __init__(self, provider: Optional[FetchProvider] = None):
        self.metadata = ToolMetadata(
            name=self.name,
            description=self.description,
            version="1.0.0",
            tags=["web", "fetch", "content_extraction", "finance", "reader"],
            is_deterministic=False,
            requires_auth=False,
        )
        super().__init__()
        self._provider = provider or get_fetch_provider()

    @property
    def provider(self) -> FetchProvider:
        """Access the underlying FetchProvider."""
        return self._provider

    async def execute(
        self,
        url: str,
        max_chars: int = DEFAULT_MAX_CHARS,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Execute safe web fetch via the configured FetchProvider.

        :param url: Validated target webpage URL.
        :param max_chars: Bounded plain text character extraction limit.
        :return: Structured dictionary of extracted webpage content.
        """
        limit = max(100, min(max_chars or DEFAULT_MAX_CHARS, HARD_MAX_CHARS))
        logger.info(
            "WebFetchTool fetching URL via provider '%s' (url=%s, max_chars=%d)",
            self._provider.name,
            url,
            limit,
        )

        response = await self._provider.fetch(url=url, max_chars=limit, **kwargs)
        res = response.result

        return {
            "url": res.url,
            "final_url": res.final_url,
            "domain": res.domain,
            "title": res.title,
            "content": res.content,
            "content_type": res.content_type,
            "status_code": res.status_code,
            "truncated": res.truncated,
            "provider": response.provider,
        }
