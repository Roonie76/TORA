import React from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../../api", () => ({ getStoredAccessToken: () => "test-token" }));

import TORAPage from "../../pages/TORAPage";

const encoder = new TextEncoder();
const sse = (type, data) => `event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`;

// A streaming response the test controls chunk by chunk; aborting rejects the pending read.
function controllableStream(signal) {
  const queue = [];
  let waiter = null;
  let closed = false;
  const reader = {
    read() {
      if (signal?.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
      if (queue.length) return Promise.resolve({ value: encoder.encode(queue.shift()), done: false });
      if (closed) return Promise.resolve({ done: true });
      return new Promise((resolve, reject) => {
        waiter = { resolve, reject };
      });
    },
    releaseLock() {},
  };
  signal?.addEventListener("abort", () => {
    if (waiter) {
      waiter.reject(new DOMException("Aborted", "AbortError"));
      waiter = null;
    }
  });
  return {
    response: { ok: true, status: 200, headers: { get: () => "text/event-stream" }, body: { getReader: () => reader } },
    push(chunk) {
      if (waiter) {
        const w = waiter;
        waiter = null;
        w.resolve({ value: encoder.encode(chunk), done: false });
      } else {
        queue.push(chunk);
      }
    },
    close() {
      closed = true;
      if (waiter) {
        waiter.resolve({ done: true });
        waiter = null;
      }
    },
  };
}

let streams;
let fetchMock;

const flush = async () => {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 30));
  });
};

const bodies = () => fetchMock.mock.calls.filter(([u]) => u === "/api/chat/stream").map(([, init]) => JSON.parse(init.body));

beforeAll(() => {
  window.matchMedia = (query) => ({
    matches: query.includes("reduce"),
    media: query,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
  });
  Element.prototype.scrollTo = function scrollTo() {};
});

