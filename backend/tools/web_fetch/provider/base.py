from abc import ABC, abstractmethod
from typing import Optional, Dict, Any

from ..models import FetchResponse


class FetchProvider(ABC):
    """
    Abstract Base Class for web fetch service providers.
    All fetch provider implementations must inherit from this class
    and implement the asynchronous fetch() method.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier name for this fetch provider."""
        pass

    @abstractmethod
    async def fetch(
        self,
        url: str,
        max_chars: int = 3000,
        timeout_seconds: float = 10.0,
        **kwargs: Any,
    ) -> FetchResponse:
        """
        Safely fetch and extract readable content from a given URL.

        :param url: The target URL to fetch.
        :param max_chars: Maximum character limit for extracted text.
        :param timeout_seconds: Maximum duration allowed for the fetch operation.
        :param kwargs: Optional provider-specific arguments.
        :return: Standardized FetchResponse containing normalized FetchResult.
        :raises FetchError: On network, SSRF, timeout, status, or content errors.
        """
        pass
