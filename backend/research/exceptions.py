from typing import Optional, Any


class ResearchError(Exception):
    """Base exception for all research-related errors."""
    def __init__(self, message: str, detail: Optional[Any] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message} (detail: {self.detail})"
        return self.message


class ResearchSearchError(ResearchError):
    """Raised when a research search operation fails."""
    pass


class ResearchFetchError(ResearchError):
    """Raised when a research fetch operation fails."""
    pass


class ResearchValidationError(ResearchError):
    """Raised when research input (e.g. URL, query, max_results) is invalid."""
    pass


class ResearchProviderError(ResearchError):
    """Raised when an underlying research provider encounters an unexpected failure."""
    pass
