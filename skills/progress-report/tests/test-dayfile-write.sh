#!/usr/bin/env bash
# Tests for dayfile-write.cjs — exercises the REAL helper against SCRATCH paths only.
# HARD RULE: never touches the live vault (/home/pedro/Documents/Necronomicon).
# Fault injection (tests 7b/15) uses `node -r` preloads GENERATED into scratch at
# runtime — the helper itself carries no test hooks.
set -u

HELPER=/home/pedro/.claude/skills/progress-report/scripts/dayfile-write.cjs
# Every fixture's frontmatter carries this uuid; the writer's collision guard
# compares it against --session-uuid on every run with an existing target.
SUUID="11111111-2222-3333-4444-555555555555"
TMP=$(mktemp -d)
PASS=0; FAIL=0

# Preserve the evidence on failure — a nuked scratch dir makes a red run undiagnosable.
cleanup() {
  chmod -R u+w "$TMP" 2>/dev/null
  if [ "${FAIL:-1}" -eq 0 ]; then
    rm -rf "$TMP"
  else
    echo "NOTE: failures above — scratch preserved for forensics at $TMP" >&2
  fi
}
trap cleanup EXIT

case "$TMP" in
  /home/pedro/Documents/Necronomicon*) echo "FATAL: scratch dir landed inside the live vault"; exit 1 ;;
esac

ok()   { PASS=$((PASS+1)); echo "  PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }

# Unique basenames per test => unique lock namespaces => tests can't interfere.
FM="$TMP/frontmatter.md"
cat > "$FM" <<'EOF'
---
type: progress-report
session_uuid: 11111111-2222-3333-4444-555555555555
project: scratch-test
date: 2026-07-18
---

# Progress — scratch-test — 2026-07-18
EOF

mkblock() { # $1=path $2=HH:MM $3=title
  printf '## %s — %s\n\n- did a thing at %s\n' "$2" "$3" "$2" > "$1"
}

seedday() { # $1=path — day-file with one 09:00 block
  cat > "$1" <<'EOF'
---
type: progress-report
session_uuid: 11111111-2222-3333-4444-555555555555
project: scratch-test
date: 2026-07-18
---

# Progress — scratch-test — 2026-07-18

## 09:00 — Morning session

- seeded block body
EOF
}

# Fault injectors (scratch-generated; used with `node -r`).
# Delay: stalls the helper AFTER its first read of the victim path — inside the lock,
# between pre-image read and write — so two writers provably contend (test 7b).
cat > "$TMP/inject-read-delay.cjs" <<'EOF'
'use strict';
const fs = require('fs');
const real = fs.readFileSync.bind(fs);
const victim = process.env.DW_DELAY_PATH || '';
const ms = parseInt(process.env.DW_DELAY_MS || '0', 10);
let done = false;
fs.readFileSync = (p, ...rest) => {
  const out = real(p, ...rest);
  if (!done && ms > 0 && typeof p === 'string' && p === victim) {
    done = true;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
  }
  return out;
};
EOF
# Corrupt: appends garbage to any write whose path starts with the victim prefix —
# simulates the audited real-world failure (stale/divergent mirror) to prove the
# helper's own verification catches it (test 15).
cat > "$TMP/inject-write-corrupt.cjs" <<'EOF'
'use strict';
const fs = require('fs');
const real = fs.writeFileSync.bind(fs);
const prefix = process.env.DW_CORRUPT_PREFIX || '';
fs.writeFileSync = (p, data, ...rest) => {
  if (prefix && typeof p === 'string' && p.startsWith(prefix)) {
    return real(p, data + '\nCORRUPTED-BY-TEST\n', ...rest);
  }
  return real(p, data, ...rest);
};
EOF

MARKER_10_30="> Superseded by the 10:30 block above (same session)."

echo "== [GREEN] 1. create-new (2 fresh runs — reproducibility) =="
for n in 1 2; do
  D="$TMP/create$n"; mkdir -p "$D"
  B="$D/block.md"; mkblock "$B" "10:00" "First session"
  F="$D/dw-create$n-$$.md"
  OUT=$(node "$HELPER" --vault-target "$F" --block-file "$B" --hh-mm 10:00 --frontmatter-file "$FM"); RC=$?
  check "run $n exit 0" "$RC" "0"
  case "$OUT" in '{"ok":true'*) ok "run $n JSON ok:true" ;; *) bad "run $n JSON ok:true (got: $OUT)" ;; esac
  check "run $n file created" "$([ -f "$F" ] && echo y || echo n)" "y"
  check "run $n new H2 present once" "$(grep -c '^## 10:00 — ' "$F")" "1"
  check "run $n frontmatter fences intact" "$(grep -c '^---$' "$F")" "2"
  check "run $n H1 present" "$(grep -c '^# Progress' "$F")" "1"
