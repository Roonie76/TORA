# TORA Re-Audit of "Done" Phases — September 2026

Scope: every phase previously marked DONE (Phase 1, 2A–2F, prompt security, 2G, 2H-A..E).
Method: code read-through, full pytest run, targeted probes against the real modules.
Result: 488 → **574 passing tests**, 0 failing. 13 defects fixed.

## Status per phase

| Phase | Verdict | Notes |
|---|---|---|
| 1 Core agent / context / LLM | Fixed | Shared fallback profile leaked facts across requests; overflow retry fired on any error containing "length"; summary tokens were not budgeted. |
| 2A Tool foundation | OK | — |
| 2B Calculator | Fixed | Huge integer results raised a raw OverflowError (isnan on int) instead of the magnitude error. |
| 2C Executor | Fixed | Live app had no tool timeout (default now 30 s, `TORA_TOOL_TIMEOUT_SECONDS`). |
| 2D Planner | Updated | Planner rules now cover the `research` tool. |
| 2E Agent tool loop | Fixed | Renderer crashed on non-dict tool output (`"conclusions" in <float>`). |
| 2F LLM hardening | Fixed | `/api/tags` was called before every generate (4 HTTP calls/turn); now cached 60 s (`TORA_MODEL_CACHE_SECONDS`). Silent model fallback now logs a warning. |
| Prompt security | Fixed (design) | Web/search/research text was injected into the **system** message. It now goes into a delimited `<external_data trust="untrusted">` user-role message right before the user turn; tag break-out is neutralised; tokens are budgeted. |
| SSRF (2H-C) | Fixed | DNS rebinding (validated IP ≠ connected IP), 2 MB cap only checked after full download, CGNAT / NAT64 / 6to4 not blocked, invalid port crashed. Connections are now resolved, validated and pinned inside the transport, the byte budget is enforced on the socket, env proxies are bypassed, and `Accept-Encoding: identity` limits decompression bombs. |
| 2G Memory | Fixed (partial) | `1.65L` / `2 cr` were parsed as ₹1.65 / ₹2 and dropped (root cause of the "previous CC balance" bug). Questions and calculations overwrote facts ("Calculate 200000 * 0.36 / 12" became the salary; "Is 50k a good salary?" became the salary). Unrelated percentages were stored as card APR. Crore amounts rendered as "₹200 Lakh". |
| 2G Summarizer | Rewritten | It emitted canned lines ("Calculated comparative interest (10% Gold Loan vs 36% Credit Card)", "guaranteed 36% risk-free return") whenever keywords appeared — fabricated calculations in long chats. It now only quotes real excerpts. |
| 2H-A..E Research | Fixed + wired | Pipeline was **not registered** in `/api/chat`. `multi_source_research` had zero tests and its failed-source fallback raised `NameError`. Now exposed as the `research` tool, fetches run concurrently (bounded to 3), and output is rendered as untrusted data. DuckDuckGo snippets were paired by index and could be attributed to the wrong source. |
| API surface | Fixed | No input limits (message ≤ 8000 chars, ≤ 200 history items, temperature 0–2, model-name pattern), `CORS *` with credentials, no rate limit (30/min per client, `TORA_RATE_LIMIT_PER_MINUTE`), `__main__` bound to 0.0.0.0 (now `TORA_HOST`, default 127.0.0.1). |
| Repo hygiene | Fixed | `.gitignore` excluded every `test_*.py`, so the suite could never be committed. |

## Not verified here (needs live Ollama / browser)
- 75-turn stress test, 31 adversarial probes, 42-turn browser acceptance — rerun against these changes.
- Live DuckDuckGo search (blocked from the audit sandbox). Live SSRF-safe fetch of rbi.org.in succeeded.

## Still open (unchanged scope)
- Memory is rebuilt from client-supplied history on every request; server-side memory needs user identity (auth).
- Corrections ("Actually it's 65k, not 60k") are stored as a revision, not as a correction of a wrong value.
- Research follow-ups ("those sources") still need a conversation-state layer; research data lives only in the turn it was fetched.
- Rate limiter is in-process (single worker).

## New / changed tests
`test_memory_poisoning_guards.py`, `test_web_fetch_transport_security.py`, `test_context_builder_isolation.py`,
`test_reliability_hardening.py`, `test_research_tool_integration.py`, `conftest.py`; updated
`test_context_compaction.py`, `test_prompt_security.py`, `test_web_fetch.py`, `test_search.py`,
`test_summarizer.py`, `test_search_normalization.py`.
