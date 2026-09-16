# TORA PHASE 2H-C — WEB FETCH & EVIDENCE EXTRACTION COMPLETION REPORT

## 1. Executive Summary
Phase 2H-C establishes the structured **Web Fetch & Evidence Extraction Layer** in TORA's research subsystem. Building directly on the Phase 2H-A provider abstraction and Phase 2H-B search normalizer, Phase 2H-C enables safe HTTP page retrieval, structured plain-text parsing, and deterministic extraction of provenanced factual claims with exact financial semantic preservation.

---

## 2. Architecture Before vs. After Phase 2H-C

### Before Phase 2H-C:
```
User Query ──> Search Provider ──> SearchNormalizer ──> Clean SearchResult[] (Unfetched URLs)
```

### After Phase 2H-C:
```
User Query
    │
    ▼
Search Provider (DuckDuckGo / Tavily)
    │
    ▼
SearchNormalizer (Canonicalization, Tracking Removal, Deduplication)
    │
    ▼
Selected Source URL
    │
    ▼
HTTPFetchProvider (Deep SSRF Security, Redirect Inspection, 2MB Bounds)
    │
    ▼
ContentExtractor (HTML Parsing, Script/Style Deletion, Structural Markdown)
    │
    ▼
FetchedPage (Strongly Typed Page Model with HTTP Status & Headers)
    │
    ▼
EvidenceExtractor (Deterministic Passage Relevance & Financial Entity Extraction)
    │
    ▼
ResearchEvidence (Provenance-Preserved Claims with Exact Numbers, Rates, & Qualifiers)
```

---

## 3. Files Created & Modified

