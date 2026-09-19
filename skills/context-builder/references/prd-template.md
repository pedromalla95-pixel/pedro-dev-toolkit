# PRD Template — Obsidian Markdown

Canonical PRD structure for `01-Projects/<project>/PRD.md`.

---

## Template source

```markdown
---
type: prd
project: <PROJECT_NAME>
status: draft
created: <YYYY-MM-DD>
updated: <YYYY-MM-DD>
tags: [prd, <project-tag>]
---

# PRD — <PROJECT_NAME>

**Pair document:** [[PM]]

## 1. Problem Statement

Lead with a one-sentence thesis (Pyramid Principle). Then a thorough, nuanced analysis of the problem — specific evidence, not abstractions. Acknowledge trade-offs. ≤200 words.

<!-- Combination Pattern: Deep Analysis Stack
     "Provide a thorough, analytical breakdown. Be precise and nuanced —
      acknowledge trade-offs. Show your work." -->

## 2. Users / Jobs-to-be-Done

Frame user needs as JTBD, not personas. Format:

> When <context>, <user role> wants to <action>, so they can <outcome>.

Include 3–5 specific, detailed JTBDs. Examine edge-case users separately.

## 3. Goals + Product KPIs

Goals are specific and actionable. Each goal has a KPI that is:
- Measurable (how to count it)
- Time-bound (when to measure)
- Attributable (whose responsibility)

| Goal | KPI | Target | Measurement method |
|---|---|---|---|
| <specific goal> | <actionable metric> | <target value> | <how we'll know> |

## 4. Functional Requirements

Numbered requirements. Each is specific and precise — no vagueness.

1. FR-1: <precise requirement>
2. FR-2: <precise requirement>
...

## 5. Non-Functional Requirements

Each non-functional requirement is rigorous and verifiable — a specific threshold or standard, not a platitude.

| Category | Requirement | Threshold | Verification |
|---|---|---|---|
| Performance | <specific NFR> | <e.g., p95 <500ms> | <how to verify> |
| Security | ... | ... | ... |
| Accessibility | ... | ... | ... |

## 6. User Stories / Acceptance Criteria

Detailed user stories. Each story includes acceptance criteria written as Bloom's Apply/Analyze/Evaluate verbs:

> As a <user>, I want to <action>, so that <outcome>.
>
> **Acceptance:** examine / compare / assess — specific behaviors that must hold.

## 7. Scope (In / Out)

**In scope:**
- <specific item>
- <specific item>

**Out of scope (explicitly excluded):**
- <specific item> — <reason>
- <specific item> — <reason>

## 8. Dependencies

Thorough list of every specific dependency — libraries, APIs, services, internal systems, human approvals.

| Dependency | Type | Owner | Risk if delayed |
|---|---|---|---|
| <specific name> | library/API/service/human | <owner> | <risk> |

## 9. Risks

Critical risk assessment. For each risk, anticipate the impact and recommend a proactive mitigation.

<!-- Combination Pattern: Proactive Planning Stack
     "Think step by step. Be proactive — anticipate risks and recommend
      mitigations. Provide a structured, actionable mitigation table." -->

| Risk | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|
| <risk> | H/M/L | H/M/L | <proactive mitigation> | <owner> |

### Reversibility-audit tags
- [REVERSIBLE] decisions: <list>
- [ONE-WAY] decisions: <list + extra scrutiny note>

## 10. Open Questions

Focused, specific open questions. Each has an owner and a by-when.

- OQ-1: <specific question> — owner: <who>, answer needed by: <when>
- OQ-2: ...

---

## Metadata (used by skill + vault)

- **Related:** [[PM]] (execution companion), [[research_log]] (audit trail).
- **Rumsfeld final sweep:** confirm Q2 + Q4 resolved before closing.
```

---

## Fill instructions (for the skill at runtime)

1. Replace every `<PROJECT_NAME>`, `<YYYY-MM-DD>`, and other angle-bracketed placeholder with real data.
2. Apply the Pyramid Principle (answer-first) and JTBD framing throughout.
3. Create wikilink target: the `[[PM]]` link resolves to `PM.md` in the same directory.

## Template-time verification

- No anti-pattern words in the boilerplate text above: `robust`, `leverage`, `delve`, `facilitate`, `try to`, `please`, `in today's ever-evolving`, `it's important to note`, `certainly!`.
- All 10 required sections present.
- YAML frontmatter valid.
