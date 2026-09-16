from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from ..base import ToolError


# ---------------------------------------------------------------------------
# Fetch Error Hierarchy
# ---------------------------------------------------------------------------

class FetchError(ToolError):
    """Base exception for all web fetch operations."""
    pass


class FetchSSRFError(FetchError):
    """Raised when a URL attempts to access private, loopback, link-local, or forbidden addresses."""
    pass


class FetchSecurityError(FetchError):
    """Raised for security violations (e.g. invalid scheme, redirect to forbidden IP, DNS rebinding)."""
    pass


class FetchConnectionError(FetchError):
    """Raised when unable to establish a connection to the target web server."""
    pass


class FetchTimeoutError(FetchError):
    """Raised when fetching the webpage exceeds the configured timeout."""
    pass


class FetchResponseError(FetchError):
    """Raised when the target web server returns an HTTP error status code (4xx, 5xx)."""
    def __init__(self, message: str, status_code: Optional[int] = None, detail: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class FetchContentError(FetchError):
    """Raised when the fetched resource has an unsupported content type (e.g., PDF, image, binary)."""
    pass


class FetchSizeLimitError(FetchError):
    """Raised when the response size exceeds the allowed maximum byte limit."""
    pass


# ---------------------------------------------------------------------------
# Fetch Result and Response Models
# ---------------------------------------------------------------------------

class FetchResult(BaseModel):
    """
    Strongly typed, normalized representation of fetched webpage content.
    Decoupled from low-level HTTP transport and HTML parsing libraries.
    """
    url: str = Field(..., description="The original URL requested.")
    final_url: str = Field(..., description="The final destination URL after following redirects.")
    domain: str = Field(default="", description="The domain/host of the final URL.")
    title: str = Field(default="", description="The extracted page title (<title>).")
    content: str = Field(default="", description="Clean, readable plain text extracted from HTML.")
    content_type: str = Field(default="text/html", description="The validated MIME content-type of the page.")
    status_code: int = Field(default=200, description="HTTP status code from the server.")
    truncated: bool = Field(default=False, description="True if content was truncated to fit max_chars limit.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata (e.g., fetch duration, raw size).")

    def to_dict(self) -> Dict[str, Any]:
        """Convert FetchResult to a standard dictionary representation."""
        return {
            "url": self.url,
            "final_url": self.final_url,
            "domain": self.domain,
            "title": self.title,
            "content": self.content,
            "content_type": self.content_type,
            "status_code": self.status_code,
            "truncated": self.truncated,
            "metadata": self.metadata,
        }


class FetchResponse(BaseModel):
    """
    Standardized response envelope returned by FetchProvider implementations.
    """
    url: str = Field(..., description="The original URL requested.")
    result: FetchResult = Field(..., description="The structured fetch result.")
    provider: str = Field(default="http", description="Identifier of the fetch provider.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Provider execution metadata.")

    def to_dict(self) -> Dict[str, Any]:
        """Convert FetchResponse to standard dictionary."""
        return {
            "url": self.url,
            "provider": self.provider,
            "result": self.result.to_dict(),
            "metadata": self.metadata,
        }
