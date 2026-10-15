#!/usr/bin/env node
// test-verdict.js — self-check for verdict.js. No framework: run with `node test-verdict.js`.
// One line per case; exits non-zero on the first failure. STOP_VERIFYING and the unmitigated
// ESCALATE are exercised through decideCap directly: under the literal verdict rules the state
// they need (round >= 2, 0 BLOCKING, 0 lint BLOCK, verdict REVISE) is only reachable via a money
// SIGNIFICANT, which routes to the money-path ESCALATE instead. money_path comes from each plan's
// frontmatter (or a lint money_path finding), so money cases write a real plan file.
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const assert = require("assert");
const { execFileSync } = require("child_process");
const { computeVerdict, decideCap } = require("./verdict.js");
const { hashPlanBody } = require(path.join(__dirname, "../../../hooks/lib/plan-hash.js"));

const F = (id, extra = {}) => ({
  id, plan: "02-06", line: 10, title: `t ${id}`, evidence: "e", impact: "i", fix: "edit line 10",
  source: "R-A", premise_tag: null, money_path: false, mitigation: null, ...extra,
});
const synth = (blocking = [], significant = [], rethink = false) => ({ blocking, significant, minor: [], rethink, reason: "r" });
const meta = (extra = {}) => ({
  round: 1, plan_paths: ["/p/02-06-PLAN.md"], agents_spawned: 6,
  reviewers_ok: ["R-A", "R-B", "R-C", "R-D", "S"], reviewers_failed: [], ...extra,
});
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "verdict-test-"));
// A plan file on disk with the given frontmatter money flag; returns meta fields pointing at it.
const moneyPlan = (flag = "true") => {
  const p = path.join(TMP, `money-${flag}-PLAN.md`);
  fs.writeFileSync(p, `---\nphase: 04\nmoney_path: ${flag}\n---\n# Plan\n`);
  return { plan_paths: [p] };
};
const lint = (block = 0, warn = 0) => ({
  schema: "gsd-plan-lint/1",
  findings: Array.from({ length: block }, (_, i) => ({ check: `L${i}`, name: `rule${i}`, severity: "BLOCK", plan: "02-06", line: i, detail: "d" })),
  counts: { block, warn, info: 0 },
});
// Round-1 verdict to use as `prior`.
const r1 = (s, l = null, m = {}) => computeVerdict({ lint: l, synth: s, meta: meta(m), prior: null });
const r2 = (s, prior, l = null, m = {}) => computeVerdict({ lint: l, synth: s, meta: meta({ round: 2, ...m }), prior });

let n = 0;
function test(name, fn) { fn(); n++; process.stdout.write(`ok ${n} - ${name}\n`); }