done

echo
echo "== [GREEN] 2. same-day re-run: new block on top, one superseded marker, old bytes kept =="
D="$TMP/rerun"; mkdir -p "$D"; F="$D/dw-rerun-$$.md"; seedday "$F"; cp "$F" "$F.pre"
B="$D/block.md"; mkblock "$B" "10:30" "Second session"
OUT=$(node "$HELPER" --vault-target "$F" --session-uuid "$SUUID" --block-file "$B" --hh-mm 10:30); RC=$?
check "exit 0" "$RC" "0"
case "$OUT" in *'"superseded_marked":true'*) ok "JSON superseded_marked:true" ;; *) bad "JSON superseded_marked:true (got: $OUT)" ;; esac
L_NEW=$(grep -n '^## 10:30 — ' "$F" | cut -d: -f1)
L_OLD=$(grep -n '^## 09:00 — ' "$F" | cut -d: -f1)
if [ -n "$L_NEW" ] && [ -n "$L_OLD" ] && [ "$L_NEW" -lt "$L_OLD" ]; then ok "new block above old block"; else bad "new block above old block (new=$L_NEW old=$L_OLD)"; fi
check "exactly one superseded marker" "$(grep -cF "$MARKER_10_30" "$F")" "1"
check "marker directly under old header" "$(sed -n "$((L_OLD+1))p" "$F")" "$MARKER_10_30"
POST_OLD=$(sed -n '/^## 09:00/,$p' "$F" | grep -vF "$MARKER_10_30")
PRE_OLD=$(sed -n '/^## 09:00/,$p' "$F.pre")
check "old block bytes preserved (marker aside)" "$POST_OLD" "$PRE_OLD"

echo
echo "== [GREEN] 3. mirror: --local-target byte-identical, parent dir created =="
D="$TMP/mirror"; mkdir -p "$D"; F="$D/dw-mirror-$$.md"; seedday "$F"
M="$D/local/deeper/mirror-$$.md"
B="$D/block.md"; mkblock "$B" "11:00" "Mirrored session"
node "$HELPER" --vault-target "$F" --session-uuid "$SUUID" --block-file "$B" --hh-mm 11:00 --local-target "$M" >/dev/null; RC=$?
check "exit 0" "$RC" "0"
if cmp -s "$F" "$M"; then ok "mirror byte-identical (cmp)"; else bad "mirror byte-identical ($F vs $M)"; cmp "$F" "$M" >&2 || true; fi

echo
echo "== [RED] 4. malformed block-file (no '## HH:MM — ' first line) => exit 2 =="
D="$TMP/badblock"; mkdir -p "$D"; F="$D/dw-badblock-$$.md"; seedday "$F"
B="$D/block.md"; printf 'not a header\n\n- body\n' > "$B"
ERR=$(node "$HELPER" --vault-target "$F" --block-file "$B" --hh-mm 12:00 2>&1 >/dev/null); RC=$?
check "exit 2" "$RC" "2"
check "stderr non-empty" "$([ -n "$ERR" ] && echo y || echo n)" "y"
check "day-file untouched" "$(grep -c '^## ' "$F")" "1"

