# KNOWN-LIMITS.md — Documented Edge Cases + Defaults

Seven edge cases (B1–B7) surfaced during context-building that the v2 skill does NOT fully automate. Each has a documented default behavior.

These are limits, not bugs. They're flagged so the user knows the skill's boundary.

---

## B1 — PRD.md / PM.md already exists at target path

**Scenario:** User invokes the skill a second time for the same project. `01-Projects/<project>/PRD.md` or `PM.md` already exists.

**Default:** The skill asks the user:

```
**File exists conflict:**
`01-Projects/<project>/PRD.md` was written <ISO timestamp>. Options:
(a) overwrite — replace existing file
(b) append-as-revision — add "## Revision <N> — <date>" section at top; preserve prior content
(c) write-with-.v2-suffix — create PRD.v2.md alongside the existing file
Your call:
```

Never silently overwrite. Same pattern for PM.md.

---

## B2 — Interview interrupted mid-session

**Scenario:** Session ends (user closes terminal, context compacts, crash) with the interview incomplete.

**Default:** The skill writes `01-Projects/<project>/interview_state.json` after every user answer. On next invocation with `--resume <project>`, the skill reads the state file and continues from the documented hint.

If the state file is corrupt (JSON parse error), the skill announces the corruption and asks: "Restart interview, or manually reconcile state file?"

---

## B3 — Research phase finds facts that contradict user's stated assumption

**Scenario:** User says "Supabase Realtime handles 10K concurrent connections." Research finds it caps at 500 on Pro tier.

**Default:** The skill does NOT silently override. It surfaces the contradiction back to the user in the next question (Socratic elenchus):

```
You said Supabase Realtime handles 10K concurrent connections. My research finds the Pro-tier cap is 500. Either the docs I found are outdated, your scaling assumption is wrong, or you have a specific arrangement. Which is it?
```

The user's reconciliation becomes the canonical answer. Research-surfaced contradiction is treated as a question, not a correction.

---

## B4 — All research tools fail simultaneously

**Scenario:** WebSearch times out, `ctx7` CLI returns quota error, sequential-thinking MCP not available.

**Default:** The skill posts a single-line degradation notice:

```
**[RESEARCH DEGRADED]** All tools failed for this session. Continuing with user-only interview. Affected MECE categories will be flagged [RESEARCH-BLOCKED] in coverage status.
```

Log the degradation in `research_log.md`. Continue the interview. Do NOT block. All subsequent research attempts skip to the user-question fallback immediately — don't keep retrying failed tools.

---

## B5 — Multi-project context switching mid-interview

**Scenario:** User is mid-interview for project A. User mentions project B. Skill detects the project-name drift.

**Default:** The skill pauses and asks:

```
You mentioned `<project-B>` during an interview for `<project-A>`. Options:
(a) pause project-A interview, switch to project-B (state saved)
(b) treat project-B as context for project-A (no switch)
(c) ignore the mention
Your call:
```

Don't assume the switch. The user may be providing context, not redirecting.

---

## B6 — Skill description field tuning

**Scenario:** The `description` field in SKILL.md frontmatter determines auto-trigger matching. Wrong keywords = skill never fires when it should, or fires on everything.

**Default:** The v2 description explicitly lists trigger keywords: `planning, design, new feature, spec, PRD, requirements, context-gathering, building, architecture, /context-builder, interview, gather context, discuss phase, plan mode, before planning`. Negative anti-trigger list: `NOT for: bug fixes, code review, debugging, operational tasks`.

**Known limit:** The skill cannot self-tune this field. If misfires are observed, the user edits the description directly. CHANGELOG entry required per Decision 18.

---

## B7 — Recursive invocation (context-build a sub-topic mid-interview)

**Scenario:** During a COMPLEX interview, the user asks to context-build a sub-topic that itself needs its own interview.

**Default (v2):** **Not supported.** The skill does NOT invoke itself recursively. If the user needs a sub-interview, they spawn a separate session.

**Future work:** potential v3 enhancement — recursive invocation with state isolation. Not in scope for v2.

---

## Summary of defaults

| Limit | Behavior |
|---|---|
| B1 File exists | Ask user: overwrite / append-revision / .v2-suffix |
| B2 Interrupt | Write `interview_state.json` after every answer; `--resume` supports continuation |
| B3 Research contradicts user | Surface as Socratic question; don't silently override |
| B4 All tools fail | Single-line degrade notice; continue user-only; log; flag categories |
| B5 Multi-project drift | Pause and ask: switch / context / ignore |
| B6 Description tuning | Manual user edit; CHANGELOG entry required |
| B7 Recursive invocation | Not supported in v2; user spawns separate session |
