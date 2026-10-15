# verify-plan v2 — one review round per call

Invoked as `verify-plan --v2 '<json args>'` (SKILL.md routes here). v1 stays reachable with `--v1` until Phase 5 of `plans/abundant-skipping-meerkat.md`. Spec: `research/2026-09-26-replay/CANDIDATE.md`, audit §6 VR-2..VR-9. Runs inline in the main thread (Workflow is stripped from forks).

One round per invocation. No inner iterations, no complexity score, no trivial path. The caller (plan-phase step 10-12, plan-perfector) owns revision and the round counter.

## Args (JSON)

```
{ "mode": "gsd-phase" | "plan",
  "plan_paths": ["/abs/..."],            // gsd-phase: every *-PLAN.md of the phase; plan: one file
  "phase_dir": "/abs" | null,
  "success_criteria": ["verbatim", ...], // gsd-phase: roadmap.get-phase SCs; plan: its own Success/Verification section
  "req_ids": [...], "context_path": ..., "research_path": ..., "readiness_path": ...,
  "round": 1 | 2 | 3,
  "prior_verdict_path": "/abs/...verdict.json" | null,
  "codex_ab": false }
```
Missing `mode`/`plan_paths`/`round` → stop, report the usage line. Never guess args.

## Flow

S = `~/.claude/skills/verify-plan/scripts`, R = report base: `<first plan without .md>.verification.<YYYY-MM-DD-HHMM>-r<round>` (plan mode: beside the plan; gsd-phase: in `phase_dir`).

1. **Lint.** gsd-phase: `python3 ~/.claude/hooks/lib/plan-lint.py --repo <git top> --phase-dir <phase_dir> --json > R.lint.json`. plan: `... --plan <path> --json`.
   - exit 1 (BLOCK) → round is REVISE regardless, but still run steps 2-3 so reviewers catch what lint cannot (decision 2026-09-27: lint-first hid every reviewer catch on fresh/W19/W21, SCORE-V2 §5; keeping MONEY_NO_FLAG as BLOCK keeps #52).
   - exit 2/3 → lint did not run. Record it as a coverage gap in the report, keep going (never a pass on its own).
2. **Bundle.** gsd-phase: `node $S/bundle.js --plans '<json plan_paths>' --emit <R>.bundle/`, plus `--since <prior_verdict_path> --round 2` only when round == 2 (round 1 and 3 always full; bundle.js also refuses any other round and falls back to full on a missing/unreadable/hash-less verdict or no changed plan, reporting `full_reason`). Output `focus_plans`/`unchanged_plans` present → focused bundle. Lint (step 1) always covers all plans. One part → one review call. Two or more parts → one `plan-review` call per part, ≤ 2 parts in flight (each ≤ 6 agents; 12 at once; 15 = rate-limit dropouts), wait for all. Part whose `reviewers_failed` has a mandatory reviewer (R-A/R-B/R-C/R-D/S) → re-run that part once after the rest finish (replace its result). Then merge in part-index order (not arrival order): concat each finding list; an id in several parts or lists keeps one entry at its highest severity (blocking > significant > minor), first occurrence within that severity; `agents_spawned` summed, `reviewers_failed` unioned (still failed → verdict.js ERROR, never clean). Never hand-roll the merge. plan mode: no bundle, pass `plan_paths`.
3. **Review.** `Workflow({name: "plan-review", args: {mode, plan_paths, bundle_path, phase_dir, success_criteria, req_ids, context_path, research_path, readiness_path, lint_json: <contents of R.lint.json>, round, prior_open: <prior verdict blocking+significant>, live_env: <lint or readiness flags live env/infra>, files_modified: <union of plan frontmatter files_modified; migration paths add the DB lens>, focus_plans, unchanged_plans: <from bundle output, if present>, reviewers_dir: "~/.claude/skills/verify-plan/reviewers" (absolute)}})`. Save its `synth` to `R.synth.json`.
   - The Workflow task's `.output` file is a wrapper (`summary, agentCount, logs, result, …`), not the synth itself: take `synth` from
     `json.load(f)['result']['synth']`, and `json.loads` `result` first when it is a string. Top-level `d['synth']` is a KeyError.
     Save `R.synth.json` before writing `R.meta.json`.
   - `{error}` returned → verdict ERROR, write it, stop.
4. **Verdict.** Write `R.meta.json` = `{round, plan_paths, agents_spawned, reviewers_ok, reviewers_failed}` (no `money_path`: verdict.js computes it from plan frontmatter + lint), then:
   `node $S/verdict.js --lint R.lint.json --synth R.synth.json --meta R.meta.json [--prior <prior_verdict_path>] --out R.verdict.json`
   verdict.js owns the verdict and the cap decision (VR-5). Never override it by hand.
5. **Report** `R.md`: verdict + cap_decision/reason, BLOCKING table (id, plan:line, source, fix), SIGNIFICANT table (with mitigation), residual risks, lint counts, reviewers failed, agents spawned. Then the trailer:
   ```
   <!-- HANDOFF-VERDICT
   verdict: <verdict>
   cap_decision: <cap_decision or none>
   cycle: <round>
   blocking_open: <n>
   significant_open: <n>
   trivial: false
   plan_path: <plan_paths[0]>
   plan_paths: <comma-separated, lexicographic; multi-plan only>
   plan_hash: sha256:<plan_hashes[plan_path]>
   verdict_json: <abs R.verdict.json>
   origin_cwd: <cwd>
   -->
   ```
   `plan_path` stays singular: `plan-handoff-trigger.js` and `plan-verifier.js` join on it.
6. **Approve.** Only on `verdict: PROCEED` or `cap_decision: STOP_VERIFYING`: set plan frontmatter `status: approved` (plan mode), and on STOP_VERIFYING write `residual_risks:` (list of `id: title — mitigation`) into each plan's frontmatter. ESCALATE → stop and show Pedro the money-path residuals; ERROR → show `reviewers_failed`, no approval.

Return to the caller: `{verdict, cap_decision, verdict_json, report_path, blocking_open, significant_open}`.

## Optional: `codex_ab`
Off by default (0 unique catches in the census, VR-8). When true, after step 3 run the codex `adversarial` role once over the same bundle, plan text data-fenced, schema with `evidence{plan_line}`; its findings go to the report under "Codex A/B" only and never feed verdict.js. Sidecar: `codex-roles/adversarial.prompt.md`.

## Why this shape
- Lint first, because mechanical defects cost 0 tokens there and reviewers re-arguing them was most of v1's churn.
- 4 reviewers (R-D = depth, rule `context-builder/references/depth-pass.md`) + conditional prober/DB + synthesizer, hard cap 6 (mandatory retry exempt): v1's 6 charters overlapped and the fan-out never saw sibling plans.
- One round per call: v1's inner loops re-reviewed unchanged plans; the cap rule (verdict.js) caps at round 3 (round 2: REDESIGN / STOP_VERIFYING / ESCALATE or one more round; round 3: HALT unless PROCEED / STOP_VERIFYING / ESCALATE) instead of cycling.
