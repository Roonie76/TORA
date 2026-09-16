# TORA Phase 2H-B Plan: Search Normalization & Quality Layer

## 1. Current Search Flow
The current search flow established in Phase 2H-A is:
1. **Tool / Agent Invocation:** `ToraAgent` or research caller invokes search with a query and limit.
2. **Provider Execution:** `CompositeResearchProvider` calls the underlying `SearchProvider` (`DuckDuckGoSearchProvider` or `TavilySearchProvider`).
3. **Basic Normalization (in Phase 2H-A):** Basic URL port/path stripping and set-based deduplication occurred inline inside `provider.py`.
4. **Output:** A `SearchResponse` containing a list of `SearchResult` objects.

---

## 2. Existing Normalization & Deduplication in 2H-A
- **URL Normalization:** `normalize_url()` in `provider.py` stripped `:80` / `:443` default ports and trailing slashes.
- **Deduplication:** Maintained `seen_urls = set()` matching normalized URLs.
- **Limitations:**
  - Tracking parameters (`utm_*`, `fbclid`, `gclid`) were not stripped, allowing identical articles with different campaign tags to appear as duplicates.
  - Duplicates were simply discarded rather than merging the best metadata (e.g., if a duplicate had a longer snippet or publication timestamp).
  - Malformed titles, HTML entity artifacts (`&amp;`, `&#39;`), extreme snippet lengths, and whitespace noise were not cleaned.
  - Normalization logic was coupled inside `provider.py` instead of being an independent, composable quality layer.

---

## 3. Missing Quality Controls Addressed in 2H-B
1. **Dedicated Normalization Component:** `SearchNormalizer` in `backend/research/normalization.py`.
2. **Parameter-Aware URL Normalization:** Strips common tracking parameters (`utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`, `fbclid`, `gclid`, `ref`, `ocid`, `ncid`, `msclkid`, `mc_eid`) while strictly preserving semantic parameters (`page`, `id`, `product`, `v`, `q`, `category`, etc.).
3. **Metadata-Preserving Deduplication:** Merges duplicate search items by preserving the richer title, longer snippet, earlier rank, and valid publication dates.
4. **Text Sanitization & Truncation:** Unescapes HTML entities, normalizes excessive whitespace, and cleanly bounds excessive title and snippet lengths.
5. **Robust Malformed Source Defense:** Guarantees zero crashes on missing titles, empty snippets, missing domains, or invalid timestamps.
6. **Query Safety Validation:** Bounded query validation with control-character stripping that preserves normal financial and natural language queries.
7. **Configurable Result Limits:** Deterministic capping of search result count.

---

## 4. Exact Files That Will Change / Be Created

### Created Files:
- [`backend/research/normalization.py`](file:///d:/Projects/Spendsy/backend/research/normalization.py): Dedicated `SearchNormalizer` with URL cleaner, text sanitizer, deduplicator, and quality assessor.
- [`backend/tests/test_search_normalization.py`](file:///d:/Projects/Spendsy/backend/tests/test_search_normalization.py): Comprehensive test suite covering all 25 required test cases.
- [`backend/docs/phase_2h_b_completion.md`](file:///d:/Projects/Spendsy/backend/docs/phase_2h_b_completion.md): Final verification and documentation report.

### Modified Files:
- [`backend/research/models.py`](file:///d:/Projects/Spendsy/backend/research/models.py): Extend `SearchResult` and `SourceMetadata` with optional quality fields if needed without breaking schema contracts.
- [`backend/research/provider.py`](file:///d:/Projects/Spendsy/backend/research/provider.py): Integrate `SearchNormalizer` into `CompositeResearchProvider`.
- [`backend/research/__init__.py`](file:///d:/Projects/Spendsy/backend/research/__init__.py): Export `SearchNormalizer` and normalization utilities.

---

## 5. Exact Tests Added (`test_search_normalization.py`)
1. Exact duplicate URL deduplication
2. Trailing slash duplicate normalization
3. Tracking parameter duplicate stripping (`utm_source`, `fbclid`, etc.)
4. Different meaningful query parameters preservation (`?page=1` vs `?page=2`)
5. Different paths on same domain preservation (`/home-loan` vs `/personal-loan`)
6. Malformed URL handling
7. Missing title handling (fallback to `"Untitled"`)
8. Missing snippet handling (empty string)
9. Missing domain handling (extracted from URL)
10. Extremely long title truncation
11. Extremely long snippet truncation
12. Invalid rank re-indexing
13. Missing publication date handling
14. Duplicate result with better metadata merge
15. Empty result set handling
16. Result limit enforcement (e.g. max_results=3 on 5 items)
17. Excessive results bounding
18. Empty search query validation
19. Whitespace search query validation
20. Excessively long search query validation
21. Normal financial query validation
22. Normal conversational query validation
23. Provider exception wrapping
24. Mixed valid and invalid results filtering
25. Retrieval timestamp preservation

---

## 6. Architectural Non-Duplication Statement
`SearchNormalizer` does NOT duplicate search engine scraping or HTTP networking. It acts purely as a stateless, functional quality transformer between raw search provider outputs and clean `SearchResult` collections, enabling future research planners (Phase 2H-C onwards) to operate on guaranteed clean, deduplicated, and bounded inputs.
