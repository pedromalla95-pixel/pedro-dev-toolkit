#!/usr/bin/env bash
# Full goal-ledger lifecycle against a scratch ledger. Never touches a real ledger
# or the live vault.
set -u
GOALS=/home/pedro/.claude/skills/progress-report/scripts/goals.py
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
# mktemp can fail (disk full, no /tmp perms) and would leave TMP empty, which
# would point GOALS_LEDGER_DIR/GOALS_LOCK_DIR at filesystem-root /ledger and
# /lock instead of a scratch dir — a path the vault guard below never checks.
[ -n "$TMP" ] && [ -d "$TMP" ] || { echo "FATAL: mktemp -d failed"; exit 1; }
# GOALS_RUN_DIR belongs in this list for exactly the same reason as the other
# two: every `load` and `commit` below names a session, and a named session now
# leaves a run receipt on its way out. Without this the suite writes real files
# into Pedro's live ~/.claude/cache/goal-run — verified, it did, before this
# line was added.
export GOALS_LEDGER_DIR="$TMP/ledger" GOALS_LOCK_DIR="$TMP/lock" GOALS_RUN_DIR="$TMP/goal-run"
mkdir -p "$GOALS_LEDGER_DIR" "$GOALS_LOCK_DIR"
FAIL=0

case "$TMP" in
  /home/pedro/Documents/Necronomicon*) echo "FATAL: scratch landed in the live vault"; exit 1 ;;
esac

say() { if [ "$1" -eq 0 ]; then echo "PASS: $2"; else echo "FAIL: $2"; FAIL=1; fi }

# 1. Empty load on a project with no ledger.
python3 "$GOALS" load --project "E2E Demo" --session sess-a > "$TMP/load1.json"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d['owned']==[] and d['others']==0 else 1)" "$TMP/load1.json"
say $? "empty load returns no goals"

# 2. Commit two goals.
cat > "$TMP/dec1.json" <<'JSON'
{"session": "sess-a", "decisions": [
  {"op": "create", "text": "Wire the goal ledger", "level": "goal", "parent": null, "origin": "extracted"}
]}
JSON
python3 "$GOALS" commit --project "E2E Demo" --session sess-a --decisions "$TMP/dec1.json" > /dev/null
say $? "commit applies"

# 3. `create` is NOT keyed by text: replaying the same create makes a second goal.
#    The skill never replays a create — it emits `seen` for unchanged goals — but
#    pin the behaviour so nobody later assumes create deduplicates.
python3 "$GOALS" commit --project "E2E Demo" --session sess-a --decisions "$TMP/dec1.json" > /dev/null
rc=$?; [ "$rc" -eq 0 ] || { echo "FATAL: replay commit failed rc=$rc"; exit 1; }
python3 "$GOALS" load --project "E2E Demo" --session sess-a > "$TMP/load2.json"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if len(d['owned'])==2 else 1)" "$TMP/load2.json"
say $? "replayed create appends rather than deduplicating (expected 2 goals)"

# 3b. The re-run path the skill actually uses IS idempotent: `seen` twice is a no-op.
GID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['owned'][0]['id'])" "$TMP/load2.json")
cat > "$TMP/dec2.json" <<JSON
{"session": "sess-a", "decisions": [{"op": "seen", "id": "$GID"}]}
JSON
python3 "$GOALS" commit --project "E2E Demo" --session sess-a --decisions "$TMP/dec2.json" > /dev/null
rc=$?; [ "$rc" -eq 0 ] || { echo "FATAL: first seen commit failed rc=$rc"; exit 1; }
cp "$GOALS_LEDGER_DIR/e2e-demo.json" "$TMP/after-seen.json"
python3 "$GOALS" commit --project "E2E Demo" --session sess-a --decisions "$TMP/dec2.json" > /dev/null
rc=$?; [ "$rc" -eq 0 ] || { echo "FATAL: second seen commit failed rc=$rc"; exit 1; }
python3 -c "
import json,sys
a=json.load(open(sys.argv[1])); b=json.load(open(sys.argv[2]))
# 'updated' (ledger-level) and 'last_seen' (per-goal) are timestamps from
# _now_iso(), which truncates to whole seconds. The two seen commits above
# run back-to-back but can straddle a real second boundary — when they do,
# goals.py legitimately advances last_seen on the second call (that is
# correct behaviour, not a bug: a later observation should update it), so
# comparing those two volatile fields verbatim makes this check flaky by
# construction rather than a real assertion. What 'idempotent' actually
# means here — no duplicate goal, no new history entry, no other field
# touched — does not depend on either timestamp, so both are normalized out.
a.pop('updated',None); b.pop('updated',None)
for d in (a, b):
    for g in d.get('goals', []):
        g.pop('last_seen', None)
sys.exit(0 if a==b else 1)" "$TMP/after-seen.json" "$GOALS_LEDGER_DIR/e2e-demo.json"
say $? "repeated seen creates no duplicate goal or history entry"

# 4. Render the mirror.
python3 "$GOALS" render --project "E2E Demo" --out "$TMP/Goals.md" > /dev/null
grep -q "Wire the goal ledger" "$TMP/Goals.md"
say $? "mirror contains the goal"
grep -q "do not edit by hand" "$TMP/Goals.md"
say $? "mirror is marked as derived"

# 5. Corrupt the ledger; load must refuse without repairing.
LEDGER="$GOALS_LEDGER_DIR/e2e-demo.json"
printf '{"schema": 1, "goals": [' > "$LEDGER"
cp "$LEDGER" "$TMP/corrupted.json"
python3 "$GOALS" load --project "E2E Demo" --session sess-a > /dev/null 2>&1
[ $? -eq 2 ]
say $? "corrupt ledger exits 2"
cmp -s "$LEDGER" "$TMP/corrupted.json"
say $? "corrupt ledger left byte-identical (not repaired)"

exit $FAIL
