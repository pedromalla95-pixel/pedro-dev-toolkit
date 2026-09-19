#!/usr/bin/env bash
# Acceptance for the SessionEnd ledger hook (hooks/outcome-session-end.js).
#
# The hook is a recorder, not a mutator: on the way out of a session that holds
# a claim it appends ONE mechanical `session-end` history entry and prints the
# next unblocked row. On a session that holds no claim it must be completely
# silent. Every ledger write goes through goals.py -- the hook writes no JSON
# itself -- so this script exercises it exactly as the harness will: payload on
# stdin, exit code and streams out.
#
# Nothing here touches the nine live ledgers under the real ledger directory or
# any real spec file. The fixture is a mktemp -d plus the three GOALS_* repoints,
# same shape as test_worktree_refusal.sh, and the repoint is asserted to have
# actually reached the python child rather than merely being exported here.
set -euo pipefail

# Derived from this script's own location, never hardcoded to the main checkout,
# for the reason test_worktree_refusal.sh states: a hardcoded path means a copy
# of this script silently tests the main checkout's code instead of its own.
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)"
CLAUDE_HOME="$(cd "$SCRIPTS/../../.." && pwd)"
# Overridable so the vacuity control can point this same suite at a stub hook.
# A suite that cannot be made to fail is not evidence; see the note at the foot.
HOOK="${HOOK_UNDER_TEST:-$CLAUDE_HOME/hooks/outcome-session-end.js}"

WORK="$(mktemp -d)"

# --- live-ledger containment guard -------------------------------------------
# Everything below repoints GOALS_* at a fixture, but nothing so far ASSERTED
# that no write escaped to the real ledger. The existing path-inequality check
# further down only proves two paths differ; it never proves nothing landed in
# the live one, so a stub that added a single execFileSync against the real
# directory would pass this suite green. Checksum the live file up front and
# re-check it in the EXIT trap.
REAL_LEDGER_FILE="$CLAUDE_HOME/goal-ledger/outcomes.json"
LIVE_MD5_BEFORE="$(md5sum "$REAL_LEDGER_FILE" 2>/dev/null | cut -d' ' -f1)"
# A missing file would make the whole guard vacuous -- the exact failure shape
# this guard exists to close -- so refuse to run rather than pass for free.
if [ -z "$LIVE_MD5_BEFORE" ]; then
  echo "FAIL: cannot checksum the live ledger at $REAL_LEDGER_FILE;" >&2
  echo "      without a baseline the containment guard proves nothing" >&2
  exit 1
fi

# `rc=$?` first and `exit $rc` last: under `set -e` a bare EXIT trap would
# silently rewrite the suite's verdict with the trap body's own status.
check_live_ledger_untouched() {
  rc=$?
  live_after="$(md5sum "$REAL_LEDGER_FILE" 2>/dev/null | cut -d' ' -f1)"
  if [ "$live_after" != "$LIVE_MD5_BEFORE" ]; then
    echo "FAIL: the LIVE ledger changed during this run." >&2
    echo "      before=$LIVE_MD5_BEFORE after=${live_after:-<missing>}" >&2
    echo "      Either this suite wrote outside its fixture (a real defect)," >&2
    echo "      or a peer session wrote the live ledger while it ran. Check" >&2
    echo "      'git -C $CLAUDE_HOME diff goal-ledger/outcomes.json' before" >&2
    echo "      concluding the code is at fault." >&2
    rc=1
  fi
  # Restore any mode this suite dropped, or rm -rf cannot clean up after it.
  chmod -R u+rwX "$WORK" 2>/dev/null || true
  rm -rf "$WORK"
  exit $rc
}
trap check_live_ledger_untouched EXIT

export GOALS_LEDGER_DIR="$WORK/goal-ledger"
export GOALS_LOCK_DIR="$WORK/cache"
export GOALS_RUN_DIR="$WORK/cache/goal-run"
mkdir -p "$GOALS_LEDGER_DIR" "$GOALS_LOCK_DIR" "$GOALS_RUN_DIR"

