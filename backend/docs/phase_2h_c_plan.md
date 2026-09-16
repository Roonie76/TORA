# TORA Phase 2H-C Plan: Web Fetch & Evidence Extraction

## 1. Executive Summary & Objective
Phase 2H-C bridges raw search result URLs to structured, provenance-backed research evidence.
The pipeline operates as:
```
User Query
    ↓
Search Provider (DuckDuckGo / Tavily)
    ↓
SearchNormalizer (Deduplication, URL Canonicalization, Tracking Stripped)
    ↓
Selected Source URL
    ↓
HTTPFetchProvider (Deep SSRF Defense, Redirect Inspection, 2MB Bounds)
    ↓
ContentExtractor (HTML Parsing, Script/Style Removal, Structural Text)
    ↓
FetchedPage (Typed Page Model)
    ↓
EvidenceExtractor (Deterministic Passage & Financial Claim Extraction)
    ↓
ResearchEvidence (Provenance-Preserved Claims with Exact Financial Semantics)
```

---

## 2. Existing Transport & Security Architecture (Reused)
The existing `HTTPFetchProvider` in [`backend/tools/web_fetch/provider/http.py`](file:///d:/Projects/Spendsy/backend/tools/web_fetch/provider/http.py) is reused without weakening:
1. **SSRF Protections:**
   - Blocks loopback (`127.0.0.1`, `localhost`, `::1`), private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`), link-local (`169.254.0.0/16`, `fe80::/10`), and cloud metadata (`169.254.169.254`, `metadata.google.internal`).
   - Async DNS resolution verifies the resolved IP before opening TCP sockets.
2. **Redirect Safety:**
   - Step-by-step redirect validation re-verifies scheme, hostname, and DNS IP on every hop (max 5 redirects).
3. **Response Limits & Content Types:**
   - 2MB hard response streaming limit.
   - Whitelist enforcement for `text/html` and `application/xhtml+xml`.
4. **HTML Content Extraction:**
   - [`backend/tools/web_fetch/extractor.py`](file:///d:/Projects/Spendsy/backend/tools/web_fetch/extractor.py) strips scripts, styles, forms, navbars, and extracts structured headings, lists, tables, and paragraphs.

---

## 3. Phase 2H-C Additions

### A. Strongly Typed Domain Models (`backend/research/models.py`)
1. **`FetchedPage`**:
   - `url`, `canonical_url`, `title`, `domain`, `status_code`, `content_type`, `retrieved_at`, `text_content`, `truncated`, `extraction_status`, `metadata: SourceMetadata`, `success`, `error`.
2. **`ExtractedClaim`**:
   - `claim_text`: The extracted factual statement.
   - `supporting_text`: The surrounding contextual passage from the page.
   - `source_url`, `source_title`, `source_domain`: Complete origin provenance.
   - `retrieved_at`: UTC ISO-8601 timestamp.
   - `confidence`: Extraction confidence score.
   - `financial_entities`: Extracted rates, amounts, currencies, tenures, and qualifier terms.
   - `extraction_type`: Mode indicator (`deterministic_passage`).
3. **`ResearchEvidence`**:
   - Top-level container aggregating all claims extracted from a page or query, retaining complete source provenance and failure state.

### B. Deterministic Evidence Extractor (`backend/research/evidence.py`)
- `EvidenceExtractor`:
  - Splits clean page text into semantic paragraphs and sentences.
  - Scores and matches passages against user search queries and financial keywords (`%`, `₹`, `loan`, `rate`, `deposit`, `tenure`, `p.a.`, `APR`, `starting from`, `repo`).
  - **Strict Financial Semantics Preservation:**
    - Currencies (`₹`, `$`, `Rs.`, `INR`)
    - Percentages & rates (`8.50%`, `7.25% p.a.`, `36% APR`)
    - Amounts (`₹1,50,000`, `₹1.5 lakh`, `₹10 crore`)
    - Qualifier terms (`starting from`, `up to`, `as of`, `subject to`, `minimum`, `maximum`)
    - Never mutates "rates starting from 8.5%" into "the rate is 8.5%".
  - If no relevant passages match, returns `claims=[]` with `"No relevant evidence found"` indicator.

### C. Unified Research API (`backend/research/provider.py`)
- Extends `ResearchProvider` with `fetch_and_extract(url: str, query: str = "", max_chars: int = 3000) -> ResearchEvidence`.
- Coordinates safe fetching via `HTTPFetchProvider` $\rightarrow$ `FetchedPage` $\rightarrow$ `EvidenceExtractor` $\rightarrow$ `ResearchEvidence`.

---

## 4. Prompt Injection & Memory Isolation Invariants
1. **Prompt Injection Defense:** Webpage text is treated strictly as passive data inside `ResearchEvidence` and never executed as prompt instructions.
2. **Financial Memory Isolation:** Extracted external claims never modify user-authoritative profile memory (`FinancialProfile` / `FinancialContext`).

---

## 5. Files to Create and Modify

### Files to Create:
- [`backend/research/evidence.py`](file:///d:/Projects/Spendsy/backend/research/evidence.py): Deterministic `EvidenceExtractor` and financial entity parser.
- [`backend/docs/phase_2h_c_plan.md`](file:///d:/Projects/Spendsy/backend/docs/phase_2h_c_plan.md): Architectural specification and plan.
- [`backend/tests/test_evidence_extraction.py`](file:///d:/Projects/Spendsy/backend/tests/test_evidence_extraction.py): 30+ unit tests covering all required scenarios.
- [`backend/docs/phase_2h_c_completion.md`](file:///d:/Projects/Spendsy/backend/docs/phase_2h_c_completion.md): Final completion documentation.

### Files to Modify:
- [`backend/research/models.py`](file:///d:/Projects/Spendsy/backend/research/models.py): Add `FetchedPage`, `ExtractedClaim`, `ResearchEvidence`.
- [`backend/research/provider.py`](file:///d:/Projects/Spendsy/backend/research/provider.py): Add `fetch_and_extract` to `ResearchProvider` & `CompositeResearchProvider`.
- [`backend/research/factory.py`](file:///d:/Projects/Spendsy/backend/research/factory.py): Wire `EvidenceExtractor` dependency.
- [`backend/research/__init__.py`](file:///d:/Projects/Spendsy/backend/research/__init__.py): Export new evidence models and extractor.
