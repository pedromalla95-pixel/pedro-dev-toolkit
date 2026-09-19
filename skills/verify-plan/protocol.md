# Verify-Plan V2 — Inter-Agent Protocol

**Purpose:** Defines all message schemas, resolution states, convergence criteria, and tool budgets
for the verify-plan V2 convergence loop. This is the single source of truth. ALL agents reference
this file. No agent may define its own schema variants.

---

## 1. Challenge Schema

```yaml
# One block per challenge. id increments globally across all iterations.
challenge:
  id: "C{N}"                        # e.g. C1, C2, C12
  origin: "challenger|domain-expert|devils-advocate|surfaced|probed|granularity"
  claim: "The plan assumes X"
  concern: "This fails when Y because Z"
  failure_scenario: "Concretely: if Y occurs, then Z breaks, causing W"
  alternative: "Instead, consider A which avoids Z by doing B"
  severity: "BLOCKING|SIGNIFICANT|MINOR"
  confidence: "HIGH|MED|LOW"
  status: "OPEN|RESOLVED|UNRESOLVED|DEFERRED|WITHDRAWN"
  resolution: "How it was settled — evidence, debate outcome, or user decision"
  iteration_introduced: 1           # integer
```

---

## 2. Unknowns Manifest Schema

```yaml
# Researcher Lane 1 (RESOLVE) generates these. One block per unknown.
unknown:
  id: "U{N}"                        # e.g. U1, U4
  description: "What we don't know and need to know"
  type: "FILE_MISSING|API_BEHAVIOR|PRIOR_DECISION|STALE_KNOWLEDGE|INTEGRATION_UNKNOWN"
  affects_challenge: "C{N}"        # which challenge this unblocks
  suggested_query: "Exact query to run in Grep or WebSearch"
  resolution: "CONFIRMED|REFUTED|UNRESOLVABLE|PARTIALLY_RESOLVED"
  finding: "[VERIFIED: source | finding | impact]"
```

---

## 3. Surfaced Context Schema

```yaml
# Researcher Lane 1 (SURFACE) generates these. One block per finding from memory/codebase/history.
surfaced_context:
  id: "S{N}"                        # e.g. S1, S3
  source: "memory|codebase|claude_md|git_history"
  location: "Exact path, key, or commit hash"
  relevance: "Why this matters to the current plan"
  impact: "changes_needed|confirms_approach|contradicts_plan"
  generates_challenge: "C{N}|null"  # if this finding spawns a new challenge
```

---

## 4. Probed Risk Schema

```yaml
# The empirical-probe phase generates these (old Researcher Mode 3 PROBE). One block per identified risk.
probed_risk:
  id: "P{N}"                        # e.g. P1, P2
  risk: "What could go wrong"
  trigger: "The specific condition that activates this risk"
  cascade: "Downstream consequences if trigger fires"
  probability: "LOW|MED"
  severity: "BLOCKING|SIGNIFICANT|MINOR"
  generates_challenge: "C{N}|null"  # if this risk escalates to a challenge
```

---

## 5. Iteration State Schema

