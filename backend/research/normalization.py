import html
import logging
import re
from typing import Optional, List, Dict, Any, Set, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from .models import (
    SearchResult,
    SourceMetadata,
    SearchResponse,
    current_utc_timestamp,
    extract_domain_from_url,
)
from .exceptions import ResearchValidationError

logger = logging.getLogger("tora.research.normalization")

# Common marketing and telemetry query parameters to safely strip
TRACKING_PARAMS: Set[str] = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "gclsrc",
    "dclid",
    "msclkid",
    "mc_eid",
    "ref",
    "ocid",
    "ncid",
    "igshid",
    "si",
    "source",
}

# Limits and safety bounds
MAX_TITLE_LENGTH: int = 250
MAX_SNIPPET_LENGTH: int = 1500
MAX_QUERY_LENGTH: int = 500
DEFAULT_MAX_RESULTS: int = 5
HARD_MAX_RESULTS: int = 20


def clean_text(text: Optional[str], max_chars: Optional[int] = None) -> str:
    """
    Sanitize text by unescaping HTML entities, stripping control characters,
    normalizing whitespace, and cleanly truncating if max_chars is provided.
    """
    if not text:
        return ""

    # Unescape HTML entities (e.g. &amp; -> &, &#39; -> ')
    unescaped = html.unescape(str(text))

    # Strip control characters (excluding newline/tab)
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", unescaped)

    # Normalize excessive internal whitespace and newlines
    collapsed = re.sub(r"\s+", " ", sanitized).strip()

    if max_chars is not None and len(collapsed) > max_chars:
        # Truncate cleanly at word boundary if possible
        truncated = collapsed[:max_chars].rstrip()
        last_space = truncated.rfind(" ")
        if last_space > int(max_chars * 0.75):
            truncated = truncated[:last_space]
        return truncated + "..."

    return collapsed


def normalize_url(url: Optional[str], strip_tracking: bool = True) -> str:
    """
    Normalize and canonicalize a URL string.
    - Lowers scheme and domain
    - Removes default ports (:80 for http, :443 for https)
    - Normalizes path slashes
    - Strips marketing/tracking query parameters while preserving semantic query parameters
    - Alphabetically sorts query parameters for deterministic deduplication
    - Strips URL fragments / anchors
    """
    if not url or not isinstance(url, str):
        return ""

    cleaned = url.strip()
    if not cleaned:
        return ""

    try:
        parsed = urlparse(cleaned)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            return cleaned

        netloc = parsed.netloc.lower()
        if not netloc:
            return cleaned

        # Strip standard default ports
        if ":" in netloc:
            host, port = netloc.split(":", 1)
            if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
                netloc = host

        # Normalize path
        path = parsed.path
        if path:
            # Remove duplicate slashes
            path = re.sub(r"/+", "/", path)
            # Remove trailing slash unless path is just root "/"
            if len(path) > 1 and path.endswith("/"):
                path = path[:-1]
        else:
            path = ""

        # Normalize query parameters
        clean_query = ""
        if parsed.query:
            query_pairs = parse_qsl(parsed.query, keep_blank_values=False)
            filtered_pairs = []
            for k, v in query_pairs:
                if strip_tracking and k.lower() in TRACKING_PARAMS:
                    continue
                filtered_pairs.append((k, v))

            if filtered_pairs:
                # Sort query parameters for consistent canonical URL
                filtered_pairs.sort(key=lambda pair: (pair[0], pair[1]))
                clean_query = urlencode(filtered_pairs)

        return urlunparse((scheme, netloc, path, "", clean_query, ""))
    except Exception:
        return cleaned


def validate_search_query(query: Optional[str], max_length: int = MAX_QUERY_LENGTH) -> str:
    """
    Validate and sanitize search query strings.
    Allows natural language and financial terminology while rejecting empty,
    whitespace-only, or maliciously oversized inputs.
    """
    if not query or not isinstance(query, str):
        raise ResearchValidationError("Search query must be a non-empty string.")

    cleaned = clean_text(query)
    if not cleaned:
        raise ResearchValidationError("Search query cannot be empty or only whitespace.")

    if len(cleaned) > max_length:
        raise ResearchValidationError(
            f"Search query exceeds maximum allowed length of {max_length} characters (got {len(cleaned)})."
        )

    return cleaned


