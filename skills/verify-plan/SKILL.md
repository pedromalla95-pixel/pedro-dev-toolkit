---
name: verify-plan
description: >-
  Adversarial review of an existing implementation plan; returns PROCEED/REVISE/RETHINK. Triggers:
  "/verify-plan", "verify plan", "check this plan", "challenge this plan", "stress-test the plan",
  "adversarial review", "blind spots in my plan", "convergence loop". Also proactively after
  writing a plan. NOT code review, SQL/YAML checks, prompt audits, or plan creation.
---

# Verify Plan V2

Convergence loop orchestrator for adversarial plan verification. The Step 3a challenge fan-out runs through the saved `plan-challenge` workflow (fresh workers per iteration); the synthesizer is a fresh one-shot agent spawn. No persistent agents, no agent-to-agent messaging. Runs inline: the harness strips `AskUserQuestion` and `Workflow` from *every* subagent and skill fork (`Agent` is not depth-blocked; see `rules-recall/known-limits-agents.md`), so under a fork Step 3a's fan-out cannot dispatch and the skill silently degrades to a single-context review. Do not re-add the fork without first re-probing that filter. "Main thread" in this file = whichever context runs this skill body; it holds all state and serializes communication. Role charters live in `challenger-charters/` inside this skill — the old hard dependency on `~/.claude/agents/plan-*.md` is retired on the primary path.

## Pipeline Position

```
Plan written → /verify-plan (THIS) → Execution
```

## Invocation

`/verify-plan [path] [--team=base|scaled] [--threshold=N]`

**Routing (v2 default for callers, 2026-09-27).** Args are a JSON object, or `--v2 '<json args>'` → stop reading here and follow `SKILL-v2.md` in this directory (one review round, verdict.json). `--v1`, a bare path, or prose → v1, everything below. plan-phase step 10 and plan-perfector pass JSON, so they get v2. v1 removed in Phase 5 of `plans/abundant-skipping-meerkat.md`.

Path resolution order:
1. Explicit path argument (e.g., `/verify-plan .claude/plans/my-plan.md`)
2. Most recently modified file in `.claude/plans/`
3. Ask user for path

Override flags:
- `--team=base` — force base team regardless of complexity
- `--team=scaled` — force scaled team regardless of complexity
- `--threshold=N` — override complexity threshold (default: 7)

## Infra Triggers (force SCALED + require Empirical Probe)

When these keywords appear in the plan body (case-insensitive, word-boundary matched — see Matching rule below), the plan is "infra-flagged". Infra-flagged plans:
- MUST run with team: SCALED regardless of surface-complexity self-assessment
- MUST include an "Empirical Probe Findings" section in the final verification report
- Cannot receive PROCEED without at least one completed probe (see § Empirical Probe and § PROCEED-gate)

**Environment state:** `.env`, `.env.local`, `.env.production`, `env var`, `secret`, `credential`, `service role`
**Cloud infra:** `vercel`, `supabase`, `aws`, `gcp`, `cloudflare`, `deploy`, `deployment`, `production`, `prod`, `staging`
**Cross-env coupling risk:** `migration`, `database url`, `project ref`, `connection string`, `feature flag`, `rollout`
**Money/tax paths:** `invoice`, `itbis`, `fiscal`, `ncf`, `rnc`, `tax`, `payroll`, `suplidor`, `ledger`, `inventory`, `inventario`, `backfill` (`hook` is deliberately excluded)
**Local self-infra:** `systemd`, `systemctl`, `daemon`
**Multi-surface tests:** `stress test`, `e2e`, `smoke test`, `integration test` (combined with any cloud/env keyword above)

Matching rule: case-insensitive, **word-boundary guarded (`\b`) — NOT raw substring**. ≥1 match = infra-flagged.

Two constraints the keyword list above does not show on its face:
- **Inflections must be explicit.** `\b`-guarding means a bare keyword does not match its own plural or verb forms — `\b(secret)\b` misses "secrets". The list above names concepts; the pattern spells out the forms (`secrets?`, `credentials?`, `deployments?`, `migrat(e|es|ed|ing|ion|ions)`).
- **`.env` cannot be `\b`-guarded.** A `\b` before a literal `.` only fires when a word char precedes it, which makes `.env` inert in ordinary prose (`.env file`, `` `.env` ``, `.env.local` would all miss). It takes a `(?:^|[^\w.])` left-guard instead.

Bias: a false positive costs tokens (an over-scaled verify); a false negative silently skips the mandatory empirical probe on a plan that touches live infra. When in doubt, flag.

Money/tax keywords produce true positives on money repos; generic words like "production" and
"migration" can false-positive, but per the Bias above they stay.

**Executable mirror:** `~/.claude/hooks/plan-verifier.js` § `INFRA_RE` (in `scanComplexity`) implements this rule and gates whether a plan gets verified at all. This section and that regex must stay in sync.

## Team Composition

### Base Team (complexity < 7, unknowns <= 3)

| Agent | Model | Role |
|-------|-------|------|
| plan-challenger | sonnet | Challenge brief + unknowns manifest |
| plan-granularity-checker | sonnet | Step decomposition & executability analysis |
| plan-researcher | sonnet | 3-lane research (code/docs, capability/reuse, external currency) |
| plan-devils-advocate | sonnet | Black-swan scenarios (non-obvious failure modes, second-order effects) |
| plan-synthesizer | opus | Binary resolution tracking + convergence |

### Scaled Team (complexity >= 7 OR unknowns > 3) — adds:

| Agent | Model | Role |
|-------|-------|------|
| plan-domain-expert | sonnet | Domain-specific challenges |

## Rumsfeld Quadrants — Synthesis Checklist Framing

The four Rumsfeld quadrants are a one-line framing the synthesizer uses to sanity-check that no
*category* of knowledge gap was ignored. This is framing only — there is no coverage score and no
gate; the verdict is driven entirely by BLOCKING/SIGNIFICANT challenge count. Don't re-add a SWOT
phase, Rumsfeld-as-gate, or a quality score.

- **Q1 — Unknown Knowns:** context in memory/codebase/git nobody connected to this plan (research Lane 1/2).
- **Q2 — Known Unknowns:** gaps the challenger flagged that need research (unknowns manifest + research).
- **Q3 — Known Knowns:** assumptions the plan treats as fact that may be wrong or stale (challenger Assumption Audit).
- **Q4 — Unknown Unknowns:** blind spots nobody thought to look for (devils-advocate + empirical probe).

## Execution Flow

### Step 00 (preflight): Capability Gate — RUN BEFORE ANYTHING ELSE

Check your own available tools for `Agent`, `Workflow`, and `AskUserQuestion`.

A tool listed as deferred (name shown, schema withheld) is NOT missing: load it with `ToolSearch select:<name>` first, one call for all deferred ones. Only a tool still absent after that counts as missing. This skill's own instructions tell you to call `Workflow` (Step 3a), so that satisfies the `Workflow` tool's opt-in rule, including on the hook-triggered path (AAR plan-perfector 2026-10-14: 2f2c82a2, 94dda40b).

**If ANY of the three is missing, STOP immediately.** Do not assess complexity, do not size a
team, do not read the plan, and above all do not emit a `PROCEED` / `REVISE` / `RETHINK` verdict.
Return exactly:

```
VERIFY-PLAN ABORTED — DEGRADED TOPOLOGY: missing [<tools>].
Multi-role fan-out cannot execute. This skill must run inline on the calling
thread, not as a subagent or `context: fork` (see § Verify Plan V2 header).
```

**Why:** partial tool stripping is silent; without this gate the skill runs Steps 1-5 as a
single-context review and returns it in multi-role verdict format, indistinguishable from a real
adversarial pass. A single-context review must never be returned in multi-role verdict format.
No hook event exists for partial stripping, so enforcement lives here. Doctrine must not outrun
enforcement: state the degradation, do not paper over it.

