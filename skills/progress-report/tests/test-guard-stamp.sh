#!/usr/bin/env bash
# test-guard-stamp.sh — behavioral test: precompact-report-guard.js freshness-stamp dedup
# (audit AUDIT-2026-07-17 L3-0: vault-only runs left the guard blind -> redundant dispatch).
# Drives the REAL hook with crafted stdin. Persona-audit hardened (2026-07-18):
#   - session keys randomized per run (no shared-cache collision, no cross-run flags)
#   - silence is proven CAUSAL: every silent scenario re-runs after removing the one
#     suppressor and must then dispatch (a fail-open crash cannot fake that pair)
#   - guard exit code asserted 0 per invocation (crash -> visible FAIL, not silence)
#   - under-threshold negative control (the gate that makes it a PREcompact guard)
set -uo pipefail

GUARD=/home/pedro/.claude/hooks/precompact-report-guard.js
CACHE=/home/pedro/.claude/cache
STAMPS="$CACHE/progress-report-fresh"
SCRATCH=$(mktemp -d)
BASE="guardtest-$$-$RANDOM"
PASS=0; FAIL=0
cleanup() { rm -rf "$SCRATCH"; rm -f "$CACHE"/progress-report-precompact-"$BASE"-*.flag "$STAMPS/$BASE"-*.json; }
trap cleanup EXIT

mk_transcript() { # $1 = session key, $2 = input_tokens; prints transcript path
  local f="$SCRATCH/$1.jsonl"
  printf '{"message":{"usage":{"input_tokens":%s,"cache_read_input_tokens":0}}}\n' "$2" > "$f"
  printf '%s' "$f"
}

fresh_key() { # $1 = suffix; prints a key with all cache state pre-cleared
  local k="$BASE-$1"
  rm -f "$CACHE/progress-report-precompact-$k.flag" "$STAMPS/$k.json"
  printf '%s' "$k"
}

GUARD_RC=0
run_guard() { # $1 = transcript path; prints hook stdout; records exit in GUARD_RC
  local out
  out=$(printf '{"prompt":"hello there","transcript_path":"%s","cwd":"%s"}' "$1" "$SCRATCH" \
    | PROGRESS_REPORT_PRECOMPACT=on PROGRESS_REPORT_PRECOMPACT_TOKENS=1000 node "$GUARD" 2>"$SCRATCH/guard.err")
  GUARD_RC=$?
  printf '%s' "$out"
}

check() { # $1 label, $2 expect ("dispatch"|"silent"), $3 output
  local got="silent"
  [[ "$3" == *"@DISPATCH:progress-report"* ]] && got="dispatch"
  if [[ "$got" == "$2" && $GUARD_RC -eq 0 ]]; then
    echo "  PASS: $1"; PASS=$((PASS+1))
  else
    echo "  FAIL: $1 (expected $2/rc0, got $got/rc$GUARD_RC)"; sed -n '1,3p' "$SCRATCH/guard.err" 2>/dev/null
    FAIL=$((FAIL+1))
  fi
}

echo "== A. no stamp, no cwd mirror, tokens over trigger => DISPATCH =="
K=$(fresh_key a); T=$(mk_transcript "$K" 50000)
check "vault-blind session gets the precompact dispatch" dispatch "$(run_guard "$T")"

echo "== B. fresh stamp => SILENT, then stamp removed => DISPATCH (causal pair) =="
K=$(fresh_key b); T=$(mk_transcript "$K" 50000)
mkdir -p "$STAMPS"
printf '{"ts":"now","vault_target":"x","local_target":""}\n' > "$STAMPS/$K.json"
check "fresh stamp suppresses dispatch" silent "$(run_guard "$T")"
rm -f "$STAMPS/$K.json"
check "removing ONLY the stamp re-enables dispatch (silence was the stamp's doing)" dispatch "$(run_guard "$T")"

echo "== C. stale stamp (>30min) => DISPATCH =="
K=$(fresh_key c); T=$(mk_transcript "$K" 50000)
printf '{"ts":"old","vault_target":"x","local_target":""}\n' > "$STAMPS/$K.json"
touch -d '45 minutes ago' "$STAMPS/$K.json"
check "stale stamp does not suppress" dispatch "$(run_guard "$T")"

echo "== D. cwd mirror => SILENT, then mirror removed => DISPATCH (causal pair) =="
K=$(fresh_key d); T=$(mk_transcript "$K" 50000)
touch "$SCRATCH/session-progress-2099-01-01.md"
check "fresh cwd mirror still dedups (pre-existing behavior intact)" silent "$(run_guard "$T")"
rm -f "$SCRATCH/session-progress-2099-01-01.md"
check "removing ONLY the mirror re-enables dispatch" dispatch "$(run_guard "$T")"

echo "== E. tokens BELOW trigger => SILENT (threshold gate is live) =="
K=$(fresh_key e); T=$(mk_transcript "$K" 500)
check "under-threshold session never dispatches" silent "$(run_guard "$T")"
# causal control for E: same key, tokens raised over trigger => dispatch
T=$(mk_transcript "$K" 50000)
check "same session over threshold dispatches (E's silence was the gate)" dispatch "$(run_guard "$T")"

echo "================================"
echo "PASS: $PASS  FAIL: $FAIL"
[[ $FAIL -eq 0 ]]