beforeEach(() => {
  localStorage.clear();
  streams = [];
  fetchMock = vi.fn(async (url, init) => {
    if (url === "/api/chat/stream") {
      const s = controllableStream(init.signal);
      streams.push(s);
      return s.response;
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });
  global.fetch = fetchMock;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const send = async (text) => {
  const box = screen.getByLabelText("Message TORA");
  fireEvent.change(box, { target: { value: text } });
  fireEvent.keyDown(box, { key: "Enter" });
  await flush();
};

describe("TORAPage streaming chat", () => {
  it("shows live progress, streams the answer and ends with checks and follow-ups", async () => {
    render(<TORAPage user={{ name: "Asha Rao" }} />);
    await send("EMI for 20 lakh at 9% for 20 years?");

    expect(screen.getByText("EMI for 20 lakh at 9% for 20 years?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop generating" })).toBeInTheDocument();
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe("Bearer test-token");

    const s = streams[0];
    s.push(": connected\n\n");
    s.push(sse("stage", { stage: "calculating", label: "Running the numbers" }));
    s.push(sse("tool", { status: "running", name: "finance_calc", label: "Finance engine", operation: "emi" }));
    await flush();
    expect(screen.getAllByText("Running the numbers").length).toBeGreaterThan(0);
    expect(screen.getByText("Finance engine")).toBeInTheDocument();

    s.push(sse("tool", { status: "done", name: "finance_calc", label: "Finance engine", operation: "emi", summary: "EMI ₹17,995" }));
    s.push(sse("stage", { stage: "writing", label: "Writing the answer" }));
    s.push(sse("token", { text: "Your EMI is " }));
    s.push(sse("token", { text: "**₹17,995**." }));
    await flush();
    expect(screen.getByText("₹17,995").tagName).toBe("STRONG");

    s.push(
      sse("final", {
        response: "Your EMI is **₹17,995** a month.",
        conversation_id: "conv-1",
        turn: 1,
        model: "gemma",
        grounding: { action: "none", checked: 3, unsupported: [] },
        tools: [{ name: "finance_calc", label: "Finance engine", operation: "emi", ok: true, summary: "EMI ₹17,995" }],
        suggestions: ["What if I prepay ₹1 lakh?"],
      })
    );
    s.close();
    await flush();

    expect(screen.getByText("3 figures checked")).toBeInTheDocument();
    expect(screen.getByText(/Worked through 2 steps/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Helpful" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
    expect(localStorage.getItem("spendsy_tora_conversation_id")).toBe("conv-1");

    fireEvent.click(screen.getByRole("button", { name: "What if I prepay ₹1 lakh?" }));
    await flush();
    expect(bodies()[1]).toEqual({ conversation_id: "conv-1", message: "What if I prepay ₹1 lakh?" });
  });

  it("applies a replace event from the grounding check", async () => {
    render(<TORAPage />);
    await send("tax on 12 lakh");
    const s = streams[0];
    s.push(sse("token", { text: "Tax is ₹99,999" }));
    await flush();
    expect(screen.getByText("Tax is ₹99,999")).toBeInTheDocument();
    s.push(sse("replace", { text: "Tax is ₹0 under the new regime", reason: "regenerated" }));
    await flush();
    expect(screen.queryByText("Tax is ₹99,999")).toBeNull();
    expect(screen.getByText("Tax is ₹0 under the new regime")).toBeInTheDocument();
    s.push(sse("final", { response: "Tax is ₹0 under the new regime", grounding: { action: "regenerated", checked: 2 } }));
    s.close();
    await flush();
    expect(screen.getByText("Figures corrected")).toBeInTheDocument();
  });

  it("stops mid-reply, keeps the partial text and can try again", async () => {
    render(<TORAPage />);
    await send("Plan my debts");
    streams[0].push(sse("stage", { stage: "planning", label: "Working out what to calculate" }));
    streams[0].push(sse("token", { text: "Start with the card" }));
    await flush();

    fireEvent.click(screen.getByRole("button", { name: "Stop generating" }));
    await flush();
    expect(screen.getByText("Start with the card")).toBeInTheDocument();
    expect(screen.getByText(/Stopped\. This reply wasn't saved/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Try again/ }));
    await flush();
    expect(bodies()).toHaveLength(2);
    expect(bodies()[1].message).toBe("Plan my debts");
    expect(screen.getAllByText("Plan my debts")).toHaveLength(1);
    expect(screen.queryByText("Start with the card")).toBeNull();
  });

  it("Escape stops generation", async () => {
    render(<TORAPage />);
    await send("hello");
    fireEvent.keyDown(screen.getByLabelText("Message TORA"), { key: "Escape" });
    await flush();
    expect(screen.getByText("Stopped before TORA answered.")).toBeInTheDocument();
  });

  it("starts a new conversation when the saved one has expired", async () => {
    localStorage.setItem("spendsy_tora_conversation_id", "old-conv");
    render(<TORAPage />);
    await send("my salary is 50k");
    expect(bodies()[0]).toEqual({ conversation_id: "old-conv", message: "my salary is 50k" });
    streams[0].push(sse("error", { status: 404, detail: "Conversation not found." }));
    streams[0].close();
    await flush();
    expect(bodies()[1]).toEqual({ message: "my salary is 50k" });
    streams[1].push(sse("final", { response: "Noted: salary ₹50,000.", conversation_id: "new-conv", turn: 1 }));
    streams[1].close();
    await flush();
    expect(screen.getByText("Noted: salary ₹50,000.")).toBeInTheDocument();
    expect(localStorage.getItem("spendsy_tora_conversation_id")).toBe("new-conv");
  });

  it("shows a readable error with a retry when the server is down", async () => {
    fetchMock.mockImplementationOnce(async () => ({
      ok: false,
      status: 503,
      json: async () => ({ detail: "Ollama is not running" }),
    }));
    render(<TORAPage />);
    await send("hi");
    expect(screen.getByRole("alert")).toHaveTextContent("Unable to reach TORA: Ollama is not running");
    expect(screen.getByRole("button", { name: /Try again/ })).toBeInTheDocument();
  });

  it("falls back to /api/chat when the streaming route is missing", async () => {
    fetchMock.mockImplementation(async (url) => {
      if (url === "/api/chat/stream") return { ok: false, status: 404, json: async () => ({ detail: "Not Found" }) };
      return { ok: true, status: 200, json: async () => ({ response: "Plain answer", conversation_id: "c9", turn: 1 }) };
    });
    render(<TORAPage />);
    await send("hi");
    await waitFor(() => expect(screen.getByText("Plain answer")).toBeInTheDocument());
    expect(fetchMock.mock.calls.map(([u]) => u)).toEqual(["/api/chat/stream", "/api/chat"]);
  });

  it("sends starter prompts and respects Shift+Enter", async () => {
    render(<TORAPage />);
    const box = screen.getByLabelText("Message TORA");
    fireEvent.change(box, { target: { value: "line one" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Plan a goal/ }));
    await flush();
    expect(bodies()[0].message).toMatch(/₹50 lakh in 15 years/);
    expect(box).toHaveValue("line one");
  });

  it("restores a reply that was interrupted by a reload as stopped", () => {
    localStorage.setItem(
      "spendsy_tora_live_chat",
      JSON.stringify([
        { id: "u1", role: "user", content: "old question" },
        { id: "a1", role: "assistant", content: "half an ans", status: "streaming", prompt: "old question" },
      ])
    );
    render(<TORAPage />);
    expect(screen.getByText("half an ans")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Try again/ })).toBeInTheDocument();
  });

  it("keeps user text as plain text", async () => {
    const user = userEvent.setup();
    render(<TORAPage />);
    await user.type(screen.getByLabelText("Message TORA"), "**not bold**");
    expect(screen.getByLabelText("Message TORA")).toHaveValue("**not bold**");
    await user.keyboard("{Enter}");
    await flush();
    expect(screen.getByText("**not bold**")).toBeInTheDocument();
  });
});
