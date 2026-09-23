# TORA: carrying on without me

Written 2026-09-23, for Rohin, to be read before changing anything.

`ROADMAP_TO_100.md` says what is left. `TORA_STATUS.md` is generated from the code
and says what exists. This file is the part that lives in neither: **why the code
is shaped the way it is**, so a change does not quietly undo a decision that cost
a week to learn.

---

## 1. The one rule

> **What must be true is rendered in code. What is merely asked for is advisory.**

Three separate times, on this model, a prompt rule was ignored on exactly the turn
that mattered. Not usually — on the turn that mattered. The answer-shape rules cut
a debt plan from 368 words to 299 and the model still opened with two sentences it
had just been told not to write.

So anything that **must** hold is rendered in code and never asked for in a prompt:

| what must hold | what holds it |
|---|---|
| every figure comes from an engine | `backend/answer/slots.py` (locked slots) |
| figures appear as a table, formatted | `backend/answer/blocks.py` |
| the model does not retype the labels | `strip_repeats()` in the same file |
| a tax answer carries its legal basis | invariant in `backend/answer/direct.py` |

If you find yourself writing "the model should always…" into a prompt, that is the
signal to render it instead.

## 2. Why it is fast now, and what must not be undone

The hardware is the constraint and it is not moving: **prefill 39 tokens/sec,
decode 4.1**. A written token costs 9.5× a read one. So speed was never won by
making the model faster — it was won by **needing the model less**. Three tiers:

- **Tier 0 — no model at all.** The engines already hold the whole answer. A
  template sentence around the rendered figures *is* the answer. EMI, SIP,
  inflation, tax, and "what was my rent in March" all land here: **0.04s, zero
  model calls.** `backend/answer/direct.py`, `backend/answer/temporal.py`.
- **Tier 1 — the model writes only prose.** Engine renders figures, streams them
  in 0.1s, model writes the judgement over the top.
- **Tier 2 — the model writes everything.** Open-ended advice. Still minutes, but
  no longer blocking (see §3).

**Do not "simplify" by routing a Tier 0 question back through the planner.** A
planner call is ~24 seconds of prefill for the largest schema. `tool_filter.py`
narrows the schemas shown, and the intent only fills in where the wording named no
engine — reversing that put a 930-token schema in front of the planner for a tool
it was never going to pick.

**Do not rebuild the answer cache.** It was built, measured, and removed, and
`ROADMAP_TO_100.md` records the bar it must clear to come back: a class of turn
that is slow, repeated, and does not depend on conversation history. No such class
was found — every turn it could serve safely was already faster without it.

## 3. Background turns (`backend/turns.py`)

`/api/chat/stream` ties a turn to its socket; closing the connection kills it. A
debt plan takes ~345s, longer than a phone stays awake.

`/api/chat/async` accepts the turn and runs it in the background:

```
POST   /api/chat/async             -> 202 {turn_id, poll, events}
GET    /api/chat/turns/{id}        -> status, answer so far, result
GET    /api/chat/turns/{id}/events -> SSE, opening with a snapshot
DELETE /api/chat/turns/{id}        -> cancel
```

Things to know before touching it:

- The buffer keeps the **state** of the answer, not the history of its typing.
  Tokens coalesce into one string; `replace` resets it. Keeping every token event
  would make memory grow with how long someone watched.
- A **running turn is never evicted** by the retention ceiling. Evicting one
  throws away work in progress, which is the failure the module exists to prevent.
- It is **not durable across a restart**, deliberately. Making it so needs a real
  queue, which is a separate decision.
- The frontend re-attaches on a dropped connection and polls before retrying, so
  an answer that finished while disconnected is simply collected.

## 4. Memory, and the thing it must never do

`backend/context/financial.py`. Facts carry revision chains with timestamps.

**A timestamp records when TORA *learned* a value, not when it became true.** A
rent that changed in March and was mentioned in June is recorded in June. So
`value_as_of()` answers "what did you have on file in March", and where it cannot
evidence a date it says so rather than returning its newest figure — which would
read exactly like memory working, and be wrong.

**A retracted value must never answer a question about the past.** The user said
it was never true. Handing it back as history is worse than saying nothing,
because it looks like memory working. `_valid_revisions()` excludes them; keep it
that way.

## 5. What actually finds bugs

