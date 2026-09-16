# TORA PHASE 2G — MEMORY & CONTEXT ENGINE ARCHITECTURE

## 1. Executive Summary

Phase 2G introduces a modular, token-aware Memory & Context Engine for TORA. The primary objective is to eliminate conversation context saturation (which previously caused `done_reason="length"` and HTTP 502 errors on long conversations) while ensuring early financial facts, state updates, and user goals remain persistent, verifiable, and distinguishable across 60+ conversation turns without requiring external database dependencies.

---

## 2. Current Context Flow & Limitations (Phase 2F Baseline)

### 2.1 Current Architecture Flow
```
User Message
     ↓
FastAPI (/api/chat)
     ↓
ConversationContext (List of raw ConversationMessage)
     ↓
ToraAgent._build_messages()
     ↓
ContextBuilder.build()
     ↓ [Takes trailing MAX_HISTORY_MESSAGES = 20]
LLMProvider (OllamaProvider with num_ctx=8192)
     ↓
LLM Response / LLMResponseError (done_reason="length" -> HTTP 502)
```

### 2.2 Critical Limitations
1. **Fixed Message Count vs. Variable Token Density**: `MAX_HISTORY_MESSAGES = 20` counts message turns, not tokens. Because TORA generates detailed markdown responses (3,000–4,500 characters per turn), 16–20 messages accumulate ~5,500 prompt tokens. On thinking models with an 8,192 context window, the model's internal reasoning chain exhausts the remaining ~2,000 token budget, returning `done_reason="length"` with an empty response.
2. **Context Window Amnesia**: Once a conversation exceeds 20 messages, early facts (e.g. Turn 1 salary of ₹75,000 or Turn 10 gold holdings of ₹4 lakh) slide out of the prompt window.
3. **No Structured State Differentiation**: The system relies on raw text history rather than a verified structured financial profile. When values change (e.g. salary ₹75k $\rightarrow$ ₹82k), the LLM must search raw text to find the latest value.
4. **Binary Failure on Context Overflow**: An empty `done_reason="length"` immediately raises an `LLMResponseError` and returns HTTP 502 without attempting automatic context compaction or retry.

---

## 3. Proposed Phase 2G Architecture

```
                       User Prompt + Request History
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │   FactExtractor      │
                         │ (Extracts candidates)│
                         └──────────┬───────────┘
                                    │ Validated facts
                                    ▼
                         ┌──────────────────────┐
                         │   FinancialProfile   │ ◄── [Tracks Current vs Previous,
                         │ (Structured Entity)  │      Provenance, Status]
                         └──────────┬───────────┘
                                    │
    ┌───────────────────────────────┴───────────────────────────────┐
    │                                                               │
    ▼                                                               ▼
┌───────────────────────┐                               ┌───────────────────────┐
│ ConversationSummarizer│                               │  TokenBudgetManager   │
│ (Condenses old turns) │                               │ (Calculates available │
└───────────┬───────────┘                               │  budget & allocations)│
            │                                           └───────────┬───────────┘
            │                                                       │
            └───────────────────────┬───────────────────────────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │    ContextBuilder    │
                         │ (Applies Priorities) │
                         └──────────┬───────────┘
                                    │
                       1. System Prompt (Protected)
                       2. Current Financial Profile (Protected)
                       3. Tool / Web Context (Untrusted Data Isolation)
                       4. Conversation Summary (if condensed)
                       5. Recent History (Token-budget bounded)
                       6. Current User Prompt (Protected)
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │      ToraAgent       │
                         │ (Overflow Recovery)  │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │     LLMProvider      │
                         └──────────────────────┘
```

---

## 4. Component Design & Responsibility Matrix

