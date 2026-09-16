import unittest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta

from backend.research.models import (
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
    ExtractedClaim,
)
from backend.research.synthesis import (
    ResearchSynthesizer,
    create_claim_signature,
    extract_entity_from_claim,
    extract_product_from_claim,
)
from backend.research.credibility import SourceVerifier
from backend.research.provider import CompositeResearchProvider
from backend.research.factory import get_research_provider
from backend.context.builder import ContextBuilder
from backend.context.tools import ToolContext


def make_verified_claim(
    claim_text: str,
    domain: str,
    source_type: SourceType = SourceType.BANK_NBFC,
    authority: AuthorityLevel = AuthorityLevel.HIGH,
    score: float = 0.90,
    is_primary: bool = True,
    qualifiers: list = None,
    rates: list = None,
    amounts: list = None,
    supporting_text: str = "",
    effective_date: str = None,
    publication_date: str = None,
) -> VerifiedClaim:
    cred = SourceCredibility(
        url=f"https://{domain}/info",
        canonical_url=f"https://{domain}/info",
        domain=domain,
        source_title=f"{domain} Title",
        source_type=source_type,
        authority_level=authority,
        credibility_score=score,
        verification_status=VerificationStatus.VERIFIED_PRIMARY if is_primary else VerificationStatus.VERIFIED_SECONDARY,
        freshness=FreshnessStatus.CURRENT if publication_date else FreshnessStatus.UNKNOWN,
        publication_date=publication_date,
        is_primary=is_primary,
        is_official=is_primary,
        is_financial_claim_suitable=True,
    )
    entities = {}
    if rates:
        entities["rates"] = rates
    if amounts:
        entities["amounts"] = amounts
    return VerifiedClaim(
        claim_text=claim_text,
        supporting_text=supporting_text or claim_text,
        source_url=f"https://{domain}/info",
        source_title=f"{domain} Title",
        source_domain=domain,
        source_credibility=cred,
        verification_status=VerificationStatus.VERIFIED_PRIMARY if is_primary else VerificationStatus.VERIFIED_SECONDARY,
        confidence=ConfidenceLevel.HIGH if is_primary else ConfidenceLevel.MEDIUM,
        qualifiers=qualifiers or [],
        financial_entities=entities,
        effective_date=effective_date,
    )


