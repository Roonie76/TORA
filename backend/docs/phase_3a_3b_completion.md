# Phase 3A (Conversation State + Memory 2.0) & Phase 3B (Financial Engine)

Test suite: 574 → **677 passing**. New modules: `backend/state/`, `backend/finance/`, `backend/tools/finance_tool.py`.

## 3A-1 Server-side conversations
- `/api/chat` accepts `conversation_id`. First message without one creates a conversation; the response returns
  `conversation_id` and the classified `intent`.
- The server stores transcript, financial memory and conversation state in SQLite
  (`TORA_SESSION_DB`, default `backend/.data/tora_sessions.db`; `TORA_SESSION_TTL_DAYS`, default 30).
- Client-sent `messages` are ignored for existing conversations (history can no longer be forged).
  Sending `messages` without an id keeps the old stateless behaviour.
- A turn is saved only if it succeeds; turns in one conversation are serialized with a per-conversation lock.
- New endpoints: `GET /api/conversations/{id}`, `DELETE /api/conversations/{id}`,
  `DELETE /api/conversations/{id}/memory`. Ids are 32-char random capability tokens.
- Frontend TORA page stores the id, resumes after reload, restarts on 404 and deletes the conversation on reset.

## 3A-2 Memory 2.0
- Turn-level provenance on facts and revisions; full JSON round-trip.
- Correction vs update: "Actually it's 65k, not 60k" / "I meant…" / "typo" retract the old value (never shown as
  previous/original; listed under *Corrected Mistakes*). "Correction: my salary increased to…" stays an update.
- Commands: "forget my rent", "delete my credit card details", "forget everything" (values scrubbed from audit
  history too). "I paid off my credit card" / "I no longer pay rent" close an existing fact to 0.
- Questions are never commands ("How do I delete my card?").

## 3A-3 Conversation state, topics, intent
- Deterministic `IntentClassifier` (12 intents) with Indian bank/regulator and product dictionaries.
- Topic tracking with pinning: returning to "the home loan comparison" restores that topic and its research.
- Research, search results and calculations are stored per topic. Follow-ups are rewritten into self-contained
  queries for the planner; if stored research already covers the entities, no new web calls are made.
- Stored third-party research is re-injected only inside the untrusted `<external_data>` block; the trusted
  system message gets structure only (topic, entities, interpretation, calculations).
- Memory recall / delete / clarification turns skip the planner LLM call.
- The planner now sees the user's known facts so it can fill calculation inputs.

## 3B Financial engine (`finance_calc` tool)
Operations: emi, amortization (extra monthly / lump-sum prepayment, interest & months saved), sip_future_value
(with annual step-up), sip_change_impact, required_sip, compound_growth, inflation_adjust, debt_payoff
(avalanche/snowball), emergency_fund, savings_rate, debt_to_income, net_worth. Inputs are validated; outputs carry
inputs, figures, an Indian-format summary and explicit assumptions. Values are checked against standard
calculators (₹20L @ 8.5% / 20y → EMI ₹17,356; ₹10k SIP @ 12% / 10y → ₹23.23L).

## Verified
- Unit/integration tests (677). Real uvicorn run against a fake Ollama: stateful turns, EMI via finance_calc,
  persistence across a server restart, CORS preflight rejection for unknown origins.

## Not verified here
- Behaviour with the real Gemma model (planner choosing `finance_calc` / `research`; answer quality).
  Re-run the stress, adversarial and browser suites against the new `conversation_id` flow.
