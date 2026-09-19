#!/usr/bin/env bash
# SKILL.md must document the goal wiring and carry enforceable criteria for it.
#
# WHY THIS TEST IS SPLIT IN TWO SCOPES
# ------------------------------------
# Every check used to grep the WHOLE file. The `## Criteria` block at the end
# quotes the very strings the wiring checks look for — `goals.py load`,
# `goals.py commit`, `goals.py render`, `goal-ledger/`, `AskUserQuestion` — so
# the criteria block satisfied the probes on its own. Deleting Step 4b and
# Step 7b in their entirety (14,546 characters: load, extractor spawn, gate,
# decision table, cascade rule, commit, render) left 10 of 11 checks PASSING.
# The suite measured whether the criteria block still existed, not whether the
# skill did.
#
# So: wiring checks grep the BODY (everything before `## Criteria`), and
# criteria-existence checks grep the CRITERIA BLOCK. A check that greps the
# wrong half proves nothing, which is exactly how this test became inert.
set -u
# Overridable so a MUTANT copy can be graded. Every check in this file is a grep
# against a Markdown file, so the only way to know a check can fail is to point
# it at a file with the guarded line removed and watch that check turn red. With
# the path hardcoded, a "mutation test" ran against the live SKILL.md and proved
# nothing — round 3 did exactly that. Defaults to the live file, so an unset
# environment behaves as before.
SKILL=${SKILL:-/home/pedro/.claude/skills/progress-report/SKILL.md}
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
FAIL=0

BODY="$TMP/body.md"
CRIT="$TMP/criteria.md"
sed '/^## Criteria/,$d' "$SKILL" > "$BODY"
sed -n '/^## Criteria/,$p' "$SKILL" > "$CRIT"

# The split itself is load-bearing. If the heading ever moves or is renamed,
# BODY silently becomes the whole file and every wiring check below reverts to
# the inert behaviour this rewrite exists to remove — a failure that would
# otherwise present as 11 cheerful PASSes.
if [ ! -s "$CRIT" ]; then
  echo "FAIL: no '## Criteria' heading in $SKILL — body/criteria split did not happen"
  exit 1
fi
if [ "$(wc -c < "$BODY")" -ge "$(wc -c < "$SKILL")" ]; then
  echo "FAIL: body is not smaller than the whole file — the split did not remove the criteria block"
  exit 1
fi

# `--` before the pattern: without it a literal beginning with `-` (any CLI flag
# this suite wants to guard, e.g. `--stamp-session "$SESSION_UUID"`) is parsed
# as an option and grep exits 2 — which reads here as a genuine FAIL for a
# string that is present in the file. Fails loud rather than silently passing,
# but it makes every flag literal unusable, so the helpers take `--`.
body_check() {  # body_check <label> <literal>
  if grep -qF -- "$2" "$BODY"; then echo "PASS: $1"; else echo "FAIL: $1 ($2)"; FAIL=1; fi
}
crit_check() {  # crit_check <label> <literal>
  if grep -qF -- "$2" "$CRIT"; then echo "PASS: $1"; else echo "FAIL: $1 ($2)"; FAIL=1; fi
}

