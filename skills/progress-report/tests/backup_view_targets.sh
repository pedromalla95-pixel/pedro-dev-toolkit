#!/usr/bin/env bash
# Phase 4 -- back up all 36 real VIEW targets and verify each backup byte-for-byte.
#
# The Obsidian vault is NOT a git repository. No checkout, no reflog, no undo.
# This cmp-verified backup is the only rollback that will exist for the single
# live write in Phase 5, so a silent backup failure is the defect this exists
# to prevent (OBSERVABLE-ARTIFACTS): any cp failure, any cmp MISMATCH, and any
# disagreement between the manifest and a fresh recount of the vault exits
# non-zero and the phase stops.
#
# WHAT THIS DOES NOT PROTECT AGAINST, stated so the header does not oversell:
#   - Disk or filesystem loss. $DEST and $VAULT are on the same host and the
#     same filesystem; this is a copy, not an independent backup medium.
#   - Cross-file snapshot atomicity. Each file is copied then immediately
#     cmp'd, which catches a peer session's edit landing inside THAT file's
#     window. The 36 files are not captured as one point-in-time snapshot, so
#     under concurrent vault writers the set can mix pre- and post-edit state.
#     Closing that needs a vault-wide advisory lock every session honours,
#     which is outside this slice.
#
# Layout: paths are stored RELATIVE TO THE VAULT ROOT, so "$DEST/01-Projects/..."
# mirrors "$VAULT/01-Projects/...". `cp --parents` is deliberately NOT used --
# it resolves against the current working directory, not against a named root,
# which would bury the tree under "$DEST/home/pedro/Documents/..." and silently
# break both Phase 5's diff of a backed-up note and the manifest-driven restore.
set -euo pipefail

VAULT=/home/pedro/Documents/Necronomicon
BACKUP_ROOT=/home/pedro/.claude/backups

# ---------------------------------------------------------------------------
# emit_artifacts -- write the three self-describing artifacts into $DEST.
#
# A backup directory has to be usable by someone who finds it months from now
# with no plan, no script, and no session transcript in hand. It therefore
# carries its own restore tool, its own integrity baseline, and its own
# completion marker.
#
# Defined as a function, and reachable via `--emit-artifacts <DEST>`, so an
# EXISTING backup can be brought up to spec WITHOUT re-running the copy. That
# is not a convenience: re-running the copy after a live write would capture
# post-write bytes into a newer directory, and every consumer resolves $DEST
# with `ls -1dt ... | head -1`, so the new directory would silently shadow the
# real undo. Backfill, never re-copy.
# ---------------------------------------------------------------------------
emit_artifacts() {
  # SHA256SUMS covers the BACKUP COPIES, not the live vault. `cmp` only proved
  # the copy matched its source at copy time; this is what detects the backup
  # itself rotting afterwards. Paths hold spaces ("Cucina Inventory",
  # "Goals & To-Do.md"), so this is an `IFS= read -r` loop, never xargs.
  : > "$DEST/SHA256SUMS"
  while IFS= read -r src || [ -n "$src" ]; do
    [ -n "$src" ] || continue
    rel="${src#"$VAULT"/}"
    ( cd "$DEST" && sha256sum "$rel" ) >> "$DEST/SHA256SUMS"
  done < "$DEST/MANIFEST.txt"

  cat > "$DEST/RESTORE.sh" <<'RESTORE'
#!/usr/bin/env bash
# Restore every file in MANIFEST.txt from this backup back into the Obsidian
# vault. The vault is NOT a git repository -- this backup is its only undo.
# Every restored file is cmp-verified; the integrity of the backup itself is
# checked against SHA256SUMS BEFORE anything is written, because restoring
# rotted bytes into an unversioned vault is worse than not restoring at all.
#
# Usage: ./RESTORE.sh --yes
set -euo pipefail

VAULT=/home/pedro/Documents/Necronomicon

cd "$(dirname "${BASH_SOURCE[0]}")" && DEST="$PWD"

[ "${1:-}" = "--yes" ] || {
  echo "This OVERWRITES live vault files under $VAULT from $DEST."
  echo "Re-run with --yes to proceed."; exit 1; }

[ -f "$DEST/DONE.txt" ] || {
  echo "no DONE.txt -- this backup did not finish; refusing to restore from it" >&2
  exit 1; }

echo "verifying the backup against SHA256SUMS before writing anything..."
sha256sum -c "$DEST/SHA256SUMS" > /dev/null || {
  echo "BACKUP INTEGRITY CHECK FAILED -- refusing to restore" >&2; exit 1; }

n=0
while IFS= read -r f || [ -n "$f" ]; do
  [ -n "$f" ] || continue
  rel="${f#$VAULT/}"
  cp -a "$DEST/$rel" "$f"
  cmp "$DEST/$rel" "$f"
  n=$(( n + 1 ))
done < "$DEST/MANIFEST.txt"
echo "restored and cmp-verified $n file(s)"
RESTORE
  chmod +x "$DEST/RESTORE.sh"

  # DONE.txt is the completion marker AND the orientation note. It is written
  # last, so a directory left by a cp failure, a cmp mismatch, or a kill
  # mid-run is distinguishable from a finished one by its absence.
  cat > "$DEST/DONE.txt" <<EOF
p11 view-target backup -- COMPLETE
finished: $(date -Is)
vault root: $VAULT
targets backed up: $(command grep -c '' "$DEST/MANIFEST.txt") (cmp-verified byte-identical at copy time)

To restore everything:   ./RESTORE.sh --yes
To check for bit rot:    sha256sum -c SHA256SUMS   (run from this directory)

MANIFEST.txt  absolute path of every backed-up file
VERIFY.txt    per-file OK/MISMATCH from the backup run
SHA256SUMS    checksums of the COPIES in this directory
$( [ -f "$DEST/LOG.txt" ] \
   && echo "LOG.txt       stdout+stderr of the run that produced this directory" \
   || echo "LOG.txt       absent -- this directory predates log capture, or was backfilled" )

Absence of this file means the backup did not finish. Do not restore from it.
EOF
}

