// Streaming client for TORA's /api/chat/stream (Server-Sent Events over a POST).
// EventSource can't POST or send headers, so we read the fetch body ourselves.

export class ChatHttpError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ChatHttpError";
    this.status = status;
  }
}

// Incremental SSE parser: feed() it text chunks as they arrive; it calls
// onEvent({ event, data }) for each complete event. `data` is parsed JSON when
// possible. Comment lines (": keep-alive") are ignored.
export function createSSEParser(onEvent) {
  let buffer = "";
  let name = "message";
  let dataLines = [];

  const dispatch = () => {
    if (dataLines.length) {
      const raw = dataLines.join("\n");
      let data;
      try {
        data = JSON.parse(raw);
      } catch {
        data = { text: raw };
      }
      onEvent({ event: name, data });
    }
    name = "message";
    dataLines = [];
  };

  const handleLine = (line) => {
    if (line === "") {
      dispatch();
      return;
    }
    if (line.startsWith(":")) return;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") name = value || "message";
    else if (field === "data") dataLines.push(value);
  };

  return {
    feed(chunk) {
      buffer += chunk;
      for (;;) {
        const match = /\r\n|\r|\n/.exec(buffer);
        if (!match) break;
        // A lone trailing "\r" may be the first half of "\r\n": wait for more.
        if (match[0] === "\r" && match.index === buffer.length - 1) break;
        handleLine(buffer.slice(0, match.index));
        buffer = buffer.slice(match.index + match[0].length);
      }
    },
    flush() {
      if (buffer) {
        handleLine(buffer.replace(/\r$/, ""));
        buffer = "";
      }
      dispatch();
    },
  };
}

// Turn a failed HTTP response into a readable message (FastAPI detail shapes).
export async function readErrorDetail(response) {
  let detail = `Server error (${response.status})`;
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") {
      detail = body.detail;
    } else if (Array.isArray(body?.detail) && body.detail.length > 0) {
      detail = body.detail.map((d) => d?.msg || String(d)).join("; ");
    }
  } catch {
    // not JSON
  }
  return detail;
}

// Run a turn in the background and follow it.
//
// streamChat ties the turn to one connection: if it drops, the server stops the
// turn and the work is gone. For a question that takes minutes on CPU that is a
// real loss -- a locked phone is enough to cause it. Here the turn is accepted
// first, then followed, so a dropped connection costs only the connection: we
// re-attach, the snapshot replays what was missed, and the answer is still there.
//
// Resolves with the same `final` payload as streamChat. Aborting cancels the turn
// on the server too, so Stop stops the work rather than just looking away.
export async function runBackgroundChat({
  startUrl = "/api/chat/async",
  body,
  headers = {},
  signal,
  onEvent,
  fetchImpl,
  maxReattaches = 4,
  reattachDelayMs = 800,
  sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
}) {
  const doFetch = fetchImpl || fetch;
  const jsonHeaders = { ...headers, "Content-Type": "application/json" };

  const accepted = await doFetch(startUrl, {
    method: "POST",
    headers: { ...jsonHeaders, Accept: "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!accepted.ok) {
    throw new ChatHttpError(await readErrorDetail(accepted), accepted.status);
  }
  const handle = await accepted.json();
  const eventsUrl = handle.events || `/api/chat/turns/${handle.turn_id}/events`;
  const pollUrl = handle.poll || `/api/chat/turns/${handle.turn_id}`;

  // Stop must stop the work, not merely stop watching it.
  const cancel = () => {
    try {
      doFetch(pollUrl, { method: "DELETE", headers })?.catch?.(() => {});
    } catch {
      // best effort: the turn also ends on its own
    }
  };
  signal?.addEventListener?.("abort", cancel, { once: true });

  const terminal = (data) => {
    if (data?.status === "done" && data?.result) return { final: data.result };
    if (data?.status === "error") return { failure: data.error || { status: 500 } };
    if (data?.status === "cancelled") return { cancelled: true };
    return null;
  };

  // Follow the event stream once. Returns a terminal outcome, or null if the
  // stream ended without one (a drop) so the caller can re-attach.
  const follow = async () => {
    const response = await doFetch(eventsUrl, {
      method: "GET",
      headers: { ...headers, Accept: "text/event-stream" },
      signal,
    });
    if (!response.ok) {
      throw new ChatHttpError(await readErrorDetail(response), response.status);
    }
    if (!response.body) return null;

    let outcome = null;
    const parser = createSSEParser((evt) => {
      if (evt.event === "final") outcome = { final: evt.data };
      else if (evt.event === "error") outcome = { failure: evt.data };
      else if (evt.event === "cancelled") outcome = { cancelled: true };
      onEvent?.(evt);
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        parser.feed(decoder.decode(value, { stream: true }));
      }
      parser.feed(decoder.decode());
      parser.flush();
    } finally {
      try {
        reader.releaseLock();
      } catch {
        // already released
      }
    }
    return outcome;
  };

  const settle = (outcome) => {
    if (outcome.final) return outcome.final;
    if (outcome.cancelled) {
      const err = new Error("The turn was stopped.");
      err.name = "AbortError";
      throw err;
    }
    const detail =
      typeof outcome.failure?.detail === "string"
        ? outcome.failure.detail
        : "Something went wrong. Please try again.";
    throw new ChatHttpError(detail, outcome.failure?.status || 500);
  };

  for (let attempt = 0; ; attempt++) {
    const outcome = await follow();
    if (outcome) return settle(outcome);

    // The stream ended without a verdict. The turn itself is unaffected, so ask
    // the server where it got to before deciding to re-attach.
    const polled = await doFetch(pollUrl, { method: "GET", headers, signal });
    if (polled.ok) {
      const state = terminal(await polled.json());
      if (state) return settle(state);
    }
    if (attempt >= maxReattaches) {
      throw new ChatHttpError(
        "Lost contact with the answer. It may still be running — reopen this conversation to check.",
        0,
      );
    }
    await sleep(reattachDelayMs);
  }
}

// POST a chat turn and stream its events. Resolves with the `final` payload
// (the same shape /api/chat returns). Throws ChatHttpError for HTTP errors and
// in-stream `error` events, and AbortError when `signal` is aborted.
export async function streamChat({ url = "/api/chat/stream", body, headers = {}, signal, onEvent, fetchImpl }) {
  const doFetch = fetchImpl || fetch;
  const response = await doFetch(url, {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    throw new ChatHttpError(await readErrorDetail(response), response.status);
  }

  const type = (response.headers?.get?.("content-type") || "").toLowerCase();
  if (!response.body || !type.includes("text/event-stream")) {
    // A server without streaming answered with plain JSON: treat it as the final event.
    const data = await response.json();
    onEvent?.({ event: "final", data });
    return data;
  }

  let final = null;
  let failure = null;
  const parser = createSSEParser((evt) => {
    if (evt.event === "final") final = evt.data;
    if (evt.event === "error") failure = evt.data;
    onEvent?.(evt);
  });

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    parser.feed(decoder.decode());
    parser.flush();
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // already released
    }
  }

  if (failure) {
    const detail = typeof failure.detail === "string" ? failure.detail : "Something went wrong. Please try again.";
    throw new ChatHttpError(detail, failure.status || 500);
  }
  if (!final) {
    throw new ChatHttpError("The reply was cut off before it finished. Please try again.", 0);
  }
  return final;
}