```yaml
# Synthesizer maintains this across all iterations. Full state per iteration.
iteration_state:
  iteration: 1                      # integer, starts at 1
  plan_path: "/absolute/path/to/plan.md"
  plan_quality: "RICH|ADEQUATE|THIN"
  team_mode: "base|scaled"

  challenges:                       # full list — include all, mark status
    - id: "C1"
      status: "OPEN"
      severity: "BLOCKING"

  unknowns:                         # all unknowns this iteration
    - id: "U1"
      resolution: "CONFIRMED"

  surfaced_contexts:                # all surfaced findings this iteration
    - id: "S1"
      impact: "contradicts_plan"
      generates_challenge: "C3"

  probed_risks:                     # all probed risks this iteration
    - id: "P1"
      generates_challenge: "null"

  convergence:
    blocking_open: 0                # count of BLOCKING challenges with status OPEN or UNRESOLVED
    significant_open: 0             # count of SIGNIFICANT challenges with status OPEN or UNRESOLVED
    status: "CONTINUE|CONVERGED|BLOCKED|FORCED_EXIT"

  assumption_audit: []               # populated by challenger Phase 2b; empty until first iteration

  coverage_gaps: []                 # UNVERIFIED-GATE lines copied from Step 2.5 pre-flight; empty if none reported

  user_responses: []                # list of user clarifications received this iteration
  synthesizer_directives: []        # instructions synthesizer passed to next iteration

  rumsfeld_coverage:                # Q1-Q4 quadrant framing; populated each iteration
    Q1_unknown_knowns: ""
    Q2_known_unknowns: ""
    Q3_known_knowns: ""
    Q4_unknown_unknowns: ""

  granularity_findings:            # null until Step 3a2 runs; populated each iteration it runs
    findings: []                   # list of granularity_finding objects (all, not just escalated)
    run_this_iteration: true       # bool — false if skipped on iteration 2-3
    dimensions_checked: []         # which of 6 dimensions were assessed
    escalated_count: 0             # how many findings generated challenges
```

---

## 6. Resolution States

| State | Meaning |
|-------|---------|
| **OPEN** | Challenge is new, not yet evaluated by any agent |
| **RESOLVED** | Research finding, user input, or agent debate settled the challenge with a clear mitigation or confirmation it is not a real risk |
| **UNRESOLVED** | Confirmed real risk with no viable mitigation identified. Counts against convergence. |
| **DEFERRED** | MINOR severity only. Not worth iterating on. Flagged in report but does not block convergence. |
| **WITHDRAWN** | Challenge was shown to be invalid — based on incorrect assumption or already handled. |

---

## 7. Convergence Criteria (Binary Resolution Tracking)

Convergence is determined by counting OPEN + UNRESOLVED challenges by severity bucket.
Scores are irrelevant to loop control. Binary counts only.

| Status | Trigger | Action |
|--------|---------|--------|
| **CONVERGED → PROCEED** | Zero BLOCKING open/unresolved + Zero SIGNIFICANT open/unresolved | Synthesizer writes final report with PROCEED verdict |
| **CONVERGED → REVISE** | Zero BLOCKING open/unresolved + 1-2 SIGNIFICANT open/unresolved | Final report includes REVISE verdict with mitigations noted inline |
| **BLOCKED → PAUSE** | Any BLOCKING challenge is OPEN or UNRESOLVED | Loop pauses, Synthesizer surfaces specific question(s) to user |
| **BLOCKED → REVISE** | Any BLOCKING open/unresolved that is localized AND mechanically checkable AND has a named concrete edit — all three, verified by the gate pre-flight, not asserted | Synthesizer emits `verdict: REVISE` with `convergence.status: BLOCKED`; the blocker and its named edit go in `summary` |
| **FORCED_EXIT → RETHINK** | Any BLOCKING still OPEN or UNRESOLVED after iteration 3 | Loop exits, Synthesizer writes RETHINK verdict — plan needs fundamental revision |

The `BLOCKED → REVISE` rung is a deliberate softening and is valid ONLY while the Step 2.5 gate
pre-flight is in place — it is what converts "mechanically checkable" from an assertion into a
verified fact. If that pre-flight is removed, remove this rung with it.

---

## 8. Convergence Guards

These rules keep the loop converging. Volume is bounded by batching at the Step 3a merge
(see `SKILL.md` § Step 3a), not by a challenge-count ceiling.

- **Net resolution rule:** Each iteration MUST resolve more challenges than it creates. If it creates more than it resolves, this is a **DEGRADATION** event — Synthesizer flags it and tightens scope for the next iteration.

**Removed 2026-09-16 — do not reinstate without reading the spec.** The 8-active-challenge
ceiling, the per-agent new-challenge ceilings (challenger 5, researcher 2, granularity 3),
and the `DEFERRED_SURFACED` tag are gone. They dropped real findings by count, and a dropped
finding returned as a fresh top-level invocation — more expensive than the tokens the cap
saved. Per-call context is now bounded by token-budget batching within a single iteration,
which defers nothing. `DEFERRED_PROBED` and every other substantive defer tag survive:
those record a role deferring something for a reason, not for a size limit.

