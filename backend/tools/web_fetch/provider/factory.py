import os
import logging
from typing import Optional, Any

from .base import FetchProvider
from .http import HTTPFetchProvider

logger = logging.getLogger("tora.web_fetch.factory")


def get_fetch_provider(
    provider_name: Optional[str] = None,
    timeout_seconds: float = 10.0,
    **kwargs: Any,
) -> FetchProvider:
    """
    Factory function to instantiate the configured FetchProvider.
    Defaults to 'http' (HTTPFetchProvider).

    :param provider_name: Explicit provider name ('http', etc.).
    :param timeout_seconds: Network timeout in seconds.
    :param kwargs: Additional provider-specific kwargs.
    :return: An initialized FetchProvider instance.
    """
    selected = (provider_name or os.environ.get("FETCH_PROVIDER", "http")).strip().lower()

    if selected == "http":
        return HTTPFetchProvider(timeout_seconds=timeout_seconds, **kwargs)
    else:
        logger.warning(
            "Unknown fetch provider '%s' requested. Falling back to HTTPFetchProvider.",
            selected,
        )
        return HTTPFetchProvider(timeout_seconds=timeout_seconds, **kwargs)
