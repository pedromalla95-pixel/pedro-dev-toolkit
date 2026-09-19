# Assumptions-First Mode — Inverted-Flow Doctrine

The default interview asks the user to supply context one question at a time (~15 interactions for a MODERATE+ scope). Assumptions-First Mode inverts that flow: the skill reads the codebase itself, states what it has inferred, and asks the user to confirm or correct **only the inferences it is unsure about**. Most decisions are settled by evidence, not by asking. Target: ~3 interactions instead of ~15.

This is a re-implementation of the inverted flow on top of the existing explore-instead-of-ask machinery (Rule 3). It does NOT call any GSD-coupled subagent.

---

## When it runs

- Activated by the `--assume` flag.
- Best fit: a brownfield codebase with enough existing structure to infer from (schema, routes, components, config, conventions). On a near-empty repo there is nothing to infer — fall back to the standard one-question-at-a-time interview and say so.
- Composes with tier: still self-classify (Step 0.1). `--assume` changes the elicitation *shape*, not the tier machinery.

---

## The flow

### 1. Scout (5–15 files)
Read the highest-signal files for the task's domain. Prioritize:
- Schema / migrations / model files (data shape).
- Routes / controllers / entry points (surface area).
- Config (`package.json`, env samples, build config) — stack + locked choices.
- The closest existing analog to what is being built (pattern to follow).
- Conventions (lint config, directory layout, naming).

Cap the scout at 5–15 files. Reading more is a signal the task is COMPLEX enough to warrant the full interview, not the assumptions flow — announce that and switch.

### 2. Surface 3–5 assumptions
From the scout, write 3–5 load-bearing assumptions. Each assumption is a decision the plan will rest on. For each, emit:

```
**Assumption N:** <the inferred decision, stated plainly>
- **Evidence:** <file path(s) + what in them supports this — e.g. `schema.sql` has `users`+`sessions` tables>
- **If wrong:** <the concrete downstream consequence of building on a false version of this>
- **Confidence:** Confident | Likely | Unclear
```

Confidence rubric:
- **Confident** — evidence is direct and unambiguous (the file says exactly this).
- **Likely** — evidence is strong but inferential (pattern implies it; one reasonable alternative exists).
- **Unclear** — evidence is thin, conflicting, or absent; a guess the user must adjudicate.

### 3. Ask only the Unclear ones
Present all 3–5 assumptions as a block so the user sees the full inferred picture. Then ask for confirmation or correction **only on the `Unclear` items** — one at a time, per the Canonical Elicitation Contract. `Confident` and `Likely` assumptions are stated as adopted defaults the user may veto, not as questions. This is where the interaction count collapses: 3–5 inferences, but only the 1–2 genuinely-uncertain ones cost a round-trip.

### 4. Lock and proceed
Once the Unclear items are resolved (or explicitly deferred), the assumption set becomes the gathered context. Hand off per Rule 7 — no auto-transition. The locked assumptions feed Phase 5 artifact generation exactly as interview answers would.

---

## Interaction-budget contract

- Standard interview: ~1 question per decision → ~15 round-trips for a MODERATE+ scope.
- Assumptions-First: 1 block (the 3–5 assumptions) + ~1–2 clarifying round-trips on Unclear items → ~3 total.
- If more than 3 assumptions land at `Unclear`, the evidence base is too thin for the inverted flow. Say so and fall back to the standard interview rather than guessing through a stack of uncertain assumptions.

---

## Guardrails

- **Never present a guess as Confident.** Mis-tagging an Unclear inference as Confident defeats the mode — the user skips it and builds on a wrong foundation. When in doubt, tag down.
- **Evidence must cite real files.** Every assumption names the path(s) it came from. An assumption with no evidence is a Known Unknown (Rumsfeld Q2), not an assumption — route it to a question.
- **Reversibility still applies.** Tag any one-way-door assumption `[ONE-WAY]` and force a confirmation round-trip on it even if confidence is high.
- **Compliance still applies.** If the scout hits a compliance-triggering domain (Rule 5 set), the compliance category stays mandatory regardless of the assumptions flow.
- **Contradiction at point-of-arising.** If two scouted files disagree, surface the contradiction as an Unclear assumption immediately — do not silently pick one.
