# TORA

TORA is the personal-finance AI assistant behind [Spendsy](https://github.com/Roonie76/Spendsy).
It is an agent built around a local LLM (Ollama, default `gemma4:e4b`) with deterministic tools,
structured financial memory, conversation state and a verified web-research pipeline tuned for India.

## Architecture

```
POST /api/chat
  └─ session store (SQLite)          transcript · financial memory · conversation state
      └─ ToraAgent
          ├─ fact extraction          Memory 2.0: provenance, corrections, forget / clear
          ├─ intent + topic tracking  12 intents, follow-up resolution, topic pinning
          ├─ planner (LLM, JSON)      chooses tools; sees known user facts
          ├─ tool executor            timeouts, structured errors
          │   ├─ calculator           safe AST arithmetic
          │   ├─ finance_calc         EMI, amortization, SIP, goals, retirement, budget, debt payoff, ratios
          │   ├─ tax_calc             versioned Indian income tax (2025-26, 2026-27), regime comparison
          │   ├─ web_search           DuckDuckGo / Tavily
          │   ├─ web_fetch            SSRF-safe (pinned DNS, byte budget)
          │   └─ research             search → fetch → evidence → source credibility → synthesis
          ├─ context builder          token budget; untrusted web data isolated from the system prompt
          ├─ LLM provider (Ollama)    overflow recovery, model-list cache
          └─ grounding verifier       every ₹ figure checked against data/tools; regenerate or caveat
observability                          metadata-only traces, /api/metrics
evals                                  scenario benchmark (offline in CI, live against Ollama)
```

| Path | Purpose |
|---|---|
| `backend/main.py` | FastAPI app, request limits, rate limiting, CORS, conversation endpoints |
| `backend/agent/` | Orchestration loop |
| `backend/context/` | Financial profile, fact extraction, summarizer, token budget, context builder |
| `backend/state/` | Intent classifier, conversation/topic state, SQLite session store |
| `backend/finance/` | Deterministic financial engine (`engine.py`) and versioned tax engine (`tax.py`) |
| `backend/verify/` | Numeric grounding verifier |
| `backend/observability/` | Per-turn traces and metrics |
| `backend/evals/` | Evaluation harness and scenarios |
| `backend/tools/` | Tool registry/executor and tools |
| `backend/research/` | Multi-source research, verification and synthesis |
| `backend/prompts/` | TORA and planner prompts |
| `backend/docs/` | Status (generated from the code), roadmap, latency notes, test plans |
| `frontend/` | TORA's chat UI — components that install into Spendsy, see below |

## Run

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
cp .env.example .env                               # adjust as needed
ollama pull gemma4:e4b && ollama serve             # separate terminal
python -m uvicorn backend.main:app --port 8000
```

## Test

```bash
python -m pytest -q
```

## Evaluate

```bash
python -m backend.evals --mode offline                      # deterministic pipeline, runs in CI
python -m backend.evals --mode live --model gemma4:e4b \
    --out evals_live.json --md evals_live.md                # real model plans and answers
python -m backend.evals --mode live --real-web              # also hit real search/fetch
```

Add scenarios to `backend/evals/scenarios.json`; use the live report to compare models and prompt changes.

## API

| Method | Path | Description |
|---|---|---|
| POST | `/api/chat` | `{ "message": "...", "conversation_id"?: "..." }` → `{ response, model, conversation_id, intent }`. Omit the id to start a conversation. Sending `messages` without an id uses the legacy stateless mode. |
| GET | `/api/conversations/{id}` | Transcript, remembered facts and topic state |
| DELETE | `/api/conversations/{id}` | Delete the conversation and its memory |
| DELETE | `/api/conversations/{id}/memory` | Forget remembered facts, keep the transcript |
| GET | `/api/health`, `/api/models` | Provider health and installed models |
| GET | `/api/metrics` | Requests, success rate, latency percentiles, intents, tool health, tokens, grounding outcomes |
| GET | `/api/traces?limit=50` | Recent metadata-only traces (requires `TORA_DEBUG_ENDPOINTS=1`) |

Chat responses also include `grounding: {action, checked, unsupported}`.

In the Spendsy frontend, Vite proxies `/api/chat`, `/api/models`, `/api/health` and `/api/conversations`
to port 8000.


## Frontend — install the chat UI into Spendsy

TORA's chat UI is **not an app**. It is three files that live inside the Spendsy frontend, and
nothing copies them there for you. Pulling this repo and starting Spendsy leaves you looking at
the old chat screen, because the new files never reached the app.

```bash
cd frontend
npm install --legacy-peer-deps
npm test                                          # 33 tests, no Spendsy checkout needed

npm run install-into-spendsy -- D:\Projects\Spendsy        # or ../Spendsy
npm run install-into-spendsy -- ../Spendsy --check          # report only, copies nothing
```

It backs up anything it replaces, does nothing when the files already match, refuses a folder
that is not a Spendsy checkout, and warns if that checkout is missing what the components import
(`@shared/utils/cn` and Spendsy's `src/api`). **Restart the Vite dev server afterwards** — it does
not always notice files swapped underneath it — and hard-reload the browser.

| file | goes to |
|---|---|
| `frontend/src/pages/TORAPage.jsx` | `<spendsy>/frontend/src/pages/TORAPage.jsx` |
| `frontend/src/pages/tora/sse.js` | `<spendsy>/frontend/src/pages/tora/sse.js` |
| `frontend/src/pages/tora/markdown.jsx` | `<spendsy>/frontend/src/pages/tora/markdown.jsx` |

`frontend/src/api.js` and `frontend/src/tests/stubs/` are stand-ins so the tests can run in this
repo. They are never copied — those modules belong to Spendsy.

## Status

`backend/docs/TORA_STATUS.md` is generated from the code by `python -m backend.status`, so it
cannot drift; CI fails if it does. `backend/docs/ROADMAP_TO_100.md` is what is left and in what
order, and `backend/docs/latency.md` records what is slow and why, with measurements.

## License

MIT
