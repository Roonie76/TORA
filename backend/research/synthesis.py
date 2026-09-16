import re
import logging
from typing import Optional, List, Dict, Any, Set, Tuple
from collections import defaultdict

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
    SourceRelationship,
    AgreementStatus,
    CorroborationStrength,
    ClaimSemanticSignature,
    ResearchClaimGroup,
    ClaimAgreement,
    SynthesisConclusion,
    ResearchSynthesis,
    current_utc_timestamp,
    extract_domain_from_url,
)
from .normalization import clean_text

logger = logging.getLogger("tora.research.synthesis")

# -----------------------------------------------------------------------------
# Institution and Product Extraction Dictionaries
# -----------------------------------------------------------------------------

KNOWN_ENTITIES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:sbi|state\s+bank\s+of\s+india)\b", re.IGNORECASE), "SBI"),
    (re.compile(r"\b(?:hdfc\s+bank|hdfc)\b", re.IGNORECASE), "HDFC Bank"),
    (re.compile(r"\b(?:icici\s+bank|icici)\b", re.IGNORECASE), "ICICI Bank"),
    (re.compile(r"\b(?:axis\s+bank|axis)\b", re.IGNORECASE), "Axis Bank"),
    (re.compile(r"\b(?:kotak\s+mahindra\s+bank|kotak\s+bank|kotak)\b", re.IGNORECASE), "Kotak Mahindra Bank"),
    (re.compile(r"\b(?:bank\s+of\s+baroda|bob)\b", re.IGNORECASE), "Bank of Baroda"),
    (re.compile(r"\b(?:punjab\s+national\s+bank|pnb)\b", re.IGNORECASE), "PNB"),
    (re.compile(r"\b(?:canara\s+bank)\b", re.IGNORECASE), "Canara Bank"),
    (re.compile(r"\b(?:union\s+bank\s+of\s+india|union\s+bank)\b", re.IGNORECASE), "Union Bank of India"),
    (re.compile(r"\b(?:rbi|reserve\s+bank\s+of\s+india)\b", re.IGNORECASE), "RBI"),
    (re.compile(r"\b(?:sebi|sebi\s+india)\b", re.IGNORECASE), "SEBI"),
    (re.compile(r"\b(?:income\s+tax\s+department|income\s+tax|incometax)\b", re.IGNORECASE), "Income Tax Department"),
    (re.compile(r"\b(?:irdai)\b", re.IGNORECASE), "IRDAI"),
    (re.compile(r"\b(?:pfrda)\b", re.IGNORECASE), "PFRDA"),
    (re.compile(r"\b(?:bajaj\s+finserv|bajaj\s+finance)\b", re.IGNORECASE), "Bajaj Finserv"),
    (re.compile(r"\b(?:lic\s+housing\s+finance|lic\s+hfl|lic)\b", re.IGNORECASE), "LIC Housing Finance"),
    (re.compile(r"\b(?:tata\s+capital)\b", re.IGNORECASE), "Tata Capital"),
]

KNOWN_PRODUCTS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:home\s+loans?|housing\s+loans?|mortgages?|housing\s+finances?)\b", re.IGNORECASE), "Home Loan"),
    (re.compile(r"\b(?:personal\s+loans?|express\s+credits?)\b", re.IGNORECASE), "Personal Loan"),
    (re.compile(r"\b(?:gold\s+loans?)\b", re.IGNORECASE), "Gold Loan"),
    (re.compile(r"\b(?:car\s+loans?|auto\s+loans?|vehicle\s+loans?)\b", re.IGNORECASE), "Car Loan"),
    (re.compile(r"\b(?:education\s+loans?|student\s+loans?)\b", re.IGNORECASE), "Education Loan"),
    (re.compile(r"\b(?:repo\s+rates?|policy\s+rates?|reverse\s+repos?)\b", re.IGNORECASE), "Repo Rate"),
    (re.compile(r"\b(?:fixed\s+deposits?|term\s+deposits?|fds?|fd\s+rates?)\b", re.IGNORECASE), "Fixed Deposit"),
    (re.compile(r"\b(?:credit\s+cards?|card\s+aprs?)\b", re.IGNORECASE), "Credit Card"),
    (re.compile(r"\b(?:tax\s+slabs?|tax\s+rates?|new\s+tax\s+regimes?|old\s+tax\s+regimes?)\b", re.IGNORECASE), "Tax Slabs"),
    (re.compile(r"\b(?:bank\s+loans?|general\s+loans?|loans?)\b", re.IGNORECASE), "Bank Loan"),
]