---

## 9. Rumsfeld Matrix Framing

The four Rumsfeld quadrants are an organizing framing for the categories of knowledge gap the verification loop addresses. They name where each agent's output lands; they carry no scoring and no gate.

| Quadrant | Name | Category of knowledge gap |
|----------|------|---------------------------|
| Q1 | Unknown knowns | Context that exists (memory, codebase, history) but the plan failed to surface |
| Q2 | Known unknowns | Things the plan acknowledges it doesn't know and needs to resolve |
| Q3 | Known knowns | Assumptions the plan treats as fact, requiring validation |
| Q4 | Unknown unknowns | Risks the plan hasn't anticipated at all |

---

## 10. Assumption Audit Schema

```yaml
# Challenger Phase 2b generates these. One block per assumption.
assumption_audit:
  id: "A{N}"                          # e.g. A1, A3
  assumption: "What the plan treats as fact"
  source: "Plan section, unstated, or inferred"
  status: "VALIDATED|CHALLENGED|STALE|UNVERIFIED"
  evidence: "Specific source that confirms, contradicts, or fails to confirm"
  escalated_to: "C{N}|U{N}|null"     # challenge ID if CHALLENGED, unknown ID if STALE/UNVERIFIED
```

---

## 11. Research Tool Budget (Per Iteration Ceiling)

Budgets are **per agent per iteration**. Exceeding budget requires Synthesizer approval in the iteration state directives. The researcher runs three lanes; Lane 1 targets open challenges (RESOLVE) and scans for missed context (SURFACE), Lane 2 audits available capability/reuse, Lane 3 checks external currency. The old Mode 3 PROBE folds into the empirical-probe phase and is not budgeted here.

### Lane 1 — Code / Docs (codebase + plan + memory + history)
| Tool | Limit |
|------|-------|
| Read | 17 |
| Grep | 9 |
| git log | 1 |

### Lane 2 — Capability / Reuse (available tools, MCPs, skills, utilities)
| Tool | Limit |
|------|-------|
| Read | 5 |
| Grep | 4 |

### Lane 3 — External Currency (smart-gated)
| Tool | Limit |
|------|-------|
| Context7 `resolve-library-id` | 3 (one per library) |
| Context7 `query-docs` | 3 (one per resolved library) |
| WebSearch | 4 |
| WebFetch | 2 |

### Combined Totals (All Three Lanes)
| Tool | Total Ceiling |
|------|---------------|
| Read | 22 |
| Grep | 13 |
| git log | 1 |
| Context7 (resolve + query) | 3 libraries (1 resolve + 1 query each) |
| WebSearch | 4 |
| WebFetch | 2 |

**Lane 3 gating:** Context7 is the default for any library/framework/SDK in scope (resolve-library-id
then query-docs, max 3 libraries/turn per the global Context7 mandate). WebSearch/WebFetch cover
best-practices and temporal risks (deprecation, versioning) and are permitted for unknown types
`API_BEHAVIOR`, `STALE_KNOWLEDGE`, and `INTEGRATION_UNKNOWN`. External tools are NEVER used for
questions answerable from the local codebase or file-based memory — Lane 1 first.

---

## 12. Granularity Finding Schema

Produced by `plan-granularity-checker` (Step 3a2). Checks whether plan steps are decomposed to a level where any model can execute them mechanically.

### 6 Dimensions

| ID | Dimension | What It Catches |
|----|-----------|----------------|
| G1 | Step Atomicity | Steps with multiple logical operations that should be split |
| G2 | Intermediate Step Coverage | Logical leaps where unstated work must happen between steps |
| G3 | Ambiguity Detection | Vague action verbs, unspecified parameters, "as needed" language |
| G4 | Success Criteria | Steps lacking a mechanically verifiable done condition |
| G5 | Dependency Explicitness | Implicit ordering assumptions, unreferenced step outputs |
| G6 | Handoff Clarity | Unspecified interfaces between producing and consuming steps |