# The hook's log is repointed too, so the log-line-count control below counts
# only lines this run produced and no real log is written during a test.
export OUTCOME_SESSION_END_LOG="$WORK/outcome-session-end.log"
LOG="$OUTCOME_SESSION_END_LOG"
: > "$LOG"

SESSION="11111111-2222-3333-4444-555555555555"
OTHER_SESSION="99999999-8888-7777-6666-555555555555"

log_lines() { wc -l < "$LOG" | tr -d ' '; }

# Runs the hook with $1 on stdin. Captures stdout+stderr combined and the exit
# code without letting set -e abort on a non-zero the test means to inspect.
run_hook() {
  set +e
  HOOK_OUT=$(printf '%s' "$1" | node "$HOOK" 2>&1)
  HOOK_RC=$?
  set -e
}

payload_for() {
  printf '{"session_id":"%s","hook_event_name":"SessionEnd","reason":"other"}' "$1"
}

# --- fixture outcome + fence -------------------------------------------------
# cwd is a real non-worktree checkout: the P09 guard that outcome-note inherits
# is per-verb and would refuse the write from inside a linked worktree, and the
# hook deliberately does not override the cwd it is handed.
cd "$CLAUDE_HOME"

python3 "$SCRIPTS/goals.py" outcome-create \
  --statement "a thing worth doing" --projects proj > /dev/null

SPEC="$WORK/a-spec.md"
cat > "$SPEC" <<'SPEC_EOF'
# fixture spec

%%PENDINGS:BEGIN%%

| id | description | depends on | status |
|---|---|---|---|
| P01 | Do the first thing that needs doing. | — | done |
| P02 | Do the second thing that needs doing. | — | open |
| P03 | Do the third thing that needs doing. | P02 | open |

%%PENDINGS:END%%
SPEC_EOF
python3 "$SCRIPTS/goals.py" pendings-ingest \
  --outcome outcome-0001 --spec "$SPEC" > /dev/null

# Guard the fixture itself: if the fence did not land, "next" is null and the
# claimed-path assertion below would pass while testing the empty branch.
python3 - "$GOALS_LEDGER_DIR/outcomes.json" <<'PY_EOF' || exit 1
import json, sys
rec = json.load(open(sys.argv[1]))["outcomes"][0]
if len(rec["pendings"]) != 3:
    print("FAIL: fixture fence did not ingest; the claimed-path test would"
          " exercise the no-rows branch and prove nothing", file=sys.stderr)
    raise SystemExit(1)
PY_EOF
echo "ok - fixture: one outcome, three pending rows, P02 the next unblocked."

# --- case 1: no claim -> silent no-op ---------------------------------------
# Most sessions hold no claim. This path must print nothing at all, so the
# hook cannot become noise on every session close.
echo "== case 1: session holds no claim"
BEFORE=$(log_lines)
run_hook "$(payload_for "$SESSION")"
if [ "$HOOK_RC" -ne 0 ]; then
  echo "FAIL: no-claim path exited $HOOK_RC, expected 0" >&2
  echo "$HOOK_OUT" >&2; exit 1
fi
if [ -n "$HOOK_OUT" ]; then
  echo "FAIL: no-claim path is not silent; it emitted:" >&2
  echo "$HOOK_OUT" >&2; exit 1
fi
AFTER=$(log_lines)
# "exit 0 and empty output" passes identically against a hook that does nothing
# at all. The log line is what distinguishes "deliberately quiet" from "inert",
# and the count comparison below is what distinguishes it from "never ran".
if [ "$AFTER" -le "$BEFORE" ]; then
  echo "FAIL: no-claim path logged nothing (before=$BEFORE after=$AFTER);" >&2
  echo "      silence here is indistinguishable from the hook not running" >&2
  exit 1
fi
command grep -q "skip-no-claim" "$LOG" \
  || { echo "FAIL: log gained a line but it does not say skip-no-claim" >&2; exit 1; }
echo "  ok - exit 0, nothing on stdout or stderr, skip-no-claim logged."

