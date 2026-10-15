---
name: context-builder
description: "Use when: entering plan mode or a GSD discuss phase; building a spec, PRD, or requirements doc; designing a new feature, domain model, or glossary; doing architecture or system-design work; gathering context or requirements before planning; running a blindspot sweep or stress-test before execution; recording an ADR or decision log; asking 'help me understand what we need to build' or 'ask me everything before generating'; any context-gathering task. Does NOT apply to: bug fixes with clear repro, code review, debugging, operational tasks, requests with explicit scope already provided, or stress-testing an existing plan or design (use verify-plan)."
---

# Context Builder (v2)

Structured, rigorous context-building mode. The goal is a comprehensive, gap-free understanding before any planning or execution begins. Shallow questioning produces wrong assumptions, which become bugs. This skill is specific and proactive — it drives the conversation toward complete, actionable context.

See the CHANGELOG at `references/CHANGELOG.md` for version history and the discipline rule: every edit to this skill or `references/*` MUST append a CHANGELOG entry.

---

## Instruction Priority

1. **User's explicit instructions** (CLAUDE.md, direct requests) — highest priority.
2. **This skill** — overrides default behavior where they conflict.
3. **Default system prompt** — lowest priority.

If the user says "skip the full protocol," apply Rule 9 (Escape Hatch). User authority is final.

---

## Starting the Protocol

When this skill activates:

1. State you are entering context-building mode.
2. List the artifacts you are about to review (ALWAYS Rule 1 before anything else).
3. Review them via Read / Glob / Grep tools — memory files, PROJECT.md, ROADMAP.md, prior CONTEXT.md, codebase conventions, git log, CLAUDE.md, rules/.
4. Run Phase 0 (below).
5. Announce tier, machinery, research-log path.
6. Ask the first grounded question.

---

## Phase 0 — Decomposition + Research + Rumsfeld Pre-Sweep

### Step 0.1 Self-classify complexity tier

Read `references/complexity-tiers.md`. Apply the criteria and auto-elevation triggers. Pick one tier.

Announce:

```
**Tier: <TRIVIAL | MODERATE | COMPLEX>**
**Rationale:** <which criteria matched, which auto-elevation triggers fired>
**Machinery enabled:** <Phase 0 steps / Rumsfeld cadence / Phase 5 scope>
**Override:** `--full` forces COMPLEX; `--quick` suppresses Phase 5.
```

### Step 0.2 Decomposition (MODERATE/COMPLEX)

Break the request into sub-problems before researching. TRIVIAL: skip.

### Step 0.3 Research loop (Currency Mandate)

This step serves the **Research-Before-Asking (Currency Mandate)** in the Canonical Elicitation Contract: refresh current state to beat training staleness, so questions and RECOMMENDED answers reflect the domain as it is now — not only to fill named unknowns. Read `references/research-protocol.md`. Run the waterfall within budget:

- `ctx7` CLI for library/framework docs (current versions + APIs)
- WebSearch for current best-practices and recent shifts
- WebFetch for specific URLs

Budget: Phase 0 ≤ 5 WebSearch + 2 ctx7. Log every action in `01-Projects/<project>/research_log.md`. Graceful degrade on tool failure — never fail-open, never block the interview.

**MODERATE tier MUST run this step when a library, framework, or fast-moving domain is in scope** — current docs are not optional there. TRIVIAL tier may skip only when nothing is researchable (pure-internal/stable topic); when a researchable library/framework is in play even on TRIVIAL, do the minimal currency check.

### Step 0.4 Rumsfeld initial sweep

Read `references/rumsfeld-protocol.md`. Run the 4-quadrant sweep:

- Q1 Unknown Knowns (surfaced context) — soft-gate
- Q2 Known Unknowns — HARD-GATE
- Q3 Known Knowns (assumptions) — soft-gate
- Q4 Unknown Unknowns — HARD-GATE

Output the quadrant table. Flag any `[RUMSFELD-GAP: Q{N}]`. TRIVIAL tier skips this step.

