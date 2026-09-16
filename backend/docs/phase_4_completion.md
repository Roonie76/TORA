# Phase 4 — Evaluation, Grounding, Tax, Planning, Observability

Test suite: 677 → **762 passing**. Offline benchmark: **22/22 scenarios, 103/103 checks**.

## 4A Evaluation benchmark (`backend/evals/`)
- Multi-turn scenarios (`scenarios.json`) run through the real pipeline with per-turn expectations:
  intent, follow-up resolution, planner use, tools and their outputs, profile/previous/retracted values,
  topic, system-prompt and untrusted-data contents, grounding action, answer text (live).
- `--mode offline` scripts the LLM so the deterministic pipeline is checked exactly (part of pytest).
  `--mode live` uses Ollama for planning and answers; web tools return fixtures unless `--real-web`.
- JSON + Markdown reports, per-category pass rates, `--min-pass-rate` for CI gating.
- The first run found three real defects, now fixed: "previous" value not stated explicitly in the profile
  when only one revision existed; "What would the EMI be for …" not classified as a calculation;
  second-order derived figures (monthly surplus × 12) rejected by the grounding check.

## 4B Numeric grounding verifier (`backend/verify/`)
- Extracts ₹ amounts (₹/Rs/INR, k/lakh/crore) and percentages from the answer.
- Evidence: the user's words, profile (incl. revisions and scenarios), tool outputs, earlier turns, stored
  research, plus derived sums/differences/×12/÷12/shares.
- Tolerance follows the precision the figure is written with (₹21.66 lakh ⇒ ±₹500), capped at 3%.
- Figures explicitly labelled as examples/assumptions pass. Percentages are enforced for research and
  comparison answers.
- `TORA_GROUNDING_MODE=regenerate` (default): one corrective regeneration, then a visible caveat;
  `annotate`: caveat only; `off`. Result returned as `grounding` in the chat response and stored per turn.

## 4C Versioned tax engine (`backend/finance/tax.py`, `tax_calc` tool)
Sources checked September 2026 — Union Budget 2026-27 left slabs, rebate, standard deduction and surcharge
unchanged; the Income Tax Act, 2025 applies from 1 April 2026 (rebate s.156).
- Tax years 2025-26 and 2026-27; new regime slabs 0/5/10/15/20/25/30 with ₹4L steps, ₹75k standard deduction,
  ₹60k rebate up to ₹12L with marginal relief; old regime (normal/senior/super-senior), ₹50k standard deduction,
  ₹12.5k rebate up to ₹5L, capped deductions (80C, 80D, 80CCD(1B), home-loan interest, 80TTA/TTB).
- STCG (listed equity) 20%, LTCG 12.5% above ₹1.25L, other LTCG 12.5%; unused basic exemption absorbs gains;
  rebate never offsets special-rate tax.
- Surcharge 10/15/25/37% (new regime capped at 25%, equity gains at 15%) with marginal relief; 4% cess.
- `compare_regimes` returns both computations and the saving.
- Data note: one secondary source still lists a ₹25,000 new-regime rebate (an older figure); ₹60,000 is used
  because it is the only value consistent with ₹12L being tax-free under the current slabs.
- Not modelled: HRA/LTA details (pass as `other` under the old regime), losses, AMT, VDA, non-residents.
- The Spendsy frontend still has its own `shared/services/taxService.js`; it should call this engine instead.

## 4D Observability (`backend/observability/`)
- Metadata-only per-turn trace: intent, follow-up, planner decision and latency, tools with latency/errors,
  LLM calls/latency/tokens (from Ollama), prompt-token estimate, overflow retry, grounding, status.
  Conversation ids are hashed; no message text or figures are recorded.
- `GET /api/metrics` (success rate, p50/p95 latency, intents, planner skips, per-tool health, tokens,
  grounding actions, errors); `GET /api/traces` behind `TORA_DEBUG_ENDPOINTS=1`; JSONL sink via `TORA_TRACE_FILE`.

## 4E Planning operations (`finance_calc`)
- `budget_plan`: 50/30/20 check with gaps and concrete actions (EMI load, overspending).
- `goal_plan`: required SIP per goal, nearest deadline funded first within a monthly capacity, shortfalls.
- `retirement_plan`: inflation-indexed corpus (growing annuity) and the SIP needed.

## Not verified here
- Live benchmark against the real Gemma model (no Ollama in the build environment). Run
  `python -m backend.evals --mode live` on your machine and keep the report for model comparisons.
