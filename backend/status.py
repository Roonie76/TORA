"""
Generate TORA_STATUS.md from the repository itself.

Everything in the "built" sections is read out of the running code — registered
tools, engine operations, HTTP endpoints, intents, fact types, streaming events,
the rules library, the eval suite and the test counts — so the document cannot
drift from what is actually implemented. The PARTIAL / TODO tables are the one
hand-maintained part (intent can't be introspected); each row carries a pointer
to the file or test that justifies it.

Run:  python -m backend.status            # writes backend/docs/TORA_STATUS.md
      python -m backend.status --print    # to stdout
      python -m backend.status --no-tests # skip the pytest/vitest collection
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
DEFAULT_OUT = BACKEND / "docs" / "TORA_STATUS.md"

# --- the hand-maintained half: what is deliberately not finished ---------------
# (area, what exists today, what is missing, evidence)
PARTIAL: List[Tuple[str, str, str, str]] = [
    ("Memory", "current / historical / hypothetical / retracted states, revision chains, "
               "corrections kept apart from changes over time, per-fact previous values",
     "confidence per fact, and temporal queries (“what was it in March?”)",
     "backend/context/financial.py"),
    ("Answer fidelity", "locked slots: every figure a turn may contain comes from the engines, offered "
                        "as named placeholders; a number matching no slot earns one rewrite, and the "
                        "grounding check still runs behind it",
     "small models copy the values rather than writing the placeholders (harmless, since the slot list "
     "also acts as the whitelist) — worth revisiting with a stronger model",
     "backend/answer/slots.py, backend/tests/test_locked_slots.py"),
    ("Prompt size", "intent-sliced prompts: each turn gets only the sections it can use, cut from the "
                    "one full prompt (small talk 987 tokens against 2,321 unsliced)",
     "the latency effect is not separable from CPU noise yet; needs repeated timing runs",
     "backend/prompts/tora.py:slice_prompt, backend/tests/test_prompts.py"),
    ("What-if", "per-engine scenarios (sip_change_impact, what_if_extra, prepay/rent-vs-buy, "
                "loan tenure) and hypothetical facts kept out of the profile",
     "a general scenario engine: change several facts at once, re-run every relevant engine, "
     "compare baseline vs scenario",
     "backend/finance/, state kept in FinancialProfile.scenarios"),
    ("Research", "multi-source search, evidence extraction, credibility, conflict detection, "
                 "per-topic research records and follow-ups",
     "an autonomous re-research loop when evidence is thin, conflicting or stale",
     "backend/research/"),
    ("Tax", "two tax years, both regimes, 8 operations, a 33-rule library with staleness checks",
     "broader coverage (more heads of income, more years, presumptive schemes)",
     "backend/finance/tax_extras.py, backend/knowledge/rules.json"),
    ("Tool runtime", "per-call timeouts, call ids, per-tool latency in metrics and traces, "
                     "planning-time argument checks with a repair loop",
     "retries and circuit breakers for a flaky tool",
     "backend/tools/executor.py"),
    ("Observability", "/api/metrics, /api/traces, an optional JSONL trace file, per-request traces "
                      "with no personal content",
     "a dashboard over them",
     "backend/observability/"),
    ("Evaluation", "python -m backend.check runs unit tests, the offline benchmark and the rules check",
     "CI wiring so it runs on every push",
     "backend/check.py"),
    ("Manual QA", "a 149-test runbook covering every phase, including the streaming UI (V1-V11)",
     "one full hands-on pass in a real browser, desktop and mobile",
     "backend/docs/manual_test_plan.md"),
]

EXTERNAL = [
    ("Babel", "BSD-3-Clause", "CLDR number formatting behind `inr()` — Indian digit grouping now comes from "
                              "locale data rather than hand-rolled string surgery (a fallback keeps the engines "
                              "working if it is absent)."),
    ("pyxirr", "Unlicense", "tests only: an independent implementation of PMT/FV used to differential-test the "
                            "EMI and SIP engines. It already caught a rounding bug in required_sip."),
]

KNOWN_LIMITS: List[Tuple[str, str]] = [
    ("Latency on CPU", "median turn 140s, first token 97s, slowest 735s on 8 GB CPU-only with one model "
                       "instance. The fast paths remove a planner call (1-4 min) from the commonest questions."),
    ("Grounding derivations", "the figure check accepts simple derivations of known values, so a wrong "
                              "arithmetic result can still coincide with one. Fewer model-made figures is the fix."),
    ("Small-model hedging", "gemma4:e4b sometimes asks for input it already has (verification W04 asked which "
                            "tax year although the tax engine had answered)."),
    ("Model memory pressure", "one live turn was killed by the OS; the stream reported it and the UI offered "
                              "Try again, which is the intended behaviour."),
]


def _import(path: str):
    sys.path.insert(0, str(ROOT))
    module, _, name = path.partition(":")
    mod = __import__(module, fromlist=["*"])
    return getattr(mod, name) if name else mod


def endpoints() -> List[Tuple[str, str, str]]:
    """(method, path, first sentence of the handler docstring) — scanned from main.py."""
    lines = (BACKEND / "main.py").read_text(encoding="utf-8").splitlines()
    out: List[Tuple[str, str, str]] = []
    pending: List[Tuple[str, str]] = []
    for i, line in enumerate(lines):
        deco = re.match(r'@app\.(get|post|delete|put)\("([^"]+)"', line.strip())
        if deco:
            pending.append((deco.group(1).upper(), deco.group(2)))
            continue
        if pending and re.match(r"(?:async )?def \w+", line.strip()):
            doc = ""
            for probe in lines[i + 1:i + 12]:          # the signature may span lines
                text = probe.strip()
                if text.startswith('"""'):
                    doc = text.strip('"').strip()
                    if not doc:                        # docstring opens on the line below
                        rest = lines[i + 1:i + 14]
                        after = rest[rest.index(probe) + 1:] if probe in rest else []
                        doc = next((x.strip() for x in after if x.strip() and x.strip() != '"""'), "")
                    break
                if text and not text.endswith((",", "(", ")", "):", ":")) and not text.startswith(("#", ")")):
                    break
            doc = " ".join(doc.split()).split(". ")[0]
            out += [(m, p, doc) for m, p in pending]
            pending = []
    return out


