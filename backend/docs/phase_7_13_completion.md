# Phases 7–13 — Finishing the TORA system (September 2026)

Goal: finish TORA's working parts, apart from training the model, so it can handle a middle-class
Indian household's money questions the way an experienced chartered accountant would. Clear-cut
questions get fast answers, big decisions get more effort, and every figure is calculated and cited.

| Phase | What it adds | Main files |
|---|---|---|
| 7 Speed and effort scaling | Clear-cut calculations skip the planner; greetings skip everything; each case is rated simple, standard or complex, and complex cases get a stronger model, reasoning mode and CA-style answer guidance | `planner/fast_path.py`, `agent/agent.py` |
| 8 Debt rescue | Debt snapshot, rescue plan (survival budget, shortfall, avalanche vs snowball, minimums-only comparison, what-ifs, using savings), consolidation check, minimum-due trap | `finance/debt.py` |
| 9 Option evaluator | Prepay vs invest, rent vs buy, loan tenure, financial health check — each with a recommendation, confidence, break-even and points to confirm | `finance/advisor.py` |
| 10 Rules library | 33 dated rules, cited by section and year; search; staleness and drift checks; `rules_lookup` tool; tax results show their legal basis | `knowledge/`, `tools/rules_tool.py` |
| 11 Documents | Reads Form 16, salary slips, bank statements and AIS (PDF with or without a password, CSV, TXT); masks personal identifiers; facts are saved only after the user confirms | `documents/`, `/api/documents` |
| 12 Complete individual tax | HRA, house property, capital gains on one sale (including indexation for property and 2018 grandfathering for equity), advance tax, choosing the ITR form, finding tax savings | `finance/tax_extras.py` |
| 13 Hardening | Opt-in training log, `/api/feedback`, export to training records, thumbs up/down in the UI, `python -m backend.check` release check | `observability/training_log.py`, `check.py` |

## New settings

| Variable | Default | Purpose |
|---|---|---|
| `TORA_FAST_PATH` | `on` | Clear-cut requests skip the planner |
| `TORA_COMPLEX_MODEL` | unset | Model used only for complex cases (e.g. a larger local model) |
| `TORA_COMPLEX_THINK` | `off` | Turns on reasoning mode only for complex cases |
| `TORA_TRAINING_LOG` | unset | Path for the masked training log. Enable only with users' consent. |

## New or changed endpoints

- `POST /api/documents`: multipart upload with `file`, optional `conversation_id`, `password` and `doc_type`. Returns a summary and proposed facts.
- `POST /api/documents/{id}/confirm` and `POST /api/documents/{id}/dismiss`: body `{conversation_id, facts?}`.
- `POST /api/feedback`: body `{conversation_id, turn, rating: up|down, comment?, better_answer?}`.
- `/api/chat` now also returns `turn`.
- Update the Spendsy Vite proxy for `/api/documents`, `/api/feedback` and `/api/me`. The included `vite.config.js` already has these.

## Checks

- `python -m backend.check`:
  - 993 unit tests pass;
  - the offline benchmark passes 155 of 155 scenarios (routing, debt, advice and rules categories added);
  - the rules library check reports OK.
- Live check with `gemma4:e4b` on CPU: see "Live results" below and `manual_test_report_2026_09.md`.
- The runbook and `manual_test_plan.md` have new sections F (speed), D (debt), V (options), L (rules), K (documents), Q (tax) and Z (feedback).

## Known limits

- **Fast path:** it only handles one clear request. Anything with follow-up references, several loans, deductions or unclear income still goes to the planner, which is slow on CPU.
- **Section numbers:** new-Act section numbers not yet confirmed from a primary source are marked "confirm the new section number". Confirmed so far: 123, 124, 126, 129, 133, 153, 156, 196, 198, 202, 263, 392, 393.
- **Document formats:** bank statements vary by bank. The parser handles standard CSV exports and text PDFs with a running balance. Scanned PDFs need the original file.
- **Tax scope:** capital gains cover sales on or after 23 July 2024 and exclude surcharge. HRA rules for Section 80GG (rent deduction without HRA) and business income beyond presumptive schemes are not calculated.
- **Regulation:** these features give guidance and calculations, not signed advice. Investment advice, data protection and CA sign-off still need the compliance review planned before launch.

## Live results
Run with `gemma4:e4b` on a 2-vCPU CPU-only machine against a live server, grounding set to `regenerate`.

**Speed and routing (Phase 7).** Fast-path turns took 25–89 s, down from 120–193 s through the planner:
- EMI (49 s): ₹17,356.
- SIP (63 s): ₹23,23,391.
- Tax on ₹12.75 lakh (89 s): ₹0.
- Tax for 2019-20 (25 s): correctly refused as an unsupported year.

Complex cases were rated `complex` and answered with options. They took 5–8 minutes on CPU (planner plus a long answer).

**Checks run live:**

| Check | Result |
|---|---|
| Debt facts over two turns, then "I can't manage, how do I get out of debt?" | Rescue plan with ₹35,000 a month: debt-free in 15 months, ₹58,812 interest, card cleared in month 8 — matches the engine |
| Minimum-due trap | 252 months and ₹3,18,498 interest on minimums only, vs 35 months and ₹1,16,233 at a fixed ₹7,750 |
| Prepay or invest | Recommends investing; break-even return 9.15%; lists the points to confirm |
| Health check | Score 50/100; priorities in the right order (emergency fund ₹3,30,000, then the card, then insurance) |
| 80C limit | Rules library via the fast path (64 s): ₹1.5 lakh, old regime only |
| Recovery agent at 10 pm | Rules library via the fast path (43 s): RBI 8 am–7 pm limit; lender grievance, then Ombudsman / 14448 |
| Property sale | ₹4,37,174 via the 20% indexation route (vs ₹7,50,000); mentions s.54/54EC |
| HRA (rerun after fix) | Fast path (88 s): exempt ₹2,40,000 |
| ITR form (rerun after fix) | ITR-2, because listed-equity gains exceed ₹1.25 lakh |
| Form 16 review (rerun after fix) | Fast document route (146 s): the new regime is cheaper by ₹86,840; unused parents' 80D worth ₹5,200 |

**Fixed during the run:**
1. The complex-case answer said "As a senior CA". The guidance now says to work like a CA but never claim to be one.
2. The planner dropped the city for HRA. There is now a fast path for HRA with metro detection.
3. The planner used wrong parameter names for the ITR check. Parameter names are now checked while planning (so the planner retries), and common alternative names are accepted.
4. The Form 16 question produced no calculation, and the answer asked for permission first. Form 16 questions now go straight to the tax-savings check, the planner sees document figures, and TORA no longer asks before calculating.
5. Debt-rescue questions were rated only "standard". The complexity words now include them.

**Latency note:** a long health-check answer took about 16 minutes on CPU. On CPU-only machines, set `TORA_MAX_ANSWER_TOKENS=700`; the rerun used this setting.

