# TORA: the path to 100%, without changing the model or the box

Written 2026-09-18. The constraint is deliberate: **gemma4:e4b on 2 CPU cores and 8 GB stays**.
Everything here is reachable without a GPU, a bigger model or a faster machine.

Today's honest position, from `backend/docs/TORA_STATUS.md` and the measurements behind it:

| area | today | what 100% means |
|---|---|---|
| Features | 88% | the five named gaps closed |
| Correctness & safety | 80% | no figure reaches a user that an engine did not produce |
| Performance for real users | 40% -> **60%** | no common question waits on the model at all |
| Operations | 35% -> **50%** | it runs itself: CI, dashboard, a completed manual pass |

## The reframe that makes 100% possible

Prefill runs at 39 tokens/sec and decode at 4.1. Those numbers are the hardware, and they are not
moving. So performance cannot be won by making the model faster. It has to be won by **needing the
model less**, and the work splits into three tiers:

**Tier 0 — no model at all.** The engines already hold the whole answer for a large class of
questions: "what is my rent", "EMI on 50 lakh at 9% for 20 years", "tax on 13.75 lakh". The figures
are computed, formatted and labelled before the model is ever called. A templated sentence around
them is an answer, and it costs zero seconds. This is the single biggest remaining lever and it is
the opposite of a model upgrade: it is deleting the model from the hot path.

**Tier 1 — the model writes only prose.** Built today: the engine renders its figures, streams them
first (0.1s), and the model writes the judgement over the top. This is where advice-shaped questions
land, and it is already acceptable.

**Tier 2 — the model writes everything.** Open-ended questions with no engine behind them. These
stay slow, so they stop blocking: the turn is accepted, the user is free, and the answer arrives
when it arrives.

Once every question is in a tier, "performance" stops meaning "the model is slow" and starts meaning
"the right tier for the question", which is a code problem and therefore finishable.

## The work, in order

### 1. CI wiring  *(ops 35% -> 50%)*  — **DONE**
`python -m backend.check` is comprehensive and nothing runs it automatically. A workflow on every
push and pull request. **Done when** a red gate blocks a merge.

### 2. Tier 0: deterministic answers  *(performance 40% -> 70%)*  — **DONE for the finance engine**
For every turn where the engines produced a complete result and the question matches a known shape,
render the whole answer from a template and never call the model. The figure block proves the data
is there; this adds the sentence around it. Live: an EMI question now answers in **0.04 s with no model call**, and a required-SIP
question in 2.6 s. Still open: tax, which needs the rules library folded in so the answer keeps
its legal basis.

### 3. Answer cache  — **BUILT, MEASURED, AND REMOVED**

The idea was sound and the implementation worked: key on the user, the question, this turn's
engine results and the system prompt, so any changed input is a miss rather than a stale hit.

Then the test suite failed in a way that mattered. One test was being served another's answer,
because the key did not include the conversation history — and a model-written answer depends on
it ("as I mentioned", "that loan"). Putting history in the key makes the cache correct and, for a
conversational turn, makes it never hit.

So the question became: which turns could it safely serve? Across the 61 live turns:

| | turns | |
|---|---|---|
| no tools at all | 41 | history-dependent, so only safe with history in the key, where it never hits |
| a deterministic engine result | 16 | already answered by tier 0 in 0.04 s with no model at all |
| live data | 3 | never cacheable — a rate fetched an hour ago is not a fact |

Every turn it could serve safely is already faster without it, and every turn it would speed up
is one it cannot serve safely. So it was removed rather than shipped: a staleness bug in a finance
tool is the exact failure this project keeps working to avoid, and a cache is a machine for
producing them.

**Do not rebuild this without first showing a class of turn that is slow, repeated, and does not
depend on conversation history.**

### 4. Non-blocking long turns  *(performance 60% -> 85%)*
A Tier 2 turn returns immediately with a handle; the answer streams into the conversation when
ready, and the UI already has the machinery for a reply arriving late. **Done when** no request
holds a connection for minutes.

### 5. Tool retries and circuit breakers  *(features)*  — **DONE**
One flaky call should not lose a turn. **Done when** a tool failing twice is skipped with the answer
saying so, and a repeatedly failing tool is held open.

### 6. Observability dashboard  *(ops 50% -> 70%)*
A single page over the metrics and traces that already exist. **Done when** turn latency, tool
mix, grounding rate and error rate are visible without reading JSON.

### 7. The manual browser pass  *(ops 70% -> 100%)*
149 tests, desktop and mobile, by hand. Cannot be automated away and has never been completed.

### 8. The remaining feature gaps  *(features 88% -> 100%)*
General scenario engine; autonomous re-research; broader tax coverage; per-fact confidence and
temporal queries.

### 9. Correctness to 100%  *(the one that is never "done")*
Every serious bug in this project was found by running it against the real model and reading the
output by hand — the invented ₹20,000 rent, the ₹37 Lakh, the false deletion, the sign flip, the
doubled units. The tests were written afterwards, every time. So the route to 100% is not more unit
tests; it is more live runs with human eyes, each one ending in a test. **Done when** a full live
pass produces no new finding.

## The rule underneath all of it

Three separate times now, on this model, a prompt rule was ignored on exactly the turn that
mattered. Locked slots, the figure block and `strip_repeats` all work because none of them ask.

> **What must be true is rendered in code. What is merely asked for is advisory.**

Tier 0 is that rule taken to its conclusion.