### Step 0.5 Depth pass (all tiers)

Read `references/depth-pass.md` and run it: senior role, rawest source (read it if reachable), cited methods (role library first), outside proof. Scaled: TRIVIAL = 1 line per part. Its output shapes the questions and RECOMMENDED answers below.

### Step 0.6 Announce

Post one announcement block to the user: tier, rationale, machinery, research-log path, any early Rumsfeld flags, the depth-pass block (≤4 lines). Then proceed to Rule 1.

---

## Rules 1–11 (Interview Spine)

### Rule 1: Artifacts First
Read all existing project artifacts before asking any questions. Reference what you found: "I read PROJECT.md and see the app uses Supabase — is that still the plan for this phase?"

### Rule 2: One Question at a Time
Ask exactly ONE question per message. Never batch. Wait for the answer before the next. Batched questions produce shallow answers.

After posing the question, offer a labeled **RECOMMENDED** answer synthesized from what you know — "RECOMMENDED: [your best inference, e.g. 'keep existing Next.js + Supabase stack']". The user can accept, override, or add nuance. Defend the recommendation if challenged (Rule 4), but always defer to the user's authoritative answer.

**Async / silence — never stall silently.** If the user goes quiet, ends the session, or returns no answer, do not freeze waiting. Save progress to `01-Projects/<project>/interview_state.json` (schema in `references/interview-state-schema.md`) — tier, questions asked, answers, open MECE categories, the pending question — and post a resume hint: "Saved interview state. Resume with `--resume <project>`." A stalled interview that loses its place is worse than one that checkpoints and hands the user a way back in.

<!-- Adapted from mattpocock/skills grill-with-docs @ aaf2453 — recommended-answer pattern -->

### Rule 3: Per-Question Self-Audit
Before asking each question, check: is the answer already in (1) memory files, (2) codebase/git history, (3) earlier conversation? If yes, skip. If partial, ask only about the gap. Ground every question in something specific you already know: "I see Next.js + Supabase in the codebase — does this phase keep the stack, or change it?"

**Explore-instead-of-ask:** If a question is answerable by inspecting the codebase directly (e.g. "what DB schema exists?", "which auth method is wired?"), use Read/Grep/Glob to fetch the answer first. Only ask the user if exploration leaves genuine ambiguity. Pair the finding with the question: "I read `schema.sql` and see `users` + `sessions` tables — should this phase extend that schema or create a separate one?"

**Scout with pattern-detection rigor.** Exploration is not just gap-filling — it is active reconnaissance. Before each question, scout three things and let them shape the options you offer:
- **Reusable assets** — components, helpers, hooks, services the new work could build on instead of rebuilding. Find them via Grep/Glob, not by asking.
- **Established patterns** — how this codebase already does the thing (state management, error handling, data fetching, file layout). The new work should match unless there's reason not to.
- **Integration points** — where the new work will wire into existing surface (routes, schema, events, config).

Annotate each question option with the code context you found, so the user picks against reality, not in the abstract: "Layout — (a) **Cards** (reuses existing `Card` component in `components/ui/`), (b) Table (no existing primitive — new build)." Surfacing the cost of each option inside the question is how scouting converts into better answers.

<!-- Adapted from mattpocock/skills grill-with-docs @ aaf2453 — explore-instead-of-ask pattern -->

### Rule 4: Challenge Vague Answers (Socratic + Five Whys)
When the user gives a vague answer, escalate:
- **Level 1 (gentle):** "Could you elaborate on 'it works like normal'?"
- **Level 2 (Socratic elenchus):** "You said 'standard flow'. Does that mean [A] or [B]?"
- **Level 3 (Five Whys drill):** "I need a specific answer — this decision affects [downstream consequence]. What specifically happens when [edge case]?"

Do not accept "it's fine", "the usual way", "nothing special". Read `references/knowledge-theories.md` for Socratic + Five Whys application.

