#!/usr/bin/env node
// verdict.js — deterministic verdict step for verify-plan v2 (AUDIT-2026-09-26 §6 VR-5).
// Merges plan-lint JSON with the synthesizer JSON, applies the fixed verdict rules, and at
// round >= 2 applies the cap-hit decision rule (REDESIGN / STOP_VERIFYING / ESCALATE / HALT).
// Rounds are flat <= 3 (the CONTINUE gate is gone): round 2 without a REDESIGN reason gets another
// round (cap null); round 3 that is not PROCEED / STOP_VERIFYING / ESCALATE is HALT. RETHINK keeps
// its verdict and gets no cap at round 2 (it used to become REDESIGN, which the caller auto-replans).
// money_path is computed here from each plan's frontmatter OR a lint money_path finding; meta no
// longer carries it (the main thread set it by hand and got it wrong: 1258-r2, 1401-r3).
//
// Id sets: `new` counts synthesizer BLOCKING ids absent from the prior round's BLOCKING and
// SIGNIFICANT (a severity promotion is not new; 2057-r2); lint has its own trend rule. `closed` also counts closed lint ids: a round that clears a lint BLOCK made progress, and
// ignoring it read one late reviewer catch as churn (two-round fixture, 2026-09-27). `reopened`
// and `closed_ids` use the full blocking_ids, which also carry lint BLOCK ids as
// `lint:<name>:<plan>` (no line — lines shift between rounds).
"use strict";
const fs = require("fs");
const path = require("path");
const { hashPlanBody } = require(path.join(__dirname, "../../../hooks/lib/plan-hash.js"));

const MANDATORY_REVIEWERS = ["R-A", "R-B", "R-C", "R-D", "S"];

function parseArgs(argv) {
  const opts = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--lint") opts.lint = argv[++i];
    else if (a === "--synth") opts.synth = argv[++i];
    else if (a === "--meta") opts.meta = argv[++i];
    else if (a === "--prior") opts.prior = argv[++i];
    else if (a === "--out") opts.out = argv[++i];
    else throw new Error(`unknown arg: ${a}`);
  }
  if (!opts.lint || !opts.synth || !opts.meta || !opts.out) {
    throw new Error(
      "usage: verdict.js --lint <lint.json|-> --synth <synth.json|-> --meta <meta.json> " +
      "[--prior <prior.verdict.json>] --out <path.verdict.json>"
    );
  }
  return opts;
}

// `-` means "absent" (lint skipped / fan-out skipped), not stdin.
function readJson(p) {
  if (!p || p === "-") return null;
  return JSON.parse(fs.readFileSync(p, "utf8"));
}

const hasText = (s) => typeof s === "string" && s.trim() !== "";
const lintId = (f) => `lint:${f.name}:${f.plan}`;

// Cap-hit rule. Pure; exported separately because branch (b) STOP_VERIFYING is unreachable
// through computeVerdict under the literal verdict rules (0 BLOCKING + 0 lint BLOCK + no money
// SIGNIFICANT is already PROCEED), so it is tested directly.
// Prior synthesizer ids: BLOCKING and SIGNIFICANT (was BLOCKING only).
const priorIds = (prior) => new Set([...((prior && prior.blocking) || []), ...((prior && prior.significant) || [])].map((f) => f.id));