### Finding Format

```yaml
granularity_finding:
  id: "G{N}"                    # Sequential: G1, G2, G3...
  dimension: "atomicity|intermediate_gaps|ambiguity|success_criteria|dependency_explicitness|handoff_clarity"
  plan_location: "Step N / Phase X / Section Y"
  issue: "Specific problem with evidence from plan text"
  quoted_text: "Exact plan text being flagged"
  example_fix: "Concrete rewrite or decomposition showing the fix"
  severity: "BLOCKING|SIGNIFICANT|MINOR"
  generates_challenge: "C{N}|null"
```

### Severity Definitions

- **BLOCKING**: Step is unexecutable as written. No reasonable model could determine what to do. Example: "Implement the feature" with no further detail.
- **SIGNIFICANT**: Step is ambiguous enough that a weaker model would make the wrong judgment call. Example: "Validate input" without specifying which fields or what schema.
- **MINOR**: Step could be slightly clearer but a competent executor can fill the gaps. Report-only, never escalated to challenges.

### Escalation

- BLOCKING and SIGNIFICANT findings escalate to challenges with `origin: "granularity"`
- Challenge IDs continue from the last challenger-assigned ID
- No escalation-count ceiling: rank by severity (BLOCKING > SIGNIFICANT), then plan position (earlier > later), and report them in that order

### Graduated Threshold

Plan complexity affects which findings escalate:
- **<=3 steps**: Only BLOCKING escalates. SIGNIFICANT is report-only.
- **4-7 steps**: BLOCKING + SIGNIFICANT escalate normally.
- **8+ steps**: Full escalation + meta-finding if plan needs decomposition.

---

## 13. Tests Contract Audit (TC-1)

