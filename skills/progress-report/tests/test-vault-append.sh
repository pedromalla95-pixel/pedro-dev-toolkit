#!/usr/bin/env bash
# EDGE tests for vault-append.cjs — exercises the REAL helper, not a reimplementation.
set -u

HELPER=/home/pedro/.claude/skills/progress-report/scripts/vault-append.cjs
TMP=$(mktemp -d)
PASS=0; FAIL=0
trap 'rm -rf "$TMP"' EXIT

ok()   { PASS=$((PASS+1)); echo "  PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }

# Each test gets a unique page basename => unique lock => tests can't interfere.
newpage() {
  local p="$TMP/$1.md"
  cat > "$p" <<'EOF'
---
type: project-index
status: active
updated: 2026-01-01
---

# Test Project

Some body text.

## Session Log
- 2026-07-01 10:00 — [seed entry](progress/2026-07-01.md)
EOF
  echo "$p"
}

echo "== [EDGE] Vault-append concurrency — real helper, 20 writers, 1 page =="
PAGE=$(newpage concurrency)
for i in $(seq -w 1 20); do
  node "$HELPER" "$PAGE" "- 2026-07-14 12:$i — [entry $i](progress/2026-07-14.md)" &
done
wait
GOT=$(grep -c '^- 2026-07-14 12:' "$PAGE")
check "all 20 concurrent appends survive" "$GOT" "20"
python3 -c "
import sys,yaml
fm=open('$PAGE').read().split('---')[1]
d=yaml.safe_load(fm)
assert d['type']=='project-index', d
assert d['status']=='active', d
print('  PASS: frontmatter still parses as valid YAML with keys intact')
" || bad "frontmatter parses after concurrent writes"
# Section integrity: no stray duplicate headers
check "exactly one '## Session Log' header" "$(grep -c '^## Session Log' "$PAGE")" "1"

echo
echo "== [EDGE] Lock timeout fails loud (fresh lock held) =="
PAGE=$(newpage locktimeout)
LOCK=/home/pedro/.claude/cache/vault-append-locktimeout.lock
rm -rf "$LOCK"; mkdir -p "$LOCK"; echo "someone-else" > "$LOCK/owner"
BEFORE=$(md5sum "$PAGE" | cut -d' ' -f1)
START=$(date +%s)
ERR=$(node "$HELPER" "$PAGE" "- should never land" 2>&1); RC=$?
ELAPSED=$(( $(date +%s) - START ))
check "exit code 3 on lock timeout" "$RC" "3"
case "$ERR" in *"[vault-append] lock timeout"*) ok "stderr names the lock timeout" ;; *) bad "stderr lock timeout (got: $ERR)" ;; esac
check "page untouched on timeout" "$(md5sum "$PAGE" | cut -d' ' -f1)" "$BEFORE"
if [ "$ELAPSED" -le 8 ]; then ok "gave up within cap (~${ELAPSED}s)"; else bad "took ${ELAPSED}s, expected <=8"; fi
check "foreign lock NOT deleted (no ownership)" "$([ -d "$LOCK" ] && echo yes || echo no)" "yes"
rm -rf "$LOCK"

echo
echo "== [EDGE] Liveness is decided by PID, not by a clock =="
# (a) Holder process is GONE => evict immediately, regardless of lock age.
PAGE=$(newpage deadpid)
LOCK=/home/pedro/.claude/cache/vault-append-deadpid.lock
DEAD_PID=$(bash -c 'echo $$')   # exits instantly; its pid is now free
while kill -0 "$DEAD_PID" 2>/dev/null; do sleep 0.05; done
rm -rf "$LOCK"; mkdir -p "$LOCK"; echo "$DEAD_PID-abc123" > "$LOCK/owner"
touch -d '5 seconds ago' "$LOCK"   # FRESH: the old clock rule would refuse to evict
node "$HELPER" "$PAGE" "- 2026-07-14 13:00 — [after takeover](progress/2026-07-14.md)"; RC=$?
check "(a) dead holder evicted even with a FRESH lock" "$RC" "0"
check "(a) append landed" "$(grep -c 'after takeover' "$PAGE")" "1"
rm -rf "$LOCK"

# (b) Holder is ALIVE but slow/suspended => never robbed, waiter fails loud.
# This is the case the previous clock-based rule got wrong: after a laptop resume
# every in-flight holder looks stale at once and its page gets double-written.
PAGE=$(newpage livepid)
LOCK=/home/pedro/.claude/cache/vault-append-livepid.lock
sleep 300 & LIVE_PID=$!
rm -rf "$LOCK"; mkdir -p "$LOCK"; echo "$LIVE_PID-abc123" > "$LOCK/owner"
touch -d '30 minutes ago' "$LOCK"   # ANCIENT: the old rule would have robbed it
BEFORE=$(md5sum "$PAGE" | cut -d' ' -f1)
node "$HELPER" "$PAGE" "- must not land" >/dev/null 2>&1; RC=$?
check "(b) live holder NOT robbed despite a 30-min-old lock" "$RC" "3"
check "(b) page untouched" "$(md5sum "$PAGE" | cut -d' ' -f1)" "$BEFORE"
check "(b) live holder's lock still intact" "$([ -d "$LOCK" ] && echo y || echo n)" "y"
kill "$LIVE_PID" 2>/dev/null; wait "$LIVE_PID" 2>/dev/null; rm -rf "$LOCK"