# Backfill mode: bring an existing backup up to spec without re-copying.
if [ "${1:-}" = "--emit-artifacts" ]; then
  DEST="${2:?usage: $0 --emit-artifacts <backup-dir>}"
  [ -f "$DEST/MANIFEST.txt" ] || { echo "no MANIFEST.txt in $DEST" >&2; exit 1; }
  emit_artifacts
  echo "artifacts written to $DEST"
  exit 0
fi

DEST="$BACKUP_ROOT/p11-view-targets-$(date +%Y%m%d-%H%M%S)"

# `mkdir` without -p, deliberately. The timestamp has one-second granularity and
# several sessions share this machine: -p would silently succeed into an
# existing DEST, truncating its MANIFEST.txt while appending to its VERIFY.txt.
# A collision must hard-fail, not merge.
mkdir "$DEST"

# Put the run's own output on disk, not only in a transcript. stderr is teed
# too -- a log missing the error message is the useless kind.
exec > >(tee "$DEST/LOG.txt") 2>&1

# ---------------------------------------------------------------------------
# Enumerate the four target classes, one absolute path per line.
#
# The transcript filter is currently a no-op -- that .jsonl quotes the marker in
# transcript text but not as a bare whole line, so `-rIl '^...$'` never returns
# it. It stays as a cheap belt-and-braces filter; --include='*.md' is the
# durable guard and carries the real weight.
#
# Each class is captured separately rather than streamed straight into the
# manifest: a zero-match `command grep` inside a `{ ... } > file` group under
# `set -euo pipefail` kills the script mid-write with NO stderr, leaving a
# truncated manifest and a bare non-zero exit with nothing to read.
# ---------------------------------------------------------------------------
goals_md=$(ls -1 "$VAULT/01-Projects"/*/Goals.md) \
  || { echo "no 01-Projects/*/Goals.md matched -- refusing to build a manifest" >&2; exit 1; }

state_notes=$(command grep -rIl --include='*.md' '^%%PROJECT-STATE:BEGIN%%$' "$VAULT/01-Projects" \
  | command grep -v '/transcript-ad6bc401\.jsonl$') \
  || { echo "no %%PROJECT-STATE:BEGIN%% notes survived the filter -- refusing to build a manifest" >&2; exit 1; }

# NOT deduped, deliberately. The four classes are disjoint on disk (verified: no
# 01-Projects/*/Goals.md carries the state marker). If that ever stops holding,
# a duplicate makes the manifest larger than the recount below and the run
# ABORTS -- which is the correct outcome. `sort -u` would absorb the overlap
# silently and let a genuinely missing target hide behind a duplicated one.
{
  printf '%s\n' "$VAULT/00-Dashboard/Portfolio.md"
  printf '%s\n' "$VAULT/00-Dashboard/Goals & To-Do.md"
  printf '%s\n' "$goals_md"
  printf '%s\n' "$state_notes"
} > "$DEST/MANIFEST.txt"

# ---------------------------------------------------------------------------
# Assert the manifest is the RIGHT SIZE, not merely internally consistent.
# The OK-count check further down can only ever confirm that the loop which
# just ran, ran; it is blind to a target that never entered the manifest.
# EXPECTED is re-derived from disk here -- never a literal, in either
# direction. The whole point is that the total comes from the vault rather
# than from a number written in a plan.
# ---------------------------------------------------------------------------
GOALSMD=$(printf '%s\n' "$goals_md" | command grep -c '')
STATEND=$(printf '%s\n' "$state_notes" | command grep -c '')
EXPECTED=$(( GOALSMD + STATEND + 2 ))   # + Portfolio.md + Goals & To-Do.md

# `command grep -c ''` counts LINES, including a final line with no trailing
# newline. `wc -l` counts newline CHARACTERS and undercounts such a line by
# one -- in lockstep with the read loop below, which is what makes the
# undercount invisible to a naive lines-vs-OK comparison.
manifest_lines=$(command grep -c '' "$DEST/MANIFEST.txt")
[ "$manifest_lines" = "$EXPECTED" ] || {
  echo "MANIFEST SIZE MISMATCH: expected=$EXPECTED manifest=$manifest_lines" >&2
  echo "  Goals.md=$GOALSMD state-notes=$STATEND (+2 dashboard files)" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Copy relative-to-vault-root, then cmp each copy against its source.
#
# `|| [ -n "$src" ]` is load-bearing: bash `read` returns non-zero on a final
# line with no trailing newline, so the plain form silently never processes
# that target -- no cp, no cmp, no OK row, and a clean exit 0.
# ---------------------------------------------------------------------------
: > "$DEST/VERIFY.txt"
copied=0
while IFS= read -r src || [ -n "$src" ]; do
  [ -n "$src" ] || { echo "empty manifest line -- abort" >&2; exit 1; }
  [ -f "$src" ] || { echo "manifest names a missing file: $src" >&2; exit 1; }
  rel="${src#"$VAULT"/}"
  [ "$rel" != "$src" ] || { echo "path is not under $VAULT: $src" >&2; exit 1; }
  mkdir -p "$DEST/$(dirname "$rel")"
  cp -a "$src" "$DEST/$rel"
  # `if cmp -s` rather than a bare cmp: under `set -e` a bare cmp kills the
  # script before it can record WHY, and MISMATCH is the line that matters.
  if cmp -s "$src" "$DEST/$rel"; then
    printf 'OK %s\n' "$src" >> "$DEST/VERIFY.txt"
    copied=$(( copied + 1 ))
  else
    printf 'MISMATCH %s\n' "$src" >> "$DEST/VERIFY.txt"
    echo "BACKUP MISMATCH: $src" >&2
    exit 1
  fi
done < "$DEST/MANIFEST.txt"

# `|| true` on every grep -c: it exits 1 on a zero count, and under `set -e` a
# failing command substitution aborts the script BEFORE the diagnostic below
# can print the numbers that explain what went wrong.
oks=$(command grep -c '^OK ' "$DEST/VERIFY.txt" || true)
mismatches=$(command grep -c '^MISMATCH ' "$DEST/VERIFY.txt" || true)

[ "$copied" = "$manifest_lines" ] || {
  echo "loop processed $copied of $manifest_lines manifest lines" >&2; exit 1; }
[ "$oks" = "$manifest_lines" ] || {
  echo "manifest lines ($manifest_lines) != OK count ($oks)" >&2; exit 1; }
[ "$mismatches" = "0" ] || { echo "MISMATCH count is $mismatches" >&2; exit 1; }

# Only now, past every assertion, does the directory get its restore tool,
# its integrity baseline, and its completion marker.
emit_artifacts

echo "manifest_lines=$manifest_lines"
echo "expected=$EXPECTED ok=$oks mismatch=$mismatches"
echo "DEST=$DEST"
