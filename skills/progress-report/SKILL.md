---
name: progress-report
description: >-
  Write a structured session progress report to the Obsidian vault + local project. Triggers:
  /progress-report, /session-report, "session summary", "session handoff", "session checkpoint",
  "what did I do this session", "where am I in this session". NOT /postmortem (efficiency +
  anti-pattern audit).
argument-hint: "What will the next session pick up?"
Context: Fork
---
<!-- last_inventoried: 2026-07-11 -->

# /progress-report — Session Progress Report Generator

Dispatches `progress-report-builder` to synthesize a forward-looking progress report from the active session transcript. Writes to vault + local mirror; appends a one-line entry to the vault project page.

## Activation

**TRIGGER ON:**
- `/progress-report`
- Keywords: "progress report", "session report", "what did I do this session", "where am I in this session", "session summary", "session handoff", "session checkpoint"

**SKIP WHEN:**
- User says "skip the report" / "no report"
- Already produced a report in the last 60 seconds (anti-double-fire)

This skill is **manual only**. Slash-command parser intercepts `/clear` and `/compact` before hooks fire (see `~/.claude/CLAUDE.md § Known Limitations`), so auto-trigger on those events is not implementable as a slash-command hook.

## Arguments

| Flag | Semantics |
|---|---|
| *(none)* | Default: write both vault file and local `<cwd>/session-progress-YYYY-MM-DD-<uuid8>.md` mirror. Both report files are per-session, not per-day — see "Session-scoped filenames" below. |
| `--no-project-file` | Skip writing the local `<cwd>/session-progress-*.md` mirror. Still writes the vault file. The skill passes `local_target=""` (empty string) to the builder; the builder guards against null-path writes and writes ONLY the vault file. Use when you want a session checkpoint without leaving a file in the project directory. |
| `--research` / `--auto-research` | Opt in to Research doc generation when the transcript digest detects real research signals. Default is OFF. |
| `--dry-run` | Resolve transcript, digest, and print intended targets/summary inputs only. Do not write reports, Research docs, or project-page entries. |
| `--out <path>` | Write a one-off report to `<path>` only. Do not mutate vault/local mirrors or project page unless a future flag explicitly requests that. |
| `--transcript <path>` | Explicit transcript override for debugging, recovery, or ambiguous active-session selection. |

Future: `--no-vault`, `--no-project-page`.

## Execution

### Step 1 — Parse flags and resolve active transcript

Parse `$ARGUMENTS` first:

- `--no-project-file` → set `LOCAL_TARGET=""`.
- `--research` or `--auto-research` → set `RESEARCH_OPT_IN=true`.
- `--dry-run` → set `DRY_RUN=true`.
- `--out <path>` → set `ONE_OFF_TARGET=<path>` and disable vault/local/project-page mutation.
- `--transcript <path>` → set `TRANSCRIPT_OVERRIDE=<path>`.

Resolve the active transcript through the versioned script. Prefer an explicit `--transcript` or Claude-provided transcript path (`$CLAUDE_TRANSCRIPT_PATH` / `$TRANSCRIPT_PATH`), otherwise derive the Claude project folder from the current `cwd` (`/home/pedro/Projects/Cucina-Nostra/cucina-inventory` → `-home-pedro-Projects-Cucina-Nostra-cucina-inventory`):

```bash
RESOLVE_JSON=$(python3 ~/.claude/skills/progress-report/scripts/digest.py resolve --cwd "$PWD" ${TRANSCRIPT_OVERRIDE:+--transcript "$TRANSCRIPT_OVERRIDE"})
```

If `RESOLVE_JSON.status == "ambiguous"`: fail closed. Print the candidate paths, ask the user to re-run with `--transcript <path>`, and stop. Do not pick the newest file silently.

If `RESOLVE_JSON.status == "error"` or the command exits non-zero: print the error and stop. Do not write any report.

Extract `TRANSCRIPT` from `RESOLVE_JSON.transcript`.

### Step 2 — Digest transcript and resolve project context

Run the bounded digest script once. This reconstructs the active branch, excludes rewound turns, and returns `session_id`, `cwd`, `git_branch`, metrics, read/modified file sets, research signals, and preflight counts.

```bash
DIGEST=$(python3 ~/.claude/skills/progress-report/scripts/digest.py digest "$TRANSCRIPT" "")
```

Extract from `DIGEST`:
- `SESSION_UUID = .session_id`
- `CWD = .cwd` (fallback to `$PWD` only if empty)
- `BRANCH = .git_branch`
- `PROJECT = .vault_project` (resolved vault folder name, or `null` — see resolution below)
- `VAULT_PROJECT_CANDIDATES = .vault_project_candidates`
- `THIN_SESSION = true` when `.event_count < 5` and `.active_msg_count < 30`
- `TOTAL_ENTRIES = .active_msg_count`
- `TOTAL_TOOL_USES = .event_count`

**Mark this run's start.** Run this now, before anything in this skill calls `goals.py`:

```bash
python3 ~/.claude/skills/progress-report/scripts/validate_report.py run-start --session "$SESSION_UUID"
```

`$SESSION_UUID` is bound immediately above, from the digest. The marker records when THIS
run began, and Step 6 measures the goal-run receipt against the LATER of it and the freshness
stamp of the last accepted report — so a receipt entry corroborates this report only when it
postdates both. Without it, the receipt's per-session append-only history means an entry from
any earlier run of the session, a dry run's included, answers for this one; a session's FIRST
report had no stamp at all and so had no floor whatsoever.

Ordering is the whole of it. Every `goals.py` call in this skill happens in Step 4b, below —
a marker written after a legitimate receipt entry would put that entry under this run's own
floor and reject an honest run at Step 6.

Overwriting is intended: a later run of the same session replaces the marker, which is what
drops the earlier run's entries below the new floor. There is no `--ts` flag and no
environment override — the timestamp is produced inside the command, because a floor that
can be supplied as an argument can be backdated, and then it is not a floor.

Non-zero exit: print the error and STOP. Do not continue to Step 3 and do not write a report.
This is the one place in this skill where a failed helper is fatal rather than degraded — the
ledger fallback (`GOAL_ACCOUNTING=ledger-error`) does not apply, because nothing has touched
the ledger yet and `GOAL_ACCOUNTING` is not bound until Step 4b. Continuing would produce a
run that is certain to be rejected at Step 6, after the builder has already written the
report — the late-rejection shape `c11`, `c12` and `c13` all exist to avoid.

**Vault project folder (deterministic — never guess):** the digest resolves `PROJECT = .vault_project` by matching the session `cwd` against the real folders under `01-Projects/` (normalized lowercase/alnum match, plus an explicit GSD-repo alias table for names that diverge — e.g. `cucina-pos → POS`, `cucina-inventory-ship → Cucina Inventory`, `contabilidad-2025 → Contabilidad`). It emits exactly one match or fails closed.

- `.vault_project` is a non-empty string → use it as `<project>`.
- `.vault_project` is `null` → **fail closed. Ask the user** with `AskUserQuestion`, offering `.vault_project_candidates` plus a "create a new folder" option. Do NOT guess and do NOT fall back to the last path segment. Proceed only after the user selects an existing folder or explicitly confirms a new folder name.