# (c) Unparsable owner + past the PID-reuse backstop => reclaimed.
PAGE=$(newpage backstop)
LOCK=/home/pedro/.claude/cache/vault-append-backstop.lock
rm -rf "$LOCK"; mkdir -p "$LOCK"; echo "not-a-pid" > "$LOCK/owner"
touch -d '2 hours ago' "$LOCK"
node "$HELPER" "$PAGE" "- 2026-07-14 13:05 — [backstop](progress/2026-07-14.md)" >/dev/null 2>&1
check "(c) backstop reclaims a lock with an unparsable owner" "$(grep -c 'backstop' "$PAGE")" "1"
rm -rf "$LOCK"

echo
echo "== [GREEN] Idempotency is line-anchored, not substring =="
PAGE=$(newpage idempotency)
LINE="- 2026-07-14 14:00 — [report](progress/2026-07-14.md)"
node "$HELPER" "$PAGE" "$LINE" >/dev/null
OUT=$(node "$HELPER" "$PAGE" "$LINE"); RC=$?
check "second identical append is a no-op" "$RC" "0"
case "$OUT" in *unchanged*) ok "reports 'unchanged'" ;; *) bad "expected 'unchanged', got '$OUT'" ;; esac
check "line present exactly once" "$(grep -cF -- "$LINE" "$PAGE")" "1"
# The killer case: new line is a strict substring of an existing line.
node "$HELPER" "$PAGE" "$LINE (superseded)" >/dev/null
SUB=$(node "$HELPER" "$PAGE" "$LINE"); RC=$?
check "substring-of-existing-line still appends (not swallowed)" "$(grep -cF -- "$LINE" "$PAGE")" "2"

echo
echo "== [GREEN] Section creation when absent =="
PAGE="$TMP/nosection.md"
printf -- '---\ntype: project-index\n---\n\n# Page\n\nBody.\n' > "$PAGE"
node "$HELPER" "$PAGE" "- 2026-07-14 15:00 — [first](progress/x.md)" >/dev/null
check "creates ## Session Log when missing" "$(grep -c '^## Session Log' "$PAGE")" "1"
check "entry landed under it" "$(grep -c 'first' "$PAGE")" "1"

echo
echo "== [GREEN] Insert lands in Session Log, not a later section =="
PAGE="$TMP/latersection.md"
cat > "$PAGE" <<'EOF'
---
type: project-index
---

## Session Log
- 2026-07-01 10:00 — [seed](progress/a.md)

## Research
- [[Research/_index|Research Index]]
EOF
node "$HELPER" "$PAGE" "- 2026-07-14 16:00 — [new](progress/b.md)" >/dev/null
LINENO_NEW=$(grep -n 'progress/b.md' "$PAGE" | cut -d: -f1)
LINENO_RES=$(grep -n '^## Research' "$PAGE" | cut -d: -f1)
if [ "$LINENO_NEW" -lt "$LINENO_RES" ]; then ok "entry inserted above ## Research"; else bad "entry leaked past ## Research"; fi

echo
echo "== [EDGE] A failed grant must not orphan the lock =="
# mkdir succeeds, the owner write then fails. A lock with no owner file is one that
# holderIsDead() refuses to evict except via the 1h backstop, so an orphan here would
# wedge this page for every session for a full hour.
PAGE=$(newpage orphan)
LOCK=/home/pedro/.claude/cache/vault-append-orphan.lock
rm -rf "$LOCK"
ERR=$(node "$(dirname "$0")/inject-owner-write-failure.cjs" "$HELPER" "$PAGE" "- x" 2>&1); RC=$?
if [ "$RC" -ne 0 ]; then ok "failed grant exits non-zero"; else bad "failed grant should fail loud (got exit 0)"; fi
case "$ERR" in *ENOSPC*) ok "stderr carries the underlying errno" ;; *) bad "stderr should name the errno (got: ${ERR:0:80})" ;; esac
check "no orphan lock left behind" "$([ -d "$LOCK" ] && echo y || echo n)" "n"
node "$HELPER" "$PAGE" "- 2026-07-14 18:00 — [healthy](progress/x.md)" >/dev/null 2>&1
check "next append is unblocked" "$(grep -c 'healthy' "$PAGE")" "1"
rm -rf "$LOCK"

echo
echo "== [EDGE] No leaked locks or tmp files =="
check "no vault-append tmp files left" "$(find "$TMP" -name '*.tmp.*' | wc -l)" "0"
check "no test lock dirs left" "$(find /home/pedro/.claude/cache -maxdepth 1 -name 'vault-append-*.lock' 2>/dev/null | wc -l)" "0"
check "no graveyard dirs left" "$(find /home/pedro/.claude/cache -maxdepth 1 -name 'vault-append-*.dead.*' 2>/dev/null | wc -l)" "0"

echo
echo "================================"
echo "PASS: $PASS  FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