# ── The wiring itself: these must live in the skill's steps, not its criteria ──
body_check "load step"                'goals.py load'
body_check "extractor spawn"          'subagent_type="goal-extractor"'
body_check "commit step"              'goals.py commit'
body_check "render step"              'goals.py render'
# Gate-SPECIFIC, not a bare `AskUserQuestion` grep: the skill uses that tool in
# Step 3 too (`.vault_project == null` fails closed with it), so the bare literal
# survived deleting the entire gate — the same false-positive class as grepping
# the criteria block.
body_check "gate uses AskUserQuestion" 'item to the user with `AskUserQuestion`'
body_check "goals_json in contract"   'goals_json:'
body_check "no hand-editing the ledger" 'goal-ledger/'
# The gate is the whole feature: the decision table is the ONLY place the three
# mutating ops may be emitted, so its absence means a goal can change with no
# human approval.
body_check "gate decision table"      '{"op": "retext"'
body_check "dry-run load is read-only" 'goals.py load --read-only'
# The `new`/Approve row is the ONLY path by which a parsed proposal's fields
# reach the ledger. `origin` shipped missing from it: the row forwarded id,
# level, parent and text, so a goal traceable to an approved plan would have
# been stored as `extracted` and rendered `(inferred)` — had the parser not
# been rejecting the field outright one step earlier. Guarded as a literal
# because deletion is the regression class this suite catches.
body_check "create row forwards origin" '"origin": item.origin'
# Every `item.<field>` the skill reads off a parsed proposal must name a field
# the parser actually returns. Derived from `goals._FIELD_NAMES` at run time,
# never a list hardcoded here — a hardcoded copy is a third place free to
# drift, which is the exact defect class this check exists for.
UNKNOWN_ITEM_FIELDS=$(SKILL="$SKILL" python3 - <<'PY'
import os, re, sys
sys.path.insert(0, "/home/pedro/.claude/skills/progress-report/scripts")
import goals
text = open(os.environ["SKILL"], encoding="utf-8").read()
unknown = sorted(set(re.findall(r"item\.([a-z_]+)", text)) - set(goals._FIELD_NAMES))
print(" ".join(unknown))
PY
)
if [ -z "$UNKNOWN_ITEM_FIELDS" ]; then
  echo "PASS: every item.<field> names a real parser field"
else
  echo "FAIL: every item.<field> names a real parser field (unknown: $UNKNOWN_ITEM_FIELDS)"; FAIL=1
fi

