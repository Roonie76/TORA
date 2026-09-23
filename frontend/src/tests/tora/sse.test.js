import { describe, expect, it, vi } from "vitest";
import { ChatHttpError, createSSEParser, runBackgroundChat, streamChat } from "../../pages/tora/sse";

const collect = (chunks) => {
  const events = [];
  const parser = createSSEParser((e) => events.push(e));
  chunks.forEach((c) => parser.feed(c));
  parser.flush();
  return events;
};

// A fetch Response-like object whose body yields the given string chunks.
const streamResponse = (chunks, { status = 200, type = "text/event-stream" } = {}) => {
  const encoder = new TextEncoder();
  let i = 0;
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => type },
    body: {
      getReader: () => ({
        read: async () => (i < chunks.length ? { value: encoder.encode(chunks[i++]), done: false } : { done: true }),
        releaseLock: () => {},
      }),
    },
    json: async () => JSON.parse(chunks.join("")),
  };
};

describe("createSSEParser", () => {
  it("parses named JSON events", () => {
    const events = collect(['event: stage\ndata: {"stage":"planning"}\n\n']);
    expect(events).toEqual([{ event: "stage", data: { stage: "planning" } }]);
  });

  it("handles events split across arbitrary chunk boundaries", () => {
    const raw = 'event: token\ndata: {"text":"Hel"}\n\nevent: token\ndata: {"text":"lo ₹5"}\n\n';
    const whole = collect([raw]);
    for (let size = 1; size < 12; size += 1) {
      const chunks = [];
      for (let i = 0; i < raw.length; i += size) chunks.push(raw.slice(i, i + size));
      expect(collect(chunks)).toEqual(whole);
    }
    expect(whole.map((e) => e.data.text).join("")).toBe("Hello ₹5");
  });

  it("ignores keep-alive comments and supports CRLF", () => {
    const events = collect([": connected\r\n\r\n", ": keep-alive\r\n\r", '\nevent: final\r\ndata: {"response":"ok"}\r\n\r\n']);
    expect(events).toEqual([{ event: "final", data: { response: "ok" } }]);
  });

  it("joins multi-line data and keeps non-JSON as text", () => {
    const events = collect(["data: line one\ndata: line two\n\n"]);
    expect(events).toEqual([{ event: "message", data: { text: "line one\nline two" } }]);
  });

  it("dispatches a trailing event without a blank line on flush", () => {
    const events = collect(['event: final\ndata: {"a":1}']);
    expect(events).toEqual([{ event: "final", data: { a: 1 } }]);
  });
});

describe("streamChat", () => {
  it("streams events and resolves with the final payload", async () => {
    const fetchImpl = vi.fn(async () =>
      streamResponse([
        ": connected\n\n",
        'event: stage\ndata: {"type":"stage","stage":"writing"}\n\nevent: token\ndata: {"text":"Hi"}\n\n',
        'event: final\ndata: {"response":"Hi","turn":1}\n\n',
      ])
    );
    const seen = [];
    const final = await streamChat({ body: { message: "hi" }, fetchImpl, onEvent: (e) => seen.push(e.event) });
    expect(final).toEqual({ response: "Hi", turn: 1 });
    expect(seen).toEqual(["stage", "token", "final"]);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("/api/chat/stream");
    expect(JSON.parse(init.body)).toEqual({ message: "hi" });
    expect(init.headers["Content-Type"]).toBe("application/json");
  });

  it("throws the in-stream error with its status", async () => {
    const fetchImpl = async () =>
      streamResponse(['event: error\ndata: {"type":"error","status":404,"detail":"Conversation not found."}\n\n']);
    await expect(streamChat({ body: {}, fetchImpl })).rejects.toMatchObject({
      name: "ChatHttpError",
      status: 404,
      message: "Conversation not found.",
    });
  });

  it("reports a cut-off stream", async () => {
    const fetchImpl = async () => streamResponse(['event: token\ndata: {"text":"par"}\n\n']);
    await expect(streamChat({ body: {}, fetchImpl })).rejects.toBeInstanceOf(ChatHttpError);
  });

  it("reads FastAPI error details from HTTP errors", async () => {
    const fetchImpl = async () => ({
      ok: false,
      status: 422,
      json: async () => ({ detail: [{ msg: "Field required" }, { msg: "Too long" }] }),
    });
    await expect(streamChat({ body: {}, fetchImpl })).rejects.toMatchObject({ status: 422, message: "Field required; Too long" });
  });

  it("accepts a plain JSON reply from a server without streaming", async () => {
    const fetchImpl = async () => streamResponse(['{"response":"plain"}'], { type: "application/json" });
    const events = [];
    const final = await streamChat({ body: {}, fetchImpl, onEvent: (e) => events.push(e) });
    expect(final.response).toBe("plain");
    expect(events[0].event).toBe("final");
  });
});

// ---------------------------------------------------------------------------
// runBackgroundChat: the turn outlives the connection that is watching it.
// ---------------------------------------------------------------------------

const jsonResponse = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: () => "application/json" },
  json: async () => body,
});

