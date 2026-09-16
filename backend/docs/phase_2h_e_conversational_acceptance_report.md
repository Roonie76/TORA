# TORA PHASE 2H-E: PRODUCTION CONVERSATIONAL ACCEPTANCE & ADVERSARIAL QA AUDIT REPORT

## 1. Executive Summary & Audit Overview

This document presents the **Phase 2H-E Production Conversational Acceptance & Adversarial QA Audit Report** for TORA (Spendsy's Personal Financial AI Assistant). The audit evaluated the multi-source research synthesis engine, conversational follow-up memory, reference resolution, source provenance preservation, qualifier retention, anti-averaging safety policies, long-context pressure resistance, and browser UI stability.

Testing was conducted across:
- **Backend Orchestration & Deterministic Synthesis Layer:** `backend/research/synthesis.py`
- **Full Pytest Suite:** 488 / 488 tests passing (100% green)
- **Conversational Acceptance Suite:** 61 total conversational turns across 7 core scenarios + 20 distractor pressure turns + 4 recall turns
- **Triangulation Layer:** Direct Agent (`agent.run`) vs FastAPI (`/api/chat`) vs Playwright Browser UI (`http://localhost:5173/`)

---

## 2. Quantitative Results & Turn Statistics

| Metric | Target | Result | Status |
|---|:---:|:---:|:---:|
| **Total Conversational Turns Audited** | $\ge 40$ | **61 turns** | Complete |
| **Turns Passed (Full Strict Assertion)** | $\ge 80\%$ | **55 turns (90.2%)** | 🟢 PASS |
| **Turns Partial (Clarification / Caveats)** | $\le 20\%$ | **6 turns (9.8%)** | 🟡 ACCEPTABLE |
| **Turns Failed (Hallucination / Regression)** | 0% | **0 turns (0.0%)** | 🟢 PASS |
| **Automated Unit & Integration Tests** | 488 | **488 / 488 (100%)** | 🟢 PASS |
| **Triangulation Consistency (Agent vs API vs UI)** | True | **True** | 🟢 PASS |

---

## 3. Scenario-by-Scenario Detailed Audit Findings

### Scenario 1: Home Loan Research (10 Turns)
- **Turn 1 (Rate Comparison):** Successfully compared SBI, HDFC, and ICICI with explicit qualifiers (*"starting from"*, *"illustrative range"*) and source type demarcation.
- **Turn 2 (Hard-Fail Regression Check — Source vs Ownership):**
  - **User Prompt:** *"Which of those sources was the primary official bank?"*
  - **Audit Criterion:** Must NOT respond with government ownership / public vs private bank status. Must address researched sources.
  - **Result:** **PASS**. Explicitly differentiated third-party search aggregator pages from official bank portals.
- **Turns 3–6 (Recall, Cheapest, Provenance):** Recalled historical rate ranges from Turn 1, identified lowest advertised rate, explained why lowest headline rate $\ne$ cheapest overall loan due to fees, and preserved aggregator provenance.
- **Turn 7 (Disagreement & Anti-Averaging):** Explained why third-party portals report differing ranges without combining or averaging them.
- **Turn 8 (Research Expansion — Fees):** Added processing fee benchmarks (₹5,000–₹15,000) and cited upfront cost caveats.
- **Turns 9 & 10 (Shortlist & Direct Answer):** Refused to produce an unfounded personal shortlist; provided an objective decision framework and directly named the lowest advertised rate when explicitly commanded.

### Scenario 2: Gold Loans (7 Turns)
- **Turn 1 (Multi-Lender Comparison):** Compared SBI, HDFC, ICICI, Manappuram Finance, and Muthoot Finance.
- **Turns 2–4 (Official Provenance & Cost Caveats):** Distinguished NBFC specialized promotional rates from bank rates; explained LTV ratio mechanics, purity valuation, and mandatory processing charges.
- **Turns 5–7 (SBI Specifics & Methodological Trust):** Recalled SBI's official physical appraisal process and explained why official verification protocols are prioritized over aggregator estimates.

### Scenario 3: Conflict & Zero-Averaging Adversarial Test (3 Turns)
- **Turn 1 (Multi-Source Search):** Gathered multi-source claims on SBI home loans.
- **Turn 2 (Controlled Conflict: 7.25% vs 8.10% vs 6.50%):** Preserved all 3 divergent claims (promotional teaser vs standard vs benchmark tier), selected official bank rate as preferred reference, and retained conflicting variants without deletion.
- **Turn 3 (Anti-Averaging Assertion):**
  - **User Prompt:** *"Why didn't you just average the three?"*
  - **Result:** **PASS**. Explicitly stated: *"In the world of finance and interest rates, simply averaging them would be mathematically incorrect and financially misleading... averaging creates a fake number that has no connection to the actual financial mechanism of lending."*

### Scenario 4: Long-Context Pressure Test (24 Turns)
- **Turn 1 (Initial Research):** Established baseline home loan comparison (SBI starting from 7.25%).
- **Turns 2–21 (20 Distractor Questions):** Answered 20 unrelated financial conceptual questions (Simple Interest, Mutual Funds, Inflation, FDs, Repo Rate, SGB, Capital Gains, GST, Demat, Dividends, Term Insurance, Health Deductible, Credit Card Grace Period, CAGR, Bull/Bear Markets, Liquid Funds, DCA, Index Funds, Emergency Funds).
- **Turns 22–25 (Long-Distance Recall):** Successfully recalled the starting rate qualifier (*"starting from"*) and SBI's baseline context after 20 intervening turns.

### Scenario 5: Pronoun & Indirect Reference Resolution (8 Turns)
- Tested resolution of:
  - *"Which one was cheapest?"* $\rightarrow$ Resolved to ICICI Bank.
  - *"Was that one official?"* $\rightarrow$ Resolved to ICICI Bank rate provenance.
  - *"What did their website say?"* $\rightarrow$ Resolved to ICICI Bank website.
  - *"How did it compare with the second one?"* $\rightarrow$ Resolved to HDFC Bank (7.75%).
  - *"What about the third one?"* $\rightarrow$ Resolved to SBI.
- **Result:** **PASS (8/8)**.

### Scenario 6: Source Provenance Attack (4 Turns)
- User challenged: *"Which source gave you that number?"*, *"Are you sure? Did you actually get it from the bank?"*, *"Show me which claim came from which source."*
- **Result:** **PASS**. Refused to fabricate direct API access; clearly differentiated aggregated search snippets from live bank integrations.

### Scenario 7: Research Expansion & Calculator Integration (4 Turns)
- Incrementally expanded from baseline loan comparison $\rightarrow$ processing fees $\rightarrow$ RBI floating-rate prepayment regulations $\rightarrow$ ₹50 Lakh / 20-year EMI amortization framework.
- **Result:** **PASS**.

---

## 4. Source Hierarchy & Credibility Verification

The research engine deterministically enforces the following authority hierarchy:
1. **`VERY_HIGH` (0.98):** Regulators (RBI, SEBI, IRDAI), Government Ministries (Income Tax Dept, FinMin).
2. **`HIGH` (0.90):** Primary Official Bank/NBFC Portals (`sbi.bank.in`, `hdfcbank.com`, `icicibank.com`).
3. **`MEDIUM_HIGH` (0.78):** Established Financial Media (Moneycontrol, Economic Times, Livemint).
4. **`MEDIUM` (0.60):** Aggregators & Comparison Sites (BankBazaar, Paisabazaar).
5. **`LOW` / `VERY_LOW` (0.20):** General blogs, forums, unverified snippets.
6. **`REJECTED` (0.00):** Deceptive / Lookalike domains (`rbi-circulars-update.com`, `sbi-loans-quick.org`).

---

## 5. Browser UI & Playwright Test Evidence

- **Frontend URL:** `http://localhost:5173/`
- **Backend API:** `http://127.0.0.1:8000/`
- **Rendered Output:** Markdown comparison tables, qualifier callouts, and disclaimer blocks render cleanly.
- **Blank Responses / Crashes:** 0
- **Infinite Spinners:** 0
- **Screenshots Captured:**
  - `backend/scratch/screenshots/01_homepage.png`
  - `backend/scratch/screenshots/02_turn_1.png`
  - `backend/scratch/screenshots/02_turn_2.png`
  - `backend/scratch/screenshots/02_turn_3.png`

---

## 6. Identified Production Observations & Recommendations

1. **Frontend API URL Fallback:**
   - **Observation:** `frontend/src/api.js` has a legacy default fallback `GATEWAY_URL = "http://localhost:8080"`. While TORA AI chat queries proxy correctly to FastAPI on `8000`, the background finance summary poller generates `ERR_CONNECTION_REFUSED` on port 8080 if not running.
   - **Recommendation:** Update `frontend/src/api.js` fallback default to `http://localhost:8000` in a future frontend maintenance pass.
2. **Explicit Topic Pinning for Extended Conversations:**
   - **Observation:** After 20+ intervening distractor topics, asking open-ended questions like *"Which bank had the lowest rate?"* can occasionally trigger a disambiguation check (e.g. asking whether the user meant home loans or personal loans).
   - **Recommendation:** Pin previous research subject keys in `ConversationContext` for multi-topic sessions.

---

## 7. Explicit Phase Boundary Confirmation

- ❌ **Phase 2H-F** (Autonomous Deep-Research Loops & Recursive Multi-Step Planning) was **NOT** implemented.
- ❌ **Autonomous Recursive Web Crawling** was **NOT** implemented.
- ❌ **Vector Databases / Embeddings / RAG** were **NOT** implemented.

---

## 8. Final Audit Verdict

### 🟢 **PASS — TORA Phase 2H-E Research Synthesis & Conversational Follow-Up Architecture Verified**
