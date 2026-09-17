import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Markdown, parseInline, parseMarkdown } from "../../pages/tora/markdown";

describe("parseMarkdown", () => {
  it("splits headings, paragraphs and rules", () => {
    const blocks = parseMarkdown("## Your plan\nPay the card first.\nThen the loan.\n\n---\n### Why");
    expect(blocks.map((b) => b.type)).toEqual(["heading", "paragraph", "rule", "heading"]);
    expect(blocks[0]).toMatchObject({ level: 2, text: "Your plan" });
    expect(blocks[1].text).toBe("Pay the card first.\nThen the loan.");
  });

  it("parses bullet and numbered lists with nesting", () => {
    const [list] = parseMarkdown("1. Card A\n   - 42% interest\n   - ₹60,000 due\n2. Loan\n3. Car");
    expect(list).toMatchObject({ type: "list", ordered: true, start: 1 });
    expect(list.items.map((i) => i.text)).toEqual(["Card A", "Loan", "Car"]);
    expect(list.items[0].sub[0]).toMatchObject({ type: "list", ordered: false });
    expect(list.items[0].sub[0].items.map((i) => i.text)).toEqual(["42% interest", "₹60,000 due"]);
  });

  it("keeps a list together across blank lines between items", () => {
    const blocks = parseMarkdown("- one\n\n- two\n\nAfter");
    expect(blocks.map((b) => b.type)).toEqual(["list", "paragraph"]);
    expect(blocks[0].items).toHaveLength(2);
  });

  it("parses tables with alignment", () => {
    const [table] = parseMarkdown("| Option | Interest |\n|:--|--:|\n| Avalanche | ₹41,200 |\n| Snowball | ₹44,900 |");
    expect(table).toMatchObject({
      type: "table",
      header: ["Option", "Interest"],
      aligns: [null, "right"],
      rows: [
        ["Avalanche", "₹41,200"],
        ["Snowball", "₹44,900"],
      ],
    });
  });

  it("treats an unclosed code fence as code (mid-stream)", () => {
    const [code] = parseMarkdown("```text\nEMI = P × r");
    expect(code).toMatchObject({ type: "code", lang: "text", text: "EMI = P × r", closed: false });
  });

  it("nests blocks inside quotes", () => {
    const [quote] = parseMarkdown("> **Note:** confirm with your bank\n> - fees apply");
    expect(quote.type).toBe("quote");
    expect(quote.children.map((b) => b.type)).toEqual(["paragraph", "list"]);
  });

  it("does not mistake a pipe in prose for a table", () => {
    expect(parseMarkdown("Rent | EMI split is fine").map((b) => b.type)).toEqual(["paragraph"]);
  });
});

describe("parseInline", () => {
  it("handles bold, italic, code and links", () => {
    const tokens = parseInline("Pay **₹5,000** now, *not* later. See `80C` or [the rule](https://incometax.gov.in).");
    expect(tokens.map((t) => t.type)).toEqual(["text", "strong", "text", "em", "text", "code", "text", "link", "text"]);
    expect(tokens[7].href).toBe("https://incometax.gov.in");
  });

  it("does not italicise snake_case or multiplication", () => {
    expect(parseInline("use tax_saving_finder and 2 * 3 * 4").every((t) => t.type === "text")).toBe(true);
  });

  it("never links non-http URLs", () => {
    const tokens = parseInline("[click](javascript:alert(1))");
    expect(tokens.some((t) => t.type === "link")).toBe(false);
  });
});

describe("<Markdown />", () => {
  it("renders model output as text, never HTML", () => {
    const { container } = render(<Markdown text={'<img src=x onerror="alert(1)"> **bold** <script>x()</script>'} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
    expect(screen.getByText("bold").tagName).toBe("STRONG");
  });

  it("renders tables with numeric cells right-aligned", () => {
    render(<Markdown text={"| Debt | Balance |\n|---|---|\n| Card | ₹60,000 |"} theme="light" />);
    const cell = screen.getByText("₹60,000").closest("td");
    expect(cell.className).toContain("text-right");
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("opens links safely in a new tab", () => {
    render(<Markdown text="Read [the notice](https://example.gov.in/notice)." />);
    const link = screen.getByRole("link", { name: "the notice" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });
});