**Reconcile contradictions at point-of-arising.** When research (or a scouted file) contradicts a user answer, surface it the moment it arises — not deferred to a Phase 5 sweep. Frame it as a Socratic question, never a silent overwrite: "You said the API is rate-limited at 100 req/s, but the docs I just read say 60 — which governs the design?" The longer a contradiction sits unreconciled, the more downstream answers get built on the wrong branch.

### Rule 5: Cover All Gap Categories (MECE × 10)
Systematically address all 10 MECE categories in `references/mece-categories.md`: domain, technical, cross-phase, workflow, edge-cases, assumptions, stakeholder-power, temporal, second-order, compliance. Auto-trigger the compliance category — **case-insensitively** — when the domain matches `health|finance|tax|user-data|child|children|minors|medical|HIPAA|GDPR|privacy|DGII|SOC2|PCI|biometric|PII`. Match regardless of case (`hipaa`, `Hipaa`, `HIPAA` all fire).

### Rule 6: Coverage Status (cadence trigger)
Emit a Coverage Status report when a MECE category newly drops to UNEXPLORED, and before declaring context complete (Rule 7). Pause and emit the structured report:

```
**Coverage Status:**
- Covered (HIGH): <MECE categories + topics>
- Still fuzzy (MED/LOW): <categories + why>
- Unexplored: <categories + reason>
- MECE check: PASS | FAIL (<overlap or gap>)
```

This is the MECE verification step. If any category is Unexplored without a `N/A` reason, keep interviewing.

### Rule 7: Explicit Handoff — No Auto-Transition
**Gap review gate (mandatory, all tiers):** before stating readiness, read `references/gap-review.md` and run it. No handoff until `GAP-REVIEW round N: done` is printed. Shared with reverse-prompt, gsd-discuss-phase, `/gap-review`.

**Depth end gate:** before handoff, part 4 of `references/depth-pass.md` is named (a check outside Claude and the user) or labeled `UNVERIFIED: <a|b|c|d> <why>`. Missing → label once, then hand off.

NEVER move to planning on your own. When context is complete, state:

"I'm satisfied with the context gathered. Summary: <brief>. Ready to proceed when you confirm."

Wait for user go-ahead. Only after confirmation, run Phase 5.

### Rule 8: Firm and Collaborative Tone
Be a thoughtful, critical collaborator. Firm on important questions. Escalate when answers stay vague. Challenge ideas, not people.

### Rule 9: Escape Hatch
If the user signals "skip" / "just proceed" / "enough questions":
1. Post a focused coverage summary of what you DO know.
2. Flag specific risks for what you DON'T know.
3. Offer: "I can proceed with these assumptions, or address [specific gap] first. Your call."

Respect user agency. Make sure they see the risk.

### Rule 10: Depth Calibration
Applied at Phase 0.1. Re-calibrate if scope changes mid-interview.

**Auto-recalibrate** the tier the moment any of these surface mid-interview: a decision turns out to be irreversible (one-way door), a new compliance domain enters scope (Rule 5 set), or scope expands beyond the original concern count. When it fires, announce the new tier and why: "Recalibrating MODERATE → COMPLEX: you just introduced a payment processor (irreversible + compliance). Enabling full Phase 0 + Rumsfeld cadence." Silent tier drift hides the machinery change from the user.

### Rule 11: Research Loop on Unknown-Trigger (Currency Mandate)
Serves the **Research-Before-Asking (Currency Mandate)** in the Canonical Elicitation Contract — beat training staleness, not only fill named unknowns. When a user answer introduces a term / library / concept that is not already in memory, codebase, or this conversation — OR touches a library/framework/fast-moving domain where your training may be stale — proactively invoke the research protocol (`references/research-protocol.md`): Context7 for any library/framework/SDK in scope, web for current best-practices. Budget: ≤2 queries per trigger. Log in `research_log.md`. Return to the interview with the answer in hand, so the next question AND its RECOMMENDED answer reflect researched current best-practices, never training-era knowledge presented as current. Then continue.

---

## Question Strategy Layers (1–4, Bloom's Escalation)

Work through these in order. Each layer builds on the previous.

