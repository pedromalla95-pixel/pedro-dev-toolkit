#!/usr/bin/env node
// bundle.js — deterministic split of a GSD phase's plan bundle for verify-plan reviewers (C10, C20).
// Reviewers get the full text of every *-PLAN.md in the phase. When the assembled bundle is over
// --max bytes (default 200000) it is split:
//   1. sort plans by frontmatter `wave` (int, missing = 1), then by basename
//   2. cut at the wave boundary whose running size is nearest half the subset; a wave whose own
//      bytes exceed max may also be cut inside, by filename order
//   3. each part = full text of its own plans + an interface extract (frontmatter verbatim + task
//      names) of every plan NOT in the part, across the whole bundle
//   4. each ASSEMBLED part still > max is split again with the same rule (recursive). A single plan
//      that is still > max stays alone, flagged `oversize: true` — never loops.
// --since <prior verdict.json> --round 2 (focused re-review): plans whose body hash differs from the
// verdict's plan_hashes, plus one hop of dependents (reverse depends_on, shared files_modified,
// key_links from/to a changed plan's must_haves.artifacts path), go in full; all others as extracts.
// Output adds focus_plans / unchanged_plans. Any other round, a missing/unreadable/hash-less verdict,
// or no changed plan -> the full bundle, with full_reason. The verdict records no commit, so no
// git-diff block is added.
"use strict";
const fs = require("fs");
const path = require("path");
const { hashPlans } = require("./verdict.js");

const DEFAULT_MAX = 200000;

function parseArgs(argv) {
  const opts = { max: DEFAULT_MAX };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--plans") opts.plans = JSON.parse(argv[++i]);
    else if (a === "--max") opts.max = Number(argv[++i]);
    else if (a === "--emit") opts.emit = argv[++i];
    else if (a === "--since") opts.since = argv[++i];
    else if (a === "--round") opts.round = Number(argv[++i]);
    else throw new Error(`unknown arg: ${a}`);
  }
  if (!Array.isArray(opts.plans) || opts.plans.length === 0 || !(opts.max > 0)) {
    throw new Error("usage: bundle.js --plans '<json array of *-PLAN.md paths>' [--max 200000] [--emit <dir>] [--since <prior.verdict.json> --round <n>]");
  }
  return opts;
}

function frontmatter(text) {
  const m = text.match(/^---\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/);
  return m ? m[0].replace(/\s+$/, "") : "";
}

function readPlan(p) {
  const text = fs.readFileSync(p, "utf8");
  const fm = frontmatter(text);
  const w = fm.match(/^wave:\s*(\d+)/m);
  const taskNames = [];
  for (const t of text.matchAll(/<task\b[^>]*>([\s\S]*?)<\/task>/g)) {
    const n = t[1].match(/<name>([\s\S]*?)<\/name>/);
    if (n) taskNames.push(n[1].trim());
  }
  return { path: p, text, bytes: Buffer.byteLength(text), wave: w ? parseInt(w[1], 10) : 1, fm, taskNames };
}

