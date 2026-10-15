#!/usr/bin/env node
// test-bundle.js — self-check for bundle.js. No framework: run with `node test-bundle.js`.
// Real D6 bundle (≈245 KB, 6 plans) must split in 2 assembled parts ≤ 200 KB; synthetic temp
// bundles cover the 1-part case, recursive re-split, a single oversize plan, and wave ordering.
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const assert = require("assert");
const { execFileSync } = require("child_process");
const { planBundle } = require("./bundle.js");

const D6 = "/home/pedro/.claude/skills/verify-plan/research/2026-09-26-replay/dev/D6-bundle-P2@d3cc8ae";
const base = (p) => path.basename(p);
const ok = (msg) => console.log(`ok - ${msg}`);
const count = (s, sub) => s.split(sub).length - 1;

// Temp plan: frontmatter with wave (or none), one task, padded body to ~kb KB.
function mkPlan(dir, name, wave, kb) {
  const fm = wave == null ? "---\nphase: 9\n---\n" : `---\nphase: 9\nwave: ${wave}\n---\n`;
  const body = `<tasks>\n<task type="auto">\n  <name>Task 1: build ${name}</name>\n</task>\n</tasks>\n`;
  const p = path.join(dir, name);
  fs.writeFileSync(p, fm + body + "x".repeat(kb * 1000) + "\n");
  return p;
}

