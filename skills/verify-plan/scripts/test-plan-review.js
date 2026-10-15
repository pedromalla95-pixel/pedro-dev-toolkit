#!/usr/bin/env node
// Runs workflows/plan-review.js with mocked agent()/parallel() to check the cap and failure rules (C9, C18).
// run: node skills/verify-plan/scripts/test-plan-review.js
const assert = require('assert')
const fs = require('fs')
const path = require('path')

const SRC = fs.readFileSync(path.join(__dirname, '../../../workflows/plan-review.js'), 'utf8')
  .replace('export const meta', 'const meta')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const body = new AsyncFunction('args', 'agent', 'parallel', 'log', 'phase', SRC)
const { computeVerdict } = require('./verdict.js')

// Minimal JSON-schema check (type/enum/properties/required/items): enough for the plan-review schemas.
function valid(schema, v) {
  const types = [].concat(schema.type || [])
  const typeOf = x => x === null ? 'null' : Array.isArray(x) ? 'array' : Number.isInteger(x) ? 'integer' : typeof x
  if (types.length && !types.includes(typeOf(v))) return false
  if (schema.enum && !schema.enum.includes(v)) return false
  if (schema.items && !v.every(x => valid(schema.items, x))) return false
  if (schema.properties) {
    if ((schema.required || []).some(k => !(k in v))) return false
    if (Object.entries(schema.properties).some(([k, s]) => k in v && !valid(s, v[k]))) return false
  }
  return true
}

async function run(args, behave) {
  const calls = [], prompts = []
  const schemas = {}
  const agent = async (prompt, opts) => { calls.push(opts.label); prompts.push(prompt); schemas[opts.label] = opts.schema; return behave(opts.label, calls) }
  const parallel = thunks => Promise.all(thunks.map(t => t().catch(() => null)))
  const res = await body(args, agent, parallel, () => {}, () => {})
  return { res, calls, prompts, schemas }
}

const ARGS = { mode: 'gsd-phase', plan_paths: ['/x/02-01-PLAN.md'], reviewers_dir: '/r', success_criteria: ['a'], round: 1 }
const clean = { findings: [] }
const synth = { blocking: [], significant: [], minor: [], rethink: false, reason: 'ok' }
const ok = label => label.startsWith('S ') ? synth : label.startsWith('P ') ? { probes: [], findings: [] } : clean

