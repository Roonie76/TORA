import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Set, Tuple
from urllib.parse import urlparse

from .models import (
    SourceType,
    AuthorityLevel,
    VerificationStatus,
    FreshnessStatus,
    ConfidenceLevel,
    SourceCredibility,
    VerifiedClaim,
    ClaimConflict,
    VerifiedResearchEvidence,
    ExtractedClaim,
    ResearchEvidence,
    FetchedPage,
    current_utc_timestamp,
    extract_domain_from_url,
)
from .normalization import normalize_url, clean_text

logger = logging.getLogger("tora.research.credibility")

# -----------------------------------------------------------------------------
# Official Domain Registries (Indian & Global Finance)
# -----------------------------------------------------------------------------

REGULATOR_DOMAINS: Set[str] = {
    "rbi.org.in",
    "sebi.gov.in",
    "irdai.gov.in",
    "pfrda.org.in",
    "ibbi.gov.in",
    "npci.org.in",
    "sidbi.in",
    "nabard.org",
    "nhb.org.in",
}

GOVERNMENT_DOMAINS: Set[str] = {
    "incometax.gov.in",
    "incometaxindia.gov.in",
    "finmin.nic.in",
    "dea.gov.in",
    "uidai.gov.in",
    "gst.gov.in",
    "mca.gov.in",
    "epfindia.gov.in",
}

GOVERNMENT_SUFFIXES: Tuple[str, ...] = (
    ".gov.in",
    ".nic.in",
    ".gov",
)

OFFICIAL_BANK_DOMAINS: Set[str] = {
    "sbi.co.in",
    "sbi.bank.in",
    "homeloans.sbi.bank.in",
    "bank.sbi",
    "hdfcbank.com",
    "icicibank.com",
    "axisbank.com",
    "kotak.com",
    "bankofbaroda.in",
    "pnbindia.in",
    "canarabank.com",
    "unionbankofindia.co.in",
    "indusind.com",
    "idfcfirstbank.com",
    "yesbank.in",
    "federalbank.co.in",
    "rblbank.com",
    "bandhanbank.com",
    "aubank.in",
    "bajajfinserv.in",
    "tatafinancial.com",
    "licindia.in",
    "lichousing.com",
    "hdfc.com",
    "icicidirect.com",
}

OFFICIAL_BANK_SUFFIXES: Tuple[str, ...] = (
    ".bank.in",
    ".bank",
)

ESTABLISHED_MEDIA_DOMAINS: Set[str] = {
    "moneycontrol.com",
    "economictimes.indiatimes.com",
    "livemint.com",
    "business-standard.com",
    "financialexpress.com",
    "thehindubusinessline.com",
    "bloomberg.com",
    "reuters.com",
    "cnbctv18.com",
    "ndtvprofit.com",
    "zeebiz.com",
    "wsj.com",
    "ft.com",
}

SECONDARY_AGGREGATOR_DOMAINS: Set[str] = {
    "bankbazaar.com",
    "paisabazaar.com",
    "cleartax.in",
    "policybazaar.com",
    "cred.club",
    "wishfin.com",
    "mymoneymantra.com",
    "valueresearchonline.com",
    "groww.in",
    "zerodha.com",
}

UNVERIFIED_FORUM_DOMAINS: Set[str] = {
    "reddit.com",
    "quora.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "medium.com",
    "substack.com",
    "blogspot.com",
    "wordpress.com",
}


def matches_domain(domain: str, target: str) -> bool:
    """
    Check if a domain exactly matches or is a valid subdomain of target.
    Prevents lookalikes (e.g. 'rbi-scam.com' will not match 'rbi.org.in').
    """
    if not domain or not target:
        return False
    d = domain.lower().strip()
    t = target.lower().strip()
    return d == t or d.endswith("." + t)


def check_lookalike_domain(domain: str) -> Optional[str]:
    """
    Detect suspicious lookalike or typosquatted domains pretending to be official regulators or banks.
    """
    if not domain:
        return None
    d = domain.lower().strip()
    suspicious_keywords = ["rbi", "sebi", "incometax", "sbi", "hdfc", "icici", "kotak", "axisbank"]

    for kw in suspicious_keywords:
        if kw in d:
            # Check if it actually belongs to the verified official list
            is_verified = (
                any(matches_domain(d, reg) for reg in REGULATOR_DOMAINS)
                or any(matches_domain(d, gov) for gov in GOVERNMENT_DOMAINS)
                or any(matches_domain(d, bnk) for bnk in OFFICIAL_BANK_DOMAINS)
                or d.endswith(GOVERNMENT_SUFFIXES)
                or d.endswith(OFFICIAL_BANK_SUFFIXES)
                or any(matches_domain(d, med) for med in ESTABLISHED_MEDIA_DOMAINS)
                or any(matches_domain(d, agg) for agg in SECONDARY_AGGREGATOR_DOMAINS)
            )
            if not is_verified:
                return f"Domain contains official keyword '{kw}' but is not an authorized official domain."
    return None