echo
echo "== [RED] 5. create-new without --frontmatter-file => exit 2, no file =="
D="$TMP/nofm"; mkdir -p "$D"; F="$D/dw-nofm-$$.md"
B="$D/block.md"; mkblock "$B" "12:00" "Orphan"
ERR=$(node "$HELPER" --vault-target "$F" --block-file "$B" --hh-mm 12:00 2>&1 >/dev/null); RC=$?
check "exit 2" "$RC" "2"
check "stderr non-empty" "$([ -n "$ERR" ] && echo y || echo n)" "y"
check "target not created" "$([ -f "$F" ] && echo y || echo n)" "n"

echo
echo "== [RED] 6. two H2 blocks in block-file => exit 2 =="
D="$TMP/twoblocks"; mkdir -p "$D"; F="$D/dw-twoblocks-$$.md"; seedday "$F"
B="$D/block.md"; printf '## 12:00 — First\n\n- a\n\n## 12:05 — Second\n\n- b\n' > "$B"
ERR=$(node "$HELPER" --vault-target "$F" --block-file "$B" --hh-mm 12:00 2>&1 >/dev/null); RC=$?
check "exit 2" "$RC" "2"
check "stderr non-empty" "$([ -n "$ERR" ] && echo y || echo n)" "y"
check "day-file untouched" "$(grep -c '^## ' "$F")" "1"

echo
echo "== [EDGE] 7. concurrency: two writers, one day-file, 5 iterations =="
for iter in 1 2 3 4 5; do
  D="$TMP/conc$iter"; mkdir -p "$D"; F="$D/dw-conc$iter-$$.md"; seedday "$F"
  B1="$D/b1.md"; mkblock "$B1" "10:00" "writer A"
  B2="$D/b2.md"; mkblock "$B2" "10:01" "writer B"
  node "$HELPER" --vault-target "$F" --session-uuid "$SUUID" --block-file "$B1" --hh-mm 10:00 >"$D/o1" 2>"$D/e1" & P1=$!
  node "$HELPER" --vault-target "$F" --session-uuid "$SUUID" --block-file "$B2" --hh-mm 10:01 >"$D/o2" 2>"$D/e2" & P2=$!
  wait "$P1"; R1=$?; wait "$P2"; R2=$?
  check "iter $iter both exit 0 (lock serialized, no timeout)" "$R1/$R2" "0/0"
  check "iter $iter both new blocks present" "$(grep -c '^## 10:0[01] — ' "$F")" "2"
  check "iter $iter total markers = 2 (top block unmarked)" "$(grep -c '^> Superseded by the ' "$F")" "2"
  TOP=$(grep -m1 -o '^## [0-9][0-9]:[0-9][0-9]' "$F" | awk '{print $2}')
  if [ "$TOP" = "10:00" ]; then MID="10:01"; else MID="10:00"; fi
  L_MID=$(grep -n "^## $MID — " "$F" | cut -d: -f1)
  check "iter $iter chain: $MID marked by $TOP exactly once" "$(sed -n "$((L_MID+1))p" "$F")" "> Superseded by the $TOP block above (same session)."
  L9=$(grep -n '^## 09:00 — ' "$F" | cut -d: -f1)
  check "iter $iter chain: 09:00 marked by $MID exactly once" "$(sed -n "$((L9+1))p" "$F")" "> Superseded by the $MID block above (same session)."
  H1LINE=$(grep -n '^# ' "$F" | head -1 | cut -d: -f1)
  check "iter $iter frontmatter untorn (one --- pair before H1)" "$(head -n "$((H1LINE-1))" "$F" | grep -c '^---$')" "2"
done