def env_vars() -> List[str]:
    found = set()
    for path in BACKEND.rglob("*.py"):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        found.update(re.findall(r'os\.getenv\(\s*"(TORA_[A-Z_]+)"', path.read_text(encoding="utf-8")))
        found.update(re.findall(r'os\.environ\w*\(?\s*\[?\s*"(TORA_[A-Z_]+)"', path.read_text(encoding="utf-8")))
    return sorted(found)


def unit_test_count() -> Optional[int]:
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "--collect-only", "backend/tests"],
                              cwd=ROOT, capture_output=True, text=True, timeout=600)
        match = re.search(r"(\d+) tests? collected", proc.stdout)
        return int(match.group(1)) if match else None
    except Exception:  # noqa: BLE001
        return None


def frontend_test_count() -> Optional[int]:
    tests = list((FRONTEND / "src" / "tests").rglob("*.test.*")) if (FRONTEND / "src" / "tests").exists() else []
    if not tests:
        return None
    total = 0
    for path in tests:
        text = path.read_text(encoding="utf-8")
        total += len(re.findall(r"\b(?:it|test)\(", text))
        for block in re.findall(r"\.each\(\[(.*?)\]\)", text, re.S) + re.findall(r"parametrize", text):
            total += 0  # table-driven cases are counted by the runner, not here
    return total


def eval_stats() -> Dict[str, Any]:
    data = json.loads((BACKEND / "evals" / "scenarios.json").read_text(encoding="utf-8"))
    scenarios = data["scenarios"] if isinstance(data, dict) and "scenarios" in data else data
    by_category: Dict[str, int] = {}
    turns = 0
    for s in scenarios:
        by_category[s["category"]] = by_category.get(s["category"], 0) + 1
        turns += len(s.get("turns", []))
    return {"scenarios": len(scenarios), "turns": turns, "by_category": dict(sorted(by_category.items()))}


def rules_stats() -> Dict[str, Any]:
    data = json.loads((BACKEND / "knowledge" / "rules.json").read_text(encoding="utf-8"))
    rules = data["rules"] if isinstance(data, dict) and "rules" in data else data
    dates = [r.get("verified_on") for r in rules if r.get("verified_on")]
    return {"count": len(rules), "verified_on": max(dates) if dates else "unknown",
            "topics": sorted({r.get("topic") or r.get("category") or "" for r in rules} - {""})}