# F-2: the check above is PRESENCE-ONLY, and presence is not the property that
# matters. The `--read-only` variant shipped as a SECOND, later code block while
# an unguarded plain `goals.py load` remained the first action of Step 4b — both
# literals present, the bug fully live, this suite green. A `requires` with no
# matching `forbids` is the inert-probe class this file exists to stop
# producing, so the real property gets asserted structurally below.
#
# PROPERTY: across the fenced code blocks in the BODY, exactly ONE block issues a
# `goals.py load` that lacks `--read-only`, and that block is the DRY_RUN if/else
# (so it names `DRY_RUN` and carries a `--read-only` branch too). Scoped to fenced
# blocks on purpose: prose mentions `goals.py load` in several places, and the
# post-commit reload is prose, so a whole-body grep would measure the wrong text.
# Line continuations count as their own line here — a bare `goals.py load \` is a
# load lacking `--read-only`, which is exactly how the shipped bug was written.
WRITE_BLOCKS=$(awk '
  /^```/ {
    if (inb) { if (bad) print "BLOCK " (dry?1:0) " " (ro?1:0); inb=0 }
    else     { inb=1; bad=0; dry=0; ro=0 }
    next
  }
  inb {
    if (index($0, "goals.py load") > 0 && index($0, "--read-only") == 0) bad=1
    if (index($0, "DRY_RUN") > 0) dry=1
    if (index($0, "--read-only") > 0) ro=1
  }
' "$BODY")
N_WRITE_BLOCKS=$(printf '%s' "$WRITE_BLOCKS" | grep -c '^BLOCK' || true)

if [ "$N_WRITE_BLOCKS" -eq 1 ]; then
  echo "PASS: exactly one ledger-writing load call site"
else
  echo "FAIL: exactly one ledger-writing load call site (found $N_WRITE_BLOCKS)"; FAIL=1
fi
if [ "$N_WRITE_BLOCKS" -eq 1 ] && [ "$WRITE_BLOCKS" = "BLOCK 1 1" ]; then
  echo "PASS: the writing load is inside the DRY_RUN if/else"
else
  echo "FAIL: the writing load is inside the DRY_RUN if/else (got '$WRITE_BLOCKS')"; FAIL=1
fi
# I-3: a commit abort must not silently drop the `seen` ops.
body_check "commit-abort fallback"    'Commit-abort fallback'
# F-3/F-5/F-7. These are PROSE checks and prove only that the prose is present —
# see the honest-limitation note at the top of the criteria checks below. They
# exist because the alternative is no guard at all, not because they establish
# the behaviour.
body_check "seen-only set re-reads the ledger" 'Building `SEEN_ONLY_DECISIONS_FILE`'
body_check "seen-only set intersects live ids" 'present in BOTH `OWNED` and the fresh `owned`'
body_check "retry is scoped to exit 2"         'Retry on exit 2 ONLY'
body_check "abort message names the cost"      'your approved goal changes were NOT saved'
# I-4: a park is announced in the run that performs it — at BOTH surfaces.
# The Step 8 chat line survives deletion of Step 4b, so checking only the
# shared `parked_now` literal would not notice the load-time announcement
# going missing.
body_check "autopark announced at load" 'Announce any auto-park immediately'
body_check "autopark in chat report"    'parked_now'
# F-4: approving `gone` renders nowhere (a dropped goal reaches neither `owned`
# nor `completed`), so without an explicit announcement the user's answer produces
# only an absence. Guarded at BOTH surfaces, same as the park above, and in the
# builder too — the skill can announce in chat and still never reach the artifact.
body_check "drops announced in chat"    'Announce approved drops'
body_check "drops passed to builder"    'dropped_this_run:'
# Third surface, matching auto-park exactly: Step 8's chat report prints a park
# line, so it must print a drop line too or the two decisions Pedro sees at the
# same gate get reported differently for no reason.
body_check "drops in Step 8 chat report" 'only if DROPPED_THIS_RUN is non-empty'
BUILDER=/home/pedro/.claude/agents/progress-report-builder.md
for lit in 'dropped_this_run' 'Dropped this run (approved):'; do
  if grep -qF "$lit" "$BUILDER"; then
    echo "PASS: builder renders drops ($lit)"
  else
    echo "FAIL: builder renders drops ($lit)"; FAIL=1
  fi
done

# The builder's agent file is the DURABLE surface — re-read from disk on every
# spawn, so a requirement placed here survives the compaction that evicted
# SKILL.md in the 2026-08-13 failure. `validate_report.py result` greps the
# written report for this literal and rejects the report without it, so the
# requirement has to be stated where the builder will actually read it.
for lit in 'Goals not recorded this run:' 'ledger-error' 'thin-session'; do
  if grep -qF "$lit" "$BUILDER"; then
    echo "PASS: builder states the missing-goals reason ($lit)"
  else
    echo "FAIL: builder states the missing-goals reason ($lit)"; FAIL=1
  fi
done

# ── WHAT THIS SUITE DOES NOT PROVE ──────────────────────────────────────────
# Read this before trusting a PASS above. Every check here is `grep -qF` against
# a Markdown file. That establishes that a piece of PROSE is present. It does
# not establish that the skill behaves the way the prose describes, because
# nothing here executes the skill.
#
# Concretely, this suite cannot see:
#   - ORDER. `c10` requires the approval gate to run BEFORE the commit that
#     carries create/retext/status. Both literals being present says nothing
#     about which happens first.
#   - REACHABILITY. F-1 shipped with `goals.py load --read-only` present in the
#     file and unreachable at runtime; the presence check for it passed
#     throughout. Only the structural call-site check above closed that, and
#     only for that one call site.
#   - REWORDING. The literals are brittle by construction. Rephrasing a guarded
#     sentence turns its check red (noisy but safe); rephrasing while the
#     behaviour regresses elsewhere is the case that gets missed.
#
# So: deletion is the regression class this catches. F-3, F-4, F-5 and F-7 are
# guarded at this level and no other. Real enforcement of the gate ordering
# needs a gate token inside goals.py — see the c10 comment in SKILL.md, where
# it is recorded as an open design task rather than an existing guarantee.

# ── The criteria that gate the above ──
crit_check "criterion c7"  '[c7]'
crit_check "criterion c8"  '[c8]'
crit_check "criterion c9"  '[c9]'
crit_check "criterion c9b" '[c9b]'
crit_check "criterion c9c" '[c9c]'
# c9b's `requires` passed throughout the entire life of the F-1 bug. Guard the
# `forbids` that actually discriminates, or the criterion can be quietly
# reduced back to a presence probe. It lives on its own criterion because
# alternatives within ONE criterion are any-of and must agree on polarity —
# see the parser check below, which is what established that.
crit_check "c9c forbids a bare load" 'forbids Bash.command contains "goals.py load --project"'

# ── The criteria block must actually PARSE ──────────────────────────────────
# The one check here that is not a text search. Everything above proves a
# criterion's TEXT is present; this proves scripts/skill-optimizer.cjs accepts
# it and that no criterion came back inert.
#
# Written because the grep guard directly above it was not enough, in the most
# literal way available: the `forbids` was first added as a second `matcher:`
# line on c9b itself, `crit_check` went green, and the parser was rejecting the
# whole criterion — `mixed matcher polarity (requires vs forbids) — alternatives
# are any-of and must agree` — with `matcher.error = true`, which forces the
# criterion INERT. The guard written to close F-1's inert probe was itself an
# inert probe, and only running the parser found it. A criterion that does not
# parse is worse than a weak one: c9b went from weak-but-live to dead, and every
# text-level check still read PASS.
#
# TWO THINGS THIS CHECK IS CAREFUL ABOUT
#
# 1. It asserts the optimizer's OWN predicate, `criterionInert`, not a proxy for
#    it. An earlier draft tested `matcher.error` alone — which happens to agree
#    today, but only because `criterionInert` short-circuits on exactly that
#    field. Testing the proxy means a future inertness path added to
#    `criterionInert` lands here silently. `c10` is EXCLUDED by design: it is
#    `judgment-declared` (verified via `inertReason`), a deliberate authoring
#    choice, so it is inert on purpose and asserting otherwise would be wrong.
# 2. It derives the skills root from $SKILL. `detectCriteria` resolves SKILL.md
#    itself from the directory it is handed, so a hardcoded path here would grade
#    the LIVE file while every check above graded $SKILL — two halves of one
#    suite silently measuring different files, which is the same defect class the
#    body/criteria split at the top of this file exists to prevent.
SKILL_DIR=$(dirname "$SKILL")
NODE_CHECK=$(SKILLS_ROOT="$(dirname "$SKILL_DIR")" SKILL_NAME="$(basename "$SKILL_DIR")" node -e '
  const o = require("/home/pedro/.claude/scripts/skill-optimizer.cjs");
  const r = o.detectCriteria({ skillsDir: process.env.SKILLS_ROOT }, process.env.SKILL_NAME);
  if (r.source !== "structured") { console.log("BAD source=" + r.source); process.exit(0); }
  const errs = r.errors || [];
  const inert = r.criteria
    .filter(c => o.criterionInert(c) && o.inertReason(c) !== "judgment-declared")
    .map(c => c.id + "(" + o.inertReason(c) + ")");
  if (errs.length || inert.length)
    console.log("BAD errors=[" + errs.join(" | ") + "] inert=[" + inert.join(",") + "]");
  else console.log("OK " + r.criteria.length);
' 2>&1)
case "$NODE_CHECK" in
  OK\ *) echo "PASS: criteria block parses, no inert criteria ($NODE_CHECK)" ;;
  # Fail closed. node is already a hard dependency of this skill (Step 7 shells
  # out to vault-append.cjs), so a missing interpreter is a broken environment,
  # not a reason to skip a safety check.
  *)     echo "FAIL: criteria block parses, no inert criteria — $NODE_CHECK"; FAIL=1 ;;
