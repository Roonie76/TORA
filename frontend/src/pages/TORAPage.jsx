import React, { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Calculator,
  Globe,
  Check,
  ChevronDown,
  CircleCheck,
  CircleX,
  Copy,
  Landmark,
  Loader2,
  Paperclip,
  PiggyBank,
  RefreshCw,
  RotateCcw,
  Scale,
  ShieldCheck,
  Sparkles,
  Square,
  ThumbsDown,
  ThumbsUp,
  Wallet,
} from "lucide-react";
import { cn } from "@shared/utils/cn";
import { getStoredAccessToken } from "../api";
import { Markdown } from "./tora/markdown";
import { ChatHttpError, readErrorDetail, streamChat } from "./tora/sse";

let msgId = 0;
const nextId = (prefix = "msg") => {
  msgId += 1;
  return `${prefix}-${Date.now()}-${msgId}`;
};

const getTimestamp = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

const CHAT_KEY = "spendsy_tora_live_chat";
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

const STARTERS = [
  {
    icon: Wallet,
    title: "Get out of debt",
    prompt:
      "I earn ₹45,000 a month and pay ₹22,000 in EMIs on two credit cards and a personal loan. How do I get out of debt?",
  },
  {
    icon: Landmark,
    title: "Old or new tax regime",
    prompt: "My salary is ₹12 lakh and I invest ₹1.5 lakh under 80C. Which tax regime is better for me?",
  },
  {
    icon: PiggyBank,
    title: "Plan a goal",
    prompt: "How much should I invest every month to have ₹50 lakh in 15 years?",
  },
  {
    icon: Calculator,
    title: "Prepay or invest",
    prompt: "Should I prepay my home loan or invest the extra ₹10,000 a month?",
  },
];

const STAGE_FALLBACK_LABELS = {
  understanding: "Understanding your question",
  remembering: "Updating what I know about you",
  planning: "Working out what to calculate",
  calculating: "Running the numbers",
  researching: "Checking sources",
  reading_rules: "Looking up the rules",
  reading_records: "Reading your Spendsy records",
  writing: "Writing the answer",
  checking: "Double-checking the figures",
};

const inr = (v) => `₹${Number(v).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

const formatDuration = (ms) => {
  const secs = Math.max(0, Math.round(ms / 1000));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  return `${mins}m ${String(secs % 60).padStart(2, "0")}s`;
};

const friendlyError = (err) => {
  if (err.status === 401) return `${err.message}\n\nSign in to Spendsy again to keep chatting with TORA.`;
  if (err.status === 429) return `${err.message}\n\nYou're sending messages quickly. Wait a moment, then try again.`;
  if (!err.status || err.status === 503) {
    return `Unable to reach TORA: ${err.message}\n\nMake sure the TORA server is running on port 8000 and Ollama is active.`;
  }
  if (err.status === 504) return "TORA took too long to answer. Try again, or ask a shorter question.";
  return `TORA couldn't answer (${err.status}): ${err.message}`;
};

// Messages saved mid-stream (tab closed) come back as stopped replies.
const loadMessages = (user) => {
  try {
    const saved = JSON.parse(localStorage.getItem(CHAT_KEY) || "null");
    if (Array.isArray(saved) && saved.length) {
      return saved.map((m) => (m.status === "streaming" ? { ...m, status: "stopped", live: false } : { ...m, live: false }));
    }
  } catch {
    // ignore
  }
  const first = user?.name ? ` ${user.name.split(" ")[0]}` : "";
  return [
    {
      id: "msg-init",
      role: "assistant",
      content: `Hello${first}. How can I help you today?`,
      timestamp: getTimestamp(),
      status: "done",
    },
  ];
};

