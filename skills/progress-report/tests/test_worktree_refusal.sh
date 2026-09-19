#!/usr/bin/env bash
# Negative control for acceptance A1.
#
# Proves the pre-fix code PERMITTED a ledger write from inside a linked worktree,
# and the post-fix code refuses it. Both halves run against a throwaway fixture
# repo built by this script in mktemp -d. It never enters, reads, or references
# any real .lane-* worktree.
set -euo pipefail

# Derived from this script's own location, never hardcoded to the main checkout.
# A hardcoded path means a copy of this script running from anywhere else --
# another checkout, a worktree, a scratch copy -- silently tests the main
# checkout's code instead of its own, which is the "tests something other than
# what it claims" failure this control exists to rule out.
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

export GOALS_LEDGER_DIR="$WORK/goal-ledger"
export GOALS_LOCK_DIR="$WORK/cache"
export GOALS_RUN_DIR="$WORK/cache/goal-run"
mkdir -p "$GOALS_LEDGER_DIR" "$GOALS_LOCK_DIR" "$GOALS_RUN_DIR"

# --- fixture repo + linked worktree -----------------------------------------
REPO="$WORK/fixture-repo"
git init -q -b main "$REPO"
git -C "$REPO" config user.email fixture@example.invalid
git -C "$REPO" config user.name Fixture
echo fixture > "$REPO/README.md"
git -C "$REPO" add README.md
git -C "$REPO" commit -qm init
LANE="$WORK/lane-fixture"
git -C "$REPO" worktree add -q -b lane "$LANE"

# --- pre-fix copy: the guard stripped out ------------------------------------
PREFIX_DIR="$WORK/prefix-scripts"
mkdir -p "$PREFIX_DIR"
cp "$SCRIPTS/goals.py" "$PREFIX_DIR/"
# Strip the CALL SITES only. The sed pattern requires leading whitespace before
# the name, so the module-level definition line -- `def assert_not_in_worktree()
# -> None:` -- does not match and survives. That is deliberate: the stripped
# copy must still IMPORT and RUN, and every call site is replaced by `pass`
# rather than deleted so an indented block never ends up empty.
sed 's/^\( *\)assert_not_in_worktree()/\1pass  # guard removed for negative control/' \
  "$SCRIPTS/outcomes.py" > "$PREFIX_DIR/outcomes.py"

# Self-check: assert what we actually mean -- that no CALL survives.
#
# An earlier draft grepped for the bare string 'assert_not_in_worktree()'. That
# string is still in the file, in the `def` line the sed deliberately leaves
# behind, so the check fired "FAIL: sed did not strip it" on every single run
# and this negative control aborted before testing anything.
#
# The `^[[:space:]]\+` prefix is the whole fix: a call site is indented inside a
# function body, the definition is at column 0.
if command grep -qE '^[[:space:]]+assert_not_in_worktree\(\)' "$PREFIX_DIR/outcomes.py"; then
  echo "FAIL: a guard CALL SITE survived the strip; the pre-fix copy is not pre-fix" >&2
  command grep -nE '^[[:space:]]+assert_not_in_worktree\(\)' "$PREFIX_DIR/outcomes.py" >&2
  exit 1
fi

# And prove the check is not vacuous: it MUST fire on the unstripped original.
# A self-check that can never fail would let a silently-failed sed through.
if ! command grep -qE '^[[:space:]]+assert_not_in_worktree\(\)' "$SCRIPTS/outcomes.py"; then
  echo "FAIL: the call-site pattern matches nothing in the real outcomes.py;" >&2
  echo "      the strip check is vacuous and proves nothing" >&2
  exit 1
fi

# "definition retained" was a claim inside a green message that nothing checked.
# Check it: the stripped copy must still carry the definition or it cannot even
# import, and the sed deliberately leaves the column-0 `def` line alone.
if ! command grep -qE '^def assert_not_in_worktree\(\)' "$PREFIX_DIR/outcomes.py"; then
  echo "FAIL: the guard DEFINITION did not survive the strip; the pre-fix copy" >&2
  echo "      cannot import, so nothing below tests the old path" >&2
  exit 1