// Frontmatter fields for the focus set. Line-anchored and block-bounded so quoted prose such as
// "closed r2: path: x" inside another key never matches.
function fmList(fm, key) {
  const lines = fm.split(/\r?\n/);
  const i = lines.findIndex((l) => new RegExp(`^${key}:`).test(l));
  if (i < 0) return [];
  const inline = lines[i].match(/:\s*\[(.*)\]/);
  const items = inline ? inline[1].split(",")
    : lines.slice(i + 1).filter((_, j, a) => a.slice(0, j + 1).every((l) => /^\s+-/.test(l)))
      .map((l) => l.replace(/^\s+-\s*/, ""));
  return items.map((x) => x.trim().replace(/^["']|["']$/g, "")).filter(Boolean);
}
// Values of `field:` inside the indented block under `key:` (e.g. artifacts -> path).
function fmBlock(fm, key, fields) {
  const lines = fm.split(/\r?\n/);
  const i = lines.findIndex((l) => new RegExp(`^\\s*${key}:`).test(l));
  if (i < 0) return [];
  const ind = lines[i].match(/^\s*/)[0].length, out = [];
  const re = new RegExp(`^\\s*(?:-\\s*)?(?:${fields.join("|")}):\\s*["']?([^"'#]+?)["']?\\s*$`);
  for (const l of lines.slice(i + 1)) {
    if (l.trim() && l.match(/^\s*/)[0].length <= ind) break;
    const m = l.match(re);
    if (m) out.push(m[1].trim());
  }
  return out;
}
const planId = (p) => path.basename(p).replace(/-PLAN\.md$/, "");

// Focus set vs a prior verdict, or {reason} when the bundle must stay full.
function focusSet(all, since, round) {
  if (round !== 2) return { reason: `round ${round} is always full` };
  let prior;
  try { prior = JSON.parse(fs.readFileSync(since, "utf8")); } catch (e) { return { reason: `prior verdict unreadable: ${e.message}` }; }
  const ph = prior && prior.plan_hashes;
  if (!ph || typeof ph !== "object" || !Object.keys(ph).length) return { reason: "prior verdict has no plan_hashes" };
  const now = hashPlans(all.map((p) => p.path));
  const changed = all.filter((p) => !ph[p.path] || ph[p.path] !== now[p.path]);
  if (!changed.length) return { reason: "no plan changed since the prior verdict" };
  // ponytail: one hop only; a transitive closure over a wave chain pulls in nearly every plan.
  const ids = new Set(changed.map((p) => planId(p.path)));
  const files = new Set(changed.flatMap((p) => fmList(p.fm, "files_modified")));
  const arts = new Set(changed.flatMap((p) => fmBlock(p.fm, "artifacts", ["path"])));
  const focus = all.filter((p) => ids.has(planId(p.path))
    || fmList(p.fm, "depends_on").some((d) => ids.has(d))
    || fmList(p.fm, "files_modified").some((f) => files.has(f))
    || fmBlock(p.fm, "key_links", ["from", "to"]).some((f) => arts.has(f)));
  return { focus };
}

function sortPlans(plans) {
  return plans.slice().sort((a, b) =>
    a.wave - b.wave || path.basename(a.path).localeCompare(path.basename(b.path)));
}

function extract(plan) {
  return [
    `===== ${path.basename(plan.path)} (interface extract) =====`,
    plan.fm || "(no frontmatter)",
    "Tasks:",
    ...(plan.taskNames.length ? plan.taskNames.map((n) => `- ${n}`) : ["- (none)"]),
  ].join("\n") + "\n";
}

// part, all: arrays of plan objects (from readPlan) or paths. Own plans full text, others as extracts.
function assemble(part, all) {
  const load = (x) => (typeof x === "string" ? readPlan(x) : x);
  part = part.map(load); all = sortPlans(all.map(load));
  const own = new Set(part.map((p) => p.path));
  const chunks = part.map((p) => `===== ${path.basename(p.path)} =====\n${p.text}${p.text.endsWith("\n") ? "" : "\n"}`);
  for (const p of all) if (!own.has(p.path)) chunks.push(extract(p));
  return chunks.join("\n");
}

// Index i in [1, len-1] where list is cut into list[0..i) and list[i..).
function cutIndex(list, max) {
  const total = list.reduce((s, p) => s + p.bytes, 0);
  const cands = [];
  for (let i = 1; i < list.length; i++) if (list[i].wave !== list[i - 1].wave) cands.push(i);
  // A wave alone over max may be cut inside, by filename order.
  const waveBytes = {};
  for (const p of list) waveBytes[p.wave] = (waveBytes[p.wave] || 0) + p.bytes;
  for (let i = 1; i < list.length; i++) {
    if (list[i].wave === list[i - 1].wave && waveBytes[list[i].wave] > max) cands.push(i);
  }
  // Subset that is one wave under max yet assembles over max (extracts push it over): any position.
  if (cands.length === 0) for (let i = 1; i < list.length; i++) cands.push(i);
  cands.sort((a, b) => a - b);
  let best = cands[0], bestDist = Infinity;
  for (const i of cands) {
    const running = list.slice(0, i).reduce((s, p) => s + p.bytes, 0);
    const d = Math.abs(running - total / 2);
    if (d < bestDist) { best = i; bestDist = d; } // strict < : earliest cut wins ties
  }
  return best;
}

function split(list, all, max) {
  if (list.length <= 1 || Buffer.byteLength(assemble(list, all)) <= max) return [list];
  const i = cutIndex(list, max);
  return [...split(list.slice(0, i), all, max), ...split(list.slice(i), all, max)];
}

function planBundle(paths, max = DEFAULT_MAX, { since, round } = {}) {
  const all = sortPlans(paths.map(readPlan));
  const sel = since ? focusSet(all, since, round) : {};
  const own = sel.focus || all;
  const parts = split(own, all, max).map((part) => {
    const text = assemble(part, all);
    const assembled = Buffer.byteLength(text);
    const out = {
      plans: part.map((p) => p.path),
      own_bytes: part.reduce((s, p) => s + p.bytes, 0),
      assembled_bytes: assembled,
    };
    if (assembled > max) out.oversize = true; // only possible for a single plan
    Object.defineProperty(out, "text", { value: text, enumerable: false });
    return out;
  });
  const res = { total_bytes: all.reduce((s, p) => s + p.bytes, 0), parts };
  if (sel.focus) {
    const f = new Set(sel.focus.map((p) => p.path));
    res.focus_plans = all.filter((p) => f.has(p.path)).map((p) => p.path);
    res.unchanged_plans = all.filter((p) => !f.has(p.path)).map((p) => p.path);
  } else if (sel.reason) res.full_reason = sel.reason;
  return res;
}

function main() {
  let opts;
  try { opts = parseArgs(process.argv.slice(2)); }
  catch (e) { process.stderr.write(`${e.message}\n`); process.exit(2); }

  const result = planBundle(opts.plans, opts.max, { since: opts.since, round: opts.round });
  if (opts.emit) {
    fs.mkdirSync(opts.emit, { recursive: true });
    result.parts.forEach((part, i) => {
      part.path = path.join(path.resolve(opts.emit), `part-${i + 1}.md`);
      fs.writeFileSync(part.path, part.text);
    });
  }
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
}

module.exports = { planBundle, assemble, extract, readPlan, sortPlans, cutIndex, focusSet, DEFAULT_MAX };
if (require.main === module) main();
