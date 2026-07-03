#!/usr/bin/env node
/**
 * validate_mermaid.mjs — parse every ```mermaid fenced block under the given
 * paths with the real mermaid engine (v11) and report which ones fail.
 *
 * This turns "does the diagram render?" into a measurable number instead of a
 * manual eyeball check. It catches SYNTAX errors (what mermaid.parse checks);
 * it does NOT catch rendering degradation like literal `$$…$$` text or a
 * quoted `rgb("…")` color — those parse fine but render wrong, and are guarded
 * by the deterministic repair passes + their unit tests instead.
 *
 * Usage:
 *   node scripts/validate_mermaid.mjs [path ...]      # default: lings-desktop/pages
 *   node scripts/validate_mermaid.mjs --verbose PATH  # also print OK lines
 *
 * Exit code: 0 if every block parses, 1 if any block fails (CI-friendly).
 *
 * Requires mermaid + jsdom (see scripts/package.json): run `make validate-mermaid`
 * which installs them on first use, or `cd scripts && npm install`.
 */
import fs from "fs";
import path from "path";

const args = process.argv.slice(2);
const verbose = args.includes("--verbose");
const targets = args.filter((a) => !a.startsWith("--"));
const roots = targets.length ? targets : ["lings-desktop/pages"];

let mermaid;
try {
  const { JSDOM } = await import("jsdom");
  const dom = new JSDOM("<!DOCTYPE html><body></body>", { pretendToBeVisual: true });
  global.window = dom.window;
  global.document = dom.window.document;
  global.navigator = dom.window.navigator;
  global.DOMPurify = { sanitize: (x) => x, addHook: () => {} };
  mermaid = (await import("mermaid")).default;
  mermaid.initialize({ startOnLoad: false, securityLevel: "loose", suppressErrorRendering: true });
} catch (e) {
  console.error(
    "✗ mermaid/jsdom not installed. Run `make validate-mermaid` or `cd scripts && npm install`.\n" +
      `  (${e.message})`,
  );
  process.exit(2);
}

function walk(target) {
  const stat = fs.statSync(target);
  if (stat.isFile()) return target.endsWith(".md") ? [target] : [];
  return fs
    .readdirSync(target)
    .flatMap((name) => walk(path.join(target, name)));
}

function mermaidBlocks(text) {
  const lines = text.split("\n");
  const blocks = [];
  let cur = null;
  let startLine = 0;
  for (let i = 0; i < lines.length; i++) {
    if (cur === null && /^\s*```mermaid\s*$/.test(lines[i])) {
      cur = [];
      startLine = i + 2; // 1-indexed first content line
    } else if (cur !== null && /^\s*```\s*$/.test(lines[i])) {
      blocks.push({ startLine, code: cur.join("\n") });
      cur = null;
    } else if (cur !== null) {
      cur.push(lines[i]);
    }
  }
  return blocks;
}

const files = roots.flatMap((r) => (fs.existsSync(r) ? walk(r) : []));
let total = 0;
let failed = 0;

for (const file of files.sort()) {
  const text = fs.readFileSync(file, "utf8");
  for (const { startLine, code } of mermaidBlocks(text)) {
    total++;
    const kind = (code.trim().split(/\s|\n/)[0] || "?").toLowerCase();
    try {
      await mermaid.parse(code);
      if (verbose) console.log(`OK    ${file}:${startLine} [${kind}]`);
    } catch (e) {
      failed++;
      const msg = String(e.message || e).replace(/\s+/g, " ").slice(0, 200);
      console.log(`FAIL  ${file}:${startLine} [${kind}] ${msg}`);
    }
  }
}

const summary = `${failed}/${total} mermaid blocks failed to parse` + (files.length ? "" : " (no .md files found)");
console.log(`\n${summary}`);
process.exit(failed > 0 ? 1 : 0);