fi
# Same two-directional standard the call-site check above sets: prove the
# definition pattern is not one that matches nothing everywhere.
if ! command grep -qE '^def assert_not_in_worktree\(\)' "$SCRIPTS/outcomes.py"; then
  echo "FAIL: the definition pattern matches nothing in the real outcomes.py;" >&2
  echo "      the retention check is vacuous and proves nothing" >&2
  exit 1
fi
echo "  ok - guard calls stripped, definition retained, check proven non-vacuous."

echo "== negative control: PRE-FIX behaviour from inside a linked worktree"
set +e
( cd "$LANE" && python3 "$PREFIX_DIR/goals.py" outcome-create \
    --statement "a thing worth doing" --projects proj >/dev/null 2>&1 )
PRE=$?
set -e
if [ "$PRE" -ne 0 ]; then
  echo "FAIL: expected the pre-fix code to PERMIT the write (exit 0), got $PRE" >&2
  echo "      the negative control proves nothing if the old path was already blocked" >&2
  exit 1
fi
# Exit 0 is necessary but not sufficient: the claim is that the old path WROTE
# the ledger from a worktree, so assert the file it should never have made.
if [ ! -e "$GOALS_LEDGER_DIR/outcomes.json" ]; then
  echo "FAIL: pre-fix run exited 0 but wrote no outcomes.json; the old path was" >&2
  echo "      not actually permissive and this control proves nothing" >&2
  exit 1
fi
echo "  ok - pre-fix code permitted the write (exit 0) and outcomes.json landed."
echo "       This is the old bug: a throwaway checkout wrote shared truth."

rm -f "$GOALS_LEDGER_DIR/outcomes.json"

echo "== A1: POST-FIX behaviour from inside the same worktree"
set +e
OUT=$( cd "$LANE" && python3 "$SCRIPTS/goals.py" outcome-create \
         --statement "a thing worth doing" --projects proj 2>&1 )
POST=$?
set -e
if [ "$POST" -ne 2 ]; then
  echo "FAIL: expected exit 2 from the worktree, got $POST" >&2
  echo "$OUT" >&2
  exit 1
fi
case "$OUT" in
  *worktree*) : ;;
  *) echo "FAIL: exit 2 but the message does not mention a worktree; something" >&2
     echo "      else refused this, not the A1 guard" >&2
     echo "$OUT" >&2; exit 1 ;;
esac
case "$OUT" in
  *"$REPO"*) : ;;
  *) echo "FAIL: refusal did not name the canonical path $REPO" >&2
     echo "$OUT" >&2; exit 1 ;;
esac
if [ -e "$GOALS_LEDGER_DIR/outcomes.json" ]; then
  echo "FAIL: the refused write still created outcomes.json" >&2
  exit 1
fi
echo "  ok - exit 2, canonical path named, nothing written."

echo "== control: the SAME command from the main tree still works"
( cd "$REPO" && python3 "$SCRIPTS/goals.py" outcome-create \
    --statement "a thing worth doing" --projects proj >/dev/null )
test -e "$GOALS_LEDGER_DIR/outcomes.json" \
  || { echo "FAIL: the main tree write did not land" >&2; exit 1; }
echo "  ok - main tree writes normally."

# --- the two P17 verbs -------------------------------------------------------
# The P09 guard is PER-VERB, not central: assert_not_in_worktree() is called at
# explicit sites in outcomes.py and goals.py has zero of them, so a new verb
# inherits nothing. Each new verb therefore needs its own case here, or the
# guard is only assumed to be wired.

SPEC="$WORK/a-spec.md"
cat > "$SPEC" <<'SPEC_EOF'
# A fixture spec

%%PENDINGS:BEGIN%%

| id | description | depends on | status |
|---|---|---|---|
| P01 | Delete the dashboard that has had no writer since 2026-08-18. | — | done |
| P02 | Triage the draft plans and report the counts kept and removed. | — | open |

%%PENDINGS:END%%
SPEC_EOF
( cd "$REPO" && python3 "$SCRIPTS/goals.py" pendings-ingest \
    --outcome outcome-0001 --spec "$SPEC" >/dev/null )

echo "== negative control: PRE-FIX outcome-note and pendings-set from the worktree"
set +e
( cd "$LANE" && python3 "$PREFIX_DIR/goals.py" outcome-note \
    --outcome outcome-0001 --text "a note written from a lane." >/dev/null 2>&1 )