echo
echo "== [EDGE] 7b. FORCED contention: delayed writer holds the lock across the other's attempt =="
# Un-staggered spawns rarely overlap at the syscall level (spawn cost >> critical
# section), so test 7 alone can miss a broken lock. Here writer A is stalled 500ms
# INSIDE its critical section (after the pre-image read); writer B launches into that
# window. Lock removed => the delayed writer commits a pre-image without the other's
# block (lost update) => the both-blocks-present assert goes red deterministically.
D="$TMP/concforced"; mkdir -p "$D"; F="$D/dw-concforced-$$.md"; seedday "$F"
B1="$D/b1.md"; mkblock "$B1" "10:00" "delayed writer"
B2="$D/b2.md"; mkblock "$B2" "10:01" "fast writer"
DW_DELAY_PATH="$F" DW_DELAY_MS=500 node -r "$TMP/inject-read-delay.cjs" \
  "$HELPER" --vault-target "$F" --session-uuid "$SUUID" --block-file "$B1" --hh-mm 10:00 >"$D/o1" 2>"$D/e1" & P1=$!
sleep 0.15
node "$HELPER" --vault-target "$F" --session-uuid "$SUUID" --block-file "$B2" --hh-mm 10:01 >"$D/o2" 2>"$D/e2" & P2=$!
wait "$P1"; R1=$?; wait "$P2"; R2=$?
check "both exit 0 (waiter survived the 500ms hold)" "$R1/$R2" "0/0"
check "both blocks present (no lost update)" "$(grep -c '^## 10:0[01] — ' "$F")" "2"
check "total markers = 2" "$(grep -c '^> Superseded by the ' "$F")" "2"
TOP=$(grep -m1 -o '^## [0-9][0-9]:[0-9][0-9]' "$F" | awk '{print $2}')
if [ "$TOP" = "10:00" ]; then MID="10:01"; else MID="10:00"; fi
L_MID=$(grep -n "^## $MID — " "$F" | cut -d: -f1)
check "chain: $MID marked by $TOP exactly once" "$(sed -n "$((L_MID+1))p" "$F")" "> Superseded by the $TOP block above (same session)."

echo
echo "== [EDGE] 8. non-conforming newest header (## Correction — no HH:MM) still gets the marker =="
D="$TMP/nonconform"; mkdir -p "$D"; F="$D/dw-nonconform-$$.md"
cat > "$F" <<'EOF'
---
type: progress-report
session_uuid: 11111111-2222-3333-4444-555555555555
date: 2026-07-18
---

# Progress — scratch-test — 2026-07-18

## Correction — earlier total was wrong

- corrected figure
EOF
B="$D/block.md"; mkblock "$B" "13:00" "After correction"
node "$HELPER" --vault-target "$F" --session-uuid "$SUUID" --block-file "$B" --hh-mm 13:00 >/dev/null; RC=$?
check "exit 0" "$RC" "0"
L_NEW=$(grep -n '^## 13:00 — ' "$F" | cut -d: -f1)
L_COR=$(grep -n '^## Correction' "$F" | cut -d: -f1)
if [ -n "$L_NEW" ] && [ -n "$L_COR" ] && [ "$L_NEW" -lt "$L_COR" ]; then ok "new block above Correction block"; else bad "new block above Correction block"; fi
check "marker directly under Correction header" "$(sed -n "$((L_COR+1))p" "$F")" "> Superseded by the 13:00 block above (same session)."
check "exactly one marker" "$(grep -c '^> Superseded by the ' "$F")" "1"

echo
echo "== [RED] 9. mirror write failure (read-only dir) fails LOUD, non-zero =="
D="$TMP/rofail"; mkdir -p "$D/ro"; F="$D/dw-rofail-$$.md"; seedday "$F"
B="$D/block.md"; mkblock "$B" "14:00" "Doomed mirror"
chmod 555 "$D/ro"
OUT=$(node "$HELPER" --vault-target "$F" --session-uuid "$SUUID" --block-file "$B" --hh-mm 14:00 --local-target "$D/ro/mirror.md" 2>"$D/err"); RC=$?
ERR=$(cat "$D/err")
chmod 755 "$D/ro"
case "$RC" in 4|6) ok "exit code is 4 or 6 (got $RC)" ;; *) bad "exit code is 4 or 6 (got $RC)" ;; esac
check "stderr non-empty (loud)" "$([ -n "$ERR" ] && echo y || echo n)" "y"
case "$ERR" in *"VAULT day-file WAS already updated"*) ok "stderr states the vault WAS updated" ;; *) bad "stderr states the vault WAS updated (got: ${ERR:0:120})" ;; esac
case "$OUT" in *'"vault_written":true'*) ok "stdout JSON flags vault_written:true" ;; *) bad "stdout JSON flags vault_written:true (got: $OUT)" ;; esac

