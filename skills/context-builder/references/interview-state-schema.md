# interview_state.json — Resume-Support Schema

JSON schema for per-invocation interview state. Enables `--resume <project>` to continue an interrupted interview with full context.

Saved to `01-Projects/<project>/interview_state.json` after every user answer.

---

## Canonical JSON schema

```json
{
  "schema_version": "1.0",
  "project": "<project-name>",
  "session_uuid": "<uuid of originating session>",
  "started": "<ISO-8601 timestamp>",
  "updated": "<ISO-8601 timestamp>",
  "tier": "TRIVIAL | MODERATE | COMPLEX",
  "tier_rationale": "<why this tier was chosen>",
  "override_flags": ["--full", "--quick", "--refresh-theory", "--cynefin", "--rewrite", "--resume", "--force"],
  "theories_active": ["rumsfeld", "mece", "socratic", "five-whys", "bloom", "pyramid", "jtbd", "premortem", "counterfactual", "reversibility"],
  "rumsfeld_state": {
    "q1_unknown_knowns": "COVERED | PARTIAL | OPEN",
    "q2_known_unknowns": {
      "status": "COVERED | PARTIAL | OPEN",
      "open_items": ["<gap>", "..."]
    },
    "q3_known_knowns": {
      "status": "COVERED | PARTIAL | OPEN",
      "audited_assumptions": ["<assumption>", "..."]
    },
    "q4_unknown_unknowns": {
      "status": "COVERED | PARTIAL | OPEN",
      "premortem_run": true,
      "surfaced_risks": ["<risk>", "..."]
    },
    "last_sweep": "<ISO-8601 timestamp>",
    "deadlock_override_used": false
  },
  "questions_asked": [
    {
      "q_id": "Q1",
      "layer": 1,
      "mece_category": 1,
      "question_text": "<verbatim>",
      "user_answer": "<verbatim>",
      "follow_up_needed": false,
      "timestamp": "<ISO-8601>"
    }
  ],
  "coverage_map": {
    "mece_1_domain": "HIGH | MED | LOW | UNEXPLORED",
    "mece_2_technical": "HIGH | MED | LOW | UNEXPLORED",
    "mece_3_cross_phase": "HIGH | MED | LOW | UNEXPLORED",
    "mece_4_workflow": "HIGH | MED | LOW | UNEXPLORED",
    "mece_5_edge_cases": "HIGH | MED | LOW | UNEXPLORED",
    "mece_6_assumptions": "HIGH | MED | LOW | UNEXPLORED",
    "mece_7_stakeholder_power": "HIGH | MED | LOW | UNEXPLORED",
    "mece_8_temporal": "HIGH | MED | LOW | UNEXPLORED",
    "mece_9_second_order": "HIGH | MED | LOW | UNEXPLORED",
    "mece_10_compliance": "HIGH | MED | LOW | UNEXPLORED | N/A"
  },
  "mandatory_question_classes": {
    "premortem": { "asked": false, "answered": false },
    "counterfactual": { "asked": false, "answered": false },
    "reversibility_audit": { "decisions_tagged": [] }
  },
  "open_gaps": ["<gap ID or description>", "..."],
  "research_budget_used": {
    "websearch": 0,
    "ctx7": 0,
    "seqthink": 0,
    "webfetch": 0
  },
  "current_rule": "0 | 1 | 2 | ... | 11",
  "current_layer": 1,
  "phase": "phase-0 | interview | phase-5 | phase-6 | complete",
  "paused_at": "<ISO-8601 timestamp if paused; null otherwise>",
  "resume_hint": "<one-line instruction for the resumer — the next concrete action to take>"
}
```

---

## Write cadence

- **Phase 0 completion:** initial state written.
- **After every user answer:** update `questions_asked`, `coverage_map`, `rumsfeld_state`, `current_rule`, `current_layer`.
- **On research action:** update `research_budget_used`.
- **Phase 5 completion:** set `phase: "complete"`, save final state.
- **Interrupt detection:** on interrupted session, set `paused_at` + `resume_hint`.

---

## Resume behavior

When the skill is invoked with `--resume <project>`:

1. Read `01-Projects/<project>/interview_state.json`.
2. Validate `schema_version` is supported.
3. Restore tier, theories, rumsfeld_state, coverage_map.
4. Announce to user: "Resuming interview for `<project>`. Current state: tier=<tier>, layer=<L>, rule=<R>. Open gaps: <list>. Last paused: <paused_at>. Resume hint: <resume_hint>."
5. Ask for confirmation to continue.
6. On confirm, continue from the `resume_hint` — typically the next pending question or the next MECE category to probe.

---

## Schema version migration

`schema_version` is the single source of forward-compatibility. On change:

- Increment `schema_version`.
- Write a migration entry in `references/CHANGELOG.md`.
- Old state files continue to load if skill recognizes the old version number; otherwise, skill prompts user to either restart or manually reconcile.

---

## Validity check

The schema is parseable with `py -c "import json; json.load(open('path/to/interview_state.json', encoding='utf-8'))"`.

Never write a partial state — either the full schema or nothing. Corruption recovery = delete file and restart interview.
