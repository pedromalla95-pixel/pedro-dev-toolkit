#!/usr/bin/env node
// test-plan-scope.js — self-check for plan-scope.js. No framework: run with `node test-plan-scope.js`.
// Builds a throwaway git repo under os.tmpdir(), commits a baseline, edits one file, and asserts
// the computed scope includes the changed file + its declared dependent + its token-overlap
// partner, and EXCLUDES an unrelated control file. Also asserts the fail-open path.
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const assert = require("assert");
const { execFileSync } = require("child_process");
const { computeScope, formatScopeBlock, NO_CHANGE_BLOCK } = require("./plan-scope.js");

function sh(cmd, args, cwd) { execFileSync(cmd, args, { cwd, encoding: "utf8" }); }

function main() {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "plan-scope-test-"));
  sh("git", ["init", "-q"], repo);
  sh("git", ["config", "user.email", "t@t.t"], repo);
  sh("git", ["config", "user.name", "t"], repo);

  const p01 = path.join(repo, "01-01-PLAN.md");
  const p02 = path.join(repo, "01-02-PLAN.md");
  const p03 = path.join(repo, "01-03-PLAN.md");
  const p04 = path.join(repo, "01-04-PLAN.md");
  fs.writeFileSync(p01, "# Plan 01-01\nbaseline text.\n");
  fs.writeFileSync(p02, "# Plan 01-02\n# Depends on plan 01-01 output\nbaseline text.\n");
  // p03 includes sharedHelper() in baseline so overlap detection tests the right path
  fs.writeFileSync(p03, "# Plan 01-03\nbaseline text includes sharedHelper() reference.\n");
  fs.writeFileSync(p04, "# Plan 01-04\nunrelated control, must stay out of scope.\n");
  sh("git", ["add", "-A"], repo);
  sh("git", ["commit", "-q", "-m", "cycle1"], repo);
  const cycle1Sha = execFileSync("git", ["rev-parse", "HEAD"], { cwd: repo, encoding: "utf8" }).trim();

  const statePath = path.join(repo, "cross-cycle-state.json");
  fs.writeFileSync(statePath, JSON.stringify({ cycle: 1, snapshot_commit: cycle1Sha }));

  // Edit only p01: introduce a token that p03 already contains.
  fs.writeFileSync(p01, "# Plan 01-01\nnow calls sharedHelper() during setup.\n");

  const planPaths = [p01, p02, p03, p04];
  const result = computeScope({ repoRoot: repo, planPaths, statePath, reviserTouch: [], openFile: [] });

  assert.strictEqual(result.failOpen, false, "should not fail open when snapshot_commit is valid");
  assert.ok(result.scope.includes(p01), "changed file must be in scope");
  assert.ok(result.scope.includes(p02), "01-02 declares a dependency on 01-01 -> must be in scope");
  assert.ok(result.scope.includes(p03), "01-03 shares the sharedHelper() token -> must be in scope");
  assert.ok(!result.scope.includes(p04), "01-04 is unrelated -> must stay OUT of scope");
  assert.ok(result.diff.touched_by_overlap.length > 0, "overlap detection must find p03 via shared token");

  const noStateResult = computeScope({ repoRoot: repo, planPaths, statePath: path.join(repo, "missing.json") });
  assert.strictEqual(noStateResult.failOpen, true, "missing state file must fail open");
  assert.deepStrictEqual(new Set(noStateResult.scope), new Set(planPaths), "fail-open scope must be the full plan set");
  assert.strictEqual(
    formatScopeBlock(noStateResult, planPaths), null,
    "fail-open must format to null so every role does a full read"
  );

  // Zero-scope cycle: commit the prior edit as this cycle's snapshot, then compute scope again
  // with nothing changed since it -> scope must be genuinely empty, and formatScopeBlock must
  // return the no-change block. This is the NORMAL path for iterations 2-3: the plan is not
  // revised between iterations, so "tracked and unchanged" is what scoping usually finds.
  sh("git", ["add", "-A"], repo);
  sh("git", ["commit", "-q", "-m", "cycle2"], repo);
  const cycle2Sha = execFileSync("git", ["rev-parse", "HEAD"], { cwd: repo, encoding: "utf8" }).trim();
  fs.writeFileSync(statePath, JSON.stringify({ cycle: 2, snapshot_commit: cycle2Sha }));
  const zeroResult = computeScope({ repoRoot: repo, planPaths, statePath });
  assert.strictEqual(zeroResult.failOpen, false, "zero-change cycle must not fail open");
  assert.deepStrictEqual(zeroResult.scope, [], "no changes since snapshot -> scope must be empty");
  assert.strictEqual(
    formatScopeBlock(zeroResult, planPaths), NO_CHANGE_BLOCK,
    "tracked + genuinely unchanged must emit the no-change block, not null"
  );

  // Untracked plan file: `git diff --name-only <sha> -- <untracked>` exits 0 with EMPTY output,
  // so the diff is indistinguishable from "clean". An untracked plan may have been rewritten end
  // to end, so scoping must fail OPEN rather than report zero changes.
  const p05 = path.join(repo, "01-05-PLAN.md");
  fs.writeFileSync(p05, "# Plan 01-05\nnever added to git.\n");
  const untrackedPaths = [p01, p02, p03, p04, p05];
  const untrackedResult = computeScope({ repoRoot: repo, planPaths: untrackedPaths, statePath });
  assert.strictEqual(untrackedResult.failOpen, true, "untracked plan file must fail open");
  assert.ok(
    /untracked/.test(untrackedResult.reason || ""),
    "fail-open reason must name the untracked plan file"
  );
  assert.deepStrictEqual(
    new Set(untrackedResult.scope), new Set(untrackedPaths),
    "untracked fail-open scope must be the full plan set"
  );
  assert.strictEqual(
    formatScopeBlock(untrackedResult, untrackedPaths), null,
    "untracked plan must format to null (full read), never a narrowed block"
  );

  // Subdirectory repoRoot: `git diff --name-only` prints paths relative to the git
  // toplevel, not to cwd. If the resolved diff paths are matched against repoRoot instead
  // of the toplevel, a genuinely changed plan silently falls out of scope and looks like
  // no change at all -- this pins that the fix resolves against the toplevel.
  fs.mkdirSync(path.join(repo, "sub"));
  const subRoot = path.join(repo, "sub");
  fs.writeFileSync(p01, "# Plan 01-01\nchanged again after the cycle2 snapshot.\n");
  const subdirResult = computeScope({ repoRoot: subRoot, planPaths, statePath });
  assert.strictEqual(subdirResult.failOpen, false, "genuine change from a subdirectory repoRoot must not fail open");
  assert.ok(
    subdirResult.scope.includes(p01),
    "changed plan must be in scope even when repoRoot is a subdirectory of the toplevel"
  );
  assert.notStrictEqual(
    formatScopeBlock(subdirResult, planPaths), NO_CHANGE_BLOCK,
    "a real change must never be reported as no-change"
  );
  // An over-broad scope is also a failure: dependency + overlap must not pull in more than
  // p01 (changed), p02 (declares dependency on 01-01), and p03 (shares token "sharedHelper").
  assert.strictEqual(
    subdirResult.scope.length, 3,
    "subdirectory scope must be exactly the changed file plus its dependent and overlap partner, not over-broad"
  );

  // Symlinked repoRoot: `git rev-parse --show-toplevel` resolves symlinks while
  // `path.resolve` does not, so any code that compares the two will mismatch.
  const repoLink = path.join(os.tmpdir(), `plan-scope-test-link-${process.pid}`);
  fs.symlinkSync(repo, repoLink);
  const linkedPlanPaths = planPaths.map((p) => path.join(repoLink, path.basename(p)));
  const symlinkResult = computeScope({ repoRoot: repoLink, planPaths: linkedPlanPaths, statePath });
  assert.strictEqual(symlinkResult.failOpen, false, "symlinked repoRoot must not fail open");
  assert.ok(
    symlinkResult.scope.includes(linkedPlanPaths[0]),
    "changed plan must be in scope even when repoRoot is reached through a symlink"
  );
  assert.notStrictEqual(
    formatScopeBlock(symlinkResult, linkedPlanPaths), NO_CHANGE_BLOCK,
    "a real change through a symlinked repoRoot must never be reported as no-change"
  );
  fs.rmSync(repoLink, { force: true });

  // Non-ASCII plan filename: git quotes and octal-escapes non-ASCII paths in
  // --name-only output by default (core.quotePath), so a naive string comparison
  // against the raw name would never match.
  const pCafe = path.join(repo, "01-06-café.md");
  fs.writeFileSync(pCafe, "# Plan 01-06\nbaseline.\n");
  sh("git", ["add", "-A"], repo);
  sh("git", ["commit", "-q", "-m", "add cafe plan"], repo);
  const cafeSha = execFileSync("git", ["rev-parse", "HEAD"], { cwd: repo, encoding: "utf8" }).trim();
  fs.writeFileSync(statePath, JSON.stringify({ cycle: 3, snapshot_commit: cafeSha }));
  fs.writeFileSync(pCafe, "# Plan 01-06\nchanged after the cafe snapshot.\n");
  const cafePaths = [pCafe];
  const cafeResult = computeScope({ repoRoot: repo, planPaths: cafePaths, statePath });
  assert.strictEqual(cafeResult.failOpen, false, "non-ASCII plan filename must not fail open");
  assert.ok(
    cafeResult.scope.includes(pCafe),
    "changed non-ASCII plan filename must be in scope"
  );
  assert.notStrictEqual(
    formatScopeBlock(cafeResult, cafePaths), NO_CHANGE_BLOCK,
    "a real change to a non-ASCII plan filename must never be reported as no-change"
  );

  fs.rmSync(repo, { recursive: true, force: true });
  console.log("plan-scope.js: all assertions passed");
}

main();
