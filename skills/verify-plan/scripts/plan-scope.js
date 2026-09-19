#!/usr/bin/env node
// plan-scope.js — computes which plan files (verify-plan cycle N>=2) actually need re-checking,
// instead of the whole document. Scope = union of:
//   1. changed    — git diff vs the last cycle's snapshot commit
//   2. dependency — a file that declares "Depends on plan <id>" where <id> changed
//   3. overlap    — a file containing a token added/removed by a changed file's diff
//   4. flagged    — files the plan-reviser named as touched (planner-revision.md Step 7)
//   5. open       — files carrying an OPEN/UNRESOLVED challenge (caller passes these in; this
//                   script has no knowledge of iteration_state.challenges)
//
// Fails OPEN: no prior state, no snapshot_commit, a failing `git diff`, or an untracked plan
// file all return the FULL plan set as scope. Silence about "what changed" must never be read
// as "nothing changed".
"use strict";
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

function parseArgs(argv) {
  const opts = { plans: [], reviserTouch: [], openFile: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--repo-root") opts.repoRoot = argv[++i];
    else if (a === "--state") opts.state = argv[++i];
    else if (a === "--plan") opts.plans.push(path.resolve(argv[++i]));
    else if (a === "--reviser-touch") opts.reviserTouch.push(path.resolve(argv[++i]));
    else if (a === "--open-file") opts.openFile.push(path.resolve(argv[++i]));
    else throw new Error(`unknown arg: ${a}`);
  }
  if (!opts.repoRoot || !opts.state || opts.plans.length === 0) {
    throw new Error(
      "usage: plan-scope.js --repo-root <dir> --state <file> --plan <file> " +
      "[--plan <file> ...] [--reviser-touch <file> ...] [--open-file <file> ...]"
    );
  }
  return opts;
}

function readState(statePath) {
  if (!fs.existsSync(statePath)) return null;
  try { return JSON.parse(fs.readFileSync(statePath, "utf8")); }
  catch (_) { return null; }
}

function gitDiffNames(repoRoot, anchor, planPaths) {
  try {
    // Ask git per plan path and test emptiness: git resolves the pathspec itself, so there is
    // no toplevel/cwd/symlink/quoted-path mismatch to get wrong. Returns planPaths members.
    return planPaths.filter((p) =>
      execFileSync("git", ["diff", "--name-only", anchor, "--", p], {
        cwd: repoRoot, encoding: "utf8",
      }).trim() !== "");
  } catch (_) {
    return null; // signals "git diff failed" -> caller fails open
  }
}

// `git diff` is blind to untracked paths: it exits 0 with empty output, which is
// indistinguishable from "clean". This is the only way to tell the two apart.
function isTracked(repoRoot, planPath) {
  try {
    execFileSync("git", ["ls-files", "--error-unmatch", "--", planPath], {
      cwd: repoRoot, encoding: "utf8", stdio: ["ignore", "ignore", "ignore"],
    });
    return true;
  } catch (_) {
    return false;
  }
}

const TOKEN_RE = /[A-Za-z_][\w./-]{3,}/g;
// Plain English words ("baseline", "text") match TOKEN_RE too; require identifier shape
// (a dot/slash/underscore, or an internal capital = camelCase) so prose overlap can't
// pull an unrelated file into scope.
const isIdentifierShaped = (t) => /[._/]\w/.test(t) || /[a-z][A-Z]/.test(t);

function gitDiffTokens(repoRoot, anchor, changedPaths) {
  const tokens = new Set();
  for (const p of changedPaths) {
    let out;
    try {
      out = execFileSync("git", ["diff", "--unified=0", anchor, "--", p], {
        cwd: repoRoot, encoding: "utf8",
      });
    } catch (_) { continue; }
    for (const line of out.split("\n")) {
      if (!/^[+-]/.test(line) || /^(\+\+\+|---)/.test(line)) continue;
      for (const m of line.slice(1).matchAll(TOKEN_RE)) {
        if (isIdentifierShaped(m[0])) tokens.add(m[0]);
      }
    }
  }
  return tokens;
}

function planId(filePath) {
  const m = path.basename(filePath).match(/^(\d+-\d+)/);
  return m ? m[1] : null;
}

// planId -> [files that declare "Depends on plan <planId>"], read from text already in the
// plan (frontmatter @-refs, "# Depends on plan X output" comments) — never inferred.
function declaredDependents(planPaths) {
  const dependents = {};
  for (const p of planPaths) {
    let text;
    try { text = fs.readFileSync(p, "utf8"); } catch (_) { continue; }
    const ids = new Set();
    for (const m of text.matchAll(/Depends on plan ([\w-]+)/gi)) ids.add(m[1]);
    for (const m of text.matchAll(/@[.\w/-]*?(\d+-\d+)-[\w-]*\.md/g)) ids.add(m[1]);
    for (const id of ids) (dependents[id] ||= []).push(p);
  }
  return dependents;
}