# --- case 2: claim held by a DIFFERENT session ------------------------------
# The join is on claim.session_id. A claim held by someone else must not be
# treated as this session's, or every session close would stamp another
# session's outcome.
echo "== case 2: claim belongs to another session"
python3 "$SCRIPTS/goals.py" outcome-claim \
  --outcome outcome-0001 --session "$OTHER_SESSION" > /dev/null
run_hook "$(payload_for "$SESSION")"
if [ "$HOOK_RC" -ne 0 ] || [ -n "$HOOK_OUT" ]; then
  echo "FAIL: foreign claim was not ignored (rc=$HOOK_RC)" >&2
  echo "$HOOK_OUT" >&2; exit 1
fi
python3 - "$GOALS_LEDGER_DIR/outcomes.json" <<'PY_EOF' || exit 1
import json, sys
rec = json.load(open(sys.argv[1]))["outcomes"][0]
if any(h.get("event") == "session-end" for h in rec["history"]):
    print("FAIL: hook wrote a session-end entry for a claim held by another"
          " session", file=sys.stderr)
    raise SystemExit(1)
PY_EOF
echo "  ok - foreign claim ignored, no history entry written."

# --- case 3: this session holds the claim -----------------------------------
echo "== case 3: this session holds the claim"
python3 "$SCRIPTS/goals.py" outcome-release \
  --outcome outcome-0001 --session "$OTHER_SESSION" > /dev/null
python3 "$SCRIPTS/goals.py" outcome-claim \
  --outcome outcome-0001 --session "$SESSION" > /dev/null
BEFORE=$(log_lines)
# The hook is a RECORDER, not a mutator. Every assertion in this file so far is
# a PRESENCE assertion -- it checks that a history entry appeared -- and a hook
# that also ran `outcome-advance` would satisfy every one of them. Absence has
# to be asserted separately, so snapshot station+status and require them to be
# byte-identical after the run.
station_status() {
  python3 - "$GOALS_LEDGER_DIR/outcomes.json" "$1" <<'PY_EOF'
import json, sys
recs = {r["id"]: r for r in json.load(open(sys.argv[1]))["outcomes"]}
r = recs[sys.argv[2]]
print(f'{r.get("station")}|{r.get("status")}')
PY_EOF
}
STATION_BEFORE="$(station_status outcome-0001)"
run_hook "$(payload_for "$SESSION")"
if [ "$HOOK_RC" -ne 0 ]; then
  echo "FAIL: claimed path exited $HOOK_RC, expected 0" >&2
  echo "$HOOK_OUT" >&2; exit 1
fi
STATION_AFTER="$(station_status outcome-0001)"
if [ "$STATION_BEFORE" != "$STATION_AFTER" ]; then
  echo "FAIL: the hook moved the outcome. station|status went" >&2
  echo "      '$STATION_BEFORE' -> '$STATION_AFTER'. This hook records; it" >&2
  echo "      must never advance a station or flip a status." >&2
  exit 1
fi
# Positive control for case 1: the same assertions there must NOT hold here,
# otherwise "empty output" was never evidence of anything.
if [ -z "$HOOK_OUT" ]; then
  echo "FAIL: claimed path printed nothing; case 1's empty-output assertion" >&2
  echo "      is then vacuous, since both paths would look identical" >&2
  exit 1
fi
case "$HOOK_OUT" in
  *P02*) : ;;
  *) echo "FAIL: claimed path printed something, but not the next unblocked" >&2
     echo "      row P02: $HOOK_OUT" >&2; exit 1 ;;
esac
AFTER=$(log_lines)
if [ "$AFTER" -le "$BEFORE" ]; then
  echo "FAIL: claimed path logged nothing (before=$BEFORE after=$AFTER)" >&2
  exit 1
fi
# The history entry is the actual deliverable. Asserting it in the FIXTURE
# ledger also proves the three GOALS_* repoints reached the python child the
# hook spawned -- if they had not, this write would have gone to the real
# ledger and this file would still show an empty history.
python3 - "$GOALS_LEDGER_DIR/outcomes.json" <<'PY_EOF' || exit 1
import json, sys
rec = json.load(open(sys.argv[1]))["outcomes"][0]
last = rec["history"][-1] if rec["history"] else {}
if last.get("event") != "session-end":
    print(f"FAIL: last history entry is not a session-end entry: {last!r}",
          file=sys.stderr)
    raise SystemExit(1)