// ── Smooth text reveal ─────────────────────────────────────────────────────────
// Tokens arrive in bursts; reveal them at an even pace so the reply glides in.
function useSmoothText(target, animate, reduced) {
  const [shown, setShown] = useState(animate && !reduced ? "" : target);
  const shownRef = useRef(shown);

  useEffect(() => {
    if (!animate || reduced) {
      shownRef.current = target;
      setShown(target);
      return undefined;
    }
    // If the text was replaced (grounding fix), keep only the common prefix.
    let current = shownRef.current;
    if (!target.startsWith(current)) {
      let i = 0;
      while (i < current.length && i < target.length && current[i] === target[i]) i += 1;
      current = target.slice(0, i);
      shownRef.current = current;
    }
    let frame;
    const step = () => {
      const len = shownRef.current.length;
      const remaining = target.length - len;
      if (remaining <= 0) return;
      const n = Math.max(1, Math.min(remaining, Math.ceil(remaining / 12)));
      shownRef.current = target.slice(0, len + n);
      setShown(shownRef.current);
      frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target, animate, reduced]);

  return shown;
}

function Elapsed({ startedAt }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return <span className="tabular-nums">{formatDuration(now - startedAt)}</span>;
}

function ToraMark({ busy, dark }) {
  return (
    <div className="relative w-8 h-8 shrink-0">
      {busy && <span className="absolute inset-0 rounded-full bg-blue-500/30 animate-ping" aria-hidden="true" />}
      <div
        className={cn(
          "relative w-8 h-8 rounded-full grid place-items-center bg-gradient-to-br from-blue-500 to-cyan-400 shadow-md",
          dark ? "shadow-blue-500/20" : "shadow-blue-500/30"
        )}
      >
        <Sparkles className="w-4 h-4 text-white" />
      </div>
    </div>
  );
}

// ── Working steps (stages + tools) ─────────────────────────────────────────────
function StepList({ steps, tools, dark }) {
  return (
    <ul className="space-y-1.5 py-1">
      {steps.map((s) => {
        const stageTools = tools.filter((t) => t.stepKey === s.key);
        return (
          <li key={s.key} className="text-xs">
            <div className="flex items-center gap-2">
              {s.status === "active" ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin text-blue-400" />
              ) : (
                <CircleCheck className={cn("w-3.5 h-3.5", dark ? "text-emerald-400/80" : "text-emerald-600")} />
              )}
              <span className={cn(s.status === "active" ? "tora-shimmer font-medium" : dark ? "text-slate-400" : "text-slate-500")}>
                {s.label}
              </span>
            </div>
            {stageTools.length > 0 && (
              <ul className="ml-[1.35rem] mt-1 space-y-1">
                {stageTools.map((t) => (
                  <li key={t.key} className="flex items-start gap-1.5">
                    {t.status === "running" ? (
                      <Loader2 className="w-3 h-3 mt-0.5 animate-spin text-blue-400 shrink-0" />
                    ) : t.status === "failed" ? (
                      <CircleX className="w-3 h-3 mt-0.5 text-rose-400 shrink-0" />
                    ) : (
                      <Check className="w-3 h-3 mt-0.5 text-emerald-400 shrink-0" />
                    )}
                    <span className={dark ? "text-slate-400" : "text-slate-500"}>
                      <span className={cn("font-medium", dark ? "text-slate-300" : "text-slate-700")}>{t.label}</span>
                      {t.operation ? ` · ${t.operation.replace(/_/g, " ")}` : ""}
                      {t.summary ? ` — ${t.summary}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function ProgressPanel({ msg, dark, reduced }) {
  const [open, setOpen] = useState(false);
  const streaming = msg.status === "streaming";
  const hasText = Boolean(msg.content);
  const steps = msg.steps || [];
  const tools = msg.tools || [];
  if (!steps.length && !tools.length) {
    if (!streaming) return null;
    return (
      <div className="flex items-center gap-2 text-xs py-1" role="status" aria-live="polite">
        <span className="flex gap-1" aria-hidden="true">
          {[0, 1, 2].map((d) => (
            <span key={d} className="w-1.5 h-1.5 rounded-full bg-blue-400 tora-bounce" style={{ animationDelay: `${d * 150}ms` }} />
          ))}
        </span>
        <span className="tora-shimmer">Connecting to TORA</span>
      </div>
    );
  }

  const active = [...steps].reverse().find((s) => s.status === "active");
  const expanded = streaming && !hasText ? true : open;
  const summary = streaming
    ? (expanded ? "Working on your answer" : active?.label || "Working on your answer")
    : `Worked through ${steps.length} step${steps.length === 1 ? "" : "s"}${msg.durationMs ? ` in ${formatDuration(msg.durationMs)}` : ""}`;

  return (
    <div className={cn("mb-2 rounded-xl border px-3 py-2", dark ? "border-white/10 bg-white/[0.02]" : "border-slate-200 bg-slate-50/70")}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 text-xs text-left"
        aria-expanded={expanded}
        aria-label={expanded ? "Hide the steps TORA took" : "Show the steps TORA took"}
      >
        {streaming ? (
          <Loader2 className="w-3.5 h-3.5 animate-spin text-blue-400 shrink-0" />
        ) : (
          <CircleCheck className={cn("w-3.5 h-3.5 shrink-0", dark ? "text-emerald-400/80" : "text-emerald-600")} />
        )}
        <span
          role="status"
          aria-live="polite"
          className={cn("flex-1 truncate", streaming ? "tora-shimmer font-medium" : dark ? "text-slate-400" : "text-slate-500")}
        >
          {summary}
        </span>
        {streaming && msg.startedAt && (
          <span className={dark ? "text-slate-500" : "text-slate-400"}>
            <Elapsed startedAt={msg.startedAt} />
          </span>
        )}
        {!(streaming && !hasText) && (
          <ChevronDown className={cn("w-3.5 h-3.5 transition-transform", dark ? "text-slate-500" : "text-slate-400", expanded && "rotate-180")} />
        )}
      </button>
      {streaming && msg.complexity === "complex" && !hasText && (
        <p className={cn("text-[11px] mt-1 ml-[1.35rem]", dark ? "text-amber-300/80" : "text-amber-700")}>
          This is a detailed case, so I'm comparing the options carefully. It can take a few minutes.
        </p>
      )}
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={reduced ? false : { height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={reduced ? { opacity: 0 } : { height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="mt-1.5">
              <StepList steps={steps} tools={tools} dark={dark} />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

const TOOL_ICONS = {
  calculator: Calculator,
  finance_calc: Calculator,
  tax_calc: Landmark,
  rules_lookup: Scale,
  spendsy_data: Wallet,
  research: Globe,
  web_search: Globe,
  web_fetch: Globe,
};


function VerificationRow({ msg, dark }) {
  const chips = [];
  const seen = new Set();
  (msg.toolResults || []).forEach((t) => {
    const key = `${t.label}-${t.ok}`;
    if (seen.has(key)) return;
    seen.add(key);
    chips.push({
      key,
      icon: t.ok ? TOOL_ICONS[t.name] || Calculator : CircleX,
      text: t.label,
      title: t.summary || t.operation || t.label,
      tone: t.ok ? "neutral" : "bad",
    });
  });
  const g = msg.grounding;
  if (g && g.checked > 0) {
    if (g.action === "none") {
      chips.push({ key: "g", icon: ShieldCheck, text: `${g.checked} figure${g.checked === 1 ? "" : "s"} checked`, tone: "good", title: "Every figure matched your details or a calculation" });
    } else if (g.action === "regenerated") {
      chips.push({ key: "g", icon: ShieldCheck, text: "Figures corrected", tone: "good", title: "A first draft had unsupported figures, so TORA rewrote it" });
    } else {
      chips.push({
        key: "g",
        icon: AlertTriangle,
        text: "Some figures unverified",
        tone: "warn",
        title: (g.unsupported || []).join(", ") || "Some figures could not be matched to a calculation",
      });
    }
  }
  if (!chips.length) return null;
  const toneClass = {
    neutral: dark ? "border-white/10 text-slate-400 bg-white/[0.03]" : "border-slate-200 text-slate-500 bg-white",
    good: dark ? "border-emerald-500/20 text-emerald-300 bg-emerald-500/5" : "border-emerald-200 text-emerald-700 bg-emerald-50",
    warn: dark ? "border-amber-500/25 text-amber-300 bg-amber-500/5" : "border-amber-200 text-amber-700 bg-amber-50",
    bad: dark ? "border-rose-500/25 text-rose-300 bg-rose-500/5" : "border-rose-200 text-rose-700 bg-rose-50",
  };
  return (
    <div className="flex flex-wrap gap-1.5 mt-3">
      {chips.map(({ key, icon: Icon, text, title, tone }) => (
        <span key={key} title={title} className={cn("inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10.5px] font-medium", toneClass[tone])}>
          <Icon className="w-3 h-3" />
          {text}
        </span>
      ))}
    </div>
  );
}

function IconButton({ onClick, label, active, activeClass, children, dark, disabled }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={label}
      aria-label={label}
      className={cn(
        "p-1.5 rounded-lg transition-colors disabled:opacity-40",
        dark ? "text-slate-500 hover:text-slate-200 hover:bg-white/5" : "text-slate-400 hover:text-slate-700 hover:bg-slate-100",
        active && activeClass
      )}
    >
      {children}
    </button>
  );
}

const AssistantMessage = memo(function AssistantMessage({
  msg,
  dark,
  reduced,
  isLast,
  busy,
  onCopy,
  copied,
  onRate,
  onRetry,
  onSuggestion,
  onResolveDocument,
}) {
  const streaming = msg.status === "streaming";
  const text = useSmoothText(msg.content || "", Boolean(msg.live), reduced);
  const catchingUp = text.length < (msg.content || "").length;
  const showCaret = (streaming || catchingUp) && Boolean(msg.content);
  const failed = msg.status === "error" || msg.isError;
  const stopped = msg.status === "stopped";
  const settled = !streaming && !catchingUp;

  return (
    <div className="flex gap-3 group">
      <ToraMark busy={streaming} dark={dark} />
      <div className="flex-1 min-w-0 pt-1">
        <ProgressPanel msg={msg} dark={dark} reduced={reduced} />

        {failed ? (
          <div
            className={cn(
              "rounded-xl border px-3.5 py-3 text-sm",
              dark ? "bg-rose-500/10 border-rose-500/30 text-rose-200" : "bg-rose-50 border-rose-200 text-rose-800"
            )}
            role="alert"
          >
            <Markdown text={msg.content} theme={dark ? "dark" : "light"} />
          </div>
        ) : (
          text && (
            <div className={dark ? "text-slate-200" : "text-slate-800"}>
              <Markdown text={text} theme={dark ? "dark" : "light"} caret={showCaret} />
            </div>
          )
        )}

        {stopped && (
          <p className={cn("mt-2 text-xs italic", dark ? "text-slate-500" : "text-slate-400")}>
            {msg.content ? "Stopped. This reply wasn't saved to the conversation." : "Stopped before TORA answered."}
          </p>
        )}

        {msg.doc && msg.doc.status === "pending" && (
          <div className="flex gap-2 mt-3">
            <button
              type="button"
              onClick={() => onResolveDocument(msg.id, msg.doc.id, "confirm")}
              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-blue-600 text-white hover:bg-blue-500 active:scale-[0.98] transition"
            >
              Save to memory
            </button>
            <button
              type="button"
              onClick={() => onResolveDocument(msg.id, msg.doc.id, "dismiss")}
              className={cn(
                "px-3 py-1.5 rounded-lg text-xs font-medium border transition",
                dark ? "border-white/15 hover:bg-white/5" : "border-slate-300 hover:bg-slate-100"
              )}
            >
              Don't save
            </button>
          </div>
        )}
        {msg.doc && msg.doc.status !== "pending" && (
          <p className="mt-2 text-xs opacity-70">{msg.doc.status === "confirm" ? "Saved to memory." : "Not saved."}</p>
        )}

        {settled && !failed && <VerificationRow msg={msg} dark={dark} />}

        {settled && (
          <div
            className={cn(
              "flex items-center gap-0.5 mt-2 -ml-1.5 text-[10px] transition-opacity",
              isLast ? "opacity-100" : "opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:focus-within:opacity-100"
            )}
          >
            {!failed && msg.content && (
              <IconButton dark={dark} onClick={() => onCopy(msg.id, msg.content)} label={copied ? "Copied" : "Copy"}>
                {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
              </IconButton>
            )}
            {!failed && msg.turn && (
              <>
                <IconButton
                  dark={dark}
                  onClick={() => onRate(msg, "up")}
                  label="Helpful"
                  active={msg.rating === "up"}
                  activeClass="text-emerald-400"
                  disabled={Boolean(msg.rating)}
                >
                  <ThumbsUp className="w-3.5 h-3.5" />
                </IconButton>
                <IconButton
                  dark={dark}
                  onClick={() => onRate(msg, "down")}
                  label="Not helpful"
                  active={msg.rating === "down"}
                  activeClass="text-rose-400"
                  disabled={Boolean(msg.rating)}
                >
                  <ThumbsDown className="w-3.5 h-3.5" />
                </IconButton>
              </>
            )}
            {(failed || stopped) && msg.prompt && (
              <button
                type="button"
                onClick={() => onRetry(msg)}
                disabled={busy}
                className={cn(
                  "ml-1 inline-flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] font-medium border transition disabled:opacity-40",
                  dark ? "border-white/15 text-slate-300 hover:bg-white/5" : "border-slate-300 text-slate-600 hover:bg-slate-100"
                )}
              >
                <RefreshCw className="w-3 h-3" />
                Try again
              </button>
            )}
            <span className={cn("ml-2", dark ? "text-slate-600" : "text-slate-400")}>{msg.timestamp}</span>
          </div>
        )}

        <AnimatePresence>
          {isLast && settled && !busy && !failed && msg.suggestions?.length > 0 && (
            <motion.div
              className="flex flex-wrap gap-2 mt-3"
              initial="hidden"
              animate="show"
              exit={{ opacity: 0 }}
              variants={{ hidden: {}, show: { transition: { staggerChildren: reduced ? 0 : 0.06 } } }}
            >
              {msg.suggestions.map((s) => (
                <motion.button
                  key={s}
                  type="button"
                  variants={{ hidden: { opacity: 0, y: reduced ? 0 : 6 }, show: { opacity: 1, y: 0 } }}
                  onClick={() => onSuggestion(s)}
                  className={cn(
                    "text-xs px-3 py-1.5 rounded-full border transition-colors text-left",
                    dark
                      ? "border-blue-400/25 text-blue-200 bg-blue-500/5 hover:bg-blue-500/15"
                      : "border-blue-200 text-blue-700 bg-blue-50 hover:bg-blue-100"
                  )}
                >
                  {s}
                </motion.button>
              ))}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
});

const UserMessage = memo(function UserMessage({ msg }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] sm:max-w-[75%] rounded-2xl rounded-br-md px-4 py-2.5 bg-blue-600 text-white shadow-md shadow-blue-600/10">
        <p className="text-sm whitespace-pre-wrap break-words leading-relaxed">{msg.content}</p>
      </div>
    </div>
  );
});

function EmptyState({ dark, onPick, reduced }) {
  return (
    <div className="py-10 sm:py-16 text-center space-y-7">
      <motion.div
        initial={reduced ? false : { opacity: 0, scale: 0.9 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.3 }}
        className="mx-auto w-14 h-14 rounded-2xl grid place-items-center bg-gradient-to-br from-blue-500 to-cyan-400 shadow-lg shadow-blue-500/25"
      >
        <Sparkles className="w-7 h-7 text-white" />
      </motion.div>
      <div className="space-y-2">
        <h2 className="text-2xl sm:text-3xl font-black tracking-tight">How can I help you today?</h2>
        <p className={cn("text-sm max-w-md mx-auto", dark ? "text-slate-400" : "text-slate-500")}>
          Debt, tax, loans, savings goals or your Form 16. Tell me your numbers and I'll work through your case.
        </p>
      </div>
      <motion.div
        className="grid sm:grid-cols-2 gap-2.5 text-left"
        initial="hidden"
        animate="show"
        variants={{ hidden: {}, show: { transition: { staggerChildren: reduced ? 0 : 0.07, delayChildren: 0.1 } } }}
      >
        {STARTERS.map(({ icon: Icon, title, prompt }) => (
          <motion.button
            key={title}
            type="button"
            variants={{ hidden: { opacity: 0, y: reduced ? 0 : 8 }, show: { opacity: 1, y: 0 } }}
            whileHover={reduced ? undefined : { y: -2 }}
            whileTap={reduced ? undefined : { scale: 0.98 }}
            onClick={() => onPick(prompt)}
            className={cn(
              "p-3.5 rounded-2xl border transition-colors",
              dark ? "bg-white/[0.03] border-white/10 hover:border-blue-400/40 hover:bg-white/[0.05]" : "bg-white border-slate-200 hover:border-blue-400 shadow-sm"
            )}
          >
            <div className="flex items-center gap-2 mb-1">
              <Icon className={cn("w-4 h-4", dark ? "text-blue-300" : "text-blue-600")} />
              <span className="text-sm font-semibold">{title}</span>
            </div>
            <p className={cn("text-xs leading-relaxed line-clamp-2", dark ? "text-slate-400" : "text-slate-500")}>{prompt}</p>
          </motion.button>
        ))}
      </motion.div>
    </div>
  );
}

// A callback whose identity never changes but always runs the latest closure,
// so memoised message rows don't re-render on every streamed frame.
function useStableCallback(fn) {
  const ref = useRef(fn);
  useLayoutEffect(() => {
    ref.current = fn;
  });
  return useCallback((...args) => ref.current(...args), []);
}

const PAGE_STYLES = `
@keyframes tora-blink { 0%, 100% { opacity: .75 } 50% { opacity: 0 } }
@keyframes tora-shimmer { 0% { background-position: 100% 0 } 100% { background-position: -100% 0 } }
@keyframes tora-bounce { 0%, 80%, 100% { transform: translateY(0); opacity: .5 } 40% { transform: translateY(-3px); opacity: 1 } }
.tora-bounce { animation: tora-bounce 1.1s ease-in-out infinite; }
.tora-shimmer {
  background: linear-gradient(90deg, currentColor 0%, currentColor 40%, rgba(96,165,250,1) 50%, currentColor 60%, currentColor 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
  animation: tora-shimmer 1.8s linear infinite;
}
.tora-caret > div > :is(p, h3, h4, h5, h6):last-child::after,
.tora-caret > div > :is(ul, ol):last-child > li:last-child > div > div:first-child::after {
  content: ""; display: inline-block; width: .5em; height: 1.05em; margin-left: 2px;
  vertical-align: -0.18em; border-radius: 2px; background: rgb(96,165,250);
  animation: tora-blink 1s steps(2, start) infinite;
}
@media (prefers-reduced-motion: reduce) {
  .tora-shimmer, .tora-bounce, .tora-caret *::after { animation: none !important; }
  .tora-shimmer { -webkit-text-fill-color: currentColor; background: none; }
}
`;

export default function TORAPage({ user, theme = "dark", showToast }) {
  const dark = theme === "dark";
  const reduced = useReducedMotion();
  const [messages, setMessages] = useState(() => loadMessages(user));
  // Server-side conversation id: history, memory and topic state live on the backend.
  const [conversationId, setConversationId] = useState(() => {
    try {
      return localStorage.getItem(CONVERSATION_KEY) || null;
    } catch {
      return null;
    }
  });
  const conversationRef = useRef(conversationId);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [copiedId, setCopiedId] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [atBottom, setAtBottom] = useState(true);

  const scrollerRef = useRef(null);
  const stickRef = useRef(true);
  const inputRef = useRef(null);
  const fileRef = useRef(null);
  const abortRef = useRef(null);
  const pendingTokens = useRef("");
  const flushFrame = useRef(null);

  const setConversation = useCallback((id) => {
    conversationRef.current = id;
    setConversationId(id);
  }, []);

  // Persist chat (not on every streamed token: once the reply settles)
  useEffect(() => {
    if (busy) return;
    try {
      localStorage.setItem(CHAT_KEY, JSON.stringify(messages.slice(-200)));
    } catch {
      // storage full or unavailable
    }
  }, [messages, busy]);

  useEffect(() => {
    try {
      if (conversationId) localStorage.setItem(CONVERSATION_KEY, conversationId);
      else localStorage.removeItem(CONVERSATION_KEY);
    } catch {
      // storage unavailable: the conversation simply won't survive a reload
    }
  }, [conversationId]);

  useEffect(() => () => abortRef.current?.abort(), []);

  // ── Scrolling: follow the reply unless the reader scrolled up ────────────────
  const scrollToBottom = useCallback(
    (smooth) => {
      const el = scrollerRef.current;
      if (!el) return;
      el.scrollTo({ top: el.scrollHeight, behavior: smooth && !reduced ? "smooth" : "auto" });
    },
    [reduced]
  );

  const handleScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    stickRef.current = near;
    setAtBottom(near);
  };

  useLayoutEffect(() => {
    if (stickRef.current) scrollToBottom(false);
  });

  // ── Composer ────────────────────────────────────────────────────────────────
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input]);

  const patchMessage = useCallback((id, patch) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, ...(typeof patch === "function" ? patch(m) : patch) } : m)));
  }, []);

  const flushTokens = useCallback(
    (id) => {
      flushFrame.current = null;
      const chunk = pendingTokens.current;
      if (!chunk) return;
      pendingTokens.current = "";
      patchMessage(id, (m) => ({ content: (m.content || "") + chunk }));
    },
    [patchMessage]
  );

  const applyEvent = useCallback(
    (id, { event, data }) => {
      if (event === "token") {
        pendingTokens.current += data.text || "";
        if (!flushFrame.current) flushFrame.current = requestAnimationFrame(() => flushTokens(id));
        return;
      }
      if (event === "replace") {
        if (flushFrame.current) cancelAnimationFrame(flushFrame.current);
        flushFrame.current = null;
        pendingTokens.current = "";
        patchMessage(id, { content: data.text || "" });
        return;
      }
      if (event === "complexity") {
        patchMessage(id, { complexity: data.level });
        return;
      }
      if (event === "stage") {
        patchMessage(id, (m) => {
          const steps = [...(m.steps || [])];
          const last = steps[steps.length - 1];
          if (last && last.stage === data.stage) return {};
          const done = steps.map((s) => (s.status === "active" ? { ...s, status: "done" } : s));
          done.push({
            key: `${data.stage}-${done.length}`,
            stage: data.stage,
            label: data.label || STAGE_FALLBACK_LABELS[data.stage] || data.stage,
            status: "active",
          });
          return { steps: done };
        });
        return;
      }
      if (event === "tool") {
        patchMessage(id, (m) => {
          const tools = [...(m.tools || [])];
          const stepKey = (m.steps || [])[(m.steps || []).length - 1]?.key;
          if (data.status === "running") {
            tools.push({
              key: `${data.name}-${tools.length}`,
              stepKey,
              name: data.name,
              label: data.label || data.name,
              operation: data.operation,
              status: "running",
            });
          } else {
            const idx = tools.map((t) => t.name === data.name && t.status === "running").lastIndexOf(true);
            if (idx >= 0) tools[idx] = { ...tools[idx], status: data.status, summary: data.summary };
          }
          return { tools };
        });
      }
    },
    [flushTokens, patchMessage]
  );

  const postPlain = async (body, signal) => {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: toraHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      signal,
    });
    if (!response.ok) throw new ChatHttpError(await readErrorDetail(response), response.status);
    return response.json();
  };

  const runTurn = async (text, { addUser = true } = {}) => {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    const assistantId = nextId("ast");
    const startedAt = Date.now();
    stickRef.current = true;
    setMessages((prev) => [
      ...prev,
      ...(addUser ? [{ id: nextId("usr"), role: "user", content: trimmed, timestamp: getTimestamp() }] : []),
      {
        id: assistantId,
        role: "assistant",
        content: "",
        status: "streaming",
        live: true,
        startedAt,
        prompt: trimmed,
        steps: [],
        tools: [],
        timestamp: getTimestamp(),
      },
    ]);
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;

    const attempt = async (id) => {
      const body = id ? { conversation_id: id, message: trimmed } : { message: trimmed };
      try {
        return await streamChat({
          body,
          headers: toraHeaders(),
          signal: controller.signal,
          onEvent: (evt) => applyEvent(assistantId, evt),
        });
      } catch (err) {
        // Older server without the streaming route: fall back to a single reply.
        if (err instanceof ChatHttpError && (err.status === 404 || err.status === 405) && !String(err.message).match(/conversation/i)) {
          return postPlain(body, controller.signal);
        }
        throw err;
      }
    };

    try {
      let final;
      try {
        final = await attempt(conversationRef.current);
      } catch (err) {
        if (err.status === 404 && conversationRef.current) {
          // Conversation expired or was deleted on the server: start a fresh one
          setConversation(null);
          patchMessage(assistantId, { steps: [], tools: [], content: "" });
          final = await attempt(null);
        } else {
          throw err;
        }
      }
      if (flushFrame.current) cancelAnimationFrame(flushFrame.current);
      flushFrame.current = null;
      pendingTokens.current = "";
      if (final.conversation_id) setConversation(final.conversation_id);
      patchMessage(assistantId, (m) => ({
        content: final.response || "No response received.",
        status: "done",
        durationMs: Date.now() - startedAt,
        model: final.model,
        turn: final.turn,
        conversationId: final.conversation_id,
        complexity: final.complexity || m.complexity,
        grounding: final.grounding,
        toolResults: final.tools || [],
        suggestions: final.suggestions || [],
        steps: (m.steps || []).map((s) => ({ ...s, status: "done" })),
        tools: (m.tools || []).map((t) => (t.status === "running" ? { ...t, status: "done" } : t)),
      }));
    } catch (err) {
      if (flushFrame.current) cancelAnimationFrame(flushFrame.current);
      flushFrame.current = null;
      const partial = pendingTokens.current;
      pendingTokens.current = "";
      if (err.name === "AbortError") {
        patchMessage(assistantId, (m) => ({
          content: (m.content || "") + partial,
          status: "stopped",
          durationMs: Date.now() - startedAt,
          steps: (m.steps || []).map((s) => ({ ...s, status: "done" })),
          tools: (m.tools || []).map((t) => (t.status === "running" ? { ...t, status: "failed", summary: "stopped" } : t)),
        }));
      } else {
        console.error("Chat error:", err);
        patchMessage(assistantId, {
          content: friendlyError(err),
          status: "error",
          isError: true,
          live: false,
          steps: [],
          tools: [],
        });
      }
    } finally {
      abortRef.current = null;
      setBusy(false);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  };

  const handleSend = (customText) => {
    const text = customText ?? input;
    if (!text.trim() || busy) return;
    if (customText === undefined) setInput("");
    runTurn(text);
  };

  const handleStop = () => abortRef.current?.abort();

  const handleRetry = (msg) => {
    if (busy) return;
    setMessages((prev) => prev.filter((m) => m.id !== msg.id));
    runTurn(msg.prompt, { addUser: false });
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    } else if (e.key === "Escape" && busy) {
      e.preventDefault();
      handleStop();
    }
  };

  const handleCopy = useCallback(
    (id, text) => {
      navigator.clipboard?.writeText(text).catch(() => {});
      setCopiedId(id);
      setTimeout(() => setCopiedId((cur) => (cur === id ? null : cur)), 2000);
      if (showToast) showToast("Copied text", "info");
    },
    [showToast]
  );

  const handleClear = () => {
    abortRef.current?.abort();
    const id = conversationRef.current;
    if (id) {
      // Delete the server-side conversation and everything TORA remembered in it
      fetch(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE", headers: toraHeaders() }).catch(() => {});
      setConversation(null);
    }
    setMessages([
      { id: nextId(), role: "assistant", content: "Chat reset. What would you like to ask?", timestamp: getTimestamp(), status: "done" },
    ]);
    if (showToast) showToast("Conversation reset", "info");
  };

  // Phase 11: read a Form 16, salary slip, bank statement or AIS. Facts are saved only after confirmation.
  const uploadDocument = async (file, password) => {
    const form = new FormData();
    form.append("file", file);
    if (conversationRef.current) form.append("conversation_id", conversationRef.current);
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
    if (!file || uploading || busy) return;
    setUploading(true);
    stickRef.current = true;
    const readingId = nextId("ast");
    setMessages((prev) => [
      ...prev,
      { id: nextId("usr"), role: "user", content: `📎 ${file.name}`, timestamp: getTimestamp() },
      {
        id: readingId,
        role: "assistant",
        content: "",
        status: "streaming",
        startedAt: Date.now(),
        steps: [{ key: "read", stage: "reading", label: `Reading ${file.name}`, status: "active" }],
        tools: [],
        timestamp: getTimestamp(),
      },
    ]);
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
      if (data.conversation_id) setConversation(data.conversation_id);
      const doc = data.document;
      const facts = doc.proposed_facts || [];
      const lines = [
        `I read your **${doc.doc_type.replace(/_/g, " ")}** (${doc.confidence} confidence).`,
        "",
        ...facts.map((f) => `- ${f.label}: **${inr(f.value)}**`),
        ...(doc.warnings || []).map((w) => `> ${w}`),
        "",
        facts.length ? "Save these to your memory?" : "You can now ask me about this document.",
      ];
      patchMessage(readingId, {
        content: lines.join("\n"),
        status: "done",
        live: true,
        steps: [],
        doc: facts.length ? { id: doc.id, status: "pending" } : null,
        suggestions: facts.length ? [] : ["What does this document tell you about my tax?"],
      });
    } catch (err) {
      patchMessage(readingId, {
        content: `I couldn't read that file: ${err.message}`,
        status: "error",
        isError: true,
        steps: [],
      });
    } finally {
      setUploading(false);
    }
  };

  const resolveDocument = useCallback(
    async (messageId, docId, action) => {
      try {
        const response = await fetch(`/api/documents/${encodeURIComponent(docId)}/${action}`, {
          method: "POST",
          headers: toraHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ conversation_id: conversationRef.current }),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        patchMessage(messageId, (m) => ({ doc: { ...m.doc, status: action } }));
        if (showToast) showToast(action === "confirm" ? "Saved to TORA's memory" : "Document discarded", "info");
      } catch (err) {
        if (showToast) showToast(`Could not update: ${err.message}`, "error");
      }
    },
    [patchMessage, showToast]
  );

  // Phase 13: answer ratings (kept for future training only if the server enables it)
  const rateAnswer = useCallback(
    async (msg, rating) => {
      if (!msg.turn || !msg.conversationId || msg.rating) return;
      patchMessage(msg.id, { rating });
      try {
        await fetch("/api/feedback", {
          method: "POST",
          headers: toraHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ conversation_id: msg.conversationId, turn: msg.turn, rating }),
        });
      } catch {
        // rating is best-effort
      }
    },
    [patchMessage]
  );

  const onRetry = useStableCallback(handleRetry);
  const onSuggestion = useStableCallback((text) => handleSend(text));
  const onPickStarter = useStableCallback((text) => handleSend(text));

  const isInitialState = messages.length <= 1 && !busy;
  const lastAssistantId = useMemo(() => [...messages].reverse().find((m) => m.role === "assistant")?.id, [messages]);
  const canSend = Boolean(input.trim()) && !busy;

  return (
    <div
      className={cn(
        "flex flex-col h-screen w-full transition-colors duration-500 overflow-hidden",
        dark ? "bg-[#08090a] text-white" : "bg-slate-50 text-slate-900"
      )}
    >
      <style>{PAGE_STYLES}</style>

      <header
        className={cn(
          "px-4 sm:px-8 py-3.5 border-b flex items-center justify-between shrink-0 z-10",
          dark ? "border-white/10 bg-[#08090a]/80 backdrop-blur-md" : "border-slate-200 bg-white/80 backdrop-blur-md"
        )}
      >
        <div className="flex items-center gap-2.5">
          <h1 className="text-lg font-black tracking-tight">TORA</h1>
          <span
            className={cn(
              "hidden sm:inline-flex items-center gap-1.5 text-[11px] px-2 py-0.5 rounded-full border",
              dark ? "border-white/10 text-slate-400" : "border-slate-200 text-slate-500"
            )}
          >
            <span className={cn("w-1.5 h-1.5 rounded-full", busy ? "bg-blue-400 animate-pulse" : "bg-emerald-400")} />
            {busy ? "Working" : "Personal finance assistant"}
          </span>
        </div>
        <button
          type="button"
          onClick={handleClear}
          className={cn(
            "flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold transition-colors border",
            dark
              ? "bg-white/5 border-white/10 text-slate-300 hover:bg-white/10 hover:text-white"
              : "bg-white border-slate-200 text-slate-600 hover:bg-slate-100 hover:text-slate-900"
          )}
        >
          <RotateCcw className="w-3.5 h-3.5" />
          <span>Reset</span>
        </button>
      </header>

      <div className="relative flex-1 min-h-0">
        <div ref={scrollerRef} onScroll={handleScroll} className="h-full overflow-y-auto px-4 sm:px-6 py-6" aria-live="off">
          <div className="max-w-3xl mx-auto w-full space-y-6">
            {isInitialState && <EmptyState dark={dark} reduced={reduced} onPick={onPickStarter} />}

            {messages.map((msg) => (
              <motion.div
                key={msg.id}
                initial={reduced ? false : { opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.2, ease: "easeOut" }}
              >
                {msg.role === "user" ? (
                  <UserMessage msg={msg} />
                ) : (
                  <AssistantMessage
                    msg={msg}
                    dark={dark}
                    reduced={reduced}
                    isLast={msg.id === lastAssistantId}
                    busy={busy}
                    copied={copiedId === msg.id}
                    onCopy={handleCopy}
                    onRate={rateAnswer}
                    onRetry={onRetry}
                    onSuggestion={onSuggestion}
                    onResolveDocument={resolveDocument}
                  />
                )}
              </motion.div>
            ))}
            <div className="h-2" />
          </div>
        </div>

        <AnimatePresence>
          {!atBottom && (
            <motion.button
              type="button"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 8 }}
              onClick={() => {
                stickRef.current = true;
                scrollToBottom(true);
              }}
              className={cn(
                "absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border shadow-lg",
                dark ? "bg-[#15171a] border-white/15 text-slate-200" : "bg-white border-slate-200 text-slate-700"
              )}
              aria-label="Jump to latest message"
            >
              <ArrowDown className="w-3.5 h-3.5" />
              {busy ? "TORA is replying" : "Latest"}
            </motion.button>
          )}
        </AnimatePresence>
      </div>

      <div
        className={cn(
          "px-4 sm:px-6 pt-3 pb-3 sm:pb-4 border-t shrink-0 z-10",
          dark ? "border-white/10 bg-[#08090a]/90 backdrop-blur-md" : "border-slate-200 bg-white/90 backdrop-blur-md"
        )}
      >
        <div
          className={cn(
            "max-w-3xl mx-auto rounded-2xl border transition-all focus-within:ring-2 focus-within:ring-blue-500/20",
            dark ? "bg-white/[0.03] border-white/15 focus-within:border-blue-500" : "bg-white border-slate-200 focus-within:border-blue-600 shadow-sm"
          )}
        >
          <textarea
            ref={inputRef}
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={busy ? "TORA is working… (Esc to stop)" : "Ask TORA about your finances…"}
            aria-label="Message TORA"
            className={cn(
              "block w-full resize-none bg-transparent outline-none text-sm px-4 pt-3 pb-1 max-h-[200px] placeholder:text-slate-400",
              dark ? "text-white" : "text-slate-900"
            )}
          />
          <div className="flex items-center justify-between px-2 pb-2">
            <input ref={fileRef} type="file" accept=".pdf,.csv,.txt" className="hidden" onChange={handleFile} />
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              disabled={uploading || busy}
              className={cn(
                "flex items-center gap-1.5 px-2.5 py-1.5 rounded-xl text-xs transition-colors disabled:opacity-40 disabled:cursor-not-allowed",
                dark ? "text-slate-400 hover:bg-white/5 hover:text-slate-200" : "text-slate-500 hover:bg-slate-100 hover:text-slate-800"
              )}
              aria-label="Upload Form 16, salary slip or bank statement"
              title="Upload Form 16, salary slip or bank statement"
            >
              {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Paperclip className="w-4 h-4" />}
              <span className="hidden sm:inline">{uploading ? "Reading…" : "Attach"}</span>
            </button>

            <button
              type="button"
              onClick={busy ? handleStop : () => handleSend()}
              disabled={!busy && !canSend}
              className={cn(
                "w-9 h-9 rounded-xl grid place-items-center transition-all duration-150",
                busy
                  ? dark
                    ? "bg-white text-black hover:bg-slate-200"
                    : "bg-slate-900 text-white hover:bg-slate-700"
                  : canSend
                    ? "bg-blue-600 text-white hover:bg-blue-500 active:scale-95 shadow-md shadow-blue-500/25"
                    : dark
                      ? "bg-white/5 text-slate-600 cursor-not-allowed"
                      : "bg-slate-100 text-slate-400 cursor-not-allowed"
              )}
              aria-label={busy ? "Stop generating" : "Send"}
              title={busy ? "Stop (Esc)" : "Send (Enter)"}
            >
              <motion.span
                key={busy ? "stop" : "send"}
                initial={reduced ? false : { scale: 0.6, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                transition={{ duration: 0.15 }}
                className="grid place-items-center"
              >
                {busy ? <Square className="w-3.5 h-3.5 fill-current" /> : <ArrowUp className="w-4 h-4" />}
              </motion.span>
            </button>
          </div>
        </div>
        <p className={cn("max-w-3xl mx-auto mt-2 text-center text-[10.5px]", dark ? "text-slate-600" : "text-slate-400")}>
          TORA can make mistakes and is not a chartered accountant. Check important decisions with a professional.
        </p>
      </div>
    </div>
  );
}
