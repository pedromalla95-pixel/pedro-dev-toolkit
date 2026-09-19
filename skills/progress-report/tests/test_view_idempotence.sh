#!/usr/bin/env bash
# Acceptance A3, scripted: "every VIEW regenerates idempotently -- running the
# generator twice with no ledger change produces a byte-identical file."
#
# Every WRITE goes to a `mktemp -d` copy of the vault and a copy of the nine
# live ledgers: it never writes /home/pedro/Documents/Necronomicon and never
# writes ~/.claude/goal-ledger.
#
# It does READ the real vault, and that is not a slip. goals.py binds
# VAULT_PROJECTS at import (`:1908`) to the real 01-Projects and NO env var
# repoints it -- `main()` repoints only LEDGER_DIR/LOCK_DIR/RUN_DIR -- so
# `render` reads each project's real `progress/` notes to build the handoff
# card it embeds. A peer session writing a progress note between the two passes
# therefore produces a TRUE diff that is not a non-idempotence, which is why
# fingerprint() covers that directory as well as git and STATE.md.
#
# The `.lane-*` claim is two different strengths, do not read them as one:
# fingerprint()'s own scan skips dot-dirs, which is enforced below. The
# generator's scan is portfolio-refresh.cjs's, inherited and NOT verified here.
#
# WHAT TWO GREEN PASSES DO AND DO NOT PROVE. They prove the generators are at a
# fixed point from the state the vault was in when the copy was taken. They do
# NOT prove convergence from an arbitrary state, because at least one flag is
# computed from the generator's own previous output: REGION-LAG fires when a
# note's `refreshed` mark trails git by >36h, and the region write is what sets
# that mark. From a stale start, pass 1 writes the flag and a fresh mark, pass 2
# writes neither -- measured, on a copy, 2026-08-28. The real vault is normally
# already converged, so an ordinary GREEN run does not exercise that path.
#
# Usage: test_view_idempotence.sh [--unpinned]
#   default    PORTFOLIO_NOW_MS pinned -- byte content is clock-independent, so
#              a diff is a real non-determinism rather than the wall clock.
#   --unpinned the clock runs. This is the half that carries A3's actual value:
#              A3 is a property of an unpinned run.
#
# EXIT: 0 A3 GREEN . 1 A3 RED (a target differed -- ONLY that) . 2 the harness
#   itself aborted (bad usage, a generator crashed, the enumeration disagreed
#   with the vault, the S1 control failed) . 6 A3 INDETERMINATE (the inputs
#   moved between passes, the two passes skipped different index notes, or the
#   index-note comparison was vacuous).
#   1 is reserved for a real acceptance failure so a caller keying off it cannot
#   confuse a broken harness with a regression -- the same discipline that made
#   6 a new code rather than an overload of 0/2/3/4/5.
set -euo pipefail

# Every harness abort goes through this, so "the harness broke" is exit 2 and
# never exit 1.
die() { echo "$@" >&2; exit 2; }

PINNED=1
case "${1:-}" in
  --unpinned) PINNED=0 ;;
  '')         : ;;
  -h|--help)  sed -n '2,45p' "${BASH_SOURCE[0]}"; exit 0 ;;
  *)          die "usage: $0 [--unpinned]   (-h for what it measures)" ;;
esac