class TestResearchSynthesisAndCorroboration(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.synthesizer = ResearchSynthesizer(max_sources=5)
        self.verifier = SourceVerifier()

    # -------------------------------------------------------------------------
    # 1. Claim Grouping Tests
    # -------------------------------------------------------------------------

    def test_01_identical_claims_group_together(self):
        c1 = make_verified_claim("SBI home loan starts from 7.25% p.a.", "sbi.bank.in", rates=["7.25% p.a."], qualifiers=["starts from"])
        c2 = make_verified_claim("SBI home loans start at 7.25% p.a.", "sbi.co.in", rates=["7.25% p.a."], qualifiers=["starts from"])
        groups = self.synthesizer.group_claims([c1, c2])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].entity, "SBI")
        self.assertEqual(groups[0].product, "Home Loan")
        self.assertEqual(len(groups[0].supporting_claims), 2)

    def test_02_equivalent_wording_groups_together(self):
        c1 = make_verified_claim("State Bank of India housing loan starting from 7.25%", "sbi.bank.in", rates=["7.25%"], qualifiers=["starting from"])
        c2 = make_verified_claim("SBI mortgage interest rate starting from 7.25%", "moneycontrol.com", source_type=SourceType.ESTABLISHED_FINANCIAL_MEDIA, is_primary=False, rates=["7.25%"], qualifiers=["starting from"])
        groups = self.synthesizer.group_claims([c1, c2])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].entity, "SBI")
        self.assertEqual(groups[0].product, "Home Loan")

    def test_03_different_rates_for_same_bank_remain_distinct_groups(self):
        c1 = make_verified_claim("SBI home loan starts from 7.25% p.a.", "sbi.bank.in", rates=["7.25% p.a."])
        c2 = make_verified_claim("SBI home loan is 8.10% p.a.", "moneycontrol.com", is_primary=False, rates=["8.10% p.a."])
        groups = self.synthesizer.group_claims([c1, c2])
        self.assertEqual(len(groups), 2)

    def test_04_different_banks_remain_separate_even_with_identical_rates(self):
        c_sbi = make_verified_claim("SBI home loans start from 7.25% p.a.", "sbi.bank.in", rates=["7.25% p.a."])
        c_hdfc = make_verified_claim("HDFC Bank home loans start from 7.25% p.a.", "hdfcbank.com", rates=["7.25% p.a."])
        groups = self.synthesizer.group_claims([c_sbi, c_hdfc])
        self.assertEqual(len(groups), 2)
        entities = {g.entity for g in groups}
        self.assertEqual(entities, {"SBI", "HDFC Bank"})

    def test_05_different_products_remain_separate(self):
        c_home = make_verified_claim("SBI home loan rate starts from 7.25%", "sbi.bank.in", rates=["7.25%"])
        c_gold = make_verified_claim("SBI gold loan rate starts from 7.25%", "sbi.bank.in", rates=["7.25%"])
        groups = self.synthesizer.group_claims([c_home, c_gold])
        self.assertEqual(len(groups), 2)
        products = {g.product for g in groups}
        self.assertEqual(products, {"Home Loan", "Gold Loan"})

    def test_06_different_effective_dates_preserved_in_groups(self):
        c1 = make_verified_claim("SBI home loan rate 7.25%", "sbi.bank.in", rates=["7.25%"], effective_date="01.04.2026")
        groups = self.synthesizer.group_claims([c1])
        self.assertEqual(groups[0].effective_date, "01.04.2026")

    def test_07_qualifiers_preserved_in_group_signatures(self):
        c1 = make_verified_claim("SBI home loans starting from 7.25%", "sbi.bank.in", rates=["7.25%"], qualifiers=["starting from", "up to"])
        groups = self.synthesizer.group_claims([c1])
        self.assertIn("starting from", groups[0].qualifiers)
        self.assertIn("up to", groups[0].qualifiers)

    # -------------------------------------------------------------------------
    # 2. Source Independence & Corroboration Tests
    # -------------------------------------------------------------------------

    def test_08_two_independent_sources_produce_strong_corroboration(self):
        c_primary = make_verified_claim("SBI home loan starts from 7.25%", "sbi.bank.in", rates=["7.25%"], is_primary=True)
        c_media = make_verified_claim("SBI home loan rate at 7.25%", "livemint.com", source_type=SourceType.ESTABLISHED_FINANCIAL_MEDIA, authority=AuthorityLevel.MEDIUM_HIGH, is_primary=False, rates=["7.25%"])
        agr = self.synthesizer.evaluate_agreement(
            self.synthesizer.group_claims([c_primary, c_media])[0],
            [c_primary, c_media],
        )
        self.assertEqual(agr.agreement_status, AgreementStatus.UNANIMOUS)
        self.assertEqual(agr.corroboration_strength, CorroborationStrength.VERY_HIGH)
        self.assertGreaterEqual(agr.independent_source_count, 2)

    def test_09_derived_quoted_source_detected_and_not_counted_as_independent(self):
        c_primary = make_verified_claim("RBI repo rate is 6.50%", "rbi.org.in", source_type=SourceType.REGULATOR, authority=AuthorityLevel.VERY_HIGH, is_primary=True, rates=["6.50%"])
        c_media = make_verified_claim("RBI repo rate remains 6.50%", "moneycontrol.com", source_type=SourceType.ESTABLISHED_FINANCIAL_MEDIA, authority=AuthorityLevel.MEDIUM_HIGH, is_primary=False, rates=["6.50%"], supporting_text="According to RBI press release, the repo rate remains 6.50%.")
        rel = self.synthesizer.evaluate_source_relationship(c_media, [c_primary, c_media])
        self.assertEqual(rel, SourceRelationship.DERIVED)

    def test_10_five_syndicated_copies_do_not_count_as_five_independent(self):
        identical_text = "PTI Wire: SBI announced home loan rate starting at 7.25% effective this quarter."
        c1 = make_verified_claim("SBI home loan 7.25%", "site1.com", source_type=SourceType.GENERIC_BLOG, authority=AuthorityLevel.LOW, is_primary=False, rates=["7.25%"], supporting_text=identical_text)
        c2 = make_verified_claim("SBI home loan 7.25%", "site2.com", source_type=SourceType.GENERIC_BLOG, authority=AuthorityLevel.LOW, is_primary=False, rates=["7.25%"], supporting_text=identical_text)
        c3 = make_verified_claim("SBI home loan 7.25%", "site3.com", source_type=SourceType.GENERIC_BLOG, authority=AuthorityLevel.LOW, is_primary=False, rates=["7.25%"], supporting_text=identical_text)
        group = self.synthesizer.group_claims([c1, c2, c3])[0]
        agr = self.synthesizer.evaluate_agreement(group, [c1, c2, c3])
        # Should detect syndication and not grant VERY_HIGH independent confirmation
        self.assertNotEqual(agr.corroboration_strength, CorroborationStrength.VERY_HIGH)
        self.assertGreaterEqual(agr.syndicated_source_count, 1)

    # -------------------------------------------------------------------------
    # 3. Conflict Detection Tests (Never average, Never silently delete)
    # -------------------------------------------------------------------------

    def test_11_primary_vs_secondary_conflict_detected_and_preferred_source_selected(self):
        c_sbi = make_verified_claim("SBI home loan starts from 7.25% p.a.", "sbi.bank.in", rates=["7.25% p.a."], is_primary=True, authority=AuthorityLevel.HIGH)
        c_media = make_verified_claim("SBI home loan rate is 8.10% p.a.", "moneycontrol.com", rates=["8.10% p.a."], is_primary=False, authority=AuthorityLevel.MEDIUM_HIGH)
        groups = self.synthesizer.group_claims([c_sbi, c_media])
        conflicts = self.synthesizer.detect_conflicts(groups)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].preferred_claim.source_domain, "sbi.bank.in")
        self.assertIn("7.25% p.a. vs 8.10% p.a.", conflicts[0].divergence_detail)
        # Verify ALL claims preserved
        self.assertEqual(len(conflicts[0].conflicting_claims), 2)

    def test_12_never_average_conflicting_rates(self):
        c1 = make_verified_claim("SBI home loan rate 7.25%", "sbi.bank.in", rates=["7.25%"])
        c2 = make_verified_claim("SBI home loan rate 8.10%", "moneycontrol.com", rates=["8.10%"])
        c3 = make_verified_claim("SBI home loan rate 6.50%", "unknown-blog.com", rates=["6.50%"])
        groups = self.synthesizer.group_claims([c1, c2, c3])
        conflicts = self.synthesizer.detect_conflicts(groups)
        self.assertEqual(len(conflicts), 1)
        # Check that no averaged rate like '7.28%' or '7.3%' was manufactured
        for grp in groups:
            self.assertIn(grp.metric_value, ["7.25%", "8.10%", "6.50%"])

    def test_13_unresolved_conflict_remains_explicit_in_synthesis(self):
        c1 = make_verified_claim("Bank loan rate 8.00%", "siteA.com", source_type=SourceType.GENERIC_BLOG, authority=AuthorityLevel.LOW, is_primary=False, rates=["8.00%"])
        c2 = make_verified_claim("Bank loan rate 9.00%", "siteB.com", source_type=SourceType.GENERIC_BLOG, authority=AuthorityLevel.LOW, is_primary=False, rates=["9.00%"])
        ev1 = VerifiedResearchEvidence(query="test", sources=[c1.source_credibility], verified_claims=[c1], success=True)
        ev2 = VerifiedResearchEvidence(query="test", sources=[c2.source_credibility], verified_claims=[c2], success=True)
        synth = self.synthesizer.synthesize("test", [ev1, ev2])
        self.assertTrue(synth.has_conflicts)
        self.assertEqual(synth.overall_status, VerificationStatus.CONFLICTING)

    # -------------------------------------------------------------------------
    # 4. Source Priority & Hierarchy Tests
    # -------------------------------------------------------------------------

    def test_14_rbi_beats_generic_blog(self):
        c_rbi = make_verified_claim("Repo rate is 6.50%", "rbi.org.in", source_type=SourceType.REGULATOR, authority=AuthorityLevel.VERY_HIGH, is_primary=True, rates=["6.50%"])
        c_blog = make_verified_claim("Repo rate is 7.00%", "crypto-blog.org", source_type=SourceType.GENERIC_BLOG, authority=AuthorityLevel.LOW, is_primary=False, rates=["7.00%"])
        conflicts = self.synthesizer.detect_conflicts(self.synthesizer.group_claims([c_rbi, c_blog]))
        self.assertEqual(conflicts[0].preferred_claim.source_domain, "rbi.org.in")

    def test_15_official_bank_beats_aggregator(self):
        c_bank = make_verified_claim("HDFC Bank personal loan rate 10.50%", "hdfcbank.com", source_type=SourceType.BANK_NBFC, authority=AuthorityLevel.HIGH, is_primary=True, rates=["10.50%"])
        c_agg = make_verified_claim("HDFC Bank personal loan rate 11.25%", "bankbazaar.com", source_type=SourceType.SECONDARY_AGGREGATOR, authority=AuthorityLevel.MEDIUM, is_primary=False, rates=["11.25%"])
        conflicts = self.synthesizer.detect_conflicts(self.synthesizer.group_claims([c_bank, c_agg]))
        self.assertEqual(conflicts[0].preferred_claim.source_domain, "hdfcbank.com")

    # -------------------------------------------------------------------------
    # 5. Multi-Source Query Comparison (e.g. SBI vs HDFC vs ICICI)
    # -------------------------------------------------------------------------

    def test_16_multi_bank_comparison_synthesis(self):
        c_sbi = make_verified_claim("SBI home loans starting from 7.25% p.a.", "sbi.bank.in", rates=["7.25% p.a."], qualifiers=["starting from"])
        c_hdfc = make_verified_claim("HDFC Bank home loans starting from 7.40% p.a.", "hdfcbank.com", rates=["7.40% p.a."], qualifiers=["starting from"])
        c_icici = make_verified_claim("ICICI Bank home loans starting from 7.50% p.a.", "icicibank.com", rates=["7.50% p.a."], qualifiers=["starting from"])

        ev = VerifiedResearchEvidence(
            query="Compare SBI, HDFC and ICICI home loan rates",
            sources=[c_sbi.source_credibility, c_hdfc.source_credibility, c_icici.source_credibility],
            verified_claims=[c_sbi, c_hdfc, c_icici],
            success=True,
        )

        synth = self.synthesizer.synthesize("Compare SBI, HDFC and ICICI home loan rates", [ev])
        self.assertEqual(len(synth.conclusions), 3)
        self.assertEqual(synth.overall_status, VerificationStatus.VERIFIED_PRIMARY)
        self.assertEqual(synth.overall_confidence, ConfidenceLevel.HIGH)

        entities_found = {c.entity for c in synth.conclusions}
        self.assertEqual(entities_found, {"SBI", "HDFC Bank", "ICICI Bank"})
        for c in synth.conclusions:
            self.assertIn("starting from", c.qualifiers)
            self.assertTrue(len(c.caveats) > 0)

    # -------------------------------------------------------------------------
    # 6. Full Provider Pipeline & Factory Resolution
    # -------------------------------------------------------------------------

    def test_17_factory_resolution_with_synthesizer(self):
        provider = get_research_provider()
        self.assertTrue(hasattr(provider, "synthesizer"))
        self.assertTrue(isinstance(provider.synthesizer, ResearchSynthesizer))

    async def test_18_composite_provider_synthesize_call(self):
        provider = get_research_provider()
        c = make_verified_claim("SBI home loan 7.25%", "sbi.bank.in", rates=["7.25%"])
        ev = VerifiedResearchEvidence(query="sbi rate", sources=[c.source_credibility], verified_claims=[c], success=True)
        synth = provider.synthesize(query="sbi rate", verified_evidences=[ev])
        self.assertTrue(synth.success)
        self.assertEqual(len(synth.claim_groups), 1)

    # -------------------------------------------------------------------------
    # 7. ContextBuilder Rendering for ResearchSynthesis
    # -------------------------------------------------------------------------

    def test_19_context_builder_renders_multi_source_synthesis(self):
        cb = ContextBuilder(default_system_prompt="System Prompt")
        synthesis_dict = {
            "query": "Compare SBI and HDFC home loan rates",
            "overall_status": "VERIFIED_PRIMARY",
            "overall_confidence": "HIGH",
            "conclusions": [
                {
                    "topic": "SBI Home Loan",
                    "entity": "SBI",
                    "product": "Home Loan",
                    "synthesized_statement": "SBI official rate for Home Loan is starting from 7.25% p.a.",
                    "confidence": "HIGH",
                    "qualifiers": ["starting from"],
                    "provenance_urls": ["https://sbi.bank.in/home-loans"],
                    "caveats": ["Individual rates depend on CIBIL and loan terms."],
                },
                {
                    "topic": "HDFC Bank Home Loan",
                    "entity": "HDFC Bank",
                    "product": "Home Loan",
                    "synthesized_statement": "HDFC Bank official rate for Home Loan is starting from 7.40% p.a.",
                    "confidence": "HIGH",
                    "qualifiers": ["starting from"],
                    "provenance_urls": ["https://hdfcbank.com/home-loans"],
                    "caveats": ["Subject to underwriting."],
                }
            ],
            "conflicts": [],
        }
        tc = ToolContext().add_result(
            tool_name="multi_source_research",
            call_id="call_synth_1",
            output=synthesis_dict,
        )
        messages = cb.build(current_message="Compare SBI and HDFC", tool_context=tc)
        combined = " ".join(m["content"] for m in messages)
        self.assertIn("Multi-Source Research Synthesis", combined)
        self.assertIn("SBI official rate", combined)
        self.assertIn("HDFC Bank official rate", combined)
        self.assertIn("starting from", combined)
        self.assertIn("https://sbi.bank.in/home-loans", combined)


if __name__ == "__main__":
    unittest.main()
