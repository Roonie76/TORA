import { describe, expect, it, vi } from "vitest";
import { ChatHttpError, createSSEParser, streamChat } from "../../pages/tora/sse";

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