**Session-scoped filenames.** Set `UUID8` = the first 8 characters of `SESSION_UUID`
(already bound above from `digest.py`'s `.session_id`). Both report targets carry it, so two
sessions working the same project on the same day write two files instead of superseding each
other:

Vault target: `/home/pedro/Documents/Necronomicon/01-Projects/<project>/progress/Progress Report YYYY-MM-DD <UUID8>.md`
Local target: `<cwd>/session-progress-YYYY-MM-DD-<UUID8>.md`
Project-page target: `/home/pedro/Documents/Necronomicon/01-Projects/<project>/<project>.md`

The mirror **must keep the `session-progress-` prefix** — three hooks match
`/^session-progress-.*\.md$/` (`precompact-report-guard.js:103,132`,
`compact-briefing-gate.js:69`, `compact-briefing-injector.js:60,168`) and a suffix change is
safe where a prefix change is not. Do **not** rename it to "Progress Report".

Do **not** touch the report's H1 either: `validate_report.py:402` pins
`^# Session Progress Report\b` with exactly-one enforcement. Renaming the file is safe;
renaming the heading is not.

Governing vault schema: read `/home/pedro/Documents/Necronomicon/CLAUDE.md` before any vault write.

If `--out <path>` was passed: set `vault_target=<path>`, `local_target=""`, `project_page=""`, and `WRITE_PROJECT_LOG=false`. This is a one-off write path with no vault/project-page side effects.

Scaffold ONLY after `<project>` is explicitly resolved (a non-null `.vault_project`, or a user-confirmed folder from the fail-closed prompt above) — never `mkdir` a guessed name, that fragments history. If the resolved `<project>` folder does NOT exist (only possible for a user-confirmed new folder), create it (`mkdir -p`) along with the `progress/` subfolder, and append one line to `/home/pedro/Documents/Necronomicon/log.md`: `## [YYYY-MM-DD] create | 01-Projects/<project>/ scaffold (progress-report)` (structural vault change; routine report appends do NOT log). Do not create the project-page `<project>.md` if missing — only append to it when it exists (skill reports "project page not found, skipped log entry" in that case).

**GSD-tracked detection + Research folder resolution:**

A vault project is GSD-tracked iff `<vault_project_dir>/GSD Tracker.md` exists. Research writes are OFF by default. Only when `RESEARCH_OPT_IN=true` and the vault project is GSD-tracked, resolve `RESEARCH_FOLDER = <vault_project_dir>/Research` and ensure `Research/` plus `Research/_index.md` exist. When not opted in, set `RESEARCH_FOLDER=""` even if the digest detects research signals.

```bash
PROJECT_DIR="/home/pedro/Documents/Necronomicon/01-Projects/$PROJECT"
if [ "$RESEARCH_OPT_IN" = "true" ] && [ -f "$PROJECT_DIR/GSD Tracker.md" ]; then
  RESEARCH_FOLDER="$PROJECT_DIR/Research"
  mkdir -p "$RESEARCH_FOLDER"
  if [ ! -f "$RESEARCH_FOLDER/_index.md" ]; then
    printf -- '---\ntype: index\n---\n\n# Research Index\n\n| Date | Topic | Sources | Conclusion |\n|---|---|---|---|\n' > "$RESEARCH_FOLDER/_index.md"
  fi
else
  RESEARCH_FOLDER=""
fi
```

First-time creation of `Research/` + `_index.md` is a structural vault change — append one line to `/home/pedro/Documents/Necronomicon/log.md`: `## [YYYY-MM-DD] create | 01-Projects/<project>/Research/ (progress-report)`.

### Step 3 — Detect linked artifacts

**GSD STATE.md:**

```bash
GSD_STATE="$CWD/.planning/STATE.md"
[ -f "$GSD_STATE" ] || GSD_STATE=""
```

**STATE drift** — if `GSD_STATE` exists and its mtime is more than 7 days older than today while this session produced a report, capture its last-touched date so the report can flag the drift (item surfaces in Step 8 + under the builder's Technical Status):

```bash
STATE_DRIFT=""
if [ -n "$GSD_STATE" ]; then
  AGE_DAYS=$(( ( $(date +%s) - $(stat -c %Y "$GSD_STATE") ) / 86400 ))
  [ "$AGE_DAYS" -gt 7 ] && STATE_DRIFT=$(date -d "@$(stat -c %Y "$GSD_STATE")" +%Y-%m-%d)
fi
```

**Active plan** — any plan in `~/.claude/plans/*.md` whose YAML frontmatter `session_uuid` equals `$SESSION_UUID`:

```bash
ACTIVE_PLAN=$(python3 -c "
import sys, pathlib, re
target = sys.argv[1]
for p in pathlib.Path(r'/home/pedro/.claude/plans').glob('*.md'):
    try:
        head = p.read_text(encoding='utf-8', errors='ignore').split('---', 2)
        if len(head) < 3: continue
        m = re.search(r'^session_uuid:\s*(\S+)', head[1], re.M)
        if m and m.group(1).strip() == target:
            print(p); break
    except: pass
" "$SESSION_UUID")
```

Either may resolve to empty string — that is normal. The agent will render "none detected".

### Step 4 — Dry-run gate

If `--dry-run` was passed: print resolved `TRANSCRIPT`, `SESSION_UUID`, `CWD`, `BRANCH`, `vault_target`, `local_target`, `project_page`, `RESEARCH_FOLDER`, `THIN_SESSION`, and the digest's top-level metrics. Then continue into Step 4b, which takes its own read-only branch, and stop where that step says to. Nothing after Step 4b runs: no builder spawn, no report, no Research doc, no project-page entry — and no write to the ledger at any point in the run.

A dry run DOES enter Step 4b. It has to: `goals.py load` is a write path, so "does not write the ledger" is a promise Step 4b must actively keep, not one it can keep by being skipped. The promise is scoped to the ledger, the report, and the project page — NOT to every file: a dry run does write a `goals.py` run receipt (see Step 4b's dry-run stop).

### Step 4b — Goal ledger: load, extract, validate, commit

Skip the rest of this step when `THIN_SESSION=true` — no gate, no ledger write — but
RECORD the skip first, then set `GOAL_ACCOUNTING=thin-session` and carry it to Step 5:
skipping this step still has to be stated in the report, or a skipped step and a step that
silently never ran are the same artifact.

```bash
python3 ~/.claude/skills/progress-report/scripts/goals.py thin-session --project "$PROJECT" --session "$SESSION_UUID" --event-count "$TOTAL_TOOL_USES" --active-msg-count "$TOTAL_ENTRIES"
```

`$TOTAL_TOOL_USES` and `$TOTAL_ENTRIES` are the digest's `.event_count` and
`.active_msg_count` from Step 2 — the same two numbers `THIN_SESSION` was computed from,
passed through unchanged. This call writes NO ledger entry and takes no lock; it leaves
only a run receipt, exactly as a dry run does. Step 6 reads that receipt and rejects a
`thin-session` report whose recorded metrics do not satisfy `event_count < 5 and
active_msg_count < 30`, so a session that is not thin cannot claim to be one. Non-zero
exit: surface the stderr line and set `GOAL_ACCOUNTING=ledger-error` instead — the ledger
must never block the report, and Step 6 will check the `ledger-error` declaration against
the failure this call recorded.

The metrics are the reason this call exists. Without them the receipt would record only
that a skip was claimed, and "an entry exists" standing in for "the condition holds for
this run" is the defect shape this file's other guards were written to remove.

`GOAL_ACCOUNTING` is set exactly once on every path through this step, and Step 5 always
passes it to the builder. The four legal values (contract: `agents/progress-report-builder.md`):

| Value | Set it when |
|---|---|
| `committed=<n> proposed=<n>` | The gate ran. `<n>` committed = decisions in the batch that was committed; `<n>` proposed = `PARSED.proposed` length. Both `>= 0`. |
| `ledger-error` | Any `goals.py` call in this step exited non-zero and you took the documented fallback below. |
| `thin-session` | This step was skipped for `THIN_SESSION=true` AND the skip was recorded by the `goals.py thin-session` call above. Step 6 rejects the report without that receipt entry, or when the metrics it records do not satisfy the thin condition. |
| `not-supplied` | Never set by this skill. It is the builder's value for "the parent gave me nothing", and it always fails validation. If Step 6 rejects the report with `not-supplied`, this step failed to set `GOAL_ACCOUNTING` or Step 5 failed to pass it. |

**Load.** The ledger is per-project and is written ONLY by `goals.py`. Never touch
`~/.claude/goal-ledger/*.json` with Edit, Write, or `>>` — that reintroduces the
lost-update race the lock exists to prevent.

There is exactly ONE load call site in this step and `DRY_RUN` chooses its flags. Run
the if/else below as written — do not lift a branch out of it, and do not add a second
load anywhere in this step:

```bash
if [ "$DRY_RUN" = "true" ]; then
  GOALS_LOAD=$(python3 ~/.claude/skills/progress-report/scripts/goals.py load --read-only --project "$PROJECT" --session "$SESSION_UUID")
else
  GOALS_LOAD=$(python3 ~/.claude/skills/progress-report/scripts/goals.py load --project "$PROJECT" --session "$SESSION_UUID")
fi
```

Without `--read-only`, `cmd_load` runs `autopark` and calls `write_ledger` whenever a
goal has gone 14+ days untouched. So a plain load on a dry run permanently parks live
work, and parks it *consumingly*: the second dry run reports different goals from the
first, because the first one already took the park. `--read-only` makes `write_ledger`
unreachable from `cmd_load` and returns the pending parks as `would_park` rather than
performing them as `parked_now`.

The if/else is the fix, and the shape matters more than the flag. This shipped once as
two separate blocks — a plain load at the top of the step, a `--read-only` load two
hundred lines below it — and a reader following the step in order had already written
the ledger before the read-only variant was ever mentioned. Two blocks read as two
independent invocations, not as a branch. One call site cannot be read that way.

Non-zero exit: surface the stderr line, set `GOALS_JSON=""`, set
`GOAL_ACCOUNTING=ledger-error`, and continue to Step 5 (on a dry run, stop instead —
there is no Step 5). The ledger must never block the report.

Extract `OWNED = GOALS_LOAD.owned` (the array of goal records this session already owns).

**Announce any auto-park immediately.** If `GOALS_LOAD.parked_now` is non-empty, print
each entry's `text` to the user now, before continuing:

```
Auto-parked (untouched 14+ days): <text>
```

On a dry run `parked_now` is always empty and `GOALS_LOAD.would_park` carries the
same list instead. Print that one, labelled for what it is — a park that has NOT
happened:

```
Would auto-park (untouched 14+ days, nothing written): <text>
```

`autopark` is the ONE ungated mutation in this system — nobody approved it and no gate
question was shown for it. The goal simultaneously leaves the report and leaves the
extractor's `stored_goals`, so this run is the only place it can be surfaced; a later
run has no signal that it happened. Surfacing it here as well as in the report (the
builder has its own `Parked this run` rule) means the park survives an abort anywhere
between here and Step 5.

**Extract.** Spawn the extractor. It is read-only — it writes nothing and never calls
`goals.py`.

```
Agent(
  subagent_type="goal-extractor",
  description="Propose session goals",
  prompt=<<<
digest_json:   <bounded digest from Step 2>
stored_goals:  <OWNED>
active_plan:   <ACTIVE_PLAN or "">
Return the <<<GOAL_EXTRACTION>>> block per ~/.claude/agents/goal-extractor.md.
  >>>
)
```

Save the agent's full response text to a scratch file (`$EXTRACTION_FILE`), then
validate it before trusting anything in it — the parser is the last checkpoint before
a hallucinated or malformed proposal can reach the ledger:

```bash
PARSED=$(python3 ~/.claude/skills/progress-report/scripts/goals.py parse-extraction \
  --file "$EXTRACTION_FILE" --session "$SESSION_UUID" --project "$PROJECT")
```

`--session` and `--project` do nothing for the parse itself — the payload is identical
with or without them. They are what makes this call leave a usable run receipt
(`~/.claude/cache/goal-run/<session>.json`), and that receipt is what
`validate_report.py result` requires in Step 6 before it will accept the report at all.
The entry is bound to BOTH: to the session, by the file it lands in, and to the project,
by the slug `--project` records in it. Step 6 matches the report's declared `proposed`
count only against a `parse-extraction` entry carrying THIS project's slug — so an
extraction run for another project, or one that recorded no project because the flag was
dropped, does not corroborate this report. Drop either flag and the report is rejected at
the end of the run, not here.

`PARSED.proposed` is a list of objects, each carrying all eight keys (`id`, `level`,
`parent`, `tag`, `was`, `text`, `evidence`, `origin`) with `null` where inapplicable — treat
`null` and "key absent" as the same thing on `parent`/`was`, but never write a branch
for "absent": the parser guarantees the key is always present.

**Building `SEEN_ONLY_DECISIONS_FILE`.** Both fallbacks in this step fall back to the
same artifact — a decision list containing nothing but `seen` ops — and both build it
the same way. It is specified once, here, so the two cannot drift apart.

Re-read the ledger read-only, then emit `{"op": "seen", "id": <id>}` for every id
present in BOTH `OWNED` and the fresh `owned`, and for no other id:

```bash
GOALS_RECHECK=$(python3 ~/.claude/skills/progress-report/scripts/goals.py load --read-only --project "$PROJECT" --session "$SESSION_UUID")
```

The intersection is load-bearing, not defensive. Several Claude Code sessions run
concurrently on this machine, and `OWNED` was read at the top of this step — before an
extractor run, or before a whole gate's worth of questions, either of which can take
minutes. If another session parks or drops one of those goals inside that window, a
`seen` op built from `OWNED` alone refreshes `last_seen` on a goal that session just
retired. Nothing catches it: the ledger never deletes, so `_find` still resolves the id
and the write lands silently rather than aborting. The record is left asserting a fresh
touch by this session while its status says parked — the decay clock corrupted in the
name of protecting it. Intersecting with the fresh `owned` removes exactly those ids,
because `owned` is `_is_live`-filtered and a goal another session parked or dropped is
not in it.

That re-read is its own process, taking and releasing the lock on its own, so this
narrows the race to the milliseconds between the re-read and the commit. It does not
close it. Nothing across two `goals.py` invocations is atomic and nothing in this step
may be written as though it were — the gain is milliseconds instead of minutes, which
is the most available without moving the fallback inside `goals.py`. A single op there
that holds ONE lock across both the liveness decision and the write would close it
properly; that is future work, and it is not what this is.

**Parse-abort fallback.** Non-zero exit here means the extraction itself is discarded
— but do not simply skip the ledger write. A *persistent* extraction failure (a prompt
regression, a model update that shifts the block format) would otherwise never emit a
`seen` op again, and `autopark` parks any `open` goal whose `last_seen` goes untouched
for `PARK_AFTER_DAYS` (14) — silently, two weeks later, with no event connecting the
park to the failure. So on a parse abort, still commit the seen-only set built above —
those ids come from the ledger, not from the untrusted extractor output, so this is
safe even though the extraction that triggered it is being thrown away:

```bash
python3 ~/.claude/skills/progress-report/scripts/goals.py commit \
  --project "$PROJECT" --session "$SESSION_UUID" --decisions "$SEEN_ONLY_DECISIONS_FILE"
```

Surface the stderr line from the failed `parse-extraction` call, set `GOALS_JSON` to
the (possibly just-refreshed) loaded state, set `GOAL_ACCOUNTING=ledger-error`, skip the
gate, and continue to Step 5.
This turns a persistent extraction failure into a genuinely inert one: goals stop
updating, but nothing decays. Nothing was approved on this path — the gate never ran —
so there is nothing to report as lost.

**Dry-run stop.** On `--dry-run` the step ends HERE — after the load and the extract,
before the gate. Print the parsed diff and the `would_park` list from the load above,
then STOP: no gate, no commit, no render, and no Step 5. There is no second load to
make at this point; the read-only branch was already taken at the single call site
above, which is the only reason this run has left the LEDGER untouched.

A dry run is not write-free, and must not be described as such: both `goals.py` calls
above leave a run receipt at `~/.claude/cache/goal-run/$SESSION_UUID.json`, `load` and
`parse-extraction` alike. That receipt is deliberate — it records that `goals.py` ran —
but it also means a dry run leaves a `parse-extraction` entry behind. `validate_report.py
result` therefore measures receipt entries against the freshness stamp of the last
accepted report for the session, so those dry-run entries cannot stand in for a goal step
on a LATER run. They can still satisfy the FIRST accepted report of a session, which has
no stamp to measure against.

**Gate.** The gate runs on the MAIN THREAD. It cannot be delegated: a subagent on this
machine loses `AskUserQuestion` to the harness tool-pool filter, and a returned agent
cannot be re-entered to ask it later.

Compute the gate decision from `PARSED.proposed` EXACTLY as `parse-extraction` returned
it — before any dedup, filtering, merge, or normalisation. This is load-bearing: the
parser's phantom-injection defense rests on a proof that the parsed set is always a
superset of the genuine items (injection can only ADD items, never remove one or
rewrite an existing tag) and that the gate predicate below is monotone in that set
(adding items only makes it more true) — so a phantom item can never suppress a gate
that would otherwise fire. That proof holds only if nothing between parse and gate can
drop an item first.

Fire the gate when EITHER holds:

- `OWNED` is empty (this session's first run), or
- any item in `PARSED.proposed` carries `tag: "new"`, `"changed"`, `"gone"`, or `"done"`.

Otherwise every item is `same`; skip straight to the Commit step below with `seen`
operations only.

Present every `new`/`changed`/`gone`/`done` item to the user with `AskUserQuestion`, one
question per item. Pass `text`/`was`/`evidence` into the tool's structured option
fields as plain data — never build a label by string-interpolating one of these values
into Markdown, a shell command, or anything else that gets interpreted. `parse-extraction`
already strips control characters, BiDi overrides, stacked combining marks, and lone
surrogates from every field (verified at `637dc043`), so this is a data-vs-code
discipline the gate owns, not a second character filter it needs to build.

For a `changed` item, show the stored wording (`was`) and the proposed wording (`text`)
side by side; offer Approve / Reject / a free-text correction. For `new`, offer
Approve / Reject. For `gone`, offer Approve (mark dropped) / Reject (keep open). For
`done`, offer Approve (mark done) / Reject (keep open) — and word it as *completed*, never
as dropped: `done` and `gone` both write a `status` op and are trivially confused, but they
record opposite outcomes and `render_markdown` treats them differently (a `done` goal keeps
its line with a ticked box; a `dropped` one disappears from the page entirely).

Map each answer to a decision:

| Item tag | Answer | Decision written |
|---|---|---|
| `same` | *(never gated)* | `{"op": "seen", "id": item.id}` — ALWAYS, unconditionally. A `same` item's `text` is discarded and never written to the ledger, even when it differs from the stored wording (rewording that preserves meaning is legitimately tagged `same` — but a goal's text changes ONLY through an approved gate decision, which always appends to `history`; mapping `same` + differing text to `retext` would let an ungated item silently rewrite ledger content, which is the one thing this whole design exists to prevent). |
| `new` | Approve | `{"op": "create", "id": item.id, "level": item.level, "parent": item.parent, "text": item.text, "origin": item.origin}` |
| `new` | Reject | *(nothing — no goal exists yet, there is nothing to keep alive — and, per the cascade rule below, this also drops any other same-batch `new` that names this item's `id` as `parent`)* |
| `changed` | Approve | `{"op": "retext", "id": item.id, "text": item.text}` |
| `changed` | Corrected | `{"op": "retext", "id": item.id, "text": <user's corrected text>}` |
| `changed` | Reject | `{"op": "seen", "id": item.id}` — the reword is discarded, but the goal was clearly touched this session, so bump `last_seen` to avoid an unrelated auto-park. |
| `gone` | Approve | `{"op": "status", "id": item.id, "status": "dropped"}` |
| `done` | Approve | `{"op": "status", "id": item.id, "status": "done"}` — the goal was ACHIEVED. Its checkbox on the rendered Goals page ticks (`render_markdown` draws `[x]` from this status and nothing else), and it stays visible; `dropped` would hide the line instead. Never map a completed goal to `gone`. |
| `done` | Reject | `{"op": "seen", "id": item.id}` — same reasoning as `changed`/Reject. |
| `gone` | Reject | `{"op": "seen", "id": item.id}` — same reasoning as `changed`/Reject. |

`create`, `retext`, and `status` decisions are reachable ONLY from an approved (or
corrected) gate answer — this table is the only place in the skill allowed to emit
those three ops; nothing else in this step should ever call `goals.py commit` with
them. This step does not resolve a `new` subgoal's `parent` itself — it forwards
`item.parent` verbatim into the `create` decision and lets `apply_decisions` own that
resolution. It also forwards `item.id` verbatim as the `create` decision's `id` —
the extractor's batch-local token (`new-1`, `new-2`, ...), never a real ledger id —
so that a same-batch subgoal's `parent` can resolve against it.

`origin` is forwarded verbatim too, for the same reason and with a sharper
consequence: it is the only signal distinguishing a goal traceable to the approved
plan from one merely inferred from the session, and `goals.py render_markdown` marks
every non-`plan:` goal `(inferred)` in the file Pedro reads. Dropping it here would
silently default every approved goal to `extracted` at `apply_decisions` — an
`(inferred)` mark on a goal that came straight out of an approved plan. The parser
guarantees the key is present and holds either `extracted` or `plan:<path>`, so
this row never needs a fallback; `apply_decisions` validates the value again on
arrival rather than trusting the hand-off.

**Cascade rule: rejecting a `new` implicitly rejects its same-batch dependents.**
A rejected `new` item produces no `create` decision at all, so its batch-local token
is never staged. Any other `new` item in the SAME batch whose `parent` names that
token has no parent to belong to — its `create` MUST be dropped too, emitting
nothing for it regardless of the user's own answer for it, rather than forwarding a
`parent` that resolves to nothing. This is the correct semantics, not merely the
convenient one: a sub-goal beneath a goal the user declined has no parent to belong
to, and creating it orphaned would be worse than dropping it. It is also the only
approach that holds: forwarding the `create` unchanged makes `apply_decisions` fall
through to `_find` against the real ledger, fail, and abort the WHOLE batch under
the all-or-nothing contract above — taking every unrelated `seen` op down with it,
which is what lets an ordinary pair of gate answers silently `autopark` untouched
goals two weeks later (same mechanism the parse-abort fallback above exists to
prevent for the whole-batch case).

Apply the drop to a fixed point, not a single pass over the batch. `apply_decisions`
resolves a same-batch `parent` as a bare batch-local id lookup (`if parent in
batch_ids: ... batch_parent_idx = batch_ids[parent]`, `goals.py`) with no check that
the named parent's own `level` is `"goal"` — nothing there stops one `new` subgoal
from naming ANOTHER same-batch `new` subgoal as its `parent`, and `_apply_create`'s
own recursion is written to walk a chain of arbitrary depth ("recursing at most as
many times as `_find_batch_cycle` already proved this batch-local graph is deep").
So a dropped item can itself be the named parent of a further item: re-scan the
newly-dropped set against the remaining batch and repeat until a pass drops nothing.
(Today's extractor contract has every `new` subgoal name a `new` GOAL as `parent`,
never another subgoal — `agents/goal-extractor.md`: "Two levels only: `goal` and
`subgoal`". `apply_decisions` now enforces that against batch-local parents too, not
just against real ledger ones, so a batch bottoms out after a single hop as a matter
of validation rather than as a property of the extractor's prompt. The fixed point is
kept anyway: it costs nothing, it is correct regardless, and it keeps this rule from
depending on a guard living in a different file.)

This rule does NOT reach a `new` subgoal whose `parent` names an EXISTING ledger
goal — an id already in `OWNED`, not a batch-local token. That parent is real
regardless of what else in this batch gets rejected, and `_find` resolves it
against the ledger normally, same as before this rule existed.

**Commit.** Write the full decision set — every `same` item's `seen` op, plus whatever
the gate above produced — to a scratch decisions file and apply it:

```bash
python3 ~/.claude/skills/progress-report/scripts/goals.py commit \
  --project "$PROJECT" --session "$SESSION_UUID" --decisions "$DECISIONS_FILE"
```

**Commit-abort fallback.** Non-zero exit here needs the SAME treatment as the
parse-abort above, for the same reason — this is not a place to merely surface the
error and move on. `apply_decisions` is all-or-nothing: one bad decision aborts every
decision in the batch, *including the `seen` ops for goals that were legitimately
touched this session*. Those goals' `last_seen` then never refreshes, and two weeks
later `autopark` parks live work with nothing linking the park to its cause. Three
ordinary, non-adversarial routes reach this abort: a batch-local id colliding with a
real ledger id, any malformed decision the gate's mapping table produced, and (before
it was moved to parse time) a duplicated batch-local token.

**Retry on exit 2 ONLY.** Branch on the exit code; do not treat every non-zero exit
alike:

| Exit | Meaning | Action |
|---|---|---|
| 2 | a decision in the batch was rejected | retry once, seen-only |
| 3 | lock timeout — another process held the lock for the full cap | no retry |
| 4 | write failure | no retry |
| 5 | lock lost mid-write; ledger NOT modified | no retry |

Only exit 2 is about the batch's CONTENT, and only there is a smaller batch a
genuinely different input with a real chance of succeeding. On 3, 4 and 5 the
decisions were never examined at all — the failure is environmental, so resubmitting a
subset changes nothing about its cause. Exit 3 is the one worth spelling out: a
concurrent writer just held the lock through an entire `LOCK_CAP_MS` wait, and
retrying stacks a second cap-length wait onto a step that has already blocked once,
for a batch no likelier to land than the first.

On exit 2, retry once with the seen-only set specified above — safe for the reason
given there: those ids come from `OWNED` re-checked against the live ledger, not from
the batch that just failed:

```bash
python3 ~/.claude/skills/progress-report/scripts/goals.py commit \
  --project "$PROJECT" --session "$SESSION_UUID" --decisions "$SEEN_ONLY_DECISIONS_FILE"
```

Set `GOAL_ACCOUNTING=ledger-error` on every one of these exits — including the exit-2
path where the seen-only retry then succeeded. The value describes what happened to the
GATE's decisions, and on all of them the gate's decisions were lost. Then continue to
Step 5 either way — the ledger must never block the report.

**Tell the user what it cost, not what the traceback said.** The user just answered a
series of questions and every answer was discarded. `decision[2]: create: text must be
a string` does not tell them that. Report the CONSEQUENCE first and the stderr line as
supporting detail, naming each lost approval in the gate's own wording:

On exit 2 (the retry ran):

```
Goal ledger: your approved goal changes were NOT saved.
  Not saved: <one line per approved create/retext/status decision, in the gate's wording>
  Preserved: activity timestamps for <N> goal(s), so nothing auto-parks because of this.
  Cause: <stderr line from the ORIGINAL failed commit, not the retry's>
  Re-run /progress-report to be asked again.
```

If the retry ALSO failed, append its stderr line and drop the `Preserved` line — the
timestamps did not survive either, and saying they did would be the more damaging
error of the two.

On exit 3, 4 or 5 (no retry ran) the timestamps were never written, so the loss is
strictly larger and the message must not borrow the exit-2 wording:

```
Goal ledger: unavailable this run — nothing was written.
  Not saved: <the same list of approved decisions>
  Not recorded: this session's activity on <N> goal(s), which feeds the 14-day auto-park clock.
  Cause: <stderr line>
  Re-run /progress-report to be asked again.
```

One message for both paths would overstate what the retry protected on exactly the
path where it did not run.

**Announce approved drops.** On a successful commit, print one line for every
`{"op": "status", "status": "dropped"}` decision that was applied:

```
Dropped (approved this run): <text>
```

`<text>` is the `text` of that id's record in `OWNED`. It is the only source: the
decision itself carries no text, and `PARSED.proposed[].was` is `null` on anything not
tagged `changed` (`agents/goal-extractor.md`), so a `gone` item never has one. Note the
asymmetry with the park line above, which prints `parked_now` texts that `cmd_load` has
already routed through `_render_line`; `owned` records come back raw. That matches what
the builder already does with every other `owned` text it renders, so it introduces no
new exposure — but it is not the same guarantee, and the two lines sitting next to each
other should not be read as if it were.

Set `DROPPED_THIS_RUN` to that same list of texts and pass it into the builder in
Step 5, so it reaches the report as well as the chat. It defaults to the empty list and
stays empty on every path that never reaches an applied `status: dropped` decision —
`THIN_SESSION=true`, an unavailable ledger, either abort fallback, a dry run, or simply
a gate at which nothing was dropped. Step 5 always passes it.

Approving `gone` is the only gate answer whose result is INVISIBLE everywhere else. A
dropped goal reaches neither `owned` nor `completed`, so it renders nowhere and the
count line does not move — the user answers a question and the only feedback is an
absence, which is indistinguishable from the answer having been lost. `autopark`
already gets exactly this treatment one step up (**Announce any auto-park
immediately**) for the same reason and it applies with more force here, because here
the user actually decided something. Announcing at both surfaces means the drop
survives an abort anywhere between this point and the report.

Set `GOALS_JSON` to the post-commit `goals.py load` output for the builder.

Set `GOAL_ACCOUNTING=committed=<n> proposed=<m>` — `<n>` = the `applied` count the
`goals.py commit` above returned in its own JSON output, `<m>` = `PARSED.proposed`
length. Read both off the commands' actual output; do not count the decisions file or
the gate's answers yourself. `applied` is the number of decisions that CHANGED
something, which is not always the batch size — a `retext` to the wording already
stored, or a `seen` on a goal already stamped this second, is committed and applied
zero. Step 6 compares `<n>` against the `applied` value `goals.py` recorded on its run
receipt, so a hand-counted `<n>` that disagrees with it rejects the report.

`<n>` = 0 is legal and is the right answer for a gate that ran and changed nothing (for
example, every `new` item rejected) — do not substitute another value to avoid reporting
a zero. `<m>` = 0 is NOT reachable here: `parse-extraction` aborts on a block carrying no
proposals, and that abort takes the parse-abort fallback above, which sets
`GOAL_ACCOUNTING=ledger-error` instead of any counts value. A run declaring
`proposed=0` therefore describes no run that can have happened, and Step 6 rejects it.


### Step 5 — Dispatch the report-generator agent

Spawn `progress-report-builder` with the full context bundle. The agent consumes the already-created digest and writes the report target(s).

```
Agent(
  subagent_type="progress-report-builder",
  description="Synthesize session progress report",
  prompt=<<<
INPUT CONTRACT — all values resolved by the skill, do not re-derive:

transcript_path: <TRANSCRIPT>
session_uuid:    <SESSION_UUID>
cwd:             <CWD>
git_branch:      <BRANCH>
today_iso:       <YYYY-MM-DD>
hh_mm_local:     <HH:MM>
vault_target:    <vault path>
local_target:    <local path>
project_page:    <project page path or "">
gsd_state:       <GSD state path or "">
state_drift:     <STATE_DRIFT date (YYYY-MM-DD) or ""; set only when STATE.md is >7 days stale>
active_plan:     <plan path or "">
thin_session:    <true|false>
total_entries:   <int>
total_tool_uses: <int>
research_folder: <RESEARCH_FOLDER path, or "" if cwd not GSD-tracked>
research_opt_in: <true|false>
goals_json:      <GOALS_JSON, or "" when the ledger was unavailable>
dropped_this_run: <DROPPED_THIS_RUN — the goal texts approved as `gone` this run; empty list when none, and always passed>
goal_accounting: <GOAL_ACCOUNTING from Step 4b — `committed=<n> proposed=<n>` | ledger-error | thin-session. ALWAYS passed; the builder emits `not-supplied` if this line is missing, and that always fails Step 6>
dry_run:         false
digest_json:     <bounded JSON from scripts/digest.py digest>
validator_path:  ~/.claude/skills/progress-report/scripts/validate_report.py
dayfile_writer:  ~/.claude/skills/progress-report/scripts/dayfile-write.cjs

TASK: Use `digest_json`; do not re-parse the raw transcript unless the digest is missing a required field.
Synthesize per `~/.claude/agents/progress-report-builder.md`.
Read the vault CLAUDE.md schema (/home/pedro/Documents/Necronomicon/CLAUDE.md) first, before any vault write.
Write the report via the locked writer (`dayfile_writer`): compose the new H2 block (and the frontmatter+H1 preamble when creating a new day-file) into scratch files, then invoke dayfile-write.cjs with --vault-target/--block-file/--hh-mm, adding --local-target only when local_target is non-empty. Never hand-write vault_target or local_target with Write/Edit or `>>` — the writer owns newest-block-on-top, the superseded marker, locking, and the byte-identical mirror (see the builder's write procedure).
Before returning, validate report markdown with `validate_report.py report` (and byte-identity with `validate_report.py pair` when a local mirror is written). Do NOT run `validate_report.py result` yourself — the skill runs it on your returned block in Step 6. If validation fails, fix the report once and re-run; if it still fails, return a failure summary instead of the success block.

RETURN CONTRACT: emit exactly this block at the end of your response:

<<<PROGRESS_REPORT_RESULT>>>
paths_written:
  - <vault_target>
  - <local_target only when non-empty and actually written>
one_line_summary: <≤90 chars, neutral past-tense, suitable for a project-page log entry>
goal_accounting: <the goal_accounting value passed above, verbatim — REQUIRED line>
research_doc: <absolute path — include ONLY when --research/--auto-research wrote one>
tldr:
  - <bullet 1, ≤80 chars>
  - <bullet 2, ≤80 chars>
  - <bullet 3, ≤80 chars>
<<<END>>>
  >>>
)
```

### Step 6 — Parse the agent's return

Extract the `<<<PROGRESS_REPORT_RESULT>>>` … `<<<END>>>` block from the agent's response. Pull `paths_written`, `one_line_summary`, `tldr`, and the optional `research_doc`.

Run the validator against the agent response and expected targets:

```bash
python3 ~/.claude/skills/progress-report/scripts/validate_report.py result "$AGENT_RESPONSE_FILE" --vault-target "$VAULT_TARGET" ${LOCAL_TARGET:+--local-target "$LOCAL_TARGET"} --stamp-session "$SESSION_UUID" --project "$PROJECT"
```

(`$SESSION_UUID` = the session id from `digest.py resolve` — the same value the guard derives from the transcript filename. `$PROJECT` = the SAME value Step 4b passed to `goals.py`; it is slugified by `goals.py`'s own `slugify`, so pass the project name, not a slug you derived.)

`--project` is required and `result` fails closed without it. `$PROJECT` is re-resolved from the cwd on every invocation, so a single session that touches project A and then project B writes ONE run receipt covering both — without this flag, A's `commit` entry would validate B's report. The flag is what ties the receipt entries to the project this report is actually about — BOTH halves of a counts declaration, the `commit` that answers `committed=<n>` and the `parse-extraction` that answers `proposed=<m>`, must carry this project's slug.

This one command is BOTH the c4 result validation AND the freshness-stamp write: on PASS the validator atomically writes `~/.claude/cache/progress-report-fresh/<session>.json`, which `precompact-report-guard.js` dedups on — the ONLY freshness signal on vault-only runs (`--no-project-file` / `--out`, which leave no `<cwd>/session-progress-*.md` mirror for the guard's cwd scan). The stamp cannot exist without a passing validation, so never write it by hand.

It is ALSO the goal-step check. `--stamp-session` is required here — without it this command fails closed, because the goal check is keyed on the session id and a caller who drops the flag would otherwise skip the check by omission. The validator reads the run receipt `goals.py` left at `~/.claude/cache/goal-run/$SESSION_UUID.json` and rejects the report when:

- the receipt is absent — on every accounting value, `thin-session` included: a thin session skips the ledger work, not the receipt;
- it records a load but no `commit`/`parse-extraction`;
- every recorded `commit`/`parse-extraction` exited non-zero while the block declares counts — a recorded FAILURE to reach a decision is not a decision;
- every entry predates this run's freshness floor — the LATER of the freshness stamp of this session's last accepted report and the run-start marker Step 2 wrote. Receipts are per-session and append-only across runs, so without a floor one entry (a dry run's included) would satisfy every later run of the session. Both halves are needed: the stamp alone leaves a session's FIRST report with no floor at all, since there is no accepted report behind it, and the marker alone would drop the "since the last accepted report" guarantee. The rejection names the floor it used and which of the two files produced it;
- the run-start marker is missing or unreadable — fail closed. A marker that could be made to mean "no floor" by not existing would turn "do not write the marker" into a complete bypass of the floor, which is worse than the hole the marker closes;
- the declared `committed=<n>` / `proposed=<m>` name a project or a count no recorded run matches — the numbers are compared against the `applied` count the commit returned and the proposal count the extraction returned, both for THIS project's slug;
- the block says `thin-session` and no clean thin-session run for THIS project is recorded, or the metrics that run recorded do not satisfy `event_count < 5 and active_msg_count < 30` — the declaration is checked against the digest metrics that define it rather than taken on trust. `validate_report.py` imports that comparison from `goals.py` (`is_thin_session`) rather than restating the numbers;
- the block says `not-supplied`;
- the block says `ledger-error` or `thin-session` and THIS RUN's block in the written report does not carry the builder's `**Goals not recorded this run:**` line. Scoped to the newest `## <hh:mm>` block on purpose: the day-file is append-on-top, so an unscoped check would let an earlier block's line — or another session's, on the same day-file — answer for a run that said nothing.

This is what welds Step 4b to the artifact: the report cannot be accepted while the goal step is silently absent. Either the ledger recorded activity for this run, or the report says on its face why it did not — the last rule above is what makes that second half true of the file rather than only of the return block. Stated exactly: the goal step ran for THIS session, since this run started AND since the last accepted report, against THIS project, and produced the effect the report declares — or the run recorded metrics that justify having skipped it. It does NOT establish that the user was asked anything — the receipt records what `goals.py` did, not who approved it.

If validation fails: report the validator errors and stop. Do not append to the project page and do not print the normal success chat report. The errors name their own next action (file, step, what to re-run) — pass them through rather than paraphrasing.

### Step 7 — Append project-page log entry

If `project_page` exists, append the log entry by **shelling out to the helper** — never with Edit/Write:

```bash
node ~/.claude/skills/progress-report/scripts/vault-append.cjs \
  "$PROJECT_PAGE" \
  "- YYYY-MM-DD HH:MM — [<one_line_summary>](<progress/Progress Report YYYY-MM-DD $UUID8.md>)"
```

**The angle brackets around the link target are required, not decorative.** The filename now
contains spaces, and a bare `](progress/Progress Report ....md)` is not a valid link — the target
ends at the first space and the rest becomes link text. The `](<...>)` form is the CommonMark
destination-in-angle-brackets syntax, which Obsidian also accepts, and it is what makes a
space-bearing path resolve on click.

The project page is a SHARED target: concurrent sessions run this skill against the same page, and per-session dedup flags in `precompact-report-guard.js` mean those runs are no longer serialized. A read-modify-write via Edit/Write loses appends and can tear the YAML frontmatter that the Dataview dashboard parses. The helper holds an advisory lock, writes via tmp+rename, and is idempotent (exact entry already present → `unchanged`, exit 0).

Helper contract: exit 0 = appended or already present (project log `updated`); exit 2 = page missing or bad input (page missing → skip silently, note it in the chat-report step); exit 3 = lock timeout (another session holds the page; surface the stderr line); exit 4 = write failure; exit 5 = lock lost mid-write, page NOT modified. On any non-zero exit, surface the stderr line — do NOT fall back to Edit/Write, that reintroduces the race the helper exists to prevent.

The helper creates the `## Session Log` section at the end of the page if absent, inserts into it when it already exists (correctly, even when later sections like `## Vault Notes` follow it), and splits frontmatter off untouched — `type: project-index`, `status`, `updated`, `disk`, `wake` are passed through byte-for-byte. It appends body sections only.

**Research section (only when `--research`/`--auto-research` was passed and the builder returned `research_doc`):** ensure `<project_page>` has a `## Research` section linking the research index, parallel to `## Session Log`. Use the SAME helper — it already creates the section when absent and dedups the exact line, and it is the same shared page, so an Edit here would reintroduce exactly the race the helper exists to prevent (with a far wider window, since a model read→Edit spans seconds):

```bash
node ~/.claude/skills/progress-report/scripts/vault-append.cjs \
  "$PROJECT_PAGE" \
  "- [[Research/_index|Research Index]]" \
  --section "## Research"
```

### Step 7b — Regenerate the goals mirror

Skip on `--dry-run` and on `--out`.

```bash
python3 ~/.claude/skills/progress-report/scripts/goals.py render \
  --project "$PROJECT" \
  --out "/home/pedro/Documents/Necronomicon/01-Projects/$PROJECT/Goals.md"
```

Then regenerate the cross-project dashboard. Same skips (`--dry-run`, `--out`):

```bash
python3 ~/.claude/skills/progress-report/scripts/goals.py dashboard \
  --out "/home/pedro/Documents/Necronomicon/00-Dashboard/Goals & To-Do.md"
```

**Both calls are non-fatal, and non-fatal must not mean silent.** The report is already written
by this point, so a dashboard failure must not fail the run — but the swallow prints the
command's stderr plus `WARNING: dashboard not refreshed (exit N)`, and the run's chat report
records that the step failed. A dashboard that quietly stops updating across nine concurrent
sessions is precisely the failure this page exists to surface, and silent error handling would
reintroduce it.

Expected non-zero exits worth naming rather than treating as a crash: exit 3 = another session
holds the dashboard lock (bounded 5 s wait, next run reconciles); the stale-render skip is a
clean exit 0 that reports `skipped: stale render` — a ledger changed while this render was in
flight, so writing would have overwritten fresher data.

`Goals.md` is derived output, regenerated wholesale on every run. A hand edit to it is
lost on the next run — the ledger is the source of truth. The same holds for
`00-Dashboard/Goals & To-Do.md`, except that only the region between its `%%GOALS%%` sentinels
is machine-owned; text outside them survives regeneration.

A project with no ledger at all writes no file: the command exits 0 with
`{"ok": true, "skipped": "no ledger for this project"}` and leaves any existing
`Goals.md` untouched. That is not an error and needs no announcement — it is the
normal shape of a first run on a project whose gate committed nothing, which is
exactly what the parse-abort fallback produces when the seen-only set is empty
too. (A `--dry-run` never reaches this step at all — see the skip line above.)
A ledger that exists but holds no visible goals still renders, so a run where the
user approved every `gone` still updates the mirror rather than leaving it
asserting goals they just closed out.

### Step 8 — Chat report (back to user)

Pull the `Resume Here` bullets and the `Session Metrics` line from the report the agent just wrote (read them back from `vault_target` if not already in context). Print exactly:

```
**Session progress report written.**

- Vault: <vault_target>
- Local: <local_target | skipped by --no-project-file/--out>
- Project log: <updated | project page not found, skipped>
- Research: <research_doc path | skipped (default) | none detected>
[**Dropped** — <N> goal(s) you approved as gone: <text>, <text>.]   ← only if DROPPED_THIS_RUN is non-empty
[**Auto-parked** — <N> goal(s) untouched 14+ days: <text>, <text>. Re-state one in a future session to revive it.]   ← only if GOALS_LOAD.parked_now is non-empty
[**Thin session** — under 5 tool calls and under 30 entries; report still generated.]   ← only if thin_session=true
[**STATE drift** — `.planning/STATE.md` last touched <STATE_DRIFT>; reports have moved past it — consider a `/gsd-progress` (or STATE.md) update.]   ← only if STATE_DRIFT is non-empty

**Resume here:**
- <Resume Here bullet 1 — Branch | Phase | Plan>
- <Resume Here bullet 2 — Pick up at>
- <remaining Resume Here bullets>

**Metrics:** <one-line Session Metrics — window, tool calls, files modified/read, errors>

**TL;DR:**
- <bullet 1>
- <bullet 2>
- <bullet 3>
```

## Report template + contracts

The canonical report template, input contract, and return contract live in `~/.claude/agents/progress-report-builder.md` — that agent file **owns** them; this skill no longer mirrors the template (single source of truth, no lockstep drift). The builder renders it: frontmatter + H1 are emitted once per day-file, and on a same-day re-run the new H2-timestamped block is inserted at the TOP with the previously-newest block marked superseded (newest-block-on-top). Report length scales with session substance — thin/clean sessions omit empty sections instead of emitting `- none …` scaffolding. No marketing language or emoji; confidence labels follow the global `HIGH/MED/LOW` rule.

Key semantics (full rules in the agent file):
- **Resume Here** — re-injection-sized bootstrap (≤6 bullets) at the top of each H2 snapshot, so a fresh agent or a post-compaction context can pick up cold; full detail lives in the sections below.
- **Five named sections** — Project Overview / Progress & Execution / Roadblocks / Technical Status / Moving Forward are the umbrella layout when populated; empty sections are omitted (Resume Here + Technical Status + Session Metrics + Confidence are the always-present spine). The enrichments (methods, key decisions, open questions, metrics) fold into them.
- **Progress & Execution → What changed** — `verified` requires explicit downstream evidence (test/read/query/commit/result count). Otherwise use `attempted` or `failed`.
- **Technical Status → Working on** vs **Modified** — a modified path is listed ONLY under Modified; Working-on = `digest_json.files_read_only` + unmodified paths queued in Pending/Next-actions (no double-listing).
- **Roadblocks → Failed attempts** — tool errors + abandoned approaches (corroborated by a correction signal); the section is OMITTED entirely when empty (thin/clean sessions carry no empty scaffolding).
- **Resume Here** and **Session Metrics** are re-derived per H2 snapshot (correct — each snapshot captures current state).
- **Research docs** — opt-in only via `--research` / `--auto-research`; default reports do not write Research notes.

## Distinct from postmortem

| Skill | Purpose | Output |
|---|---|---|
| `/postmortem` | Backward-looking audit: efficiency, anti-patterns, learnings | CLAUDE.md patches + memory entries |
| `/progress-report` | Forward-looking checkpoint: state, pending, next | Vault + local report + project-page log line |

## Out of scope (v1)

- Auto-trigger hooks (deferred — slash-command parser limitation).
- Memory / CLAUDE.md updates (postmortem covers this).
- Efficiency scoring (postmortem covers this).
- Batch reporting across multiple sessions.
- HTML / PDF output.

## Criteria

- [c1] unconditional: The skill MUST resolve the active transcript by calling `scripts/digest.py resolve` via Bash (Step 1) — never by re-deriving the transcript path by hand. Holds even when `--transcript` is supplied, since Step 1 passes the override INTO the script.
  matcher: requires Bash.command contains "digest.py resolve"
  matcher-absence: exhaustive
- [c2] unconditional: The skill MUST run `scripts/digest.py digest` via Bash (Step 2) to produce the bounded digest.
  matcher: requires Bash.command contains "digest.py digest"
  matcher-absence: exhaustive
- [c3] conditional-on: A Bash tool_use containing `digest.py digest` has a tool_result with is_error=false, AND the skill invocation text contains no `--dry-run`, AND no tool_use{name:"AskUserQuestion"} appears after that tool_result. (These three exclude every legitimate abort path: Step 1 ambiguous/error never reaches the digest call; Step 4 dry-run stops before dispatch; `.vault_project == null` fails closed via AskUserQuestion.) — The report MUST be produced by dispatching the dedicated `progress-report-builder` subagent (Step 5), and that spawn MUST occur after the digest call — the skill MUST NOT synthesize or write the report in the main thread.
  matcher: requires Agent.subagent_type equals "progress-report-builder"
  matcher: requires Task.subagent_type equals "progress-report-builder"
  matcher-absence: exhaustive
- [c4] conditional-on: A `progress-report-builder` Agent/Task tool_use has a returned tool_result in the window. — After the builder returns, the skill MUST run `scripts/validate_report.py result` against the agent response via Bash (Step 6). The builder is explicitly forbidden from running this itself — it is the skill's check.
  matcher: requires Bash.command contains "validate_report.py result"
  matcher-absence: exhaustive
  <!-- The literal was `validate_report.py` until Step 2 gained a SECOND call
       into the same script (`run-start`, criterion c14). From that moment a bare
       script-name match was satisfiable by the Step 2 call alone, so a run that
       spawned the builder and never validated its return would have reported c4
       healthy — the c9b failure documented below, in which both literals appear
       and the criterion cannot tell which call happened. `result` is the
       subcommand this criterion is actually about, and `run-start` does not
       contain it. -->
- [c14] conditional-on: A Bash tool_use containing `digest.py digest` returned is_error=false. — The skill MUST run `scripts/validate_report.py run-start --session` via Bash (Step 2) BEFORE any `goals.py` call. The marker it writes is half the freshness floor Step 6 measures the goal-run receipt against, and it is the half every run has: the freshness stamp comes from the last ACCEPTED REPORT, so a session's first report has none and had, until this criterion existed, no floor at all — an entry from any earlier run of the session, a dry run's included, corroborated it. Step 6 fails closed when the marker is absent, so a run that skips this call is rejected at the END, after the builder has written the report. Same late-rejection cost `c11`, `c12` and `c13` exist to avoid, and the same remedy: assert the call at the step that makes it.
  matcher: requires Bash.command contains "validate_report.py run-start"
  matcher-absence: exhaustive
- [c5] conditional-on: The window contains any tool_use whose input references the project-page path `01-Projects/<project>/<project>.md` (i.e. a project-page log entry was attempted). — The project-page append MUST go through `scripts/vault-append.cjs` via Bash — never via Edit or Write on the project page (Step 7 hard mandate; Edit/Write reintroduces the concurrent-append race and can tear the Dataview frontmatter).
  matcher: forbids Edit.file_path contains "01-Projects/"
  matcher: forbids Write.file_path contains "01-Projects/"
- [c6] conditional-on: The skill invocation text in the window contains `--dry-run`. — On `--dry-run` the skill MUST run no further than Step 4b's dry-run stop (Step 4, then 4b's read-only load and extract): no `progress-report-builder` spawn, and no write to vault_target, local_target, the project page, or Research docs. "Stops at Step 4" is the wording this criterion carried while the dry run was in fact entering Step 4b and writing the ledger there — a dry run reaches 4b by design, and what makes it safe is the read-only branch at 4b's single load call site (`c9b`), not an early exit.
  matcher: forbids Agent.subagent_type equals "progress-report-builder"
  matcher: forbids Task.subagent_type equals "progress-report-builder"
- [c7] conditional-on: A Bash tool_use containing `digest.py digest` returned is_error=false AND the digest reported `thin_session` false. — The skill MUST call `goals.py load` before spawning `goal-extractor`; the extractor must never run against an unloaded ledger.
  matcher: requires Bash.command contains "goals.py load"
  matcher-absence: exhaustive
- [c8] unconditional: The goal ledger MUST be written only through `goals.py`. Edit/Write on a `goal-ledger/` path reintroduces exactly the lost-update race the lock exists to prevent.
  matcher: forbids Edit.file_path contains "goal-ledger/"
  matcher: forbids Write.file_path contains "goal-ledger/"
- [c9] conditional-on: The skill invocation text in the window contains `--dry-run`. — On `--dry-run` the skill MUST NOT call `goals.py commit`, `goals.py render`, or `goals.py dashboard`. The dashboard is a vault write like the other two, and enumerating forbidden commands is exactly how `load` slipped past this criterion once already (see `c9b`).
  matcher: forbids Bash.command contains "goals.py commit"
  matcher: forbids Bash.command contains "goals.py render"
  matcher: forbids Bash.command contains "goals.py dashboard"
- [c10] conditional-on: A decision set written in the window carries a `create`, `retext`, or `status` op — i.e. the Step 4b gate table produced a goal-text or goal-status mutation rather than a `seen`-only batch. — The approval gate MUST have run: an `AskUserQuestion` tool_use MUST appear in the window, before the `goals.py commit` that carried those ops. Those three ops are emitted by the Step 4b decision table and by nothing else in this skill, so one reaching the ledger without a preceding gate means a goal was altered with no human approval — the single thing this feature exists to prevent. `c3` already asserts this shape of ordering between two tool calls; the argument that gate prose emits no `tool_use` event holds for the cascade rule, but not here — both halves of this are tool calls.
  matcher: none (judgment)
  <!-- Declared judgment-only, not overlooked. The matcher target grammar in
       scripts/skill-optimizer.cjs is `<Tool>.<field>` with dot segments that
       must each begin with a letter or underscore, and it resolves a candidate
       only when the field is a STRING. `AskUserQuestion`'s input carries
       exactly one field, `questions`, which is an ARRAY — the extractor's
       `wrap()` recurses into lists and preserves that shape rather than
       flattening it, and no array index is expressible in the target grammar.
       So there is no field on this tool any matcher can resolve, and an
       alternative that never resolves reports as a healthy criterion while
       being structurally incapable of matching (the `neverResolvedAlts` hazard
       that file documents). A `predicate-matcher` on `Write.content` for the
       three ops does parse, but the parser rejects it alongside a judgment
       marker ("declares predicate-matcher with no matcher line"), so the
       condition is stated in prose above instead.

       WHAT GUARDS THIS, EXACTLY. tests/test_skill_goal_wiring.sh greps the
       skill BODY (criteria block excluded) and fails when the gate step is
       deleted — verified by deleting it. That is the whole of it, and it is
       much less than "the mechanical regression guard for the gate", which is
       what this comment used to call it. `grep` establishes that the gate's
       PROSE is present. It cannot establish that the gate RUNS, that it runs
       BEFORE the commit, or that the commit carrying `create`/`retext`/
       `status` is the same one the gate preceded — and the ordering is the
       entire invariant. A file can hold every literal this suite looks for and
       still describe a control flow that commits first; F-1 was precisely that
       shape, with `goals.py load --read-only` present and unreachable.
       Deletion is the only regression the grep catches. Rewording escapes it
       (the literals are brittle by construction), and reordering escapes it
       completely.

       FUTURE DESIGN TASK — a gate token. Nothing STRUCTURAL prevents a commit
       that skipped the gate: `goals.py` has no concept of one having run, so a
       decision set carrying the three mutating ops is accepted from any caller
       for any reason. Closing it means `goals.py commit` REQUIRING a token
       that only an answered gate can mint, which makes an ungated mutation
       fail at exit 2 instead of being caught by a text search — enforcement in
       the actor's own code path, per VERIFY-BEFORE-SURFACE. That is a design
       change to the goals.py contract, deliberately not attempted as part of a
       fix pass. Until it exists, this criterion is judgment-enforced with a
       deletion-only backstop, and no reader of this file should conclude
       otherwise. -->

- [c9b] conditional-on: The skill invocation text in the window contains `--dry-run`. — `goals.py load` is itself a write path (`cmd_load` persists `autopark`), so on `--dry-run` the load MUST carry `--read-only`. Enumerating forbidden commands is what let this one through: `c6` and `c9` each list what a dry run must not do and both miss `load`.
  matcher: requires Bash.command contains "goals.py load --read-only"
  <!-- This `requires` is satisfied by a run that ALSO issues an unguarded load:
       both literals appear, and `c9b` reported healthy for the entire life of
       F-1, while --dry-run wrote the ledger on every invocation. It cannot
       distinguish "the read-only load happened" from "a read-only load happened
       somewhere, alongside the write". `c9c` below is the half that
       discriminates, and it is a SEPARATE criterion because it must be: a
       `forbids` added as a second `matcher:` line here is rejected by
       scripts/skill-optimizer.cjs with `mixed matcher polarity ... alternatives
       are any-of and must agree`, which sets `matcher.error` and forces the
       whole criterion INERT — trading a weak live criterion for a dead one, and
       passing every text-level check on the way. -->

- [c9c] conditional-on: The skill invocation text in the window contains `--dry-run`. — On `--dry-run` the skill MUST NOT issue a bare `goals.py load`. This is `c9b`'s other half: `c9b` requires the read-only form to appear, `c9c` forbids the writing form appearing beside it.
  matcher: forbids Bash.command contains "goals.py load --project"
  <!-- Matches by substring and therefore by FLAG ORDER. Step 4b's two branches
       are written `load --read-only --project ...` and `load --project ...`, so
       the violating form contains `goals.py load --project` and the compliant
       one does not. That is a real coupling to the skill's own wording, and it
       is why both branches are written on ONE line with no backslash
       continuation between `load` and its first flag: a continuation puts a
       newline inside the command string and NEITHER literal matches anything.
       Verified by hand against all three forms — compliant, violating, and the
       pre-F-1 continuation, which this criterion would have missed entirely.
       Reorder the flags or re-wrap those lines and this silently stops
       discriminating. The structural check in tests/test_skill_goal_wiring.sh
       does not share the coupling: it asserts the call-site SHAPE, not a flag
       order, and fails on the pre-F-1 file. -->
- [c11] conditional-on: A Bash tool_use containing `validate_report.py result` appears in the window. — That call MUST carry `--stamp-session`. `result` fails closed without the flag, because the goal-ledger receipt check is keyed on the session id; a call missing it is a run that skipped the goal check by omission rather than by decision, which is the exact shape of the 2026-08-13 failure this criterion exists for.
  matcher: requires Bash.command contains "--stamp-session"
  matcher-absence: exhaustive
- [c12] conditional-on: A Bash tool_use containing `validate_report.py result` appears in the window. — That call MUST carry `--project`. `result` fails closed without the flag, because the goal-run receipt is matched on this project's slug. `$PROJECT` is re-resolved from cwd on every invocation, so one session spanning two projects writes a single receipt covering both; without this flag the first project's `commit` entry validates the second project's report, and the declared counts are checked against a run that was about something else.
  matcher: requires Bash.command contains "--project"
  matcher-absence: exhaustive
- [c13] conditional-on: A Bash tool_use containing `digest.py digest` returned is_error=false AND the digest reported `thin_session` true. — The skill MUST call `goals.py thin-session` with `--event-count` and `--active-msg-count` before skipping Step 4b. This is `c7`'s other half: `c7` covers the false branch, where the ledger is loaded and the goal step runs; nothing covered the true branch, so the one path that writes no ledger entry was also the one path with no criterion. Without the call, `validate_report.py result` rejects the report at the END of the run — the same late-rejection shape `c11` and `c12` were added for, and for the same reason: the flag is cheap here and expensive to discover at Step 6.
  matcher: requires Bash.command contains "goals.py thin-session"
  matcher-absence: exhaustive

