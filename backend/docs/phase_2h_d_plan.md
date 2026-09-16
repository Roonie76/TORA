# TORA Phase 2H-D Plan: Source Verification & Credibility Engine

## 1. Architectural Inspection & Answers

### 1. How SearchResult is represented:
In [`backend/research/models.py`](file:///d:/Projects/Spendsy/backend/research/models.py), `SearchResult` is a Pydantic model tracking `query`, `title`, `url`, `domain`, `snippet`, `rank`, `metadata: SourceMetadata`, `success`, and `error`.

### 2. How FetchedPage is represented:
`FetchedPage` contains `url`, `canonical_url`, `title`, `domain`, `status_code`, `content_type`, `retrieved_at`, `text_content`, `truncated`, `extraction_status`, `metadata: SourceMetadata`, `success`, and `error`.

### 3. How ExtractedClaim is represented:
`ExtractedClaim` contains `claim_text`, `supporting_text`, `source_url`, `source_title`, `source_domain`, `retrieved_at`, `confidence`, `financial_entities: Dict[str, Any]`, and `extraction_type`.

### 4. How ResearchEvidence is represented:
`ResearchEvidence` aggregates `query`, `source_url`, `source_title`, `source_domain`, `retrieved_at`, `claims: List[ExtractedClaim]`, `page_summary`, `raw_content_truncated`, `success`, and `error`.

### 5. How provenance is preserved:
Every claim and page retains origin URL, canonical normalized URL, domain hostname, page title, full supporting passage text, and UTC retrieval timestamp.

### 6. How retrieved_at is represented:
ISO-8601 UTC string (`YYYY-MM-DDTHH:MM:SS.mmmmmm+00:00`).

### 7. How evidence reaches ToolContext:
`ToolExecutor.execute_step()` runs `WebSearchTool` or `WebFetchTool` and attaches `ToolResult` to `ToolContext.results`.

### 8. How tool results reach ContextBuilder:
`ContextBuilder._render_tool_result()` serializes `ToolResult` into readable markdown data sections within the structured LLM message context.

### 9. How ToraAgent executes research tools:
`ToraAgent.run()` plans tool steps via `Planner.plan()`, executes them via `ToolExecutor`, builds contextual messages via `ContextBuilder.build()`, and invokes LLM chat completion.

### 10. Existing credibility functionality:
`SourceMetadata.is_verified: bool` was present as a placeholder, but no authority tiers, domain verification engines, freshness models, conflict detectors, or qualifier evaluators existed.

---

## 2. Phase 2H-D Architectural Design

### A. Core Models (`backend/research/models.py`)
1. **Enumerations**:
   - `SourceType`: `PRIMARY_OFFICIAL`, `REGULATOR`, `BANK_NBFC`, `GOVERNMENT`, `ESTABLISHED_FINANCIAL_MEDIA`, `SECONDARY_AGGREGATOR`, `GENERIC_BLOG`, `UNVERIFIED_FORUM`, `UNKNOWN`
   - `AuthorityLevel`: `VERY_HIGH`, `HIGH`, `MEDIUM_HIGH`, `MEDIUM`, `LOW`, `VERY_LOW`
   - `VerificationStatus`: `VERIFIED_PRIMARY`, `VERIFIED_SECONDARY`, `CORROBORATED`, `CONFLICTING`, `UNVERIFIED`, `STALE`, `INSUFFICIENT_EVIDENCE`, `REJECTED`
   - `FreshnessStatus`: `CURRENT`, `RECENT`, `AGING`, `STALE`, `UNKNOWN`
   - `ConfidenceLevel`: `VERY_HIGH`, `HIGH`, `MEDIUM`, `LOW`, `VERY_LOW`

2. **Domain Models**:
   - `SourceCredibility`: Structured assessment of a domain/URL (authority, credibility score [0.0–1.0], source type, freshness, suitability for financial claims, reasons, warnings, is_primary, is_official).
   - `VerifiedClaim`: Binds an `ExtractedClaim` to its `SourceCredibility`, `VerificationStatus`, `ConfidenceLevel`, preserved `qualifiers`, and `effective_date`.
   - `ClaimConflict`: Represents detected divergence between multiple claims for the same subject with preferred claim selection and rationale.
   - `VerifiedResearchEvidence`: Complete verified research package aggregating sources, verified claims, conflicts, and overall confidence.

---

### B. Source Classification Engine (`backend/research/credibility.py`)
- `SourceVerifier` / `CredibilityEngine`:
  - **Regulators & Government (`VERY_HIGH` authority, `REGULATOR`/`GOVERNMENT` type)**:
    - Exact domain matching for `rbi.org.in`, `sebi.gov.in`, `irdai.gov.in`, `pfrda.org.in`, `incometax.gov.in`, `finmin.nic.in`, `*.gov.in`, `*.nic.in`.
  - **Official Banks & Regulated NBFCs (`HIGH` authority, `BANK_NBFC`/`PRIMARY_OFFICIAL` type)**:
    - Exact domain matching for `sbi.co.in`, `sbi.bank.in`, `hdfcbank.com`, `icicibank.com`, `axisbank.com`, `kotak.com`, `bankofbaroda.in`, `pnbindia.in`, `canarabank.com`, `unionbankofindia.co.in`, `*.bank.in`, etc.
  - **Established Financial Media (`MEDIUM_HIGH`/`MEDIUM` authority, `ESTABLISHED_FINANCIAL_MEDIA` type)**:
    - `moneycontrol.com`, `economictimes.indiatimes.com`, `livemint.com`, `business-standard.com`, `financialexpress.com`, `thehindubusinessline.com`, `bloomberg.com`, `reuters.com`.
  - **Financial Aggregators (`MEDIUM`/`LOW` authority, `SECONDARY_AGGREGATOR` type)**:
    - `bankbazaar.com`, `paisabazaar.com`, `cleartax.in`, `policybazaar.com`.
  - **Lookalike & Squatting Defense**:
    - Lookalike domains (e.g. `rbi-circulars.com`, `sbi-loans-offers.com`, `sebi-updates.net`) are strictly classified as `UNKNOWN` or `UNVERIFIED_BLOG` and flagged with security warnings.
  - **Deterministic Score Calculation**:
    - Based on authority tier, HTTPS validity, first-party data presence, and domain reputation. Zero LLM hallucinations.

---

### C. Freshness & Qualifier Preservation
1. **Freshness Assessment**:
   - `CURRENT`: Published/updated within last 30 days.
   - `RECENT`: Published within 30–180 days.
   - `AGING`: Published within 180–365 days.
   - `STALE`: Published > 365 days ago.
   - `UNKNOWN`: Missing publication date. Retrieval timestamp is strictly decoupled from publication date.
2. **Qualifier Preservation**:
   - Explicitly preserves: `starting from`, `up to`, `minimum`, `maximum`, `subject to eligibility`, `subject to applicable terms`, `effective from`, `valid until`, `indicative`, `approximate`, `promotional`.

---

### D. Conflict Detection
- `ConflictDetector` inside `CredibilityEngine`:
  - Compares verified claims across multiple sources for the same query/rate entity.
  - Detects diverging interest rates (e.g. 7.25% vs 8.10%), amounts, or effective dates.
  - Generates a `ClaimConflict` prioritizing primary official sources over secondary aggregators, while preserving all conflicting claims in evidence context without silent deletion.

---

### E. Research Provider & Context Integration
- `CompositeResearchProvider` extends `verify_source()`, `verify_claims()`, and `fetch_extract_and_verify()`.
- `ContextBuilder` renders verified claims with authority, verification status, qualifiers, and citations.

---

## 3. Files Created & Modified
- **Create:** `backend/research/credibility.py` (Classification, verification, freshness, conflict engine).
- **Create:** `backend/tests/test_source_credibility.py` (56+ unit tests).
- **Create:** `backend/docs/phase_2h_d_plan.md` (This document).
- **Create:** `backend/docs/phase_2h_d_completion.md` (Completion documentation).
- **Modify:** `backend/research/models.py` (Add credibility & verification models).
- **Modify:** `backend/research/provider.py` (Wire verification into provider).
- **Modify:** `backend/research/factory.py` (Wire `SourceVerifier`).
- **Modify:** `backend/research/__init__.py` (Export credibility models & classes).
- **Modify:** `backend/context/builder.py` (Render verified research evidence).

---

## 4. Explicit Phase Boundary
- ❌ Phase 2H-E (Multi-Source Synthesis & LLM Corroboration Loop) is NOT implemented in 2H-D.
- ❌ Phase 2H-F (Autonomous Deep Research Loop) is NOT implemented in 2H-D.
- ❌ Financial decision / investment recommendation logic is NOT implemented in 2H-D.