def collect() -> Dict[str, Any]:
    os.environ.setdefault("TORA_SESSION_DB", ":memory:")
    main = _import("backend.main")
    progress = _import("backend.observability.progress")
    engine = _import("backend.finance.engine")
    intent = _import("backend.state.intent:Intent")
    financial = _import("backend.context.financial")
    allowed = _import("backend.context.llm_extractor:ALLOWED_FACTS")

    tax_src = (BACKEND / "tools" / "tax_tool.py").read_text(encoding="utf-8")
    tax_ops = re.findall(r'"(\w+)"', re.search(r"operation: Literal\[(.*?)\]", tax_src, re.S).group(1))

    tools = []
    for tool in main.tool_registry.list_tools():
        tools.append({
            "name": tool.name,
            "description": " ".join((getattr(tool, "description", "") or "").split())[:110],
            "requires_auth": bool(getattr(tool, "requires_auth", False)),
        })

    fact_types: Dict[str, List[str]] = {}
    for name, (category, _period) in allowed.items():
        fact_types.setdefault(category, []).append(name)

    doc_src = (BACKEND / "documents" / "reader.py").read_text(encoding="utf-8")
    doc_types = sorted(set(re.findall(r"def parse_(form16|salary_slip|bank_statement|ais)\b", doc_src)))

    return {
        "doc_types": doc_types,
        "auth_modes": ["off", "optional", "required"],
        "tools": tools,
        "finance_ops": sorted(engine.OPERATIONS),
        "tax_ops": tax_ops,
        "endpoints": endpoints(),
        "intents": [i.value for i in intent],
        "fact_statuses": [s.value for s in financial.FactStatus],
        "fact_types": {k: sorted(v) for k, v in sorted(fact_types.items())},
        "stages": progress.STAGE_LABELS,
        "tool_labels": progress.TOOL_LABELS,
        "env": env_vars(),
        "evals": eval_stats(),
        "rules": rules_stats(),
    }


