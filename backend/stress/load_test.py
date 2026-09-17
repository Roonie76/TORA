"""
Load and robustness test for the TORA API (no real model needed).

Starts the real FastAPI app under uvicorn on a spare port with a fake LLM that
streams tokens with realistic jitter, then drives it over real HTTP:

  1. throughput     - many concurrent conversations, mixed /api/chat and /api/chat/stream
  2. isolation      - signed-in users never see each other's memory or conversations
  3. same_convo     - concurrent turns on one conversation are serialised, none lost
  4. cancel         - streams closed mid-reply leave no running tasks and save nothing
  5. rate_limit     - the per-client limit returns 429 + Retry-After, stream reports it in-band
  6. fuzz           - malformed and hostile payloads never cause a 5xx
  7. resources      - LLM concurrency and memory after the run

Run:  python -m backend.stress.load_test [--users 200] [--json out.json]
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import inspect
import json
import os
import random
import re
import resource
import socket
import string
import sys
import time
from typing import Any, Dict, List

# Standalone runs own the process, so they may set the environment the app reads
# at import time. Under pytest the suite owns it, so run() sets only what it needs
# and restores it afterwards (see ENV_FOR_RUN).
if __name__ == "__main__":
    os.environ.setdefault("TORA_SESSION_DB", ":memory:")
    os.environ["TORA_RATE_LIMIT_PER_MINUTE"] = "0"

ENV_FOR_RUN = {
    "TORA_AUTH_MODE": "optional",     # the isolation scenario signs users in
    "TORA_FAST_PATH": "on",           # the fake planner asks for no tools; the fast path runs the real ones
    "TORA_TRAINING_LOG": "off",
    "TORA_LLM_EXTRACTION": "off",     # the fake model returns no facts anyway
}

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import backend.main as main_module  # noqa: E402
from backend.auth import StaticAuthVerifier  # noqa: E402
from backend.llm.base import LLMProvider, LLMResponse  # noqa: E402

PLANNER_PREFIX = "You are TORA's Tool Planner"
RUPEE = "₹"


class StreamingFakeLLM(LLMProvider):
    """Plans nothing (the fast path still runs real tools); answers echo the income it was given."""

    def __init__(self, min_ms: int = 20, max_ms: int = 120):
        self.min_ms, self.max_ms = min_ms, max_ms
        self.calls = 0
        self.active = 0
        self.peak = 0

    @property
    def default_model(self):
        return "fake"

    def resolve_model(self, requested=None, available=None):
        return "fake"

    async def generate(self, messages, model=None, options=None):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(random.uniform(self.min_ms, self.max_ms) / 1000)
            system = messages[0]["content"]
            if system.startswith(PLANNER_PREFIX):
                return LLMResponse(content='{"thought":"none","requires_tools":false,"steps":[]}', model="fake")
            if (options or {}).get("format"):
                return LLMResponse(content='{"facts":[]}', model="fake")
            income = re.search(r"Monthly Income: (" + RUPEE + r"[\d,]+)", system)
            text = f"Noted. Your income on record is {income.group(1)}." if income else "I don't know your income yet."
            on_token = (options or {}).get("on_token")
            if callable(on_token):
                for word in re.findall(r"\S+\s*", text):
                    await asyncio.sleep(random.uniform(1, 6) / 1000)
                    result = on_token(word)
                    if inspect.isawaitable(result):
                        await result
            return LLMResponse(content=text, model="fake")
        finally:
            self.active -= 1

    async def list_models(self):
        return ["fake"]

    async def health_check(self):
        return {"connected": True}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pct(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = min(len(values) - 1, max(0, round(p / 100 * (len(values) - 1))))
    return round(values[k], 1)


def inr(n: int) -> str:
    s = str(n)
    head, tail = s[:-3], s[-3:]
    head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
    return f"{RUPEE}{head},{tail}" if head else f"{RUPEE}{tail}"


async def read_stream(client, body, headers=None, stop_after_tokens=None):
    """POST to /api/chat/stream; return (events, final, error, ttft_ms). Optionally hang up early."""
    events, final, error, ttft = [], None, None, None
    t0 = time.monotonic()
    async with client.stream("POST", "/api/chat/stream", json=body, headers=headers or {}) as r:
        if r.status_code != 200:
            return events, None, {"status": r.status_code, "detail": (await r.aread()).decode()[:200]}, None
        name, tokens = None, 0
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                events.append(name)
                if name == "token":
                    tokens += 1
                    if ttft is None:
                        ttft = (time.monotonic() - t0) * 1000
                    if stop_after_tokens and tokens >= stop_after_tokens:
                        return events, None, {"status": "client_closed"}, ttft
                elif name == "final":
                    final = data
                elif name == "error":
                    error = data
    return events, final, error, ttft


async def scenario_throughput(client, users: int) -> Dict[str, Any]:
    latencies: List[float] = []
    ttfts: List[float] = []
    errors: List[str] = []
    wrong: List[str] = []

    async def one_user(i: int):
        cid = None
        salary = 30000 + i * 7
        msgs = [f"my salary is {salary}", "EMI for 5 lakh at 10% for 3 years", "what is my salary?"]
        for n, msg in enumerate(msgs):
            body = {"message": msg, **({"conversation_id": cid} if cid else {})}
            t0 = time.monotonic()
            try:
                if (i + n) % 2:
                    r = await client.post("/api/chat", json=body)
                    if r.status_code != 200:
                        errors.append(f"http {r.status_code}: {r.text[:120]}")
                        return
                    final = r.json()
                else:
                    _, final, error, ttft = await read_stream(client, body)
                    if ttft is not None:
                        ttfts.append(ttft)
                    if final is None:
                        errors.append(f"stream failed: {error}")
                        return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
                return
            latencies.append((time.monotonic() - t0) * 1000)
            cid = final["conversation_id"]
            if n == 1 and not any(t.get("operation") == "emi" and t.get("ok") for t in final.get("tools") or []):
                wrong.append(f"user {i}: EMI turn did not run the EMI engine ({final.get('tools')})")
            if n == 2 and inr(salary) not in final["response"]:
                wrong.append(f"user {i}: expected {inr(salary)}, got {final['response'][:80]!r}")

    t0 = time.monotonic()
    await asyncio.gather(*(one_user(i) for i in range(users)))
    elapsed = time.monotonic() - t0
    return {
        "users": users,
        "turns": len(latencies),
        "seconds": round(elapsed, 2),
        "turns_per_sec": round(len(latencies) / elapsed, 1),
        "p50_ms": pct(latencies, 50),
        "p95_ms": pct(latencies, 95),
        "p99_ms": pct(latencies, 99),
        "stream_ttft_p50_ms": pct(ttfts, 50),
        "stream_ttft_p95_ms": pct(ttfts, 95),
        "errors": errors[:5],
        "error_count": len(errors),
        "wrong_answers": wrong[:5],
        "wrong_count": len(wrong),
        "ok": not errors and not wrong and len(latencies) == users * 3,
    }


async def scenario_isolation(client, users: int) -> Dict[str, Any]:
    tokens = {f"tok-{i}": f"user{i}" for i in range(users)}
    main_module.auth_verifier = StaticAuthVerifier(tokens)
    problems: List[str] = []
    convs: Dict[int, str] = {}

    async def setup(i):
        h = {"Authorization": f"Bearer tok-{i}"}
        r = await client.post("/api/chat", json={"message": f"my salary is {40000 + i}"}, headers=h)
        if r.status_code != 200:
            problems.append(f"user{i} setup {r.status_code}")
            return
        convs[i] = r.json()["conversation_id"]

    await asyncio.gather(*(setup(i) for i in range(users)))

    async def verify(i):
        h = {"Authorization": f"Bearer tok-{i}"}
        other = (i + 1) % users
        mem = await client.get("/api/me/memory", headers=h)
        blob = mem.text
        if str(40000 + i) not in blob.replace(",", "") :
            problems.append(f"user{i} lost own salary ({mem.status_code})")
        if str(40000 + other) in blob.replace(",", ""):
            problems.append(f"user{i} sees user{other}'s salary")
        # a brand-new conversation for the same user uses account memory
        r = await client.post("/api/chat", json={"message": "what is my salary?"}, headers=h)
        if r.status_code != 200 or inr(40000 + i) not in r.json()["response"]:
            problems.append(f"user{i} new conversation lacks account memory: {r.status_code} {r.text[:80]}")
        new_cid = r.json().get("conversation_id") if r.status_code == 200 else None
        if other in convs:
            r = await client.get(f"/api/conversations/{convs[other]}", headers=h)
            if r.status_code != 404:
                problems.append(f"user{i} read user{other}'s conversation: {r.status_code}")
            r = await client.post("/api/chat", json={"message": "hi", "conversation_id": convs[other]}, headers=h)
            if r.status_code != 404:
                problems.append(f"user{i} wrote to user{other}'s conversation: {r.status_code}")
            r = await client.delete(f"/api/conversations/{convs[other]}", headers=h)
            if r.status_code != 404:
                problems.append(f"user{i} deleted user{other}'s conversation: {r.status_code}")
        if i in convs:
            r = await client.get(f"/api/conversations/{convs[i]}")
            if r.status_code != 404:
                problems.append(f"anonymous read user{i}'s conversation: {r.status_code}")
        listed = await client.get("/api/conversations", headers=h)
        mine = {convs.get(i), new_cid}
        ids = set(re.findall(r'"(?:conversation_id|id)":\s*"([^"]+)"', listed.text))
        foreign = {v for k, v in convs.items() if k != i}
        if ids & foreign:
            problems.append(f"user{i} lists foreign conversations")
        if convs.get(i) and convs[i] not in ids:
            problems.append(f"user{i} does not see own conversation in list ({listed.status_code})")
        _ = mine

    await asyncio.gather(*(verify(i) for i in range(users)))
    r = await client.get("/api/me/memory", headers={"Authorization": "Bearer forged"})
    if r.status_code != 401:
        problems.append(f"forged token got {r.status_code}")
    return {"users": users, "problems": problems[:8], "problem_count": len(problems), "ok": not problems}


async def scenario_same_conversation(client, parallel: int) -> Dict[str, Any]:
    r = await client.post("/api/chat", json={"message": "hello"})
    cid = r.json()["conversation_id"]

    async def turn(k):
        body = {"message": f"what is {k} + {k}?", "conversation_id": cid}
        if k % 2:
            r = await client.post("/api/chat", json=body)
            return r.status_code, (r.json().get("turn") if r.status_code == 200 else None)
        _, final, error, _ = await read_stream(client, body)
        return (200 if final else (error or {}).get("status")), (final or {}).get("turn")

    results = await asyncio.gather(*(turn(k) for k in range(parallel)))
    convo = (await client.get(f"/api/conversations/{cid}")).json()
    turns = sorted(t for _, t in results if t)
    stored = len(convo.get("turns") or [])
    ok = (all(s == 200 for s, _ in results) and turns == list(range(2, parallel + 2))
          and stored == 2 * (parallel + 1))
    return {"parallel_turns": parallel, "statuses": sorted({str(s) for s, _ in results}),
            "turn_numbers": f"{turns[:3]}...{turns[-2:]}" if turns else [],
            "stored_messages": stored, "expected_messages": 2 * (parallel + 1), "ok": ok}


async def scenario_cancel(client, streams: int) -> Dict[str, Any]:
    cids = []
    for _ in range(streams):
        r = await client.post("/api/chat", json={"message": "hello"})
        cids.append(r.json()["conversation_id"])
    await asyncio.sleep(0.2)
    before = len(asyncio.all_tasks())

    async def cancel_one(cid):
        await read_stream(client, {"message": "tell me about budgeting", "conversation_id": cid}, stop_after_tokens=1)

    await asyncio.gather(*(cancel_one(c) for c in cids))
    await asyncio.sleep(1.0)
    leftover = len(asyncio.all_tasks()) - before
    saved = 0
    followups_ok = 0
    for cid in cids:
        convo = (await client.get(f"/api/conversations/{cid}")).json()
        if len(convo.get("turns") or []) != 2:  # the cancelled turn must not have been stored
            saved += 1
        r = await client.post("/api/chat", json={"message": "thanks", "conversation_id": cid})
        followups_ok += r.status_code == 200
    return {"cancelled_streams": streams, "leftover_tasks": leftover, "cancelled_turns_saved": saved,
            "followups_ok": followups_ok, "ok": leftover <= 2 and saved == 0 and followups_ok == streams}


async def scenario_rate_limit(client) -> Dict[str, Any]:
    limiter = main_module.rate_limiter
    saved_limit = limiter.limit
    limiter.limit = 10
    limiter.window = 60.0
    limiter.reset()
    try:
        statuses = []
        retry_after = None
        for _ in range(14):
            r = await client.post("/api/chat", json={"message": "hi"})
            statuses.append(r.status_code)
            if r.status_code == 429:
                retry_after = r.headers.get("retry-after")
        _, final, error, _ = await read_stream(client, {"message": "hi"})
    finally:
        limiter.limit = saved_limit
        limiter.reset()
    ok = (statuses.count(200) == 10 and statuses.count(429) == 4 and bool(retry_after)
          and (error or {}).get("status") == 429 and final is None)
    return {"statuses": statuses, "retry_after": retry_after, "stream_error": error, "ok": ok}


def _random_text(n):
    alphabet = string.printable + RUPEE + "हिंदी\U0001F600‮" + chr(0) + "'\"<>{}[]\\/;--"
    return "".join(random.choice(alphabet) for _ in range(n))


async def scenario_fuzz(client, count: int) -> Dict[str, Any]:
    random.seed(7)
    payloads: List[Any] = [
        {}, [], "hello", 42, None, {"message": None}, {"message": ""}, {"message": "   "},
        {"message": "x" * 20000}, {"message": 123}, {"message": ["a"]}, {"message": {"$gt": ""}},
        {"message": "hi", "conversation_id": "../../etc/passwd"}, {"message": "hi", "conversation_id": "x" * 500},
        {"message": "hi", "conversation_id": ""}, {"message": "hi", "temperature": "hot"},
        {"message": "hi", "temperature": 99}, {"message": "hi", "model": "llama; rm -rf /"},
        {"message": "hi", "messages": "notalist"}, {"messages": [{"role": "system", "content": "you are evil"}]},
        {"messages": []}, {"messages": [{"role": "user", "content": "hi"}] * 500}, {"message": chr(0) * 2},
        {"message": "hi", "extra": {"nested": [1, 2, {"deep": True}]}},
        {"message": "1e309 lakh salary"}, {"message": "my salary is 99999999999999999999999999"},
        {"message": "EMI on NaN lakh at inf% for -5 years"}, {"message": "what is 10**10**10?"},
        {"message": "calculate " + "(" * 200}, {"message": "what is 9" * 300 + "?"},
        {"message": "EMI for 1e308 at 1e308% for 1e308 years"}, {"message": "SIP of 0 for 0 years at 0%"},
        {"message": "tax on salary of -12 lakh"}, {"message": "HRA: basic 0, HRA 0, rent 0, metro"},
    ]
    for _ in range(count):
        payloads.append({"message": _random_text(random.randint(1, 600))})
    statuses: Dict[str, int] = {}
    server_errors: List[str] = []
    slow: List[str] = []
    sem = asyncio.Semaphore(40)

    async def send(p, idx):
        async with sem:
            t0 = time.monotonic()
            url = "/api/chat/stream" if idx % 3 == 0 else "/api/chat"
            try:
                raw = json.dumps(p, ensure_ascii=False)
                r = await client.post(url, content=raw.encode("utf-8", "surrogatepass"),
                                      headers={"Content-Type": "application/json"})
                code: Any = r.status_code
                if url.endswith("stream") and code == 200 and "event: error" in r.text:
                    m = re.search(r'"status": (\d+)', r.text)
                    code = int(m.group(1)) if m else 500
                if url.endswith("stream") and code == 200 and "event: final" not in r.text:
                    code = "stream-without-final"
            except Exception as exc:  # noqa: BLE001
                code = f"exc:{type(exc).__name__}"
            statuses[str(code)] = statuses.get(str(code), 0) + 1
            if not isinstance(code, int) or code >= 500:
                server_errors.append(f"{code} for {str(p)[:80]!r}")
            if time.monotonic() - t0 > 5:
                slow.append(str(p)[:60])

    await asyncio.gather(*(send(p, i) for i, p in enumerate(payloads)))
    for body in (b"{", b"\xff\xfe", b'{"message": "hi"', b"null", b"[1,2"):
        r = await client.post("/api/chat", content=body, headers={"Content-Type": "application/json"})
        statuses[str(r.status_code)] = statuses.get(str(r.status_code), 0) + 1
        if r.status_code >= 500:
            server_errors.append(f"{r.status_code} for raw {body!r}")
    for method, url in (("GET", "/api/conversations/%00"), ("DELETE", "/api/conversations/nope"),
                        ("GET", "/api/me/memory"), ("POST", "/api/feedback"), ("POST", "/api/documents"),
                        ("POST", "/api/documents/x/confirm"), ("GET", "/api/conversations/" + "a" * 5000)):
        r = await client.request(method, url, json={} if method == "POST" else None)
        if r.status_code >= 500:
            server_errors.append(f"{r.status_code} for {method} {url[:40]}")
    return {"requests": len(payloads) + 12, "statuses": statuses, "server_errors": server_errors[:8],
            "server_error_count": len(server_errors), "slow": slow[:5], "ok": not server_errors}


async def run(args) -> Dict[str, Any]:
    fake = StreamingFakeLLM()
    # The limiter is per client IP; every scenario here shares one. Only the
    # rate-limit scenario wants it enforced, so disable it around the others.
    env_saved = {k: os.environ.get(k) for k in ENV_FOR_RUN}
    os.environ.update(ENV_FOR_RUN)
    limiter_saved = main_module.rate_limiter.limit
    main_module.rate_limiter.limit = 0
    main_module.rate_limiter.reset()
    main_module.agent.llm_provider = fake
    main_module.planner.llm_provider = fake
    port = free_port()
    config = uvicorn.Config(main_module.app, host="127.0.0.1", port=port, log_level="warning",
                            lifespan="on", backlog=4096)
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    limits = httpx.Limits(max_connections=args.users + 50, max_keepalive_connections=args.users)
    report: Dict[str, Any] = {}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=120, limits=limits) as client:
        scenarios = (
            ("throughput", lambda: scenario_throughput(client, args.users)),
            ("isolation", lambda: scenario_isolation(client, args.isolation_users)),
            ("same_conversation", lambda: scenario_same_conversation(client, args.parallel_turns)),
            ("cancel", lambda: scenario_cancel(client, args.cancel)),
            ("rate_limit", lambda: scenario_rate_limit(client)),
            ("fuzz", lambda: scenario_fuzz(client, args.fuzz)),
        )
        for name, make in scenarios:
            t0 = time.monotonic()
            try:
                result = await make()
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "crash": f"{type(exc).__name__}: {exc}"}
            result.setdefault("seconds", round(time.monotonic() - t0, 2))
            report[name] = result
            detail = json.dumps({k: v for k, v in result.items() if k != "ok"}, ensure_ascii=False)
            print(f"{name:18s} {'PASS' if result['ok'] else 'FAIL'}  {detail[:500]}", flush=True)
    gc.collect()
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report["resources"] = {
        "llm_calls": fake.calls,
        "peak_concurrent_llm_calls": fake.peak,
        "max_rss_mb": round(rss_after / 1024, 1),
        "rss_growth_mb": round((rss_after - rss_before) / 1024, 1),
        "ok": True,
    }
    server.should_exit = True
    await server_task
    main_module.rate_limiter.limit = limiter_saved
    main_module.rate_limiter.reset()
    for key, value in env_saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    report["ok"] = all(v.get("ok", False) for v in report.values() if isinstance(v, dict))
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--users", type=int, default=200)
    parser.add_argument("--isolation-users", type=int, default=50)
    parser.add_argument("--parallel-turns", type=int, default=20)
    parser.add_argument("--cancel", type=int, default=30)
    parser.add_argument("--fuzz", type=int, default=300)
    parser.add_argument("--json", help="write the report here")
    args = parser.parse_args(argv)
    report = asyncio.run(run(args))
    print(json.dumps(report["resources"]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, ensure_ascii=False)
    print("LOAD TEST", "PASSED" if report["ok"] else "FAILED")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
