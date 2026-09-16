import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
import pytest

from backend.research.normalization import (
    SearchNormalizer,
    normalize_url,
    clean_text,
    validate_search_query,
    TRACKING_PARAMS,
    MAX_TITLE_LENGTH,
    MAX_SNIPPET_LENGTH,
    MAX_QUERY_LENGTH,
)
from backend.research.models import (
    SearchResult,
    FetchResult,
    SourceMetadata,
    SearchResponse,
)
from backend.research.exceptions import (
    ResearchValidationError,
    ResearchSearchError,
    ResearchProviderError,
)
from backend.research.provider import CompositeResearchProvider
from backend.tools.search.base import (
    SearchProvider,
    SearchResult as BaseSearchResult,
    SearchResponse as BaseSearchResponse,
    SearchConnectionError,
)


class TestSearchNormalizationLayer(unittest.TestCase):
    """
    Comprehensive tests for the Search Normalization & Quality Layer (Phase 2H-B).
    Covers all 25 quality, deduplication, URL handling, text sanitization, and safety requirements.
    """

    def setUp(self):
        self.normalizer = SearchNormalizer()

    # 1. Exact Duplicate URL
    def test_exact_duplicate_url(self):
        raw_items = [
            {"title": "SBI Home Loans", "url": "https://sbi.co.in/home-loans", "snippet": "First item"},
            {"title": "SBI Home Loans", "url": "https://sbi.co.in/home-loans", "snippet": "Exact duplicate item"},
        ]
        results = self.normalizer.normalize_results(raw_items, query="sbi home loan")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://sbi.co.in/home-loans")
        self.assertEqual(results[0].rank, 1)

    # 2. Trailing Slash Duplicate
    def test_trailing_slash_duplicate(self):
        raw_items = [
            {"title": "HDFC Home Loan", "url": "https://www.hdfcbank.com/personal/borrow/popular-loans/home-loan", "snippet": "Snippet without slash"},
            {"title": "HDFC Home Loan", "url": "https://www.hdfcbank.com/personal/borrow/popular-loans/home-loan/", "snippet": "Snippet with slash"},
        ]
        results = self.normalizer.normalize_results(raw_items, query="hdfc home loan")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://www.hdfcbank.com/personal/borrow/popular-loans/home-loan")

    # 3. Tracking Parameter Duplicate
    def test_tracking_parameter_duplicate(self):
        raw_items = [
            {"title": "BankBazaar Gold Loan", "url": "https://www.bankbazaar.com/gold-loan.html", "snippet": "Base clean URL"},
            {"title": "BankBazaar Gold Loan", "url": "https://www.bankbazaar.com/gold-loan.html?utm_source=google&utm_medium=cpc&gclid=12345", "snippet": "Tracked URL"},
        ]
        results = self.normalizer.normalize_results(raw_items, query="gold loan")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://www.bankbazaar.com/gold-loan.html")

    # 4. Different Meaningful Query Parameters
    def test_different_meaningful_query_parameters(self):
        raw_items = [
            {"title": "Search Page 1", "url": "https://rbi.org.in/notifications?page=1&category=circulars", "snippet": "Page 1 circulars"},
            {"title": "Search Page 2", "url": "https://rbi.org.in/notifications?page=2&category=circulars", "snippet": "Page 2 circulars"},
        ]
        results = self.normalizer.normalize_results(raw_items, query="rbi circulars")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].rank, 1)
        self.assertEqual(results[1].rank, 2)
        self.assertIn("page=1", results[0].url)
        self.assertIn("page=2", results[1].url)

    # 5. Different Paths on Same Domain
    def test_different_paths_on_same_domain(self):
        raw_items = [
            {"title": "SBI Home Loan", "url": "https://sbi.co.in/home-loan", "snippet": "Home loan details"},
            {"title": "SBI Personal Loan", "url": "https://sbi.co.in/personal-loan", "snippet": "Personal loan details"},
        ]
        results = self.normalizer.normalize_results(raw_items, query="sbi loans")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].url, "https://sbi.co.in/home-loan")
        self.assertEqual(results[1].url, "https://sbi.co.in/personal-loan")

    # 6. Malformed URL
    def test_malformed_url(self):
        raw_items = [
            {"title": "Invalid Scheme", "url": "ftp://files.example.com/rates.pdf", "snippet": "FTP link"},
            {"title": "No Protocol", "url": "just_a_string", "snippet": "Invalid URL"},
            {"title": "Empty URL", "url": "", "snippet": "Empty"},
            {"title": "Valid Result", "url": "https://example.com/rates", "snippet": "Valid rate page"},
        ]
        results = self.normalizer.normalize_results(raw_items, query="rates")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/rates")

    # 7. Missing Title
    def test_missing_title(self):
        raw_items = [
            {"title": "", "url": "https://sbi.co.in/rates", "snippet": "Rates page"},
            {"title": None, "url": "https://icicibank.com/rates", "snippet": "ICICI rates"},
        ]
        results = self.normalizer.normalize_results(raw_items, query="rates")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "Untitled")
        self.assertEqual(results[1].title, "Untitled")

    # 8. Missing Snippet
    def test_missing_snippet(self):
        raw_items = [
            {"title": "RBI Repo Rate", "url": "https://rbi.org.in/repo", "snippet": ""},
            {"title": "RBI Reverse Repo", "url": "https://rbi.org.in/reverse-repo", "snippet": None},
        ]
        results = self.normalizer.normalize_results(raw_items, query="rbi repo")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].snippet, "")
        self.assertEqual(results[1].snippet, "")

    # 9. Missing Domain
    def test_missing_domain(self):
        raw_item = {"title": "Axis Bank FD", "url": "https://www.axisbank.com/fd-rates", "domain": "", "snippet": "FD rates"}
        res = self.normalizer.normalize_result(raw_item, query="axis fd")
        self.assertIsNotNone(res)
        self.assertEqual(res.domain, "www.axisbank.com")
        self.assertEqual(res.metadata.domain, "www.axisbank.com")

    # 10. Extremely Long Title
    def test_extremely_long_title(self):
        long_title = "SBI Home Loan Rates 2026 " * 20  # ~500 chars
        raw_item = {"title": long_title, "url": "https://sbi.co.in/long-title", "snippet": "Test"}
        res = self.normalizer.normalize_result(raw_item, query="sbi")
        self.assertIsNotNone(res)
        self.assertLessEqual(len(res.title), MAX_TITLE_LENGTH + 5)
        self.assertTrue(res.title.endswith("..."))

    # 11. Extremely Long Snippet
    def test_extremely_long_snippet(self):
        long_snippet = "Current home loan interest rate is 8.5% with flexible tenure and zero processing fee. " * 30  # ~2500 chars
        raw_item = {"title": "SBI Rates", "url": "https://sbi.co.in/rates", "snippet": long_snippet}
        res = self.normalizer.normalize_result(raw_item, query="sbi")
        self.assertIsNotNone(res)
        self.assertLessEqual(len(res.snippet), MAX_SNIPPET_LENGTH + 5)
        self.assertTrue(res.snippet.endswith("..."))

    # 12. Invalid Rank
    def test_invalid_rank(self):
        raw_items = [
            {"title": "Item 1", "url": "https://example.com/1", "rank": -5},
            {"title": "Item 2", "url": "https://example.com/2", "rank": 0},
            {"title": "Item 3", "url": "https://example.com/3", "rank": 999},
        ]
        results = self.normalizer.normalize_results(raw_items, query="test")
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].rank, 1)
        self.assertEqual(results[1].rank, 2)
        self.assertEqual(results[2].rank, 3)

    # 13. Missing Publication Date
    def test_missing_publication_date(self):
        raw_item = {"title": "Test", "url": "https://example.com/page", "snippet": "Test", "published_at": None}
        res = self.normalizer.normalize_result(raw_item, query="test")
        self.assertIsNotNone(res)
        self.assertIsNone(res.metadata.published_at)

    # 14. Duplicate Result with Better Metadata Merge
    def test_duplicate_result_with_better_metadata_merge(self):
        raw_items = [
            {
                "title": "Short Title",
                "url": "https://sbi.co.in/home-loans",
                "snippet": "Short snippet",
                "published_at": None,
                "rank": 1,
            },
            {
                "title": "State Bank of India - Comprehensive Home Loan Interest Rates 2026",
                "url": "https://sbi.co.in/home-loans?utm_source=google",
                "snippet": "State Bank of India offers home loans starting at 8.50% p.a. with zero processing charges for women borrowers.",
                "published_at": "2026-01-20T08:00:00Z",
                "author": "SBI Retail Team",
                "rank": 2,
            },
        ]
        results = self.normalizer.normalize_results(raw_items, query="sbi home loans")
        self.assertEqual(len(results), 1)
        merged = results[0]
        # Should preserve the better title
        self.assertEqual(merged.title, "State Bank of India - Comprehensive Home Loan Interest Rates 2026")
        # Should preserve the longer, richer snippet
        self.assertIn("zero processing charges", merged.snippet)
        # Should preserve the publication date and author from secondary duplicate
        self.assertEqual(merged.metadata.published_at, "2026-01-20T08:00:00Z")
        self.assertEqual(merged.metadata.author, "SBI Retail Team")
        # Should preserve earliest rank
        self.assertEqual(merged.rank, 1)

    # 15. Empty Result Set
    def test_empty_result_set(self):
        results = self.normalizer.normalize_results([], query="unmatched query")
        self.assertEqual(results, [])

        resp = self.normalizer.normalize_response(None, query="unmatched query")
        self.assertEqual(resp.total_results, 0)
        self.assertEqual(resp.results, [])

    # 16. Result Limit
    def test_result_limit(self):
        raw_items = [{"title": f"Item {i}", "url": f"https://example.com/{i}", "snippet": f"Snippet {i}"} for i in range(10)]
        results = self.normalizer.normalize_results(raw_items, query="test", max_results=3)
        self.assertEqual(len(results), 3)
        self.assertEqual([r.rank for r in results], [1, 2, 3])

    # 17. Excessive Results Bounding
    def test_excessive_results_bounding(self):
        raw_items = [{"title": f"Item {i}", "url": f"https://example.com/{i}", "snippet": f"Snippet {i}"} for i in range(50)]
        results = self.normalizer.normalize_results(raw_items, query="test", max_results=100)
        # Bounded by HARD_MAX_RESULTS (20)
        self.assertEqual(len(results), 20)

    # 18. Empty Search Query
    def test_empty_search_query(self):
        with self.assertRaises(ResearchValidationError):
            validate_search_query("")

    # 19. Whitespace Query
    def test_whitespace_query(self):
        with self.assertRaises(ResearchValidationError):
            validate_search_query("     \t\n  ")

    # 20. Excessively Long Query
    def test_excessively_long_query(self):
        with self.assertRaises(ResearchValidationError):
            validate_search_query("what are rates " * 60)  # ~900 chars

    # 21. Normal Financial Query
    def test_normal_financial_query(self):
        q = "What are the current SBI home loan rates in India?"
        cleaned = validate_search_query(q)
        self.assertEqual(cleaned, q)

    # 22. Normal Conversational Query
    def test_normal_conversational_query(self):
        q = "Can you check the latest RBI monetary policy announcement on repo rate?"
        cleaned = validate_search_query(q)
        self.assertEqual(cleaned, q)

    # 23. Provider Exception Handling
    def test_html_entity_decoding_in_text(self):
        raw_title = "SBI &amp; HDFC Home Loans &#45; Compare &quot;Top Rates&quot;"
        raw_snippet = "Interest rate &lt; 9.00% &amp; processing fee is &#8377;0"
        raw_item = {"title": raw_title, "url": "https://example.com/compare", "snippet": raw_snippet}
        res = self.normalizer.normalize_result(raw_item, query="compare")
        self.assertIsNotNone(res)
        self.assertEqual(res.title, "SBI & HDFC Home Loans - Compare \"Top Rates\"")
        self.assertIn("Interest rate < 9.00% & processing fee is ₹0", res.snippet)

    # 24. Mixed Valid / Invalid Results
    def test_mixed_valid_and_invalid_results(self):
        raw_items = [
            None,
            {"title": "Valid 1", "url": "https://bank1.com/rates", "snippet": "Rates 1"},
            {"title": "Invalid URL", "url": "not_a_url", "snippet": "No scheme"},
            {"title": "Valid 2", "url": "https://bank2.com/rates", "snippet": "Rates 2"},
            {"title": "Empty URL", "url": "", "snippet": "Empty"},
        ]
        results = self.normalizer.normalize_results(raw_items, query="banks")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "Valid 1")
        self.assertEqual(results[1].title, "Valid 2")
        self.assertEqual(results[0].rank, 1)
        self.assertEqual(results[1].rank, 2)

    # 25. Retrieval Timestamp Preservation
    def test_retrieval_timestamp_preservation(self):
        raw_item = {"title": "RBI Repo", "url": "https://rbi.org.in/repo", "snippet": "Repo rate"}
        res = self.normalizer.normalize_result(raw_item, query="rbi")
        self.assertIsNotNone(res)
        self.assertIsNotNone(res.metadata.retrieved_at)
        # Parse ISO-8601 UTC timestamp
        dt = datetime.fromisoformat(res.metadata.retrieved_at)
        self.assertIsNotNone(dt.tzinfo)