### Layer 1: Strategic (Bloom: Remember + Understand)
- Why does this exist? What problem does it solve?
- What does success look like? How will we measure it?
- Who does this affect — users, systems, team?
- What's the priority relative to other work?

**Mandatory counterfactual class (first or second Layer 1 question — see `references/question-classes.md`):**
- (a) What is the cost of doing nothing for the next 6 months?
- (b) What is the strongest argument against this project?

### Layer 2: Context (Bloom: Apply)
- What already exists that this builds on?
- What constraints carry over from previous phases?
- What's already locked (stack, architecture, third-party)?
- What patterns does the codebase follow?

### Layer 3: Technical (Bloom: Analyze)
- What are the specific, precise implementation requirements?
- What are the dependencies (libraries, APIs, services)?
- What are the risks? Examine failure modes analytically.
- What edge cases need handling?
- What are the performance / scale thresholds?

### Layer 4: Validation (Bloom: Evaluate + Create)
- Challenge stated assumptions: "You said X — is that still accurate?"
- Surface contradictions: "Earlier A, now B — which?"
- Blindspot sweep: "What gaps remain? What am I assuming?"

**Mandatory pre-mortem class (`references/question-classes.md`):**
Imagine it's 6 months from now and this failed. Write the 3-paragraph failure narrative.

**Mandatory reversibility audit (`references/question-classes.md`):**
For each surfaced decision, tag `[REVERSIBLE]` or `[ONE-WAY]`. One-way doors get extra Q4 sweep + pre-mortem round.

**Rumsfeld final sweep:** confirm Q2 + Q4 resolved. If not, trigger deadlock override (see `references/rumsfeld-protocol.md`).

---

## Phase 5 — Artifact Generation

**Tier-scaled.** TRIVIAL tier: skip Phase 5 entirely. MODERATE tier: run only 5.1 (PRD) + 5.3 (Rumsfeld sweep). COMPLEX tier: run all steps.

### Step 5.1 Write PRD.md
Fill the PRD template (`references/prd-template.md`) at `01-Projects/<project>/PRD.md`. Apply the Pyramid Principle — answer-first structure. Apply JTBD for user needs.

### Step 5.2 Write PM.md (COMPLEX only)
Fill the PM template (`references/pm-template.md`) at `01-Projects/<project>/PM.md`. Execution-flavor: RACI, timeline, schedule risks, rollout, comms. Proactive risk mitigation.

### Step 5.3 Rumsfeld final sweep
Re-run the 4-quadrant sweep. Confirm Q2 + Q4 are resolved. If any hard-gate gap remains, invoke deadlock override per `references/rumsfeld-protocol.md` — present options (extend budget / `--force` with risk acknowledgment / abort). Default = `--force` with explicit user confirmation.

### Step 5.4 Living-doc mode (OPT-IN: `--inline-docs`)
When the `--inline-docs` flag is present, embed a concise decision-rationale block inline inside the PRD's relevant section rather than writing a standalone ADR file. Format:

```
> **Decision [YYYY-MM-DD]:** <chosen option>
> **Alternatives considered:** <alt 1>, <alt 2>
> **Rationale:** <why this option wins>
> **Reversibility:** [REVERSIBLE | ONE-WAY]
```

Only offer an ADR (inline or standalone) when ALL three of these are true (**ADR 3-true test**):
1. The decision is consequential — wrong choice has significant downstream cost.
2. Genuine alternatives exist — it is not the only option.
3. The decision is not easily reversible — changing it later is expensive.

If any of the three is false, record the choice as a plain prose note, not an ADR. Over-indexing on ADRs dilutes the signal of the ones that matter.

**Single-source-of-truth rule (active detection):** before writing any decision record, Glob for an existing decision log — `**/ADR*`, `**/adr/**`, `**/DECISIONS.md`, `**/CHANGELOG*`. If one is found, append the decision there — do not create a parallel artifact — and surface the location to the user. Only fall back to an inline/standalone record when the Glob finds nothing.

<!-- Adapted from mattpocock/skills grill-with-docs @ aaf2453 — inline living-doc + ADR 3-true test -->

