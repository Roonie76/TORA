import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
import pytest

from backend.research.models import (
    SourceMetadata,
    SearchResult,
    FetchResult,
    SearchResponse as ResearchSearchResponse,
    current_utc_timestamp,
    extract_domain_from_url,
)
from backend.research.exceptions import (
    ResearchError,
    ResearchSearchError,
    ResearchFetchError,
    ResearchValidationError,
    ResearchProviderError,
)
from backend.research.provider import (
    ResearchProvider,
    CompositeResearchProvider,
    normalize_url,
    MAX_RESEARCH_QUERY_LENGTH,
    MAX_RESEARCH_URL_LENGTH,
)
from backend.research.factory import get_research_provider

from backend.tools.search.base import (
    SearchProvider,
    SearchResult as BaseSearchResult,
    SearchResponse as BaseSearchResponse,
    SearchError,
    SearchConnectionError,
    SearchTimeoutError,
)
from backend.tools.web_fetch.provider.base import FetchProvider
from backend.tools.web_fetch.models import (
    FetchResponse as BaseFetchResponse,
    FetchResult as BaseFetchResult,
    FetchError,
    FetchConnectionError,
    FetchTimeoutError,
    FetchSSRFError,
)


class TestResearchModels(unittest.TestCase):
    """Test research data models, serialization, and metadata preservation."""

    def test_source_metadata_creation_and_defaults(self):
        meta = SourceMetadata(url="https://rbi.org.in/scripts/BS_CircularIndexDisplay.aspx")
        self.assertEqual(meta.url, "https://rbi.org.in/scripts/BS_CircularIndexDisplay.aspx")
        self.assertEqual(meta.domain, "rbi.org.in")
        self.assertIsNotNone(meta.retrieved_at)
        self.assertFalse(meta.is_verified)
        self.assertEqual(meta.content_type, "text/html")

        # Verify ISO-8601 format of retrieval timestamp
        dt = datetime.fromisoformat(meta.retrieved_at)
        self.assertIsNotNone(dt.tzinfo)

    def test_metadata_preservation(self):
        meta = SourceMetadata(
            url="https://sbi.co.in/loans",
            title="SBI Loan Rates",
            domain="sbi.co.in",
            source_name="State Bank of India",
            published_at="2026-01-15T10:00:00Z",
            author="SBI Research Team",
            is_verified=True,
            extra={"rating": 5, "category": "banking"},
        )
        d = meta.to_dict()
        self.assertEqual(d["source_name"], "State Bank of India")
        self.assertEqual(d["published_at"], "2026-01-15T10:00:00Z")
        self.assertEqual(d["author"], "SBI Research Team")
        self.assertTrue(d["is_verified"])
        self.assertEqual(d["extra"]["category"], "banking")

    def test_search_result_factory_create(self):
        res = SearchResult.create(
            query="gold loan rates",
            title="Gold Loan 2026",
            url="https://bankbazaar.com/gold-loan",
            snippet="Current gold loan interest rates start from 8.5%",
            published_at="2026-02-01",
            author="BankBazaar",
        )
        self.assertEqual(res.query, "gold loan rates")
        self.assertEqual(res.domain, "bankbazaar.com")
        self.assertEqual(res.metadata.published_at, "2026-02-01")
        self.assertTrue(res.success)
        self.assertIsNone(res.error)

    def test_fetch_result_factory_create(self):
        res = FetchResult.create(
            url="https://rbi.org.in/rates",
            final_url="https://rbi.org.in/rates/current",
            title="Policy Rates",
            content="Repo Rate is 6.50%",
            status_code=200,
            truncated=False,
        )
        self.assertEqual(res.url, "https://rbi.org.in/rates")
        self.assertEqual(res.final_url, "https://rbi.org.in/rates/current")
        self.assertEqual(res.domain, "rbi.org.in")
        self.assertEqual(res.content, "Repo Rate is 6.50%")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.success)

    def test_extract_domain_from_url(self):
        self.assertEqual(extract_domain_from_url("https://www.hdfcbank.com/personal/loans"), "www.hdfcbank.com")
        self.assertEqual(extract_domain_from_url("http://incometax.gov.in:8080/portal"), "incometax.gov.in:8080")
        self.assertEqual(extract_domain_from_url(""), "")
        self.assertEqual(extract_domain_from_url("invalid-url"), "")

    def test_normalize_url(self):
        self.assertEqual(normalize_url("https://example.com/page/"), "https://example.com/page")
        self.assertEqual(normalize_url("http://EXAMPLE.COM:80/path"), "http://example.com/path")
        self.assertEqual(normalize_url("https://example.com:443/"), "https://example.com/")


