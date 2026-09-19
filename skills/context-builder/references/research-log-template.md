# research_log.md — Sidecar Audit Trail Template

Every research action writes one structured entry to `01-Projects/<project>/research_log.md`. This is the skill's audit trail — what it queried, what it got, what it applied.

---

## File header (written once when the log is created)

```markdown
---
type: research-log
project: <project-name>
skill: context-builder
created: <YYYY-MM-DD>
---

# Research Log — <project-name>

Structured audit trail of every research action taken during context-building.
Each entry corresponds to one tool invocation or one explicit degrade event.
```

---

## Entry schema

Each entry is a level-2 heading block, appended in chronological order:

```markdown
## {ISO-8601 timestamp} — {tool} — {trigger}

- **query:** "<exact query text, quoted>"
- **tool:** ctx7 | websearch | webfetch | seqthink | firecrawl
- **tokens_est:** <approximate output token count>
- **trigger:** phase0 | rule11-unknown | rumsfeld-q2 | rumsfeld-q4 | premortem | counterfactual
- **budget_used:** <running total of queries this session, e.g. "3 of 5 WebSearch">
- **degrade:** false | true (<reason>) | ALL_FAILED
- **result_summary:** <≤200 words, extracted key findings — thorough but focused>
- **applied_to:** <MECE category number + label> | <Rumsfeld Q{N}> | <open question ID>
- **follow_up:** <next action, if any — further query, user question, added to Q2>
```

---

## Example entry

```markdown
## 2026-04-15T17:42:03Z — ctx7 — rule11-unknown

- **query:** "Supabase Realtime rate limits and reliability characteristics"
- **tool:** ctx7
- **tokens_est:** ~800
- **trigger:** rule11-unknown (user mentioned Supabase Realtime; no prior context in memory)
- **budget_used:** 1 of 2 ctx7 docs
- **degrade:** false
- **result_summary:** Supabase Realtime caps at 200 concurrent connections per project on Free tier; 500 on Pro. Message throughput ~100 msgs/sec per channel. Reconnection logic is client-side; server does not buffer during disconnect. Historical incidents: 2025-Q4 had 3 >30min outages affecting Realtime specifically.
- **applied_to:** MECE-2 (Technical Constraints), Rumsfeld Q2 resolved
- **follow_up:** ask user about expected concurrent-connection count and whether Pro tier is already budgeted
```

---

## Example degrade entry

```markdown
## 2026-04-15T17:45:11Z — websearch — rumsfeld-q4

- **query:** "DGII NCF expiration format E32 B02 2026 policy changes"
- **tool:** websearch
- **tokens_est:** 0
- **trigger:** rumsfeld-q4 (devil's advocate probe on tax compliance)
- **budget_used:** 5 of 5 WebSearch (exhausted)
- **degrade:** ALL_FAILED (WebSearch returned 0 usable results after 2 retries)
- **result_summary:** No current-year DGII NCF policy information surfaced. Question routed to user.
- **applied_to:** MECE-10 (Compliance) → flagged [RESEARCH-BLOCKED]
- **follow_up:** user question in Layer 3: "Has DGII published any NCF format updates for 2026 you're aware of?"
```

---

## When to write to the log

- **Before every research action** (even if the action fails) — ensures audit trail is complete.
- **On every degrade event** — explicit record of why the waterfall escalated.
- **On every Rumsfeld Q2/Q4 resolution** — tie the research finding back to the epistemic quadrant.
- **Never after Phase 5 handoff** — the log is closed when the skill exits.

---

## Log invariants

- Entries are append-only (no in-place edits).
- Timestamps are ISO-8601 UTC.
- `budget_used` is monotonic within a session.
- `result_summary` is bounded at ≤200 words to prevent the log from bloating.
- `follow_up` is either a concrete next action or the literal string `none`.