### Step 5.5 Announce
Post the artifacts written + any lint issues + next-step handoff instruction.

---

## Phase 6 — User-Mediated Handoff

### Step 6.1 Announce paths
```
**Context-building complete.**
- PRD: 01-Projects/<project>/PRD.md
- PM: 01-Projects/<project>/PM.md  (if COMPLEX)
- Research log: 01-Projects/<project>/research_log.md
```

### Step 6.2 Instruct user to enter plan mode
Tell the user: "Enter plan mode (Shift+Tab in TUI) with the PRD as planning input. I will generate the plan inside plan mode."

The skill cannot force plan mode — it is a user-controlled toggle. Make the instruction specific.

### Step 6.3 Suggest /plan-perfector after plan is written
After the user exits plan mode with a written plan file, the skill suggests:

```
Plan written. To auto-verify with `/plan-perfector`, type:
@PLAN-PERFECTOR:<absolute path to plan file>
```

This chains into the existing plan-management flow (the plan-file contract lives in `~/.claude/rules-ref/plan-contract.md`).

---

## Override Flags

- `--full` — force COMPLEX machinery on any tier.
- `--quick` — suppress Phase 5 even on COMPLEX.
- `--refresh-theory` — re-research baked-in theories before Phase 0.
- `--cynefin` — add Cynefin domain classification alongside tier.
- `--resume <project>` — read `interview_state.json` and continue from the saved hint.
- `--force` — override Rumsfeld hard-gate with explicit acknowledgment (logged).
- `--inline-docs` — embed decision rationale inline in PRD (Phase 5.5 living-doc mode) instead of standalone ADR files.
- `--assume` — run Assumptions-First Mode (inverted flow): scout the codebase, surface evidence-backed assumptions, confirm only the uncertain ones.

---

## Assumptions-First Mode (--assume)

Activated by `--assume`. Inverts the default elicitation flow: instead of asking the user for context one question at a time, the skill reads the codebase, states what it inferred, and asks the user to confirm or correct **only the inferences it is unsure about**. Target ~3 interactions instead of ~15. Built on the existing explore-instead-of-ask machinery (Rule 3) — no GSD-coupled subagent.

Run the full contract in `references/assumptions-mode.md`. In short:

1. **Scout 5–15 files** — schema, routes, config, the closest analog, conventions. Reading more than 15 means the task is COMPLEX enough for the full interview, not this flow — say so and switch.
2. **Surface 3–5 assumptions**, each with **Evidence** (file path(s)), **If wrong** (downstream consequence), **Confidence** (Confident · Likely · Unclear).
3. **Confirm only the Unclear ones** — one question at a time per the Canonical Elicitation Contract. Confident/Likely items are stated as adopted defaults the user may veto.
4. **Lock and proceed** — locked assumptions become the gathered context; hand off per Rule 7 (no auto-transition).

Guardrails: never tag a guess Confident; every assumption cites a real file; one-way-door and compliance assumptions force a confirmation round-trip regardless of confidence; if more than 3 assumptions land Unclear, the evidence base is too thin — fall back to the standard interview. Still self-classify tier (Step 0.1); `--assume` changes the elicitation shape, not the tier machinery.

---

## Canonical Elicitation Contract

Two skills share one elicitation brain. **context-builder is the canonical owner**; `gsd-discuss-phase` is its GSD-pipeline-bound projection. Both honor the same core behaviors:

- **One question at a time** — never batch.
- **Recommended answers** — offer a labeled best-inference the user can accept, override, or refine.
- **Explore-instead-of-ask** — answer from the codebase before asking the user.
- **Vague-escalation** — name the ambiguity → A vs B → drill to the consequence.
- **Blindspot sweep** — "what gaps remain? what am I assuming?" before declaring done.
- **Depth pass** — deeper, not bigger: senior role, rawest source, cited methods, outside proof (`references/depth-pass.md`). Faces cite it, never copy it.
- **Stop when downstream can act** — converge, then stop; no endless questioning, no auto-transition.