# Script-relative, never a hardcoded checkout path: a copy of this script running
# from another checkout must test ITS OWN scripts, not the main tree's.
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts" && pwd)"
TESTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PF=/home/pedro/.claude/scripts/portfolio-refresh.cjs
REAL_VAULT=/home/pedro/Documents/Necronomicon
REAL_LEDGERS=/home/pedro/.claude/goal-ledger

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The projects root is read OUT OF portfolio-refresh.cjs's own DEFAULT_ROOT
# rather than written here. Two copies of that path is how a test comes to scan
# a different tree than the generator it is measuring.
ROOT="$(python3 -c "
import re, sys
s = open(sys.argv[1], encoding='utf-8').read()
m = re.search(r\"^const DEFAULT_ROOT = '([^']+)';\", s, re.M)
if not m:
    sys.exit('cannot read DEFAULT_ROOT out of portfolio-refresh.cjs')
print(m.group(1))
" "$PF")"

cp -a "$REAL_VAULT" "$WORK/vault"

export GOALS_LEDGER_DIR="$WORK/goal-ledger"
export GOALS_LOCK_DIR="$WORK/cache"
export GOALS_RUN_DIR="$WORK/cache/goal-run"
mkdir -p "$GOALS_LEDGER_DIR" "$GOALS_LOCK_DIR" "$GOALS_RUN_DIR"
cp -a "$REAL_LEDGERS/." "$GOALS_LEDGER_DIR"/

export PORTFOLIO_VAULT="$WORK/vault"
export PORTFOLIO_LOG="$WORK/portfolio.log"
# PORTFOLIO_PROJECTS_ROOT is deliberately LEFT UNSET so the run inherits
# DEFAULT_ROOT. The scan is read-only.
if [ "$PINNED" = 1 ]; then
  # Pinned to THIS RUN's start, not to a literal epoch. The pin's job is to hold
  # the clock still ACROSS THE TWO PASSES, and a hardcoded constant does that
  # only until it rots: a backdated `now` makes the `refreshed` mark pass 1
  # writes trail git last-activity by however stale the constant is, so pass 2
  # sees REGION-LAG (:>36h) where pass 1 saw none, and 9 targets differ for a
  # reason that is the constant rather than the generator. Measured: the
  # literal 1756339200000 is 2025-08-27, and produced exactly that.
  PORTFOLIO_NOW_MS="$(date +%s)000"
  export PORTFOLIO_NOW_MS
else
  unset PORTFOLIO_NOW_MS
fi

# --- input fingerprint -------------------------------------------------------
# A commit, a STATE.md edit, or a session dying between the two passes produces
# a TRUE diff that is not an idempotence failure. Fingerprinting before pass 1
# and after pass 2 is what lets that flake exit 6 instead of masquerading as a
# regression.
fingerprint() {
  for d in "$ROOT"/*/ "$ROOT"/*/*/; do
    [ -d "$d" ] || continue
    # Never descend into a dot-dir. The nine live `.lane-*` worktrees are the
    # reason this is a hard filter and not an incidental property of the glob.
    case "$d" in *"/."*) continue ;; esac
    if [ -e "$d.git" ]; then
      echo "git $d $(git -C "$d" rev-parse HEAD 2>/dev/null || echo NOGIT)"
    fi
    if [ -f "$d.planning/STATE.md" ]; then
      echo "state $d $(stat -c %Y "$d.planning/STATE.md")"
    fi
  done | sort
  # goals.py reads each project's REAL progress/ notes to build the handoff card
  # it embeds in Goals.md (VAULT_PROJECTS is import-bound and no env var
  # repoints it -- see the header). A peer session landing a progress report
  # between the passes is therefore a moved INPUT, not a non-idempotence, and
  # without this line it would surface as A3 RED.
  cut -f2 "$WORK/pairs.txt" | while IFS= read -r dir; do
    p="$REAL_VAULT/01-Projects/$dir/progress"
    [ -d "$p" ] || continue
    find "$p" -type f -printf 'progress %p %T@\n' 2>/dev/null || true
  done | sort
  # The live/dead split goals.py renders into every Goals.md, resolved the same
  # way goals.py resolves it. A session dying mid-run flips `**LIVE**` to `dead`
  # and rewrites eight files for a reason that is not non-idempotence.
  python3 - <<'PY' | sort
import json, os, sys, time
sys.path.insert(0, "/home/pedro/.claude/skills/progress-report/scripts")
import goals
led = os.environ["GOALS_LEDGER_DIR"]
now = time.time()
seen = set()
for name in sorted(os.listdir(led)):
    if not name.endswith(".json"):
        continue
    try:
        data = json.load(open(os.path.join(led, name), encoding="utf-8"))
    except Exception:
        continue
    for goal in data.get("goals", []):
        owner = goal.get("owner_session")
        if isinstance(owner, str):
            seen.add(owner)
for owner in sorted(seen):
    print(f"session {owner} live={goals.session_live(owner, now)}")
PY
}