esac
crit_check "criterion c10 (approval gate)" '[c10]'
crit_check "criterion c11 (result carries --stamp-session)" '[c11]'
crit_check "criterion c12 (result carries --project)" '[c12]'
# c13 is c7's other half. c7 grades the thin_session=false branch; until round 4
# the true branch — the only path that writes no ledger entry — had no criterion
# at all, so the cheapest route through the skill was also the ungraded one.
crit_check "criterion c13 (thin branch records its skip)" '[c13]'
crit_check "c13 requires the thin-session call" 'requires Bash.command contains "goals.py thin-session"'
crit_check "criterion c14 (run-start marker is written)" '[c14]'
crit_check "c14 requires the run-start call" 'requires Bash.command contains "validate_report.py run-start"'
# c4's matcher MUST name the `result` subcommand. Step 2 now shells out to
# `validate_report.py` as well, so a bare script-name match is satisfied by that
# call alone — a run that spawned the builder and never validated its return
# would grade healthy. Exactly the c9b hazard the criteria block documents at
# length: both literals present, and the criterion unable to say which call
# happened. Asserted in BOTH polarities, because the presence of the tightened
# literal says nothing about whether the loose one is still there beside it.
crit_check "c4 matches the result subcommand" 'requires Bash.command contains "validate_report.py result"'
N_LOOSE_C4=$(grep -cF -- 'requires Bash.command contains "validate_report.py"' "$CRIT" || true)
if [ "$N_LOOSE_C4" -eq 0 ]; then
  echo "PASS: no criterion matches validate_report.py by bare script name"
