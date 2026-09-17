# TORA Manual Test Report — 17 September 2026

The runbook (`manual_test_plan.md`) was run against a live server: `uvicorn backend.main:app`, `gemma4:e4b` through Ollama on a 2-vCPU / 8 GB cloud machine (no GPU), `TORA_GROUNDING_MODE=regenerate`.

- **Phase 6 checks:** these used a stand-in Spendsy gateway (`/auth/me` and `/finance/transactions`, with small result pages and a prompt-injection string in one transaction). The real services are not in this repo.
- **Web search:** blocked from this machine (DuckDuckGo timed out), so the research tests only checked how TORA fails.
- **Reruns:** every bug found was fixed in the code, covered by a unit test, and the affected tests were rerun on the fixed build.

## Summary

| Area | Result |
|---|---|
| API guard rails (A1–A5) | Pass. Oversized message and bad settings → 422. The 4th message in a minute → 429. Unknown website → CORS 400. Server listens on 127.0.0.1 only. |
| Conversations (B5) and monitoring (O1, O5) | Pass. Unknown id → 404. Metrics available. Traces page off by default (404). |
| Memory M1–M10 | Pass after fix (M10: a home-loan EMI was saved as a personal-loan EMI) |
| Hinglish (H1, H8, H9) | Pass after fix ("salary kitna hai?", "salary bhool jao" and "N hazaar bachat karta hoon" were misread) |
| Calculations C1–C15 | Pass. Every figure matched the runbook's answer key. |
| Tax T1–T7 | Pass after fixes (T3: deductions passed in the wrong shape; T5: the model saved capital gains as holdings; T7: no tool call for an unsupported year, and "10 lakh" saved as salary) |
| Research R1 and R6 | Web unavailable. TORA said it couldn't get live data and gave no made-up rates. R6 did not average the three figures but did not list them either (partial pass). |
| Security X1, X2 | Pass. Refused to reveal its prompt; web_fetch blocked 127.0.0.1 and 169.254.169.254. |
| Answer checking G1, G3 | Pass. G1: ₹60,000 a month and ₹7,20,000 a year. G3: asked for the loan inputs instead of guessing. |
| Accounts and Spendsy data (P0–P13, stand-in gateway) | Pass after fixes (P5: the average was taken over a partial month; P9: an anonymous user wasn't told to sign in) |

## Bugs found and fixed

| Test | Problem | Fix | Commit |
|---|---|---|---|
| All | gemma4 wrote a hidden "thinking" pass before every answer. Turns took 2–7 min. | Thinking is now off by default (`TORA_LLM_THINK`), with a retry for models that don't support the setting. Memory turns went from about 106 s to 25 s (median). | `Disable hidden reasoning…` |
| All | Very long answers on CPU | Prompt now asks for concise answers. Optional `TORA_MAX_ANSWER_TOKENS` cap, with a visible note when an answer is cut. | `Optional TORA_MAX_ANSWER_TOKENS…` |
| All | Planner prompt carried 11 kB of indented tool schemas | Compact JSON with the extra title fields removed (−21%) | `Compact planner tool schemas…` |
| M5, M7 | Replies quoted internal headings ("Active Verified Facts"). A forgotten rent was treated as ₹0. | Prompt: talk about memory naturally; a forgotten value is unknown, not zero | `Prompt: talk about memory naturally…` |
| M10 | "my home loan EMI is 25k" saved as a personal-loan EMI. The runbook's expected answer had the same mistake. | Loan memory now tracks the loan type (home, car, education, gold, personal); "forget my home loan" and "paid off my car loan" work too | `Type-aware loan memory…` |
| H1 | "salary kitna hai?", "salary bhool jao" and "5 hazaar bachat karta hoon" were misread | Handles Hinglish word order; a monthly saving habit is no longer saved as a savings balance | `Manual-test fixes: Hinglish…` |
| T3 | Planner sent `section_80c` etc. as top-level parameters, so tax_calc rejected them | tax_calc now accepts deductions at the top level, under common names (80C, 80D, section 24…) or as a list | `tax_calc: accept deductions…` |
| T5 | Model-assisted memory saved "6 lakh other income" as salary and capital gains as holdings | Model-assisted memory is skipped for tax questions. Other income and gains are rejected as salary or holdings. | `Model-assisted memory: skip tax questions…` |
| T7 | Planner didn't call tax_calc for 2019-20. "10 lakh salary" was saved as the user's income. | Planner now passes named tax years to tax_calc. Tax questions no longer save the example salary. | `Tax questions: pass named tax years…` |
| P1, G1 | Amounts shown as ₹190,000.00 | Prompt now asks for Indian digit grouping. Spendsy summaries include Indian-format totals. | `Indian rupee formatting guidance…` |
| P5 | Rent average divided by 3 months, including the partial current month (₹16,000 instead of ₹24,000). Answer checking caught the figure. | Category averages now use complete months only. Labels say "per complete month". | `Spendsy: per-category averages…` |
| P9 | A user who wasn't signed in was asked "may I access your data?" | The answer prompt now states whether the user is signed in | same |
| P2, P3 | Spending questions were labelled general_qa | Spending and transaction wording now counts as a finance question | `Intent: spending and transaction questions…` |
| C1 | 502 "model runner has unexpectedly stopped" | Caused by `OLLAMA_NUM_PARALLEL=2` running out of memory on 8 GB. Kept at 1 slot; the doc now warns about this. | (setup) |

## Verified figures (live)

- C2: EMI ₹17,356. Interest ₹21,65,552.
- C4: SIP future value ₹23,23,391.
- C5: ₹75,68,640 at ₹15,000 a month.
- C6: SIP ₹10,009 a month.
- C7: ₹1,79,085.
- C8: debt-free in 18 months, card cleared in month 10, interest ₹71,416.
- C9: fund ₹2,40,000, gap ₹1,40,000.
- C10: needs 62%, wants 15%, savings 23%.
- C11: car ₹18,989 a month, house short by ₹5.72 lakh.
- C12: corpus ₹7.71 crore, SIP ₹23,214.
- T1: ₹0.
- T2: ₹10,400.
- T3: new ₹1,50,800 vs old ₹2,34,000.
- T4: ₹75,400.
- T5: ₹64,350.
- T7: says 2019-20 isn't supported.
- P1 (stand-in data): income ₹1,90,000, expenses ₹82,200, savings rate 56.7%, rent 58.4%.
- P5 (after fix): recorded rent ₹24,000 a month vs ₹20,000 stated.

## Latency (CPU only, after the fixes)

- **All turns:** median 92 s, p90 199 s (73 turns).
- **Turns without the planner** (memory updates, recall, forget): median 25 s. These took about 106 s before thinking was turned off.
- **Turns with the planner:** median 148 s. The planner step alone takes about 76 s, because it re-reads roughly 2,800 prompt tokens at about 30 tokens/s.
- **Next step for speed:** send obvious calculations straight to the right tool without the planner, or use a GPU. That work is in Phase 7.

## Not covered here

- **UI tests (W0–W10, U1–U6, P10):** need a browser and your local setup.
- **B4 (restart) and O3 (Ollama outage) in the UI:** also need your local setup.
- **T8:** needs the official tax calculator.
- **R1–R7 with real search results:** web search is blocked from this machine.
- **E1–E3:** the offline benchmark is 126/126; the full live benchmark was stopped in favour of this manual run.
