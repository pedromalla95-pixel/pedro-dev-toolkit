# Gap Review (shared, canonical)

Owner: context-builder. Callers cite, never copy: context-builder, reverse-prompt, gsd-discuss-phase, `/gap-review`.
Goal: before skill finish, fresh reviewers read full conversation, find gaps. Auto at end of each skill + manual `/gap-review`.

## Gate

No handoff / synthesis approval / CONTEXT.md write until line `GAP-REVIEW round N: done` printed. Missing line = step skipped, run it.
Manual `/gap-review`: same pipeline, any time mid-session or after.

## Step A — DIGEST

Main Claude writes `<scratchpad>/gap-review-digest.md`. Mechanical, not free summary:
- Goal (user words)
- Every question asked + user's answer VERBATIM (exact words, never paraphrased)
- Decisions, assumptions, rejected options, recommended answers user overrode
- Files read, research notes, artifacts written
- Open items / `[NEEDS INPUT]` / parked items
- Raw transcript path (`~/.claude/projects/<project>/<session>.jsonl`) as backup
- Gap ledger from earlier rounds (IDs + status)

## Step B — 4 REVIEWERS (parallel, fresh subagents, read-only)

One Agent call each, same message. Pass digest path + lens. Reviewers never write files, never guess answers. Time not a constraint.
Each reviewer returns list: `gap | why it matters | evidence (digest line / transcript) | proposed question`.

Lens prompt core (same for all, swap LENS):
> You read a finished-looking interview cold. Find what is missing, wrong, or unsafe. LENS: {lens}. Assume the interviewer was blind to its own assumptions. Check the digest; spot-check transcript when a detail looks thin. Report only real gaps with evidence, no praise, no padding.

Lenses:
1. **Scenarios + edge cases** — happy/unhappy paths, empty/huge/concurrent/repeated input, first-run vs steady state, who/what else touches it, what-ifs never asked.
2. **Failure modes (pre-mortem)** — "it failed in 2 months, why?" what breaks, how detected, fallback, rollback, one-way doors, security/compliance.
3. **Logistics + order of work** — dependencies, handoffs, tools/access/credentials, who does what, sequence, timing, how it gets verified, where output lives.
4. **Contradictions + missing info** — answers that clash, vague words never defined ("simple", "soon"), facts never asked, orphan requirements, goals with no done-measure, unstated assumptions.

## Step C — MERGE (main Claude)

- Dedupe across lenses. Keep evidence.
- Tag: **BLOCKER** (breaks plan/execute) / **RISK** (likely rework or harm) / **NICE** (polish).
- Rank by risk, BLOCKER first. Give each ID `G<round>-<n>`, status `open|answered|parked`.
- Show FULL ranked list to user. No cap.

## Step D — ASK

- BLOCKER + RISK → one question at a time, RECOMMENDED answer each (canonical contract). Answer → `answered`. User skips → `parked` (explicit).
- NICE → parked list, shown, not asked.
- Answer contradicts earlier answer → reconcile at point-of-arising (Rule 4).

## Step E — LOOP

Repeat A-D. Max 3 rounds. Round is **CLEAN** when: no NEW BLOCKER/RISK found AND every earlier BLOCKER/RISK is `answered` or user-`parked`. NICE never blocks.
Clean → stop. Round 3 not clean → stop anyway, list remaining open gaps as risks.
Print `GAP-REVIEW round N: done` after each round.

## Step F — CLOSING CHECK

Before finish: every `answered` gap appears in final artifact (PRD / CONTEXT.md / mega-prompt). Missing → fold in now. Open/parked BLOCKER/RISK → list under "Known risks" in final artifact. Show ledger summary to user.

## Day-one test

Run `/gap-review` on one past interview where misses are known. Must find them. Long-term signal: fewer late surprises, less plan/execute rework.
