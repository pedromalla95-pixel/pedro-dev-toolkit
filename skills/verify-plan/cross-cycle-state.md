# Cross-Cycle State Artifact — format specification

One JSON file per phase, written to:

```
<phase_dir>/<padded_phase>-cross-cycle-state.json
```

It exists so the finding-generating roles and the plan reviser stop re-deriving what
already happened. Before it existed, the `byte-identity` finding was raised in cycle 2,
closed in cycle 3, absent in cycle 4, and back in cycle 5 — the loop had no memory.

## Shape

Exactly these seven top-level keys, and no others:

```json
{
  "closed_findings": [
    {"slug": "byte-identity", "closed_in_cycle": 3, "why": "one-sentence reason", "canonical_id": "C7"}
  ],
  "blessed_assertions": [
    {"file": "/abs/path", "claim": "one sentence", "verified_in_cycle": 3}
  ],
  "cycle": 6,
  "snapshot_commit": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
  "last_diff": {
    "changed_files": ["/abs/path/to/plan.md"],
    "touched_by_overlap": [{"file": "/abs/path", "token": "some-token"}],
    "touched_by_dependency": [{"file": "/abs/path", "depends_on": "/abs/other-file"}]
  },
  "flagged_dependents": [
    {"file": "/abs/path", "touches": "/abs/other/path", "why": "one-sentence reason", "flagged_in_cycle": 4}
  ],
  "inner_loop": {
    "iteration": 1,
    "status": "CONTINUE",
    "iteration_state": {},
    "updated_at": "2026-09-16T00:00:00Z"
  }
}
```

- `closed_findings[]` — findings a prior cycle resolved. `slug` is the stable topic slug,
  `closed_in_cycle` the cycle that closed it, `why` a one-sentence reason, `canonical_id`
  the finding ID it carried when closed.
- `blessed_assertions[]` — plan text verified correct. `verified_in_cycle` is the cycle
  that verified it.
- `cycle` — the cycle now running.
- `snapshot_commit` — the git SHA (`git -C <REPO> rev-parse HEAD`) recorded when this cycle's
  state was written; `plan-scope.js` diffs the next cycle against it.
- `last_diff` — the `diff` object `plan-scope.js` returned this cycle
  (`{changed_files, touched_by_overlap, touched_by_dependency}`), or `null` on cycle 1.
- `flagged_dependents[]` — files a plan-reviser's `Touches` column named as affected by a change
  it made, NOT itself changed. Fed to `plan-scope.js --reviser-touch <file>` on the next cycle so
  that file stays in scope even though a bare diff wouldn't catch it. Same 3-cycle expiry rule as
  `closed_findings`/`blessed_assertions`.
- `inner_loop` — the Step 3 convergence loop's position, so a context compaction cannot reset
  it. `iteration` is the inner-loop counter (1-3), distinct from the top-level `cycle` above.
  `status` is the last Step 3d verdict (`CONTINUE`, `BLOCKED`, `CONVERGED`, `FORCED_EXIT`).
  `iteration_state` is the full serialized `iteration_state` object from `protocol.md` § 5,
  stored verbatim so a resumed run needs nothing else. `updated_at` is an ISO-8601 UTC
  timestamp. Written by Step 3d after every iteration; read by the Resume Check that runs
  immediately after Step 00.

  Added 2026-09-16. Before it, the inner-loop position lived only in conversation context.
  Session `71b3fafe` compacted 8 times; each compaction reset the counter to 1, so three
  top-level invocations each ran one iteration and the iteration-3 circuit breaker never
  fired.

Absent on cycle 1. Written from cycle 1's outcome, read from cycle 2 onward.

## Expiry rule — binding, not a suggestion

An entry whose `closed_in_cycle` (or `verified_in_cycle`) is **more than 3 cycles** behind
the current `cycle` is **not** trusted. It is re-opened for audit: a role MAY re-raise it
and the reviser MUST re-audit it.

Within the window (3 cycles or fewer), the entry is authoritative:

- A role does NOT re-raise a `closed_findings` slug unless it has NEW evidence the closure
  was wrong — and if it does, it names that evidence and the cycle that closed it.
- The reviser does NOT re-audit, rewrite, or count toward scope anything in
  `blessed_assertions[]`.

Why 3: verify-plan's own convergence loop is capped at 3 iterations (`protocol.md:151`,
`SKILL.md:176`). The window matches the loop it guards.

## Consumers

- `codex-roles/adversarial.prompt.md` — reads `closed_findings[]` before raising a finding.
- `codex-roles/empirical-domain.prompt.md` — same.
- `~/.claude/get-shit-done/references/planner-revision.md` Step 1 — reads
  `blessed_assertions[]` to know where not to cut.

Passed to the codex fan-out via `--cross-cycle-state "<abs path>"`.