// A fetch stand-in that routes by URL+method and records every call.
const router = ({ accept, events = [], polls = [] }) => {
  const calls = [];
  const eventQueue = [...events];
  const pollQueue = [...polls];
  const impl = async (url, opts = {}) => {
    const method = opts.method || "GET";
    calls.push({ url, method });
    if (url === "/api/chat/async") return jsonResponse(accept);
    if (url.endsWith("/events")) {
      const next = eventQueue.shift();
      return next === undefined ? streamResponse([]) : streamResponse(next);
    }
    if (method === "DELETE") return jsonResponse({ stopped: true });
    const next = pollQueue.shift();
    return jsonResponse(next ?? { status: "running" });
  };
  impl.calls = calls;
  return impl;
};

const ACCEPTED = {
  turn_id: "t1",
  status: "running",
  poll: "/api/chat/turns/t1",
  events: "/api/chat/turns/t1/events",
};

describe("runBackgroundChat", () => {
  it("accepts the turn, follows it, and resolves with the final answer", async () => {
    const fetchImpl = router({
      accept: ACCEPTED,
      events: [[
        'event: snapshot\ndata: {"status":"running","text":""}\n\n',
        'event: token\ndata: {"text":"Rs 43,391"}\n\n',
        'event: final\ndata: {"response":"Rs 43,391 a month.","model":"gemma4:e4b"}\n\n',
      ]],
    });
    const seen = [];
    const final = await runBackgroundChat({
      body: { message: "EMI?" },
      fetchImpl,
      onEvent: (e) => seen.push(e.event),
      sleep: async () => {},
    });
    expect(final.response).toBe("Rs 43,391 a month.");
    expect(seen[0]).toBe("snapshot");
  });

  it("re-attaches when the stream drops and still returns the answer", async () => {
    // The first attach dies mid-answer; the second replays a snapshot and finishes.
    const fetchImpl = router({
      accept: ACCEPTED,
      events: [
        ['event: token\ndata: {"text":"partial"}\n\n'],
        [
          'event: snapshot\ndata: {"status":"running","text":"partial"}\n\n',
          'event: final\ndata: {"response":"the whole answer","model":"m"}\n\n',
        ],
      ],
      polls: [{ status: "running" }],
    });
    const final = await runBackgroundChat({
      body: { message: "advice?" },
      fetchImpl,
      sleep: async () => {},
    });
    expect(final.response).toBe("the whole answer");
    expect(fetchImpl.calls.filter((c) => c.url.endsWith("/events")).length).toBe(2);
  });

  it("takes the answer from a poll when the turn finished while disconnected", async () => {
    const fetchImpl = router({
      accept: ACCEPTED,
      events: [[]], // stream ends with no verdict at all
      polls: [{ status: "done", result: { response: "finished without me", model: "m" } }],
    });
    const final = await runBackgroundChat({ body: {}, fetchImpl, sleep: async () => {} });
    expect(final.response).toBe("finished without me");
  });

  it("cancels the turn on the server when aborted mid-answer, rather than just looking away", async () => {
    // Stop has to stop the work. If it only closed the stream, the model would
    // keep decoding for minutes on a box with two cores.
    let releaseReader;
    const gate = new Promise((resolve) => {
      releaseReader = resolve;
    });
    const calls = [];
    const fetchImpl = async (url, opts = {}) => {
      const method = opts.method || "GET";
      calls.push({ url, method });
      if (url === "/api/chat/async") return jsonResponse(ACCEPTED);
      if (method === "DELETE") return jsonResponse({ stopped: true });
      if (url.endsWith("/events")) {
        return {
          ok: true,
          status: 200,
          headers: { get: () => "text/event-stream" },
          body: {
            getReader: () => ({
              // Never yields: the turn is still being written when we abort.
              read: async () => {
                await gate;
                return { done: true };
              },
              releaseLock: () => {},
            }),
          },
        };
      }
      return jsonResponse({ status: "cancelled" });
    };

    const controller = new AbortController();
    const promise = runBackgroundChat({
      body: {},
      fetchImpl,
      signal: controller.signal,
      sleep: async () => {},
    });
    // Let the start POST and the attach happen, then stop mid-answer.
    await Promise.resolve();
    await Promise.resolve();
    controller.abort();
    releaseReader();
    await expect(promise).rejects.toMatchObject({ name: "AbortError" });
    expect(calls.some((c) => c.method === "DELETE")).toBe(true);
  });

  it("surfaces an in-stream error as a ChatHttpError", async () => {
    const fetchImpl = router({
      accept: ACCEPTED,
      events: [['event: error\ndata: {"status":503,"detail":"TORA is warming up."}\n\n']],
    });
    await expect(
      runBackgroundChat({ body: {}, fetchImpl, sleep: async () => {} }),
    ).rejects.toMatchObject({ name: "ChatHttpError", status: 503, message: "TORA is warming up." });
  });

  it("gives up after enough failed re-attaches, and says the turn may still be running", async () => {
    const fetchImpl = router({ accept: ACCEPTED, events: [], polls: [] });
    await expect(
      runBackgroundChat({ body: {}, fetchImpl, maxReattaches: 1, sleep: async () => {} }),
    ).rejects.toThrow(/may still be running/);
  });

  it("reports a rejected start without pretending a turn exists", async () => {
    const fetchImpl = async () => jsonResponse({ detail: "Too many requests." }, 429);
    await expect(runBackgroundChat({ body: {}, fetchImpl })).rejects.toMatchObject({ status: 429 });
  });
});
