#!/usr/bin/env python3
"""The `audit_window.py:993` writeback join, tested as a JOIN — not as two regexes.

`writeback_targets = set(sig["vault_targets"]) & snapshotted_vault_targets` is an
intersection of two independently-built strings:

  * left  — `VAULT_DAYFILE_TARGET_RE.finditer(transcript_line)`, `m.group(0)`
  * right — `"01-Projects/" + relpath(f, VAULT_PROJECTS)` for each file on disk
            whose basename matches `DAYFILE_NAME_RE`

Asserting each regex matches its own input is NOT sufficient and is the trap this
file exists for: widen one and not the other and the intersection is permanently
empty, `report_block_landed` is permanently False, and every future session-scoped
report is recorded as never having landed — with no error anywhere.
"""
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
AW = HERE / "audit_window.py"

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} (expected {want!r}, got {got!r})")


def load():
    spec = importlib.util.spec_from_file_location("aw", AW)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def join(aw, vault_projects, transcript_line):
    """Replicate both sides of the :993 intersection verbatim."""
    right = set()
    for pdir in sorted(pathlib.Path(vault_projects).iterdir()):
        prog = pdir / "progress"
        if not prog.is_dir():
            continue
        for f in sorted(prog.glob("*.md")):
            if aw.DAYFILE_NAME_RE.match(f.name):
                right.add("01-Projects/" + os.path.relpath(str(f), str(vault_projects)))
    left = {m.group(0) for m in aw.VAULT_DAYFILE_TARGET_RE.finditer(transcript_line)}
    return sorted(left & right), left, right


def main():
    aw = load()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="join-test-"))
    vp = tmp / "01-Projects"
    (vp / "Claude" / "progress").mkdir(parents=True)
    (vp / "Cucina Inventory" / "progress").mkdir(parents=True)

    NEW = "Progress Report 2026-08-18 1c9e50cb.md"
    OLD = "2026-08-13.md"
    SPACED = "Progress Report 2026-08-18 4e6f9f82.md"
    for name in (NEW, OLD):
        (vp / "Claude" / "progress" / name).write_text("x")
    (vp / "Cucina Inventory" / "progress" / SPACED).write_text("x")

    print("== [RED] 1. the join FIRES on a new-format name ==")
    line = ('{"type":"tool_use","input":{"command":"node dayfile-write.cjs '
            '--vault-target \\"/home/pedro/Documents/Necronomicon/01-Projects/Claude/'
            f'progress/{NEW}\\""}}}}')
    hit, left, right = join(aw, vp, line)
    check("left side found the target", len(left), 1)
    check("right side snapshotted it", f"01-Projects/Claude/progress/{NEW}" in right, True)
    check("intersection NON-empty", hit, [f"01-Projects/Claude/progress/{NEW}"])
    check("report_block_landed", bool(hit), True)

    print("\n== [GREEN] 2. a legacy day-file still joins ==")
    line_old = f'"vault_target":"/x/01-Projects/Claude/progress/{OLD}"'
    hit, _, _ = join(aw, vp, line_old)
    check("intersection NON-empty", hit, [f"01-Projects/Claude/progress/{OLD}"])

    print("\n== [EDGE] 3. a project name with a SPACE joins ==")
    # The left regex is `[^"\\\n]+?` between 01-Projects/ and /progress/, so a
    # space in the folder name must survive on both sides byte-for-byte.
    line_sp = f'"vault_target":"/x/01-Projects/Cucina Inventory/progress/{SPACED}"'
    hit, left, right = join(aw, vp, line_sp)
    check("intersection NON-empty", hit,
          [f"01-Projects/Cucina Inventory/progress/{SPACED}"])

    print("\n== [RED] 4. widening only DAYFILE_NAME_RE would break the join ==")
    # Simulate the bug the symmetry comment at :65-67 warns about: restore the
    # OLD left-side pattern (digits required immediately after /progress/) while
    # the right side stays widened. The intersection must go empty — which is
    # exactly the silent failure, and why spec 1 above is not redundant.
    import re
    real = aw.VAULT_DAYFILE_TARGET_RE
    aw.VAULT_DAYFILE_TARGET_RE = re.compile(
        r'01-Projects/[^"\\\n]+?/progress/(\d{4}-\d{2}-\d{2})\.md')
    try:
        hit_broken, left_broken, _ = join(aw, vp, line)
    finally:
        aw.VAULT_DAYFILE_TARGET_RE = real
    check("un-widened left finds nothing", len(left_broken), 0)
    check("intersection EMPTY (the bug)", hit_broken, [])
    check("report_block_landed would be False", bool(hit_broken), False)
    # And the fix restores it:
    hit_fixed, _, _ = join(aw, vp, line)
    check("widened left restores the join", bool(hit_fixed), True)

    print("\n== [EDGE] 5. a malformed suffix is rejected on BOTH sides ==")
    bad = "Progress Report 2026-08-18 12345.md"   # 5 hex, not 8
    (vp / "Claude" / "progress" / bad).write_text("x")
    line_bad = f'"vault_target":"/x/01-Projects/Claude/progress/{bad}"'
    hit_bad, left_bad, right_bad = join(aw, vp, line_bad)
    check("left rejects it", len(left_bad), 0)
    check("right rejects it", f"01-Projects/Claude/progress/{bad}" in right_bad, False)
    check("intersection empty", hit_bad, [])

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 32)
    print(f"PASS: {PASS}  FAIL: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
