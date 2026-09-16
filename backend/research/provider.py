import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, Set
from urllib.parse import urlparse, urlunparse

from .models import (
    SearchResult,
    FetchResult,
    SourceMetadata,
    SearchResponse,
    FetchedPage,
    ExtractedClaim,
    ResearchEvidence,
    SourceCredibility,
    VerifiedClaim,
    ClaimConflict,
    VerifiedResearchEvidence,
    SourceRelationship,
    AgreementStatus,
    CorroborationStrength,
    ClaimSemanticSignature,
    ResearchClaimGroup,
    ClaimAgreement,
    SynthesisConclusion,
    ResearchSynthesis,
    VerificationStatus,
    ConfidenceLevel,
    current_utc_timestamp,
    extract_domain_from_url,
)
from .exceptions import (
    ResearchError,
    ResearchSearchError,
    ResearchFetchError,
    ResearchValidationError,
    ResearchProviderError,
)
from ..tools.search.base import SearchProvider, SearchError
from ..tools.search.factory import get_search_provider
from ..tools.web_fetch.provider.base import FetchProvider
from ..tools.web_fetch.provider.factory import get_fetch_provider
from ..tools.web_fetch.models import FetchError

from .normalization import (
    SearchNormalizer,
    normalize_url,
    validate_search_query,
    MAX_QUERY_LENGTH as MAX_RESEARCH_QUERY_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_SNIPPET_LENGTH,
    DEFAULT_MAX_RESULTS as DEFAULT_RESEARCH_MAX_RESULTS,
    HARD_MAX_RESULTS as MAX_RESEARCH_RESULTS_LIMIT,
)
from .evidence import EvidenceExtractor
from .credibility import SourceVerifier
from .synthesis import ResearchSynthesizer

logger = logging.getLogger("tora.research.provider")

MAX_RESEARCH_URL_LENGTH: int = 2048
DEFAULT_RESEARCH_MAX_CHARS: int = 3000
MAX_CONCURRENT_RESEARCH_FETCHES: int = 3


class ResearchProvider(ABC):
    """
    Abstract Base Class for Research Providers.
    Defines unified search, fetch, page normalization, and evidence extraction capabilities.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier name for this research provider."""
        pass

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = DEFAULT_RESEARCH_MAX_RESULTS,
        **kwargs: Any,
    ) -> SearchResponse:
        """
        Execute a search query and return strongly typed, deduplicated SearchResponse.
        """
        pass

    @abstractmethod
    async def fetch(
        self,
        url: str,
        max_chars: int = DEFAULT_RESEARCH_MAX_CHARS,
        **kwargs: Any,
    ) -> FetchResult:
        """
        Fetch and extract readable plain text content from a target URL.
        """
        pass

    @abstractmethod
    async def fetch_page(
        self,
        url: str,
        max_chars: int = DEFAULT_RESEARCH_MAX_CHARS,
        **kwargs: Any,
    ) -> FetchedPage:
        """
        Fetch webpage and wrap in structured FetchedPage model.
        """
        pass

    @abstractmethod
    def verify_source(
        self,
        url: str,
        title: str = "Untitled",
        publication_date: Optional[str] = None,
        retrieved_at: Optional[str] = None,
    ) -> SourceCredibility:
        """
        Evaluate source authority, credibility score, and verification eligibility.
        """
        pass

    @abstractmethod
    def verify_evidence(
        self,
        evidence: ResearchEvidence,
    ) -> VerifiedResearchEvidence:
        """
        Verify claims, evaluate source credibility, and detect conflicts for ResearchEvidence.
        """
        pass

    @abstractmethod
    async def fetch_extract_and_verify(
        self,
        url: str,
        query: str = "",
        max_chars: int = DEFAULT_RESEARCH_MAX_CHARS,
        max_claims: int = 5,
        publication_date: Optional[str] = None,
        **kwargs: Any,
    ) -> VerifiedResearchEvidence:
        """
        Fetch a webpage, extract evidence, verify claims, assess credibility, and detect conflicts.
        """
        pass

    @abstractmethod
    def synthesize(
        self,
        query: str,
        verified_evidences: List[VerifiedResearchEvidence],
    ) -> ResearchSynthesis:
        """
        Synthesize multiple verified research evidences into a cohesive, corroborated report.
        """
        pass

    @abstractmethod
    async def multi_source_research(
        self,
        query: str,
        max_sources: int = 5,
        max_chars_per_source: int = DEFAULT_RESEARCH_MAX_CHARS,
        max_claims_per_source: int = 4,
    ) -> ResearchSynthesis:
        """
        Execute full multi-source search, fetch, verification, and synthesis pipeline.
        """
        pass