else
  echo "FAIL: no criterion matches validate_report.py by bare script name (found $N_LOOSE_C4)"; FAIL=1
fi

# ── Task 11: the goal step is welded to the artifact ──
# `parse-extraction` needs neither a session nor a project to do its work; it is
# passed both solely so the call leaves a receipt entry bound to this session AND
# this project. Drop either flag and Step 6 rejects the report at the END of the
# run, with nothing here to say why — so both are guarded as ONE literal at the
# single call site.
#
# ONE literal covering both flags, not two: `body_check` is `grep -qF`, a
# substring match, so a separate `--project "$PROJECT"` check would pass on the
# `goals.py load` line above and stay green with the flag deleted from THIS call
# — the no-signal shape round 2's review caught on `goal_accounting:`. The
# literal below is the whole argument line and matches nowhere else in the file.
# It must stay on ONE line in SKILL.md: `grep -F` cannot span a newline, so
# re-wrapping that call breaks this check loudly rather than silently.
body_check "parse-extraction leaves a receipt" 'parse-extraction \'
body_check "parse-extraction carries --session and --project" \
  '--file "$EXTRACTION_FILE" --session "$SESSION_UUID" --project "$PROJECT"'
# `goal_accounting:` on its own occurs TWICE in the body — once in the builder's
# INPUT contract and once in its RETURN contract — so a check for the bare
# literal stayed green with either line deleted, which is the whole regression
# it was named for. Each half is now anchored to text unique to that half.
# Anchored to distinctive prose rather than a count: a count assertion rots on
# the next edit that legitimately mentions the line again.
body_check "goal_accounting passed into the builder (input contract)" \
  'goal_accounting: <GOAL_ACCOUNTING from Step 4b'
body_check "goal_accounting required back from the builder (return contract)" \
  'goal_accounting: <the goal_accounting value passed above'
# The artifact-side half: the builder must state the reason in the REPORT when
# there are no goals to show, and Step 6 rejects the report when it does not.
body_check "report body must state why goals are missing" \
  '**Goals not recorded this run:**'
# The day-file is append-on-top, so the check is scoped to THIS run's block —
# SKILL.md must not go on promising a whole-file search the code stopped doing.
body_check "stated-reason check is documented as scoped to this run's block" \
  "THIS RUN's block in the written report"
body_check "thin-session sets the accounting" 'GOAL_ACCOUNTING=thin-session'
# THE OPERATIONAL RISK GUARD. Thin sessions are common. If this call site is
# missing or misspelled, every short session's report is rejected at Step 6 —
# /progress-report becomes unusable on exactly the runs it is cheapest to do,
# and the failure surfaces at the END of the run with nothing here to say why.
#
# The whole argument line as ONE literal, for the reason the parse-extraction
# check above documents: a substring like `thin-session --project` would also
# match prose, and `grep -qF` cannot tell which occurrence it found. The unique-
# occurrence assertion below is the other half — a literal that appears twice
# cannot fail for the deletion it guards, which is the round-2 `goal_accounting:`
# defect. It must stay on ONE line in SKILL.md: `grep -F` cannot span a newline,
# so re-wrapping that call breaks this check loudly rather than silently.
THIN_CALL='goals.py thin-session --project "$PROJECT" --session "$SESSION_UUID" --event-count "$TOTAL_TOOL_USES" --active-msg-count "$TOTAL_ENTRIES"'
body_check "thin session records its skip" "$THIN_CALL"
N_THIN_CALL=$(grep -cF -- "$THIN_CALL" "$BODY" || true)
if [ "$N_THIN_CALL" -eq 1 ]; then
  echo "PASS: the thin-session call literal occurs exactly once"
else
  echo "FAIL: the thin-session call literal occurs exactly once (found $N_THIN_CALL)"; FAIL=1