# --- target list, computed ONCE ----------------------------------------------
# The slug/dir pairs come from the VAULT, never from "$GOALS_LEDGER_DIR"/*.json.
# The ledger-derived list yields 9 slugs against 8 vault directories and
# goals.py mkdirs the parent before writing, so that recipe CREATES vault
# directories. Step "S1 control" below reproduces exactly that, on the copy.
python3 "$TESTS/enumerate_goals_targets.py" > "$WORK/pairs.txt"
PAIRS=$(wc -l < "$WORK/pairs.txt")
VAULT_GOALS=$(ls -1 "$WORK/vault/01-Projects"/*/Goals.md | wc -l)
if [ "$PAIRS" != "$VAULT_GOALS" ]; then
  die "FAIL: enumeration emitted $PAIRS pairs but the vault holds $VAULT_GOALS
      01-Projects/*/Goals.md. A shortfall means a vault Goals.md has no
      matching ledger -- report it, never widen the enumeration."
fi

{
  echo "00-Dashboard/Portfolio.md"
  echo "00-Dashboard/Goals & To-Do.md"
  cut -f2 "$WORK/pairs.txt" | while IFS= read -r dir; do
    echo "01-Projects/$dir/Goals.md"
  done
  # Bare whole-line match, the same shape Phase 4's manifest uses: a substring
  # match would also hit prose that quotes the marker. Restricted to *.md by
  # EXTENSION rather than by excluding one known transcript filename: the marker
  # appears in transcript text too, and a per-filename exclusion only ever
  # covers the one contaminated file somebody already found.
  ( cd "$WORK/vault" && command grep -rIl --include='*.md' \
      '^%%PROJECT-STATE:BEGIN%%$' 01-Projects | sort )
} | sort -u > "$WORK/targets.txt"

# Derived from the DEDUPED file. A Goals.md that itself quoted the marker would
# otherwise be counted once by the pairs loop and again by the marker grep, and
# `- PAIRS` would silently overcount the index notes.
STATE_NOTES=$(command grep -c '^01-Projects/.*\.md$' "$WORK/targets.txt" || true)
STATE_NOTES=$(( STATE_NOTES - PAIRS ))
TOTAL=$(wc -l < "$WORK/targets.txt")
echo "targets: $TOTAL  (Portfolio.md 1 + Goals & To-Do.md 1 + Goals.md $PAIRS + index notes $STATE_NOTES)"

snapshot() {  # $1 = destination dir
  local dest="$1"
  mkdir -p "$dest"
  while IFS= read -r rel; do
    mkdir -p "$dest/$(dirname "$rel")"
    cp "$WORK/vault/$rel" "$dest/$rel"
  done < "$WORK/targets.txt"
}

# The index notes portfolio-refresh reported as debounce-skipped, one name per
# line. writeStateRegion's return value is DISCARDED by main() and appears in
# neither stdout nor --json, so the STATE-FILE-HOT warning is the only place the
# skip is observable -- and it is exactly the boundary (STATE_HOT_MS) whose
# flipping between passes must read as INDETERMINATE, not RED.
skipset() {  # $1 = --json capture
  # QUOTED heredoc. The regex ends in `$`, and inside a double-quoted `python3
  # -c "..."` that has to be written `\$` and survives only because bash strips
  # the backslash -- move such a snippet into a quoted heredoc later and the
  # backslash reaches python, the anchor becomes a literal `$`, the match
  # silently never fires, and an always-empty skip set reads as "no notes were
  # skipped": the two skip sets compare equal, INDETERMINATE never fires, and
  # debounced notes are counted as regenerated. Quoting it now removes the trap.
  python3 - "$1" <<'PY' | sort
import json, re, sys
d = json.load(open(sys.argv[1], encoding='utf-8'))
for w in d['summary']['warnings']:
    m = re.match(r'STATE-FILE-HOT .*state region for (.+) skipped this pass$', w)
    if m:
        print(m.group(1))
PY
}

run_pass() {  # $1 = --json capture path
  set +e
  node "$PF" --json > "$1" 2> "$1.err"
  local rc=$?
  set -e
  # 1 means flags are present, not that the run failed (main() sets the code
  # from flagCount). Anything else is a real failure and must not be measured.
  if [ "$rc" != 0 ] && [ "$rc" != 1 ]; then
    cat "$1.err" >&2
    die "FAIL: portfolio-refresh exited $rc (harness abort, not an A3 result)"
  fi
  python3 "$SCRIPTS/goals.py" dashboard \
    --out "$WORK/vault/00-Dashboard/Goals & To-Do.md" > /dev/null
  while IFS=$'\t' read -r slug dir; do
    # Both fields and the output path asserted non-empty BEFORE anything is
    # written. `[ -n "$OUT" ]` is the root fix, not a nicety: dirname "" returns
    # ".", which always exists, so the bare -d test passes on an empty $OUT.
    [ -n "$slug" ] && [ -n "$dir" ] || die "empty enumeration field -- abort"
    OUT="$WORK/vault/01-Projects/$dir/Goals.md"
    [ -n "$OUT" ] && [ -d "$(dirname "$OUT")" ] || die "refusing to create $OUT"
    python3 "$SCRIPTS/goals.py" render --project "$slug" --out "$OUT" > /dev/null
  done < "$WORK/pairs.txt"
}

# --- S1 negative control -----------------------------------------------------
# The OLD ledger-derived recipe, watched creating phantom directories, then
# cleaned up with the cleanup itself verified. COPY ONLY, never the real vault.
echo "== S1 control: the ledger-derived enumeration creates vault directories"
ls -1d "$WORK/vault/01-Projects"/*/ | sort > "$WORK/proj.before"
while IFS= read -r slug; do
  python3 "$SCRIPTS/goals.py" render --project "$slug" \
    --out "$WORK/vault/01-Projects/$slug/Goals.md" > /dev/null 2>&1 || true
done < <(cd "$GOALS_LEDGER_DIR" && ls -1 ./*.json | sed 's#^\./##; s#\.json$##' | command grep -v '^outcomes$')
ls -1d "$WORK/vault/01-Projects"/*/ | sort > "$WORK/proj.after"
comm -13 "$WORK/proj.before" "$WORK/proj.after" > "$WORK/proj.created"
[ -s "$WORK/proj.created" ] || die "S1 control did not reproduce -- do not trust the fix"
command grep -q '/ovn-fixture/$' "$WORK/proj.created" \
  || die "ovn-fixture not among the created dirs. If it was removed from the real
      ledger this is a STALE FIXTURE, not a broken generator fix -- seed it or
      relax this assertion; do not read it as 'the fix is untrustworthy'."
echo "  phantom directories created: $(wc -l < "$WORK/proj.created")"
sed 's#^#    #' "$WORK/proj.created"
while IFS= read -r d; do
  # Containment before rm -rf, unconditionally. These names trace back to slugs
  # taken from filenames in a directory five concurrent sessions write, and an
  # embedded newline would split one record into two, the tail of which is no
  # longer under $WORK.
  case "$d" in
    "$WORK"/*) rm -rf -- "$d" ;;
    *) die "refusing to rm outside the temp tree: $d" ;;
  esac
done < "$WORK/proj.created"
# LOAD-BEARING. If cleanup silently no-ops, both passes render the phantoms
# consistently, per-file cmp passes, and A3 goes GREEN on a contaminated copy.
cmp <(ls -1d "$WORK/vault/01-Projects"/*/ | sort) "$WORK/proj.before" \
  || die "control contaminated the copy"