if not last.get("text", "").strip():
    print("FAIL: session-end entry carries no text", file=sys.stderr)
    raise SystemExit(1)
if not last.get("at"):
    print("FAIL: session-end entry carries no timestamp", file=sys.stderr)
    raise SystemExit(1)
if "P02" not in last["text"]:
    print(f"FAIL: session-end text does not name the next unblocked row:"
          f" {last['text']!r}", file=sys.stderr)
    raise SystemExit(1)
# Naming the row is not enough: a hook that hardcodes "P02" and never calls
# pendings-next passes every other assertion in this file. The counts are
# derived data -- 1 of 3 rows done, from the fixture fence -- so requiring
# them forces the hook to have actually read the ledger it reports on.
if "1/3" not in last["text"]:
    print(f"FAIL: session-end text carries no 1/3 progress count, so nothing"
          f" proves the hook read pendings-next rather than hardcoding a row"
          f" id: {last['text']!r}", file=sys.stderr)
    raise SystemExit(1)
sessions = [h for h in rec["history"] if h.get("event") == "session-end"]
if len(sessions) != 1:
    print(f"FAIL: expected exactly one session-end entry, found"
          f" {len(sessions)}", file=sys.stderr)
    raise SystemExit(1)
PY_EOF
echo "  ok - exit 0, next row printed, one session-end entry in the FIXTURE"
echo "       ledger (which also proves the repoint reached the child)."

# The repoint claim above is only worth anything if the real ledger directory
# is genuinely elsewhere. Assert the two are not the same path.
REAL_LEDGER="$CLAUDE_HOME/goal-ledger"
if [ "$(cd "$GOALS_LEDGER_DIR" && pwd -P)" = "$(cd "$REAL_LEDGER" 2>/dev/null && pwd -P || echo /nonexistent)" ]; then
  echo "FAIL: the fixture ledger IS the real ledger; this suite has been" >&2
  echo "      writing live state and its repoint assertion means nothing" >&2
  exit 1
fi
echo "  ok - fixture ledger is not the live ledger."

# --- case 4: malformed payload ----------------------------------------------
# Session teardown must not be broken by a payload the hook cannot parse, but
# the failure still has to be observable (OBSERVABLE-ARTIFACTS).
echo "== case 4: malformed stdin payload"
BEFORE=$(log_lines)
run_hook 'not json'
if [ "$HOOK_RC" -ne 0 ]; then
  echo "FAIL: malformed payload exited $HOOK_RC, expected 0" >&2
  echo "$HOOK_OUT" >&2; exit 1
fi
AFTER=$(log_lines)
if [ "$AFTER" -le "$BEFORE" ]; then
  echo "FAIL: malformed payload logged nothing; the failure is invisible" >&2
  exit 1
fi
command grep -q "bad-payload" "$LOG" \
  || { echo "FAIL: malformed payload did not log bad-payload" >&2; exit 1; }
# The diagnostic belongs in the log, not on the session's way out. run_hook
# merges stderr into stdout, so this one assertion covers both streams.
if [ -n "$HOOK_OUT" ]; then
  echo "FAIL: malformed payload printed '$HOOK_OUT'; the quiet paths must" >&2
  echo "      stay quiet on both streams and report via the log only" >&2
  exit 1
fi
echo "  ok - exit 0, nothing printed, bad-payload logged."

# --- case 5: payload carries no session id ----------------------------------
echo "== case 5: payload carries no session id"
BEFORE=$(log_lines)
run_hook '{"hook_event_name":"SessionEnd"}'
if [ "$HOOK_RC" -ne 0 ] || [ -n "$HOOK_OUT" ]; then
  echo "FAIL: missing session id: rc=$HOOK_RC out=$HOOK_OUT" >&2; exit 1