| Component | File Path | Primary Responsibility |
|---|---|---|
| **TokenBudgetManager** | `backend/context/token_budget.py` | Provider-independent token estimation, reservation for generation, system, prompt, and token-bounded history allocation. |
| **FinancialProfile** | `backend/context/financial.py` | Strongly typed financial entity models (`FinancialFact`, `FinancialProfile`), state transitions (`current`, `historical`, `hypothetical`), and formatted context rendering. |
| **FactExtractor** | `backend/context/extractor.py` | Rule-based & regex candidate fact extraction, Indian financial notation normalization (`₹`, `lakh`, `k`), and hypothesis tagging. |
| **ConversationSummarizer**| `backend/context/summarizer.py` | Structured, non-hallucinatory condensation of older conversation turns preserving goals, decisions, recommendations, and unresolved items. |
| **ContextBuilder** | `backend/context/builder.py` | Assembly of LLM prompt conforming strictly to the priority hierarchy and token budget constraints. |
| **ToraAgent** | `backend/agent/agent.py` | Autonomous tool loop orchestration, fact extraction trigger, observability logging, and 1-shot context reduction overflow recovery. |
| **FastAPI Gateway** | `backend/main.py` | Full backward-compatible API contracts (`POST /api/chat`, `GET /api/health`, `GET /api/models`). |

---

## 5. Context Priority Hierarchy

1. **System Instructions** (Priority 1 — Never truncated)
2. **Current User Message** (Priority 2 — Never truncated)
3. **Verified Financial Profile** (Priority 3 — Current facts, active debts, assets, goals)
4. **Deterministic Tool Results** (Priority 4 — Calculator outputs)
5. **External Web Research** (Priority 5 — Compacted, marked as untrusted)
6. **Recent Conversation Turns** (Priority 6 — Sliced to fit remaining token budget)
7. **Conversation Summary** (Priority 7 — Compressed representation of older turns)
8. **Older Low-Value Raw Turns** (Priority 8 — Safely dropped when summarized)

---

## 6. Overflow Recovery Specification

1. When `OllamaProvider` (or any LLMProvider) returns `done_reason="length"` with an empty response:
   - Detect context length exhaustion.
   - Trigger `ContextManager.reduce_context()`:
     - Drops 50% of the oldest raw messages and triggers immediate summarization.
     - Tightens tool and web context limits.
   - Retry generation **exactly once** with the compacted context.
   - If retry succeeds $\rightarrow$ return generated response with `metadata={"recovered_from_overflow": True}`.
   - If retry fails $\rightarrow$ raise clean structured `LLMResponseError`.
   - Maximum automatic retries: **1** (Strictly prevents infinite loops).

---

## 7. Files to Modify vs. Files to Keep Untouched

### Files to Create / Modify:
- [NEW] `backend/context/token_budget.py`
- [NEW] `backend/context/extractor.py`
- [NEW] `backend/context/summarizer.py`
- [MODIFY] `backend/context/financial.py`
- [MODIFY] `backend/context/conversation.py`
- [MODIFY] `backend/context/builder.py`
- [MODIFY] `backend/context/__init__.py`
- [MODIFY] `backend/agent/agent.py`
- [MODIFY] `backend/main.py`

### Files to Keep Untouched:
- `backend/tools/calculator.py`
- `backend/tools/registry.py`
- `backend/tools/executor.py`
- `backend/tools/search/`
- `backend/tools/web/`
- `backend/planner/`
- `backend/prompts/` (preserves existing strict confidentiality & tool schemas)

---

## 8. Test Strategy & Acceptance Criteria

1. **Unit & Component Tests**:
   - `test_token_budget.py`: Budget calculation, priority slicing, safety margins.
   - `test_financial_profile.py`: Fact extraction, state updates, previous vs current tracking, hypothetical isolation.
   - `test_summarizer.py`: Structured summary formatting, zero hallucination, retention of goals & recommendations.
   - `test_context_overflow.py`: Overflow detection, single-retry mechanism, no infinite loops.
2. **Full Regression Suite**: Run all 308 existing tests (must remain 100% green).
3. **60-Turn Stress Test**: Execute full longitudinal conversation ensuring **0 context-induced 502 errors** and 100% recall of original vs current financial facts.
4. **Adversarial & Security Verification**: Verify prompt confidentiality and data-instruction isolation.
5. **Manual Browser Verification**: Validate responsiveness and UI rendering in the live React client.