echo "  ok - cleanup removed exactly those, the copy is back to its pre-control state."

# --- the measurement ---------------------------------------------------------
fingerprint > "$WORK/fp.before"
echo "== pass 1"
run_pass "$WORK/pass1.json"
snapshot "$WORK/pass1"
skipset "$WORK/pass1.json" > "$WORK/skip1"
echo "== pass 2"
run_pass "$WORK/pass2.json"
snapshot "$WORK/pass2"
skipset "$WORK/pass2.json" > "$WORK/skip2"
fingerprint > "$WORK/fp.after"

for p in 1 2; do
  M=$(wc -l < "$WORK/skip$p")
  echo "pass $p: regenerated-and-compared $(( STATE_NOTES - M )) / skipped $M"
done
N=$(( STATE_NOTES - $(wc -l < "$WORK/skip1") ))

if ! cmp -s "$WORK/fp.before" "$WORK/fp.after"; then
  echo "A3 INDETERMINATE -- inputs moved between passes, re-run"
  diff -u "$WORK/fp.before" "$WORK/fp.after" | head -20 || true
  exit 6
fi
if ! cmp -s "$WORK/skip1" "$WORK/skip2"; then
  echo "A3 INDETERMINATE -- inputs moved between passes, re-run"
  echo "  the two passes skipped different index notes (STATE-FILE-HOT boundary):"
  diff -u "$WORK/skip1" "$WORK/skip2" | head -20 || true
  exit 6
fi

DIFFERED=0
while IFS= read -r rel; do
  if ! cmp -s "$WORK/pass1/$rel" "$WORK/pass2/$rel"; then
    echo "DIFFERS: $rel"
    diff -u "$WORK/pass1/$rel" "$WORK/pass2/$rel" | head -20 || true
    DIFFERED=$(( DIFFERED + 1 ))
  fi
done < "$WORK/targets.txt"

if [ "$DIFFERED" -gt 0 ]; then
  echo "A3 RED -- $DIFFERED of $TOTAL targets differed across two passes"
  exit 1
fi
# Without this floor the index-note half of A3 passes vacuously: a note whose
# .planning/STATE.md was just touched is skipped rather than compared, and a
# green run then proves nothing about it.
if [ "$N" -le 0 ]; then
  echo "A3 INDETERMINATE -- 0 index notes were regenerated and compared; the"
  echo "  index-note half of this run is vacuous, re-run"
  exit 6
fi
echo "A3 GREEN -- all $TOTAL targets byte-identical across two passes"
exit 0