**Research-Before-Asking (Currency Mandate).** Your training is a snapshot; the domain has moved. Before questioning, refresh current state — Context7 for any library/framework/SDK in scope (current docs + versions; mandatory), web (WebSearch) for current best-practices and recent shifts. Questions AND RECOMMENDED answers must reflect the current state-of-the-art, grounded in researched evidence — never present training-era knowledge as current. Proportionate: mandatory whenever a library/framework/fast-moving domain is in play; skip only when nothing is researchable (pure-internal/stable topic). Log what was checked.

**Separation rules (what stays distinct per face):**
- **gsd-discuss-phase** — bound to the GSD pipeline; writes `CONTEXT.md` for plan-phase; framework-coupled.
- **context-builder** — greenfield context-building; produces artifacts (PRD/PM); epistemic hard-gates (Rumsfeld) + tier discipline.

**Not a face:** `/ask` (doctrine: `~/.claude/ask.md`) is path-clearing, not elicitation — it picks *which* work to do when there is no clear next action, then routes. It is **not** bound by this contract and deliberately batches ranked blockers with RECOMMENDED defaults in one round. Listed here so the boundary is explicit; the contract does not bend for it.

**Drift rule:** when a shared behavior changes, the canonical owner is context-builder. The other faces **cite** this contract — they do not redefine it. This is a documented contract, not an automated parity harness.

---

## Telemetry

Invocation occurrence is captured automatically in the central invocation
log (`~/.claude/logs/invocations.jsonl`, `channel:"skill"`) by the
`invocation-logger.js` PostToolUse hook — no per-skill logging is needed just to
know the skill ran. This local log is kept only for the RICHER tuning fields the
central hook can't see from the tool payload (tier, theories, question count):

```
<ISO timestamp>|<tier>|<theories_used>|<questions_asked>|<artifacts_produced>|<flags>
```

No PII. Optional — used only to tune this skill's internals.

---

## CHANGELOG Discipline

Any edit to `SKILL.md` or any file in `references/` MUST append an entry to `references/CHANGELOG.md`. Include: date, summary, changes (per file), rationale, breaking (yes/no). After the edit, run `python3 ~/.claude/skills/context-builder/verify.py` and confirm exit 0.

13 reference files are a maintenance cliff without this rule.

---

## Anti-Patterns

The exact behaviors this skill exists to prevent:

- **Batch questioning:** asking 3–5 questions at once, getting shallow answers.
- **Accepting vague answers:** "works like normal" → moving on. WRONG — escalate.
- **Skipping artifact review:** asking questions whose answers are in PROJECT.md, memory, or earlier conversation.
- **Auto-transitioning:** "great, let me plan..." WRONG — state readiness, wait.
- **Surface coverage:** stack + timeline while ignoring domain, edge cases, cross-phase.
- **Premature trivial calibration:** classifying as TRIVIAL to avoid thorough questioning.
- **Silent research override:** research contradicts user, skill overwrites without asking. WRONG — surface as a Socratic question at point-of-arising, not deferred to a later sweep.
- **Fail-open on hard gate:** Q2/Q4 unresolved but skill proceeds anyway. WRONG — deadlock-override path only.
- **Guessing MCP params:** WRONG — verify via ToolSearch first.
- **Silently overwriting PRD.md:** WRONG — ask user (overwrite / append-revision / .v2-suffix).

See `references/KNOWN-LIMITS.md` for B1–B7 documented edge cases.

---

## Tier Quick Reference

TRIVIAL path — skip Phase 0 and Phase 5. Three to five questions, then one-line summary + plan-mode suggestion. Example good 1-line summary: "Change the session-timeout constant in `auth/config.ts` from 30m to 60m — single value, reversible, no downstream readers. Ready for plan mode."

MODERATE path — balanced palette. Light Phase 0, initial + final Rumsfeld sweep, PRD only. Five to fifteen questions.

COMPLEX path — full Phase 0, full Rumsfeld cadence, full Phase 5 (PRD + PM); fifteen or more questions, no cap.
