import os
import logging
from typing import Optional, Dict, Any, Type

from .base import SearchProvider, SearchProviderNotConfiguredError
from .duckduckgo import DuckDuckGoSearchProvider
from .tavily import TavilySearchProvider

logger = logging.getLogger("tora.search.factory")


def get_search_provider(
    provider_name: Optional[str] = None,
    timeout_seconds: float = 10.0,
    **kwargs: Any,
) -> SearchProvider:
    """
    Factory function to instantiate the configured SearchProvider.
    Reads provider name from SEARCH_PROVIDER environment variable if not explicitly passed.
    Defaults to 'duckduckgo' which requires zero external credentials.

    :param provider_name: Explicit provider name ('duckduckgo', 'tavily', etc.).
    :param timeout_seconds: Network timeout in seconds.
    :param kwargs: Additional provider-specific kwargs.
    :return: An initialized SearchProvider instance.
    """
    selected = (provider_name or os.environ.get("SEARCH_PROVIDER", "duckduckgo")).strip().lower()

    if selected in ("duckduckgo", "ddg"):
        return DuckDuckGoSearchProvider(timeout_seconds=timeout_seconds, **kwargs)
    elif selected == "tavily":
        return TavilySearchProvider(timeout_seconds=timeout_seconds, **kwargs)
    else:
        logger.warning(
            "Unknown search provider '%s' requested. Falling back to DuckDuckGoSearchProvider.",
            selected,
        )
        return DuckDuckGoSearchProvider(timeout_seconds=timeout_seconds, **kwargs)
