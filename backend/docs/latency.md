# Where TORA's response time actually goes

Everything here is measured on the live box — gemma4:e4b on 2 CPU cores and 8 GB RAM,
via `llama-server`'s own timings and `/api/traces`. Nothing is estimated.

## The two rates that decide everything

| | rate | cost |
|---|---|---|
| prefill (reading the prompt) | **39 tok/s** | 100 prompt tokens = 2.6 s |
| decode (writing the answer) | **4.1 tok/s** | 100 answer tokens = 24 s |

A written token is **9.5x more expensive than a read one**. Every latency decision
follows from that one ratio.

Across 82 live turns the answer length was median **179 tokens (44 s of decode)** and
p90 **903 tokens (220 s)**. On this box, answer length is the single largest term.

## The KV cache is the other lever

llama.cpp reuses the KV cache for the longest *identical* prefix. Measured on the same
prompt: unchanged prefix replayed in **260 ms**, the same prompt cold took **39,096 ms** —
150x. Change one byte near the start and the whole prompt is re-read (39,892 ms).

So the system message is ordered stable-first, volatile-last, and slices are sticky
(`ConversationState.prompt_sections`): the prompt may grow but never shrinks and regrows,
because a slice that shrinks costs a full re-prefill for nothing.

## What was fixed (commit `latency: stop re-reading the prompt the model already knows`)

| | before | after |
|---|---|---|
| "My salary is 88k and rent is 22k" | ttft 34.2 s | **13.1 s** |
| "What is my salary?" | ttft 18.1 s | 17.4 s |
| "And my rent?" | ttft 159.2 s, 5,938 prompt tokens | **1.0 s, total 4.1 s, 1,243 tokens** |

Four changes, all about the prompt rather than the model:

* per-turn notes moved to the tail of the system message, so the stable part stays
  byte-identical and the cache holds;
* sticky prompt slices;
* `backend/planner/tool_filter.py` — skip the planner on turns answerable from memory,
  and otherwise send only the plausible tool schemas. The planner prompt is ~3,683 tokens
  of schemas (finance_calc alone is 930) and **19 of 35** live planner calls returned
  "no tools needed";
* the intent classifier now reads a bare possessive question ("And my rent?") as a recall
  instead of financial_qa, which had been buying a 53-second planner call for nothing.

## A hypothesis that measurement killed

The model is 9.6 GB on an 8 GB box, so "it doesn't fit in RAM" looked like the biggest
remaining lever. Reading the GGUF showed two real problems:

* `per_layer_token_embd.weight`, a [10752 x 262144] lookup table, is left at **BF16** in a
  file advertised as Q4_K_M — **5,376 MiB, 65% of the entire text tower**;
* 1,411 of the 2,131 tensors are the **vision (16 blocks) and audio (12 blocks) towers**,
  942 MiB that a text-only assistant never reads.

`backend/ops/slim_gguf.py` fixes both in one pass — it requantizes that one tensor to Q8_0
and drops the towers, copying every other byte through. (`llama-quantize` cannot: COPY mode
ignores `--tensor-type`, and any real base type would requantize the other 719 tensors too.)

Result: **9.61 GB -> 5.98 GB**, and at temperature 0 the slim model's answers are
**byte-identical** to the original's on the four probe prompts.

And it made no difference to speed. Same harness, same 1,157-token prompt:

| | prefill | decode |
|---|---|---|
| original 9.61 GB | 37.6 / 39.0 tok/s | 4.13 / 3.87 tok/s |
| slim 5.98 GB | 40.5 / 39.6 tok/s | 4.19 / 3.60 tok/s |

Within noise. **This box is compute-bound on 2 cores, not memory-bound.** The slim model is
worth keeping for headroom — it removes the OOM kill that took out one turn of the stress
run — but it is a stability fix, not a latency fix.

The value of the experiment is the negative result: on this hardware nothing about the
*model file* will make it faster. Only sending fewer tokens and writing fewer tokens will.


## Lever A: the engine prints its own figures

Built in `backend/answer/blocks.py`. A turn whose tools produced figures gets a block rendered
from the locked slots (already trusted-tool-only, already formatted, already the grounding
whitelist), streamed **ahead of** the model's text, and the model is told not to repeat it.

Live, same five-turn debt conversation as the V01-V05 pass:

| | original | answer-shape rules only | + rendered block |
|---|---|---|---|
| time to first content | 217.7 s | 217.7 s | **0.1 s** |
| total turn | 719.9 s | 719.9 s | **525.3 s** |
| words | 368 | 299 | 262 (block included) |

The 0.1 s is the headline: the figures are on screen before the model has written a word, and
they are the part of the answer the user actually came for.

### What did not work, and what replaced it