def parse_date_string(date_str: Optional[str]) -> Optional[datetime]:
    """
    Safely parse date strings into timezone-aware UTC datetime.
    Supports ISO-8601, YYYY-MM-DD, DD.MM.YYYY, DD-MM-YYYY, DD/MM/YYYY.
    """
    if not date_str or not isinstance(date_str, str):
        return None

    cleaned = date_str.strip()
    if not cleaned:
        return None

    # Try ISO-8601 first
    try:
        dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass

    # Try standard date patterns
    date_formats = [
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%B %d, %Y",
        "%d %B %Y",
        "%Y/%m/%d",
    ]
    for fmt in date_formats:
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

    return None


def evaluate_freshness(publication_date: Optional[str], reference_dt: Optional[datetime] = None) -> Tuple[FreshnessStatus, List[str]]:
    """
    Evaluate the freshness of a publication date.
    Strictly decouples publication date from retrieval timestamp.
    """
    reasons: List[str] = []
    if not publication_date:
        return FreshnessStatus.UNKNOWN, ["Publication date is unknown; cannot verify freshness."]

    dt = parse_date_string(publication_date)
    if dt is None:
        return FreshnessStatus.UNKNOWN, [f"Unparseable publication date format: '{publication_date}'."]

    now = reference_dt or datetime.now(timezone.utc)
    age = now - dt

    if age < timedelta(days=0):
        # Future publication date (e.g. w.e.f. effective future date)
        return FreshnessStatus.CURRENT, [f"Effective / publication date is current or upcoming ({publication_date})."]

    if age <= timedelta(days=30):
        return FreshnessStatus.CURRENT, [f"Published within the last 30 days ({publication_date})."]
    elif age <= timedelta(days=180):
        return FreshnessStatus.RECENT, [f"Published within the last 6 months ({publication_date})."]
    elif age <= timedelta(days=365):
        return FreshnessStatus.AGING, [f"Published between 6 to 12 months ago ({publication_date})."]
    else:
        return FreshnessStatus.STALE, [f"Published over 1 year ago ({publication_date}); rate may be outdated."]


