#!/usr/bin/env bash
# Phase 5 -- assert the live generator run moved NO human-owned bytes.
#
# For every sentinel-spliced target, compares the bytes OUTSIDE the sentinel
# pair between the Phase 4 backup copy and the current vault file. Prefix and
# suffix are compared separately so the report names which side moved.
#
# This is the mechanical form of the Sprint-2000 guarantee: that note's
# "$2,000 USD of NEW external income" and its 12 September 2026 deadline sit
# outside the fence, so they are covered by construction rather than by
# inspection. The vault has no git; there is no second chance to notice.
#
# Per-project 01-Projects/*/Goals.md is EXCLUDED by design -- the vault schema
# (Necronomicon/CLAUDE.md:20) declares it machine-owned and rewritten wholesale,
# so it has no outside-fence region to protect.
#
# Usage: assert_outside_fence_unchanged.sh [BACKUP_DEST] [VAULT_ROOT]
# Both default to the live values; the parameters exist so the non-vacuity
# control can point the script at a mktemp -d fixture.
set -euo pipefail

DEST="${1:-$(ls -1dt /home/pedro/.claude/backups/p11-view-targets-* | head -1)}"
VAULT="${2:-/home/pedro/Documents/Necronomicon}"

[ -n "$DEST" ] && [ -f "$DEST/MANIFEST.txt" ] \
  || { echo "no backup manifest at ${DEST:-<empty>}/MANIFEST.txt" >&2; exit 1; }

DEST="$DEST" VAULT="$VAULT" python3 - <<'PY'
import os, sys

DEST = os.environ["DEST"]
VAULT = os.environ["VAULT"]

# Sentinels quoted from source, matched as BARE WHOLE LINES. A substring match
# would also hit prose that merely quotes a marker -- exactly why the manifest
# filters out the Perfwatch transcript.
#   Portfolio.md   portfolio-refresh.cjs:93-94
#   Goals & To-Do  goals.py:44-45
#   index notes    portfolio-refresh.cjs:984-985
PAIRS = {
    "00-Dashboard/Portfolio.md":      (b"%%PORTFOLIO:BEGIN%%", b"%%PORTFOLIO:END%%"),
    "00-Dashboard/Goals & To-Do.md":  (b"%%GOALS:BEGIN%%", b"%%GOALS:END%%"),
}
STATE = (b"%%PROJECT-STATE:BEGIN%%", b"%%PROJECT-STATE:END%%")

def split_outside(raw, begin, end, label, failures):
    """Return (prefix, suffix) bytes around the fence, or None on a bad fence."""
    lines = raw.split(b"\n")
    b = [i for i, ln in enumerate(lines) if ln == begin]
    e = [i for i, ln in enumerate(lines) if ln == end]
    # A missing or duplicated sentinel is a FAILURE, never a skip: a file whose
    # fence got mangled must not sail through as "nothing outside to compare".
    if len(b) != 1 or len(e) != 1:
        failures.append(f"{label}: expected exactly one {begin.decode()} and one "
                        f"{end.decode()} as bare lines, found {len(b)} and {len(e)}")
        return None
    if b[0] > e[0]:
        failures.append(f"{label}: {end.decode()} appears before {begin.decode()}")
        return None
    return (b"\n".join(lines[:b[0]]), b"\n".join(lines[e[0] + 1:]))

failures = []
checked = 0
skipped_goals = 0

with open(os.path.join(DEST, "MANIFEST.txt"), "rb") as fh:
    manifest = [ln for ln in fh.read().decode("utf-8").split("\n") if ln]

for src in manifest:
    rel = src[len(VAULT) + 1:] if src.startswith(VAULT + "/") else None
    if rel is None:
        failures.append(f"{src}: manifest path is not under {VAULT}")
        continue
    if rel.startswith("01-Projects/") and rel.endswith("/Goals.md"):
        skipped_goals += 1          # wholesale-generated, no outside-fence region
        continue

    sentinels = PAIRS.get(rel, STATE)
    backup_path = os.path.join(DEST, rel)
    if not os.path.exists(backup_path):
        failures.append(f"{rel}: no backup copy at {backup_path}")
        continue
    if not os.path.exists(src):
        failures.append(f"{rel}: current file is missing from the vault")
        continue

    with open(backup_path, "rb") as fh:
        before = fh.read()
    with open(src, "rb") as fh:
        after = fh.read()

    ob = split_outside(before, *sentinels, f"{rel} (backup)", failures)
    oa = split_outside(after, *sentinels, f"{rel} (current)", failures)
    if ob is None or oa is None:
        continue

    checked += 1
    if ob[0] != oa[0]:
        failures.append(f"{rel}: bytes BEFORE the fence changed "
                        f"({len(ob[0])} -> {len(oa[0])} bytes)")
    if ob[1] != oa[1]:
        failures.append(f"{rel}: bytes AFTER the fence changed "
                        f"({len(ob[1])} -> {len(oa[1])} bytes)")

print(f"outside-fence compared: {checked} file(s); "
      f"excluded per-project Goals.md: {skipped_goals}")

# A run that compared nothing must not report success.
if checked == 0:
    print("FAIL: zero files compared -- the assertion would be vacuous", file=sys.stderr)
    sys.exit(1)

if failures:
    print(f"FAIL: {len(failures)} outside-fence violation(s):", file=sys.stderr)
    for f in failures:
        print(f"  {f}", file=sys.stderr)
    sys.exit(1)

print("OK: human-owned bytes outside every fence are byte-identical")
PY