PRE_NOTE=$?
( cd "$LANE" && python3 "$PREFIX_DIR/goals.py" pendings-set \
    --outcome outcome-0001 --spec "$SPEC" --row P02 --status active >/dev/null 2>&1 )
PRE_SET=$?
set -e
if [ "$PRE_NOTE" -ne 0 ] || [ "$PRE_SET" -ne 0 ]; then
  echo "FAIL: expected the guard-stripped copy to PERMIT both verbs from a" >&2
  echo "      worktree (exit 0,0), got outcome-note=$PRE_NOTE pendings-set=$PRE_SET;" >&2
  echo "      the refusals below would then prove nothing" >&2
  exit 1
fi
command grep -q "a note written from a lane" "$GOALS_LEDGER_DIR/outcomes.json" \
  || { echo "FAIL: pre-fix outcome-note exited 0 but wrote no history entry" >&2; exit 1; }
command grep -q '^| P02 | .* | active |$' "$SPEC" \
  || { echo "FAIL: pre-fix pendings-set exited 0 but did not write the fence" >&2; exit 1; }
echo "  ok - without the guard both verbs wrote shared truth from a lane."

echo "== A1: POST-FIX outcome-note and pendings-set from the same worktree"
FENCE_BEFORE="$(cat "$SPEC")"
refuse_from_lane() {          # $1 = label, rest = argv for goals.py
  local what="$1"; shift
  local rc out
  set +e
  out=$( cd "$LANE" && python3 "$SCRIPTS/goals.py" "$@" 2>&1 )
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    echo "FAIL: $what was PERMITTED from a worktree (exit 0)" >&2
    echo "$out" >&2; exit 1
  fi
  case "$out" in
    *worktree*) : ;;
    *) echo "FAIL: $what exited $rc but the message never mentions a worktree;" >&2
       echo "      something else refused it, not the A1 guard" >&2
       echo "$out" >&2; exit 1 ;;
  esac
  echo "  ok - $what refused from the worktree (exit $rc), guard named."
}
refuse_from_lane outcome-note \
  outcome-note --outcome outcome-0001 --text "a second note from a lane."
refuse_from_lane pendings-set \
  pendings-set --outcome outcome-0001 --spec "$SPEC" --row P02 --status done
[ "$(cat "$SPEC")" = "$FENCE_BEFORE" ] \
  || { echo "FAIL: the refused pendings-set still edited the spec fence" >&2; exit 1; }
# An `if`, not a `&&` chain: under `set -e` the negative form of a `&&` list is
# the shape most likely to be read as an assertion while never being one.
if command grep -q "a second note from a lane" "$GOALS_LEDGER_DIR/outcomes.json"; then
  echo "FAIL: the refused outcome-note still reached the ledger" >&2
  exit 1
fi
echo "  ok - neither refusal touched the spec or the ledger."

echo "== control: the SAME two verbs from the main tree still work"
( cd "$REPO" && python3 "$SCRIPTS/goals.py" outcome-note \
    --outcome outcome-0001 --text "a note from the main tree." >/dev/null )
( cd "$REPO" && python3 "$SCRIPTS/goals.py" pendings-set \
    --outcome outcome-0001 --spec "$SPEC" --row P02 --status done >/dev/null )
command grep -q '^| P02 | .* | done |$' "$SPEC" \
  || { echo "FAIL: main-tree pendings-set did not set P02 done in the fence" >&2; exit 1; }
python3 - "$GOALS_LEDGER_DIR/outcomes.json" <<'PY_EOF' || exit 1
import json, sys
rec = json.load(open(sys.argv[1]))["outcomes"][0]
p02 = [r for r in rec["pendings"] if r["id"] == "P02"]
if [r["status"] for r in p02] != ["done"]:
    print("FAIL: main-tree pendings-set did not set P02 done in the LEDGER;",
          "fence and ledger disagree", file=sys.stderr)
    raise SystemExit(1)
if not any(h.get("text") == "a note from the main tree." for h in rec["history"]):
    print("FAIL: main-tree outcome-note wrote no history entry", file=sys.stderr)
    raise SystemExit(1)
PY_EOF
echo "  ok - both verbs write fence and ledger from the main tree."

echo
echo "A1 GREEN: worktree write refused, main tree unaffected, old path proved permissive."
