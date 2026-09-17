# Phase 6 — Accounts and Spendsy Data (September 2026)

TORA can now tell users apart and read each user's own Spendsy transactions.
It still does not create accounts or log people in: the Spendsy auth service does that.
TORA only checks the token the Spendsy app already has.

## 6A — Accounts

| Setting | Default | Meaning |
|---|---|---|
| `TORA_AUTH_MODE` | `off` | `off`: tokens are ignored and everyone is anonymous (the old behaviour). `optional`: anonymous use is allowed, but a token that is sent must be valid. `required`: every chat needs a valid token. |
| `TORA_AUTH_URL` | `http://localhost:8080/auth` | TORA calls `GET {url}/me` with `Authorization: Bearer <token>`. The Spendsy `{ok, data:{id,...}}` envelope, `{user:{...}}` and a bare `{id,...}` are all accepted. |
| `TORA_AUTH_CACHE_SECONDS` | `60` | How long a valid token is remembered (as a SHA-256 key; the token itself is never stored). |

How it works:

- **Where the token comes from:** the `Authorization: Bearer` header, or the `access_token` cookie when the Vite proxy makes the call same-origin. `TORAPage.jsx` now sends the stored Spendsy token.
- **Error codes:**
  - A missing token (in `required` mode) or a rejected token gets **401** with `WWW-Authenticate: Bearer`.
  - If the auth service can't be reached, TORA returns **503**. It never quietly treats the user as anonymous.
- **Who can see a conversation:** a conversation started by a signed-in user belongs to that user.
  - Anyone else, including anonymous callers, gets **404**, the same answer as for an unknown id, so ids can't be probed.
  - Anonymous conversations still work as before: anyone with the id can use them.
- **Account memory:** financial facts belong to the account, so every conversation for that user shares them.
  - Each turn reloads the latest saved copy while holding a per-user lock, so an update in one tab shows up in the others.
  - Account memory does not expire with conversations. It stays until the user clears it.
- **Rate limiting:** limits are per user when signed in (per IP otherwise). A 429 now includes `Retry-After`.
- **Database migration:** the `sessions` table gets an `owner_id` column, added automatically. A new `user_profiles` table holds account memory.

New endpoints (all except `/api/me` return 404 when `TORA_AUTH_MODE=off`, and 401 when there is no valid token):

| Endpoint | Purpose |
|---|---|
| `GET /api/me` | Auth mode, signed-in state, user id and available features |
| `GET /api/conversations?limit=50` | The user's conversations, newest first (id, title, turns, timestamps) |
| `GET /api/me/memory` | Account memory (JSON plus a readable summary) |
| `DELETE /api/me/memory` | Forget all remembered facts; transcripts are kept |
| `DELETE /api/me/data` | Delete all of the user's conversations and memory |

Changes to existing endpoints:

- `DELETE /api/conversations/{id}` deletes only that transcript. Account memory is kept.
- `DELETE /api/conversations/{id}/memory` clears the account memory when the conversation belongs to an account.

## 6B — Spendsy transactions

- **`spendsy_data` tool** (`backend/tools/spendsy_tool.py`): read-only. It makes GET requests only, always with the user's own token. It has two operations:
  - `spending_summary` (`months`, optional `category`): income, expenses, net and savings rate per month, plus top categories. Averages use complete months only, and the current month is flagged as partial. Transfers are excluded.
  - `recent_transactions` (`limit` ≤ 25, optional `category` or merchant filter).
- **Client** (`backend/integrations/spendsy.py`):
  - Reads `{TORA_FINANCE_URL}/transactions` (default `http://localhost:8080/finance`), page by page with the cursor.
  - Stops once it reaches transactions older than the period asked for, and never reads more than 10 × 100 transactions.
  - Handles `income/expense/credit/debit` types, ISO dates, `dd-mm-yyyy` dates and epoch timestamps, and amounts with commas or ₹.
  - Transactions it can't read are skipped.
- **Only signed-in users can use it.** The planner doesn't list the tool for anonymous requests, and the tool refuses to run without a signed-in user.
- **Treated as untrusted:** descriptions and categories can come from parsed bank statements, so results are placed in the `<external_data>` block like web results. The totals still count as evidence for the answer-checking step, so made-up spending figures are caught and the answer is rewritten.
- **Records stay separate from memory.** Facts the user states in chat stay in memory, and Spendsy records only reach the answer through the tool. When the two disagree, TORA is told to point out the difference.

## Tests and benchmark

- `backend/tests/test_accounts_and_spendsy.py` (30 tests):
  - token parsing, gateway verifier (envelope, caching, 401/500/bad JSON/unreachable);
  - off / optional / required modes, and 503 when the auth service is down;
  - conversation privacy, account memory shared across conversations, clearing memory and deleting account data;
  - per-user rate limit and `Retry-After`, and the database migration from the old schema;
  - transaction normalisation, summary maths, paging and stopping at the requested period, and client errors;
  - the tool refusing anonymous callers, results rendered as untrusted, the planner hiding the tool, and an end-to-end signed-in chat.
- Benchmark: 5 new `accounts` scenarios, 126/126 offline.

## Live check (stand-in gateway)

Run against a stand-in auth and finance service with `gemma4:e4b` (report: `manual_test_report_2026_09.md`, checks P0–P13):
- `/api/me`, 401 for bad tokens, and 404 for other users' conversations all behaved correctly.
- Spending totals matched the data exactly.
- The injected text in a transaction description was ignored.
- Account memory carried into a new conversation, and clear/delete worked.

Two issues were fixed during the run (P5 averages over a partial month, P9 sign-in hint).

## Not done / needs the Spendsy services

- Tested against a simulated auth service and finance service only. Point `TORA_AUTH_URL` and `TORA_FINANCE_URL` at the real gateway and run the manual checks in `manual_test_plan.md` (section "Accounts & Spendsy data").
- The field names in the `/transactions` response were taken from the frontend (`amount`, `type`, `category`, `date`, `is_transfer`, `description`). If the real API uses different names, update `normalise_transaction`.
- The frontend still shows a single chat. A conversation list (using `GET /api/conversations`) and a "clear account memory" button are UI work for a later phase.
