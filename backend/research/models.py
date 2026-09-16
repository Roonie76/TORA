from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse
from pydantic import BaseModel, Field, field_validator, model_validator


def current_utc_timestamp() -> str:
    """Generate ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


def extract_domain_from_url(url: str) -> str:
    """Safely extract netloc domain from a URL string."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        return parsed.netloc.lower()
    except Exception:
        return ""


class SourceMetadata(BaseModel):
    """
    Metadata associated with a research source (web page, article, guideline, paper).
    Preserves origin provenance, timestamps, verification markers, and publication context.
    """
    url: str = Field(..., description="Canonical or requested URL of the source.")
    title: Optional[str] = Field(default=None, description="Extracted title of the source.")
    domain: Optional[str] = Field(default=None, description="Extracted hostname/domain of the source.")
    source_name: Optional[str] = Field(default=None, description="Human-readable publisher or source name.")
    retrieved_at: str = Field(default_factory=current_utc_timestamp, description="ISO-8601 UTC timestamp of retrieval.")
    published_at: Optional[str] = Field(default=None, description="Optional ISO-8601 or date string of publication.")
    author: Optional[str] = Field(default=None, description="Optional author or institution.")
    content_type: Optional[str] = Field(default="text/html", description="MIME content type of the source.")
    is_verified: bool = Field(default=False, description="Whether the source has been verified against trusted authorities.")
    extra: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary additional provider or source metadata.")

    @model_validator(mode="before")
    @classmethod
    def populate_domain_and_defaults(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("domain") and data.get("url"):
                data["domain"] = extract_domain_from_url(data["url"])
        return data

    def to_dict(self) -> Dict[str, Any]:
        """Serialize metadata to dictionary."""
        return {
            "url": self.url,
            "title": self.title,
            "domain": self.domain,
            "source_name": self.source_name,
            "retrieved_at": self.retrieved_at,
            "published_at": self.published_at,
            "author": self.author,
            "content_type": self.content_type,
            "is_verified": self.is_verified,
            "extra": self.extra,
        }


class SearchResult(BaseModel):
    """
    Strongly typed research search result item.
    Represents an individual search hit with metadata and success/error status.
    """
    query: str = Field(..., description="Query string that produced this result.")
    title: str = Field(..., description="Title of the search result.")
    url: str = Field(..., description="Destination URL.")
    domain: str = Field(default="", description="Source domain or host.")
    snippet: str = Field(default="", description="Snippet or summary text.")
    rank: Optional[int] = Field(default=None, description="1-indexed rank position in search results.")
    metadata: SourceMetadata = Field(..., description="Source metadata and provenance.")
    success: bool = Field(default=True, description="True if result was successfully retrieved and parsed.")
    error: Optional[str] = Field(default=None, description="Error detail if result extraction encountered an issue.")

    @classmethod
    def create(
        cls,
        query: str,
        title: str,
        url: str,
        snippet: str = "",
        domain: Optional[str] = None,
        rank: Optional[int] = None,
        published_at: Optional[str] = None,
        author: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
        success: bool = True,
        error: Optional[str] = None,
    ) -> "SearchResult":
        """Convenience factory method to construct a SearchResult with standard SourceMetadata."""
        clean_url = (url or "").strip()
        resolved_domain = domain or extract_domain_from_url(clean_url)
        meta = SourceMetadata(
            url=clean_url,
            title=title,
            domain=resolved_domain,
            published_at=published_at,
            author=author,
            extra=extra or {},
        )
        return cls(
            query=query,
            title=title,
            url=clean_url,
            domain=resolved_domain,
            snippet=snippet,
            rank=rank,
            metadata=meta,
            success=success,
            error=error,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize search result to dictionary."""
        return {
            "query": self.query,
            "title": self.title,
            "url": self.url,
            "domain": self.domain,
            "snippet": self.snippet,
            "rank": self.rank,
            "metadata": self.metadata.to_dict(),
            "success": self.success,
            "error": self.error,
        }


class FetchResult(BaseModel):
    """
    Strongly typed research fetch result.
    Represents the full or extracted content of a webpage with rich provenance metadata.
    """
    url: str = Field(..., description="Requested source URL.")
    final_url: str = Field(..., description="Final URL after following redirects.")
    title: str = Field(default="", description="Extracted webpage title.")
    domain: str = Field(default="", description="Webpage domain / host.")
    content: str = Field(default="", description="Clean extracted plain text content.")
    content_type: str = Field(default="text/html", description="Validated MIME content type.")
    status_code: int = Field(default=200, description="HTTP status code from target server.")
    truncated: bool = Field(default=False, description="True if content was truncated to character budget.")
    metadata: SourceMetadata = Field(..., description="Source metadata and provenance.")
    success: bool = Field(default=True, description="True if fetch and extraction succeeded.")
    error: Optional[str] = Field(default=None, description="Error detail if fetch failed.")

    @classmethod
    def create(
        cls,
        url: str,
        final_url: Optional[str] = None,
        title: str = "",
        content: str = "",
        domain: Optional[str] = None,
        content_type: str = "text/html",
        status_code: int = 200,
        truncated: bool = False,
        published_at: Optional[str] = None,
        author: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
        success: bool = True,
        error: Optional[str] = None,
    ) -> "FetchResult":
        """Convenience factory method to construct a FetchResult with SourceMetadata."""
        clean_url = (url or "").strip()
        resolved_final = (final_url or clean_url).strip()
        resolved_domain = domain or extract_domain_from_url(resolved_final)
        meta = SourceMetadata(
            url=resolved_final,
            title=title,
            domain=resolved_domain,
            content_type=content_type,
            published_at=published_at,
            author=author,
            extra=extra or {},
        )
        return cls(
            url=clean_url,
            final_url=resolved_final,
            title=title,
            domain=resolved_domain,
            content=content,
            content_type=content_type,
            status_code=status_code,
            truncated=truncated,
            metadata=meta,
            success=success,
            error=error,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize fetch result to dictionary."""
        return {
            "url": self.url,
            "final_url": self.final_url,
            "title": self.title,
            "domain": self.domain,
            "content": self.content,
            "content_type": self.content_type,
            "status_code": self.status_code,
            "truncated": self.truncated,
            "metadata": self.metadata.to_dict(),
            "success": self.success,
            "error": self.error,
        }


class SearchResponse(BaseModel):
    """
    Unified research search response envelope.
    Contains ordered, validated, deduplicated SearchResult items.
    """
    query: str = Field(..., description="Query string executed.")
    results: List[SearchResult] = Field(default_factory=list, description="List of structured search results.")
    total_results: int = Field(default=0, description="Count of returned search results.")
    provider: str = Field(default="unknown", description="Research provider identifier.")
    retrieved_at: str = Field(default_factory=current_utc_timestamp, description="ISO-8601 UTC execution timestamp.")
    success: bool = Field(default=True, description="True if provider fulfilled query without fatal error.")
    error: Optional[str] = Field(default=None, description="Error detail if query failed.")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize response envelope to dictionary."""
        return {
            "query": self.query,
            "total_results": self.total_results,
            "provider": self.provider,
            "retrieved_at": self.retrieved_at,
            "success": self.success,
            "error": self.error,
            "results": [r.to_dict() for r in self.results],
        }


class FetchedPage(BaseModel):
    """
    Strongly typed representation of a fetched web page.
    Captures status, cleaned text content, headers, truncation flags, and provenance.
    """
    url: str = Field(..., description="Requested source URL.")
    canonical_url: str = Field(..., description="Canonical final URL after redirects and normalization.")
    title: str = Field(default="", description="Extracted title of the page.")
    domain: str = Field(default="", description="Hostname or domain of the page.")
    status_code: int = Field(default=200, description="HTTP status code.")
    content_type: str = Field(default="text/html", description="Validated MIME content type.")
    retrieved_at: str = Field(default_factory=current_utc_timestamp, description="ISO-8601 UTC retrieval timestamp.")
    text_content: str = Field(default="", description="Structured, readable extracted plain text.")
    truncated: bool = Field(default=False, description="True if extracted text was bounded by character budget.")
    extraction_status: str = Field(default="success", description="Extraction status indicator ('success', 'partial', 'failed').")
    metadata: SourceMetadata = Field(..., description="Origin provenance metadata.")
    success: bool = Field(default=True, description="True if HTTP fetch and extraction succeeded.")
    error: Optional[str] = Field(default=None, description="Error message if fetch or extraction failed.")

    @classmethod
    def from_fetch_result(cls, fetch_res: FetchResult, canonical_url: Optional[str] = None) -> "FetchedPage":
        """Construct a FetchedPage directly from a FetchResult."""
        canon = canonical_url or fetch_res.final_url or fetch_res.url
        return cls(
            url=fetch_res.url,
            canonical_url=canon,
            title=fetch_res.title,
            domain=fetch_res.domain,
            status_code=fetch_res.status_code,
            content_type=fetch_res.content_type,
            retrieved_at=fetch_res.metadata.retrieved_at or current_utc_timestamp(),
            text_content=fetch_res.content,
            truncated=fetch_res.truncated,
            extraction_status="success" if fetch_res.success else "failed",
            metadata=fetch_res.metadata,
            success=fetch_res.success,
            error=fetch_res.error,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize FetchedPage to dictionary."""
        return {
            "url": self.url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "domain": self.domain,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "retrieved_at": self.retrieved_at,
            "text_content": self.text_content,
            "truncated": self.truncated,
            "extraction_status": self.extraction_status,
            "metadata": self.metadata.to_dict(),
            "success": self.success,
            "error": self.error,
        }


class ExtractedClaim(BaseModel):
    """
    Strongly typed evidence claim extracted from a research source.
    Always maintains complete provenance (URL, title, domain, retrieval timestamp)
    alongside exact financial semantics.
    """
    claim_text: str = Field(..., description="The factual statement or assertion.")
    supporting_text: str = Field(default="", description="Surrounding contextual sentence or paragraph from source.")
    source_url: str = Field(..., description="Canonical source URL.")
    source_title: str = Field(default="", description="Source document title.")
    source_domain: str = Field(default="", description="Source domain hostname.")
    retrieved_at: str = Field(default_factory=current_utc_timestamp, description="ISO-8601 UTC retrieval timestamp.")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Extraction confidence score.")
    financial_entities: Dict[str, Any] = Field(
        default_factory=dict,
        description="Preserved numerical entities (rates, amounts, currencies, tenure, qualifiers).",
    )
    extraction_type: str = Field(default="deterministic_passage", description="Extraction mechanism type.")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize claim to dictionary."""
        return {
            "claim_text": self.claim_text,
            "supporting_text": self.supporting_text,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "source_domain": self.source_domain,
            "retrieved_at": self.retrieved_at,
            "confidence": self.confidence,
            "financial_entities": self.financial_entities,
            "extraction_type": self.extraction_type,
        }


class ResearchEvidence(BaseModel):
    """
    Structured container for research evidence extracted from a source.
    Aggregates provenanced claims, underlying page metadata, and extraction status.
    """
    query: str = Field(default="", description="Research query or topic that guided extraction.")
    source_url: str = Field(..., description="Canonical source URL.")
    source_title: str = Field(default="", description="Source document title.")
    source_domain: str = Field(default="", description="Source domain.")
    retrieved_at: str = Field(default_factory=current_utc_timestamp, description="ISO-8601 UTC timestamp.")
    claims: List[ExtractedClaim] = Field(default_factory=list, description="List of provenanced claims.")
    page_summary: Optional[str] = Field(default=None, description="Optional brief summary passage.")
    raw_content_truncated: bool = Field(default=False, description="Whether source page text was truncated.")
    success: bool = Field(default=True, description="True if evidence was successfully extracted.")
    error: Optional[str] = Field(default=None, description="Error detail if extraction or fetch failed.")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize ResearchEvidence to dictionary."""
        return {
            "query": self.query,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "source_domain": self.source_domain,
            "retrieved_at": self.retrieved_at,
            "claims": [c.to_dict() for c in self.claims],
            "page_summary": self.page_summary,
            "raw_content_truncated": self.raw_content_truncated,
            "success": self.success,
            "error": self.error,
        }


# =============================================================================
# Phase 2H-D: Source Verification & Credibility Models
# =============================================================================

from enum import Enum


class SourceType(str, Enum):
    """Categorical classification of a research source."""
    PRIMARY_OFFICIAL = "primary_official"        # Official product issuer/bank/lender
    REGULATOR = "regulator"                      # RBI, SEBI, IRDAI, PFRDA, etc.
    GOVERNMENT = "government"                    # Income Tax Dept, Ministry of Finance, etc.
    BANK_NBFC = "bank_nbfc"                      # Regulated commercial bank or NBFC
    ESTABLISHED_FINANCIAL_MEDIA = "established_financial_media"  # Moneycontrol, ET, Mint, etc.
    SECONDARY_AGGREGATOR = "secondary_aggregator"  # BankBazaar, Paisabazaar, Policybazaar, etc.
    GENERIC_BLOG = "generic_blog"                # Unverified financial blog / article
    UNVERIFIED_FORUM = "unverified_forum"        # Reddit, Quora, discussion forums
    UNKNOWN = "unknown"                          # Unclassified or unrecognizable domain


class AuthorityLevel(str, Enum):
    """Authority tier of a source."""
    VERY_HIGH = "VERY_HIGH"  # Regulators, Government, Gazette, Official Circulars
    HIGH = "HIGH"            # Official Bank / Primary Product Issuers
    MEDIUM_HIGH = "MEDIUM_HIGH"  # Tier-1 Financial Publications
    MEDIUM = "MEDIUM"        # Reputable Aggregators & Industry Portals
    LOW = "LOW"              # Generic blogs, content farms, affiliate portals
    VERY_LOW = "VERY_LOW"    # Anonymous, unverified, or user-generated forums


class VerificationStatus(str, Enum):
    """Verification outcome for a research source or claim."""
    VERIFIED_PRIMARY = "VERIFIED_PRIMARY"        # Confirmed via authoritative primary source
    VERIFIED_SECONDARY = "VERIFIED_SECONDARY"    # Confirmed via established secondary source
    CORROBORATED = "CORROBORATED"                # Confirmed across multiple independent sources
    CONFLICTING = "CONFLICTING"                  # Contradicted by another credible source
    UNVERIFIED = "UNVERIFIED"                    # Source lacks sufficient verifiable authority
    STALE = "STALE"                              # Verified but outdated / expired
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # Source does not contain sufficient detail
    REJECTED = "REJECTED"                        # Failed safety, lookalike, or authenticity check


class FreshnessStatus(str, Enum):
    """Freshness status of publication dates."""
    CURRENT = "CURRENT"      # Published / updated within last 30 days
    RECENT = "RECENT"        # Published within 30 to 180 days
    AGING = "AGING"          # Published within 180 to 365 days
    STALE = "STALE"          # Published > 365 days ago
    UNKNOWN = "UNKNOWN"      # Publication date is unknown (retrieval date is NOT publication date)


class ConfidenceLevel(str, Enum):
    """Confidence level assigned to claims and evidence."""
    VERY_HIGH = "VERY_HIGH"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    VERY_LOW = "VERY_LOW"


class SourceCredibility(BaseModel):
    """
    Detailed, explainable credibility assessment for a research source.
    Evaluates authority, domain identity, HTTPS security, freshness, and claim suitability.
    """
    url: str = Field(..., description="Canonical source URL.")
    canonical_url: str = Field(..., description="Normalized canonical URL.")
    domain: str = Field(..., description="Source hostname domain.")
    source_title: str = Field(default="Untitled", description="Extracted source document title.")
    source_type: SourceType = Field(default=SourceType.UNKNOWN, description="Classified source category.")
    authority_level: AuthorityLevel = Field(default=AuthorityLevel.LOW, description="Assessed authority level.")
    credibility_score: float = Field(default=0.5, ge=0.0, le=1.0, description="Normalized credibility score (0.0 to 1.0).")
    verification_status: VerificationStatus = Field(default=VerificationStatus.UNVERIFIED, description="Verification status.")
    freshness: FreshnessStatus = Field(default=FreshnessStatus.UNKNOWN, description="Date freshness evaluation.")
    retrieval_timestamp: str = Field(default_factory=current_utc_timestamp, description="UTC retrieval timestamp.")
    publication_date: Optional[str] = Field(default=None, description="Explicit publication or last-modified date if known.")
    reasons: List[str] = Field(default_factory=list, description="Explicit deterministic reasons for the credibility rating.")
    warnings: List[str] = Field(default_factory=list, description="Security or data quality warnings.")
    is_primary: bool = Field(default=False, description="True if source is an official primary publisher.")
    is_official: bool = Field(default=False, description="True if source is an official regulator, government, or bank.")
    is_financial_claim_suitable: bool = Field(default=False, description="True if source is suitable for grounding financial claims.")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize SourceCredibility to dictionary."""
        return {
            "url": self.url,
            "canonical_url": self.canonical_url,
            "domain": self.domain,
            "source_title": self.source_title,
            "source_type": self.source_type.value,
            "authority_level": self.authority_level.value,
            "credibility_score": self.credibility_score,
            "verification_status": self.verification_status.value,
            "freshness": self.freshness.value,
            "retrieval_timestamp": self.retrieval_timestamp,
            "publication_date": self.publication_date,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "is_primary": self.is_primary,
            "is_official": self.is_official,
            "is_financial_claim_suitable": self.is_financial_claim_suitable,
        }


class VerifiedClaim(BaseModel):
    """
    Strongly typed verified research claim with bound source credibility,
    exact qualifiers, and explainable confidence.
    """
    claim_text: str = Field(..., description="The verified factual statement or assertion.")
    supporting_text: str = Field(default="", description="Surrounding contextual passage from source.")
    source_url: str = Field(..., description="Canonical source URL.")
    source_title: str = Field(default="", description="Source document title.")
    source_domain: str = Field(default="", description="Source domain hostname.")
    source_credibility: SourceCredibility = Field(..., description="Credibility evaluation of the source.")
    verification_status: VerificationStatus = Field(default=VerificationStatus.UNVERIFIED, description="Claim verification status.")
    confidence: ConfidenceLevel = Field(default=ConfidenceLevel.LOW, description="Assessed confidence level.")
    qualifiers: List[str] = Field(default_factory=list, description="Preserved semantic qualifiers ('starting from', 'up to', etc.).")
    financial_entities: Dict[str, Any] = Field(default_factory=dict, description="Preserved numerical rates, amounts, currencies.")
    effective_date: Optional[str] = Field(default=None, description="Effective date associated with this claim.")
    retrieved_at: str = Field(default_factory=current_utc_timestamp, description="UTC retrieval timestamp.")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize VerifiedClaim to dictionary."""
        return {
            "claim_text": self.claim_text,
            "supporting_text": self.supporting_text,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "source_domain": self.source_domain,
            "source_credibility": self.source_credibility.to_dict(),
            "verification_status": self.verification_status.value,
            "confidence": self.confidence.value,
            "qualifiers": self.qualifiers,
            "financial_entities": self.financial_entities,
            "effective_date": self.effective_date,
            "retrieved_at": self.retrieved_at,
        }


class ClaimConflict(BaseModel):
    """
    Represents a detected contradiction or divergence between multiple claims.
    Preserves all conflicting sources while recording the preferred source based on authority.
    """
    claim_type: str = Field(..., description="Category of the conflicting claim (e.g. 'interest_rate', 'tenure').")
    conflicting_claims: List[VerifiedClaim] = Field(..., description="All conflicting claim variants.")
    conflict_detected: bool = Field(default=True, description="True if conflict exists.")
    preferred_claim: Optional[VerifiedClaim] = Field(default=None, description="Preferred claim based on authority/freshness.")
    reason: str = Field(..., description="Deterministic reason for the conflict evaluation.")
    divergence_detail: str = Field(default="", description="Summary of the conflicting numerical values.")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize ClaimConflict to dictionary."""
        return {
            "claim_type": self.claim_type,
            "conflict_detected": self.conflict_detected,
            "reason": self.reason,
            "divergence_detail": self.divergence_detail,
            "preferred_claim": self.preferred_claim.to_dict() if self.preferred_claim else None,
            "conflicting_claims": [c.to_dict() for c in self.conflicting_claims],
        }


class VerifiedResearchEvidence(BaseModel):
    """
    Structured container for verified research evidence.
    Combines verified sources, claims, conflict detections, and aggregate verification status.
    """
    query: str = Field(default="", description="Research query or topic.")
    sources: List[SourceCredibility] = Field(default_factory=list, description="Evaluated sources.")
    verified_claims: List[VerifiedClaim] = Field(default_factory=list, description="Verified claims with provenance.")
    conflicts: List[ClaimConflict] = Field(default_factory=list, description="Detected claim conflicts.")
    overall_verification_status: VerificationStatus = Field(default=VerificationStatus.UNVERIFIED, description="Overall status.")
    overall_confidence: ConfidenceLevel = Field(default=ConfidenceLevel.LOW, description="Overall confidence level.")
    retrieved_at: str = Field(default_factory=current_utc_timestamp, description="UTC execution timestamp.")
    success: bool = Field(default=True, description="True if research verification completed.")
    error: Optional[str] = Field(default=None, description="Error detail if verification failed.")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize VerifiedResearchEvidence to dictionary."""
        return {
            "query": self.query,
            "sources": [s.to_dict() for s in self.sources],
            "verified_claims": [c.to_dict() for c in self.verified_claims],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "overall_verification_status": self.overall_verification_status.value,
            "overall_confidence": self.overall_confidence.value,
            "retrieved_at": self.retrieved_at,
            "success": self.success,
            "error": self.error,
        }


# =============================================================================
# Phase 2H-E: Multi-Source Research Synthesis & Corroboration Models
# =============================================================================

class SourceRelationship(str, Enum):
    """Relationship between a research source and underlying claims."""
    INDEPENDENT = "independent"      # Genuinely independent first-party or original reporting
    DERIVED = "derived"              # Quotes or attributes figures directly to another source
    SYNDICATED = "syndicated"        # Exact or near-identical republished wire/partner text
    UNKNOWN = "unknown"              # Relationship cannot be determined with certainty


class AgreementStatus(str, Enum):
    """Consensus and agreement state across multiple research sources."""
    UNANIMOUS = "UNANIMOUS"                          # All independent evaluated sources agree completely
    MAJORITY_AGREEMENT = "MAJORITY_AGREEMENT"        # Predominant consensus across credible sources
    CONFLICTING = "CONFLICTING"                      # Contradictory claims/rates detected
    UNVERIFIED_SINGLE_SOURCE = "UNVERIFIED_SINGLE_SOURCE"  # Only a single uncorroborated source available
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # Incomplete or snippet-only evidence


class CorroborationStrength(str, Enum):
    """Strength of multi-source corroboration."""
    VERY_HIGH = "VERY_HIGH"  # Primary official source + independent confirmation
    HIGH = "HIGH"            # Primary official source alone OR >=2 independent tier-1 sources
    MEDIUM = "MEDIUM"        # Single tier-1 media OR multiple secondary aggregators
    LOW = "LOW"              # Single secondary aggregator or blog
    NONE = "NONE"            # Low-quality, forum, or unverified single source


class ClaimSemanticSignature(BaseModel):
    """
    Structured key used to group semantically identical claims across disparate sources.
    Prevents false merges (e.g. SBI 7.25% vs HDFC 7.25%).
    """
    entity: str = Field(default="unknown", description="Institution or entity (e.g. 'sbi', 'hdfc', 'rbi').")
    product: str = Field(default="general", description="Financial product category (e.g. 'home_loan', 'repo_rate').")
    metric_type: str = Field(default="interest_rate", description="Metric type (e.g. 'interest_rate', 'loan_amount', 'tax_slab').")
    raw_value: str = Field(default="", description="Normalized raw numerical value (e.g. '7.25% p.a.').")
    qualifiers: List[str] = Field(default_factory=list, description="Preserved semantic qualifiers.")

    def signature_key(self) -> str:
        """Generate deterministic lookup string based on entity, product, metric type, and rate."""
        return f"{self.entity.lower().strip()}:{self.product.lower().strip()}:{self.metric_type.lower().strip()}:{self.raw_value.lower().strip()}"



class ResearchClaimGroup(BaseModel):
    """
    Aggregates semantically matching claims from multiple sources.
    Maintains all supporting evidence, source relationships, and authority tiers.
    """
    group_id: str = Field(..., description="Unique claim group identifier.")
    normalized_claim_text: str = Field(..., description="Representative normalized statement of the claim.")
    entity: str = Field(default="unknown", description="Associated financial institution or regulator.")
    product: str = Field(default="general", description="Associated product or policy domain.")
    metric_type: str = Field(default="interest_rate", description="Type of metric.")
    metric_value: str = Field(default="", description="Extracted numerical figure or rate.")
    qualifiers: List[str] = Field(default_factory=list, description="Preserved qualifiers.")
    effective_date: Optional[str] = Field(default=None, description="Effective date if reported.")
    supporting_claims: List[VerifiedClaim] = Field(default_factory=list, description="All verified claims in this group.")
    supporting_sources: List[SourceCredibility] = Field(default_factory=list, description="All supporting source evaluations.")
    is_primary_supported: bool = Field(default=False, description="True if supported by at least one primary official source.")
    highest_authority: AuthorityLevel = Field(default=AuthorityLevel.LOW, description="Highest authority level in this group.")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize claim group to dictionary."""
        return {
            "group_id": self.group_id,
            "normalized_claim_text": self.normalized_claim_text,
            "entity": self.entity,
            "product": self.product,
            "metric_type": self.metric_type,
            "metric_value": self.metric_value,
            "qualifiers": self.qualifiers,
            "effective_date": self.effective_date,
            "is_primary_supported": self.is_primary_supported,
            "highest_authority": self.highest_authority.value,
            "supporting_claims": [c.to_dict() for c in self.supporting_claims],
            "supporting_sources": [s.to_dict() for s in self.supporting_sources],
        }


class ClaimAgreement(BaseModel):
    """
    Represents the degree of agreement and independence for a claim group.
    """
    group_id: str = Field(..., description="Claim group identifier.")
    claim_text: str = Field(..., description="Representative claim text.")
    independent_source_count: int = Field(default=0, description="Count of genuinely independent supporting sources.")
    derived_source_count: int = Field(default=0, description="Count of derived/quoting supporting sources.")
    syndicated_source_count: int = Field(default=0, description="Count of syndicated/republished sources.")
    agreement_status: AgreementStatus = Field(default=AgreementStatus.UNVERIFIED_SINGLE_SOURCE, description="Agreement status.")
    corroboration_strength: CorroborationStrength = Field(default=CorroborationStrength.NONE, description="Corroboration strength.")
    explanation: str = Field(..., description="Deterministic explanation of the agreement/corroboration outcome.")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize ClaimAgreement to dictionary."""
        return {
            "group_id": self.group_id,
            "claim_text": self.claim_text,
            "independent_source_count": self.independent_source_count,
            "derived_source_count": self.derived_source_count,
            "syndicated_source_count": self.syndicated_source_count,
            "agreement_status": self.agreement_status.value,
            "corroboration_strength": self.corroboration_strength.value,
            "explanation": self.explanation,
        }


class SynthesisConclusion(BaseModel):
    """
    Final synthesized conclusion for a specific entity or comparison topic.
    Preserves qualifiers, preferred claim, competing claims, caveats, and citations.
    """
    topic: str = Field(..., description="Topic or comparison header.")
    entity: str = Field(default="unknown", description="Entity name (e.g. 'SBI', 'HDFC', 'RBI').")
    product: str = Field(default="general", description="Product category.")
    synthesized_statement: str = Field(..., description="Authoritative synthesized factual statement.")
    preferred_claim: Optional[VerifiedClaim] = Field(default=None, description="Preferred primary or authoritative claim.")
    conflicting_claims: List[VerifiedClaim] = Field(default_factory=list, description="Any competing or conflicting claims.")
    confidence: ConfidenceLevel = Field(default=ConfidenceLevel.LOW, description="Synthesis confidence level.")
    agreement_status: AgreementStatus = Field(default=AgreementStatus.UNVERIFIED_SINGLE_SOURCE, description="Consensus status.")
    corroboration_strength: CorroborationStrength = Field(default=CorroborationStrength.NONE, description="Corroboration tier.")
    qualifiers: List[str] = Field(default_factory=list, description="Preserved semantic qualifiers ('starting from', 'up to', etc.).")
    effective_date: Optional[str] = Field(default=None, description="Effective date if available.")
    caveats: List[str] = Field(default_factory=list, description="Disclaimers and underwriting uncertainty notes.")
    provenance_urls: List[str] = Field(default_factory=list, description="Canonical source URLs supporting this conclusion.")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize SynthesisConclusion to dictionary."""
        return {
            "topic": self.topic,
            "entity": self.entity,
            "product": self.product,
            "synthesized_statement": self.synthesized_statement,
            "preferred_claim": self.preferred_claim.to_dict() if self.preferred_claim else None,
            "conflicting_claims": [c.to_dict() for c in self.conflicting_claims],
            "confidence": self.confidence.value,
            "agreement_status": self.agreement_status.value,
            "corroboration_strength": self.corroboration_strength.value,
            "qualifiers": self.qualifiers,
            "effective_date": self.effective_date,
            "caveats": self.caveats,
            "provenance_urls": self.provenance_urls,
        }


class ResearchSynthesis(BaseModel):
    """
    Top-level multi-source research synthesis result.
    Synthesizes multiple sources, groups claims, verifies independence,
    evaluates consensus, resolves conflicts, and generates structured conclusions.
    """
    query: str = Field(..., description="Research query or comparison topic.")
    claim_groups: List[ResearchClaimGroup] = Field(default_factory=list, description="Identified claim groups.")
    agreements: List[ClaimAgreement] = Field(default_factory=list, description="Agreement analysis per claim group.")
    conflicts: List[ClaimConflict] = Field(default_factory=list, description="Detected claim conflicts across sources.")
    conclusions: List[SynthesisConclusion] = Field(default_factory=list, description="Synthesized takeaways per topic/entity.")
    sources_evaluated: List[SourceCredibility] = Field(default_factory=list, description="All sources evaluated.")
    overall_status: VerificationStatus = Field(default=VerificationStatus.UNVERIFIED, description="Aggregate verification status.")
    overall_confidence: ConfidenceLevel = Field(default=ConfidenceLevel.LOW, description="Aggregate synthesis confidence.")
    synthesis_summary: str = Field(default="", description="High-level narrative synthesis summary.")
    has_conflicts: bool = Field(default=False, description="True if any conflicting claims were detected.")
    has_unverified_claims: bool = Field(default=False, description="True if any unverified or low-confidence claims exist.")
    retrieved_at: str = Field(default_factory=current_utc_timestamp, description="UTC synthesis timestamp.")
    success: bool = Field(default=True, description="True if multi-source synthesis succeeded.")
    error: Optional[str] = Field(default=None, description="Error detail if synthesis failed.")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize ResearchSynthesis to dictionary."""
        return {
            "query": self.query,
            "overall_status": self.overall_status.value,
            "overall_confidence": self.overall_confidence.value,
            "synthesis_summary": self.synthesis_summary,
            "has_conflicts": self.has_conflicts,
            "has_unverified_claims": self.has_unverified_claims,
            "retrieved_at": self.retrieved_at,
            "success": self.success,
            "error": self.error,
            "conclusions": [c.to_dict() for c in self.conclusions],
            "conflicts": [cf.to_dict() for cf in self.conflicts],
            "agreements": [a.to_dict() for a in self.agreements],
            "claim_groups": [g.to_dict() for g in self.claim_groups],
            "sources_evaluated": [s.to_dict() for s in self.sources_evaluated],
        }