class SearchNormalizer:
    """
    Dedicated, provider-agnostic Search Normalization & Quality Engine.
    Transforms raw search provider outputs into bounded, deduplicated,
    sanitized, and metadata-enriched SearchResult collections.
    """

    def __init__(
        self,
        max_title_length: int = MAX_TITLE_LENGTH,
        max_snippet_length: int = MAX_SNIPPET_LENGTH,
        default_max_results: int = DEFAULT_MAX_RESULTS,
        hard_max_results: int = HARD_MAX_RESULTS,
        strip_tracking_params: bool = True,
    ):
        self.max_title_length = max_title_length
        self.max_snippet_length = max_snippet_length
        self.default_max_results = default_max_results
        self.hard_max_results = hard_max_results
        self.strip_tracking_params = strip_tracking_params

    def normalize_result(
        self,
        raw: Any,
        query: str = "",
        assigned_rank: Optional[int] = None,
    ) -> Optional[SearchResult]:
        """
        Normalize a single search result item.
        Handles missing fields, unescapes text, extracts domain, and populates SourceMetadata.
        Returns None if item has no valid URL.
        """
        if raw is None:
            return None

        # Extract attributes from dict or object
        if isinstance(raw, dict):
            raw_url = raw.get("url") or ""
            raw_title = raw.get("title") or ""
            raw_snippet = raw.get("snippet") or ""
            raw_domain = raw.get("domain") or ""
            raw_rank = raw.get("rank")
            raw_published_at = raw.get("published_at")
            raw_author = raw.get("author")
            raw_metadata = raw.get("metadata") or {}
        else:
            raw_url = getattr(raw, "url", "")
            raw_title = getattr(raw, "title", "")
            raw_snippet = getattr(raw, "snippet", "")
            raw_domain = getattr(raw, "domain", "")
            raw_rank = getattr(raw, "rank", None)
            raw_published_at = getattr(raw, "published_at", None)
            raw_author = getattr(raw, "author", None)
            raw_metadata = getattr(raw, "metadata", {})
            if hasattr(raw_metadata, "to_dict"):
                raw_metadata = raw_metadata.to_dict()
            elif not isinstance(raw_metadata, dict):
                raw_metadata = {}

        clean_url_str = (raw_url or "").strip()
        if not clean_url_str:
            return None

        # Check URL validity (must have scheme)
        if not (clean_url_str.startswith("http://") or clean_url_str.startswith("https://")):
            return None

        canonical_url = normalize_url(clean_url_str, strip_tracking=self.strip_tracking_params)
        resolved_domain = raw_domain.strip().lower() if raw_domain else extract_domain_from_url(canonical_url)

        # Title normalization: fallback to "Untitled" if empty
        cleaned_title = clean_text(raw_title, max_chars=self.max_title_length)
        if not cleaned_title:
            cleaned_title = "Untitled"

        # Snippet normalization
        cleaned_snippet = clean_text(raw_snippet, max_chars=self.max_snippet_length)

        # Rank resolution
        final_rank = assigned_rank if assigned_rank is not None else (raw_rank if isinstance(raw_rank, int) and raw_rank > 0 else None)

        # Build SourceMetadata
        meta = SourceMetadata(
            url=canonical_url,
            title=cleaned_title,
            domain=resolved_domain,
            published_at=str(raw_published_at).strip() if raw_published_at else None,
            author=clean_text(str(raw_author)) if raw_author else None,
            retrieved_at=current_utc_timestamp(),
            extra=raw_metadata if isinstance(raw_metadata, dict) else {},
        )

        return SearchResult(
            query=query,
            title=cleaned_title,
            url=canonical_url,
            domain=resolved_domain,
            snippet=cleaned_snippet,
            rank=final_rank,
            metadata=meta,
            success=True,
            error=None,
        )

    def _merge_duplicate_results(self, primary: SearchResult, secondary: SearchResult) -> SearchResult:
        """
        Merge two SearchResults pointing to the same canonical URL.
        Preserves the primary rank/title (unless placeholder), richer snippet, publication date, and author.
        """
        # Choose richer title: prefer primary title unless placeholder/empty or secondary is substantially more descriptive
        best_title = primary.title
        if primary.title in ("Untitled", "Untitled Source", "") or primary.title.startswith("Source from"):
            if secondary.title and secondary.title not in ("Untitled", "Untitled Source", ""):
                best_title = secondary.title
        elif len(secondary.title) > len(primary.title) + 15 and len(secondary.title) >= 1.5 * len(primary.title):
            best_title = secondary.title

        # Choose longer/richer snippet
        best_snippet = primary.snippet if len(primary.snippet) >= len(secondary.snippet) else secondary.snippet

        # Preserve publication date if available
        best_published = primary.metadata.published_at or secondary.metadata.published_at

        # Preserve author if available
        best_author = primary.metadata.author or secondary.metadata.author

        # Merge extra metadata
        merged_extra = {**secondary.metadata.extra, **primary.metadata.extra}

        # Keep earliest rank
        best_rank = primary.rank
        if secondary.rank is not None and (best_rank is None or secondary.rank < best_rank):
            best_rank = secondary.rank

        meta = SourceMetadata(
            url=primary.url,
            title=best_title,
            domain=primary.domain,
            published_at=best_published,
            author=best_author,
            retrieved_at=primary.metadata.retrieved_at,
            extra=merged_extra,
        )

        return SearchResult(
            query=primary.query,
            title=best_title,
            url=primary.url,
            domain=primary.domain,
            snippet=best_snippet,
            rank=best_rank,
            metadata=meta,
            success=True,
            error=None,
        )

    def normalize_results(
        self,
        raw_results: List[Any],
        query: str = "",
        max_results: Optional[int] = None,
    ) -> List[SearchResult]:
        """
        Normalize a list of search result items with URL deduplication,
        quality merging, consecutive re-ranking, and bounded result limiting.
        """
        if not raw_results:
            return []

        limit = max_results if max_results is not None else self.default_max_results
        bounded_limit = max(1, min(limit, self.hard_max_results))

        normalized_map: Dict[str, SearchResult] = {}
        ordered_keys: List[str] = []

        for item in raw_results:
            norm_res = self.normalize_result(item, query=query)
            if norm_res is None:
                continue

            canonical_key = norm_res.url
            if canonical_key in normalized_map:
                # Merge duplicate with existing record
                merged = self._merge_duplicate_results(normalized_map[canonical_key], norm_res)
                normalized_map[canonical_key] = merged
            else:
                normalized_map[canonical_key] = norm_res
                ordered_keys.append(canonical_key)

        # Extract ordered results, assign consecutive 1..N ranks, and bound to limit
        final_results: List[SearchResult] = []
        for idx, key in enumerate(ordered_keys[:bounded_limit], start=1):
            res = normalized_map[key]
            res.rank = idx
            final_results.append(res)

        return final_results

    def normalize_response(
        self,
        raw_response: Any,
        query: str = "",
        provider_name: str = "search_normalizer",
        max_results: Optional[int] = None,
    ) -> SearchResponse:
        """
        Normalize a search response envelope into a clean SearchResponse.
        """
        if raw_response is None:
            return SearchResponse(
                query=query,
                results=[],
                total_results=0,
                provider=provider_name,
                retrieved_at=current_utc_timestamp(),
                success=True,
                error=None,
            )

        # Extract query and results from response
        resp_query = getattr(raw_response, "query", query) or query
        resp_results = getattr(raw_response, "results", []) or []
        resp_provider = getattr(raw_response, "provider", provider_name) or provider_name

        if isinstance(raw_response, dict):
            resp_query = raw_response.get("query", query) or query
            resp_results = raw_response.get("results", []) or []
            resp_provider = raw_response.get("provider", provider_name) or provider_name

        cleaned_results = self.normalize_results(
            raw_results=resp_results,
            query=resp_query,
            max_results=max_results,
        )

        return SearchResponse(
            query=resp_query,
            results=cleaned_results,
            total_results=len(cleaned_results),
            provider=resp_provider,
            retrieved_at=current_utc_timestamp(),
            success=True,
            error=None,
        )