ATTRIBUTION_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(?:according\s+to|as\s+reported\s+by|citing|quotes|quoted\s+in|as\s+per|press\s+release\s+by|statement\s+from|sourced\s+from|per\s+pti)\s+([A-Za-z0-9\s\.\-]+)", re.IGNORECASE),
]


def extract_entity_from_claim(claim: VerifiedClaim) -> str:
    """Identify the institution/entity associated with a claim from text, domain, or title."""
    full_text = f"{claim.claim_text} {claim.supporting_text} {claim.source_title} {claim.source_domain}"
    for pat, name in KNOWN_ENTITIES:
        if pat.search(full_text):
            return name
    if "sbi" in claim.source_domain:
        return "SBI"
    if "hdfc" in claim.source_domain:
        return "HDFC Bank"
    if "icici" in claim.source_domain:
        return "ICICI Bank"
    if "rbi" in claim.source_domain:
        return "RBI"
    if "incometax" in claim.source_domain:
        return "Income Tax Department"

    # Macro products defaulting to primary authorities
    prod = extract_product_from_claim(claim)
    if prod == "Repo Rate":
        return "RBI"
    if prod == "Tax Slabs":
        return "Income Tax Department"

    return "General"


def extract_product_from_claim(claim: VerifiedClaim) -> str:
    """Identify the financial product associated with a claim."""
    full_text = f"{claim.claim_text} {claim.supporting_text} {claim.source_title}"
    for pat, prod in KNOWN_PRODUCTS:
        if pat.search(full_text):
            return prod
    if claim.financial_entities.get("rates"):
        return "Bank Loan"
    return "Financial Product"



def create_claim_signature(claim: VerifiedClaim) -> ClaimSemanticSignature:
    """Create a structured semantic key for grouping identical claims."""
    entity = extract_entity_from_claim(claim)
    product = extract_product_from_claim(claim)

    metric_type = "interest_rate"
    raw_value = ""

    rates = claim.financial_entities.get("rates", [])
    amounts = claim.financial_entities.get("amounts", [])

    if rates:
        metric_type = "interest_rate"
        raw_value = rates[0].lower().strip()
    elif amounts:
        metric_type = "amount"
        raw_value = amounts[0].lower().strip()
    else:
        metric_type = "statement"
        raw_value = claim.claim_text.lower().strip()[:50]

    return ClaimSemanticSignature(
        entity=entity,
        product=product,
        metric_type=metric_type,
        raw_value=raw_value,
        qualifiers=claim.qualifiers,
    )