fi
# The metrics are what make the recorded skip checkable, so the two variables the
# call passes must be ones the file actually BINDS from the digest, and binds
# EARLIER in the file than the call that reads them. A call passing an unbound
# variable sends empty strings, `goals.py` rejects them at argparse, and the
# report is rejected at Step 6 with nothing here to say why. Order is asserted by
# line number rather than assumed — presence of both lines says nothing about
# which comes first, and this suite's own limitations note calls out ORDER as the
# thing it cannot normally see.
THIN_CALL_LINE=$(grep -nF -- "$THIN_CALL" "$BODY" | head -1 | cut -d: -f1)
for var in TOTAL_TOOL_USES TOTAL_ENTRIES; do
  BIND_LINE=$(grep -nE -- "^- \`$var = \." "$BODY" | head -1 | cut -d: -f1)
  if [ -n "$BIND_LINE" ] && [ -n "$THIN_CALL_LINE" ] && [ "$BIND_LINE" -lt "$THIN_CALL_LINE" ]; then
    echo "PASS: $var is bound from the digest above the thin-session call"
  else
    echo "FAIL: $var is bound from the digest above the thin-session call (bind='$BIND_LINE' call='$THIN_CALL_LINE')"; FAIL=1
  fi
done
# THE RUN-START MARKER. Every run carries its own freshness floor because Step 2
# writes this marker; without the call, Step 6 fails closed and rejects the
# report at the END of the run, after the builder has already written it. Same
# literal-on-one-line discipline as the thin-session call above, and for the same
# reasons: `grep -F` cannot span a newline, and a substring like
# `validate_report.py run-start` alone would also match the prose around it.
RUNSTART_CALL='python3 ~/.claude/skills/progress-report/scripts/validate_report.py run-start --session "$SESSION_UUID"'
body_check "run-start marker is written" "$RUNSTART_CALL"
N_RUNSTART=$(grep -cF -- "$RUNSTART_CALL" "$BODY" || true)
if [ "$N_RUNSTART" -eq 1 ]; then
  echo "PASS: the run-start call literal occurs exactly once"
else
  echo "FAIL: the run-start call literal occurs exactly once (found $N_RUNSTART)"; FAIL=1
fi
# ORDER, asserted by line number rather than assumed — and this one is not a
# convenience, it is the invariant. The marker is a floor: written BEFORE the
# goal step it is the start of this run, written AFTER it, it puts an honest
# run's own receipt entry underneath its own floor and rejects a run that did
# everything right. `$SESSION_UUID` must likewise be bound above the call, or the
# call passes an empty string and argparse rejects it.
#
# The lower bound greps `scripts/goals.py`, the INVOCATION form, not the bare
# name: `goals.py` appears in prose throughout Step 4b, and the earliest prose
# mention sits above the earliest call — a check anchored on prose would read as
# a violation while the file was correct, and would go on reading that way.
RUNSTART_LINE=$(grep -nF -- "$RUNSTART_CALL" "$BODY" | head -1 | cut -d: -f1)
UUID_BIND_LINE=$(grep -nE -- '^- `SESSION_UUID = \.' "$BODY" | head -1 | cut -d: -f1)
FIRST_GOALS_LINE=$(grep -nF -- 'scripts/goals.py' "$BODY" | head -1 | cut -d: -f1)
if [ -n "$RUNSTART_LINE" ] && [ -n "$UUID_BIND_LINE" ] && [ "$UUID_BIND_LINE" -lt "$RUNSTART_LINE" ]; then
  echo "PASS: SESSION_UUID is bound from the digest above the run-start call"
else
  echo "FAIL: SESSION_UUID is bound from the digest above the run-start call (bind='$UUID_BIND_LINE' call='$RUNSTART_LINE')"; FAIL=1
fi
if [ -n "$RUNSTART_LINE" ] && [ -n "$FIRST_GOALS_LINE" ] && [ "$RUNSTART_LINE" -lt "$FIRST_GOALS_LINE" ]; then
  echo "PASS: the run-start marker is written before the first goals.py call"
else
  echo "FAIL: the run-start marker is written before the first goals.py call (marker='$RUNSTART_LINE' goals='$FIRST_GOALS_LINE')"; FAIL=1
fi
# A failed marker write is FATAL, not degraded. The ledger fallback does not
# apply — nothing has touched the ledger at Step 2 and GOAL_ACCOUNTING is not
# bound yet — so a run that continued would be certain to be rejected at Step 6
# with the report already written.
body_check "a failed run-start stops the run" \
  'Non-zero exit: print the error and STOP.'
