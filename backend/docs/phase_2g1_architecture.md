# TORA Phase 2G.1 — Memory Integrity Hardening Architecture Specification

## 1. Overview & Objectives

Phase 2G.1 provides targeted hardening for TORA's Memory & Context Engine without redesigning the core architecture or introducing external database dependencies (PostgreSQL, Redis, vector stores, embeddings, RAG).

This phase addresses two critical memory integrity issues identified during the 75-turn manual browser acceptance audit:
- **BUG #1 (Hypothetical Infiltration):** Hypothetical or conditional financial amounts in historical messages (e.g., *"If I earned ₹1 lakh..."*) can inadvertently mutate active current facts during stateless conversation rehydration.
- **BUG #2 (Provenance Truncation):** Multi-hop historical state transitions (e.g., `₹1.8L -> ₹1.65L -> ₹1.55L`) lose intermediate history because `FinancialFact` only maintained a single scalar `previous_value`.

---

## 2. Current Memory Data Flow & Lifecycle

```
[User Message / History]
          │
          ▼
   [FactExtractor] ──────────► (Regex + Trigger Matching)
          │
          ▼
   [Candidate Facts] ────────► (name, value, category, status, notes)
          │
          ▼
    [FactManager] ───────────► (Applies candidate facts to FinancialProfile)
          │
          ▼
  [FinancialProfile] ────────► (Mutates income/rent/debts/expenses/investments)
          │
          ▼
   [ContextBuilder] ─────────► (Priority 1..7 Prompt Assembly)
          │
          ▼
     [LLM Prompt] ───────────► (Ollama / Gemma)
```

### 2.1 Current Extraction & Classification Flow
1. `FactExtractor.is_hypothetical(text)` checks for simple trigger substrings (`if i `, `what if `, `suppose i `, `assume i `).
2. If matched, `status` is set to `"hypothetical"`; otherwise `"current"`.
3. Candidate dictionaries are constructed with `name`, `value`, `category`, `period`, `status`.

### 2.2 Current Storage & Rehydration Flow
1. `main.py` creates a fresh `FinancialProfile()` on every incoming request.
2. It iterates through all historical user messages and calls `FactExtractor.extract_candidate_facts(msg.content)`.
3. `FactManager.apply_candidates(profile, cands)` calls `profile.set_fact()`.
4. `agent.run()` also calls `FactExtractor.extract_candidate_facts(current_user_message)`.

---

## 3. Exact Root Cause Analysis

### 3.1 BUG #1 — Hypothetical Mutation of Active Profile
**Root Cause:**
In `backend/context/financial.py`, `FinancialProfile.set_fact()` assigned incoming facts directly to `self.income`, `self.rent`, or `target_dict[clean_name]` regardless of whether `status == "hypothetical"`.
```python
# Flawed logic in set_fact:
if category == "income" or clean_name in ("income", "salary"):
    existing = self.income
    if existing and existing.status == FactStatus.CURRENT.value:
        previous_val = existing.value
    fact = FinancialFact(
        name="income",
        value=value,
        period="monthly",
        status=status,  # <-- status was "hypothetical"
        previous_value=previous_val,
        source=source,
        notes=notes,
    )
    self.income = fact  # <-- BUG: Replaced active current income with hypothetical fact!
```
When `to_context_string()` rendered the profile:
- The verified current salary (`₹82,000`) was displaced into `self.income.previous_value`.
- `to_context_string()` rendered:
  `- Original Monthly Income: ₹82,000 (Updated to current: ₹1 Lakh)`
- Consequently, during long-context turns (Turns 52–53), the LLM was explicitly instructed that the current salary had become ₹1 Lakh.

### 3.2 BUG #2 — Loss of Multi-Hop Provenance
**Root Cause:**
`FinancialFact` contained only a single scalar attribute `previous_value: Optional[Any] = None`.
When a fact underwent multiple sequential corrections:
1. Turn 6: `credit_card_debt = 180000` (initial)
2. Turn 21: `credit_card_debt = 165000` (`previous_value = 180000`)
3. Turn 63: `credit_card_debt = 155000` (`previous_value = 165000`) -> **`180000` (original) was permanently overwritten and lost**.
Furthermore:
- No formal `FactRevision` provenance log existed on the fact.
- `to_context_string()` lacked multi-hop provenance formatting (`Original` vs `Previous` vs `Current`).
- Helper methods `get_current_value()`, `get_previous_value()`, and `get_original_value()` were not available.

---

## 4. Enhanced Phase 2G.1 Architecture