class TestCompositeResearchProvider(unittest.IsolatedAsyncioTestCase):
    """Test CompositeResearchProvider execution, error handling, deduplication, and validation."""

    def setUp(self):
        self.mock_search_provider = AsyncMock(spec=SearchProvider)
        self.mock_search_provider.name = "mock_search"

        self.mock_fetch_provider = AsyncMock(spec=FetchProvider)
        self.mock_fetch_provider.name = "mock_fetch"

        self.research_provider = CompositeResearchProvider(
            search_provider=self.mock_search_provider,
            fetch_provider=self.mock_fetch_provider,
        )

    # 1. Valid Search Result
    async def test_valid_search_result(self):
        self.mock_search_provider.search.return_value = BaseSearchResponse(
            query="sbi gold loan",
            provider="mock_search",
            results=[
                BaseSearchResult(
                    title="SBI Gold Loan",
                    url="https://sbi.co.in/gold-loan",
                    snippet="Interest rates start at 8.65%",
                    domain="sbi.co.in",
                    rank=1,
                ),
                BaseSearchResult(
                    title="HDFC Gold Loan",
                    url="https://hdfcbank.com/gold-loan",
                    snippet="Quick approval for gold loans",
                    domain="hdfcbank.com",
                    rank=2,
                ),
            ],
        )

        response = await self.research_provider.search(query="sbi gold loan", max_results=5)
        self.assertTrue(response.success)
        self.assertEqual(len(response.results), 2)
        self.assertEqual(response.total_results, 2)
        self.assertEqual(response.results[0].title, "SBI Gold Loan")
        self.assertEqual(response.results[0].domain, "sbi.co.in")
        self.assertEqual(response.results[0].rank, 1)
        self.assertIsNotNone(response.results[0].metadata.retrieved_at)
        self.assertIsNotNone(response.retrieved_at)

    # 2. Failed Search (Provider Exception)
    async def test_failed_search_raises_research_search_error(self):
        self.mock_search_provider.search.side_effect = SearchConnectionError("Failed to reach search backend")

        with self.assertRaises(ResearchSearchError) as ctx:
            await self.research_provider.search(query="loan rates")
        self.assertIn("Failed to reach search backend", str(ctx.exception))

    async def test_unexpected_search_provider_exception(self):
        self.mock_search_provider.search.side_effect = RuntimeError("Fatal memory error")

        with self.assertRaises(ResearchProviderError) as ctx:
            await self.research_provider.search(query="loan rates")
        self.assertIn("Unexpected search error", str(ctx.exception))

    # 3. Failed Fetch (Provider Exception)
    async def test_failed_fetch_raises_research_fetch_error(self):
        self.mock_fetch_provider.fetch.side_effect = FetchConnectionError("Connection refused")

        with self.assertRaises(ResearchFetchError) as ctx:
            await self.research_provider.fetch(url="https://example.com/rates")
        self.assertIn("Connection refused", str(ctx.exception))

    async def test_fetch_ssrf_error(self):
        self.mock_fetch_provider.fetch.side_effect = FetchSSRFError("Forbidden IP address: 127.0.0.1")

        with self.assertRaises(ResearchFetchError) as ctx:
            await self.research_provider.fetch(url="http://127.0.0.1/admin")
        self.assertIn("Forbidden IP address", str(ctx.exception))

    # 4. Malformed Source & Graceful Handling
    async def test_malformed_search_source_handling(self):
        self.mock_search_provider.search.return_value = BaseSearchResponse(
            query="test",
            provider="mock_search",
            results=[
                BaseSearchResult(
                    title="",  # Empty title
                    url="https://example.com/test",
                    snippet="",
                    domain="",  # Missing domain
                    rank=None,
                ),
            ],
        )

        response = await self.research_provider.search(query="test")
        self.assertEqual(len(response.results), 1)
        self.assertEqual(response.results[0].title, "Untitled")
        self.assertEqual(response.results[0].domain, "example.com")

    # 5. Empty Search Result
    async def test_empty_search_results(self):
        self.mock_search_provider.search.return_value = BaseSearchResponse(
            query="nonexistent topic 9999",
            provider="mock_search",
            results=[],
        )

        response = await self.research_provider.search(query="nonexistent topic 9999")
        self.assertTrue(response.success)
        self.assertEqual(len(response.results), 0)
        self.assertEqual(response.total_results, 0)

    # 6. Duplicate Result Deduplication
    async def test_duplicate_url_deduplication(self):
        self.mock_search_provider.search.return_value = BaseSearchResponse(
            query="sbi loans",
            provider="mock_search",
            results=[
                BaseSearchResult(
                    title="SBI Loans Official",
                    url="https://sbi.co.in/loans",
                    snippet="Official loans page",
                    domain="sbi.co.in",
                    rank=1,
                ),
                BaseSearchResult(
                    title="SBI Loans Duplicate",
                    url="https://sbi.co.in/loans/",  # Trailing slash duplicate
                    snippet="Duplicate snippet",
                    domain="sbi.co.in",
                    rank=2,
                ),
                BaseSearchResult(
                    title="SBI Car Loans",
                    url="https://sbi.co.in/car-loans",
                    snippet="Car loans page",
                    domain="sbi.co.in",
                    rank=3,
                ),
            ],
        )

        response = await self.research_provider.search(query="sbi loans")
        self.assertEqual(len(response.results), 2)
        self.assertEqual(response.results[0].url, "https://sbi.co.in/loans")
        self.assertEqual(response.results[1].url, "https://sbi.co.in/car-loans")
        # Ensure ranks are re-indexed consecutively
        self.assertEqual(response.results[0].rank, 1)
        self.assertEqual(response.results[1].rank, 2)

    # 7. URL Validation
    async def test_url_validation_for_fetch(self):
        # Empty URL
        with self.assertRaises(ResearchValidationError):
            await self.research_provider.fetch(url="")

        # Non-HTTP scheme
        with self.assertRaises(ResearchValidationError):
            await self.research_provider.fetch(url="ftp://ftp.example.com/file.txt")

        # Javascript pseudo-protocol
        with self.assertRaises(ResearchValidationError):
            await self.research_provider.fetch(url="javascript:alert(1)")

        # Excessively long URL
        with self.assertRaises(ResearchValidationError):
            await self.research_provider.fetch(url="https://example.com/" + "a" * (MAX_RESEARCH_URL_LENGTH + 10))

    # 8. Query Validation
    async def test_query_validation_for_search(self):
        # Empty query
        with self.assertRaises(ResearchValidationError):
            await self.research_provider.search(query="")

        # Whitespace-only query
        with self.assertRaises(ResearchValidationError):
            await self.research_provider.search(query="    ")

        # Excessively long query
        with self.assertRaises(ResearchValidationError):
            await self.research_provider.search(query="a" * (MAX_RESEARCH_QUERY_LENGTH + 10))

    # 9. Successful Fetch Content & Metadata
    async def test_valid_fetch_content(self):
        self.mock_fetch_provider.fetch.return_value = BaseFetchResponse(
            url="https://rbi.org.in/notification",
            provider="http",
            result=BaseFetchResult(
                url="https://rbi.org.in/notification",
                final_url="https://rbi.org.in/notification/2026/01",
                title="RBI Notification on Repo Rate",
                content="The Monetary Policy Committee decided to maintain the policy repo rate at 6.50%.",
                domain="rbi.org.in",
                content_type="text/html",
                status_code=200,
                truncated=False,
            ),
        )

        res = await self.research_provider.fetch(url="https://rbi.org.in/notification")
        self.assertTrue(res.success)
        self.assertEqual(res.title, "RBI Notification on Repo Rate")
        self.assertEqual(res.final_url, "https://rbi.org.in/notification/2026/01")
        self.assertEqual(res.domain, "rbi.org.in")
        self.assertEqual(res.status_code, 200)
        self.assertIn("repo rate at 6.50%", res.content.lower())
        self.assertIsNotNone(res.metadata.retrieved_at)
        self.assertEqual(res.metadata.extra["status_code"], 200)

    # 10. Factory Resolution
    def test_factory_instantiation(self):
        provider = get_research_provider(
            search_provider=self.mock_search_provider,
            fetch_provider=self.mock_fetch_provider,
        )
        self.assertIsInstance(provider, ResearchProvider)
        self.assertIsInstance(provider, CompositeResearchProvider)

    # 11. Serialization & Truncation Checks
    async def test_fetch_truncation_and_serialization(self):
        self.mock_fetch_provider.fetch.return_value = BaseFetchResponse(
            url="https://example.com/long",
            provider="http",
            result=BaseFetchResult(
                url="https://example.com/long",
                final_url="https://example.com/long",
                title="Long Article",
                content="A" * 1000,
                domain="example.com",
                content_type="text/html",
                status_code=200,
                truncated=True,
            ),
        )

        res = await self.research_provider.fetch(url="https://example.com/long", max_chars=1000)
        self.assertTrue(res.truncated)
        d = res.to_dict()
        self.assertEqual(d["url"], "https://example.com/long")
        self.assertTrue(d["truncated"])
        self.assertIn("retrieved_at", d["metadata"])

    async def test_search_response_serialization(self):
        self.mock_search_provider.search.return_value = BaseSearchResponse(
            query="mutual fund tax 2026",
            provider="mock_search",
            results=[
                BaseSearchResult(
                    title="Mutual Fund Tax 2026",
                    url="https://cleartax.in/s/mutual-fund-tax",
                    snippet="LTCG on equity mutual funds is 12.5%",
                    domain="cleartax.in",
                    rank=1,
                )
            ],
        )

        resp = await self.research_provider.search(query="mutual fund tax 2026")
        d = resp.to_dict()
        self.assertEqual(d["query"], "mutual fund tax 2026")
        self.assertEqual(d["total_results"], 1)
        self.assertEqual(len(d["results"]), 1)
        self.assertEqual(d["results"][0]["title"], "Mutual Fund Tax 2026")
        self.assertEqual(d["results"][0]["metadata"]["domain"], "cleartax.in")