echo
echo "== [EDGE] 10. retry after mirror failure is idempotent: heals mirror, no duplicate, no self-marker =="
OUT=$(node "$HELPER" --vault-target "$F" --session-uuid "$SUUID" --block-file "$B" --hh-mm 14:00 --local-target "$D/ro/mirror.md"); RC=$?
check "retry exit 0" "$RC" "0"
case "$OUT" in *'"already_applied":true'*) ok "JSON already_applied:true" ;; *) bad "JSON already_applied:true (got: $OUT)" ;; esac
check "no duplicate 14:00 block" "$(grep -c '^## 14:00 — ' "$F")" "1"
L14=$(grep -n '^## 14:00 — ' "$F" | cut -d: -f1)
check "14:00 block NOT self-superseded (no marker under its own header)" "$(sed -n "$((L14+1))p" "$F" | grep -c '^> Superseded by the ')" "0"
check "legit 14:00 marker on old 09:00 block still single" "$(grep -c '^> Superseded by the 14:00 block' "$F")" "1"
if cmp -s "$F" "$D/ro/mirror.md"; then ok "mirror healed byte-identical"; else bad "mirror healed byte-identical ($F vs $D/ro/mirror.md)"; cmp "$F" "$D/ro/mirror.md" >&2 || true; fi

echo
echo "== [RED] 11. same --hh-mm but DIFFERENT content at top => exit 2 (label collision) =="
B2="$D/block-diff.md"; mkblock "$B2" "14:00" "Different content, same label"
BEFORE=$(md5sum "$F" | cut -d' ' -f1)
ERR=$(node "$HELPER" --vault-target "$F" --session-uuid "$SUUID" --block-file "$B2" --hh-mm 14:00 2>&1 >/dev/null); RC=$?
check "exit 2" "$RC" "2"
case "$ERR" in *"bump --hh-mm"*) ok "stderr tells caller the recovery" ;; *) bad "stderr tells caller the recovery (got: ${ERR:0:100})" ;; esac
check "day-file untouched" "$(md5sum "$F" | cut -d' ' -f1)" "$BEFORE"

echo
echo "== [EDGE] 12. '## ' inside a code fence is content, not a block boundary =="
D="$TMP/fence"; mkdir -p "$D"; F="$D/dw-fence-$$.md"
cat > "$F" <<'EOF'
---
type: progress-report
session_uuid: 11111111-2222-3333-4444-555555555555
date: 2026-07-18
---

# Progress — scratch-test — 2026-07-18

## 09:30 — Session with quoted markdown

- pasted a doc excerpt:

```markdown
## this is fenced content, not a real block
## neither is this
```

- end of block
EOF
cp "$F" "$F.pre"
B="$D/block.md"; mkblock "$B" "15:00" "After fenced block"
node "$HELPER" --vault-target "$F" --session-uuid "$SUUID" --block-file "$B" --hh-mm 15:00 >/dev/null; RC=$?
check "exit 0" "$RC" "0"
check "fenced pseudo-headers survive intact" "$(grep -c '^## this is fenced content' "$F")" "1"
check "exactly one marker (fenced block treated as ONE block)" "$(grep -c '^> Superseded by the ' "$F")" "1"
L9=$(grep -n '^## 09:30 — ' "$F" | cut -d: -f1)
check "marker under the 09:30 header, not a fenced fragment" "$(sed -n "$((L9+1))p" "$F")" "> Superseded by the 15:00 block above (same session)."
POST_OLD=$(sed -n '/^## 09:30/,$p' "$F" | grep -v '^> Superseded by the ')
PRE_OLD=$(sed -n '/^## 09:30/,$p' "$F.pre")
check "fenced block bytes preserved (marker aside)" "$POST_OLD" "$PRE_OLD"

