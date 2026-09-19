#!/usr/bin/env node
/**
 * Copy TORA's chat UI into a Spendsy checkout.
 *
 * This repo holds TORA's backend plus three frontend files that belong inside the Spendsy app.
 * Nothing moved them there automatically, which is how you end up pulling this repo, running
 * Spendsy, and still seeing the old chat screen — the new files were never in the app.
 *
 *   node scripts/install-into-spendsy.mjs <path-to-spendsy>
 *   node scripts/install-into-spendsy.mjs D:\Projects\Spendsy
 *   node scripts/install-into-spendsy.mjs ../Spendsy --check    (report only, copy nothing)
 *
 * Every file it would overwrite is backed up next to itself first, and it refuses to write
 * anywhere that does not look like a Spendsy frontend.
 */
import { copyFileSync, existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCE_ROOT = resolve(HERE, "..", "src");

// relative to <spendsy>/frontend/src
const FILES = [
  "pages/TORAPage.jsx",
  "pages/tora/sse.js",
  "pages/tora/markdown.jsx",
  "tests/tora/TORAPage.test.jsx",
  "tests/tora/sse.test.js",
  "tests/tora/markdown.test.jsx",
];

const args = process.argv.slice(2);
const checkOnly = args.includes("--check");
const target = args.find((a) => !a.startsWith("--"));

function fail(message) {
  console.error(`\n  ${message}\n`);
  process.exit(1);
}

if (!target) {
  fail("Where is Spendsy?  node scripts/install-into-spendsy.mjs <path-to-spendsy>");
}

const spendsy = resolve(target);
if (!existsSync(spendsy)) fail(`No such folder: ${spendsy}`);

// Accept either the repo root or the frontend folder itself.
const candidates = [join(spendsy, "frontend", "src"), join(spendsy, "src")];
const destRoot = candidates.find((c) => existsSync(c));
if (!destRoot) {
  fail(`That does not look like a Spendsy checkout — expected ${candidates.join("  or  ")}`);
}
if (!existsSync(join(destRoot, "pages"))) {
  fail(`Found ${destRoot} but no pages/ inside it. Point this at the Spendsy repo root.`);
}

// The components import two things that belong to Spendsy, not to this repo. Copying the files
// into a checkout that lacks either produces a build error at first render rather than a clear
// message, so it is checked up front.
const REQUIRES = [
  { what: '@shared/utils/cn', paths: ["shared/utils/cn.ts", "shared/utils/cn.js", "shared/utils/cn.jsx"], fromRepoRoot: true },
  { what: "Spendsy's src/api", paths: ["api.js", "api.ts", "api/index.js"], fromRepoRoot: false },
];
const missing = [];
for (const need of REQUIRES) {
  const roots = need.fromRepoRoot ? [spendsy, resolve(destRoot, "..", "..")] : [destRoot];
  const found = need.paths.some((rel) => roots.some((root) => existsSync(join(root, rel))));
  if (!found) missing.push(`${need.what} (looked for ${need.paths.join(", ")})`);
}
if (missing.length) {
  console.warn("\n  WARNING — this checkout is missing what the chat UI imports:");
  for (const m of missing) console.warn(`    ${m}`);
  console.warn("  The files will still be copied, but Spendsy will fail to build until these exist.");
}

const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
let copied = 0;
let identical = 0;
const changed = [];

for (const relative of FILES) {
  const from = join(SOURCE_ROOT, relative);
  const to = join(destRoot, relative);
  if (!existsSync(from)) fail(`Missing source file: ${from}`);

  const incoming = readFileSync(from);
  if (existsSync(to) && readFileSync(to).equals(incoming)) {
    identical += 1;
    continue;
  }
  changed.push(relative);
  if (checkOnly) continue;

  mkdirSync(dirname(to), { recursive: true });
  if (existsSync(to)) {
    const backup = `${to}.backup-${stamp}`;
    copyFileSync(to, backup);
    console.log(`  saved your version -> ${backup}`);
  }
  copyFileSync(from, to);
  copied += 1;
  console.log(`  installed ${relative}`);
}

console.log("");
if (checkOnly) {
  console.log(changed.length
    ? `  ${changed.length} file(s) differ and would be installed:\n    ${changed.join("\n    ")}`
    : "  Spendsy already has this version of the TORA chat UI.");
  process.exit(changed.length ? 1 : 0);
}
console.log(`  ${copied} file(s) installed, ${identical} already up to date.`);
if (copied) {
  console.log("\n  Restart the Spendsy dev server. Vite does not always notice a file swapped");
  console.log("  underneath it, and the browser may need a hard reload (Ctrl+Shift+R).");
}
