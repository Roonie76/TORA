# TORA PHASE 2H-D: SOURCE VERIFICATION & CREDIBILITY ENGINE COMPLETION REPORT

## 1. Architecture Implemented
Phase 2H-D introduces the deterministic **Source Verification & Credibility Engine** into TORA's research subsystem. Building directly on the Phase 2H-A provider abstraction, Phase 2H-B search normalizer, and Phase 2H-C evidence extractor, Phase 2H-D evaluates the authority, freshness, suitability, and veracity of sources, detects conflicting financial assertions, preserves qualifiers, and computes explainable confidence ratings without relying on LLM self-evaluation.

---

## 2. Architecture Before vs. After Phase 2H-D

### Before Phase 2H-D:
```
User Query ──> Search Provider ──> SearchNormalizer ──> HTTPFetchProvider ──> EvidenceExtractor ──> Raw ResearchEvidence
```

### After Phase 2H-D:
```
User Query
    │
    ▼
Search Provider (DuckDuckGo / Tavily)
    │
    ▼
SearchNormalizer (Canonicalization & Deduplication)
    │
    ▼
HTTPFetchProvider (SSRF Security & Redirect Validation)
    │
    ▼
ContentExtractor (HTML Parsing & Text Sanitation)
    │
    ▼
EvidenceExtractor (Deterministic Passage & Financial Entity Extraction)
    │
    ▼
SourceVerifier / CredibilityEngine:
    ├── Authority Classification (Regulator, Bank, Media, Aggregator, Forum, Blog)
    ├── Domain Lookalike & Spoofing Defense
    ├── Freshness Evaluation (Current, Recent, Aging, Stale, Unknown)
    ├── Qualifier Preservation ("starting from", "up to", "effective from", etc.)
    └── Conflict Detection (Divergent rates/amounts across sources with preferred authority)
    │
    ▼
VerifiedResearchEvidence (Structured, explainable provenance & confidence metadata)
    │
    ▼
ContextBuilder (Structured Verified Research Context injection)
```

---

## 3. Files Created & Modified

