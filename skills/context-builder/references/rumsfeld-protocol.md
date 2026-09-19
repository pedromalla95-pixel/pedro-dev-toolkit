# Rumsfeld Protocol — Epistemic Coverage

Rigorous 4-quadrant matrix tracking knowledge gaps across the interview. Runs as a critical, proactive loop.

---

## The four quadrants

| Quadrant | Label | Meaning | Source |
|---|---|---|---|
| **Q1** | Unknown Knowns | Context in memory/codebase/git nobody connected to the topic | Artifact sweep, memory files, CLAUDE.md, git log |
| **Q2** | Known Unknowns | Gaps the user or skill has explicitly flagged | User answers + research loop (ctx7 / WebSearch) |
| **Q3** | Known Knowns | Assumptions being taken as fact | Assumption Audit — surface them, ask to confirm |
| **Q4** | Unknown Unknowns | Black swans — failure modes not yet imagined | Devil's-advocate probe + Pre-mortem round |

---

## Gate classification

- **Q2 (Known Unknowns) = HARD-GATE.** Must resolve via research or direct question before Rule 7 handoff.
- **Q4 (Unknown Unknowns) = HARD-GATE.** Must run devil's-advocate probe at least once per session (Layer 4 Pre-mortem satisfies this).
- **Q1 (Unknown Knowns) = SOFT-GATE.** Flag with `[RUMSFELD-GAP: Q1]` tag. Surface to user but do not block.
- **Q3 (Known Knowns) = SOFT-GATE.** Audit and confirm. Block only when a contradiction is detected between a Q3 assumption and a later user answer.

---

## Cadence

Looped, trigger-based — **NOT every turn**. Running every turn burns tokens without signal.

### 1. Initial sweep (Phase 0.4, once per invocation)
Full 4-quadrant pass before the first question. Establishes baseline epistemic coverage. Output: quadrant table with flags.

### 2. Re-sweep on triggers (during interview)
- **New entity/domain/assumption** introduced mid-interview → re-run Q3 + Q4 for the new surface.
- **Blindspot sweep flag** (Rule 6 Coverage Status every 3-5 questions) → full re-sweep if any quadrant is uncovered.
- **Contradiction detected** between a current answer and a prior one → re-run Q3 (known-knowns audit).
- **Safety-net cadence:** full re-sweep every 5 questions even if no trigger fires.

### 3. Final sweep (Phase 5.4, before Rule 7 handoff)
Confirm all four quadrants are green. Any still-open Q2 or Q4 BLOCKS the handoff and routes to the deadlock override path.

---

## Deadlock override path (Decision 19)

When the research budget is exhausted and Q2 or Q4 still has open gaps, present this proactive escalation:

```
**Rumsfeld Deadlock — Unresolved Hard-Gate Gaps:**

Q2 (Known Unknowns): {list}
Q4 (Unknown Unknowns): {list}

Research budget exhausted ({X WebSearch + Y ctx7 + Z seqthink used}).

Options:
(a) Extend research budget by N more queries (specify N)
(b) Escalate to user as open-risk and proceed with `--force` (default; requires explicit user "yes")
(c) Abort and return to interview spine

Your call:
```

Default = **(b)**. The `--force` flag is logged in the audit trail with the unresolved gap list. It never silently proceeds.

---

## Sweep output schema

Each sweep writes a structured block to the working context or `research_log.md`:

```
## Rumsfeld Sweep — {phase|trigger} — {timestamp}

Q1 Unknown Knowns: COVERED | PARTIAL | OPEN
  - {surfaced context items}
  - [RUMSFELD-GAP: Q1] {item} — (soft)

Q2 Known Unknowns: COVERED | PARTIAL | OPEN
  - {resolved gaps and source}
  - [RUMSFELD-GAP: Q2] {unresolved gap} — (HARD-GATE)

Q3 Known Knowns (assumptions): COVERED | PARTIAL | OPEN
  - {audited assumptions}
  - [RUMSFELD-GAP: Q3] {unconfirmed assumption} — (soft)

Q4 Unknown Unknowns: COVERED | PARTIAL | OPEN
  - {devil's-advocate findings}
  - [RUMSFELD-GAP: Q4] {suspected black swan} — (HARD-GATE)
```

---

## Anti-patterns

- Running Rumsfeld every turn (token bloat, user fatigue).
- Running Rumsfeld only once at start (new primitives surface mid-interview and drift).
- Silently overriding a Q2/Q4 hard gate ("fail-open" on safety check — explicitly forbidden per ANTIPATTERNS).
- Tagging a gap as `[RUMSFELD-GAP: Q2]` without documenting what research was attempted.