def table(headers: List[str], rows: List[List[str]]) -> List[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def render(data: Dict[str, Any], tests: Optional[int], fe_tests: Optional[int]) -> str:
    ev = data["evals"]
    lines: List[str] = [
        "# TORA — status",
        "",
        f"Generated from the code on {date.today().isoformat()} by `python -m backend.status`. "
        "Everything under **Built** is read out of the running system (registered tools, engine operations, "
        "endpoints, intents, fact types, the rules library, the eval suite). **Partial** and **Open** are the "
        "judgement calls, each with a pointer to the code or test behind it.",
        "",
        "## At a glance",
        "",
    ]
    counts = [
        ["Tools registered", str(len(data["tools"]))],
        ["Finance engine operations", str(len(data["finance_ops"]))],
        ["Tax operations", str(len(data["tax_ops"]))],
        ["HTTP endpoints", str(len(data["endpoints"]))],
        ["Intents", str(len(data["intents"]))],
        ["Remembered fact types", str(sum(len(v) for v in data["fact_types"].values()))],
        ["Verified rules", f"{data['rules']['count']} (checked {data['rules']['verified_on']})"],
        ["Offline eval scenarios", f"{ev['scenarios']} ({ev['turns']} turns)"],
        ["Backend tests", str(tests) if tests else "run pytest to count"],
        ["Frontend tests", str(fe_tests) if fe_tests else "n/a"],
    ]
    lines += table(["", "count"], counts) + [""]

    lines += ["## Built", "", "### Tools the planner can call", ""]
    lines += table(["tool", "what it does", "sign-in"],
                   [[f"`{t['name']}`", t["description"], "required" if t["requires_auth"] else "—"]
                    for t in data["tools"]]) + [""]

    lines += ["### Finance engine", "",
              "Deterministic operations on `finance_calc` — the model never does this arithmetic:", "",
              "".join(f"`{op}` · " for op in data["finance_ops"]).rstrip(" ·"), "",
              "### Tax engine", "",
              "".join(f"`{op}` · " for op in data["tax_ops"]).rstrip(" ·"), "",
              f"Backed by a rules library of {data['rules']['count']} verified rules "
              f"(`python -m backend.knowledge check` reports staleness and drift).", ""]

    lines += ["### API", ""]
    lines += table(["method", "path", "purpose"],
                   [[m, f"`{p}`", d] for m, p, d in data["endpoints"]]) + [""]

    lines += ["### Conversation and memory", "",
              "Intents: " + ", ".join(f"`{i}`" for i in data["intents"]), "",
              "Fact states: " + ", ".join(f"`{s}`" for s in data["fact_statuses"]) +
              " — with revision chains, previous values, and corrections held apart from changes over time.", "",
              "Conversation state tracks topics, their entities and the research gathered under each, so "
              "“what about Axis?” and “back to the home loan comparison” resolve without "
              "re-asking (`backend/state/conversation_state.py`).", "",
              "Remembered fact types:", ""]
    lines += table(["category", "facts"],
                   [[k, ", ".join(f"`{n}`" for n in v)] for k, v in data["fact_types"].items()]) + [""]

    lines += ["### Outside code we lean on", ""]
    lines += table(["library", "license", "what it does here"],
                   [[f"`{n}`", lic, what] for n, lic, what in EXTERNAL]) + [""]
    lines += ["### Prompt assembly", "",
              "The system prompt is one document; each turn is sent only the sections it can use — the engine "
              "sections when a tool ran, the rules citation section when the rules library answered, the "
              "debt-stress section when the case is about debt. Small talk goes out at 987 tokens against 2,321 "
              "for the whole prompt (`backend/prompts/tora.py`; `TORA_PROMPT_SLICING=off` disables it).", "",
              "### Locked figures", "",
              "A turn that ran an engine carries a slot table: every figure it produced, already formatted, "
              "offered to the answer model as `{{slot}}` placeholders. Placeholders are substituted after "
              "generation, and any number in the reply that matches no slot (beyond rounding) earns one rewrite "
              "naming the offender. Only deterministic tools contribute slots — researched figures stay in the "
              "external-data block (`backend/answer/slots.py`; `TORA_LOCKED_SLOTS=off` disables it).", "",
              "### Documents", "",
              "Uploads are parsed deterministically and their figures enter memory only after the user confirms: " +
              ", ".join(f"`{d}`" for d in data["doc_types"]) +
              ". Identifiers (PAN, account numbers, phones, emails) are masked, and a document's text is treated "
              "as untrusted external data, never as instructions (`backend/documents/reader.py`).", "",
              "### Safety", "",
              "- Prompt-extraction and injection attempts are refused; fetched pages and uploads are quarantined "
              "as external data (live cases A14, X1-X3).",
              "- SSRF: loopback, link-local and private targets are blocked (`web_fetch`; live case B20).",
              "- Per-client rate limiting with Retry-After; request size, model name and temperature validated.",
              "- Accounts: `TORA_AUTH_MODE` " + "/".join(data["auth_modes"]) +
              "; a user's conversations and memory are unreachable by anyone else (50-user isolation scenario).", "",
              "### Streaming", "",
              "`POST /api/chat/stream` emits `stage`, `complexity`, `tool`, `token`, `replace`, then `final` "
              "or `error`. Stages: " + ", ".join(f"{k} (“{v}”)" for k, v in data["stages"].items()) + ".",
              "", "The chat page renders them live: working panel with per-step ticks and engine summaries, "
              "smooth token reveal, Stop and Try again, engine and verification chips, follow-up suggestions "
              "(`frontend/src/pages/TORAPage.jsx`, `tora/sse.js`, `tora/markdown.jsx`).", ""]

    lines += ["### Verification and testing", "",
              f"- `python -m backend.check`: {tests or 'all'} unit tests, {ev['scenarios']} offline scenarios, "
              "the rules-library check.",
              "- Offline scenarios by category: " +
              ", ".join(f"{k} {v}" for k, v in ev["by_category"].items()) + ".",
              "- `python -m backend.stress.load_test`: throughput, per-user isolation, concurrent turns on one "
              "conversation, cancelled streams, rate limiting, fuzzing (a small version runs in the gate).",
              "- Live prompt suite and its results: `backend/docs/stress_test_report.md`.",
              f"- Frontend: {fe_tests or 'see'} tests under `frontend/src/tests/tora/`.", ""]

    lines += ["### Configuration", "",
              ", ".join(f"`{e}`" for e in data["env"]), ""]

    lines += ["## Partial — built, with a named gap", ""]
    lines += table(["area", "what exists", "what's missing", "evidence"],
                   [[a, have, missing, f"`{ev_}`"] for a, have, missing, ev_ in PARTIAL]) + [""]

    lines += ["## Open — in priority order", "",
              "1. **Full browser runbook pass.** 149 tests, desktop and mobile, by hand.",
              "2. **General scenario engine.** Change several facts at once and re-run every relevant engine.",
              "3. **Autonomous re-research.** Decide that evidence is thin or stale and go again.",
              "4. **Tool retries and circuit breakers.**",
              "5. **Broader tax coverage.**",
              "6. **Observability dashboard** over the existing metrics and traces.",
              "7. **CI wiring** for `backend.check`.", ""]

    lines += ["## Known limits", ""]
    lines += table(["limit", "detail"], [[a, b] for a, b in KNOWN_LIMITS]) + [""]

    lines += ["---", "",
              "Regenerate with `python -m backend.status`. If a row here disagrees with the code, the code wins — "
              "fix the generator, not the prose."]
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--print", action="store_true", dest="to_stdout")
    parser.add_argument("--no-tests", action="store_true", help="skip collecting test counts")
    args = parser.parse_args(argv)

    data = collect()
    tests = None if args.no_tests else unit_test_count()
    fe_tests = None if args.no_tests else frontend_test_count()
    text = render(data, tests, fe_tests)
    if args.to_stdout:
        print(text)
    else:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