class SourceVerifier:
    """
    Deterministic Source Verification & Credibility Engine.
    Classifies sources into explicit authority tiers, scores trustworthiness,
    evaluates freshness, validates claims against qualifiers, and detects conflicts.
    """

    def __init__(self):
        pass

    def evaluate_source(
        self,
        url: str,
        title: str = "Untitled",
        publication_date: Optional[str] = None,
        retrieved_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SourceCredibility:
        """
        Evaluate source authority, credibility score, and verification eligibility.
        """
        canonical = normalize_url(url)
        domain = extract_domain_from_url(canonical)
        reasons: List[str] = []
        warnings: List[str] = []

        is_https = canonical.startswith("https://")
        if not is_https:
            warnings.append("Insecure HTTP transport (missing HTTPS).")

        # 1. Lookalike Domain Check
        lookalike_warning = check_lookalike_domain(domain)
        if lookalike_warning:
            warnings.append(lookalike_warning)

        # 2. Source Classification & Authority Tiering
        source_type = SourceType.UNKNOWN
        authority = AuthorityLevel.LOW
        score = 0.40
        is_primary = False
        is_official = False

        if any(matches_domain(domain, reg) for reg in REGULATOR_DOMAINS):
            source_type = SourceType.REGULATOR
            authority = AuthorityLevel.VERY_HIGH
            score = 0.98
            is_primary = True
            is_official = True
            reasons.append(f"Official financial regulatory authority ({domain}).")

        elif any(matches_domain(domain, gov) for gov in GOVERNMENT_DOMAINS) or domain.endswith(GOVERNMENT_SUFFIXES):
            source_type = SourceType.GOVERNMENT
            authority = AuthorityLevel.VERY_HIGH
            score = 0.96
            is_primary = True
            is_official = True
            reasons.append(f"Official government domain ({domain}).")

        elif any(matches_domain(domain, bnk) for bnk in OFFICIAL_BANK_DOMAINS) or domain.endswith(OFFICIAL_BANK_SUFFIXES):
            source_type = SourceType.BANK_NBFC
            authority = AuthorityLevel.HIGH
            score = 0.90
            is_primary = True
            is_official = True
            reasons.append(f"Official regulated commercial bank / lender ({domain}).")

        elif any(matches_domain(domain, med) for med in ESTABLISHED_MEDIA_DOMAINS):
            source_type = SourceType.ESTABLISHED_FINANCIAL_MEDIA
            authority = AuthorityLevel.MEDIUM_HIGH
            score = 0.78
            is_primary = False
            is_official = False
            reasons.append(f"Established tier-1 financial publication ({domain}).")

        elif any(matches_domain(domain, agg) for agg in SECONDARY_AGGREGATOR_DOMAINS):
            source_type = SourceType.SECONDARY_AGGREGATOR
            authority = AuthorityLevel.MEDIUM
            score = 0.60
            is_primary = False
            is_official = False
            reasons.append(f"Secondary financial aggregator / comparison portal ({domain}).")

        elif any(matches_domain(domain, frm) for frm in UNVERIFIED_FORUM_DOMAINS):
            source_type = SourceType.UNVERIFIED_FORUM
            authority = AuthorityLevel.VERY_LOW
            score = 0.15
            is_primary = False
            is_official = False
            warnings.append(f"User-generated or unverified public forum ({domain}).")

        else:
            source_type = SourceType.GENERIC_BLOG
            authority = AuthorityLevel.LOW
            score = 0.35
            is_primary = False
            is_official = False
            warnings.append(f"Unverified external source / general web domain ({domain}).")

        # Penalize score if HTTP or lookalike
        if not is_https:
            score = max(0.05, score - 0.15)
        if lookalike_warning:
            authority = AuthorityLevel.VERY_LOW
            source_type = SourceType.GENERIC_BLOG
            score = 0.10
            is_primary = False
            is_official = False

        # 3. Freshness Assessment
        freshness, freshness_reasons = evaluate_freshness(publication_date)
        reasons.extend(freshness_reasons)

        if freshness == FreshnessStatus.STALE:
            score = max(0.10, score - 0.20)
            warnings.append("Information is over 1 year old and may be stale.")

        # 4. Verification Status
        if lookalike_warning:
            verification_status = VerificationStatus.REJECTED
        elif freshness == FreshnessStatus.STALE:
            verification_status = VerificationStatus.STALE
        elif is_primary and authority in (AuthorityLevel.VERY_HIGH, AuthorityLevel.HIGH):
            verification_status = VerificationStatus.VERIFIED_PRIMARY
        elif authority in (AuthorityLevel.MEDIUM_HIGH, AuthorityLevel.MEDIUM):
            verification_status = VerificationStatus.VERIFIED_SECONDARY
        else:
            verification_status = VerificationStatus.UNVERIFIED

        is_financial_claim_suitable = (
            verification_status in (VerificationStatus.VERIFIED_PRIMARY, VerificationStatus.VERIFIED_SECONDARY)
            and authority in (AuthorityLevel.VERY_HIGH, AuthorityLevel.HIGH, AuthorityLevel.MEDIUM_HIGH, AuthorityLevel.MEDIUM)
        )

        return SourceCredibility(
            url=url,
            canonical_url=canonical,
            domain=domain,
            source_title=title or "Untitled",
            source_type=source_type,
            authority_level=authority,
            credibility_score=round(score, 2),
            verification_status=verification_status,
            freshness=freshness,
            retrieval_timestamp=retrieved_at or current_utc_timestamp(),
            publication_date=publication_date,
            reasons=reasons,
            warnings=warnings,
            is_primary=is_primary,
            is_official=is_official,
            is_financial_claim_suitable=is_financial_claim_suitable,
        )

    def verify_claim(
        self,
        claim: ExtractedClaim,
        credibility: Optional[SourceCredibility] = None,
    ) -> VerifiedClaim:
        """
        Verify an ExtractedClaim against its source credibility and preserve exact qualifiers.
        """
        cred = credibility or self.evaluate_source(
            url=claim.source_url,
            title=claim.source_title,
            retrieved_at=claim.retrieved_at,
        )

        # Extract preserved qualifiers from financial entities or claim text
        qualifiers: List[str] = []
        if claim.financial_entities and "qualifiers" in claim.financial_entities:
            qualifiers = list(claim.financial_entities["qualifiers"])

        # Determine confidence level based on source authority and freshness
        confidence = ConfidenceLevel.LOW
        if cred.verification_status == VerificationStatus.VERIFIED_PRIMARY:
            confidence = ConfidenceLevel.VERY_HIGH if cred.authority_level == AuthorityLevel.VERY_HIGH else ConfidenceLevel.HIGH
        elif cred.verification_status == VerificationStatus.VERIFIED_SECONDARY:
            confidence = ConfidenceLevel.MEDIUM
        elif cred.verification_status == VerificationStatus.STALE:
            confidence = ConfidenceLevel.LOW
        elif cred.verification_status == VerificationStatus.REJECTED:
            confidence = ConfidenceLevel.VERY_LOW
        else:
            confidence = ConfidenceLevel.LOW

        # Extract effective date if mentioned in supporting text
        effective_date_match = re.search(r"\b(?:effective\s+from|w\.?e\.?f\.?|as\s+on|as\s+of)\s+([0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}|[A-Za-z]+\s+[0-9]{1,2},?\s+[0-9]{4})\b", claim.supporting_text, re.IGNORECASE)
        effective_date = effective_date_match.group(0) if effective_date_match else None

        return VerifiedClaim(
            claim_text=claim.claim_text,
            supporting_text=claim.supporting_text,
            source_url=claim.source_url,
            source_title=claim.source_title,
            source_domain=claim.source_domain,
            source_credibility=cred,
            verification_status=cred.verification_status,
            confidence=confidence,
            qualifiers=qualifiers,
            financial_entities=claim.financial_entities,
            effective_date=effective_date,
            retrieved_at=claim.retrieved_at,
        )

    def detect_conflicts(self, verified_claims: List[VerifiedClaim]) -> List[ClaimConflict]:
        """
        Detect discrepancies or contradictions between multiple verified claims.
        Maintains all conflicting variants without silently dropping secondary claims.
        """
        if len(verified_claims) < 2:
            return []

        conflicts: List[ClaimConflict] = []
        rate_claims: List[Tuple[str, VerifiedClaim]] = []

        for vc in verified_claims:
            rates = vc.financial_entities.get("rates", [])
            for r in rates:
                rate_claims.append((r.lower().strip(), vc))

        # Check if distinct rates exist
        distinct_rates = list({r[0] for r in rate_claims})
        if len(distinct_rates) > 1:
            # Conflict detected!
            claims_involved = [r[1] for r in rate_claims]

            # Choose preferred claim based on highest credibility score
            claims_involved_sorted = sorted(
                claims_involved,
                key=lambda c: (
                    1 if c.source_credibility.is_primary else 0,
                    c.source_credibility.credibility_score,
                ),
                reverse=True,
            )
            preferred = claims_involved_sorted[0]

            divergence = " vs ".join(distinct_rates)
            conflict_reason = (
                f"Multiple sources report divergent rates ({divergence}). "
                f"Preferred source is '{preferred.source_title}' ({preferred.source_domain}) "
                f"due to higher authority tier ({preferred.source_credibility.authority_level.value})."
            )

            conflicts.append(
                ClaimConflict(
                    claim_type="interest_rate",
                    conflicting_claims=claims_involved,
                    conflict_detected=True,
                    preferred_claim=preferred,
                    reason=conflict_reason,
                    divergence_detail=divergence,
                )
            )

        return conflicts

    def verify_evidence(self, evidence: ResearchEvidence) -> VerifiedResearchEvidence:
        """
        Perform full verification, credibility evaluation, and conflict detection on ResearchEvidence.
        """
        if not evidence.success or not evidence.claims:
            source_cred = self.evaluate_source(url=evidence.source_url, title=evidence.source_title)
            return VerifiedResearchEvidence(
                query=evidence.query,
                sources=[source_cred],
                verified_claims=[],
                conflicts=[],
                overall_verification_status=VerificationStatus.INSUFFICIENT_EVIDENCE if evidence.success else VerificationStatus.REJECTED,
                overall_confidence=ConfidenceLevel.VERY_LOW,
                retrieved_at=evidence.retrieved_at,
                success=evidence.success,
                error=evidence.error,
            )

        # Evaluate source credibility
        source_cred = self.evaluate_source(
            url=evidence.source_url,
            title=evidence.source_title,
            retrieved_at=evidence.retrieved_at,
        )

        verified_claims = [self.verify_claim(c, credibility=source_cred) for c in evidence.claims]
        conflicts = self.detect_conflicts(verified_claims)

        overall_status = source_cred.verification_status
        if conflicts:
            overall_status = VerificationStatus.CONFLICTING

        overall_confidence = ConfidenceLevel.LOW
        if verified_claims:
            overall_confidence = verified_claims[0].confidence

        return VerifiedResearchEvidence(
            query=evidence.query,
            sources=[source_cred],
            verified_claims=verified_claims,
            conflicts=conflicts,
            overall_verification_status=overall_status,
            overall_confidence=overall_confidence,
            retrieved_at=evidence.retrieved_at,
            success=True,
            error=None,
        )