const cases = {
  'clean round spawns 5 (no prober), R-D among them': async () => {
    const { res, calls } = await run(ARGS, ok)
    assert.strictEqual(res.agents_spawned, 5)
    assert.ok(calls.includes('R-D depth'))
    assert.ok(res.reviewers_ok.includes('R-D'))
    assert.deepStrictEqual(res.reviewers_failed, [])
  },
  'unknowns trigger the prober, total 6': async () => {
    const { res } = await run(ARGS, l => l.startsWith('R-A') ? { findings: [], unknowns: ['is X live?'] } : ok(l))
    assert.strictEqual(res.agents_spawned, 6)
    assert.ok(res.reviewers_ok.includes('P'))
  },
  'failed R-C while prober runs: cap holds (+1 exempt retry), S still runs, R-C reported failed': async () => {
    const { res, calls } = await run({ ...ARGS, live_env: true }, l => l.startsWith('R-C') ? null : ok(l))
    assert.strictEqual(calls.filter(l => l.startsWith('R-C')).length, 2, 'mandatory R-C gets exactly one retry')
    assert.ok(res.agents_spawned <= 7, `spawned ${res.agents_spawned}`)
    assert.ok(calls.includes('S synthesizer'), 'synthesizer must still run')
    assert.ok(res.reviewers_failed.includes('R-C'))
    assert.ok(res.synth, 'synth result returned so verdict.json can be written')
  },
  'migration plan: DB lens spawned, prober skipped, total <= 6': async () => {
    const { res, calls } = await run({ ...ARGS, live_env: true, files_modified: ['src/db/schema/orders.ts', 'drizzle/'] },
      l => l.startsWith('R-A') ? { findings: [], unknowns: ['is X live?'] } : ok(l))
    assert.ok(calls.some(l => l.startsWith('DB ')), 'DB lens must run')
    assert.ok(!calls.some(l => l.startsWith('P ')), 'prober must be skipped')
    assert.ok(calls.includes('S synthesizer'))
    assert.ok(res.agents_spawned <= 6, `spawned ${res.agents_spawned}`)
    assert.deepStrictEqual(res.reviewers_failed, [])
    assert.ok(res.reviewers_ok.includes('DB'))
  },
  'migration plan: R-D null on first call still gets one retry (cap-exempt) and an OK verdict': async () => {
    let rdCalls = 0
    const { res, calls } = await run({ ...ARGS, files_modified: ['drizzle/0007_orders.sql'] },
      l => l.startsWith('R-D') ? (rdCalls++ ? clean : null) : ok(l))
    assert.strictEqual(calls.filter(l => l.startsWith('R-D')).length, 2, 'R-D retried once')
    assert.ok(calls.includes('S synthesizer'), 'S still fits after the exempt retry')
    assert.ok(calls.some(l => l.startsWith('DB ')))
    assert.deepStrictEqual(res.reviewers_failed, [])
    assert.strictEqual(res.agents_spawned, 7)
    const v = computeVerdict({ lint: null, synth: res.synth, prior: null,
      meta: { round: 1, plan_paths: ARGS.plan_paths, agents_spawned: res.agents_spawned, reviewers_ok: res.reviewers_ok, reviewers_failed: res.reviewers_failed } })
    assert.strictEqual(v.verdict, 'PROCEED')
  },
  'real SYNTH schema accepts source R-D (and rejects an unknown source)': async () => {
    const { schemas } = await run(ARGS, ok)
    const SYNTH = schemas['S synthesizer']
    assert.ok(SYNTH && SYNTH.properties.significant, 'synthesizer schema captured from plan-review.js')
    const f = { id: '02-01#unverified-b', severity: 'SIGNIFICANT', plan: '02-01-PLAN.md', line: 14, title: 't',
      evidence: 'UNVERIFIED: b', impact: 'i', fix: 'proof: run fixtures', premise_tag: null, money_path: false, source: 'R-D', mitigation: null }
    const out = { blocking: [], significant: [f], minor: [], rethink: false, reason: 'r' }
    assert.ok(valid(SYNTH, out), 'R-D source must pass the SYNTH schema')
    assert.ok(!valid(SYNTH, { ...out, significant: [{ ...f, source: 'R-X' }] }), 'validator must reject unknown source')
  },
  'non-migration plan: no DB lens, prober still runs': async () => {
    const { res, calls } = await run({ ...ARGS, files_modified: ['src/app/page.tsx', 'tests/orders.test.ts'] },
      l => l.startsWith('R-A') ? { findings: [], unknowns: ['is X live?'] } : ok(l))
    assert.ok(!calls.some(l => l.startsWith('DB ')), 'no DB lens')
    assert.ok(calls.includes('P prober'))
    assert.strictEqual(res.agents_spawned, 6)
  },
  'round 2 passes fixed ids to reviewers; round 1 does not': async () => {
    const fixed = ['02-01#gate-count']
    const r2 = await run({ ...ARGS, round: 2, fixed_ids: fixed }, ok)
    // a new money SIGNIFICANT must still surface at round >= 2, or verdict.js's money ESCALATE never sees it
    assert.ok(r2.prompts.slice(0, 4).every(p => p.includes('02-01#gate-count') && /new BLOCKING and any new SIGNIFICANT with money_path: true/.test(p)))
    const r1 = await run({ ...ARGS, round: 1, fixed_ids: fixed }, ok)
    assert.ok(!r1.prompts.some(p => /new BLOCKING and/.test(p)))
  },
  'round 2 with focus_plans names focus vs unchanged plans; money clause intact': async () => {
    const r2 = await run({ ...ARGS, round: 2, fixed_ids: ['x'], focus_plans: ['/x/02-01-PLAN.md'], unchanged_plans: ['/x/02-02-PLAN.md'] }, ok)
    assert.ok(r2.prompts.slice(0, 4).every(p => p.includes('/x/02-01-PLAN.md') && p.includes('/x/02-02-PLAN.md')
      && /carried-over findings on unchanged plans stay open unless a focus change fixes them/.test(p)
      && /new BLOCKING and any new SIGNIFICANT with money_path: true/.test(p)))
    const r2full = await run({ ...ARGS, round: 2, fixed_ids: ['x'] }, ok)
    assert.ok(!r2full.prompts.some(p => /Focus plans/.test(p)), 'no focus sentence without focus_plans')
  },
  'bad args abort before any spawn': async () => {
    const { res, calls } = await run({ mode: 'x' }, ok)
    assert.ok(res.error)
    assert.strictEqual(calls.length, 0)
  },
}

;(async () => {
  let fail = 0
  for (const [name, fn] of Object.entries(cases)) {
    try { await fn(); console.log(`ok   ${name}`) } catch (e) { fail++; console.log(`FAIL ${name}: ${e.message}`) }
  }
  process.exit(fail ? 1 : 0)
})()