### Step 000 (resume): Inner-Loop Resume Check — RUNS AFTER STEP 00, BEFORE STEP 0

Step 00 has already passed at this point. Never reorder these two.

Read `<phase_dir>/<padded_phase>-cross-cycle-state.json` if it exists. Resume ONLY when all
three hold:

1. `inner_loop.status` is `CONTINUE`;
2. `inner_loop.iteration` is present and `< 3`;
3. the plan file is unchanged since `snapshot_commit` — determined by the existing
   `plan-scope.js` call, not by a second staleness check written here.

When all three hold: **skip Step 1 and Step 2 entirely.** Do not re-discover the plan, do not
re-score complexity, do not re-size or re-announce the team. Load
`inner_loop.iteration_state` as the live `iteration_state` and enter Step 3a with
`iteration = inner_loop.iteration + 1`. Tell the user one line:
`"Resuming verification at iteration N of 3 (state restored from disk)."`

On ANY ambiguity — file missing, unreadable, malformed JSON, absent `inner_loop`, plan
changed, `plan-scope.js` reporting `fail_open` — fail open to a normal fresh invocation and
continue to Step 0. This matches `plan-scope.js`'s own fail-open stance. "Cannot tell" is
never "resume".

**Why:** loop state lives in conversation context, and compaction discards it; without this
check a compacted run restarts at iteration 1 and the 3-iteration circuit breaker never fires.

### Step 0: Resolve Verification Backend (config-gated, additive)

Before anything else, resolve which engine runs the per-role analysis. This is purely additive —
the default is `claude` and the entire Claude flow below is UNCHANGED for that backend.

```bash
SKILL="verify-plan"
BACKEND=$(gsd-sdk query config-get "verification.per_skill.${SKILL}" 2>/dev/null | jq -r '.' 2>/dev/null)
if [ -z "$BACKEND" ] || [ "$BACKEND" = "null" ]; then
  BACKEND=$(gsd-sdk query config-get "verification.backend" 2>/dev/null | jq -r '.' 2>/dev/null)
fi
if [ -z "$BACKEND" ] || [ "$BACKEND" = "null" ]; then BACKEND="claude"; fi
```

- **`BACKEND=claude` (default, or config absent):** run Steps 1-5 below EXACTLY as written. Ignore
  the `## Codex Backend` section entirely. Nothing about the Claude path changes.
- **`BACKEND=codex`:** every per-role **Task spawn** is replaced by a
  **codex-verify helper call** per the `## Codex Backend (config-gated, additive)` section near the
  end of this file. That section ALSO resolves `verification.codex.plan_depth` (fallback **`lite`**)
  and branches:
  - **`plan_depth=lite` (default):** run **3 merged roles in a single pass, NO convergence loop** —
    `adversarial`, `empirical-domain`, `structural-verdict` (which emits the verdict). Step 2
    still runs for reporting.
  - **`plan_depth=faithful` (opt-in):** run Steps 1-5 below with one and only one substitution —
    each per-role Task agent becomes a codex-verify call. ALL orchestration stays identical.

  Either depth uses the same RC=3 CODEX_UNAVAILABLE pre-flight checkpoint and the same Step-4
  plan-perfector summary contract.

### Step 1: Discover Plan File

Resolve path using invocation order above. Confirm file exists and is readable. Read plan content.

Also resolve `<REPO>` here — the absolute repo root the plan's gates are written against, used
by Step 2.5 and (when active) the codex backend. Take the plan's `project`/`repo` frontmatter if
present, else `git -C <plan dir> rev-parse --show-toplevel`, else the session cwd. Resolve it on
every path, not just `BACKEND=codex` — Step 2.5 runs on the default `claude` backend too.

### Step 2: Complexity Assessment & Team Sizing

Assess plan quality (RICH / ADEQUATE / THIN / TRIVIAL) and complexity:

| Factor | Points |
|---|---|
| Steps/phases in plan | +1/step (max 3) |
| Distinct domains | +1/domain (max 3) |
| External integrations | +2/system |
| Compliance/regulatory | +2 |
| Unknown signals (TBD, TODO, unclear, etc.) | +1 each |
| 10+ referenced files | +1 |

Decision:
- **TRIVIAL plan (score <= 2 AND no external integrations AND no compliance/unknown signals — e.g. a one-liner, single-file edit, or pure-internal rename/refactor with no library/framework/API/program/app/repo mention):** Skip team entirely. Output abbreviated `PROCEED (trivial)` with 1-2 sentence note. ~2K tokens. DONE.
- **THIN plan:** Force BASE team even if complexity score suggests SCALED.
- **Score < 7 AND unknowns <= 3:** BASE team (5 agents).
- **Score >= 7 OR unknowns > 3:** SCALED team (6 agents).
- Override: `--team=base` | `--team=scaled` | `--threshold=N`

Show assessment to user: `"Complexity: N/14. Team: BASE|SCALED. Starting verification..."`

### Step 2.5: Deterministic Gate Pre-Flight (zero tokens, runs before any backend call)

Run, before any codex or Claude role is dispatched:

```bash
python3 ~/.claude/skills/verify-plan/scripts/preflight-gates.py "<ABS_PLAN_PATH>" --repo-root "<REPO>"
```

Exit 1 → do NOT dispatch the backend. Report the `UNRUNNABLE` / `SELF_INVALIDATING` /
`UNREACHABLE` lines as findings and return `verdict: REVISE` with those lines as `key_issues`.
These are arithmetic, not judgment.

Exit 0, no `UNVERIFIED-GATES:` line → continue to Step 3 unchanged.

Exit 0 WITH an `UNVERIFIED-GATES:` line → continue to Step 3, AND copy every
`UNVERIFIED-GATE <path>:<line> \`<cmd>\`` line into `iteration_state.coverage_gaps` as a
declared coverage gap. The synthesizer reads that field and the final report must name these
gates as unverified. Do NOT treat this as a clean pass and do NOT convert it into a blocker:
every code-target gate is `NOT-MODELLED` by design (the pass models prose supply only), so
blocking here would auto-REVISE every plan that touches code. The defect being fixed is a
discarded signal, not an unraised alarm.

**Why:** an exit-0 pre-flight can still list gates it could not verify; dropping them loses
the signal that `protocol.md` § 7's `BLOCKED → REVISE` rung depends on.

This step reports ONLY the three blocking mechanical classes plus the one coverage-gap class. A gate
that reads false today because a task in this plan is what makes it true is CORRECT and is never
reported here.

### Step 3: CONVERGENCE LOOP (max 3 iterations)

Initialize `iteration_state` per `protocol.md` schema. Each iteration runs Steps 3a-3d.

