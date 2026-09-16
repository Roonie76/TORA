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
| `backend/docs/` | Phase plans, completion reports and audits |
| `frontend/src/pages/TORAPage.jsx` | Spendsy chat UI for TORA |

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

## Status

See `backend/docs/` (`tora_reaudit_2026_09.md`, `phase_3a_3b_completion.md`, `phase_4_completion.md`).
Next: live benchmark runs for model selection, authenticated access to Spendsy account data,
streaming responses, Hindi/Hinglish understanding and compliance hardening.

## License

MIT
