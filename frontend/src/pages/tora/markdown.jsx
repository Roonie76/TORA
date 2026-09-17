// A small, safe markdown renderer for TORA replies. It never injects HTML:
// everything becomes React elements, so model output can't run script.
import React from "react";
import { cn } from "@shared/utils/cn";

const TABLE_SEPARATOR = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const BULLET = /^(\s*)[-*•+]\s+(.*)$/;
const ORDERED = /^(\s*)(\d+)[.)]\s+(.*)$/;
const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const FENCE = /^\s*(```|~~~)\s*([\w+-]*)\s*$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;

const splitRow = (line) => {
  let row = line.trim();
  if (row.startsWith("|")) row = row.slice(1);
  if (row.endsWith("|") && !row.endsWith("\\|")) row = row.slice(0, -1);
  return row.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
};

const isTableStart = (lines, i) =>
  lines[i].includes("|") && i + 1 < lines.length && TABLE_SEPARATOR.test(lines[i + 1]) && lines[i + 1].includes("-");

// Parse markdown into a flat list of blocks:
// heading | paragraph | list | quote | code | table | rule
export function parseMarkdown(source) {
  const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i += 1;
      continue;
    }

    const fence = FENCE.exec(line);
    if (fence) {
      const body = [];
      i += 1;
      while (i < lines.length && !new RegExp(`^\\s*${fence[1]}\\s*$`).test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      const closed = i < lines.length;
      i += 1; // skip the closing fence (or run past the end while streaming)
      blocks.push({ type: "code", lang: fence[2] || "", text: body.join("\n"), closed });
      continue;
    }

    const heading = HEADING.exec(trimmed);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, text: heading[2] });
      i += 1;
      continue;
    }

    if (RULE.test(trimmed)) {
      blocks.push({ type: "rule" });
      i += 1;
      continue;
    }

    if (isTableStart(lines, i)) {
      const header = splitRow(lines[i]);
      const aligns = splitRow(lines[i + 1]).map((c) =>
        c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : null
      );
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      blocks.push({ type: "table", header, aligns, rows });
      continue;
    }

    if (trimmed.startsWith(">")) {
      const body = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        body.push(lines[i].trim().replace(/^>\s?/, ""));
        i += 1;
      }
      blocks.push({ type: "quote", children: parseMarkdown(body.join("\n")) });
      continue;
    }

    if (BULLET.test(line) || ORDERED.test(line)) {
      const ordered = !BULLET.test(line);
      const pattern = ordered ? ORDERED : BULLET;
      const baseIndent = pattern.exec(line)[1].length;
      const items = [];
      const start = ordered ? Number(ORDERED.exec(line)[2]) : null;
      while (i < lines.length) {
        const current = lines[i];
        const m = pattern.exec(current);
        if (m && m[1].length <= baseIndent + 1) {
          items.push({ text: ordered ? m[3] : m[2], sub: [] });
          i += 1;
          continue;
        }
        const indent = current.length - current.trimStart().length;
        if (current.trim() && indent > baseIndent && items.length) {
          // Nested list or continuation line
          const nested = [];
          while (i < lines.length && lines[i].trim() && lines[i].length - lines[i].trimStart().length > baseIndent) {
            const lineIndent = lines[i].length - lines[i].trimStart().length;
            nested.push(lines[i].slice(Math.min(baseIndent + 2, lineIndent)));
            i += 1;
          }
          const last = items[items.length - 1];
          const nestedBlocks = parseMarkdown(nested.join("\n"));
          if (nestedBlocks.length === 1 && nestedBlocks[0].type === "paragraph") {
            last.text = `${last.text} ${nestedBlocks[0].text}`;
          } else {
            last.sub.push(...nestedBlocks);
          }
          continue;
        }
        // A blank line inside a list continues it if the next line is another item
        if (!current.trim() && i + 1 < lines.length && pattern.test(lines[i + 1])) {
          i += 1;
          continue;
        }
        break;
      }
      blocks.push({ type: "list", ordered, start, items });
      continue;
    }

    // Paragraph: gather until a blank line or another block starts
    const body = [trimmed];
    i += 1;
    while (
      i < lines.length &&
      lines[i].trim() &&
      !FENCE.test(lines[i]) &&
      !HEADING.test(lines[i].trim()) &&
      !lines[i].trim().startsWith(">") &&
      !BULLET.test(lines[i]) &&
      !ORDERED.test(lines[i]) &&
      !RULE.test(lines[i].trim()) &&
      !isTableStart(lines, i)
    ) {
      body.push(lines[i].trim());
      i += 1;
    }
    blocks.push({ type: "paragraph", text: body.join("\n") });
  }
  return blocks;
}

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+?\*\*|__[^_\n]+?__)|(\*[^*\s][^*\n]*?\*|(?<![\w])_[^_\s][^_\n]*?_(?![\w]))|(~~[^~\n]+?~~)|(\[[^\]\n]+\]\((https?:\/\/[^\s)]+)\))|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/;

// Parse inline markdown into tokens: text | code | strong | em | del | link | br
export function parseInline(text) {
  const tokens = [];
  let rest = String(text || "");
  while (rest) {
    const m = INLINE.exec(rest);
    if (!m) {
      tokens.push({ type: "text", text: rest });
      break;
    }
    if (m.index > 0) tokens.push({ type: "text", text: rest.slice(0, m.index) });
    const [whole] = m;
    if (m[1]) tokens.push({ type: "code", text: whole.slice(1, -1) });
    else if (m[2]) tokens.push({ type: "strong", children: parseInline(whole.slice(2, -2)) });
    else if (m[3]) tokens.push({ type: "em", children: parseInline(whole.slice(1, -1)) });
    else if (m[4]) tokens.push({ type: "del", children: parseInline(whole.slice(2, -2)) });
    else if (m[5]) tokens.push({ type: "link", href: m[6], children: parseInline(whole.slice(1, whole.indexOf("]("))) });
    else if (m[7]) tokens.push({ type: "link", href: whole, children: [{ type: "text", text: whole }] });
    rest = rest.slice(m.index + whole.length);
  }
  return tokens;
}

const ALIGN_CLASS = { left: "text-left", right: "text-right", center: "text-center" };
const NUMERIC_CELL = /^[-+]?\s*(₹|rs\.?|inr)?\s*[\d,]+(\.\d+)?\s*(%|x|l|lakh|cr|k)?$/i;

function Inline({ text, dark }) {
  const render = (tokens, keyPrefix) =>
    tokens.map((t, idx) => {
      const key = `${keyPrefix}-${idx}`;
      switch (t.type) {
        case "code":
          return (
            <code
              key={key}
              className={cn(
                "px-1.5 py-0.5 rounded-md font-mono text-[0.8em]",
                dark ? "bg-white/10 text-cyan-300" : "bg-slate-100 text-cyan-700"
              )}
            >
              {t.text}
            </code>
          );
        case "strong":
          return (
            <strong key={key} className={cn("font-semibold", dark ? "text-white" : "text-slate-900")}>
              {render(t.children, key)}
            </strong>
          );
        case "em":
          return <em key={key}>{render(t.children, key)}</em>;
        case "del":
          return <del key={key} className="opacity-70">{render(t.children, key)}</del>;
        case "link":
          return (
            <a
              key={key}
              href={t.href}
              target="_blank"
              rel="noopener noreferrer nofollow"
              className={cn("underline underline-offset-2", dark ? "text-blue-300 hover:text-blue-200" : "text-blue-600 hover:text-blue-700")}
            >
              {render(t.children, key)}
            </a>
          );
        default: {
          const parts = t.text.split("\n");
          return parts.map((part, j) => (
            <React.Fragment key={`${key}-${j}`}>
              {j > 0 && <br />}
              {part}
            </React.Fragment>
          ));
        }
      }
    });
  return <>{render(parseInline(text), "i")}</>;
}

function Block({ block, dark }) {
  switch (block.type) {
    case "heading": {
      const size = block.level <= 1 ? "text-lg" : block.level === 2 ? "text-base" : "text-sm";
      const Tag = `h${Math.min(block.level + 2, 6)}`;
      return (
        <Tag
          className={cn(
            size,
            "font-bold tracking-tight mt-4 first:mt-0 mb-1",
            block.level >= 3 ? (dark ? "text-blue-300" : "text-blue-700") : dark ? "text-white" : "text-slate-900"
          )}
        >
          <Inline text={block.text} dark={dark} />
        </Tag>
      );
    }
    case "rule":
      return <hr className={cn("my-3", dark ? "border-white/10" : "border-slate-200")} />;
    case "code":
      return (
        <div className={cn("rounded-xl border overflow-hidden", dark ? "bg-black/40 border-white/10" : "bg-slate-50 border-slate-200")}>
          {block.lang && (
            <div className={cn("px-3 py-1 text-[10px] uppercase tracking-wider", dark ? "text-slate-400 bg-white/5" : "text-slate-500 bg-slate-100")}>
              {block.lang}
            </div>
          )}
          <pre className="px-3 py-2.5 overflow-x-auto text-xs leading-relaxed font-mono">
            <code>{block.text}</code>
          </pre>
        </div>
      );
    case "table":
      return (
        <div className={cn("rounded-xl border overflow-x-auto", dark ? "border-white/10" : "border-slate-200")}>
          <table className="w-full text-xs border-collapse">
            <thead className={dark ? "bg-white/[0.06]" : "bg-slate-100"}>
              <tr>
                {block.header.map((cell, c) => (
                  <th
                    key={c}
                    className={cn("px-3 py-2 font-semibold whitespace-nowrap", ALIGN_CLASS[block.aligns[c] || "left"])}
                  >
                    <Inline text={cell} dark={dark} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, r) => (
                <tr key={r} className={cn("border-t", dark ? "border-white/5 hover:bg-white/[0.03]" : "border-slate-100 hover:bg-slate-50")}>
                  {block.header.map((_, c) => {
                    const cell = row[c] ?? "";
                    const align = block.aligns[c] || (NUMERIC_CELL.test(cell.replace(/\*/g, "")) ? "right" : "left");
                    return (
                      <td key={c} className={cn("px-3 py-1.5 align-top", ALIGN_CLASS[align], align === "right" && "tabular-nums whitespace-nowrap")}>
                        <Inline text={cell} dark={dark} />
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "quote":
      return (
        <div
          className={cn(
            "px-3.5 py-2.5 rounded-xl border-l-2 text-[0.92em]",
            dark ? "bg-blue-500/10 border-blue-400 text-blue-100" : "bg-blue-50 border-blue-500 text-blue-900"
          )}
        >
          <MarkdownBlocks blocks={block.children} dark={dark} />
        </div>
      );
    case "list": {
      const ListTag = block.ordered ? "ol" : "ul";
      return (
        <ListTag className="space-y-1.5" start={block.ordered && block.start !== 1 ? block.start : undefined}>
          {block.items.map((item, idx) => (
            <li key={idx} className="flex items-start gap-2.5">
              {block.ordered ? (
                <span
                  className={cn(
                    "mt-[1px] shrink-0 min-w-[1.35rem] h-[1.35rem] rounded-full grid place-items-center text-[10px] font-bold tabular-nums",
                    dark ? "bg-blue-500/15 text-blue-300" : "bg-blue-100 text-blue-700"
                  )}
                  aria-hidden="true"
                >
                  {(block.start || 1) + idx}
                </span>
              ) : (
                <span className={cn("mt-[0.55em] w-1.5 h-1.5 rounded-full shrink-0", dark ? "bg-blue-400" : "bg-blue-500")} aria-hidden="true" />
              )}
              <div className="flex-1 min-w-0 space-y-1.5">
                <div>
                  <Inline text={item.text} dark={dark} />
                </div>
                {item.sub.length > 0 && <MarkdownBlocks blocks={item.sub} dark={dark} />}
              </div>
            </li>
          ))}
        </ListTag>
      );
    }
    default:
      return (
        <p>
          <Inline text={block.text} dark={dark} />
        </p>
      );
  }
}

function MarkdownBlocks({ blocks, dark }) {
  return (
    <div className="space-y-2.5">
      {blocks.map((block, idx) => (
        <Block key={idx} block={block} dark={dark} />
      ))}
    </div>
  );
}

export function Markdown({ text, theme = "dark", className, caret = false }) {
  const dark = theme === "dark";
  const blocks = React.useMemo(() => parseMarkdown(text), [text]);
  return (
    <div className={cn("tora-md text-sm leading-relaxed break-words", caret && "tora-caret", className)}>
      <MarkdownBlocks blocks={blocks} dark={dark} />
    </div>
  );
}

export default Markdown;
