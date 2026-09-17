import React, { useState, useEffect, useRef } from "react";
import { motion } from "framer-motion";
import {
  Send,
  Copy,
  Check,
  RotateCcw,
  Paperclip,
} from "lucide-react";
import { cn } from "@shared/utils/cn";
import { getStoredAccessToken } from "../api";

let msgId = 0;
const nextId = (prefix = "msg") => {
  msgId += 1;
  return `${prefix}-${Date.now()}-${msgId}`;
};

const getTimestamp = () =>
  new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

const CONVERSATION_KEY = "spendsy_tora_conversation_id";

// Phase 6A: send the Spendsy sign-in token so TORA can keep account memory
// and read the user's own transactions. Without a token TORA works anonymously.
function toraHeaders(extra = {}) {
  const headers = { ...extra };
  const token = getStoredAccessToken();
  if (token) {
    headers.Authorization = token.startsWith("Bearer ") ? token : `Bearer ${token}`;
  }
  return headers;
}

const SUGGESTIONS = [

];

// Clean text formatter without symbols or emojis
function MessageContent({ text }) {
  const lines = text.split("\n");

  return (
    <div className="space-y-2 text-sm leading-relaxed">
      {lines.map((line, idx) => {
        const trimmed = line.trim();
        if (!trimmed) {
          return <div key={idx} className="h-1.5" />;
        }

        // Heading 3
        if (trimmed.startsWith("### ")) {
          return (
            <h4 key={idx} className="text-sm font-bold tracking-tight text-blue-400 mt-2 mb-0.5">
              {trimmed.replace(/^###\s+/, "")}
            </h4>
          );
        }

        // Heading 2
        if (trimmed.startsWith("## ")) {
          return (
            <h3 key={idx} className="text-base font-bold tracking-tight text-white mt-3 mb-1">
              {trimmed.replace(/^##\s+/, "")}
            </h3>
          );
        }

        // Bullet list
        if (trimmed.startsWith("- ") || trimmed.startsWith("• ") || trimmed.startsWith("* ")) {
          const item = trimmed.replace(/^[-•*]\s+/, "");
          return (
            <div key={idx} className="flex items-start gap-2 ml-1">
              <span className="w-1.5 h-1.5 rounded-full bg-blue-400 mt-2 shrink-0" />
              <div className="flex-1">{formatInlineText(item)}</div>
            </div>
          );
        }

        // Numbered list
        const numMatch = trimmed.match(/^(\d+)\.\s+(.*)/);
        if (numMatch) {
          return (
            <div key={idx} className="flex items-start gap-2 ml-1">
              <span className="text-xs font-semibold text-blue-400 mt-0.5 shrink-0 min-w-4">
                {numMatch[1]}.
              </span>
              <div className="flex-1">{formatInlineText(numMatch[2])}</div>
            </div>
          );
        }

        // Highlight blockquote
        if (trimmed.startsWith("> ")) {
          return (
            <div
              key={idx}
              className="p-3 my-1.5 rounded-xl bg-blue-500/10 border-l-2 border-blue-500 text-blue-200 text-xs font-medium"
            >
              {formatInlineText(trimmed.replace(/^>\s+/, ""))}
            </div>
          );
        }

        return <p key={idx}>{formatInlineText(trimmed)}</p>;
      })}
    </div>
  );
}

function formatInlineText(text) {
  const parts = [];
  const regex = /(\*\*.*?\*\*|`.*?`)/g;
  let lastIdx = 0;
  let match;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIdx) {
      parts.push(text.substring(lastIdx, match.index));
    }
    const token = match[0];
    if (token.startsWith("**") && token.endsWith("**")) {
      parts.push(
        <strong key={match.index} className="font-semibold text-white">
          {token.slice(2, -2)}
        </strong>
      );
    } else if (token.startsWith("`") && token.endsWith("`")) {
      parts.push(
        <code
          key={match.index}
          className="px-1.5 py-0.5 rounded bg-white/10 text-cyan-300 font-mono text-xs"
        >
          {token.slice(1, -1)}
        </code>
      );
    }
    lastIdx = regex.lastIndex;
  }

  if (lastIdx < text.length) {
    parts.push(text.substring(lastIdx));
  }

  return parts.length > 0 ? parts : text;
}

export default function TORAPage({
  user,
  theme = "dark",
  showToast,
}) {
  const [messages, setMessages] = useState(() => {
    try {
      const saved = localStorage.getItem("spendsy_tora_live_chat");
      if (saved) return JSON.parse(saved);
    } catch {
      // ignore
    }
    return [
      {
        id: "msg-init",
        role: "assistant",
        content: `Hello ${user?.name ? user.name.split(" ")[0] : ""}. How can I help you today?`.trim(),
        timestamp: getTimestamp(),
      },
    ];
  });

  // Server-side conversation id: history, memory and topic state live on the backend.
  const [conversationId, setConversationId] = useState(() => {
    try {
      return localStorage.getItem(CONVERSATION_KEY) || null;
    } catch {
      return null;
    }
  });

  const [input, setInput] = useState("");
  const [isTyping, setIsTyping] = useState(false);
  const [copiedId, setCopiedId] = useState(null);

  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  const fileRef = useRef(null);
  const [uploading, setUploading] = useState(false);

  // Persist chat
  useEffect(() => {
    try {
      localStorage.setItem("spendsy_tora_live_chat", JSON.stringify(messages));
    } catch (e) {
      console.error("Failed to save chat:", e);
    }
  }, [messages]);

  useEffect(() => {
    try {
      if (conversationId) localStorage.setItem(CONVERSATION_KEY, conversationId);
      else localStorage.removeItem(CONVERSATION_KEY);
    } catch {
      // storage unavailable: the conversation simply won't survive a reload
    }
  }, [conversationId]);

  // Auto scroll
  useEffect(() => {
    scrollRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isTyping]);

  const handleCopy = (id, text) => {
    navigator.clipboard.writeText(text);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
    if (showToast) showToast("Copied text", "info");
  };

  const handleClear = () => {
    if (conversationId) {
      // Delete the server-side conversation and everything TORA remembered in it
      fetch(`/api/conversations/${encodeURIComponent(conversationId)}`, {
        method: "DELETE",
        headers: toraHeaders(),
      }).catch(() => {});
      setConversationId(null);
    }
    const reset = [
      {
        id: nextId(),
        role: "assistant",
        content: `Chat reset. What would you like to ask?`,
        timestamp: getTimestamp(),
      },
    ];
    setMessages(reset);
    if (showToast) showToast("Conversation reset", "info");
  };

  // Phase 11: read a Form 16, salary slip, bank statement or AIS. Facts are saved only after confirmation.
  const inr = (v) => `₹${Number(v).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

  const uploadDocument = async (file, password) => {
    const form = new FormData();
    form.append("file", file);
    if (conversationId) form.append("conversation_id", conversationId);
    if (password) form.append("password", password);
    const response = await fetch("/api/documents", { method: "POST", headers: toraHeaders(), body: form });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(typeof data.detail === "string" ? data.detail : `Upload failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  };

  const handleFile = async (event) => {
    const file = event.target.files && event.target.files[0];
    event.target.value = "";
    if (!file || uploading) return;
    setUploading(true);
    setMessages((prev) => [...prev, { id: nextId("usr"), role: "user", content: `📎 ${file.name}`, timestamp: getTimestamp() }]);
    try {
      let data;
      try {
        data = await uploadDocument(file);
      } catch (err) {
        if (err.status === 422 && /password/i.test(err.message)) {
          const password = window.prompt("This PDF is password-protected. Enter its password:");
          if (!password) throw err;
          data = await uploadDocument(file, password);
        } else {
          throw err;
        }
      }
      if (data.conversation_id) setConversationId(data.conversation_id);
      const doc = data.document;
      const facts = doc.proposed_facts || [];
      const lines = [
        `I read your ${doc.doc_type.replace("_", " ")} (${doc.confidence} confidence).`,
        ...facts.map((f) => `• ${f.label}: ${inr(f.value)}`),
        ...(doc.warnings || []).map((w) => `Note: ${w}`),
        facts.length ? "Save these to your memory?" : "You can now ask me about this document.",
      ];
      setMessages((prev) => [...prev, {
        id: nextId("ast"), role: "assistant", content: lines.join("\n"), timestamp: getTimestamp(),
        doc: facts.length ? { id: doc.id, status: "pending" } : null,
      }]);
    } catch (err) {
      setMessages((prev) => [...prev, {
        id: nextId("ast-err"), role: "assistant", isError: true, timestamp: getTimestamp(),
        content: `I couldn't read that file: ${err.message}`,
      }]);
    } finally {
      setUploading(false);
    }
  };

  const resolveDocument = async (msgId, docId, action) => {
    try {
      const response = await fetch(`/api/documents/${encodeURIComponent(docId)}/${action}`, {
        method: "POST",
        headers: toraHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ conversation_id: conversationId }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setMessages((prev) => prev.map((m) => (m.id === msgId ? { ...m, doc: { ...m.doc, status: action } } : m)));
      if (showToast) showToast(action === "confirm" ? "Saved to TORA's memory" : "Document discarded", "info");
    } catch (err) {
      if (showToast) showToast(`Could not update: ${err.message}`, "error");
    }
  };

  const handleSend = async (customText) => {
    const text = customText || input;
    const trimmed = text.trim();
    if (!trimmed || isTyping) return;

    const userMsg = {
      id: nextId("usr"),
      role: "user",
      content: trimmed,
      timestamp: getTimestamp(),
    };

    setMessages((prev) => [...prev, userMsg]);
    if (!customText) setInput("");
    setIsTyping(true);

    try {
      // Complete loop: Chat UI -> FastAPI (/api/chat) -> session store -> ToraAgent -> LLMProvider
      const postTurn = (id) =>
        fetch("/api/chat", {
          method: "POST",
          headers: toraHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify(id ? { conversation_id: id, message: trimmed } : { message: trimmed }),
        });

      let response = await postTurn(conversationId);
      if (response.status === 404 && conversationId) {
        // Conversation expired or was deleted on the server: start a fresh one
        setConversationId(null);
        response = await postTurn(null);
      }

      if (!response.ok) {
        let errDetail = `Server error (${response.status})`;
        try {
          const errJson = await response.json();
          if (typeof errJson?.detail === "string") {
            errDetail = errJson.detail;
          } else if (Array.isArray(errJson?.detail) && errJson.detail.length > 0) {
            // FastAPI validation errors (422) arrive as a list of objects
            errDetail = errJson.detail.map((d) => d?.msg || String(d)).join("; ");
          }
        } catch {
          // ignore
        }
        const httpError = new Error(errDetail);
        httpError.status = response.status;
        throw httpError;
      }

      const data = await response.json();
      if (data.conversation_id) setConversationId(data.conversation_id);

      const assistantMsg = {
        id: nextId("ast"),
        role: "assistant",
        content: data.response || "No response received.",
        timestamp: getTimestamp(),
        model: data.model,
      };

      setMessages((prev) => [...prev, assistantMsg]);
    } catch (err) {
      console.error("Chat error:", err);
      const errorMsg = {
        id: nextId("ast-err"),
        role: "assistant",
        content:
          err.status === 401
            ? `${err.message}\n\nSign in to Spendsy again to keep chatting with TORA.`
            : !err.status || err.status === 503
              ? `Unable to reach TORA: ${err.message}\n\nMake sure FastAPI is running on port 8000 and Ollama is active.`
              : `TORA couldn't answer (${err.status}): ${err.message}`,
        timestamp: getTimestamp(),
        isError: true,
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setIsTyping(false);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const isInitialState = messages.length <= 1;

  return (
    <div
      className={cn(
        "flex flex-col h-screen w-full transition-colors duration-500 overflow-hidden",
        theme === "dark"
          ? "bg-[#08090a] text-white"
          : "bg-slate-50 text-slate-900"
      )}
    >
      {/* ── Top Header (Full Width) ─────────────────────────────────────────── */}
      <header
        className={cn(
          "px-6 sm:px-8 py-4 border-b flex items-center justify-between shrink-0 z-10",
          theme === "dark"
            ? "border-white/10 bg-[#08090a]/80 backdrop-blur-md"
            : "border-slate-200 bg-white/80 backdrop-blur-md"
        )}
      >
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-black tracking-tight">TORA</h1>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={handleClear}
            className={cn(
              "flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold transition-colors border",
              theme === "dark"
                ? "bg-white/5 border-white/10 text-slate-300 hover:bg-white/10 hover:text-white"
                : "bg-white border-slate-200 text-slate-600 hover:bg-slate-100 hover:text-slate-900"
            )}
          >
            <RotateCcw className="w-3.5 h-3.5" />
            <span>Reset</span>
          </button>
        </div>
      </header>

      {/* ── Message Feed (Centered Reading Column in Full Page) ──────────────── */}
      <div className="flex-1 overflow-y-auto px-4 sm:px-6 py-6 space-y-5">
        <div className="max-w-3xl mx-auto w-full space-y-5">
          {isInitialState && (
            <div className="py-12 sm:py-20 text-center space-y-6">
              <div className="space-y-2">
                <h2 className="text-2xl sm:text-3xl font-black tracking-tight">
                  How can I help you today?
                </h2>
                <p className={cn("text-sm max-w-md mx-auto", theme === "dark" ? "text-slate-400" : "text-slate-500")}>
                  Ask anything about personal budgeting, emergency reserves, or financial planning.
                </p>
              </div>

              {/* Clean Suggestion Pills */}
              <div className="flex flex-wrap justify-center gap-2.5 pt-3">
                {SUGGESTIONS.map((item, idx) => (
                  <button
                    key={idx}
                    onClick={() => handleSend(item)}
                    className={cn(
                      "text-xs font-semibold px-4 py-2.5 rounded-full border transition-all hover:scale-[1.02] active:scale-[0.98]",
                      theme === "dark"
                        ? "bg-white/5 border-white/10 text-slate-300 hover:border-blue-400/50 hover:text-white"
                        : "bg-white border-slate-200 text-slate-700 hover:border-blue-500/50 hover:bg-slate-50 shadow-sm"
                    )}
                  >
                    {item}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Message Stream */}
          {messages.map((msg) => {
            const isUser = msg.role === "user";
            return (
              <motion.div
                key={msg.id}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.15 }}
                className={cn("flex", isUser ? "justify-end" : "justify-start")}
              >
                <div
                  className={cn(
                    "rounded-2xl px-4 sm:px-5 py-3 sm:py-3.5 transition-all",
                    isUser
                      ? "max-w-[85%] sm:max-w-[75%] bg-blue-600 text-white shadow-md"
                      : cn(
                        "max-w-[90%] sm:max-w-[80%] border",
                        msg.isError
                          ? "bg-rose-500/10 border-rose-500/30 text-rose-200"
                          : theme === "dark"
                            ? "bg-white/[0.04] border-white/10 text-slate-200"
                            : "bg-white border-slate-200 text-slate-800 shadow-sm"
                      )
                  )}
                >
                  {isUser ? (
                    <p className="text-sm font-medium whitespace-pre-wrap leading-relaxed">
                      {msg.content}
                    </p>
                  ) : (
                    <MessageContent text={msg.content} />
                  )}
                  {msg.doc && msg.doc.status === "pending" && (
                    <div className="flex gap-2 mt-3">
                      <button
                        onClick={() => resolveDocument(msg.id, msg.doc.id, "confirm")}
                        className="px-3 py-1.5 rounded-lg text-xs font-medium bg-blue-600 text-white hover:bg-blue-500"
                      >
                        Save to memory
                      </button>
                      <button
                        onClick={() => resolveDocument(msg.id, msg.doc.id, "dismiss")}
                        className="px-3 py-1.5 rounded-lg text-xs font-medium border border-slate-400/40 hover:bg-white/5"
                      >
                        Don't save
                      </button>
                    </div>
                  )}
                  {msg.doc && msg.doc.status !== "pending" && (
                    <p className="mt-2 text-xs opacity-70">
                      {msg.doc.status === "confirm" ? "Saved to memory." : "Not saved."}
                    </p>
                  )}

                  <div
                    className={cn(
                      "flex items-center justify-between mt-2 pt-1 text-[10px]",
                      isUser ? "text-blue-100 opacity-80" : "text-slate-400 opacity-70"
                    )}
                  >
                    <span>{msg.timestamp}</span>

                    {!isUser && !msg.isError && (
                      <button
                        onClick={() => handleCopy(msg.id, msg.content)}
                        className="p-1 hover:opacity-100 transition-opacity rounded"
                        title="Copy"
                      >
                        {copiedId === msg.id ? (
                          <Check className="w-3.5 h-3.5 text-emerald-400" />
                        ) : (
                          <Copy className="w-3.5 h-3.5" />
                        )}
                      </button>
                    )}
                  </div>
                </div>
              </motion.div>
            );
          })}

          {/* Typing indicator */}
          {isTyping && (
            <div className="flex justify-start">
              <div
                className={cn(
                  "rounded-2xl px-4 py-3 border flex items-center gap-1.5",
                  theme === "dark" ? "bg-white/[0.04] border-white/10" : "bg-white border-slate-200 shadow-sm"
                )}
              >
                <span className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-pulse" />
                <span className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-pulse delay-150" />
                <span className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-pulse delay-300" />
              </div>
            </div>
          )}

          <div ref={scrollRef} />
        </div>
      </div>

      {/* ── Input Box (Docked Bottom of Whole Page) ─────────────────────────── */}
      <div
        className={cn(
          "p-4 sm:p-6 border-t shrink-0 z-10",
          theme === "dark"
            ? "border-white/10 bg-[#08090a]/90 backdrop-blur-md"
            : "border-slate-200 bg-white/90 backdrop-blur-md"
        )}
      >
        <div className="max-w-3xl mx-auto flex items-center gap-2">
          <input
            ref={fileRef}
            type="file"
            accept=".pdf,.csv,.txt"
            className="hidden"
            onChange={handleFile}
          />
          <button
            onClick={() => fileRef.current?.click()}
            disabled={uploading || isTyping}
            className={cn(
              "p-3.5 rounded-2xl shrink-0 transition-all flex items-center justify-center border",
              theme === "dark" ? "border-white/15 text-slate-300 hover:bg-white/5" : "border-slate-200 text-slate-600 hover:bg-slate-50",
              (uploading || isTyping) && "opacity-50 cursor-not-allowed"
            )}
            aria-label="Upload Form 16, salary slip or bank statement"
            title="Upload Form 16, salary slip or bank statement"
          >
            <Paperclip className="w-4 h-4" />
          </button>
          <div
            className={cn(
              "flex-1 rounded-2xl border px-4 py-3 flex items-center transition-all focus-within:ring-2 focus-within:ring-blue-500/20",
              theme === "dark"
                ? "bg-white/[0.03] border-white/15 focus-within:border-blue-500"
                : "bg-slate-50 border-slate-200 focus-within:border-blue-600 focus-within:bg-white"
            )}
          >
            <input
              ref={inputRef}
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask TORA about your finances..."
              className={cn(
                "w-full bg-transparent outline-none text-sm placeholder:text-slate-400",
                theme === "dark" ? "text-white" : "text-slate-900"
              )}
            />
          </div>

          <button
            onClick={() => handleSend()}
            disabled={!input.trim() || isTyping}
            className={cn(
              "p-3.5 rounded-2xl shrink-0 transition-all flex items-center justify-center shadow-md",
              input.trim() && !isTyping
                ? "bg-blue-600 text-white hover:bg-blue-500 active:scale-95 shadow-blue-500/20"
                : "bg-white/5 text-slate-500 cursor-not-allowed border border-white/10"
            )}
            aria-label="Send"
          >
            <Send className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