function main() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "bundle-test-"));

  // 1. Real D6 bundle.
  const d6 = fs.readdirSync(D6).filter((f) => f.endsWith("-PLAN.md")).map((f) => path.join(D6, f));
  const r = planBundle(d6, 200000);
  assert.strictEqual(d6.length, 6, "D6 has 6 plans");
  assert.strictEqual(r.parts.length, 2, "D6 splits into exactly 2 parts");
  assert.deepStrictEqual(r.parts.map((p) => p.plans.map(base)), [
    ["02-02-PLAN.md", "02-03-PLAN.md", "02-04-PLAN.md"],
    ["02-05-PLAN.md", "02-06-PLAN.md", "02-07-PLAN.md"],
  ], "D6 cut lands after 02-04 (running size nearest half)");
  for (const [i, part] of r.parts.entries()) {
    assert.ok(part.assembled_bytes <= 200000, `part ${i + 1} assembled ≤ 200000`);
    assert.strictEqual(part.assembled_bytes, Buffer.byteLength(part.text), "assembled_bytes measures the text");
    const other = r.parts[1 - i].plans;
    for (const p of part.plans) assert.strictEqual(count(part.text, `===== ${base(p)} =====`), 1, `${base(p)} full text in part ${i + 1}`);
    for (const p of other) {
      assert.strictEqual(count(part.text, `===== ${base(p)} =====`), 0, `${base(p)} not full text in part ${i + 1}`);
      assert.strictEqual(count(part.text, `===== ${base(p)} (interface extract) =====`), 1, `${base(p)} extract in part ${i + 1}`);
    }
  }
  const everyPlan = r.parts.flatMap((p) => p.plans).sort();
  assert.deepStrictEqual(everyPlan, d6.slice().sort(), "every D6 plan in exactly one part");
  assert.ok(/Task 1:/.test(r.parts[0].text.split("02-05-PLAN.md (interface extract)")[1]), "extract lists task names");
  ok(`D6 splits into 2 parts: ${r.parts.map((p) => `${p.assembled_bytes}B (own ${p.own_bytes})`).join(", ")}; total ${r.total_bytes}`);

  // 2. Small bundle under max -> 1 part.
  const sDir = fs.mkdtempSync(path.join(tmp, "small-"));
  const small = [mkPlan(sDir, "01-01-PLAN.md", 1, 1), mkPlan(sDir, "01-02-PLAN.md", 2, 1)];
  const rs = planBundle(small, 200000);
  assert.strictEqual(rs.parts.length, 1, "small bundle -> 1 part");
  assert.deepStrictEqual(rs.parts[0].plans, small, "1 part holds all plans");
  assert.ok(!rs.parts[0].text.includes("interface extract"), "1 part has no extracts");
  ok("small bundle under max -> 1 part");

  // 3. Recursion: 4 plans x 10 KB, max 18 KB. First cut -> 2x(20 KB own + 2 extracts) > 18 KB -> split again.
  const rDir = fs.mkdtempSync(path.join(tmp, "rec-"));
  const rec = [1, 2, 3, 4].map((w) => mkPlan(rDir, `03-0${w}-PLAN.md`, w, 10));
  const rr = planBundle(rec, 18000);
  assert.strictEqual(rr.parts.length, 4, "recursion splits to 4 parts");
  assert.ok(rr.parts.every((p) => p.assembled_bytes <= 18000 && !p.oversize), "all recursive parts ≤ max");
  // Guard the fixture: a first-level half really is over max.
  assert.ok(Buffer.byteLength(require("./bundle.js").assemble(rec.slice(0, 2), rec)) > 18000, "fixture: half is over max");
  ok(`recursion: 4 parts, sizes ${rr.parts.map((p) => p.assembled_bytes).join(", ")}`);

  // 4. Single plan over max -> own part, flagged, no loop.
  const oDir = fs.mkdtempSync(path.join(tmp, "over-"));
  const over = [mkPlan(oDir, "04-01-PLAN.md", 1, 5), mkPlan(oDir, "04-02-PLAN.md", 1, 50), mkPlan(oDir, "04-03-PLAN.md", 1, 5)];
  const ro = planBundle(over, 20000);
  const big = ro.parts.find((p) => p.plans.includes(over[1]));
  assert.deepStrictEqual(big.plans, [over[1]], "oversize plan alone in its part");
  assert.strictEqual(big.oversize, true, "oversize part flagged");
  assert.ok(ro.parts.filter((p) => p !== big).every((p) => p.assembled_bytes <= 20000), "other parts ≤ max");
  ok(`single plan over max -> own part (${big.assembled_bytes}B, oversize), ${ro.parts.length} parts`);

  // 5. Wave ordering: filename order disagrees with wave; numeric wave (10 after 2); missing wave = 1.
  const wDir = fs.mkdtempSync(path.join(tmp, "wave-"));
  const a = mkPlan(wDir, "a-PLAN.md", 10, 10);
  const b = mkPlan(wDir, "b-PLAN.md", 2, 10);
  const c = mkPlan(wDir, "c-PLAN.md", null, 10);
  const d = mkPlan(wDir, "d-PLAN.md", 1, 10);
  const rw = planBundle([a, b, c, d], 25000);
  assert.deepStrictEqual(rw.parts.flatMap((p) => p.plans).map(base), ["c-PLAN.md", "d-PLAN.md", "b-PLAN.md", "a-PLAN.md"],
    "order is wave (missing=1, numeric) then filename");
  ok("wave ordering respected: wave 1 (missing, explicit) < wave 2 < wave 10");

  // 6. CLI --emit writes part files and adds path.
  const eDir = path.join(tmp, "emit");
  const out = JSON.parse(execFileSync("node", [path.join(__dirname, "bundle.js"), "--plans", JSON.stringify(d6), "--emit", eDir], { encoding: "utf8" }));
  assert.strictEqual(out.parts.length, 2, "CLI: 2 parts");
  out.parts.forEach((p, i) => {
    assert.strictEqual(p.path, path.join(eDir, `part-${i + 1}.md`), "CLI: part path");
    assert.strictEqual(fs.statSync(p.path).size, p.assembled_bytes, "CLI: emitted size = assembled_bytes");
  });
  ok("CLI --emit writes part-N.md matching assembled_bytes");

  // 7. --since <prior verdict.json>: focus = changed plans + one-hop dependents; others extract.
  const fDir = fs.mkdtempSync(path.join(tmp, "since-"));
  const fp = (name, fm) => {
    const p = path.join(fDir, name);
    fs.writeFileSync(p, `---\n${fm}\n---\n<tasks>\n<task type="auto">\n  <name>Task 1: ${name}</name>\n</task>\n</tasks>\nbody v1\n`);
    return p;
  };
  const A = fp("09-01-PLAN.md", "wave: 1\ndepends_on: []\nfiles_modified:\n  - src/a.ts\nmust_haves:\n  artifacts:\n    - path: \"src/a-api.ts\"\n      provides: \"x: y\"\n  key_links: []");
  const B = fp("09-02-PLAN.md", "wave: 2\ndepends_on: [09-01]\nfiles_modified: [src/b.ts]");
  const C = fp("09-03-PLAN.md", "wave: 2\ndepends_on: []\nfiles_modified:\n  - src/c.ts\nnote: \"path: src/a-api.ts is only a mention\"");
  const D = fp("09-04-PLAN.md", "wave: 3\ndepends_on:\n  - 09-03\nfiles_modified:\n  - src/a.ts");
  const E = fp("09-05-PLAN.md", "wave: 3\ndepends_on: []\nfiles_modified: [src/e.ts]\nmust_haves:\n  key_links:\n    - from: \"src/e.ts\"\n      to: \"src/a-api.ts\"\n      via: \"import\"");
  const plans = [A, B, C, D, E];
  const { hashPlans } = require("./verdict.js");
  const vPath = path.join(fDir, "r1.verdict.json");
  fs.writeFileSync(vPath, JSON.stringify({ round: 1, plan_hashes: hashPlans(plans) }));
  const full = planBundle(plans, 200000);
  // No edit yet: empty focus -> full bundle, with the reason.
  const r0 = planBundle(plans, 200000, { since: vPath, round: 2 });
  assert.strictEqual(r0.parts[0].assembled_bytes, full.parts[0].assembled_bytes, "no change -> full");
  assert.ok(/no plan changed/.test(r0.full_reason), "no change -> full_reason");
  fs.appendFileSync(A, "body v2\n");
  const rf = planBundle(plans, 200000, { since: vPath, round: 2 });
  assert.deepStrictEqual(rf.focus_plans.map(base), ["09-01-PLAN.md", "09-02-PLAN.md", "09-04-PLAN.md", "09-05-PLAN.md"],
    "focus = changed + reverse depends_on + shared files_modified + key_links to its artifacts");
  assert.deepStrictEqual(rf.unchanged_plans.map(base), ["09-03-PLAN.md"], "unrelated plan unchanged");
  const ft = rf.parts.map((p) => p.text).join("\n");
  assert.strictEqual(count(ft, "===== 09-01-PLAN.md =====\n"), 1, "changed plan -> full text");
  assert.strictEqual(count(ft, "===== 09-02-PLAN.md =====\n"), 1, "dependent of changed -> full text");
  assert.strictEqual(count(ft, "===== 09-03-PLAN.md (interface extract) ====="), 1, "unchanged plan -> extract");
  assert.strictEqual(count(ft, "===== 09-03-PLAN.md =====\n"), 0, "unchanged plan not in full");
  assert.ok(rf.parts[0].assembled_bytes < full.parts[0].assembled_bytes, "focused bundle is smaller");
  // Safety: missing verdict / hash-less verdict / round 1 or 3 -> full, never focus.
  const fullCase = (opts, why) => {
    const r = planBundle(plans, 200000, opts);
    assert.strictEqual(r.focus_plans, undefined, `${why}: no focus_plans`);
    assert.strictEqual(r.parts[0].assembled_bytes, Buffer.byteLength(require("./bundle.js").assemble(plans, plans)), `${why}: full`);
    return r;
  };
  assert.ok(fullCase({ since: path.join(fDir, "missing.json"), round: 2 }, "missing verdict").full_reason);
  const hl = path.join(fDir, "hashless.json");
  fs.writeFileSync(hl, JSON.stringify({ round: 1, plan_hashes: {} }));
  fullCase({ since: hl, round: 2 }, "hash-less verdict");
  fs.writeFileSync(hl, "{not json");
  fullCase({ since: hl, round: 2 }, "unreadable verdict");
  fullCase({ since: vPath, round: 1 }, "round 1");
  fullCase({ since: vPath, round: 3 }, "round 3");
  fullCase({}, "no --since");
  const cli = JSON.parse(execFileSync("node", [path.join(__dirname, "bundle.js"), "--plans", JSON.stringify(plans), "--since", vPath, "--round", "2"], { encoding: "utf8" }));
  assert.strictEqual(cli.focus_plans.length, 4, "CLI --since --round 2 focuses");
  ok(`--since: focus ${rf.focus_plans.length}/5, ${rf.parts[0].assembled_bytes}B vs full ${full.parts[0].assembled_bytes}B; missing/hash-less/round 1/3 -> full`);

  fs.rmSync(tmp, { recursive: true, force: true });
  console.log("bundle.js: all assertions passed");
}

main();
