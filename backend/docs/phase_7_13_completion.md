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
  - 987 unit tests pass;
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
LIVE_PLACEHOLDER