echo
echo "== [RED] 14. foreign LIVE lock => exit 3 within cap, day-file untouched, lock not stolen =="
D="$TMP/locktimeout"; mkdir -p "$D"; F="$D/dw-locktimeout-$$.md"; seedday "$F"
B="$D/block.md"; mkblock "$B" "16:00" "Never lands"
LOCK="/home/pedro/.claude/cache/dayfile-write-dw-locktimeout-$$.lock"
sleep 60 & LIVE_PID=$!
rm -rf "$LOCK"; mkdir -p "$LOCK"; echo "$LIVE_PID-abc123" > "$LOCK/owner"
BEFORE=$(md5sum "$F" | cut -d' ' -f1)
START=$(date +%s)
ERR=$(node "$HELPER" --vault-target "$F" --block-file "$B" --hh-mm 16:00 2>&1 >/dev/null); RC=$?
ELAPSED=$(( $(date +%s) - START ))
check "exit 3 on live foreign lock" "$RC" "3"
case "$ERR" in *"lock timeout"*) ok "stderr names the lock timeout" ;; *) bad "stderr names the lock timeout (got: ${ERR:0:100})" ;; esac
check "day-file untouched" "$(md5sum "$F" | cut -d' ' -f1)" "$BEFORE"
if [ "$ELAPSED" -le 8 ]; then ok "gave up within cap (~${ELAPSED}s)"; else bad "took ${ELAPSED}s, expected <=8"; fi
check "live holder's lock NOT deleted" "$([ -d "$LOCK" ] && echo y || echo n)" "y"
kill "$LIVE_PID" 2>/dev/null; wait "$LIVE_PID" 2>/dev/null; rm -rf "$LOCK"

echo
echo "== [RED] 15. genuine verification failure (injected mirror corruption) => exit 6, JSON ok:false =="
# Reproduces the audited real-world symptom — mirror bytes diverge from the vault —
# and proves the helper's own post-write verification catches it loudly.
D="$TMP/corrupt"; mkdir -p "$D"; F="$D/dw-corrupt-$$.md"; seedday "$F"
M="$D/mirror.md"
B="$D/block.md"; mkblock "$B" "17:00" "Corrupted mirror"
OUT=$(DW_CORRUPT_PREFIX="$M.tmp." node -r "$TMP/inject-write-corrupt.cjs" \
  "$HELPER" --vault-target "$F" --session-uuid "$SUUID" --block-file "$B" --hh-mm 17:00 --local-target "$M" 2>"$D/err"); RC=$?
ERR=$(cat "$D/err")
check "exit 6" "$RC" "6"
case "$OUT" in *'"ok":false'*) ok "stdout JSON ok:false" ;; *) bad "stdout JSON ok:false (got: $OUT)" ;; esac
case "$OUT" in *"byte-identical"*) ok "violation names the mirror mismatch" ;; *) bad "violation names the mirror mismatch (got: $OUT)" ;; esac
check "stderr non-empty (loud)" "$([ -n "$ERR" ] && echo y || echo n)" "y"

echo
echo "== [EDGE] 17. 8-char uuid COLLISION fails loudly, target byte-unchanged =="
# Two DIFFERENT sessions sharing an 8-char prefix land on the same filename.
# Appending would reproduce the exact cross-session stomping per-session files
# exist to prevent, so this must fail rather than merge.
D="$TMP/collision"; mkdir -p "$D"; F="$D/dw-collision-$$.md"; seedday "$F"
B="$D/block.md"; mkblock "$B" "18:00" "Impostor session"
BEFORE=$(md5sum "$F" | cut -d' ' -f1)
OTHER="11111111-9999-9999-9999-999999999999"   # same uuid8 as $SUUID, different session
ERR=$(node "$HELPER" --vault-target "$F" --session-uuid "$OTHER" \
  --block-file "$B" --hh-mm 18:00 2>&1 >/dev/null); RC=$?