// frontmatter `money_path: true` in any plan (unreadable plan = not flagged; lint still ORs in).
function plansMoneyPath(planPaths) {
  return planPaths.some((p) => {
    let txt;
    try { txt = fs.readFileSync(p, "utf8"); } catch (_) { return false; }
    const m = txt.match(/^---\r?\n([\s\S]*?)\r?\n---/);
    return !!m && /^money_path:\s*["']?true["']?\s*(#.*)?$/m.test(m[1]);
  });
}

function decideCap({ round, verdict, blocking, significant, blockingIds, lintBlock, prior, moneyPath }) {
  if (!(round >= 2) || (verdict !== "REVISE" && verdict !== "RETHINK")) return { cap_decision: null, cap_reason: null };
  if (verdict === "RETHINK") {
    return round >= 3 ? { cap_decision: "HALT", cap_reason: "round 3 cap: synthesizer marked RETHINK" } : { cap_decision: null, cap_reason: null };
  }
  const priorSynthIds = priorIds(prior);
  const priorClosed = new Set((prior && prior.closed_ids) || []);
  const curSynthIds = new Set(blocking.map((f) => f.id));
  const priorLintBlock = prior && prior.lint ? prior.lint.block || 0 : 0;

  const reasons = [];
  const reopened = blockingIds.filter((id) => priorClosed.has(id));
  if (reopened.length) reasons.push(`BLOCKING reopened after being closed: ${reopened.join(", ")}`);
  const nNew = [...curSynthIds].filter((id) => !priorSynthIds.has(id)).length;
  const priorLintIds = ((prior && prior.blocking_ids) || []).filter((id) => id.startsWith("lint:"));
  const nClosed = [...priorSynthIds].filter((id) => !curSynthIds.has(id)).length +
    priorLintIds.filter((id) => !blockingIds.includes(id)).length;
  if (blocking.length > 0 && nNew >= nClosed) reasons.push(`new BLOCKING (${nNew}) >= closed BLOCKING (${nClosed})`);
  const byPremise = {};
  for (const f of blocking) if (f.premise_tag) (byPremise[f.premise_tag] ||= []).push(f.id);
  for (const [tag, ids] of Object.entries(byPremise)) {
    if (ids.length >= 2) reasons.push(`${ids.length} open BLOCKING share premise "${tag}"`);
  }
  if (priorLintBlock > 0 && lintBlock >= priorLintBlock) {
    reasons.push(`lint BLOCK count did not fall (${priorLintBlock} -> ${lintBlock})`);
  }

  if (blocking.length === 0 && lintBlock === 0) {
    const money = significant.filter((f) => f.money_path);
    if (moneyPath && money.length) {
      return { cap_decision: "ESCALATE", cap_reason: `money-path SIGNIFICANT open (${money.map((f) => f.id).join(", ")}); STOP_VERIFYING forbidden, needs Pedro` };
    }
    const unmitigated = significant.filter((f) => !hasText(f.mitigation));
    if (!unmitigated.length) return { cap_decision: "STOP_VERIFYING", cap_reason: `0 BLOCKING; ${significant.length} SIGNIFICANT carried as named residual risks` };
    return { cap_decision: "ESCALATE", cap_reason: `SIGNIFICANT without a named mitigation: ${unmitigated.map((f) => f.id).join(", ")}` };
  }

  if (round >= 3) return { cap_decision: "HALT", cap_reason: ["round 3 cap: verdict REVISE", ...reasons].join("; ") };
  if (reasons.length) return { cap_decision: "REDESIGN", cap_reason: reasons.join("; ") };
  return { cap_decision: null, cap_reason: null }; // round 2 with progress: one more round
}

function computeVerdict({ lint, synth, meta, prior, planHashes }) {
  const lintFindings = (lint && lint.findings) || [];
  const lintCounts = { block: (lint && lint.counts && lint.counts.block) || 0, warn: (lint && lint.counts && lint.counts.warn) || 0 };
  const blocking = (synth && synth.blocking) || [];
  const significant = (synth && synth.significant) || [];
  const rethink = !!(synth && synth.rethink);
  const failed = meta.reviewers_failed || [];
  const planPaths = meta.plan_paths || [];
  const moneyPath = plansMoneyPath(planPaths) || lintFindings.some((f) => f.check === "money_path");

  const blockingIds = [...new Set([
    ...blocking.map((f) => f.id),
    ...lintFindings.filter((f) => f.severity === "BLOCK").map(lintId),
  ])];
  const priorBlockingIds = (prior && prior.blocking_ids) || [];
  const closedIds = [...new Set([
    ...((prior && prior.closed_ids) || []),
    ...priorBlockingIds.filter((id) => !blockingIds.includes(id)),
  ])];

  let verdict;
  if (failed.some((r) => MANDATORY_REVIEWERS.includes(r))) verdict = "ERROR";
  else if (lintCounts.block > 0) verdict = "REVISE";
  else if (blocking.length) verdict = rethink ? "RETHINK" : "REVISE";
  else if (moneyPath && significant.some((f) => f.money_path)) verdict = "REVISE";
  else verdict = "PROCEED";

  const cap = decideCap({
    round: meta.round, verdict, blocking, significant, blockingIds,
    lintBlock: lintCounts.block, prior, moneyPath,
  });
  // New SIGNIFICANT ids are tracked like BLOCKING. At round >= 3 a new non-money SIGNIFICANT is
  // carried as residual debt instead of forcing another revise; money SIGNIFICANT never is.
  const seen = priorIds(prior);
  const newSigIds = prior ? significant.filter((f) => !seen.has(f.id)).map((f) => f.id) : significant.map((f) => f.id);
  const toResidual = (f) => ({ id: f.id, title: f.title, mitigation: f.mitigation });
  const residual = verdict === "PROCEED" || cap.cap_decision === "STOP_VERIFYING"
    ? significant.map(toResidual)
    : meta.round >= 3 ? significant.filter((f) => !f.money_path && newSigIds.includes(f.id)).map(toResidual) : [];

  return {
    verdict, cap_decision: cap.cap_decision, cap_reason: cap.cap_reason,
    blocking, significant, residual_risks: residual, new_significant_ids: newSigIds, money_path: moneyPath, lint: lintCounts,
    plan_hashes: planHashes || {}, round: meta.round, agents_spawned: meta.agents_spawned,
    reviewers_ok: meta.reviewers_ok || [], reviewers_failed: failed,
    plan_path: planPaths[0] || null, plan_paths: planPaths,
    blocking_ids: blockingIds, closed_ids: closedIds,
  };
}

function hashPlans(planPaths) {
  const out = {};
  for (const p of planPaths) {
    try { out[p] = hashPlanBody(fs.readFileSync(p, "utf8")); } catch (_) { out[p] = null; }
  }
  return out;
}

function main() {
  let opts;
  try { opts = parseArgs(process.argv.slice(2)); }
  catch (e) { process.stderr.write(`${e.message}\n`); process.exit(2); }

  const meta = readJson(opts.meta);
  const result = computeVerdict({
    lint: readJson(opts.lint), synth: readJson(opts.synth), meta,
    prior: opts.prior ? readJson(opts.prior) : null,
    planHashes: hashPlans(meta.plan_paths || []),
  });
  fs.writeFileSync(opts.out, JSON.stringify(result, null, 2) + "\n");
  process.stdout.write(`${result.verdict}${result.cap_decision ? " / " + result.cap_decision : ""}\n`);
}

module.exports = { computeVerdict, decideCap, hashPlans, MANDATORY_REVIEWERS };
if (require.main === module) main();