class TestCompositeProviderWithNormalizer(unittest.IsolatedAsyncioTestCase):
    """
    Test CompositeResearchProvider end-to-end integration with SearchNormalizer.
    """

    async def test_provider_search_integration(self):
        mock_search = AsyncMock(spec=SearchProvider)
        mock_search.name = "mock_search_engine"
        mock_search.search.return_value = BaseSearchResponse(
            query="sbi home loan rates",
            provider="mock_search_engine",
            results=[
                BaseSearchResult(
                    title="SBI Home Loans &amp; Rates",
                    url="https://sbi.co.in/home-loans/?utm_source=bing",
                    snippet="Current SBI home loan rate starts at 8.50% &amp; offers concessions.",
                    domain="sbi.co.in",
                    rank=1,
                ),
                BaseSearchResult(
                    title="SBI Home Loans Official",
                    url="https://sbi.co.in/home-loans",
                    snippet="Full comprehensive guide to SBI home loans.",
                    domain="sbi.co.in",
                    rank=2,
                ),
            ],
        )

        provider = CompositeResearchProvider(search_provider=mock_search)
        resp = await provider.search(query="sbi home loan rates", max_results=5)

        self.assertTrue(resp.success)
        # Should deduplicate trailing slash + utm_source into 1 single merged result
        self.assertEqual(len(resp.results), 1)
        res = resp.results[0]
        self.assertEqual(res.url, "https://sbi.co.in/home-loans")
        self.assertEqual(res.rank, 1)
        self.assertEqual(res.title, "SBI Home Loans & Rates")
        self.assertIn("8.50% & offers concessions", res.snippet)
        self.assertIsNotNone(res.metadata.retrieved_at)


def test_duckduckgo_snippets_paired_by_position():
    """A result without a snippet must not receive the next result's snippet."""
    from backend.tools.search.duckduckgo import DuckDuckGoSearchProvider

    html_doc = (
        '<a class="result__a" href="https://a.example/x">Result A</a>'
        '<a class="result__a" href="https://b.example/y">Result B</a>'
        '<a class="result__snippet" href="#">Snippet for B</a>'
    )
    results = DuckDuckGoSearchProvider()._parse_html_results(html_doc, max_results=5)
    assert [r.title for r in results] == ["Result A", "Result B"]
    assert results[0].snippet == ""
    assert results[1].snippet == "Snippet for B"
