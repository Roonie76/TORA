from typing import Optional
from .provider import ResearchProvider, CompositeResearchProvider
from .normalization import SearchNormalizer
from .evidence import EvidenceExtractor
from .credibility import SourceVerifier
from .synthesis import ResearchSynthesizer
from ..tools.search.base import SearchProvider
from ..tools.web_fetch.provider.base import FetchProvider


def get_research_provider(
    search_provider: Optional[SearchProvider] = None,
    fetch_provider: Optional[FetchProvider] = None,
    normalizer: Optional[SearchNormalizer] = None,
    evidence_extractor: Optional[EvidenceExtractor] = None,
    source_verifier: Optional[SourceVerifier] = None,
    synthesizer: Optional[ResearchSynthesizer] = None,
) -> ResearchProvider:
    """
    Factory function to resolve and instantiate the configured ResearchProvider.
    """
    return CompositeResearchProvider(
        search_provider=search_provider,
        fetch_provider=fetch_provider,
        normalizer=normalizer,
        evidence_extractor=evidence_extractor,
        source_verifier=source_verifier,
        synthesizer=synthesizer,
    )




