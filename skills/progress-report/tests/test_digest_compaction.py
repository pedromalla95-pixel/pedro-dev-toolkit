#!/usr/bin/env python3
"""active_branch must bridge a compaction boundary without swallowing a real rewind.

A compaction severs the parentUuid chain: the `compact_boundary` system entry carries
`parentUuid: null`, so a naive walk back from the tail stops there and counts every
pre-compaction turn as rewound. On a long session that is the entire day's work, reported
as `rewind_excluded` and dropped from the digest — the report then calls a 12-hour session
"thin" and records that nothing happened.

Claude Code already records the bridge: the boundary entry carries `logicalParentUuid`
pointing at the last pre-compaction entry. Verified against a real transcript
(c9f24e72, 2026-08-26): 6 boundaries, all carrying it, all resolving, and NO non-boundary
entry carries the field — so following it cannot bridge anything that is not a compaction.

The rewind case must keep working: a rewound branch has no boundary and no
logicalParentUuid, so its orphaned turns still fall outside the walk.
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "digest.py"
spec = importlib.util.spec_from_file_location("digest_under_test", SCRIPT)
digest = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = digest  # digest.py uses @dataclass, which resolves via sys.modules
spec.loader.exec_module(digest)

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, got, want) -> None:
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")


def msg(uid: str, parent: str | None, kind: str = "user") -> dict:
    return {"uuid": uid, "parentUuid": parent, "type": kind}


def boundary(uid: str, logical_parent: str | None) -> dict:
    return {
        "uuid": uid,
        "parentUuid": None,
        "logicalParentUuid": logical_parent,
        "type": "system",
        "subtype": "compact_boundary",
    }


def uuids(entries: list[dict]) -> list[str]:
    return [e["uuid"] for e in entries]


def test_compaction_boundary_is_bridged() -> None:
    entries = [
        msg("a", None),
        msg("b", "a", "assistant"),
        boundary("bnd", "b"),
        msg("c", "bnd"),
        msg("d", "c", "assistant"),
    ]
    active, excluded = digest.active_branch(entries)
    check("bridged: pre-compaction turns kept", uuids(active), ["a", "b", "c", "d"])
    check("bridged: nothing reported as rewound", excluded, 0)


def test_two_boundaries_in_series_are_both_bridged() -> None:
    entries = [
        msg("a", None),
        boundary("bnd1", "a"),
        msg("b", "bnd1"),
        boundary("bnd2", "b"),
        msg("c", "bnd2"),
    ]
    active, excluded = digest.active_branch(entries)
    check("two boundaries: all turns kept", uuids(active), ["a", "b", "c"])
    check("two boundaries: nothing rewound", excluded, 0)


def test_genuine_rewind_is_still_excluded() -> None:
    """The regression guard. `b` is an abandoned sibling of `c` — no boundary, no bridge."""
    entries = [
        msg("a", None),
        msg("b", "a", "assistant"),
        msg("c", "a"),
        msg("d", "c", "assistant"),
    ]
    active, excluded = digest.active_branch(entries)
    check("rewind: abandoned turn dropped", uuids(active), ["a", "c", "d"])
    check("rewind: still counted as excluded", excluded, 1)


def test_dangling_logical_parent_does_not_crash() -> None:
    entries = [
        msg("a", None),
        boundary("bnd", "nonexistent"),
        msg("c", "bnd"),
    ]
    active, excluded = digest.active_branch(entries)
    check("dangling bridge: walk stops cleanly", uuids(active), ["c"])
    check("dangling bridge: unreached turn counted", excluded, 1)


def test_self_referential_bridge_terminates() -> None:
    """A boundary naming itself must not loop forever."""
    entries = [msg("a", None), boundary("bnd", "bnd"), msg("c", "bnd")]
    active, _ = digest.active_branch(entries)
    check("self-reference: terminates", uuids(active), ["c"])


def main() -> int:
    for fn in (
        test_compaction_boundary_is_bridged,
        test_two_boundaries_in_series_are_both_bridged,
        test_genuine_rewind_is_still_excluded,
        test_dangling_logical_parent_does_not_crash,
        test_self_referential_bridge_terminates,
    ):
        fn()
    print(f"{CHECKS} assertions, {len(FAILURES)} failed")
    for failure in FAILURES:
        print(f"  FAIL {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
