# PM Template — Obsidian Markdown

Canonical PM (Product Manager execution) structure for `01-Projects/<project>/PM.md`. Execution flavor: RACI, timeline, risks, rollout, comms. Paired with `PRD.md`.

---

## Template source

```markdown
---
type: pm
project: <PROJECT_NAME>
status: draft
created: <YYYY-MM-DD>
updated: <YYYY-MM-DD>
tags: [pm, <project-tag>]
---

# PM — <PROJECT_NAME>

**Pair document:** [[PRD]]

## 1. Stakeholder RACI

{{POWER_WORDS: specific, structured}}

Structured RACI with specific named owners. Not roles — people.

| Item | Responsible | Accountable | Consulted | Informed |
|---|---|---|---|---|
| <specific deliverable> | <name> | <name> | <name(s)> | <name(s)> |

### Hidden stakeholders (from MECE-7 Stakeholder-Power probe)
- **Champions:** <who actively supports>
- **Silent vetoes:** <who could kill this with one email>
- **Information-only must-know:** <political necessity>

## 2. Delivery KPIs

{{POWER_WORDS: specific, actionable}}

Execution metrics. Separate from product KPIs (which live in PRD §3).

| Metric | Measurement | Target | Review cadence |
|---|---|---|---|
| Velocity | <specific unit> | <target> | weekly |
| Blockers cleared | count/week | <target> | weekly |
| Budget burn | % of estimate | <target> | per-milestone |

## 3. Execution Dependencies

{{POWER_WORDS: specific, sequential}}

Sequential execution dependencies. Format each as: **this blocks that**.

- **Blocks:** <specific dep> → <dependent item>
- **Requires:** <dependent item> ← <specific dep>

External dependencies (teams, vendors, upstream work):

| External | Owner | Needed by | Risk if late |
|---|---|---|---|
| <specific> | <who> | <date> | <impact> |

## 4. Schedule Risks

{{POWER_WORDS: proactive, anticipate, critical}}

Critical schedule-risk analysis. Anticipate time-sensitive failure modes and proactively state mitigations.

<!-- Combination Pattern: Proactive Planning Stack -->

| Risk | Trigger | Days lost if hit | Proactive mitigation | Owner |
|---|---|---|---|---|
| <schedule risk> | <what causes it> | <estimate> | <specific action> | <owner> |

### Temporal constraints (from MECE-8 probe)
- **Hard deadlines:** <date + driver>
- **Calendar events that constrain:** <tax period, release windows, holidays>
- **Sequencing locks:** <what must happen before what>

## 5. Timeline / Milestones

{{POWER_WORDS: step-by-step, structured, sequential}}

Step-by-step milestone breakdown. Structured, sequential — each milestone has a clear start, end, and done-criteria.

| Milestone | Start | End | Done criteria | Verifier |
|---|---|---|---|---|
| M1: <specific> | <date> | <date> | <mechanically verifiable> | <who> |
| M2: ... | ... | ... | ... | ... |

### Critical path
<Which milestones are on the critical path — slippage here slips launch>

## 6. Rollout / Launch Plan

{{POWER_WORDS: actionable, practical, implementable}}

<!-- Combination Pattern: Practical Advisor Stack
     "As an expert, give actionable, specific recommendations. Be concise
      and prioritize by impact." -->

Practical, implementable rollout with specific gates. Phased where appropriate.

### Phase 1 — Internal / Dogfood
- <actionable step>
- Rollback trigger: <specific signal>

### Phase 2 — Limited beta
- <actionable step>
- Rollback trigger: <specific signal>

### Phase 3 — General availability
- <actionable step>
- Rollback trigger: <specific signal>

### Rollback plan
Specific, mechanical rollback steps. Who executes, in what order, with what verification.

## 7. Communication Plan

{{POWER_WORDS: structured, describe, explain}}

Structured communication cadence. Each audience has a channel, frequency, and format.

| Audience | Channel | Frequency | Format | Owner |
|---|---|---|---|---|
| Internal team | Slack | daily | standup thread | <name> |
| Leadership | email digest | weekly | describe + explain | <name> |
| Customers / users | in-app + blog | per-release | describe impact + explain action | <name> |

### Second-order effects (from MECE-9 probe)
- **What this enables:** <specific>
- **What this disables:** <specific>
- **New dependencies we're creating:** <specific>

## 8. Open Questions

{{POWER_WORDS: focused, specific}}

- OQ-1: <specific execution question> — owner: <who>, answer needed by: <when>
- OQ-2: ...

---

## Metadata (used by skill + vault)

- **Related:** [[PRD]] (requirements pair), [[research_log]] (audit trail).
- **Handoff next step:** enter plan mode (Shift+Tab) with PRD as planning input. After plan is written, the skill suggests `@PLAN-PERFECTOR:<planpath>`.
```

---

## Fill instructions (for the skill at runtime)

1. Replace every `<PROJECT_NAME>`, `<YYYY-MM-DD>`, and angle-bracketed placeholder with real data.
2. Each `{{POWER_WORDS: ...}}` slot is replaced with prose using the listed HIGH-impact words.
3. Run `powerword_audit.md` lint on the filled PM. Offer `--rewrite` if density low.
4. Wikilink target: `[[PRD]]` resolves to `PRD.md` in the same directory.

## Template-time verification

- No anti-pattern words in the boilerplate text: `robust`, `leverage`, `delve`, `facilitate`, `try to`, `please`, `in today's ever-evolving`, `it's important to note`, `certainly!`.
- All 8 required sections present.
- All `{{POWER_WORDS: ...}}` slot markers well-formed.
- YAML frontmatter valid.
- Sections use execution vocabulary (not strategic/GTM).