Produced by `plan-challenger` during Phase 2b (Assumption Audit). Checks that code-change plans carry a machine-readable `## Tests` section as required by `~/.claude/rules-ref/plan-contract.md` §3-§4 (the canonical contract; the constitution's `## Plan Mode Contract` points there).

### Scope Gate — "Is this a code-change plan?"

Auditor answers YES if ANY of:
- Plan body contains file-path references matching `\.(js|ts|tsx|jsx|py|go|rs|rb|java|cs|cpp|c|sh|sql)$`.
- Plan body contains verbs: `implement`, `refactor`, `fix bug`, `add feature`, `build`, `write code`, `rewrite`, `port`.
- Plan has execution steps that Write or Edit source files (as opposed to `.md`, `.json` config, `.yaml` config).

If NO (pure docs/config/research), auditor records `TC-1: EXEMPT` and skips the rest.

### Check

Parse `## Tests` section from plan body.

**Valid exemption marker (PASS immediately):**
```
tests: n/a (docs-only|config-only|research-only|planning-only)
```
Any other `tests: n/a (...)` value is rejected → BLOCKING.

**Valid specs (required if no exemption):**
- Section exists AND contains ≥3 bullet lines.
- Each spec bullet matches: `- [RED|GREEN|EDGE]  <behavior>  → <expectation>` (whitespace flexible).
- Each spec names a testable behavior (non-empty on both sides of `→`).

### Finding Format

```yaml
tests_contract_finding:
  id: "TC-1"
  plan_is_code_change: true|false
  section_present: true|false
  exemption_marker: "docs-only|config-only|research-only|planning-only|null"
  spec_count: <integer>
  valid_specs: <integer>              # specs that parse cleanly with phase tag + behavior + expectation
  phase_coverage:
    red: <int>
    green: <int>
    edge: <int>
  issues: []                          # list of per-spec problems (missing tag, vague behavior, no expectation)
  severity: "BLOCKING|SIGNIFICANT|MINOR|EXEMPT"
  generates_challenge: "C{N}|null"
```

### Escalation

| Condition | Severity | Verdict Impact |
|-----------|----------|----------------|
| Plan is code-change AND `## Tests` section missing entirely | **BLOCKING** | Blocks PROCEED. Forces REVISE; if unresolvable after 3 cycles → RETHINK. |
| Section present, but invalid/unrecognized exemption marker | **BLOCKING** | Blocks PROCEED. |
| Section present, code-change plan, valid_specs < 3 | **SIGNIFICANT** | Blocks PROCEED. plan-perfector auto-REVISE fleshes out specs from plan context. |
| Section present, specs have missing phase tags or vague behaviors | **SIGNIFICANT** | Blocks PROCEED. |
| Section present, valid_specs ≥ 3, all phases ([RED] + [GREEN] + [EDGE]) represented | **PASS** | No challenge generated. |
| Plan is docs-only/config-only/research-only AND exemption marker present | **EXEMPT** | No challenge generated. Recorded as `TC-1: EXEMPT` in report. |

### Convergence

TC-1 escalations ride the existing binary convergence tracking (§ 7) — they count toward BLOCKING/SIGNIFICANT open/unresolved tallies just like any other challenge. No new verdict logic.

### Auto-REVISE Hint for plan-perfector

When TC-1 generates a SIGNIFICANT challenge for "weak specs", plan-perfector SHOULD, in the next cycle, have the planner extract candidate test behaviors from the plan's Objective / Context / Tasks and propose concrete RED/GREEN/EDGE bullets. It MUST cite behaviors present in the plan body — hallucinated behaviors are a re-escalation condition.

### Interaction with Existing Sections

- Does NOT require a new agent. Runs inside `plan-challenger` Phase 2b alongside existing Assumption Audit.
- BLOCKING TC-1 always wins over MINOR/SIGNIFICANT items from other origins.

---

## 14. Codex Backend — Role-Call Substitution & Folding (additive)

The schemas in §§ 1-13 are engine-agnostic — they define the SHAPE of each agent's output
regardless of whether a Claude Task agent or the Codex CLI produces it. When `verify-plan` resolves
`BACKEND=codex` (SKILL.md § Step 0), each per-role Task spawn is replaced by a call to
`~/.claude/get-shit-done/bin/codex-verify.mjs` (contract:
`~/.claude/get-shit-done/references/codex-verification-adapter.md`). The convergence loop, convergence
criteria (§ 7), resolution states (§ 6), and Rumsfeld-framing/granularity logic are UNCHANGED — only the leaf
"ask an agent" call changes engine.

### Depth modes (`verification.codex.plan_depth`, fallback `lite`)

The codex backend resolves `plan_depth` and runs ONE of two role sets. Both produce engine-agnostic
output conforming to §§1-13; both expose the same Step-4 plan-perfector contract.

- **`faithful`** — the 7 roles below across the 3-iteration convergence loop (Steps 3a-3d). Opt-in.
- **`lite` (default)** — 3 merged roles, single pass, NO convergence loop. Each merged role inlines
  the briefs of its source roles into one adversarial pass; `structural-verdict` emits the verdict.

#### LITE role → prompt + schema (3 merged roles)

| Merged role | prompt template | schema | Merges (source roles) |
|---|---|---|---|
| Adversarial (§1, §10) | `adversarial.prompt.md` | `adversarial.schema.json` | challenger + devils-advocate |
| Empirical-domain (§1, §2, §3, §4) | `empirical-domain.prompt.md` | `empirical-domain.schema.json` | domain-expert + researcher (Lane 1 RESOLVE/SURFACE + empirical-probe) |
| Structural-verdict (§5, §6, §7, §9, §12) | `structural-verdict.prompt.md` | `structural-verdict.schema.json` | granularity-checker + synthesizer (carries `verdict` + plan-perfector `findings[]`) |

LITE folding: adversarial `.raw.findings`→challenges, `.raw.assumption_audit`→§10;
empirical-domain `.raw.unknowns_resolutions`→§2, `.raw.surfaced_contexts`→§3,
`.raw.probed_risks`→§4, `.raw.findings`→challenges; structural-verdict `.raw.granularity_findings`→§12,
`.raw.resolution_table` updates statuses (§6), `.raw.verdict` = the terminal verdict,
`.raw.findings` (OPEN/UNRESOLVED) drives the helper's `issue_count`/`challenge_topics` for
plan-perfector. The §7 convergence criteria still apply within a single pass; volume is bounded by
token-budget batching at the Step 3a merge, not by a challenge-count ceiling; there are no
cross-iteration directives.

#### FAITHFUL role → prompt + schema (under `~/.claude/skills/verify-plan/codex-roles/`)

| Protocol role | prompt template | schema | Claude equivalent |
|---|---|---|---|
| Challenger (§1, §10, §13) | `challenger.prompt.md` | `challenger.schema.json` | plan-challenger |
| Domain expert (§1) | `domain-expert.prompt.md` | `domain-expert.schema.json` | plan-domain-expert |
| Devil's advocate (§1) | `devils-advocate.prompt.md` | `devils-advocate.schema.json` | plan-devils-advocate |
| Granularity (§12) | `granularity-checker.prompt.md` | `granularity-checker.schema.json` | plan-granularity-checker |
| Researcher (§2, §3, §4) | `researcher.prompt.md` | `researcher.schema.json` | gsd-phase-researcher (Lane 1 RESOLVE) + plan-researcher (Lane 1 SURFACE + Lanes 2-3), CONSOLIDATED into one call |
| Synthesizer (§5, §6, §7, §9) | `synthesizer.prompt.md` | `synthesizer.schema.json` | plan-synthesizer |

### Folding helper output into `iteration_state`

The helper's normalized stdout exposes only `verdict`, `findings[]`, `issue_count`,
`challenge_topics`, `raw`. Each role's FULL contribution (the §1-§13 blocks) lives in `.raw` (the
raw Codex message = the JSON object matching the role schema). The orchestrator **re-parses `.raw`
with the same defensive extraction the helper uses** (direct `JSON.parse` → fenced ```json block →
first balanced `{...}` span; `.raw` is bare JSON in practice, but matching the helper keeps folding
robust if a future `.raw` arrives fenced/prose-wrapped) and merges:

- Challenger → `.raw.findings` → `iteration_state.challenges` (origin `challenger`);
  `.raw.assumption_audit` → `iteration_state.assumption_audit` (§10);
  `.raw.unknowns_manifest` → `iteration_state.unknowns` (§2);
  `.raw.tests_contract` → TC-1 finding (§13), escalating per the table there.
- Domain-expert / devils-advocate → `.raw.findings` → `iteration_state.challenges` (their origins).
- Granularity → `.raw.findings` → `iteration_state.granularity_findings.findings` (§12);
  `.raw.escalated_challenges` → `iteration_state.challenges` (origin `granularity`).
- Researcher → `.raw.unknowns_resolutions` → update `iteration_state.unknowns` resolutions (§2);
  `.raw.surfaced_contexts` → `iteration_state.surfaced_contexts` (§3);
  `.raw.probed_risks` → `iteration_state.probed_risks` (§4);
  `.raw.findings` → new challenges (origins `surfaced`/`probed`).
- Synthesizer → `.raw.resolution_table` updates challenge statuses (§6); `.raw.convergence.status`
  drives loop control (§7) — **this, not `verdict`, is what the loop branches on**;
  `.raw.directives` gates the next iteration's conditional roles;
  `.raw.rumsfeld_coverage` (§9) feeds the report.

The §7 convergence criteria and Step 3a merge batching apply identically to codex-sourced
challenges — the engine does not get extra budget.

### Determinism note (plan-perfector oscillation guard)

The helper deterministically slugifies + sorts `challenge_topics` from `findings[].title`, so
identical findings yield byte-identical arrays. Step-4 builds the plan-perfector summary's
`challenge_topics`/`issue_count` from the FINAL synthesizer call's normalized output (whose
`findings[]` are the OPEN/UNRESOLVED challenges). Cross-run stability additionally requires the
synthesizer to reuse stable challenge titles — the prompt instructs this; it is the documented
residual oscillation risk.