function computeScope({ repoRoot, planPaths, statePath, reviserTouch = [], openFile = [] }) {
  const state = readState(statePath);
  if (!state || !state.snapshot_commit) {
    return {
      scope: planPaths.slice(), failOpen: true,
      reason: state ? "state file has no snapshot_commit" : "no prior state file", diff: null,
    };
  }
  const untracked = planPaths.filter((p) => !isTracked(repoRoot, p));
  if (untracked.length) {
    return {
      scope: planPaths.slice(), failOpen: true,
      reason: `plan file untracked by git: ${untracked.map((p) => path.basename(p)).join(", ")}`,
      diff: null,
    };
  }
  const changed = gitDiffNames(repoRoot, state.snapshot_commit, planPaths);
  if (changed === null) {
    return { scope: planPaths.slice(), failOpen: true, reason: "git diff failed against snapshot_commit", diff: null };
  }

  const scopeSet = new Set(changed);
  reviserTouch.forEach((f) => scopeSet.add(f));
  openFile.forEach((f) => scopeSet.add(f));

  const dependents = declaredDependents(planPaths);
  const touchedByDependency = [];
  for (const c of changed) {
    const id = planId(c);
    if (id && dependents[id]) {
      for (const dep of dependents[id]) {
        if (dep === c) continue;
        scopeSet.add(dep);
        touchedByDependency.push({ file: dep, depends_on: c });
      }
    }
  }

  const tokens = gitDiffTokens(repoRoot, state.snapshot_commit, changed);
  const touchedByOverlap = [];
  for (const p of planPaths) {
    if (scopeSet.has(p)) continue;
    let text;
    try { text = fs.readFileSync(p, "utf8"); } catch (_) { continue; }
    for (const t of tokens) {
      if (text.includes(t)) { scopeSet.add(p); touchedByOverlap.push({ file: p, token: t }); break; }
    }
  }

  return {
    scope: [...scopeSet], failOpen: false, reason: null,
    diff: { changed_files: changed, touched_by_overlap: touchedByOverlap, touched_by_dependency: touchedByDependency },
  };
}

// Emitted when scoping ran successfully and found nothing changed. This is NOT the same as
// `null` (fail-open, which means "could not determine what changed" -> full read).
const NO_CHANGE_BLOCK = [
  "No plan file changed since the last cycle. Audit ONLY the OPEN and UNRESOLVED challenges in",
  "your iteration_state, and the fixes claimed for them. Do NOT re-read the plan in full and do",
  "NOT re-explore the codebase.",
].join("\n");

function formatScopeBlock(result, allPlanPaths) {
  if (result.failOpen) return null;
  if (result.scope.length === 0) return NO_CHANGE_BLOCK;
  const out = allPlanPaths.filter((p) => !result.scope.includes(p));
  const lines = [`In scope this cycle (${result.scope.length}/${allPlanPaths.length} files):`];
  for (const f of result.scope) {
    const reasons = [];
    if (result.diff.changed_files.includes(f)) reasons.push("changed");
    const dep = result.diff.touched_by_dependency.find((d) => d.file === f);
    if (dep) reasons.push(`depends on changed file ${path.basename(dep.depends_on)}`);
    const ov = result.diff.touched_by_overlap.find((o) => o.file === f);
    if (ov) reasons.push(`shares token "${ov.token}" with a changed file`);
    lines.push(`- ${f}${reasons.length ? " (" + reasons.join("; ") + ")" : ""}`);
  }
  if (out.length) {
    lines.push(`Out of scope, already verified, do NOT re-read or re-flag: ${out.map((f) => path.basename(f)).join(", ")}`);
  }
  return lines.join("\n");
}

function main() {
  let opts;
  try { opts = parseArgs(process.argv.slice(2)); }
  catch (e) { process.stderr.write(`${e.message}\n`); process.exit(2); }

  const result = computeScope({
    repoRoot: opts.repoRoot, planPaths: opts.plans, statePath: opts.state,
    reviserTouch: opts.reviserTouch, openFile: opts.openFile,
  });
  const scopeBlock = formatScopeBlock(result, opts.plans);
  process.stdout.write(JSON.stringify(
    { scope: result.scope, fail_open: result.failOpen, reason: result.reason, diff: result.diff, scope_block: scopeBlock },
    null, 2
  ) + "\n");
}

module.exports = { computeScope, formatScopeBlock, declaredDependents, planId, NO_CHANGE_BLOCK };
if (require.main === module) main();