fi
AFTER=$(log_lines)
if [ "$AFTER" -le "$BEFORE" ]; then
  echo "FAIL: missing session id logged nothing" >&2; exit 1
fi
command grep -q "skip-no-session-id" "$LOG" \
  || { echo "FAIL: missing session id did not log skip-no-session-id" >&2; exit 1; }
echo "  ok - exit 0, skip-no-session-id logged."

# --- case 6: claimed outcome with no pending rows ---------------------------
# The no-rows branch is not an edge case, it is the ordinary starting state:
# `pendings-next` returns next=null for every outcome before a fence is
# ingested and again once every row is done. Case 3 asserts P02 appears, so it
# can only ever cover the has-a-row path -- without this case the null branch
# ships untested and would first run in production. The specific hazard is that
# the text degrades to something empty, which `outcome-note` refuses.
echo "== case 6: claim held, but the outcome has no pending rows"
THIRD_SESSION="22222222-3333-4444-5555-666666666666"
python3 "$SCRIPTS/goals.py" outcome-create \
  --statement "a second thing worth doing" --projects proj > /dev/null
python3 "$SCRIPTS/goals.py" outcome-claim \
  --outcome outcome-0002 --session "$THIRD_SESSION" > /dev/null
run_hook "$(payload_for "$THIRD_SESSION")"
if [ "$HOOK_RC" -ne 0 ]; then
  echo "FAIL: no-rows path exited $HOOK_RC, expected 0" >&2
  echo "$HOOK_OUT" >&2; exit 1
fi
if [ -z "$HOOK_OUT" ]; then
  echo "FAIL: no-rows path printed nothing; a claimed session must still" >&2
  echo "      report where it left off" >&2; exit 1
fi
python3 - "$GOALS_LEDGER_DIR/outcomes.json" <<'PY_EOF' || exit 1
import json, sys
recs = {r["id"]: r for r in json.load(open(sys.argv[1]))["outcomes"]}
rec = recs["outcome-0002"]
last = rec["history"][-1] if rec["history"] else {}
if last.get("event") != "session-end":
    print(f"FAIL: no-rows path wrote no session-end entry: {last!r}",
          file=sys.stderr)
    raise SystemExit(1)
if not last.get("text", "").strip():
    print("FAIL: no-rows session-end entry has empty text; outcome-note"
          " rejects that, so this branch cannot have written it",
          file=sys.stderr)
    raise SystemExit(1)
# outcome-0002 has no fence at all, so a row id belonging to outcome-0001's
# fence must not appear here. This is the assertion that catches a hook which
# hardcodes a row instead of reading pendings-next for the outcome in hand.
if "P02" in last["text"]:
    print(f"FAIL: an outcome with no pending rows reported P02, a row from a"
          f" DIFFERENT outcome's fence -- the hook is not reading"
          f" pendings-next: {last['text']!r}", file=sys.stderr)
    raise SystemExit(1)
if "0/0" not in last["text"]:
    print(f"FAIL: no-rows entry does not report the 0/0 state:"
          f" {last['text']!r}", file=sys.stderr)
    raise SystemExit(1)
PY_EOF
echo "  ok - exit 0, entry written with non-empty text on the null-row branch."

# --- case 7: one session holding claims on several outcomes -----------------
# goals.py permits it, and the hook joins with a singular `.find()`-shaped
# lookup, so the second outcome is dropped. The plan specifies that singular
# join and looping would change the stdout contract, so the drop stays -- but
# it must be VISIBLE. Untested silent behaviour is what this round exists to
# close, and this branch had no coverage at all.
echo "== case 7: one session holds claims on two outcomes"
python3 "$SCRIPTS/goals.py" outcome-release \
  --outcome outcome-0002 --session "$THIRD_SESSION" > /dev/null
python3 "$SCRIPTS/goals.py" outcome-claim \
  --outcome outcome-0002 --session "$SESSION" > /dev/null
BEFORE=$(log_lines)
run_hook "$(payload_for "$SESSION")"
if [ "$HOOK_RC" -ne 0 ]; then
  echo "FAIL: multi-claim path exited $HOOK_RC, expected 0" >&2
  echo "$HOOK_OUT" >&2; exit 1