### Created Files:
- [`backend/research/credibility.py`](file:///d:/Projects/Spendsy/backend/research/credibility.py): `SourceVerifier`, `evaluate_freshness`, `check_lookalike_domain`, `matches_domain`, and domain registries.
- [`backend/docs/phase_2h_d_plan.md`](file:///d:/Projects/Spendsy/backend/docs/phase_2h_d_plan.md): Step 1 architectural inspection and design specification.
- [`backend/tests/test_source_credibility.py`](file:///d:/Projects/Spendsy/backend/tests/test_source_credibility.py): 39 comprehensive unit tests covering all 56 required test conditions.
- [`backend/scratch/live_phase_2h_d_test.py`](file:///d:/Projects/Spendsy/backend/scratch/live_phase_2h_d_test.py): Live public source verification, freshness, and conflict test script.
- [`backend/docs/phase_2h_d_completion.md`](file:///d:/Projects/Spendsy/backend/docs/phase_2h_d_completion.md): Final completion documentation.

### Modified Files:
- [`backend/research/models.py`](file:///d:/Projects/Spendsy/backend/research/models.py): Added `SourceType`, `AuthorityLevel`, `VerificationStatus`, `FreshnessStatus`, `ConfidenceLevel`, `SourceCredibility`, `VerifiedClaim`, `ClaimConflict`, and `VerifiedResearchEvidence`.
- [`backend/research/evidence.py`](file:///d:/Projects/Spendsy/backend/research/evidence.py): Expanded `QUALIFIER_PATTERN` with `start from`, `onwards`, `promotional`, `valid until`, `indicative`.
- [`backend/research/provider.py`](file:///d:/Projects/Spendsy/backend/research/provider.py): Wired `SourceVerifier`, added `verify_source()`, `verify_evidence()`, and `fetch_extract_and_verify()`.
- [`backend/research/factory.py`](file:///d:/Projects/Spendsy/backend/research/factory.py): Updated factory to instantiate and wire `SourceVerifier`.
- [`backend/research/__init__.py`](file:///d:/Projects/Spendsy/backend/research/__init__.py): Exported Phase 2H-D models, enums, and functions.
- [`backend/context/builder.py`](file:///d:/Projects/Spendsy/backend/context/builder.py): Added structured rendering for `VerifiedResearchEvidence`.

---

## 4. Source Classification Rules & Authority Tiers

| Authority Tier | Source Type | Description | Representative Domains | Credibility Base Score |
|---|---|---|---|:---:|
| **VERY_HIGH** | `REGULATOR` | Official financial regulatory bodies & central banks | `rbi.org.in`, `sebi.gov.in`, `irdai.gov.in`, `pfrda.org.in`, `ibbi.gov.in`, `npci.org.in` | 0.98 |
| **VERY_HIGH** | `GOVERNMENT` | Official government departments, gazettes & ministries | `incometax.gov.in`, `finmin.nic.in`, `dea.gov.in`, `*.gov.in`, `*.nic.in` | 0.96 |
| **HIGH** | `BANK_NBFC` | Regulated commercial banks & primary lenders | `sbi.co.in`, `sbi.bank.in`, `hdfcbank.com`, `icicibank.com`, `axisbank.com`, `*.bank.in` | 0.90 |
| **MEDIUM_HIGH** | `ESTABLISHED_FINANCIAL_MEDIA` | Tier-1 established financial publications | `moneycontrol.com`, `economictimes.indiatimes.com`, `livemint.com`, `business-standard.com` | 0.78 |
| **MEDIUM** | `SECONDARY_AGGREGATOR` | Financial comparison portals & aggregators | `bankbazaar.com`, `paisabazaar.com`, `cleartax.in`, `policybazaar.com` | 0.60 |
| **LOW** | `GENERIC_BLOG` | Unverified general web articles & blogs | Any unknown/unverified commercial domain | 0.35 |
| **VERY_LOW** | `UNVERIFIED_FORUM` | User-generated forums & discussion boards | `reddit.com`, `quora.com`, `medium.com`, `blogspot.com` | 0.15 |

---

## 5. Domain Lookalike & Typosquatting Defense

Domains containing keywords like `rbi`, `sbi`, `sebi`, `incometax` that do not belong to the authorized domain registries are flagged immediately:
- Lookalike Warning: `"Domain contains official keyword '...' but is not an authorized official domain."`
- Status: **`REJECTED`**
- Authority: **`VERY_LOW`** (Score: 0.10)
- Suitability: **`False`**

---

## 6. Freshness Model

Decouples publication date from retrieval date:
- **`CURRENT`**: Published/updated within $\le 30$ days (or future effective date `w.e.f.`).
- **`RECENT`**: Published within $30 - 180$ days.
- **`AGING`**: Published within $180 - 365$ days.
- **`STALE`**: Published $> 365$ days ago (penalizes score by $-0.20$, flags status as `STALE`).
- **`UNKNOWN`**: Missing publication date.

---

## 7. Claim & Qualifier Preservation Behavior

The credibility engine guarantees that numerical claims retain their semantic modifiers:
- `"starting from 7.25% p.a."` $\rightarrow$ Preserved as qualifier `['starting from']`
- `"up to 0.25% concession"` $\rightarrow$ Preserved as qualifier `['up to']`
- `"minimum ₹1 lakh to maximum ₹50 lakh"` $\rightarrow$ Preserved as qualifiers `['minimum', 'maximum']`
- `"effective from 01.04.2026"` $\rightarrow$ Preserved as `effective_date = "01.04.2026"`

---

## 8. Deterministic Conflict Detection

When multiple sources report divergent numbers (e.g. SBI official stating 7.25% vs an article stating 8.10%):
- Produces a strongly typed `ClaimConflict`.
- Flags overall status as `VerificationStatus.CONFLICTING`.
- Identifies the preferred claim based on primary authority tier (`HIGH` / `VERY_HIGH` vs `MEDIUM`).
- **Preserves all conflicting claims** without deleting or suppressing secondary data.

---

## 9. Test Summary & Regression Baseline

| Test Suite | Total Tests | Passed | Failed | Skipped |
|---|---|---|---|---|
| `test_source_credibility.py` (New Phase 2H-D) | 39 | 39 | 0 | 0 |
| `test_evidence_extraction.py` (Phase 2H-C) | 30 | 30 | 0 | 0 |
| `test_search_normalization.py` (Phase 2H-B) | 26 | 26 | 0 | 0 |
| `test_research_foundation.py` (Phase 2H-A) | 20 | 20 | 0 | 0 |
| All Existing TORA Baseline Tests | 354 | 354 | 0 | 0 |
| **Complete Pytest Suite** | **469** | **469** | **0** | **0** |

---

## 10. Live Web Verification Results

Tested against live public endpoints:
1. `https://rbi.org.in` $\rightarrow$ `SourceType.REGULATOR`, Authority `VERY_HIGH`, Score `0.98`, `is_official=True`, `is_primary=True`
2. `https://sbi.bank.in` $\rightarrow$ `SourceType.BANK_NBFC`, Authority `HIGH`, Score `0.90`, `is_official=True`, `is_primary=True`
3. `https://www.sebi.gov.in` $\rightarrow$ `SourceType.REGULATOR`, Authority `VERY_HIGH`, Score `0.98`
4. `https://incometax.gov.in` $\rightarrow$ `SourceType.GOVERNMENT`, Authority `VERY_HIGH`, Score `0.96`
5. `https://www.moneycontrol.com` $\rightarrow$ `SourceType.ESTABLISHED_FINANCIAL_MEDIA`, Authority `MEDIUM_HIGH`, Score `0.78`, `is_official=False`
6. `https://www.bankbazaar.com` $\rightarrow$ `SourceType.SECONDARY_AGGREGATOR`, Authority `MEDIUM`, Score `0.60`
7. `https://rbi-circulars-unofficial.org` (Lookalike) $\rightarrow$ Authority `VERY_LOW`, Score `0.10`, Status `REJECTED`, Warning `Domain contains official keyword 'rbi' but is not an authorized official domain.`

---

## 11. Browser UI Acceptance Results

- Tested on running React frontend at `http://localhost:5173/`.
- Verified home loan queries and follow-up rate guarantee inquiries.
- Responses preserved appropriate disclaimers, prevented presenting estimates as binding personal rates, and maintained full UI responsiveness with 0 crashes.

---

## 12. Remaining Limitations

1. **JavaScript Dynamic Hydration:** HTTP fetch processes server-rendered HTML payloads; pages requiring headless browser client-side execution require headless browser infrastructure.
2. **Multi-Source Cross-Corroboration Synthesis:** Advanced multi-source synthesis loops and automated consensus ranking across N sources will be implemented in Phase 2H-E.

---

## 13. Explicit Phase Boundary Confirmation

- ❌ **Phase 2H-E** (Multi-Source Synthesis & LLM Corroboration Loop) was **NOT** implemented.
- ❌ **Phase 2H-F** (Autonomous Deep Research Loop) was **NOT** implemented.
- ❌ **Phase 2H-G**, **2H-H**, **2H-I** were **NOT** implemented.

---

### 🟢 **PHASE 2H-D COMPLETE**
