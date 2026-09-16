# TORA PHASE 2H-B — SEARCH NORMALIZATION & QUALITY REPORT

## 1. Executive Summary
Phase 2H-B established a dedicated, production-grade **Search Normalization & Quality Layer** for TORA's research subsystem. Building directly upon the Phase 2H-A provider abstraction, Phase 2H-B introduces deterministic URL canonicalization, tracking parameter stripping, metadata-preserving duplicate merging, text/HTML sanitization, robust malformed source defense, and query validation.

---

## 2. What Changed
- Extracted and centralized search result normalization into a standalone, provider-agnostic `SearchNormalizer` component.
- Implemented parameter-aware URL normalization that strips 18+ common telemetry/marketing tags (`utm_*`, `fbclid`, `gclid`, etc.) while strictly retaining functional query parameters (`page`, `id`, `category`, etc.).
- Built metadata-preserving duplicate detection that merges multiple occurrences of canonical URLs while retaining richer snippets, descriptive titles, earlier ranks, and publication timestamps.
- Added comprehensive text sanitization, HTML entity decoding, and clean word-boundary truncation for titles and snippets.
- Expanded the test suite with 26 new unit tests, bringing total test count to **400 passing tests (100% green)**.

---

## 3. Files Created & Modified

### Created Files:
- [`backend/research/normalization.py`](file:///d:/Projects/Spendsy/backend/research/normalization.py): Dedicated `SearchNormalizer` engine, URL canonicalization, HTML entity unescaping, text sanitization, and query validation.
- [`backend/docs/phase_2h_b_plan.md`](file:///d:/Projects/Spendsy/backend/docs/phase_2h_b_plan.md): Step 1 architecture and non-duplication plan.
- [`backend/tests/test_search_normalization.py`](file:///d:/Projects/Spendsy/backend/tests/test_search_normalization.py): 26 comprehensive unit tests covering all quality scenarios.
- [`backend/scratch/live_search_quality_test.py`](file:///d:/Projects/Spendsy/backend/scratch/live_search_quality_test.py): Live provider validation script.

### Modified Files:
- [`backend/research/models.py`](file:///d:/Projects/Spendsy/backend/research/models.py): Added `model_validator(mode="before")` for domain auto-population.
- [`backend/research/provider.py`](file:///d:/Projects/Spendsy/backend/research/provider.py): Integrated `SearchNormalizer` inside `CompositeResearchProvider`.
- [`backend/research/factory.py`](file:///d:/Projects/Spendsy/backend/research/factory.py): Added `normalizer` argument to `get_research_provider()`.
- [`backend/research/__init__.py`](file:///d:/Projects/Spendsy/backend/research/__init__.py): Exported `SearchNormalizer` and normalization utilities.

---

## 4. Architecture

```
Raw Search Provider (DuckDuckGo / Tavily)
                 │
                 ▼
     [CompositeResearchProvider]
                 │
                 ▼
        [SearchNormalizer]
        ├─ validate_search_query()
        ├─ normalize_url() (Tracking stripped, ports removed, params sorted)
        ├─ clean_text() (HTML entities unescaped, whitespace collapsed)
        ├─ _merge_duplicate_results() (Best title, longest snippet, timestamps preserved)
        └─ Rank Consecutive Re-indexing (1..N) & Result Capping
                 │
                 ▼
     Clean List[SearchResult] in SearchResponse
                 │
                 ▼
     Future Research Planner / ToraAgent
```

---

## 5. Normalization Rules

1. **Scheme & Host:** Lowercased (`HTTPS://SBI.CO.IN` $\rightarrow$ `https://sbi.co.in`).
2. **Default Ports:** Stripped (`:80` on HTTP, `:443` on HTTPS).
3. **Paths:** Collapsed redundant slashes (`//personal//loans` $\rightarrow$ `/personal/loans`). Trailing slash stripped unless path is root `/`.
4. **Fragments:** Anchors (`#section-1`) stripped for canonical deduplication.
5. **Text Formatting:** HTML entities (`&amp;`, `&#39;`, `&quot;`, `&#8377;`) decoded to plain text (`&`, `'`, `"`, `₹`).
6. **Title / Snippet Bounds:** Excessively long titles bounded to 250 chars; snippets bounded to 1500 chars with word-boundary ellipsis.

---

## 6. Deduplication Rules

1. **Exact Matches:** Identical canonical URLs are collapsed.
2. **Derived Equivalence:** URLs differing only by trailing slashes, default ports, or tracking parameters are identified as the same resource.
3. **Metadata Preservation on Merge:**
   - **Title:** Primary rank title is kept unless it is a placeholder (`"Untitled"`), or secondary title is substantially more descriptive.
   - **Snippet:** Longer, more informative snippet is preserved.
   - **Rank:** Highest (earliest) rank is preserved.
   - **Timestamps / Author:** Valid publication date and author metadata are merged.
4. **Distinct Path Isolation:** Different paths or semantic query parameters on the same domain (e.g. `/home-loan` vs `/personal-loan`, `?page=1` vs `?page=2`) are strictly preserved as distinct results.

---

## 7. Query Validation Rules

- **Allowed:** Natural language questions, financial rate queries, mixed casing, standard punctuation.
- **Rejected:** Empty strings, whitespace-only strings, queries exceeding 500 characters.
- **Sanitization:** Unprintable control characters (`\x00-\x1f`) are stripped.

---

## 8. Result Limits & Bounding

- **Configurable Default Limit:** `DEFAULT_MAX_RESULTS = 5`.
- **Hard Maximum Guard:** `HARD_MAX_RESULTS = 20`.
- **Edge Cases Verified:** 0 results, 1 result, exact limit, and over-limit inputs all return clean, consecutively ranked items (1..N).

---

## 9. Error Handling & Malformed Source Defense

- **Missing Title:** Automatically falls back to `"Untitled"` without throwing exceptions.
- **Missing Snippet:** Defaulted to empty string.
- **Missing Domain:** Derived automatically from canonical URL.
- **Invalid Scheme / No Protocol:** Filtered out safely during normalization.
- **Provider Exceptions:** Wrapped cleanly in typed `ResearchSearchError` or `ResearchProviderError`.
- **Zero Hallucination on Failure:** If live search fails (e.g. network timeout or rate limit), TORA explicitly acknowledges the lack of verified real-time rates and provides official bank contact steps without fabricating numbers.

---

## 10. Test Summary & Regression Baseline

| Test Suite | Total Tests | Passed | Failed | Skipped |
|---|---|---|---|---|
| `test_search_normalization.py` (New Phase 2H-B) | 26 | 26 | 0 | 0 |
| `test_research_foundation.py` (Phase 2H-A) | 20 | 20 | 0 | 0 |
| All Existing TORA Tests (Phase 1 through 2G.1) | 354 | 354 | 0 | 0 |
| **Complete Pytest Suite** | **400** | **400** | **0** | **0** |

---

## 11. Live Search & Browser Acceptance Results

### Live Test Queries Executed
1. *"What are the current SBI home loan rates?"* $\rightarrow$ Succeeded with 5 normalized, deduplicated results, UTC timestamps, and clean domains.
2. *"Compare current SBI and HDFC home loan rates."* $\rightarrow$ Succeeded with 5 deduplicated comparison sources.
3. *Rate Limit Failure Simulation:* DuckDuckGo HTTP 202 bot-challenge caught cleanly, wrapped in `ResearchSearchError`, and handled gracefully by TORA with zero crashes and zero hallucinated numbers.

### Browser UI Verification
- UI loaded cleanly at `http://localhost:5173/`.
- Planner recognized need for current rates and produced `web_search` plan.
- TORA handled live responses gracefully without exposing raw tracking URLs, internal tool stacks, or system prompt leaks.

---

## 12. Known Limitations & Explicit Boundary Confirmation

### Known Limitations
1. **Dynamic Rate Limit Backoff:** Public scraping providers (like DuckDuckGo HTML) are subject to external IP rate limits; production deployments should configure Tavily API key or dedicated search endpoints.
2. **Source Authority Scoring:** Assigning explicit domain trust scores (e.g. government/RBI vs blog) is deferred to Phase 2H-D.

### Boundary Confirmation
- ❌ **Phase 2H-C** (Multi-Query Research Planner) was **NOT** implemented.
- ❌ **Phase 2H-D** (Source Verification & Credibility Engine) was **NOT** implemented.
- ❌ **Phase 2H-E** (Multi-Source Synthesis) was **NOT** implemented.
- ❌ **Phase 2H-F** (Autonomous Deep Research Loop) was **NOT** implemented.
- ❌ **Phase 2H-G**, **2H-H**, **2H-I** were **NOT** implemented.

---

### 🟢 **PHASE 2H-B COMPLETE**
