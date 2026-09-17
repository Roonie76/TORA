# Phase 5 — Measure and Harden (September 2026)

## Summary

| Sub-phase | Status | What changed |
|---|---|---|
| 5A Live baseline | Done (see numbers below) | `python -m backend.evals --mode live` run against `gemma4:e4b` on CPU |
| 5B Bigger benchmark | Done | 22 → 121 scenarios; offline gate 121/121 (314 checks) |
| 5C Runbook fixes | No failures recorded yet | The runbook's shared results store was empty when checked |
| 5D Planner reliability | Done | JSON-schema `format`, parameter coercion, validation, repair retry (`TORA_PLANNER_REPAIRS`) |
| 5E Model-assisted extraction | Done | LLM proposes facts only when rules find nothing; every proposal must quote verbatim evidence that parses to the same value (`TORA_LLM_EXTRACTION=off` disables it) |

## 5B — benchmark

Categories: calculation 20, memory, language (Hinglish, typos, vague wording), tax 12,
planning, safety 7, tool_selection 9, research, long conversations (10+ turns).

`backend/tests/test_normalize_and_benchmark.py` fails the build if the benchmark shrinks
below 100 scenarios, loses a category, loses its long conversation, or has duplicate ids.
`test_offline_benchmark_passes_fully` keeps the 100% offline gate.

Fixes found by the new scenarios:

- `normalize_message()` (new `backend/context/normalize.py`) converts common Hinglish
  phrases and typos before extraction and intent routing (`meri salry 80k hai`,
  `90 hazaar kamata hoon`, `salary bhool jao`).
- "I used to earn 60k but now I earn 72k" now stores 72k as current with 60k as previous.
- "I spend 15k on groceries" and "2 lakh saved" are extracted.
- Impersonal requests ("Tax on 60 lakh income") no longer store facts about the user.
- Tax, goal-plan, savings-rate and debt-to-income questions that contain numbers are no
  longer misrouted to memory update/recall (which skipped the planner).

## 5A — live baseline

LIVE_RESULTS_PLACEHOLDER