Told plainly that the figures were already displayed, gemma4:e4b restated all eight of them and
the answer grew to **443 words — longer than before the change**. That is the second time in one
day a prompt rule was ignored on the turn that mattered, so the repetition is now removed rather
than requested: `strip_repeats` drops a list item or table row whose numbers all came from the
block and which carries no advice of its own, keeping prose that uses a figure meaningfully
("debt-free in 9 months") and any line with a figure the block does not have.

Stripping cleans the answer but recovers no time — the tokens were already generated. So a turn
with a block also gets a tighter token budget (`TORA_BLOCK_ANSWER_TOKENS`), since the figures are
guaranteed on screen and only the prose is left to write. 320 proved too tight: it cut a debt
plan mid-action-list. 450 is the current value and wants one more live run to confirm.

### The rule this establishes

Three times now the same thing has held: **what must be true gets rendered in code; what is
merely asked for is advisory.** Locked slots for the figures, the block for the layout, and
`strip_repeats` for the repetition — none of them depend on the model cooperating.

## What is left, in order of measured value

1. **Confirm the 450-token cap** does not truncate a long plan, and extend the block to
   the standing caveats, which are still typed every time.
2. **Send less.** History window trimming and further prompt slicing, at 2.6 s per
   100 tokens saved.
3. **More cores, or a smaller model.** The only way past ~4 tok/s decode. Speculative
   decoding needs a draft model, which needs the RAM the slim build just freed.
4. **A two-model cascade** (a tiny model for the planner) is now cheaper to try, for the
   same reason — but see item 1 first.

## Tier 0: the turns that call no model at all

Confirmed live on localhost, with the trace showing the model call count:

| question | before | after | model calls |
|---|---|---|---|
| "EMI for a 50 lakh home loan at 9% for 20 years?" | ~40 s | **0.04 s** | **0** |
| "How much monthly to reach 5 lakh in 3 years at 12%?" | 235 s | **2.6 s** | 1 (planner only) |
| "Should I take that loan?" | 367 s | 367 s | 3 — correctly left to the model |

All 31 engine operations already write their own summary sentence, so for an unambiguous
calculation there is nothing for a model to add. The fast path had removed the planner call; this
removes the answer call.

The gate is narrow on purpose, and the eval suite wrote most of it. tax_calc is excluded because
every tax answer has to cite the section it rests on and the engine summary does not carry it.
The comparison engines are excluded because their figures are half the answer. Anything asking
what to *do* — "should I", "which is better", "prepay or invest" — goes to the model, which is
what the model is for.

## Full localhost pass, 19 Sep

Real browser against the real backend and the real model, end to end through the chat UI.

| question | end to end | model calls |
|---|---|---|
| EMI on 50 lakh at 9% for 20 years | **0.57 s** | 0 |
| Monthly needed to reach 5 lakh in 3 years | **0.54 s** (was 20.35 s) | 0 |
| What 10 lakh is worth in 10 years at 6% inflation | **0.55 s** | 0 |
| Recall: "What is my salary?" | 4.2 s | 1 |
| Follow-up: "And my rent?" | 16.6 s | 1 |
| Tax on 13.75 lakh | 185 s (figures on screen in 0.06 s) | 1 |
| "Prepay or invest?" — judgement, left to the model | 345 s (figures in 203 s) | 2 |

The 20.35 s -> 0.54 s came from the pass itself: a turn the engine answered by itself was still
paying for an LLM fact-extraction call that returned nothing. A self-contained sum is not a
statement about the user.

Load and robustness, same box: 200 conversations, 600 turns, 67.3 turns/sec, p95 7.3 s, zero
errors; 50 signed-in users isolated; 20 parallel turns on one conversation with no lost or
duplicated messages; 30 cancelled streams with no leftover tasks; rate limiting returns 429 with
Retry-After; 346 malformed and hostile payloads with zero server errors. Peak RSS 86.6 MB.

What is still slow is what should be: a question that asks what to *do*. The figures for it are
on screen in seconds; the judgement takes minutes, and only different hardware changes that.

## Tier 0 for tax

Tax was held out of tier 0 because an answer must cite the rule it rests on and the engine
summary does not carry that. It does not have to: `tax_calc` already returns those citations from
the reviewed rules library, and the direct answer now renders them.

Live: **"How much tax on a 13.75 lakh salary?" went from 185 s to 0.04 s with no model call**,
carrying the tax year, the regime, the figure and four citations including the rebate section.

The guarantee is enforced rather than tested: a `tax_calc` result with no `legal_basis` does not
take this path at all, and goes to the model, where the prompt's citation rules apply. Writing
that guard immediately caught a gap it was designed to catch — `capital_gains_tax` and
`advance_tax_plan` return through a different branch that never attached a legal basis, so those
answers had been citing nothing since they were written. The rules were in the library; nothing
reached for them.

Choosing between regimes still goes to the model. That is a recommendation, not a figure.