class CompositeResearchProvider(ResearchProvider):
    """
    Default Composite Research Provider.
    Coordinates underlying SearchProvider, FetchProvider, SearchNormalizer,
    EvidenceExtractor, SourceVerifier, and ResearchSynthesizer.
    """

    def __init__(
        self,
        search_provider: Optional[SearchProvider] = None,
        fetch_provider: Optional[FetchProvider] = None,
        normalizer: Optional[SearchNormalizer] = None,
        evidence_extractor: Optional[EvidenceExtractor] = None,
        source_verifier: Optional[SourceVerifier] = None,
        synthesizer: Optional[ResearchSynthesizer] = None,
        name: str = "composite_research_provider",
    ):
        self._search_provider = search_provider or get_search_provider()
        self._fetch_provider = fetch_provider or get_fetch_provider()
        self._normalizer = normalizer or SearchNormalizer()
        self._evidence_extractor = evidence_extractor or EvidenceExtractor()
        self._source_verifier = source_verifier or SourceVerifier()
        self._synthesizer = synthesizer or ResearchSynthesizer()
        self._name = name

    @property
    def normalizer(self) -> SearchNormalizer:
        return self._normalizer

    @property
    def evidence_extractor(self) -> EvidenceExtractor:
        return self._evidence_extractor

    @property
    def source_verifier(self) -> SourceVerifier:
        return self._source_verifier

    @property
    def synthesizer(self) -> ResearchSynthesizer:
        return self._synthesizer

    @property
    def name(self) -> str:
        return self._name

    @property
    def search_provider(self) -> SearchProvider:
        return self._search_provider

    @property
    def fetch_provider(self) -> FetchProvider:
        return self._fetch_provider

    def _validate_query(self, query: str) -> str:
        return validate_search_query(query, max_length=MAX_RESEARCH_QUERY_LENGTH)

    def _validate_url(self, url: str) -> str:
        if not url or not isinstance(url, str):
            raise ResearchValidationError("URL must be a non-empty string.")
        cleaned = url.strip()
        if not cleaned:
            raise ResearchValidationError("URL cannot be empty or only whitespace.")
        if len(cleaned) > MAX_RESEARCH_URL_LENGTH:
            raise ResearchValidationError(
                f"URL exceeds maximum allowed length of {MAX_RESEARCH_URL_LENGTH} characters."
            )
        if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
            raise ResearchValidationError("URL must use 'http://' or 'https://' scheme.")
        return cleaned

    async def search(
        self,
        query: str,
        max_results: int = DEFAULT_RESEARCH_MAX_RESULTS,
        **kwargs: Any,
    ) -> SearchResponse:
        """
        Execute search, normalize, enrich metadata, and deduplicate results by URL.
        """
        cleaned_query = self._validate_query(query)
        bounded_limit = max(1, min(max_results or DEFAULT_RESEARCH_MAX_RESULTS, MAX_RESEARCH_RESULTS_LIMIT))

        logger.info(
            "CompositeResearchProvider executing search (provider=%s, query=%s, limit=%d)",
            self._search_provider.name,
            cleaned_query,
            bounded_limit,
        )

        try:
            raw_response = await self._search_provider.search(
                query=cleaned_query,
                max_results=bounded_limit,
                **kwargs,
            )
        except SearchError as e:
            logger.error("Underlying SearchProvider failed: %s", str(e))
            raise ResearchSearchError(f"Search provider '{self._search_provider.name}' failed: {str(e)}", detail=e)
        except Exception as e:
            logger.exception("Unexpected exception in search provider: %s", str(e))
            raise ResearchProviderError(f"Unexpected search error: {str(e)}", detail=e)

        # Delegate normalization, deduplication, and bounding to SearchNormalizer
        cleaned_response = self._normalizer.normalize_response(
            raw_response=raw_response,
            query=cleaned_query,
            provider_name=f"research_composite({self._search_provider.name})",
            max_results=bounded_limit,
        )
        return cleaned_response

    async def fetch(
        self,
        url: str,
        max_chars: int = DEFAULT_RESEARCH_MAX_CHARS,
        **kwargs: Any,
    ) -> FetchResult:
        """
        Fetch webpage content, extract readable text, and wrap in enriched FetchResult.
        """
        cleaned_url = self._validate_url(url)
        bounded_chars = max(100, min(max_chars or DEFAULT_RESEARCH_MAX_CHARS, 50000))

        logger.info(
            "CompositeResearchProvider fetching URL (provider=%s, url=%s, max_chars=%d)",
            self._fetch_provider.name,
            cleaned_url,
            bounded_chars,
        )

        try:
            raw_fetch_response = await self._fetch_provider.fetch(
                url=cleaned_url,
                max_chars=bounded_chars,
                **kwargs,
            )
            res = raw_fetch_response.result
            domain = res.domain or extract_domain_from_url(res.final_url or cleaned_url)

            meta = SourceMetadata(
                url=res.final_url or cleaned_url,
                title=res.title or "Untitled",
                domain=domain,
                content_type=res.content_type,
                retrieved_at=current_utc_timestamp(),
                extra={"status_code": res.status_code, "provider": raw_fetch_response.provider},
            )

            return FetchResult(
                url=cleaned_url,
                final_url=res.final_url or cleaned_url,
                title=res.title or "Untitled",
                domain=domain,
                content=res.content or "",
                content_type=res.content_type or "text/html",
                status_code=res.status_code,
                truncated=res.truncated,
                metadata=meta,
                success=True,
                error=None,
            )
        except FetchError as e:
            logger.warning("FetchProvider error for URL '%s': %s", cleaned_url, str(e))
            raise ResearchFetchError(f"Fetch failed for '{cleaned_url}': {str(e)}", detail=e)
        except Exception as e:
            logger.exception("Unexpected exception fetching URL '%s': %s", cleaned_url, str(e))
            raise ResearchProviderError(f"Unexpected fetch failure for '{cleaned_url}': {str(e)}", detail=e)

    async def fetch_page(
        self,
        url: str,
        max_chars: int = DEFAULT_RESEARCH_MAX_CHARS,
        **kwargs: Any,
    ) -> FetchedPage:
        """
        Fetch a webpage and return a structured, validated FetchedPage.
        """
        fetch_res = await self.fetch(url=url, max_chars=max_chars, **kwargs)
        canonical = normalize_url(fetch_res.final_url or fetch_res.url)
        return FetchedPage.from_fetch_result(fetch_res, canonical_url=canonical)

    async def fetch_and_extract(
        self,
        url: str,
        query: str = "",
        max_chars: int = DEFAULT_RESEARCH_MAX_CHARS,
        max_claims: int = 5,
        **kwargs: Any,
    ) -> ResearchEvidence:
        """
        Fetch a webpage safely and extract structured, provenance-preserved research evidence.
        """
        page = await self.fetch_page(url=url, max_chars=max_chars, **kwargs)
        return self._evidence_extractor.extract_evidence(
            page=page,
            query=query,
            max_claims=max_claims,
        )

    def verify_source(
        self,
        url: str,
        title: str = "Untitled",
        publication_date: Optional[str] = None,
        retrieved_at: Optional[str] = None,
    ) -> SourceCredibility:
        """
        Evaluate source authority, credibility score, and verification eligibility.
        """
        return self._source_verifier.evaluate_source(
            url=url,
            title=title,
            publication_date=publication_date,
            retrieved_at=retrieved_at,
        )

    def verify_evidence(
        self,
        evidence: ResearchEvidence,
    ) -> VerifiedResearchEvidence:
        """
        Verify claims, evaluate source credibility, and detect conflicts for ResearchEvidence.
        """
        return self._source_verifier.verify_evidence(evidence=evidence)

    async def fetch_extract_and_verify(
        self,
        url: str,
        query: str = "",
        max_chars: int = DEFAULT_RESEARCH_MAX_CHARS,
        max_claims: int = 5,
        publication_date: Optional[str] = None,
        **kwargs: Any,
    ) -> VerifiedResearchEvidence:
        """
        Fetch a webpage, extract evidence, verify claims, assess credibility, and detect conflicts.
        """
        evidence = await self.fetch_and_extract(
            url=url,
            query=query,
            max_chars=max_chars,
            max_claims=max_claims,
            **kwargs,
        )
        return self.verify_evidence(evidence=evidence)

    def synthesize(
        self,
        query: str,
        verified_evidences: List[VerifiedResearchEvidence],
    ) -> ResearchSynthesis:
        """
        Synthesize multiple verified research evidences into a cohesive, corroborated report.
        """
        return self._synthesizer.synthesize(
            query=query,
            verified_evidences=verified_evidences,
        )

    async def multi_source_research(
        self,
        query: str,
        max_sources: int = 5,
        max_chars_per_source: int = DEFAULT_RESEARCH_MAX_CHARS,
        max_claims_per_source: int = 4,
    ) -> ResearchSynthesis:
        """
        Execute full multi-source search, fetch, verification, and synthesis pipeline.
        Bounded to max_sources to prevent unbounded crawling.
        """
        cleaned_query = self._validate_query(query)
        bounded_sources = max(1, min(max_sources, 10))

        # 1. Search for relevant sources
        search_res = await self.search(query=cleaned_query, max_results=bounded_sources)
        if not search_res.results:
            return self.synthesize(query=cleaned_query, verified_evidences=[])

        # 2. Fetch and verify distinct source URLs concurrently (bounded), preserving
        #    search-rank order. Sequential fetching could take max_sources x fetch-timeout.
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_RESEARCH_FETCHES)

        async def _fetch_one(r) -> VerifiedResearchEvidence:
            async with semaphore:
                try:
                    return await self.fetch_extract_and_verify(
                        url=r.url,
                        query=cleaned_query,
                        max_chars=max_chars_per_source,
                        max_claims=max_claims_per_source,
                    )
                except Exception as e:
                    logger.warning("Failed to fetch/verify search result URL '%s': %s", r.url, str(e))
                    # Insufficient-evidence placeholder keeps the failed source visible
                    cred = self.verify_source(url=r.url, title=r.title)
                    return VerifiedResearchEvidence(
                        query=cleaned_query,
                        sources=[cred],
                        verified_claims=[],
                        conflicts=[],
                        overall_verification_status=VerificationStatus.INSUFFICIENT_EVIDENCE,
                        overall_confidence=ConfidenceLevel.VERY_LOW,
                        retrieved_at=current_utc_timestamp(),
                        success=False,
                        error=str(e),
                    )

        verified_evidences: List[VerifiedResearchEvidence] = list(
            await asyncio.gather(*(_fetch_one(r) for r in search_res.results[:bounded_sources]))
        )

        # 3. Synthesize all collected evidence
        return self.synthesize(query=cleaned_query, verified_evidences=verified_evidences)



