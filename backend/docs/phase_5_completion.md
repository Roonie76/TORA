# Phase 5 — Measure and Harden (September 2026)

## Summary

| Sub-phase | Status | What changed |
|---|---|---|
| 5A Live check | Done: a live manual run replaced the full live benchmark | See `manual_test_report_2026_09.md` |
| 5B Bigger benchmark | Done | 22 → 121 scenarios; offline gate 121/121 (314 checks) |
| 5C Runbook fixes | Done | Every problem found in the live run was fixed, tested and rerun (see report) |
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

The full live benchmark (22 scenarios, pre-5D code) was stopped after about 30 minutes because each model call took about 2 minutes.
Instead, the manual runbook was run live on the current code, which tests the same abilities and shows real answers.

The main finding was that gemma4 writes a hidden thinking pass on every call. Turning it off (`TORA_LLM_THINK=false`, now the default) made memory turns about 4× faster.
Planner turns are still slow on CPU (planner step median 76 s), so skipping the planner for obvious requests is the top item for Phase 7.

New settings from this phase:

| Variable | Default | Purpose |
|---|---|---|
| `TORA_LLM_EXTRACTION` | `auto` | Model-assisted memory when the rules find nothing (`off` to disable) |
| `TORA_PLANNER_REPAIRS` | `1` | How many times the planner may retry after producing an invalid plan |
| `TORA_LLM_THINK` | `false` | Turn off the model's hidden thinking pass (`true` or `auto` to change) |
| `TORA_MAX_ANSWER_TOKENS` | unset | Optional answer-length cap for slow machines (for example 700) |

Keep `OLLAMA_NUM_PARALLEL=1` on 8 GB machines: 2 slots ran out of memory during the run.

Full results: `backend/docs/manual_test_report_2026_09.md`.

