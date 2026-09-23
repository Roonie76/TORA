# TORA — status

Generated from the code on 2026-09-23 by `python -m backend.status`. Everything under **Built** is read out of the running system (registered tools, engine operations, endpoints, intents, fact types, the rules library, the eval suite). **Partial** and **Open** are the judgement calls, each with a pointer to the code or test behind it.

## At a glance

|  | count |
|---|---|
| Tools registered | 8 |
| Finance engine operations | 24 |
| Tax operations | 8 |
| HTTP endpoints | 26 |
| Intents | 12 |
| Remembered fact types | 35 |
| Verified rules | 33 (checked 2026-09-17) |
| Offline eval scenarios | 160 (256 turns) |
| Backend tests | 1368 |
| Frontend tests | 40 |

## Built

### Tools the planner can call

| tool | what it does | sign-in |
|---|---|---|
| `calculator` | Perform deterministic mathematical calculations. Supports +, -, *, /, %, **, unary +/-, and parentheses. Use f | — |
| `web_search` | Search the public web for current information, live loan/FD/gold rates, tax circulars, RBI guidelines, financi | — |
| `web_fetch` | Fetch and extract readable plain text content from a specific public webpage URL (HTTP/HTTPS). Use this when y | — |
| `research` | Multi-source verified research: searches the web, reads several pages, checks source authority (RBI/SEBI/offic | — |
| `finance_calc` | Deterministic personal-finance calculator. Use for EMIs, loan prepayment/amortization, SIP and lump-sum growth | — |
| `tax_calc` | Deterministic Indian income-tax calculator for resident individuals (tax years 2025-26, 2026-27): slab tax, st | — |
| `rules_lookup` | Look up official Indian tax, RBI and consumer-protection rules from TORA's reviewed library: limits, rates, el | — |
| `spendsy_data` | Read the signed-in user's OWN Spendsy transactions (read-only). Use for questions about what they actually ear | — |

### Finance engine

Deterministic operations on `finance_calc` — the model never does this arithmetic:

`amortization` · `budget_plan` · `compound_growth` · `consolidation_check` · `debt_payoff` · `debt_rescue_plan` · `debt_snapshot` · `debt_to_income` · `emergency_fund` · `emi` · `financial_health_check` · `goal_plan` · `inflation_adjust` · `loan_tenure_choice` · `minimum_due_trap` · `net_worth` · `prepay_vs_invest` · `rent_vs_buy` · `required_sip` · `retirement_plan` · `savings_rate` · `scenario_compare` · `sip_change_impact` · `sip_future_value`

### Tax engine

`compute_tax` · `compare_regimes` · `hra_exemption` · `house_property_income` · `capital_gains_tax` · `advance_tax_plan` · `itr_form_choice` · `tax_saving_finder`

Backed by a rules library of 33 verified rules (`python -m backend.knowledge check` reports staleness and drift).

### API

| method | path | purpose |
|---|---|---|
| GET | `/` | Health check for FastAPI and LLM provider connectivity. |
| GET | `/api/health` | Health check for FastAPI and LLM provider connectivity. |
| GET | `/api/models` | List available models from the LLM provider. |
| POST | `/api/chat` | Traced entry point (metadata-only traces; see backend/observability). |
| POST | `/chat` | Traced entry point (metadata-only traces; see backend/observability). |
| POST | `/api/chat/stream` | Same as /api/chat, streamed as Server-Sent Events: |
| POST | `/api/chat/async` | Accept a turn and answer it in the background. |
| GET | `/api/chat/turns/{turn_id}` | The current state of a background turn, including the answer so far. |
| DELETE | `/api/chat/turns/{turn_id}` | Stop a running turn |
| GET | `/api/chat/turns/{turn_id}/events` | Live progress for a background turn, as SSE. |
| GET | `/api/metrics` | Aggregate, content-free TORA metrics since process start. |
| GET | `/api/alerts` | What is wrong with TORA right now, worst first. |
| GET | `/api/dashboard` | A single page over the metrics and traces that already exist. |
| GET | `/api/traces` | Recent per-turn traces (metadata only) |
| GET | `/api/me` | Who TORA thinks is calling, and whether account features are on. |
| GET | `/api/conversations` | The signed-in user's conversations, newest first. |
| GET | `/api/me/memory` | Financial facts TORA remembers for this account (shared by all its conversations). |
| DELETE | `/api/me/memory` | Forget every remembered financial fact for this account (transcripts are kept). |
| DELETE | `/api/me/data` | Delete all of this account's TORA conversations and remembered facts. |
| POST | `/api/documents` | Read a Form 16, salary slip, bank statement or AIS |
| POST | `/api/documents/{doc_id}/confirm` | Save the chosen proposed facts from a parsed document into memory. |
| POST | `/api/documents/{doc_id}/dismiss` | Forget a parsed document's summary. |
| POST | `/api/feedback` | Thumbs up/down on an answer (optionally with a corrected answer) — used for later training. |
| GET | `/api/conversations/{conversation_id}` | Transcript, remembered facts and topic state for one conversation. |
| DELETE | `/api/conversations/{conversation_id}` | Delete a conversation (for signed-in users, account memory is kept; see /api/me/memory). |
| DELETE | `/api/conversations/{conversation_id}/memory` | Forget all remembered financial facts but keep the transcript. |

### Conversation and memory

Intents: `general_qa`, `financial_qa`, `calculation`, `memory_recall`, `memory_update`, `memory_delete`, `research`, `research_followup`, `comparison`, `planning`, `what_if`, `clarification`

Fact states: `current`, `historical`, `hypothetical`, `conditional`, `estimate`, `unknown`, `ambiguous`, `retracted` — with revision chains, previous values, and corrections held apart from changes over time.

Conversation state tracks topics, their entities and the research gathered under each, so “what about Axis?” and “back to the home loan comparison” resolve without re-asking (`backend/state/conversation_state.py`).

Remembered fact types:

| category | facts |
|---|---|
| debt | `credit_card_apr`, `credit_card_debt`, `credit_card_min_due` |
| expense | `commute`, `essential_expenses`, `food`, `insurance_premium`, `utilities` |
| goal | `car_goal`, `house_goal` |
| income | `income` |
| investment | `epf`, `fixed_deposit`, `gold`, `mutual_funds`, `ppf`, `sip_monthly`, `stocks` |
| loan | `car_loan_balance`, `car_loan_emi`, `car_loan_rate`, `education_loan_balance`, `education_loan_emi`, `education_loan_rate`, `gold_loan_balance`, `gold_loan_emi`, `gold_loan_rate`, `home_loan_balance`, `home_loan_emi`, `home_loan_rate`, `personal_loan_balance`, `personal_loan_emi`, `personal_loan_rate` |
| rent | `rent` |
| savings | `savings` |

### Outside code we lean on

| library | license | what it does here |
|---|---|---|
| `Babel` | BSD-3-Clause | CLDR number formatting behind `inr()` — Indian digit grouping now comes from locale data rather than hand-rolled string surgery (a fallback keeps the engines working if it is absent). |
| `pyxirr` | Unlicense | tests only: an independent implementation of PMT/FV used to differential-test the EMI and SIP engines. It already caught a rounding bug in required_sip. |

### Prompt assembly

The system prompt is one document; each turn is sent only the sections it can use — the engine sections when a tool ran, the rules citation section when the rules library answered, the debt-stress section when the case is about debt. Small talk goes out at 987 tokens against 2,321 for the whole prompt (`backend/prompts/tora.py`; `TORA_PROMPT_SLICING=off` disables it).

### Locked figures

A turn that ran an engine carries a slot table: every figure it produced, already formatted, offered to the answer model as `{{slot}}` placeholders. Placeholders are substituted after generation, and any number in the reply that matches no slot (beyond rounding) earns one rewrite naming the offender. Only deterministic tools contribute slots — researched figures stay in the external-data block (`backend/answer/slots.py`; `TORA_LOCKED_SLOTS=off` disables it).

### Documents

Uploads are parsed deterministically and their figures enter memory only after the user confirms: `ais`, `bank_statement`, `form16`, `salary_slip`. Identifiers (PAN, account numbers, phones, emails) are masked, and a document's text is treated as untrusted external data, never as instructions (`backend/documents/reader.py`).

### Safety

- Prompt-extraction and injection attempts are refused; fetched pages and uploads are quarantined as external data (live cases A14, X1-X3).
- SSRF: loopback, link-local and private targets are blocked (`web_fetch`; live case B20).
- Per-client rate limiting with Retry-After; request size, model name and temperature validated.
- Accounts: `TORA_AUTH_MODE` off/optional/required; a user's conversations and memory are unreachable by anyone else (50-user isolation scenario).

### Streaming

`POST /api/chat/stream` emits `stage`, `complexity`, `tool`, `token`, `replace`, then `final` or `error`. Stages: understanding (“Understanding your question”), remembering (“Updating what I know about you”), planning (“Working out what to calculate”), calculating (“Running the numbers”), researching (“Checking sources”), reading_rules (“Looking up the rules”), reading_records (“Reading your Spendsy records”), writing (“Writing the answer”), checking (“Double-checking the figures”).

The chat page renders them live: working panel with per-step ticks and engine summaries, smooth token reveal, Stop and Try again, engine and verification chips, follow-up suggestions (`frontend/src/pages/TORAPage.jsx`, `tora/sse.js`, `tora/markdown.jsx`).

### Verification and testing

- `python -m backend.check`: 1368 unit tests, 160 offline scenarios, the rules-library check.
- Offline scenarios by category: accounts 5, advice 5, calculation 20, debt 8, followup 9, grounding 7, language 16, memory 37, planning 5, routing 8, rules 5, safety 7, tax 19, tool_selection 9.
- `python -m backend.stress.load_test`: throughput, per-user isolation, concurrent turns on one conversation, cancelled streams, rate limiting, fuzzing (a small version runs in the gate).
- Live prompt suite and its results: `backend/docs/stress_test_report.md`.
- Frontend: 40 tests under `frontend/src/tests/tora/`.

### Configuration

`TORA_ALERT_GROUNDING_MIN_TURNS`, `TORA_ALERT_GROUNDING_RATE`, `TORA_ALERT_REQUEST_ERROR_RATE`, `TORA_ALERT_REQUEST_MIN_TOTAL`, `TORA_ALERT_TOOL_ERROR_RATE`, `TORA_ALERT_TOOL_MIN_CALLS`, `TORA_ANSWER_BLOCK`, `TORA_AUTH_CACHE_SECONDS`, `TORA_AUTH_MODE`, `TORA_AUTH_URL`, `TORA_BLOCK_ANSWER_TOKENS`, `TORA_COMPLEX_MODEL`, `TORA_COMPLEX_THINK`, `TORA_CORS_ORIGINS`, `TORA_DEBUG_ENDPOINTS`, `TORA_DIRECT_ANSWER`, `TORA_FAST_PATH`, `TORA_FINANCE_URL`, `TORA_GROUNDING_MODE`, `TORA_HOST`, `TORA_LLM_EXTRACTION`, `TORA_LLM_NUM_CTX`, `TORA_LLM_THINK`, `TORA_LLM_TIMEOUT_SECONDS`, `TORA_LOCKED_SLOTS`, `TORA_MAX_ANSWER_TOKENS`, `TORA_MODEL_CACHE_SECONDS`, `TORA_PLANNER_REPAIRS`, `TORA_PROMPT_SLICING`, `TORA_RATE_LIMIT_PER_MINUTE`, `TORA_SESSION_DB`, `TORA_TOOL_BREAKER_FAILURES`, `TORA_TOOL_BREAKER_SECONDS`, `TORA_TOOL_RETRIES`, `TORA_TRACE_FILE`, `TORA_TRAINING_LOG`, `TORA_TURN_MAX_ANSWER_CHARS`, `TORA_TURN_MAX_EVENTS`, `TORA_TURN_MAX_RETAINED`, `TORA_TURN_TTL_SECONDS`

## Partial — built, with a named gap

| area | what exists | what's missing | evidence |
|---|---|---|---|
| Memory | current / historical / hypothetical / retracted states, revision chains, corrections kept apart from changes over time, per-fact previous values | confidence per fact, and temporal queries (“what was it in March?”) | `backend/context/financial.py` |
| Answer fidelity | locked slots: every figure a turn may contain comes from the engines, offered as named placeholders; a number matching no slot earns one rewrite, and the grounding check still runs behind it | small models copy the values rather than writing the placeholders (harmless, since the slot list also acts as the whitelist) — worth revisiting with a stronger model | `backend/answer/slots.py, backend/tests/test_locked_slots.py` |
| Prompt size | intent-sliced prompts: each turn gets only the sections it can use, cut from the one full prompt (small talk 987 tokens against 2,321 unsliced) | measured: prefill 39 tok/s against decode 4.1, KV-cache prefix reuse 150x, and the three-turn A/B in backend/docs/latency.md. What is left is answer length, not prompt length | `backend/prompts/tora.py:slice_prompt, backend/tests/test_prompts.py` |
| What-if | per-engine scenarios (sip_change_impact, what_if_extra, prepay/rent-vs-buy, loan tenure) and hypothetical facts kept out of the profile | a general scenario engine: change several facts at once, re-run every relevant engine, compare baseline vs scenario | `backend/finance/, state kept in FinancialProfile.scenarios` |
| Research | multi-source search, evidence extraction, credibility, conflict detection, per-topic research records and follow-ups | an autonomous re-research loop when evidence is thin, conflicting or stale | `backend/research/` |
| Tax | two tax years, both regimes, 8 operations, a 33-rule library with staleness checks | broader coverage (more heads of income, more years, presumptive schemes) | `backend/finance/tax_extras.py, backend/knowledge/rules.json` |
| Tool runtime | per-call timeouts, call ids, per-tool latency in metrics and traces, planning-time argument checks with a repair loop, one retry for a transient failure, and a per-tool circuit breaker reported by /api/metrics | a failing tool is skipped and named, but nothing routes around it to a second source | `backend/tools/executor.py, backend/tools/resilience.py` |
| Observability | /api/metrics, /api/traces, an optional JSONL trace file, per-request traces with no personal content, and /api/dashboard — one page over them, metadata only | no alerting: someone has to look at the page | `backend/observability/` |
| Evaluation | python -m backend.check runs unit tests, the offline benchmark and the rules check | CI wiring so it runs on every push | `backend/check.py` |
| Manual QA | a 149-test runbook covering every phase, including the streaming UI (V1-V11) | one full hands-on pass in a real browser, desktop and mobile | `backend/docs/manual_test_plan.md` |

## Open — in priority order

The full plan, with what each item is worth, is in `backend/docs/ROADMAP_TO_100.md`.

1. **Full browser runbook pass.** 149 tests, desktop and mobile, by hand. Never completed.
   A first pass on 19 Sep found three defects; `backend/docs/latency.md` has the results.
2. **Non-blocking long turns.** A minutes-long turn should not hold a connection.
3. **General scenario engine.** Change several facts at once and re-run every relevant engine.
4. **Autonomous re-research.** Decide that evidence is thin or stale and go again.
5. **Broader tax coverage.**
6. **Alerting** on the metrics the dashboard now shows — nothing shouts when a tool breaks.

An answer cache is deliberately NOT on this list: it was built, measured and removed. See ROADMAP_TO_100.md for the bar it would have to clear to come back.

## Known limits

| limit | detail |
|---|---|
| Latency on CPU | 2 cores, 8 GB, one model instance: prefill 39 tok/s, decode 4.1 tok/s, so a written token costs 9.5x a read one. A memory recall is now ~4s end to end; a full debt plan is still minutes, and its cost is the length of the answer. See docs/latency.md. |
| Grounding derivations | the figure check accepts simple derivations of known values, so a wrong arithmetic result can still coincide with one. Fewer model-made figures is the fix. |
| Small-model hedging | gemma4:e4b sometimes asks for input it already has (verification W04 asked which tax year although the tax engine had answered). |
| Prompt rules are advisory | the answer-shape rules cut the debt plan 368 -> 299 words, but the model still opened with two sentences it had just been told not to write. Anything that must hold is rendered in code, not asked for. |
| Model memory pressure | one live turn was killed by the OS; the stream reported it and the UI offered Try again, which is the intended behaviour. backend/ops/slim_gguf.py cuts the model 9.6 GB -> 6.0 GB with byte-identical answers, which removes the pressure without changing speed. |

---

Regenerate with `python -m backend.status`. If a row here disagrees with the code, the code wins — fix the generator, not the prose.
