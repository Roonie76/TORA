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