check "exit 2 on uuid8 collision" "$RC" "2"
case "$ERR" in *"COLLISION"*) ok "stderr names it a collision" ;; *) bad "stderr names it a collision (got: ${ERR:0:120})" ;; esac
case "$ERR" in *"$OTHER"*) ok "stderr names the incoming session" ;; *) bad "stderr names the incoming session" ;; esac
check "target byte-unchanged (NOT superseded)" "$(md5sum "$F" | cut -d' ' -f1)" "$BEFORE"

echo
echo "== [RED] 18. absent --session-uuid on an EXISTING target => exit 2, no silent skip =="
D="$TMP/noflag"; mkdir -p "$D"; F="$D/dw-noflag-$$.md"; seedday "$F"
B="$D/block.md"; mkblock "$B" "19:00" "Unidentified writer"
BEFORE=$(md5sum "$F" | cut -d' ' -f1)
ERR=$(node "$HELPER" --vault-target "$F" --block-file "$B" --hh-mm 19:00 2>&1 >/dev/null); RC=$?
check "exit 2 when the flag is omitted" "$RC" "2"
case "$ERR" in *"--session-uuid is required"*) ok "stderr names the missing flag" ;; *) bad "stderr names the missing flag (got: ${ERR:0:120})" ;; esac
check "target byte-unchanged" "$(md5sum "$F" | cut -d' ' -f1)" "$BEFORE"

echo
echo "== [RED] 19. two DIFFERENT sessions, same project+date => two files, zero supersessions =="
# The whole point of the change. Against the old per-day naming this produced ONE
# file with a superseded marker; it must now produce two independent files.
D="$TMP/twosess"; mkdir -p "$D"
S1="aaaaaaaa-1111-1111-1111-111111111111"
S2="bbbbbbbb-2222-2222-2222-222222222222"
F1="$D/Progress Report 2026-07-18 aaaaaaaa.md"
F2="$D/Progress Report 2026-07-18 bbbbbbbb.md"
FM1="$D/fm1.md"; FM2="$D/fm2.md"
for pair in "1:$S1:$FM1" "2:$S2:$FM2"; do
  n="${pair%%:*}"; rest="${pair#*:}"; u="${rest%%:*}"; fmp="${rest#*:}"
  cat > "$fmp" <<EOF
---
type: progress-report
session_uuid: $u
project: scratch-test
date: 2026-07-18
---

# Progress — scratch-test — 2026-07-18
EOF
done
B1="$D/b1.md"; mkblock "$B1" "20:00" "session one"
B2="$D/b2.md"; mkblock "$B2" "20:01" "session two"
node "$HELPER" --vault-target "$F1" --session-uuid "$S1" --block-file "$B1" --hh-mm 20:00 --frontmatter-file "$FM1" >/dev/null; R1=$?
node "$HELPER" --vault-target "$F2" --session-uuid "$S2" --block-file "$B2" --hh-mm 20:01 --frontmatter-file "$FM2" >/dev/null; R2=$?
check "both exit 0" "$R1/$R2" "0/0"
check "two separate files exist" "$([ -f "$F1" ] && [ -f "$F2" ] && echo y || echo n)" "y"
check "file 1 has exactly one H2 block" "$(grep -c '^## ' "$F1")" "1"
check "file 2 has exactly one H2 block" "$(grep -c '^## ' "$F2")" "1"
check "ZERO superseded markers across both" "$(cat "$F1" "$F2" | grep -c '^> Superseded by the ')" "0"
check "each file carries its own session_uuid" "$(grep -h '^session_uuid:' "$F1" "$F2" | sort -u | wc -l)" "2"

echo
echo "== [EDGE] 16. hygiene: no leaked locks or tmp files =="
check "no dayfile-write lock dirs leaked" "$(find /home/pedro/.claude/cache -maxdepth 1 -name 'dayfile-write-dw-*' 2>/dev/null | wc -l)" "0"
check "no tmp files leaked in scratch" "$(find "$TMP" -name '*.tmp.*' 2>/dev/null | wc -l)" "0"

echo
echo "================================"
echo "PASS: $PASS  FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