function main() {
  test("ERROR on failed mandatory reviewer, never PROCEED", () => {
    const v = computeVerdict({ lint: null, synth: synth(), meta: meta({ reviewers_failed: ["R-B"] }), prior: null });
    assert.strictEqual(v.verdict, "ERROR");
    assert.strictEqual(v.cap_decision, null);
    const p = computeVerdict({ lint: null, synth: synth(), meta: meta({ reviewers_failed: ["P"] }), prior: null });
    assert.strictEqual(p.verdict, "PROCEED", "failed prober alone is not ERROR");
  });

  test("ERROR when depth reviewer R-D is missing (mandatory)", () => {
    const v = computeVerdict({ lint: null, synth: synth(), meta: meta({ reviewers_ok: ["R-A", "R-B", "R-C", "S"], reviewers_failed: ["R-D"] }), prior: null });
    assert.strictEqual(v.verdict, "ERROR");
  });

  test("lint BLOCK -> REVISE with synth absent; lint ids in blocking_ids", () => {
    const v = r1(null, lint(2));
    assert.strictEqual(v.verdict, "REVISE");
    assert.deepStrictEqual(v.blocking_ids, ["lint:rule0:02-06", "lint:rule1:02-06"]);
    assert.deepStrictEqual(v.lint, { block: 2, warn: 0 });
  });

  test("BLOCKING + rethink -> RETHINK; plain BLOCKING -> REVISE", () => {
    assert.strictEqual(r1(synth([F("a")], [], true)).verdict, "RETHINK");
    assert.strictEqual(r1(synth([F("a")])).verdict, "REVISE");
  });

  test("money SIGNIFICANT blocks PROCEED on money path", () => {
    const s = synth([], [F("m", { money_path: true, mitigation: "x" })]);
    assert.strictEqual(r1(s, null, moneyPlan()).verdict, "REVISE");
    assert.strictEqual(r1(s).verdict, "PROCEED", "non-money plan set: carried as residual");
    assert.strictEqual(r1(s, null, moneyPlan("false")).verdict, "PROCEED");
  });

  test("regression 1258-r2: plan frontmatter money_path + lint clean + money SIG -> not PROCEED, meta flag ignored", () => {
    const s = synth([], [F("m", { money_path: true, mitigation: "x" })]);
    const v = r1(s, lint(0, 3), { ...moneyPlan(), money_path: false });
    assert.strictEqual(v.verdict, "REVISE");
    assert.strictEqual(v.money_path, true);
    assert.strictEqual(r1(s, null, { money_path: true }).verdict, "PROCEED", "hand-written meta money_path is not read");
  });

  test("lint money_path finding sets money path without a frontmatter flag", () => {
    const l = { schema: "gsd-plan-lint/1", counts: { block: 0, warn: 1, info: 0 },
      findings: [{ check: "money_path", name: "MONEY_PROSE", severity: "WARN", plan: "02-06", line: 1, detail: "d" }] };
    assert.strictEqual(r1(synth([], [F("m", { money_path: true })]), l).verdict, "REVISE");
  });

  test("PROCEED carries every SIGNIFICANT as residual_risks", () => {
    const v = r1(synth([], [F("s1", { mitigation: "monitor" }), F("s2")]));
    assert.strictEqual(v.verdict, "PROCEED");
    assert.strictEqual(v.cap_decision, null);
    assert.deepStrictEqual(v.residual_risks, [
      { id: "s1", title: "t s1", mitigation: "monitor" }, { id: "s2", title: "t s2", mitigation: null },
    ]);
  });

  test("reopened at round 3 -> HALT, reason kept", () => {
    const p1 = r1(synth([F("a"), F("b"), F("c")]));
    const p2 = r2(synth([F("b")]), p1); // a, c closed
    assert.deepStrictEqual(p2.closed_ids.sort(), ["a", "c"]);
    const p3 = computeVerdict({ lint: null, synth: synth([F("a", { fix: "" })]), meta: meta({ round: 3 }), prior: p2 });
    assert.strictEqual(p3.cap_decision, "HALT", "round 3 halts; the REDESIGN reason is kept in cap_reason");
    assert.ok(p3.cap_reason.includes("reopened"), p3.cap_reason);
  });

  test("cap REDESIGN via new >= closed", () => {
    const v = r2(synth([F("b"), F("c")]), r1(synth([F("a"), F("b")])));
    assert.strictEqual(v.cap_decision, "REDESIGN");
    assert.ok(v.cap_reason.includes("new BLOCKING (1) >= closed BLOCKING (1)"), v.cap_reason);
  });

  test("closed lint BLOCK counts as closed: one late catch after a lint fix is another round, not REDESIGN", () => {
    const prior = r1(synth([F("a")]), lint(1));
    const v = r2(synth([F("b")]), prior);
    assert.strictEqual(v.cap_decision, null, v.cap_reason);
  });

  test("severity promotion (prior SIGNIFICANT -> BLOCKING, same id) is not new", () => {
    const prior = r1(synth([F("a")], [F("x")]));
    const v = r2(synth([F("x")]), prior); // a closed, x promoted
    assert.notStrictEqual(v.cap_decision, "REDESIGN", v.cap_reason);
    assert.strictEqual(v.cap_decision, null);
  });

  test("cap REDESIGN via shared premise", () => {
    const prior = r1(synth([F("a"), F("b"), F("c"), F("d")]));
    const v = r2(synth([F("a", { premise_tag: "p1" }), F("b", { premise_tag: "p1" })]), prior);
    assert.strictEqual(v.cap_decision, "REDESIGN");
    assert.ok(v.cap_reason.includes('share premise "p1"'), v.cap_reason);
  });

  test("cap REDESIGN via lint BLOCK count not falling", () => {
    const v = r2(null, r1(null, lint(2)), lint(2));
    assert.strictEqual(v.cap_decision, "REDESIGN");
    assert.ok(v.cap_reason.includes("lint BLOCK count did not fall"), v.cap_reason);
  });

  test("RETHINK at round >= 2 stays RETHINK, never REDESIGN (REDESIGN auto-replans)", () => {
    const v = r2(synth([F("a")], [], true), r1(synth([F("a"), F("b"), F("c")])));
    assert.strictEqual(v.verdict, "RETHINK");
    assert.strictEqual(v.cap_decision, null);
    const w = r2(synth([F("b"), F("c")], [], true), r1(synth([F("a"), F("b")]))); // new >= closed too
    assert.strictEqual(w.verdict, "RETHINK");
    assert.notStrictEqual(w.cap_decision, "REDESIGN", w.cap_reason);
  });

  test("cap STOP_VERIFYING (decideCap direct) + unmitigated -> ESCALATE", () => {
    const base = { round: 2, verdict: "REVISE", blocking: [], blockingIds: [], lintBlock: 0, prior: null, moneyPath: false, rethink: false };
    const stop = decideCap({ ...base, significant: [F("s", { mitigation: "feature flag" })] });
    assert.strictEqual(stop.cap_decision, "STOP_VERIFYING");
    assert.ok(stop.cap_reason);
    assert.strictEqual(decideCap({ ...base, significant: [F("s", { mitigation: " " })] }).cap_decision, "ESCALATE");
  });

  test("cap ESCALATE on money path (end-to-end)", () => {
    const prior = r1(synth([F("a")], [], false), null, moneyPlan());
    const v = r2(synth([], [F("m", { money_path: true, mitigation: "x" })]), prior, null, moneyPlan());
    assert.strictEqual(v.verdict, "REVISE");
    assert.strictEqual(v.cap_decision, "ESCALATE");
    assert.deepStrictEqual(v.residual_risks, []);
  });

  test("round 2 with progress and no REDESIGN reason: cap null, another round (no CONTINUE gate)", () => {
    const v = r2(synth([F("a", { fix: "" })]), r1(synth([F("a"), F("b"), F("c")])));
    assert.strictEqual(v.cap_decision, null);
    const l = r2(null, r1(null, lint(3)), lint(1));
    assert.strictEqual(l.verdict, "REVISE");
    assert.strictEqual(l.cap_decision, null);
  });

  test("round 3 not PROCEED / STOP_VERIFYING -> HALT", () => {
    const p1 = r1(synth([F("a"), F("b"), F("c")]));
    const p2 = r2(synth([F("a"), F("b")]), p1);
    assert.strictEqual(p2.cap_decision, null);
    const p3 = computeVerdict({ lint: null, synth: synth([F("a")]), meta: meta({ round: 3 }), prior: p2 });
    assert.strictEqual(p3.verdict, "REVISE");
    assert.strictEqual(p3.cap_decision, "HALT");
    const rt = computeVerdict({ lint: null, synth: synth([F("a")], [], true), meta: meta({ round: 3 }), prior: p2 });
    assert.strictEqual(rt.verdict, "RETHINK");
    assert.strictEqual(rt.cap_decision, "HALT");
  });

  test("round 3: new non-money SIGNIFICANT -> residual_risks; money SIG still ESCALATE", () => {
    const m = moneyPlan();
    const p2 = r2(synth([F("a")], [F("m", { money_path: true, mitigation: "x" }), F("old")]), r1(synth([F("a"), F("b")]), null, m), null, m);
    const v = computeVerdict({ lint: null, meta: meta({ round: 3, ...m }), prior: p2,
      synth: synth([], [F("m", { money_path: true, mitigation: "x" }), F("old"), F("late", { mitigation: "monitor" })]) });
    assert.strictEqual(v.verdict, "REVISE");
    assert.strictEqual(v.cap_decision, "ESCALATE");
    assert.deepStrictEqual(v.new_significant_ids, ["late"]);
    assert.deepStrictEqual(v.residual_risks, [{ id: "late", title: "t late", mitigation: "monitor" }]);
    const h = computeVerdict({ lint: null, meta: meta({ round: 3 }), prior: p2, synth: synth([F("a")], [F("late")]) });
    assert.strictEqual(h.cap_decision, "HALT");
    assert.deepStrictEqual(h.residual_risks.map((r) => r.id), ["late"]);
  });

  test("closed_ids accumulate across rounds (incl. lint ids)", () => {
    const p1 = r1(synth([F("a"), F("b")]), lint(1));
    assert.deepStrictEqual(p1.closed_ids, []);
    const p2 = r2(synth([F("b")]), p1);
    assert.deepStrictEqual(p2.closed_ids.sort(), ["a", "lint:rule0:02-06"]);
    const p3 = computeVerdict({ lint: null, synth: synth([]), meta: meta({ round: 3 }), prior: p2 });
    assert.deepStrictEqual(p3.closed_ids.sort(), ["a", "b", "lint:rule0:02-06"]);
  });

  test("focused round 2: open finding on an unchanged plan, re-emitted, stays open", () => {
    const p1 = r1(synth([F("04-03#x", { plan: "04-03" }), F("04-05#y", { plan: "04-05" })]));
    // 04-05 changed and fixed y; 04-03 was unchanged context, so reviewers re-emit x under its id.
    const p2 = r2(synth([F("04-03#x", { plan: "04-03" })]), p1);
    assert.deepStrictEqual(p2.blocking_ids, ["04-03#x"]);
    assert.deepStrictEqual(p2.closed_ids, ["04-05#y"], "only the fixed id closes");
    assert.strictEqual(p2.verdict, "REVISE");
  });

  test("CLI round-trip writes verdict.json with plan hashes", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "verdict-test-"));
    const plan = path.join(dir, "02-06-PLAN.md");
    const text = "---\nstatus: draft\n---\n# Plan\nbody\n";
    fs.writeFileSync(plan, text);
    const missing = path.join(dir, "missing.md");
    const w = (name, obj) => { const p = path.join(dir, name); fs.writeFileSync(p, JSON.stringify(obj)); return p; };
    const out = path.join(dir, "r.verdict.json");
    const stdout = execFileSync(process.execPath, [
      path.join(__dirname, "verdict.js"), "--lint", "-",
      "--synth", w("synth.json", synth([], [F("s", { mitigation: "m" })])),
      "--meta", w("meta.json", meta({ plan_paths: [plan, missing] })), "--out", out,
    ], { encoding: "utf8" });
    assert.strictEqual(stdout.trim(), "PROCEED");
    const v = JSON.parse(fs.readFileSync(out, "utf8"));
    assert.strictEqual(v.verdict, "PROCEED");
    assert.strictEqual(v.plan_path, plan);
    assert.strictEqual(v.plan_hashes[plan], hashPlanBody(text));
    assert.strictEqual(v.plan_hashes[missing], null);
    assert.deepStrictEqual(v.lint, { block: 0, warn: 0 });
    fs.rmSync(dir, { recursive: true, force: true });
  });

  fs.rmSync(TMP, { recursive: true, force: true });
  process.stdout.write(`all ${n} passed\n`);
}

try { main(); }
catch (e) { process.stderr.write(`FAIL: ${e.message}\n`); process.exit(1); }
