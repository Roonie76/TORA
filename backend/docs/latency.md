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

## What is left, in order of measured value

1. **Write less.** At 4.1 tok/s, every 100 answer tokens is 24 s. Rendering tables,
   figure lists and standing caveats deterministically in code — instead of having the
   model type them — converts decode seconds into zero. The locked-slot table already
   proves the figures are available without the model; the same is true of the layout.
2. **Send less.** History window trimming and further prompt slicing, at 2.6 s per
   100 tokens saved.
3. **More cores, or a smaller model.** The only way past ~4 tok/s decode. Speculative
   decoding needs a draft model, which needs the RAM the slim build just freed.
4. **A two-model cascade** (a tiny model for the planner) is now cheaper to try, for the
   same reason — but see item 1 first.
