from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
from dataclasses import dataclass


class LLMProviderError(Exception):
    """Base exception for all LLM provider errors."""
    pass


class LLMConnectionError(LLMProviderError):
    """Raised when the LLM provider service is unreachable."""
    pass


class LLMTimeoutError(LLMProviderError):
    """Raised when request to LLM provider times out."""
    pass


class LLMResponseError(LLMProviderError):
    """Raised when LLM provider returns an error status or invalid output."""
    def __init__(self, message: str, status_code: Optional[int] = None, detail: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail




@dataclass
class LLMResponse:
    content: str
    model: str
    done: bool = True
    raw: Optional[Dict[str, Any]] = None


class LLMProvider(ABC):
    """
    Abstract interface for LLM providers (e.g. Ollama, OpenAI, Anthropic).
    Provides a standardized contract for non-streaming chat generation,
    model discovery, and health checks.
    """

    @property
    @abstractmethod
    def default_model(self) -> str:
        """Returns the default model configured for this provider."""
        pass

    @abstractmethod
    async def generate(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        """
        Generate a non-streaming chat completion.

        :param messages: List of message dictionaries with 'role' and 'content'.
        :param model: Optional override for the target model.
        :param options: Provider options (e.g. temperature, num_ctx).
        :return: LLMResponse containing the text output and metadata.
        """
        pass

    @abstractmethod
    async def list_models(self) -> List[str]:
        """Fetch list of available models from the provider."""
        pass

    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        """Check provider connectivity and status."""
        pass