**Cross-cycle state (cycle scoping).** Before Step 3a on iteration 2 or 3, compute this cycle's
scope:
```bash
node ~/.claude/skills/verify-plan/scripts/plan-scope.js \
  --repo-root "<REPO>" --state "<phase_dir>/<padded_phase>-cross-cycle-state.json" \
  --plan "<ABS_PLAN_PATH>" [--plan "<ABS_PLAN_PATH_2>" ...] \
  [--reviser-touch "<file named in a prior cycle's revision Touches: column>" ...] \
  | tee "<phase_dir>/<padded_phase>-scope.json"
```
Parse the JSON on stdout (also written to `<phase_dir>/<padded_phase>-scope.json` for the codex
path's `--scope` flag in Step 5). `scope_block` is a pre-formatted string, or `null` ONLY when
`fail_open` is `true` — no prior state, a failing `git diff`, or a plan file untracked by git.
`null` means verify normally, as a full read; never treat it as "nothing changed".

When the cycle ran successfully and found zero changed files (`fail_open: false`, `scope: []`),
`scope_block` is NOT null: it carries the no-change block, which tells every role to audit only
the OPEN and UNRESOLVED challenges in `iteration_state` and the fixes claimed for them. Pass it
through unchanged — this is the normal case for iterations 2-3, because the plan file is not
revised between iterations of one invocation. Skip this call entirely on iteration 1 — no
snapshot exists yet, so there is nothing to diff against.

#### Step 3a: Fan-Out Phase (challenger + granularity + research run CONCURRENTLY)

The challenger, granularity-checker, and researcher are independent — none consumes another's output to produce its own. They run **concurrently inside ONE `plan-challenge` Workflow call**. This is a LATENCY optimization, not a token saving (same total work, fewer serial round-trips). The synthesizer (Step 3c) runs LAST, after the workflow returns.

Challenge-ID numbering is the ONLY coupling between challenger and granularity-checker; it is resolved by **renumbering all challenge IDs on the main thread at the merge step** (below), NOT by sequencing the workers. Each worker numbers its own challenges from C1 locally; the merge reassigns global IDs.

**Primary dispatch — ONE Workflow call per iteration:**

```
Workflow({name: "plan-challenge", args: {
  plan_path: "<absolute plan path — the primary plan; ALWAYS set, single or multi>",
  plan_paths: ["<absolute plan path>", "..."],   // optional; multi-plan verification only
  iteration_state: "<serialized state JSON, or 'first iteration'>",
  team: "base" | "scaled",
  run_granularity: <true on iteration 1; on 2-3 only if Synthesizer directives include RE-GRANULARITY or the plan file was revised since the last granularity run>,
  run_devils_advocate: <true on iteration 1; on 2-3 only if Synthesizer directives include RE-ADVERSARIAL>,
  run_domain_expert: <true on iteration 1; on 2-3 only if Synthesizer directives include RE-DOMAIN. Ignored unless team is "scaled">,
  charters_dir: "/home/pedro/.claude/skills/verify-plan/challenger-charters",
  scope_block: <the `scope_block` string from this cycle's plan-scope.js output — omit or pass null on iteration 1, or when plan-scope.js reported fail_open>,
  cross_cycle_state: <raw contents of `<phase_dir>/<padded_phase>-cross-cycle-state.json` when that file exists — same condition as the codex path's `--cross-cycle-state` flag; omit on iteration 1 or when the file does not exist>
}})
```

- The workflow runs challenger + researcher (always), devils-advocate (per `run_devils_advocate`), granularity (per `run_granularity`), and domain-expert (team: scaled, and per `run_domain_expert`) as concurrent workers, each executing its charter from `challenger-charters/` at the roster's model tiers (sonnet for challenger/domain/devils/researcher/granularity).
- **If the returned envelope carries an `error` key** (bad args, budget too low for challenger+researcher, mandatory worker failed), surface the message verbatim and STOP the verification with a clear failure — never emit a verdict from a partial fan-out. This is NOT the fallback case.
- `workers[].output` holds each role's raw text in protocol.md shapes — feed them to the merge step exactly as the legacy Task returns were. `failed_roles`/`dropped_roles` (optional workers only) → record as coverage gaps for the synthesizer.
- If `run_granularity` was false, set `granularity_findings.run_this_iteration = false`.
- **A gated role is not a coverage gap.** If `run_devils_advocate` or `run_domain_expert` was false, that role is absent from `ran`, from `failed_roles`, and from `dropped_roles`. Record it as deliberately skipped — NOT as a failure and NOT as a coverage gap for the synthesizer. Only `failed_roles`/`dropped_roles` are coverage gaps. Cross-check the returned `gating` object: if it shows `true` for a role on iteration 2-3, the gate was never wired and you are paying for a worker you intended to skip.
- **Multi-plan verification.** `plan_path` is always set and always singular — it names the primary
  plan and is what every downstream consumer reads. `plan_paths` is additive and optional: omit it for
  a single plan, or pass the full array (including the primary) for a multi-plan run.
  Merge order is lexicographic by filename — 23-01, 23-02, 23-03 — and findings cite the plan they
  came from by filename, never by index.

**Fallback (only if the Workflow tool is unavailable in the session):** the legacy path — spawn the roles as parallel `Agent` (Task) calls in ONE message using the `plan-challenger`/`plan-researcher`/`plan-devils-advocate`/`plan-granularity-checker` (+`plan-domain-expert` on SCALED) agent types, same briefs as the workflow's worker prompts. This path requires the `~/.claude/agents/plan-*.md` roster to exist. On this path there is NO spawn-level gate — apply the same iteration-2/3 selection yourself: omit `plan-devils-advocate` unless the directives carry `RE-ADVERSARIAL`, and `plan-domain-expert` unless they carry `RE-DOMAIN`.

**Researcher lane contract** — the 3 mandatory lanes live in the researcher's charter (`challenger-charters/plan-researcher.md`); Step 3b below documents them.

**Merge step (main thread, after ALL fan-out workers return):**
1. Collect every worker's challenges. Renumber ALL challenges sequentially (C1, C2, …) in a stable order: challenger first, then domain-expert, then devils-advocate, then granularity escalations, then researcher escalations. This makes challenge IDs globally unique without sequencing the spawns.
2. Merge EVERY challenge every worker surfaced into `iteration_state.challenges`. There is no
   count ceiling — nothing is dropped or deferred for volume. Then size the merged set against
   a single synthesizer call's budget, using the same class of estimate the codebase already
   uses for worker cost (`plan-challenge.js:64`'s `WORKER_COST` is the precedent: budget math
   from an estimate, not a measurement).
   - **Fits the budget:** one Step 3c call, exactly as before. This is the normal case.
   - **Does not fit:** split into severity-ordered batches — all BLOCKING first, then
     SIGNIFICANT, then the rest — and run Step 3c over them sequentially **inside this same
     iteration**. Do NOT start a new iteration and do NOT return to the user between batches.
     After each batch, write the accumulated `iteration_state` to `inner_loop.iteration_state`
     (Step 3d's write, schema in `cross-cycle-state.md`) so a compaction mid-run resumes at the
     next unprocessed batch through the Step 000 Resume Check instead of losing completed work.
   - **The verdict comes from the FINAL batch only.** Earlier batches contribute resolution
     updates and directives, never a verdict. `protocol.md` § 7 derives convergence by counting
     OPEN + UNRESOLVED challenges across the WHOLE set — `CONVERGED → PROCEED` requires zero
     BLOCKING *and* zero SIGNIFICANT — so a synthesizer that has seen only the BLOCKING batch
     cannot evaluate it. Pass each batch the accumulated state from the batches before it.
3. Store all granularity findings (including non-escalated) in `iteration_state.granularity_findings`.

#### Step 3b: Research Phase — 3 mandatory lanes (`plan-researcher`)

Runs concurrently inside the Step 3a `plan-challenge` workflow. The researcher runs THREE lanes. Lanes 1 and 2 are NOT skippable — there is no "skipped this iteration" default for them. Lane 3 is smart-gated on external mentions. The full contract lives in the researcher's charter; the brief below documents it (and is the prompt to use on the legacy Agent fallback path):
```
Plan at [path]. Goal: [plan goal]. Iteration state: [serialized state including challenges and unknowns manifest].
Run all three lanes below. Lanes 1 and 2 are MANDATORY. Lane 3 fires on any external mention.

Lane 1 — Code/docs (ALWAYS, for any non-trivial plan):
  Read the actual files / modules / docs the plan will touch. Resolve the unknowns manifest against
  real code, not the plan text. For each unknown, resolve CONFIRMED/REFUTED/UNRESOLVABLE/PARTIALLY_RESOLVED
  with [VERIFIED: source | finding | impact].

Lane 2 — Capability/reuse (SMART):
  Inventory the tools, MCP servers, skills, and existing functions/utilities available in this repo and
  environment. If the plan reinvents something that already exists, escalate as a challenge:
  "plan reinvents X that already exists at <path>".

Lane 3 — External currency (SMART-GATED — trigger on ANY mention of a library, framework, SDK, API,
  program, app, or repo in the plan):
  For EACH external thing mentioned, use Context7 (resolve-library-id + query-docs) for current docs AND
  web (WebSearch/WebFetch) for current best-practices. Escalate any stale assumption as BLOCKING or
  SIGNIFICANT: "plan assumes <old API/pattern>; current docs show <new>".
  SKIP Lane 3 entirely only on a pure-internal plan with no external mention.

Return: resolved unknowns, surfaced contexts (Lane 1/2), reuse findings (Lane 2), currency findings
(Lane 3), and escalated challenges (numbered from C1 locally) in protocol schema format.
```
Merge findings into `iteration_state`; renumber escalated challenges at the Step 3a merge step.

> **Temporal/external-risk probing** is folded into the Empirical Probe phase below. Lane 3 covers documentation/best-practice currency; the Empirical Probe covers live environment state.

## Empirical Probe (required when infra-flagged)

When the plan is infra-flagged (see § Infra Triggers), the researcher (or a dedicated probe agent) MUST collect empirical evidence about the actual environment state — not just the plan text. Run these probes as applicable to the plan's keywords:

1. **Env files:** Read all `.env*` files in the project root (`.env`, `.env.local`, `.env.production`, `.env.staging`). Record every variable name; redact values but log a short hash (first 8 chars) for cross-file comparison.
2. **Vercel env (if plan mentions vercel):** Run `vercel env ls` (scope to team if applicable). If unauthenticated or CLI missing, record "unknown — tool unavailable" explicitly.
3. **Supabase (if plan mentions supabase):** Run `supabase projects list` (CLI at `/home/linuxbrew/.linuxbrew/bin/supabase`) and cross-reference project refs found in `.env*` files. Flag any overlap between dev and prod project refs. Note: there is no `mcp__supabase__*` server on this machine — the only Supabase MCP tool is `search_docs` (docs lookup, not project state), so the CLI is the sole path here, not a fallback. If unauthenticated or CLI missing, record "unknown — tool unavailable" explicitly.
4. **Deploy URL (if plan references production URL / deploy):** `vercel ls` for stable prod URL; `vercel inspect <deployment>` for config. Record the actual resolved URL.
5. **Feature flags (if plan mentions feature flag or rollout):** Grep `process.env.NEXT_PUBLIC_*` and `process.env.*_FLAG` in the codebase; record which flags exist and where they're consumed.

**Output contract:** the verification report MUST contain a section titled exactly `## Empirical Probe Findings`. One bullet per probe, format:
`- <source> | <value or summary> | cross-env match: yes/no/unknown`

Cross-env mismatches (e.g., dev `.env.local` project ref == prod Vercel project ref) are recorded as HIGH severity structural challenges and block PROCEED.

Never silently skip a probe. If a tool is unavailable, record it. If a keyword doesn't apply (e.g., no supabase keyword), skip that specific probe but still add "N/A — not referenced in plan" to keep the section complete.

## Pre-Draft Probe Checklist (upstream contract)

Verification-time probes (§ Empirical Probe above) catch drafted assumptions late. The cheaper move is upstream: **before DRAFTING any infra-flagged plan against an EXISTING system, the plan author runs this read-only probe checklist first.** This section is the canonical home of that contract (formerly `~/.claude/rules/pre-draft-probes.md`); plan authors load it via this skill, and the verification team's job becomes to RATIFY the probes, not to discover them.

**Trigger:** same keyword list as § Infra Triggers above — do not maintain a second list.

**When it applies:**
- Plan modifies, references, or interacts with state on a live cloud system (Supabase project, Vercel deployment, AWS resource, etc.) where the plan body will make claims about that system's current state.
- Plan targets a specific resource by reference (project ref, deployment ID, env name) where assumptions about that target's configuration would be drafted into the plan body.
- Plan adds, removes, or modifies records in a system-managed table (`supabase_migrations.schema_migrations`, vercel project envs, AWS IAM policies, etc.) where the table's existing rows shape the safe-change envelope.

**NOT applicable for:** greenfield infrastructure (nothing to probe); plans touching ONLY local files with no cloud-state dependencies; plans where the existing state IS the spec (pure source-code refactor).

**The probe checklist** (capture empirical answers BEFORE writing the plan body):

1. **Cost / billing model.** If plan provisions or modifies billed resources, probe the actual cost source (`supabase` CLI / project dashboard, `vercel pricing`, etc.) — DO NOT guess units (hourly vs monthly, per-resource vs per-invocation, partial-period prorating rules).
2. **Operator identity baseline.** If plan writes audit-trail fields (`created_by`, `updated_by`, commit author, etc.), probe the EXISTING values used by prior writes — match the dominant identity, do not introduce a new operator email/identity unless explicitly intentional.
3. **Data shape of target tables / fields.** If plan writes specific values to existing records, probe the current shape — empty vs NULL, array vs scalar, populated vs unpopulated fields, default vs explicit. Do NOT assume the shape from system documentation; documentation drifts.
4. **Naming / slug / version stamp conventions.** If plan asserts about IDs, version stamps, slug formats, or filename conventions, probe the actual values in the target system — DO NOT assume system X uses convention Y when system X has been touched by multiple tools/operators over time.
5. **Row counts and known anomalies.** If plan asserts a record count (e.g., "expects 91 rows"), probe the actual count and inspect anomalies. Pre-existing orphan rows, leftover migrations, half-deleted records can invalidate count-based assertions.
6. **Enforcement model for documentation-only mitigations.** If plan proposes a "document in runbook" mitigation, probe whether the runbook will be DISCOVERABLE by the threat actor (typically: a future AI session or operator). Documentation outside the threat's tool path is not a mitigation; it's a memo.

**Output of the probes:** a compact pre-draft notes file (`plans/<plan-name>.pre-draft-probes.md` or a section in the plan's own appendix) capturing verbatim probe results. The plan body cites these as the empirical basis for its assertions, e.g.:

> "Cost: $10/month (verified via `supabase` CLI / project dashboard on YYYY-MM-DD)."
> "Operator email: `contabilidad@cucinanostrasrl.com` (matches all 89 prior CLI-repaired rows on live ledger)."

**Verification-side enforcement:** when verifying an infra-flagged plan, the researcher checks whether a pre-draft probe artifact exists and is cited. Present → ratify it in `## Empirical Probe Findings`. Absent → run the § Empirical Probe set as usual AND record "no pre-draft probes were run" as a process finding.

#### Step 3c: Synthesis Phase

Spawn ONE fresh `Agent(general-purpose)` as the synthesizer — pass `model: "opus"` on the Agent call (verdict quality is opus-tier; do not let it inherit the session model) — with FULL iteration state:

**Do NOT pass a `name` parameter to this `Agent` call.** An unnamed `Agent` call is
synchronous and one-shot: its single return value IS the report. Passing `name` makes the
synthesizer an addressable long-lived teammate that delivers through `teammate-message` and
`idle_notification` channels instead — multiple partial messages, arriving out of order,
some of them stale. `SendMessage`, `teammate-message`, and `idle_notification` traffic is out
of contract for this step and must never be read as, or substituted for, the synthesizer's
report.

```
Read /home/pedro/.claude/skills/verify-plan/challenger-charters/plan-synthesizer.md with the Read
tool — that file is your role charter (ignore its YAML frontmatter plumbing; adopt the convergence-
controller role, method, and output schema it defines).
Evaluate all challenges, unknowns, surfaced contexts, reuse findings, and currency findings.
Iteration state: [full serialized state]
Sanity-check (framing only, no score, no gate) that no Rumsfeld quadrant category was ignored
(Q1 Unknown Knowns / Q2 Known Unknowns / Q3 Known Knowns / Q4 Unknown Unknowns).
Produce: resolution updates, convergence check, directives (or final verdict if converging).
The verdict is driven entirely by BLOCKING/SIGNIFICANT challenge count — do NOT compute a quality score.
```

#### Step 3d: Convergence Evaluation (main thread)

Read Synthesizer output. Update `iteration_state.convergence`.

| Status | Action |
|---|---|
| CONVERGED | Exit loop → Step 4 |
| BLOCKED | Pause for user via the Fail-Loud Checkpoint Protocol below: present blocking challenge + options (provide context / acknowledge risk / override / abort). Feed response into next iteration as `user_response`. |
| CONTINUE | Show progress: `"Iteration N: X resolved, Y remaining. Continuing..."` Auto-continue next iteration. |
| FORCED_EXIT | Exit loop → Step 4 |

**Early convergence shortcut:** If after iteration 1 there are zero BLOCKING challenges AND every challenge status is RESOLVED, DEFERRED, or WITHDRAWN, exit the loop immediately → Step 4. Do not run iterations 2-3. This prevents 30-60K tokens of waste when the first pass resolves everything.

**Between iterations (non-blocking):** Update user with progress summary.

**Redesign checkpoint.** Fires once, before Step 4, when the plan has not converged AND either
iteration 3 just finished or the cross-cycle state's `cycle` is 3 or higher. Challenge counts
are uncapped, so this is the brake on a loop that only patches. Spawn ONE fresh
`Agent(general-purpose)` with `model: "opus"` — not a challenger, not the synthesizer — given the
plan path, the full `iteration_state`, and `closed_findings`. Ask it: "Step back from the
individual challenges. Is this approach converging on a shippable plan, or are the iterations
patching a flawed design and spending tokens without getting closer? Answer KEEP (name the real
defects still being found) or REDESIGN (name the flawed premise and the approach a from-scratch
plan should take)." REDESIGN → the final verdict is RETHINK, with that premise as its first
structural issue. KEEP → Step 4 unchanged; quote its one-line reason in the yield line.

**Write cross-cycle state.** After Step 3d resolves this iteration (CONVERGED,
BLOCKED-pending-response, CONTINUE, or FORCED_EXIT), update
`<phase_dir>/<padded_phase>-cross-cycle-state.json` (creating it if this is iteration 1's first
write):
```bash
git -C "<REPO>" rev-parse HEAD
```

Write:
```json
{
  "cycle": "<the outer cycle now running, NOT the inner iteration>",
  "snapshot_commit": "<the SHA from the command above>",
  "closed_findings": "<carry forward the prior value; append any challenge this cycle's synthesizer marked RESOLVED>",
  "blessed_assertions": "<carry forward the prior value>",
  "last_diff": "<the `diff` object plan-scope.js returned this cycle, or null on cycle 1>",
  "flagged_dependents": "<carry forward the prior value>",
  "inner_loop": {
    "iteration": "<the iteration that just finished>",
    "status": "<CONVERGED | BLOCKED | CONTINUE | FORCED_EXIT>",
    "iteration_state": "<the full live iteration_state object, verbatim>",
    "updated_at": "<ISO-8601 UTC>"
  }
}
```

This write is what makes `CONTINUE` mean "continue" across a compaction. Write it on EVERY
outcome, not only `CONTINUE` — a `CONVERGED` or `FORCED_EXIT` value is what stops the Resume
Check from re-entering a loop that already finished.

Schema: `~/.claude/skills/verify-plan/cross-cycle-state.md`. This step is the only writer of this
file; keep it that way — do not let a second call site re-derive the write.

### Fail-Loud Checkpoint Protocol

The Step 3d BLOCKED pause calls `AskUserQuestion` on the calling thread. If that call
is unavailable, errors, times out, or returns an empty/unparseable/ambiguous result,
treat it as a hard STOP — write the failure state to the on-disk report, surface the
raw error/unavailability in this skill's return text, and NEVER default to any option
(provide context / acknowledge risk / override / abort) as if the user had answered.
A checkpoint that silently no-ops or auto-resolves instead of surfacing to the user is
the same class of silent degradation as a gutted tool pool — it must fail loud, not quiet.

Note this guard is NOT a substitute for the Step 00 capability gate: it fires only on a
BLOCKED verdict, so a run that converges to REVISE never reaches it. Step 00 covers the whole run.

### Step 4: Final Verdict

> **PROCEED-gate (infra-flagged plans only):** If the plan is infra-flagged (see § Infra Triggers) AND the verification report lacks an "Empirical Probe Findings" section with at least one completed probe (not all "unknown — tool unavailable"), the synthesizer MUST downgrade the verdict from PROCEED to REVISE and list "missing empirical probe" as a structural issue. PROCEED is forbidden without probe evidence.

**Yield line (cycle 2+, verdict not PROCEED):** end the reply to Pedro with one line: how many
real defects this cycle found vs wording/churn fixes, and a recommendation: run another cycle, or
replan from scratch. Base it on the count. A cycle still finding real defects means continue — the highest-value
findings have come from late cycles and from outside the narrowed scope; a cycle finding mostly
wording/churn means recommend replanning, since it is spending tokens without moving the plan.

**Verdict-presence + correspondence guard:** write the verdict, Resolution table, and
all sections below to the on-disk report file before returning — this
report is what any caller (plan-perfector, or a human) reads as the source of truth,
never the skill's return text alone. The report's filename timestamp AND an explicit
`cycle: {N}` header line (N = the iteration_state's current iteration/cycle) together
serve as this run's token, so a caller can detect and reject a stale leftover report
from an earlier cycle instead of trusting mere file presence.

Present final output to user:
- **Verdict:** PROCEED / REVISE / RETHINK (driven entirely by BLOCKING/SIGNIFICANT challenge count)
- **Resolution table** — all challenges with final status
- **Surfaced context summary** — relevant findings nobody asked about (incl. Lane 2 reuse + Lane 3 currency findings)
- **Strengths/Opportunities note** — optional 1-2 line report-only highlight from synthesis (no agent, no score)
- **Recommended next step:**
  - PROCEED → "Plan verified, ready to execute"
  - REVISE → "Update plan with changes listed, then re-run /verify-plan"
  - RETHINK → "Fundamental issues found. Rework approach first."

**HANDOFF-VERDICT trailer (machine-parseable, every verdict, every branch — required for the plan-handoff auto-execute mechanism):** append this HTML-comment block to the on-disk report, after the prose sections above, before returning:

```
<!-- HANDOFF-VERDICT
verdict: PROCEED
cycle: 1
blocking_open: 0
significant_open: 0
trivial: false
plan_path: /home/pedro/.claude/plans/<slug>.md
plan_paths: <OPTIONAL — multi-plan runs only; comma-separated, lexicographic. Omit on a single-plan verdict.>
plan_hash: sha256:<hex>
origin_cwd: /home/pedro/Projects/<...>
-->
```

- `verdict:` is always the bare token `PROCEED` / `REVISE` / `RETHINK` — never `PROCEED (trivial)`. Trivial-ness is its own field (`trivial: true`); emit it on the trivial fast path with `blocking_open: 0` / `significant_open: 0` explicit (never omitted).
- `plan_hash` — compute via `node ~/.claude/hooks/lib/plan-hash.js <plan-file-path>` (sha256 hex of the plan body with YAML frontmatter stripped; never mtime — see that script's own header for why). Do not hand-compute this hash.
- `origin_cwd` — the shell's own `pwd` at the point this skill is running (this skill runs inline on the calling thread, so it holds the origin session's cwd).
- `plan_path` / `plan_paths` — **`plan_path` is REQUIRED, singular, and never renamed.** It carries the primary plan and is the key live consumers parse: `hooks/plan-handoff-trigger.js:137` reads `block.plan_path` and skips any artifact lacking it (so a missing key silently disables plan-handoff auto-execute), and `hooks/plan-verifier.js:143` joins artifact→plan on it. `plan_paths` is an ADDITIVE second key for multi-plan verification: a comma-separated list in lexicographic filename order, equal to `plan_path` on a single-plan run. Adding `plan_paths` needs no hook change; renaming or dropping `plan_path` breaks both hooks above.
- **Multi-plan verification MUST commit its report generator under the phase directory**, so the report's numbers stay reproducible.
- Emit this block on **every** verdict — PROCEED, REVISE, RETHINK, and the trivial fast path.
- **This trailer's `verdict`/`blocking_open`/`significant_open` fields are owned exclusively by a completed Step 3 convergence loop (a real fan-out + synthesis).** A later hand-patch/fix-application pass — applying a prior cycle's findings by direct edit, with no fresh fan-out — must NEVER write or edit an existing trailer's `verdict`, `blocking_open`, or `significant_open` fields, even when every finding it fixed is real and every fix is correct. It may append a new `plan_hash` (hashes are safe to update by hand) and a prose fix-summary elsewhere in the report, but the existing trailer block stays exactly as the last real fan-out left it until another real fan-out runs. State plainly that the plan is unverified pending re-run (see `~/.claude/skills/verify-plan/` project usage precedent: a hand-patch round once wrote an optimistic result by hand and it regressed — the fix was to never do that again, not to be more careful doing it). This matters because `plan-handoff-trigger.js` auto-launches execution on `verdict: PROCEED` — a hand-written PROCEED after an un-reviewed patch pass would auto-execute a plan nobody actually re-checked.

**Frontmatter flip on PROCEED:** when the verdict is PROCEED (including the trivial fast path), also `Edit` the plan file's frontmatter to `status: approved` — the same flip plan-perfector's own PROCEED branch performs. This makes the top-level `/verify-plan` path self-sufficient without depending on plan-perfector, and is safe to run alongside plan-perfector's own flip (that skill's PROCEED branch checks current status first and skips its Edit if already `approved` — see its SKILL.md).

### Step 5: Cleanup

No team teardown needed — the challenge phase runs as a `plan-challenge` Workflow call (legacy fallback: parallel `Agent` spawns), not a `TeamCreate` team, so there is no team to delete. Discard `iteration_state` and emit the report.

## Codex Backend (config-gated, additive)

Active ONLY when Step 0 resolved `BACKEND=codex`. This section changes the LEAF call of each role
step — the "ask an agent" — from a Claude Task agent to a `codex-verify.mjs` helper call. It
does NOT change any orchestration: the convergence loop, team sizing, the Step 3a token-budget
batching, gating, directives, and Step-4 assembly all stay exactly as Steps 1-5 describe. Contract reference:
`~/.claude/get-shit-done/references/codex-verification-adapter.md`.

**Editing `codex-roles/*.schema.json`:** OpenAI strict mode requires `required` to list EVERY key in
`properties`. If one is missing, the API returns HTTP 400 before the model runs, and `codex-verify.mjs`
reports it as `PARSE_ERROR` with an empty `raw`. Treat an empty-raw PARSE_ERROR as a schema problem
first. After any schema edit, run `node ~/.claude/get-shit-done/bin/validate-codex-schemas.mjs`
plus one live role call.

### Role → helper mapping

`<REPO>` = the plan's project root (where `gsd-sdk`/the plan live; pass as `--cwd` so Codex can
explore the repo for empirical grounding). `<ROLES>` = `~/.claude/skills/verify-plan/codex-roles`.
Read model/effort/plan_depth once:

```bash
MODEL=$(gsd-sdk query config-get verification.codex.model  2>/dev/null | jq -r '.' 2>/dev/null)
EFFORT=$(gsd-sdk query config-get verification.codex.effort 2>/dev/null | jq -r '.' 2>/dev/null)
DEPTH=$(gsd-sdk query config-get verification.codex.plan_depth 2>/dev/null | jq -r '.' 2>/dev/null)
[ "$MODEL" = "null" ] && MODEL=""   ; [ -z "$MODEL" ]  && MODEL_FLAG=""  || MODEL_FLAG="--model $MODEL"
[ "$EFFORT" = "null" ] && EFFORT="" ; [ -z "$EFFORT" ] && EFFORT_FLAG="" || EFFORT_FLAG="--effort $EFFORT"
[ -z "$DEPTH" ] || [ "$DEPTH" = "null" ] && DEPTH="lite"   # fallback "lite" — config-get reads only project .planning/config.json, so absent-key => lite is what runs by default
```

### Depth branch (`plan_depth`: `lite` default | `faithful` opt-in)

`DEPTH` selects which codex role set + iteration model runs. The Claude backend path is unaffected —
depth is a codex-only switch.

- **`lite` (default):** run the **3 merged roles** below — `adversarial`, then `empirical-domain`,
  then `structural-verdict` — as a SINGLE pass, NO convergence loop, NO iterations 2-3. Pass
  `first iteration` as `{{ITERATION_STATE}}` to the adversarial + empirical-domain roles; pass the
  accumulated iteration_state (their folded findings/audits/contexts/risks) to `structural-verdict`,
  which emits the final PROCEED/REVISE/RETHINK verdict and the plan-perfector summary. Each call
  passes `--timeout 360000`. Same RC=3 CODEX_UNAVAILABLE pre-flight checkpoint as the faithful path
  (Exit-code policy below). The faithful 7-role table and the convergence-loop substitution
  (Steps 3a-3d) do NOT run.
- **`faithful` (opt-in):** the EXISTING role × 3-iteration convergence path — the role→helper
  table and per-call/synthesizer/exit-code procedures below — mirrors the Claude orchestration.

#### LITE role → helper mapping (3 merged roles, single pass)

| Order | Merged role (source roles) | prompt + schema | Folds into iteration_state |
|---|---|---|---|
| 1 | **adversarial** (challenger + devils-advocate) | `adversarial.prompt.md` + `adversarial.schema.json` | `.raw.findings` → challenges (origin `adversarial`); `.raw.assumption_audit` → assumption_audit |
| 2 | **empirical-domain** (domain-expert + researcher Lanes 1/2/3) | `empirical-domain.prompt.md` + `empirical-domain.schema.json` | `.raw.findings` → challenges (origin `empirical-domain`); `.raw.unknowns_resolutions` → unknowns; `.raw.surfaced_contexts` → surfaced_contexts; `.raw.reuse_findings`/`.raw.currency_findings` → surfaced_contexts |
| 3 | **structural-verdict** (granularity-checker + synthesizer) | `structural-verdict.prompt.md` + `structural-verdict.schema.json` | `.raw.verdict` → final verdict; `.raw.granularity_findings` → granularity_findings; `.raw.resolution_table` updates statuses; `.raw.findings` (OPEN/UNRESOLVED) → plan-perfector `issue_count`/`challenge_topics` |

LITE per-call procedure is identical to the faithful **Per-call procedure** below (assemble prompt →
temp file → `codex-verify.mjs` call with `--timeout 360000` → branch on `$RC` → fold `.raw`). The
ONLY differences: exactly 3 calls, no loop, no conditional gates. `structural-verdict` is the LITE
analog of the synthesizer for Step-4 assembly — read `.raw.verdict` directly (it is never null in
LITE) and build the plan-perfector summary from its normalized `issue_count`/`challenge_topics`. The
report file (and `## Empirical Probe Findings` for infra-flagged plans) is written the same way; the
empirical-domain role collects the probes.

#### FAITHFUL role → helper mapping (convergence loop — runs only when `DEPTH=faithful`)

| Step | Claude path (unchanged orchestration) | Codex substitution: prompt + schema |
|---|---|---|
| 3a challenger | Task `plan-challenger` | `challenger.prompt.md` + `challenger.schema.json` |
| 3a domain-expert (SCALED) | Task `plan-domain-expert` (parallel Agent) | `domain-expert.prompt.md` + `domain-expert.schema.json` (parallel — one fanout call) |
| 3a devils-advocate | Task `plan-devils-advocate` (parallel Agent) | `devils-advocate.prompt.md` + `devils-advocate.schema.json` (parallel — one fanout call) |
| 3a granularity | Task `plan-granularity-checker` | `granularity-checker.prompt.md` + `granularity-checker.schema.json` |
| 3b research (Lanes 1+2+3) | Task `plan-researcher` | `researcher.prompt.md` + `researcher.schema.json` — ONE consolidated call doing all 3 lanes |
| 3c synthesis | Task `plan-synthesizer` | **not substituted — always runs on Claude** (fresh `Agent(general-purpose)`, `model: "opus"`, charter under `challenger-charters/`). `synthesizer.prompt.md` + `synthesizer.schema.json` remain on disk for `lite`/legacy use only. |

The conditional-execution gates are unchanged and decided by the orchestrator BEFORE the call:
granularity skips on iter 2-3 unless `RE-GRANULARITY`/plan-revised; research Lane 1 (unknowns
resolution) runs only if the unknowns manifest is non-empty; research re-runs on iter 2-3 only on a
`RE-SWEEP`/`RE-PROBE` directive. If a gate says skip, do NOT make the codex call.

### Per-iteration fan-out procedure

Every challenge role for one iteration runs in ONE `codex-verify-fanout.mjs` call, concurrently —
structurally identical to the Claude path's own concurrency documented below. The synthesizer is
NOT in the fanout: it always runs on Claude (Step 3c), never on codex.

1. **Select this iteration's roles.** The conditional gates are unchanged and still decided
   BEFORE the call: challenger always; `domain-expert` only on a SCALED team;
   `granularity-checker` only when the granularity gate permits; `researcher` only when the
   research gate permits; `devils-advocate` only on iteration 1 or when the directives carry
   `RE-ADVERSARIAL`; `domain-expert` additionally requires `RE-DOMAIN` on iterations 2-3.
   Iteration 1 BASE → `challenger,devils-advocate,researcher,granularity-checker`; SCALED adds
   `,domain-expert`. Iterations 2-3 BASE typically → `challenger,researcher` plus whichever of
   `devils-advocate` / `granularity-checker` the directives re-enable.
2. **Write the iteration state.** On iterations 2-3, serialize the current `iteration_state` to an
   absolute JSON file under `~/.claude/cache/codex-verify/` and pass it as `--iteration-state`. On
   iteration 1 pass nothing — the runner substitutes the literal `first iteration`, and passing
   `--iteration-state` on iteration 1 is a usage error (exit 2).
3. **Call the fan-out runner once:**
   ```bash
   node ~/.claude/get-shit-done/bin/codex-verify-fanout.mjs \
     --roles "<selected roles, comma-separated>" --iteration <n> \
     --plan "<ABS_PLAN_PATH>" [--plan "<ABS_PLAN_PATH_2>" ...] --cwd "<REPO>" --roles-dir "<ROLES>" \
     $MODEL_FLAG $EFFORT_FLAG --timeout 360000 [--iteration-state "<statefile>"] \
     [--cross-cycle-state "<abs path>"] \
     [--scope "<phase_dir>/<padded_phase>-scope.json" (written by Step 3's plan-scope.js call, iteration 2-3 only)]
   RC=$?
   ```
   Pass `--cross-cycle-state` whenever `<phase_dir>/<padded_phase>-cross-cycle-state.json` exists.
   Absent on cycle 1; required from cycle 2 onward. Format:
   `~/.claude/skills/verify-plan/cross-cycle-state.md`.

   **Multi-plan:** `--plan` is repeatable — pass it once per plan file. The runner concatenates the
   plan contents in the order given, so pass them in lexicographic filename order (23-01, 23-02,
   23-03). A single plan is just one `--plan`.
   The runner assembles each role prompt itself (`{{PLAN_PATH}}`, `{{PLAN_CONTENT}}`,
   `{{ITERATION_STATE}}`), writes it to `~/.claude/cache/codex-verify/<role>-<iteration>-<timestamp>.prompt.md`,
   and caps in-flight children at `--concurrency` (default 5). Do NOT hand-assemble prompts or call
   `codex-verify.mjs` once per role — that sequential path is what this replaces.

   stdout is `{"results": {"<role>": {"exit_code": N, "output": <helper stdout>}}, "challenge_topics": [...]}`.
   Read each role's `exit_code` and apply the Exit-code policy below to it exactly as if it were a
   single call's `$RC`; the runner's own `RC=3` means at least one role hit CODEX_UNAVAILABLE.
   Build `role_outputs` in selected-role order from the entries with `exit_code === 0` and a
   non-null `output`, and feed only those to the merge in step 5 and then to the Claude synthesizer.
   The runner's **top-level `challenge_topics` is intermediate scheduling data only** — never pass
   it to plan-perfector; plan-perfector's `issue_count` and `challenge_topics` come exclusively
   from the final synthesizer call (see Step-4 summary below).
4. **Branch on `$RC`** (see Exit-code policy). On `RC=0`, parse stdout JSON.
5. **Fold back into `iteration_state`.** The helper's normalized stdout exposes only
   `verdict`, `findings[]`, `issue_count`, `challenge_topics`, `raw`. The full role contribution
   (assumption_audit, unknowns_manifest, surfaced_contexts, reuse_findings, currency_findings,
   granularity findings, resolution_table, convergence, directives) lives in
   `.raw` — **re-parse `.raw` with the SAME defensive extraction the helper uses** (direct
   `JSON.parse` → fenced ```json block → first balanced `{...}` span; in practice `.raw` is bare
   JSON, but match the helper so a future fenced/prose-wrapped `.raw` does not break folding) and
   merge each block into `iteration_state` in the SAME
   shape the Claude agent would have returned (per protocol.md schemas). Apply the SAME merge rules
   the Claude path applies — merge every challenge; volume is bounded by token-budget batching at
   the Step 3a merge, not by a challenge-count ceiling.

### Synthesizer is special (loop control)

In codex mode the synthesizer runs on Claude, so Step 3c returns the synthesizer object directly —
read `convergence.status` and `directives[]` off it and drive Step 3d as on the Claude path. The
`.raw` re-parse below applies to any codex-run verdict-bearing call (`lite` mode's
structural-verdict, or a legacy codex synthesizer run):

`normalize()` puts `verdict` at top level, but the **loop branches on `convergence.status`**, which
lives only in `.raw`. After such a call:

- Re-parse `.raw` (same defensive extraction: direct → fenced → balanced span); read
  `convergence.status` (`CONTINUE|CONVERGED|BLOCKED|FORCED_EXIT`) and drive
  Step 3d exactly as the Claude path's Synthesizer output would (CONVERGED/FORCED_EXIT → Step 4;
  BLOCKED → user pause; CONTINUE → next iteration).
- Read `.raw.directives[]` to gate the next iteration's conditional roles (RE-SWEEP / RE-PROBE /
  RE-GRANULARITY / RE-ADVERSARIAL / RE-DOMAIN). If you drop `.raw` and read only the normalized `verdict`, the
  directives vanish and iter-2/3 gating silently breaks — do not do that.
- The early-convergence shortcut (Step 3d) still applies: after iteration 1, if zero BLOCKING and
  every challenge is RESOLVED/DEFERRED/WITHDRAWN, exit to Step 4.

### Exit-code policy

| RC | Meaning | Action |
|---|---|---|
| 0 | OK | Parse + fold as above. |
| 3 | CODEX_UNAVAILABLE | **Pre-flight gate** (fires at the auth probe on the first call). Raise the notify-and-decide `AskUserQuestion`: **[Stop — I'll run `codex login`]** vs **[Proceed with Claude verification this run]**. "Proceed" → abandon the codex path and run Steps 1-5 as the unmodified Claude flow (`BACKEND=claude`) from here. "Stop" → return the structured summary with `verdict: HALTED`, `key_issues: ["Codex unavailable — run codex login, then re-verify"]`, `report_path: null`, `issue_count: 0`, `challenge_topics: []`, and do NOT run any verification this turn. Never silently skip. |
| 4 | PARSE_ERROR | Surface to the user (one line + the role/iteration). Treat that role's output as **ABSENT** for this iteration — do NOT fabricate findings. The synthesizer notes the coverage gap; the loop CONTINUES. |
| 1 / 2 | ERROR / usage | Same as RC=4: surface, treat the role as absent, continue. (RC=2 is a caller bug — fix the invocation.) |

A single rule covers 4/1/2: *non-OK and non-3 → surface + role-absent + continue.* Exit 3 is the
only code that triggers the user checkpoint.

### Step-4 summary for plan-perfector (codex mode)

Step 4 returns the SAME structured summary plan-perfector consumes — `verdict`, `report_path`,
`key_issues`, `issue_count`, `challenge_topics` — built from the FINAL verdict-bearing call: the
**synthesizer** call in `faithful` mode, or the single **structural-verdict** call in `lite` mode
(both expose `.raw.verdict` and a `findings[]` of OPEN/UNRESOLVED challenges, so the contract is
identical):

- `verdict` ← `.raw.verdict` (PROCEED|REVISE|RETHINK).
- `issue_count` ← the helper's `issue_count` from that final call (= count of OPEN/UNRESOLVED
  challenges in its `findings[]`).
- `challenge_topics` ← the helper's `challenge_topics` from that final call (already
  lowercase-slugified, deduped, lexicographically sorted → byte-identical for identical findings, so
  plan-perfector's oscillation guard does not false-trip). Do NOT re-derive from the union of every
  per-role finding — use the final call's normalized topics so the field matches the existing
  contract.

> **LITE & plan-perfector oscillation guard:** unaffected — plan-perfector compares
> `issue_count`/`challenge_topics` across its OWN cycles (each = one full `/verify-plan` invocation),
> and a LITE run returns a terminal verdict + a stable `findings[]`.
- `report_path` ← the verification report path (write the same report file the Claude path writes,
  including all the Step-4 sections; for infra-flagged plans the `## Empirical Probe Findings`
  section is still required — instruct the researcher role to collect those probes).

> Caveat: cross-run determinism of `challenge_topics` depends on the synthesizer reusing stable
> challenge titles (the prompt instructs this); the slug normalization guarantees byte-identical
> output only for textually-identical titles. This is the documented residual oscillation risk.

## User Checkpoint Protocol

Team runs autonomously except:
1. **BLOCKING challenge** unresolvable by agents → pause, present options
2. **Convergence stall** (no progress between iterations) → pause, confirm continue
3. **Max iterations** with unresolved BLOCKING → pause, present findings
4. **Contradictory research findings** → pause, request tiebreak

Between iterations (non-blocking): `"Iteration N: X resolved, Y remaining. Continuing..."`

## Communication Topology

Main thread is the hub. No direct agent-to-agent messaging — so a debating `TeamCreate` team is unnecessary; the fan-out runs inside the `plan-challenge` Workflow (legacy fallback: parallel `Agent` spawns). Each worker receives full input from the main thread (via workflow args) and returns output to the main thread (via the workflow envelope).

```
Main Thread (orchestrator + state holder)
    │  Step 3a fan-out (concurrent, one message):
    ├──[spawn]──→ Challenger        ──[return]──→ challenges (local C1..) + unknowns
    ├──[spawn]──→ Granularity       ──[return]──→ granularity findings + escalations (local C1..)   (gated)
    ├──[spawn]──→ Researcher        ──[return]──→ Lane1/2/3 findings + escalations (local C1..)
    ├──[spawn]──→ Devils-advocate   ──[return]──→ black-swan challenges (local C1..)   (gated)
    ├──[spawn]──→ Domain-expert*    ──[return]──→ domain challenges (local C1..)   (*SCALED only, gated)
    │  [merge] renumber ALL challenge IDs globally, no count ceiling
    ├──[spawn]──→ Synthesizer       ──[return]──→ resolution state + convergence  (runs LAST)
    └──[evaluate]──→ CONVERGED? / PAUSE? / CONTINUE?
```

## Token Budget

| Scenario | Typical | Worst Case |
|---|---|---|
| Trivial plan | ~2K | ~2K |
| Base team, 1 iteration | ~80K | ~100K |
| Base team, 2 iterations | ~114K | ~151K |
| Base team, 3 iterations | ~148K | ~215K |
| Scaled team, 1 iteration | ~89K | ~104K |
| Scaled team, 2 iterations | ~144K | ~189K |
| Scaled team, 3 iterations | ~188K | ~263K |

Devils-advocate (Step 3a) is a BASE worker (sonnet) on iteration 1 — the base-team figures above include one extra sonnet challenge worker (~+18K on iteration 1). On iterations 2-3 it is gated at spawn time like granularity: it is not started at all unless the synthesizer's directives carry `RE-ADVERSARIAL`; the base-team rows assume that. Under budget pressure it is dropped only AFTER granularity (challenger + researcher stay mandatory; the ~70K abort floor is unchanged). The SCALED rows have NOT been re-measured for `run_domain_expert` gating — gating domain-expert should reduce scaled iteration-2/3 cost, but by how much is unverified, so the scaled figures stand as a worst case.

Granularity checker (Step 3a) adds ~+7K per iteration it runs. On iterations 2-3 it is conditional (only re-runs on `RE-GRANULARITY` directive or plan revision), so total granularity overhead across 3 iterations is capped at ~+18K.

## Integration Points

- Terminal check before execution
- **RETHINK** → rework the plan's approach first
- **REVISE** → user updates plan, optionally re-runs /verify-plan. For a manual re-run: apply all accepted fixes in one batch, check the cheap signal first, re-run only the charters whose area changed, and brief them with anchors plus the diff since the last cycle. If the verdict is the same 3 re-runs in a row, stop and tell Pedro. (plan-perfector `--fix` has its own max_cycles and oscillation guard.)
- **PROCEED** → ready to execute

## Anti-Patterns — Do NOT

- **Check structure** — YAML fields, empty sections; out of scope here (gsd-plan-checker retired 2026-07-09, not replaced)
- **Review code** — multipersona-auditor's job
- **Skip complexity assessment** — always assess before team sizing
- **Research inside plan-challenger** — unknowns manifest only, research delegated to plan-researcher
- **Let granularity checker rewrite the plan** — it flags issues with concrete example_fix suggestions, but revision is plan-perfector's job. The checker diagnoses, it does not treat.
- **Demand pseudocode in plan steps** — naming files, operations, and done criteria is sufficient granularity. The checker should NOT flag steps for lacking implementation details like variable names or line-by-line code.