body_check "ledger failure sets the accounting" 'GOAL_ACCOUNTING=ledger-error'
body_check "result call carries --stamp-session" '--stamp-session "$SESSION_UUID"'
# The receipt is per-SESSION, but `$PROJECT` is re-resolved from cwd on every
# invocation — so one session spanning two projects writes ONE receipt covering
# both. Without this flag the first project's `commit` entry validates the
# second project's report. Guarded as a literal at its single call site, exactly
# as --stamp-session is.
body_check "result call carries --project" '--project "$PROJECT"'
# The declared `committed=<n>` is compared against the `applied` count goals.py
# recorded, so SKILL.md must tell the reader to read `<n>` off the commit's own
# output rather than counting the decisions batch — those two numbers differ
# whenever a decision was a no-op.
body_check "committed count is defined as commit's applied value" \
  'the `applied` count the'
body_check "proposed=0 is documented as unreachable" 'is NOT reachable here'

# Every legal `goal_accounting` word documented in SKILL.md must be one
# `validate_report.py` actually accepts. Derived from the validator at run time,
# never a list hardcoded here — a hardcoded copy is a third place free to drift,
# which is the same defect class the item.<field> check above exists for.
UNKNOWN_ACCOUNTING=$(BODY="$BODY" python3 - <<'PY'
import os, re, sys
sys.path.insert(0, "/home/pedro/.claude/skills/progress-report/scripts")
import validate_report
text = open(os.environ["BODY"], encoding="utf-8").read()
# The token stops at the first character that cannot be part of a value, so
# markdown punctuation around the assignment (a closing backtick, a comma) is
# never captured. The trailing `=?` is only there to recognise — and then drop —
# the counts form (`GOAL_ACCOUNTING=committed=<n> proposed=<m>`), whose shape is
# checked by the validator's own regex rather than by this word list.
used = {token for token in re.findall(r"GOAL_ACCOUNTING=([a-z][a-z-]*=?)", text)
        if not token.endswith("=")}
print(" ".join(sorted(used - set(validate_report._GOAL_ACCOUNTING_WORDS))))
PY
)
if [ -z "$UNKNOWN_ACCOUNTING" ]; then
  echo "PASS: every GOAL_ACCOUNTING word is one the validator accepts"
else
  echo "FAIL: every GOAL_ACCOUNTING word is one the validator accepts (unknown: $UNKNOWN_ACCOUNTING)"; FAIL=1
fi

# Every threshold SKILL.md states in prose must be the one the code enforces.
# The comparison itself lives in exactly one place (`goals.is_thin_session`, which
# `validate_report.py` imports rather than restating), but the file describes it
# in English in three separate steps — and English is where a threshold change
# silently rots, because nothing executes it. Derived from the constants at run
# time, never hardcoded here: the same defect class the item.<field> and
# GOAL_ACCOUNTING checks above exist for. Fails loud if the prose stops mentioning
# the numbers at all, so this cannot pass vacuously.
THIN_PROSE=$(BODY="$BODY" python3 - <<'PY'
import os, re, sys
sys.path.insert(0, "/home/pedro/.claude/skills/progress-report/scripts")
import goals
text = open(os.environ["BODY"], encoding="utf-8").read()
problems = []
for field, limit in (("event_count", goals.THIN_EVENT_COUNT_MAX),
                     ("active_msg_count", goals.THIN_ACTIVE_MSG_COUNT_MAX)):
    stated = re.findall(rf"{field}\s*<\s*(\d+)", text)
    if not stated:
        problems.append(f"{field}: SKILL.md states no threshold at all")
        continue
    wrong = sorted({value for value in stated if int(value) != limit})
    if wrong:
        problems.append(f"{field}: SKILL.md says {', '.join(wrong)}, goals.py says {limit}")
print(" | ".join(problems))
PY
)
if [ -z "$THIN_PROSE" ]; then
  echo "PASS: SKILL.md's stated thin thresholds match goals.py's constants"
else
  echo "FAIL: SKILL.md's stated thin thresholds match goals.py's constants ($THIN_PROSE)"; FAIL=1
fi

# The gate must be described as main-thread; a subagent cannot ask.
if grep -q 'subagent' "$BODY" && grep -qi 'main thread' "$BODY"; then
  echo "PASS: gate placement documented"
else
  echo "FAIL: gate placement not documented"; FAIL=1
fi

exit $FAIL