### 4.1 Extended Semantic Fact Statuses
We introduce explicit semantic statuses in `FactStatus`:
- `CURRENT`: Verified, active current financial reality.
- `HISTORICAL`: Explicitly stated past fact (e.g. *"I used to earn ₹75,000"*).
- `HYPOTHETICAL`: Counterfactual exploration (e.g. *"If I earned ₹1 lakh next year..."*).
- `CONDITIONAL`: Dependent future contingency (e.g. *"If my salary increases by ₹10,000..."*).
- `ESTIMATE`: Approximated or unconfirmed figure (e.g. *"I think I spend around ₹20,000"*).
- `UNKNOWN`: Ambiguous / unquantified statement (e.g. *"My expenses increased"*).

### 4.2 Semantic Classifier & Pattern Engine
Enhance `FactExtractor` to:
1. Distinguish between **current statements**, **explicit historical statements**, **hypothetical/conditional inquiries**, and **future expectations**.
2. Identify linguistic markers:
   - *Hypothetical:* `if i `, `what if `, `suppose i `, `assume i `, `imagine i `, `let's say `, `for a scenario where `.
   - *Historical:* `i used to `, `previously i `, `originally my `, `earlier i `, `before my raise `.
   - *Conditional / Future:* `next year i expect `, `will probably increase `, `if my salary increases `.
   - *Estimate:* `i think i spend `, `roughly around `, `approx `.
   - *Same-turn correction:* `my salary is ₹75,000. actually, it is ₹82,000.` -> correctly resolving the final assertion as current and the earlier as historical.

### 4.3 Multi-Hop Provenance & Revision Tracking
Enhance `FinancialFact` with a structured revision log:
```python
@dataclass
class FactRevision:
    value: Any
    status: str
    source: str
    timestamp: str
    notes: Optional[str] = None

@dataclass
class FinancialFact:
    name: str
    value: Any
    currency: str = "INR"
    period: Optional[str] = None
    status: str = FactStatus.CURRENT.value
    revisions: List[FactRevision] = field(default_factory=list)
    source: str = "user"
    notes: Optional[str] = None
    updated_at: Optional[str] = None

    def get_current_value(self) -> Any: ...
    def get_previous_value(self) -> Optional[Any]: ...
    def get_original_value(self) -> Any: ...
    def get_provenance_chain(self) -> List[Any]: ...
```

### 4.4 Strict Isolation of Non-Current Facts in FinancialProfile
In `FinancialProfile.set_fact()`:
- Facts with `status == FactStatus.CURRENT.value`:
  - Update the active slot (`income`, `rent`, `debts[...]`, etc.).
  - Append old value to the fact's `revisions` list.
- Facts with `status in (HYPOTHETICAL, CONDITIONAL)`:
  - Are stored strictly in `self.scenarios` / `self.assumptions`.
  - **NEVER** modify or replace `self.income`, `self.rent`, or other active facts.
- Facts with `status == FactStatus.HISTORICAL.value`:
  - Are stored as historical revisions on the existing fact or recorded in `self.history`.
  - **NEVER** overwrite current active values.

### 4.5 Context Rendering with Multi-Hop Provenance
`FinancialProfile.to_context_string()` renders three distinct, structured sections:
1. `### Active Verified Facts (Current Reality)`
   - Formatted current values.
   - Summarized provenance (e.g., `Credit Card Balance: ₹1.55 Lakh (Previous: ₹1.65 Lakh, Original: ₹1.8 Lakh)`).
2. `### Historical Facts & Revision Provenance`
   - Explicit historical state progression (e.g., `- Credit Card Debt History: ₹1.8 Lakh -> ₹1.65 Lakh -> ₹1.55 Lakh`).
   - `- Original Monthly Income was ₹75,000 (Updated to current: ₹82,000)`.
3. `### Hypothetical Scenarios & Conditional Assumptions`
   - Explicitly marked counterfactuals (e.g., `- [HYPOTHETICAL] Income Scenario: ₹1 Lakh ("If I earned ₹1 lakh next year...")`).

---

## 5. Verification Plan

1. **Unit & Component Testing:**
   - Classification tests for all semantic statuses (`CURRENT`, `HISTORICAL`, `HYPOTHETICAL`, `CONDITIONAL`, `ESTIMATE`, `UNKNOWN`).
   - Provenance tracking tests verifying multi-hop retrieval (`get_current_value()`, `get_previous_value()`, `get_original_value()`).
   - Stateless rehydration simulation verifying that 50+ turns with hypothetical queries do not pollute active state.
   - Same-turn correction tests (`₹75k -> actually ₹82k`).
   - Security injection resistance on memory facts.
2. **Full Automated Suite:**
   - Execute all 338+ tests to guarantee zero regressions.
3. **Browser Acceptance Verification:**
   - Re-run the 75-turn acceptance test verifying 100% accuracy on Turns 52, 53, 54, 55, 64, 65, and 66.