class ResearchSynthesizer:
    """
    Deterministic Multi-Source Research Synthesis & Corroboration Engine.
    Coordinates claim semantic normalization, cross-source grouping,
    source independence verification, conflict resolution, and structured takeaways.
    """

    def __init__(self, max_sources: int = 5):
        self.max_sources = max_sources

    def evaluate_source_relationship(
        self,
        target_claim: VerifiedClaim,
        all_claims: List[VerifiedClaim],
    ) -> SourceRelationship:
        """
        Determine if a source is genuinely independent or derived/syndicated.
        """
        # Primary official sources are always independent
        if target_claim.source_credibility.is_primary:
            return SourceRelationship.INDEPENDENT

        supporting_text = target_claim.supporting_text.lower()

        # Check if secondary source explicitly attributes/quotes a primary or media source
        for pat in ATTRIBUTION_PATTERNS:
            match = pat.search(supporting_text)
            if match:
                return SourceRelationship.DERIVED

        # Check for near-identical duplicate supporting text across different domains (syndication)
        for other in all_claims:
            if other.source_domain != target_claim.source_domain:
                if target_claim.supporting_text and other.supporting_text:
                    if target_claim.supporting_text.strip() == other.supporting_text.strip():
                        return SourceRelationship.SYNDICATED

        if target_claim.source_credibility.authority_level in (AuthorityLevel.VERY_HIGH, AuthorityLevel.HIGH, AuthorityLevel.MEDIUM_HIGH):
            return SourceRelationship.INDEPENDENT

        return SourceRelationship.UNKNOWN

    def group_claims(self, claims: List[VerifiedClaim]) -> List[ResearchClaimGroup]:
        """
        Group semantically matching claims by Entity + Product + Rate/Value + Qualifiers.
        Prevents merging distinct banks or distinct products together.
        """
        groups_dict: Dict[str, List[VerifiedClaim]] = defaultdict(list)
        signatures: Dict[str, ClaimSemanticSignature] = {}

        for claim in claims:
            sig = create_claim_signature(claim)
            key = sig.signature_key()
            groups_dict[key].append(claim)
            signatures[key] = sig

        claim_groups: List[ResearchClaimGroup] = []
        for idx, (key, grouped_claims) in enumerate(groups_dict.items(), start=1):
            sig = signatures[key]
            first_claim = grouped_claims[0]

            # Highest authority in group
            highest_auth = max(
                (c.source_credibility.authority_level for c in grouped_claims),
                key=lambda a: self._authority_rank(a),
            )
            is_primary = any(c.source_credibility.is_primary for c in grouped_claims)

            # Deduplicate supporting sources
            sources_seen: Set[str] = set()
            supporting_sources: List[SourceCredibility] = []
            for c in grouped_claims:
                if c.source_domain not in sources_seen:
                    sources_seen.add(c.source_domain)
                    supporting_sources.append(c.source_credibility)

            # Representative statement
            quals_str = f" ({', '.join(sig.qualifiers)})" if sig.qualifiers else ""
            eff_date = next((c.effective_date for c in grouped_claims if c.effective_date), None)
            eff_str = f" [Effective: {eff_date}]" if eff_date else ""
            norm_text = f"{sig.entity} {sig.product}: {sig.raw_value}{quals_str}{eff_str}".strip()

            claim_groups.append(
                ResearchClaimGroup(
                    group_id=f"group_{idx}_{sig.entity.lower()}_{sig.product.lower()}",
                    normalized_claim_text=norm_text,
                    entity=sig.entity,
                    product=sig.product,
                    metric_type=sig.metric_type,
                    metric_value=sig.raw_value,
                    qualifiers=sig.qualifiers,
                    effective_date=eff_date,
                    supporting_claims=grouped_claims,
                    supporting_sources=supporting_sources,
                    is_primary_supported=is_primary,
                    highest_authority=highest_auth,
                )
            )

        return claim_groups

    def evaluate_agreement(
        self,
        group: ResearchClaimGroup,
        all_claims: List[VerifiedClaim],
    ) -> ClaimAgreement:
        """
        Evaluate consensus, independence, and corroboration strength for a claim group.
        """
        independent_count = 0
        derived_count = 0
        syndicated_count = 0

        for claim in group.supporting_claims:
            rel = self.evaluate_source_relationship(claim, all_claims)
            if rel == SourceRelationship.INDEPENDENT:
                independent_count += 1
            elif rel == SourceRelationship.DERIVED:
                derived_count += 1
            elif rel == SourceRelationship.SYNDICATED:
                syndicated_count += 1

        # Check if competing groups exist for the same entity and product
        competing = [
            c for c in all_claims
            if extract_entity_from_claim(c) == group.entity
            and extract_product_from_claim(c) == group.product
            and create_claim_signature(c).raw_value != group.metric_value
        ]

        if competing:
            status = AgreementStatus.CONFLICTING
            corroboration = CorroborationStrength.LOW if not group.is_primary_supported else CorroborationStrength.HIGH
            explanation = (
                f"Conflicting rates reported for {group.entity} {group.product}. "
                f"This figure ({group.metric_value}) is supported by {len(group.supporting_sources)} sources "
                f"({independent_count} independent), but competing reports exist."
            )
        elif group.is_primary_supported and independent_count >= 2:
            status = AgreementStatus.UNANIMOUS
            corroboration = CorroborationStrength.VERY_HIGH
            explanation = (
                f"Strong corroboration: Primary official source ({group.entity}) "
                f"confirmed alongside {independent_count - 1} independent source(s)."
            )
        elif group.is_primary_supported:
            status = AgreementStatus.UNANIMOUS
            corroboration = CorroborationStrength.HIGH
            explanation = f"Primary official confirmation from {group.entity} official portal."
        elif independent_count >= 2:
            status = AgreementStatus.MAJORITY_AGREEMENT
            corroboration = CorroborationStrength.MEDIUM
            explanation = f"Corroborated across {independent_count} independent secondary publications."
        elif len(group.supporting_claims) == 1:
            status = AgreementStatus.UNVERIFIED_SINGLE_SOURCE
            corroboration = CorroborationStrength.LOW
            explanation = "Single uncorroborated report from an external secondary source."
        else:
            status = AgreementStatus.MAJORITY_AGREEMENT
            corroboration = CorroborationStrength.LOW
            explanation = f"Supported by {derived_count + syndicated_count} derived or syndicated references."

        return ClaimAgreement(
            group_id=group.group_id,
            claim_text=group.normalized_claim_text,
            independent_source_count=independent_count,
            derived_source_count=derived_count,
            syndicated_source_count=syndicated_count,
            agreement_status=status,
            corroboration_strength=corroboration,
            explanation=explanation,
        )

    def detect_conflicts(self, claim_groups: List[ResearchClaimGroup]) -> List[ClaimConflict]:
        """
        Detect numerical or semantic divergence between claim groups for the same Entity and Product.
        Never averages figures and never silently discards secondary variants.
        """
        conflicts: List[ClaimConflict] = []
        entity_product_map: Dict[Tuple[str, str], List[ResearchClaimGroup]] = defaultdict(list)

        for g in claim_groups:
            entity_product_map[(g.entity, g.product)].append(g)

        for (entity, product), groups in entity_product_map.items():
            if len(groups) > 1:
                # Multiple divergent figures for same entity & product!
                all_conflicting_claims: List[VerifiedClaim] = []
                for grp in groups:
                    all_conflicting_claims.extend(grp.supporting_claims)

                # Prioritize primary official or highest credibility score
                sorted_claims = sorted(
                    all_conflicting_claims,
                    key=lambda c: (
                        1 if c.source_credibility.is_primary else 0,
                        self._authority_rank(c.source_credibility.authority_level),
                        c.source_credibility.credibility_score,
                    ),
                    reverse=True,
                )
                preferred = sorted_claims[0]
                rates = [g.metric_value for g in groups if g.metric_value]
                divergence = " vs ".join(rates)

                reason = (
                    f"Divergent figures detected for {entity} {product} ({divergence}). "
                    f"Preferred reference is '{preferred.source_title}' ({preferred.source_domain}) "
                    f"due to higher authority ({preferred.source_credibility.authority_level.value}). "
                    f"Secondary reports are preserved."
                )

                conflicts.append(
                    ClaimConflict(
                        claim_type=f"{entity}_{product}_rate",
                        conflicting_claims=all_conflicting_claims,
                        conflict_detected=True,
                        preferred_claim=preferred,
                        reason=reason,
                        divergence_detail=divergence,
                    )
                )

        return conflicts

    def generate_conclusions(
        self,
        claim_groups: List[ResearchClaimGroup],
        conflicts: List[ClaimConflict],
        agreements: List[ClaimAgreement],
    ) -> List[SynthesisConclusion]:
        """
        Construct structured, qualifier-preserved synthesis conclusions per entity/product topic.
        """
        conclusions: List[SynthesisConclusion] = []
        conflict_map: Dict[str, ClaimConflict] = {}
        for cf in conflicts:
            for c in cf.conflicting_claims:
                conflict_map[c.source_url] = cf

        for group in claim_groups:
            # Find matching agreement
            agr = next((a for a in agreements if a.group_id == group.group_id), None)
            agr_status = agr.agreement_status if agr else AgreementStatus.UNVERIFIED_SINGLE_SOURCE
            corrob_tier = agr.corroboration_strength if agr else CorroborationStrength.NONE

            # Find matching conflict if any
            matched_conflict = next(
                (cf for cf in conflicts if cf.preferred_claim and extract_entity_from_claim(cf.preferred_claim) == group.entity and extract_product_from_claim(cf.preferred_claim) == group.product),
                None,
            )

            preferred_claim = None
            conflicting_claims: List[VerifiedClaim] = []
            if matched_conflict:
                preferred_claim = matched_conflict.preferred_claim
                conflicting_claims = [c for c in matched_conflict.conflicting_claims if c != preferred_claim]
            elif group.supporting_claims:
                preferred_claim = group.supporting_claims[0]

            # Determine confidence
            confidence = ConfidenceLevel.LOW
            if group.is_primary_supported:
                confidence = ConfidenceLevel.VERY_HIGH if group.highest_authority == AuthorityLevel.VERY_HIGH else ConfidenceLevel.HIGH
            elif corrob_tier in (CorroborationStrength.VERY_HIGH, CorroborationStrength.HIGH):
                confidence = ConfidenceLevel.HIGH
            elif corrob_tier == CorroborationStrength.MEDIUM:
                confidence = ConfidenceLevel.MEDIUM
            else:
                confidence = ConfidenceLevel.LOW

            # Caveats & Disclaimers
            caveats: List[str] = [
                "Individual loan interest rates and fees are subject to lender underwriting, credit score, and eligibility.",
            ]
            if matched_conflict:
                caveats.append(f"Discrepancies found across reports ({matched_conflict.divergence_detail}). Primary source preferred.")
            if "starting from" in group.qualifiers or "start from" in group.qualifiers:
                caveats.append("Advertised rates are starting baseline figures and not guaranteed for every borrower.")

            quals_display = f" starting from" if any(q in group.qualifiers for q in ("starting from", "start from")) else ""
            statement = (
                f"{group.entity} official rate for {group.product} is{quals_display} {group.metric_value}."
                if group.is_primary_supported
                else f"Retrieved sources report {group.entity} {group.product} at{quals_display} {group.metric_value}."
            )

            provenance_urls = list({c.source_url for c in group.supporting_claims})

            conclusions.append(
                SynthesisConclusion(
                    topic=f"{group.entity} {group.product}",
                    entity=group.entity,
                    product=group.product,
                    synthesized_statement=statement,
                    preferred_claim=preferred_claim,
                    conflicting_claims=conflicting_claims,
                    confidence=confidence,
                    agreement_status=agr_status,
                    corroboration_strength=corrob_tier,
                    qualifiers=group.qualifiers,
                    effective_date=group.effective_date,
                    caveats=caveats,
                    provenance_urls=provenance_urls,
                )
            )

        return conclusions

    def synthesize(
        self,
        query: str,
        verified_evidences: List[VerifiedResearchEvidence],
    ) -> ResearchSynthesis:
        """
        Execute full multi-source synthesis, claim grouping, corroboration, and conflict detection.
        """
        all_claims: List[VerifiedClaim] = []
        all_sources: List[SourceCredibility] = []
        sources_seen: Set[str] = set()

        for ve in verified_evidences:
            if not ve.success:
                continue
            all_claims.extend(ve.verified_claims)
            for src in ve.sources:
                if src.domain not in sources_seen:
                    sources_seen.add(src.domain)
                    all_sources.append(src)

        if not all_claims:
            return ResearchSynthesis(
                query=query,
                claim_groups=[],
                agreements=[],
                conflicts=[],
                conclusions=[],
                sources_evaluated=all_sources,
                overall_status=VerificationStatus.INSUFFICIENT_EVIDENCE,
                overall_confidence=ConfidenceLevel.VERY_LOW,
                synthesis_summary="No verifiable financial claims were extracted from the retrieved sources.",
                has_conflicts=False,
                has_unverified_claims=True,
                success=True,
                error=None,
            )

        claim_groups = self.group_claims(all_claims)
        agreements = [self.evaluate_agreement(g, all_claims) for g in claim_groups]
        conflicts = self.detect_conflicts(claim_groups)
        conclusions = self.generate_conclusions(claim_groups, conflicts, agreements)

        has_conflicts = len(conflicts) > 0
        has_primary = any(g.is_primary_supported for g in claim_groups)

        overall_status = VerificationStatus.UNVERIFIED
        if has_conflicts:
            overall_status = VerificationStatus.CONFLICTING
        elif has_primary:
            overall_status = VerificationStatus.VERIFIED_PRIMARY
        elif any(a.corroboration_strength in (CorroborationStrength.HIGH, CorroborationStrength.MEDIUM) for a in agreements):
            overall_status = VerificationStatus.CORROBORATED
        else:
            overall_status = VerificationStatus.VERIFIED_SECONDARY

        overall_conf = ConfidenceLevel.LOW
        if conclusions:
            overall_conf = max((c.confidence for c in conclusions), key=lambda c: self._confidence_rank(c))

        # Generate summary narrative
        summary_lines = []
        for conc in conclusions:
            summary_lines.append(f"• {conc.synthesized_statement} (Confidence: {conc.confidence.value})")
        if conflicts:
            summary_lines.append(f"⚠️ Conflict Note: {conflicts[0].reason}")

        summary_text = "\n".join(summary_lines)

        return ResearchSynthesis(
            query=query,
            claim_groups=claim_groups,
            agreements=agreements,
            conflicts=conflicts,
            conclusions=conclusions,
            sources_evaluated=all_sources,
            overall_status=overall_status,
            overall_confidence=overall_conf,
            synthesis_summary=summary_text,
            has_conflicts=has_conflicts,
            has_unverified_claims=any(c.confidence in (ConfidenceLevel.LOW, ConfidenceLevel.VERY_LOW) for c in conclusions),
            success=True,
            error=None,
        )

    def _authority_rank(self, level: AuthorityLevel) -> int:
        ranks = {
            AuthorityLevel.VERY_HIGH: 6,
            AuthorityLevel.HIGH: 5,
            AuthorityLevel.MEDIUM_HIGH: 4,
            AuthorityLevel.MEDIUM: 3,
            AuthorityLevel.LOW: 2,
            AuthorityLevel.VERY_LOW: 1,
        }
        return ranks.get(level, 0)

    def _confidence_rank(self, level: ConfidenceLevel) -> int:
        ranks = {
            ConfidenceLevel.VERY_HIGH: 5,
            ConfidenceLevel.HIGH: 4,
            ConfidenceLevel.MEDIUM: 3,
            ConfidenceLevel.LOW: 2,
            ConfidenceLevel.VERY_LOW: 1,
        }
        return ranks.get(level, 0)
