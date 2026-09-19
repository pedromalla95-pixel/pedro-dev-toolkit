# Research Protocol — Tool Waterfall + Budget + Graceful Degrade

Rigorous, proactive research system. Bounded by explicit budgets; degrades gracefully on tool failure without fail-opening on safety.

---

## Tool waterfall (revised per C1 environment audit)

Research uses a specific, ordered waterfall. Each tool has a distinct purpose.

| Order | Tool | Purpose | Invocation |
|---|---|---|---|
| 1 | **`ctx7` CLI** | Library / framework docs (React, Next.js, Supabase, etc.) | `npx ctx7@latest library <name> "<question>"` → then `npx ctx7@latest docs <id> "<question>"` |
| 2 | **WebSearch** | General search, recent events, practitioner posts | Native Claude Code tool |
| 3 | **WebFetch** | Read a specific URL | Native Claude Code tool |
| 4 | **`mcp__sequential-thinking__sequentialthinking`** | Decomposition / branching analysis (COMPLEX tier only) | MCP tool — verify parameters via ToolSearch before invocation |

### Optional upgrades (if user has enabled these MCPs later)

| Tool | Purpose | Fallback if absent |
|---|---|---|
| Firecrawl MCP | Deep-scrape / bulk crawl | WebFetch |

The skill detects availability at runtime (via ToolSearch or tool-call attempt) and gracefully falls back.

---

## Budget caps

### Phase 0 pre-interview research
- ≤ 5 WebSearch queries
- ≤ 2 `ctx7` doc fetches
- ≤ 1 sequential-thinking MCP pass

### Mid-interview unknown-trigger research
- ≤ 2 queries per trigger (any mix of WebSearch / WebFetch / ctx7)

### Hard ceiling per session
- 15 queries total across the full invocation. At ceiling, skill announces: "Research ceiling reached. Remaining unknowns will be escalated to user per Rumsfeld deadlock protocol."

---

## When to trigger research

- **Phase 0.3 (initial):** unconditional, within budget, if tier is MODERATE+.
- **Rule 11 (mid-interview):** trigger when an answer introduces a term, library, or concept the skill cannot confidently explain. Log the trigger in `research_log.md`.
- **Rumsfeld Q2 resolution:** any Known Unknown flagged during a sweep that is research-resolvable.

---

## Graceful degrade (Decision 15 — never fail-open)

Tool failure cascade:

1. **Tool fails (timeout, error, unavailable):**
   - Log entry in `research_log.md` with `degrade: true` and failure reason.
   - Try the next tool in the waterfall for the same query.

2. **All waterfall tools fail for a query:**
   - Log `degrade: ALL_FAILED` in `research_log.md`.
   - Skip the research attempt for this specific query.
   - Mark the affected MECE category `[RESEARCH-BLOCKED]`.
   - Continue the interview with user-only questions. Do NOT block the interview.

3. **Budget exhausted with Q2/Q4 gaps still open:**
   - Do NOT silently proceed.
   - Invoke Rumsfeld deadlock override (see `rumsfeld-protocol.md`).

**Never fail-open.** A safety check (Q2/Q4 hard-gate) that fails to resolve must downgrade autonomy (escalate to user + require `--force`), not silently pass.

---

## `research_log.md` entry format

Each query — successful or failed — appends one block to the log:

```
## {ISO-timestamp} — {tool} — {trigger}
query: "<exact query text>"
tool: ctx7 | websearch | webfetch | seqthink
tokens_est: <approximate>
trigger: phase0 | rule11 | rumsfeld-q2 | rumsfeld-q4
degrade: false | true (reason) | ALL_FAILED
result_summary: <≤200 words, extracted key findings>
applied_to: <MECE category or Rumsfeld quadrant this resolves>
```

See `research-log-template.md` for the canonical template.

---

## MCP tool parameter discipline (ANTIPATTERNS rule)

Do NOT guess MCP tool parameters. Before embedding an MCP call in the skill:

1. Use ToolSearch: `select:mcp__sequential-thinking__sequentialthinking` (or the relevant tool name).
2. Read the returned schema.
3. Embed exact parameters.

If ToolSearch fails, the schema is not resolvable, or the param-fetch errors, fall back to a structured prose decomposition (or a non-MCP tool in the waterfall for research queries). Log the degrade in `research_log.md` (`degrade: true` + reason). Never stall on a missing or unresolvable MCP — degrade and continue.

---

## Example: resolving a Q2 Known Unknown

User says: "we'll use Supabase Realtime for live updates."

Skill Q2 flag: `[RUMSFELD-GAP: Q2] Supabase Realtime rate limits and reliability characteristics`

Research sequence:
1. `npx ctx7@latest library "Supabase" "Realtime rate limits and reliability"` → get library id
2. `npx ctx7@latest docs /supabase/supabase "Realtime rate limits"` → fetch docs
3. If empty, WebSearch: `"Supabase Realtime rate limits 2026 outage history"` (≤2 WebSearch hits).
4. Log findings in `research_log.md`.
5. Surface back to user: "Supabase Realtime caps at X connections per project. Under load they've documented Y behavior. Does your use case fit within that envelope?"

The question resumes the interview with the gap now filled.