### Created Files:
- [`backend/research/evidence.py`](file:///d:/Projects/Spendsy/backend/research/evidence.py): Deterministic `EvidenceExtractor` and `extract_financial_entities` parser.
- [`backend/docs/phase_2h_c_plan.md`](file:///d:/Projects/Spendsy/backend/docs/phase_2h_c_plan.md): Step 1 architecture and design document.
- [`backend/tests/test_evidence_extraction.py`](file:///d:/Projects/Spendsy/backend/tests/test_evidence_extraction.py): 30 unit tests covering all required functional, security, and financial scenarios.
- [`backend/scratch/live_evidence_extraction_test.py`](file:///d:/Projects/Spendsy/backend/scratch/live_evidence_extraction_test.py): Live web fetching, evidence extraction, and SSRF security validation script.
- [`backend/docs/phase_2h_c_completion.md`](file:///d:/Projects/Spendsy/backend/docs/phase_2h_c_completion.md): Final verification and completion documentation.

### Modified Files:
- [`backend/research/models.py`](file:///d:/Projects/Spendsy/backend/research/models.py): Added `FetchedPage`, `ExtractedClaim`, and `ResearchEvidence` models.
- [`backend/research/provider.py`](file:///d:/Projects/Spendsy/backend/research/provider.py): Added `fetch_page` and `fetch_and_extract` to `ResearchProvider` ABC and `CompositeResearchProvider`.
- [`backend/research/factory.py`](file:///d:/Projects/Spendsy/backend/research/factory.py): Added `evidence_extractor` dependency to `get_research_provider()`.
- [`backend/research/__init__.py`](file:///d:/Projects/Spendsy/backend/research/__init__.py): Exported Phase 2H-C models and functions.

---

## 4. Reused Fetch & Security Implementation

Phase 2H-C strictly reuses [`backend/tools/web_fetch/provider/http.py`](file:///d:/Projects/Spendsy/backend/tools/web_fetch/provider/http.py) without duplicating HTTP transport:
1. **SSRF Protections:**
   - Loopback (`127.0.0.1`, `localhost`, `::1`, `0.0.0.0`)
   - Private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`)
   - Link-local & cloud metadata (`169.254.169.254`, `metadata.google.internal`, `fe80::/10`)
   - Scheme enforcement (strict `http` and `https` whitelist; rejects `file:`, `javascript:`, `data:`, `ftp:`)
   - Async DNS resolution pre-flight validation preventing DNS rebinding.
2. **Redirect Safety:**
   - Step-by-step redirect validation re-verifying hostname, scheme, and IP on every redirect hop (up to 5 max).
3. **Response Bounds & Content Filtering:**
   - 2MB streaming byte limit preventing memory exhaustion.
   - Content-Type enforcement (`text/html`, `application/xhtml+xml`).

---

## 5. HTML Content Extraction & Cleaning

- [`backend/tools/web_fetch/extractor.py`](file:///d:/Projects/Spendsy/backend/tools/web_fetch/extractor.py) parses HTML using BeautifulSoup with a regex fallback.
- Strips `<script>`, `<style>`, `<noscript>`, `<nav>`, `<header>`, `<footer>`, `<svg>`, `<form>`, `<aside>`, `<iframe>`, and comments.
- Formats structural Markdown: `### Heading`, `- List items`, `cell | cell` for tables, and clean paragraphs.
- Bounded text extraction with character budget limits (`max_chars`) and `truncated = True` tracking.

---

## 6. Deterministic Evidence Extraction & Financial Number Preservation

`EvidenceExtractor` (`backend/research/evidence.py`):
1. **Semantic Passage Segmentation:** Splits clean text into coherent blocks/paragraphs.
2. **Exact Numerical Entity Preservation:**
   - **Percentages:** `8.50%`, `7.25% p.a.`, `36% APR`, `2.50% p.a.`
   - **Currencies & Amounts:** `₹1,50,000`, `₹1.5 lakh`, `₹10 crore`, `Rs. 5,000`, `$500`
   - **Qualifiers:** `starting from`, `up to`, `effective from`, `as of`, `subject to`, `minimum`, `maximum`
3. **Semantic Integrity:** Terms such as `"starting from 8.50% p.a."` are never mutated or rounded into unconditional assertions like `"the rate is 8.5%"`.
4. **Relevance Scoring:** Whole-word regex matching matches user query terms and scores passages by financial keyword and entity density.
5. **No Hallucination Fallback:** If zero relevant passages match, returns `claims=[]` with explicit `"No relevant evidence found"` indicator.

---

## 7. Provenance Model

Every `ExtractedClaim` includes:
- `source_url`: Canonical source URL.
- `source_title`: Document title.
- `source_domain`: Extracted host domain.
- `retrieved_at`: UTC ISO-8601 timestamp.
- `supporting_text`: Complete sentence/paragraph context.
- `financial_entities`: Structured dictionary of preserved rates, amounts, and qualifiers.

---

## 8. Prompt Injection & Memory Isolation Guarantees

1. **Prompt Injection Defense:** Webpage text is treated strictly as passive data inside `ResearchEvidence` and never executed as prompt instructions.
2. **Financial Memory Isolation:** Extracted external claims never modify user-authoritative profile memory (`FinancialProfile` / `FinancialContext`).

---

## 9. Test Summary & Regression Baseline

| Test Suite | Total Tests | Passed | Failed | Skipped |
|---|---|---|---|---|
| `test_evidence_extraction.py` (New Phase 2H-C) | 30 | 30 | 0 | 0 |
| `test_search_normalization.py` (Phase 2H-B) | 26 | 26 | 0 | 0 |
| `test_research_foundation.py` (Phase 2H-A) | 20 | 20 | 0 | 0 |
| All Existing TORA Tests (Phase 1 through 2G.1) | 354 | 354 | 0 | 0 |
| **Complete Pytest Suite** | **430** | **430** | **0** | **0** |

---

## 10. Live Verification Results

### Live Security & SSRF Tests
- `http://localhost:8000/api/health` $\rightarrow$ **Blocked** (`FetchSSRFError`)
- `http://127.0.0.1:8000/api/health` $\rightarrow$ **Blocked** (`FetchSSRFError`)
- `http://169.254.169.254/latest/meta-data/` $\rightarrow$ **Blocked** (`FetchSSRFError`)
- `http://10.0.0.1/admin` $\rightarrow$ **Blocked** (`FetchSSRFError`)
- `file:///c:/windows/win.ini` $\rightarrow$ **Blocked** (`FetchSSRFError`)
- `javascript:alert(1)` $\rightarrow$ **Blocked** (`FetchSSRFError`)

### Live Public Web Fetch & Evidence Extraction
- Target: `https://sbi.bank.in` (Query: `home loan interest rates`)
  - Successfully retrieved `FetchedPage` (HTTP 200).
  - Extracted 2 structured claims with exact rates: `Interest Rates 7.25%* p.a. onwards w.e.f. 01.04.2026`.
  - Preserved qualifiers and complete provenance metadata.

---

## 11. Known Limitations & Explicit Boundary Confirmation

### Known Limitations
1. **Dynamic HTML / Client-Side Rendering:** Uses standard HTTP GET/POST with HTML parsing; pages requiring JavaScript execution are extracted from initial server-rendered HTML.
2. **Deterministic Passage Extraction:** Uses deterministic keyword/entity scoring rather than heavy LLM claim extraction (reserved for multi-source synthesis in Phase 2H-E).

### Explicit Boundary Confirmation
- ❌ **Phase 2H-D** (Source Verification & Credibility Engine) was **NOT** implemented.
- ❌ **Phase 2H-E** (Multi-Source Synthesis) was **NOT** implemented.
- ❌ **Phase 2H-F** (Autonomous Deep Research Loop) was **NOT** implemented.
- ❌ **Phase 2H-G**, **2H-H**, **2H-I** were **NOT** implemented.

---

### 🟢 **PHASE 2H-C COMPLETE**