Every serious bug in this project was found by **running it against the real model
and reading the output** — the invented ₹20,000 rent, the ₹37 Lakh, the false
deletion, the sign flip, the doubled units, the "true figures checked" chip, the
table falling off a 390px phone. The tests were written afterwards, every time.

Three of those were found by *screenshotting*, and none by tests.

So: the route to correctness is not more unit tests. It is more live runs with
human eyes, each one ending in a test. `backend/docs/manual_test_plan.md` has 149
of them; a full pass has still never been completed.

## 6. Running it

```bash
# from the repo root
python -m uvicorn backend.main:app --port 8000
cd frontend && npm run dev          # UI on :5173

python -m backend.check             # the gate: tests + 160 scenarios + rules
python -m backend.status            # regenerate TORA_STATUS.md (CI checks it)
```

No `.env` is needed — every setting has a working default (Ollama at
`127.0.0.1:11434`, model `gemma4:e4b`, auth off, DuckDuckGo search).

**The gate must be green before any commit**, and if the test count changes,
`python -m backend.status` must be re-run or CI goes red on the doc check. That
has bitten twice.

Python 3.10 works (CI covers 3.10, 3.11, 3.12 — 3.10 is in the matrix because an
SSRF bug that only appeared below 3.11.10 reached a dev machine when CI tested
neither).

## 7. What I would do next, in order

1. **The 149-test manual browser pass.** Desktop and mobile, by hand. This is the
   highest-value thing left and it does not need me — it needs eyes. A first
   partial pass found three defects, two of which no test could see.

2. **More tax coverage.** Presumptive taxation (44AD / 44ADA) is done, and the
   way it was done is the template for the rest — **read this before adding any
   tax rule.**

   I first refused this work, on the grounds that I could not verify Indian
   thresholds against a primary source. That was wrong: incometaxindia.gov.in is
   reachable, and I had not tried. Worth recording, because the failure mode was
   not recklessness but the opposite — declining real work on an assumption I had
   not tested.

   The care was still warranted. Two official pages disagreed on 44ADA: one
   carried the pre-2024 text with no proviso at all. A tax answer ships its legal
   basis, so a wrong threshold becomes a wrong figure *with a citation attached*,
   and the citation is what makes it believable.

   So the order is fixed, and it is rules-library-first:

   1. verify the figure against the Act on incometaxindia.gov.in, and
      cross-check a second page — they do disagree;
   2. add it to `backend/knowledge/rules.json` with `source_url`, `verified_on`
      and a citation that names the amending Act ("proviso inserted by Finance
      Act 2023, w.e.f. 1-4-2024"), so the reader can date it;
   3. only then write the operation, and have it **read the figures out of the
      rule** rather than restating them. A threshold with two homes will
      eventually differ in two homes, and the one nobody updates is the one that
      answers. `test_presumptive_tax.py` changes a library figure and asserts the
      answer follows; keep that pattern.

   Scope a rule to the tax years you actually verified. The Income-tax Act, 2025
   renumbers these provisions from 2026-27 and only secondary sources describe
   the new numbering, so the presumptive rules carry no citation for that year.
   A guessed section number is worse than none, because it reads as authority.

3. **Use confidence in answers.** The score exists and `/api/me/memory` shows it,
   but nothing caveats a reply built on a low-confidence fact. That means a
   prompt change, so it has to be measured against the 160 scenarios rather than
   assumed — see §1 on why prompt rules are advisory.

4. **Corroboration counting.** Confidence deliberately ignores how often a value
   has been repeated, because restating a figure leaves no trace today. Adding
   one means touching the write path, which is why it was left alone.

5. **Alert delivery.** Detection, severity and log lines exist; shipping them to
   a webhook or pager is a deployment decision that was left open on purpose.

Deliberately *not* on this list: teaching the fast path to parse capital-gains and
advance-tax questions. Both need several amounts pulled out of prose and assigned
to distinct roles — purchase against sale, TDS against advance tax paid — and
swapping a pair produces a confidently wrong tax figure with a legal citation
attached. That is the exact failure this project keeps working against, and it
costs more than the seconds it would save. Complex tax routes to the planner on
purpose.

Also not on it: rebuilding the answer cache. See §2.

## 8. If something looks wrong

The commit messages are long on purpose. `git log` is the design record: each one
says what was wrong, what changed, and what was deliberately *not* done. If code
and a doc disagree, the code wins — fix the generator, not the prose.