fi
command grep -q "multi-claim .*matched=2" "$LOG" || {
  echo "FAIL: a session holding two claims had one silently dropped -- no" >&2
  echo "      multi-claim line in the log. The drop is allowed; hiding it" >&2
  echo "      is not." >&2
  exit 1
}
# The stdout contract stays singular: exactly one outcome id on one line.
if [ "$(printf '%s\n' "$HOOK_OUT" | command grep -c "outcome-000")" -ne 1 ]; then
  echo "FAIL: multi-claim path printed more than one outcome; the stdout" >&2
  echo "      contract is singular. Got: $HOOK_OUT" >&2
  exit 1
fi
echo "  ok - drop is logged with a match count, stdout stays singular."

# --- case 8: the write fails -------------------------------------------------
# LAST on purpose. This case makes goals.py abort, and an abort inside
# outcomes_lock() that failed to release would wedge every later case behind
# LOCK_ABANDON_MS = 1 hour (goals.py:35).
#
# This is the case the suite could not previously make: deleting the hook's
# `log("error: ...")` and changing `process.exit(1)` to `process.exit(0)` --
# turning a real failure into a silent success -- passed every assertion above,
# because the error path and the quiet success path both print nothing. That
# shape has burned this project three times, so it gets pinned directly.
#
# NOTE on the fixture: chmod 0444 on outcomes.json does NOT work. goals.py
# commits via tmp-file + os.replace, and os.replace over a read-only TARGET
# succeeds -- measured, the write landed and the command exited 0. The
# permission that bites is on the DIRECTORY, where the tmp file is created.
echo "== case 8: the ledger write fails"
BEFORE=$(log_lines)
chmod 0555 "$GOALS_LEDGER_DIR"
run_hook "$(payload_for "$SESSION")"
chmod 0755 "$GOALS_LEDGER_DIR"   # restore before any assertion can exit early
if [ "$HOOK_RC" -eq 0 ]; then
  echo "FAIL: the ledger write failed but the hook exited 0. A" >&2
  echo "      fire-and-forget hook that swallows a failure is a defect" >&2
  echo "      (OBSERVABLE-ARTIFACTS); it must surface both ways." >&2
  exit 1
fi
AFTER=$(log_lines)
if [ "$AFTER" -le "$BEFORE" ]; then
  echo "FAIL: the ledger write failed and nothing was logged" >&2; exit 1
fi
command grep -q "error:" "$LOG" || {
  echo "FAIL: failure logged no line matching 'error:'; non-zero exit alone" >&2
  echo "      leaves no cause behind on a hook nobody is watching" >&2
  exit 1
}
echo "  ok - non-zero exit AND an 'error:' log line."

echo
echo "GREEN: hook records a session-end entry for its own claim, with or"
echo "without pending rows, stays silent without a claim, never moves the"
echo "outcome it records, makes a dropped second claim visible, surfaces a"
echo "failed write both ways, survives malformed input observably, and"
echo "writes nothing outside its fixture."
echo
echo "Vacuity control -- this suite must FAIL against an inert hook:"
echo "  printf 'process.exit(0);' > /tmp/stub-hook.js"
echo "  HOOK_UNDER_TEST=/tmp/stub-hook.js bash ${BASH_SOURCE[0]}"
echo
echo "Each of the four gates below was verified by a stub that defeats it, all"
echo "run via HOOK_UNDER_TEST against a COPY of the hook. A gate nobody has"
echo "watched reject something proves nothing:"
echo "  case 8      swallow  - error path logs nothing and exits 0"
echo "  case 3      mutator  - also runs 'outcome-advance'"
echo "  live md5    escape   - one execFileSync with GOALS_LEDGER_DIR stripped"
echo "  case 7      singular - no multi-claim line when two claims match"
echo "The escape stub writes to the REAL ledger. Back it up first"
echo "(cp goal-ledger/outcomes.json elsewhere) and restore it afterwards."
