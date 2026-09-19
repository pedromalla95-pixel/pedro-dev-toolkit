#!/usr/bin/env python3
"""Cross-project outcome store — the layer above GSD phases.

Importable only. There is deliberately no __main__ block and no argparse here:
`goals.py` is the single CLI that writes ledger JSON, and it delegates to this
module. See docs/superpowers/specs/2026-08-27-outcome-ledger-design.md section 3.1.

Storage: ~/.claude/goal-ledger/outcomes.json, beside the nine per-project files.
Locking, atomic writes and exit codes are goals.py's, reused by import.
"""
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import goals


def _time_now() -> float:
    """Indirection so tests can freeze the clock without patching the stdlib."""
    return time.time()

SCHEMA = 1

# Section 3.3 of the spec. Order is the forward path; index position is meaningful.
STATIONS = ("captured", "specced", "planned", "executing", "verifying", "closed")
VALID_STATION = frozenset(STATIONS)

# Section 3.4 of the spec. Deliberately NOT goals.VALID_STATUS, which is the
# older per-goal vocabulary {open, done, dropped, parked}. Reconciling the two is
# a migration, not part of this plan.
VALID_STATUS = frozenset({"open", "active", "blocked", "parked", "done"})

OUTCOMES_FILE = "outcomes.json"
# Double dash on purpose. goals.lock_path("outcomes") produces
# "goal-ledger-outcomes.lock", and goals._SLUG_RE forbids a leading dash, so no
# real project slug can ever produce this name. Without the extra dash a project
# slugifying to "outcomes" would contend on one physical lock dir under two
# different _HELD keys — a spurious exit 3, not corruption, but confusing.
OUTCOMES_LOCK_NAME = "goal-ledger--outcomes.lock"
_OUTCOME_ID_RE = re.compile(r"^outcome-(\d{4,})$")

_HELD_KEY = "__outcomes__"


def outcomes_path() -> Path:
    """Read goals.LEDGER_DIR at call time, never at import — the test suite
    repoints that attribute in setUp and the repoint has to reach us."""
    return goals.LEDGER_DIR / OUTCOMES_FILE


def outcomes_lock_path() -> Path:
    return goals.LOCK_DIR / OUTCOMES_LOCK_NAME


@contextmanager
def outcomes_lock():
    """The one lock for the cross-project file. Delegates to goals._lock_at, the
    same mkdir advisory lock used by ledger_lock and vault-append.cjs."""
    with goals._lock_at(outcomes_lock_path(), _HELD_KEY, "outcomes_lock"):
        yield


def _fresh() -> dict:
    return {"schema": SCHEMA, "updated": goals._now_iso(), "outcomes": []}


def read_outcomes() -> dict:
    path = outcomes_path()
    if not path.exists():
        return _fresh()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise goals.Abort(2, f"outcomes.json unreadable: {exc}")
    check_shape(data)
    return data


def write_outcomes(data: dict) -> None:
    """Atomic replace. MUST be called while holding outcomes_lock().

    The ownership re-check, the tmp+os.replace dance and the exit-5 contract are
    NOT reimplemented here — they are goals._atomic_write, the single body
    goals.write_ledger also calls (see Task 1 Step 2a). Two copies of an exit-5
    contract drift; one does not. Everything specific to this store is the three
    arguments below plus the noun in the abort messages.
    """
    check_shape(data)
    data["updated"] = goals._now_iso()
    goals._atomic_write(outcomes_path(), outcomes_lock_path(), _HELD_KEY, data,
                        what="outcomes")


def new_outcome_id(existing: list) -> str:
    """Highest id seen, plus one — never the lowest free gap.

    Monotonic on purpose: a deleted outcome's id can still be referenced from
    another record's `history`/`pendings`, and reusing the number would silently
    re-point those references at an unrelated outcome.
    """
    used = set()
    for rec in existing:
        if not isinstance(rec, dict):
            continue
        m = _OUTCOME_ID_RE.match(str(rec.get("id", "")))
        if m:
            used.add(int(m.group(1)))
    return f"outcome-{(max(used) + 1) if used else 1:04d}"


def check_shape(data: object) -> None:
    """Validate the whole file before any mutation is persisted. Raises
    goals.Abort(2) with a message naming the offending field."""
    if not isinstance(data, dict):
        raise goals.Abort(2, "outcomes.json must be a JSON object")
    if data.get("schema") != SCHEMA:
        raise goals.Abort(
            2, f"outcomes.json schema is {data.get('schema')!r}, expected {SCHEMA}")
    records = data.get("outcomes")
    if not isinstance(records, list):
        raise goals.Abort(2, "outcomes must be a list")

    seen = set()
    for i, rec in enumerate(records):
        where = f"outcomes[{i}]"
        if not isinstance(rec, dict):
            raise goals.Abort(2, f"{where} must be an object")

        oid = rec.get("id")
        if not isinstance(oid, str) or not _OUTCOME_ID_RE.match(oid):
            raise goals.Abort(2, f"{where}.id {oid!r} is not outcome-NNNN")
        if oid in seen:
            raise goals.Abort(2, f"{where}.id {oid!r} is a duplicate id")
        seen.add(oid)

        statement = rec.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise goals.Abort(2, f"{where}.statement must be a non-empty sentence")

        projects = rec.get("projects")
        if (not isinstance(projects, list) or not projects
                or not all(isinstance(p, str) and p.strip() for p in projects)):
            raise goals.Abort(
                2, f"{where}.projects must be a non-empty list of project names")

        if rec.get("station") not in VALID_STATION:
            raise goals.Abort(
                2, f"{where}.station {rec.get('station')!r} not in {sorted(VALID_STATION)}")

        if rec.get("status") not in VALID_STATUS:
            raise goals.Abort(
                2, f"{where}.status {rec.get('status')!r} not in {sorted(VALID_STATUS)}")

        for field in ("pendings", "history"):
            if not isinstance(rec.get(field), list):
                raise goals.Abort(2, f"{where}.{field} must be a list")

        # The ROWS, not just the list around them. cmd_pendings_next indexes
        # r["status"] and unblocked() indexes r["depends_on"] straight off what
        # is stored, so a hand-edited row missing a key raised KeyError -- an
        # uncaught exception out of goals.main(), which owes its caller nothing
        # but an Abort, and an exit 1 outside the fixed code set. ONE guard here
        # rather than .get() at each use: every reader reaches its rows through
        # read_outcomes, and the claim field is already defended this way while
        # pendings was defended nowhere.
        for j, row in enumerate(rec["pendings"]):
            at = f"{where}.pendings[{j}]"
            if not isinstance(row, dict):
                raise goals.Abort(2, f"{at} must be an object")
            rid = row.get("id")
            if not isinstance(rid, str) or not rid.strip():
                raise goals.Abort(2, f"{at}.id {rid!r} must be a row id")
            if row.get("status") not in STORED_PENDING_STATUS:
                raise goals.Abort(
                    2, f"{at} {rid}: status {row.get('status')!r} not in "
                       f"{sorted(STORED_PENDING_STATUS)}")
            if not isinstance(row.get("depends_on"), list):
                raise goals.Abort(2, f"{at} {rid}: depends_on must be a list")


def goal_outcome(goal: dict) -> str | None:
    """The outcome id a per-project goal belongs to, or None.

    None is legal and expected: the nine ledgers that exist today predate this
    field. An unattached goal is surfaced in the `unassigned` bucket (spec 3.2),
    not rejected.
    """
    value = goal.get("outcome")
    return value if isinstance(value, str) and _OUTCOME_ID_RE.match(value) else None


def set_goal_outcome(goal: dict, outcome_id: str) -> None:
    if not isinstance(outcome_id, str) or not _OUTCOME_ID_RE.match(outcome_id):
        raise goals.Abort(2, f"{outcome_id!r} is not an outcome id (outcome-NNNN)")
    goal["outcome"] = outcome_id


def find_outcome(data: dict, outcome_id: str) -> dict:
    for rec in data["outcomes"]:
        if rec.get("id") == outcome_id:
            return rec
    raise goals.Abort(2, f"no such outcome: {outcome_id}")


def next_station(station: str) -> str | None:
    """The station that follows, or None at the end of the line."""
    if station not in VALID_STATION:
        raise goals.Abort(2, f"unknown station {station!r}")
    i = STATIONS.index(station)
    return STATIONS[i + 1] if i + 1 < len(STATIONS) else None


def _split_projects(raw: str) -> list:
    items = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not items:
        raise goals.Abort(2, "--projects needs at least one project name")
    return items


def assert_not_in_worktree() -> None:
    """Refuse a pending or station write from inside a linked git worktree.

    Spec section 3.6: lanes shard work, they do not shard truth. A worktree is
    a throwaway checkout, so ledger state written from one is invisible to
    every other session. This is the mechanical stop.

    Detection: `--absolute-git-dir` and `--git-common-dir` are equal in a main
    working tree (both `<repo>/.git`) and differ in a linked worktree, where
    the first is `<repo>/.git/worktrees/<name>`. Simpler than the spec's
    path-containment phrasing and needs no path arithmetic. Outside a repo git
    exits non-zero and the write is allowed -- running goals.py from a non-repo
    directory is normal and must not break.

    ALLOWING and NOT DETERMINING are different things here, and the branches
    below keep them apart. A positive "you are not in a worktree" allows the
    write; learning nothing at all does not automatically. See the two comments
    below, which state why a timeout refuses and a too-old git allows.

    Version floor: `--path-format` landed in git 2.31 and `--absolute-git-dir`
    in git 2.13. Below either floor this guard cannot answer the question and
    says so explicitly rather than mis-comparing -- see the shape check below.

    Loud per OBSERVABLE-ARTIFACTS: exit 2 with the canonical path named, so the
    caller is told where to run instead rather than just being blocked.
    """
    try:
        git_dir = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, timeout=5)
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5)
    except OSError:
        # No git on PATH. A POSITIVE determination: there is no repo layer at
        # all on this machine, so there is no linked worktree to be in, and
        # allowing the write is correct.
        return
    except subprocess.SubprocessError as exc:
        # TimeoutExpired and its siblings. NOT a determination -- we learned
        # nothing about where we are. An earlier version of this handler folded
        # this in with the two allowed cases and claimed a timeout was
        # "indistinguishable from them here"; it is not, TimeoutExpired is its
        # own exception class and splitting the except distinguishes it. Never
        # fail-open on a safety check whose entire purpose is refusing a write:
        # downgrade autonomy instead. A git that hangs past the timeout is
        # anomalous, not routine, so the anomaly is surfaced rather than
        # silently spent on permitting the write this guard exists to stop.
        raise goals.Abort(
            2,
            f"cannot determine whether this is a linked git worktree: "
            f"git rev-parse failed in a way that answers nothing "
            f"({type(exc).__name__}: {exc}).\n"
            f"  you are in : {os.getcwd()}\n"
            f"Refusing the write rather than guessing. Re-run once git responds.")
    if git_dir.returncode != 0 or common.returncode != 0:
        return          # not inside a repo at all

    here = git_dir.stdout.strip()
    canonical = common.stdout.strip()
    if not here.startswith("/") or not canonical.startswith("/"):
        # `git rev-parse` exits 0 on an option it does not recognise and echoes
        # the option back on stdout, so `returncode != 0` is NOT a valid "did
        # this option work" check:
        #     $ git rev-parse --totally-unknown --git-common-dir
        #     rc=0  out=[--totally-unknown\n.git]
        # Below git 2.31 `canonical` is therefore "--path-format=absolute\n.git"
        # and below git 2.13 `here` is junk the same way -- one floor per flag,
        # same failure. Either way the equality test below is False in a MAIN
        # TREE, so without this branch every mutating command in every repo
        # refuses, printing an empty canonical path (os.path.dirname of a string
        # containing no "/" is "").
        #
        # This is a positive "cannot tell", and here allowing is correct. The
        # asymmetry with the timeout above is deliberate: a timeout is an
        # anomaly on a git that is otherwise working, while an unrecognised
        # option is a capability floor -- refusing on it turns a too-old git
        # into a tool that never writes at all, everywhere, forever.
        return
    if here == canonical:
        return          # main working tree

    main_tree = os.path.dirname(canonical.rstrip("/"))
    raise goals.Abort(
        2,
        f"refusing to write ledger state from a linked git worktree.\n"
        f"  you are in : {os.getcwd()}\n"
        f"  canonical  : {main_tree}\n"
        f"A worktree reads the ledger and never writes pending state "
        f"(spec 3.6). Run this from the canonical path above.")


def cmd_outcome_create(statement: str, projects: str) -> dict:
    assert_not_in_worktree()
    if not isinstance(statement, str) or not statement.strip():
        raise goals.Abort(2, "--statement must be a non-empty sentence")
    names = _split_projects(projects)
    now = goals._now_iso()
    with outcomes_lock():
        data = read_outcomes()
        rec = {
            "id": new_outcome_id(data["outcomes"]),
            "statement": statement.strip(),
            "projects": names,
            "station": "captured",
            "status": "open",
            "wake": None,
            "claim": None,
            "created": now,
            "last_seen": now,
            "pendings": [],
            "history": [],
        }
        data["outcomes"].append(rec)
        write_outcomes(data)
    return rec


def cmd_outcome_list(station: str | None = None,
                     status: str | None = None) -> dict:
    if station is not None and station not in VALID_STATION:
        raise goals.Abort(2, f"unknown station {station!r}")
    if status is not None and status not in VALID_STATUS:
        raise goals.Abort(2, f"unknown status {status!r}")
    data = read_outcomes()
    rows = [r for r in data["outcomes"]
            if (station is None or r["station"] == station)
            and (status is None or r["status"] == status)]
    return {"outcomes": rows, "count": len(rows)}


def cmd_outcome_attach(outcome_id: str, project: str, goal_id: str) -> dict:
    """Link one per-project goal upward.

    RE-PARENTING IS ALLOWED, and stated here because silence would read as an
    oversight: a goal that already carries an `outcome` is overwritten, since a
    goal moving between outcomes is ordinary and refusing it would need a
    detach command that does not exist. `--project` is likewise NOT required to
    be one of the outcome's own `projects` -- an outcome's span is a statement
    of intent, not a membership list, and work landing in a project nobody
    listed at capture time is the normal way that list turns out to be wrong.

    Two locks are taken SEQUENTIALLY, never
    nested: outcomes_lock() is acquired and released for the existence check,
    then ledger_lock(slug) is acquired separately for the write. Since no two
    locks are ever held at once, there is no lock-ordering hazard to guard
    against — this is safer than a nested-hold shape, not just simpler.

    Known future hazard: because outcomes_lock() is released before
    ledger_lock() opens, an outcome-deletion command (none exists yet) could
    delete the outcome in the gap between the two locks, and this call would
    attach a goal to an outcome that no longer exists. If deletion is ever
    added, it needs a real guard here — this note is not one."""
    assert_not_in_worktree()
    with outcomes_lock():
        find_outcome(read_outcomes(), outcome_id)   # existence check
    slug = goals.slugify(project)
    with goals.ledger_lock(slug):
        data = goals.read_ledger(slug, project)
        target = None
        for g in data["goals"]:
            if g.get("id") == goal_id:
                target = g
                break
        if target is None:
            raise goals.Abort(2, f"no goal {goal_id} in project {project}")
        set_goal_outcome(target, outcome_id)
        data["updated"] = goals._now_iso()
        goals.write_ledger(slug, data)
    return {"outcome": outcome_id, "project": project, "goal": goal_id,
            "attached": True}


def cmd_outcome_advance(outcome_id: str, to: str | None = None,
                        reopen: bool = False) -> dict:
    """Move one station. Forward-by-one by default; any earlier station is
    reachable only with --reopen, and the move is recorded in history.

    Skipping forward is refused outright: a plan that never existed cannot be
    executed, and silently allowing the jump is how a station stops meaning
    anything.
    """
    assert_not_in_worktree()
    with outcomes_lock():
        data = read_outcomes()
        rec = find_outcome(data, outcome_id)
        current = rec["station"]

        if to is None:
            target = next_station(current)
            if target is None:
                raise goals.Abort(
                    2, f"{outcome_id} is at {current}; there is no station after it")
        else:
            if to not in VALID_STATION:
                raise goals.Abort(2, f"unknown station {to!r}")
            here, there = STATIONS.index(current), STATIONS.index(to)
            if there == here + 1:
                pass                        # ordinary forward step
            elif there < here:
                if not reopen:
                    raise goals.Abort(
                        2, f"{outcome_id} is at {current}; moving back to {to} "
                           f"needs --reopen")
            elif there == here:
                # A no-op move needs its own branch. Without it this case falls
                # to the skip-forward else below, whose message indexes
                # STATIONS[here + 1] -- out of range at `closed`, the last
                # station, so the CLI died with an uncaught IndexError instead
                # of an Abort. At the other five stations it merely produced a
                # nonsense "cannot skip from X to X" message.
                raise goals.Abort(2, f"{outcome_id} is already at {current}")
            else:
                raise goals.Abort(
                    2, f"cannot skip from {current} to {to}; "
                       f"the next station is {STATIONS[here + 1]}")
            target = to

        now = goals._now_iso()
        rec["history"].append({"at": now, "from": current, "to": target,
                               "reopen": bool(reopen and to is not None)})
        rec["station"] = target
        rec["last_seen"] = now
        write_outcomes(data)

    return {"id": outcome_id, "station": target, "previous_station": current,
            "next_station": next_station(target)}


# Two values, closed, stated as a frozenset like every other vocabulary in this
# module (VALID_STATION, VALID_STATUS, VALID_PENDING_STATUS). Closed rather than
# free-form because `history` is now mixed-provenance: human free text and
# machine transitions share one list, and anything rendering that list has to
# discriminate on `event`. A typo minting a third event name would be invisible
# until a reader silently skipped the entry.
VALID_NOTE_EVENT = frozenset({"note", "session-end"})


def cmd_outcome_note(outcome_id: str, text: str, event: str = "note") -> dict:
    """Append one free-text, timestamped entry to an outcome's history.

    The only human-authored surface on an outcome. Every other append here is
    machine-generated with a fixed key set, which is why this one carries an
    explicit `event` rather than being told apart by key-shape sniffing.

    `event` is "note" when a person writes it and "session-end" when the
    SessionEnd hook records where the outcome stood on the way out. Nothing
    else.
    """
    assert_not_in_worktree()
    if not isinstance(text, str) or not text.strip():
        raise goals.Abort(2, "text is empty")
    if event not in VALID_NOTE_EVENT:
        raise goals.Abort(
            2, f"unknown event {event!r}, expected one of {sorted(VALID_NOTE_EVENT)}")

    text = text.strip()
    with outcomes_lock():
        data = read_outcomes()
        rec = find_outcome(data, outcome_id)     # aborts 2 if there is no such id
        now = goals._now_iso()
        rec["history"].append({"at": now, "event": event, "text": text})
        rec["last_seen"] = now
        write_outcomes(data)

    return {"outcome": outcome_id, "event": event, "text": text, "at": now}


def cmd_outcome_status(outcome_id: str, status: str,
                       wake: str | None = None) -> dict:
    """Set status. `parked` requires a written wake condition and nothing else
    accepts one -- leaving parked clears it, so a stale wake cannot linger."""
    assert_not_in_worktree()
    if status not in VALID_STATUS:
        raise goals.Abort(
            2, f"unknown status {status!r}, expected one of {sorted(VALID_STATUS)}")
    if status == "parked":
        if not isinstance(wake, str) or not wake.strip():
            raise goals.Abort(
                2, "--status parked requires --wake: the condition that "
                   "reactivates this outcome. A park with no wake is how a "
                   "deadline rots silently.")
    elif wake is not None and wake.strip():
        raise goals.Abort(2, "--wake is only meaningful with --status parked")

    with outcomes_lock():
        data = read_outcomes()
        rec = find_outcome(data, outcome_id)
        previous = rec["status"]
        now = goals._now_iso()
        rec["status"] = status
        rec["wake"] = wake.strip() if status == "parked" else None
        rec["last_seen"] = now
        rec["history"].append({"at": now, "status_from": previous,
                               "status_to": status})
        write_outcomes(data)

    return {"id": outcome_id, "status": status, "previous_status": previous,
            "wake": rec["wake"]}


# A claim is not a lock. goals.LOCK_ABANDON_MS (1 hour) is how long a *write*
# lock may sit abandoned; an outcome is held across a whole working session, so
# the claim needs its own number. Four hours: longer than a sitting, far shorter
# than PARK_AFTER_DAYS (14 days).
#
# The heartbeat is the ONLY automatic staleness signal, deliberately. Storing a
# pid here would be worse than useless: goals.py runs as a short-lived CLI
# process, so the pid it recorded is already dead microseconds later and every
# claim the CLI ever wrote would read as stale on the next invocation. A crashed
# session therefore holds its outcome for up to CLAIM_EXPIRY_MS; the manual
# escape is `outcome-release --steal`, which records who took it.
CLAIM_EXPIRY_MS = 14_400_000


def claim_is_live(claim, now_epoch: float) -> bool:
    """False means the outcome is takeable: no claim, or a heartbeat older
    than CLAIM_EXPIRY_MS. A live session refreshes the heartbeat by re-running
    outcome-claim with its own session id, which is always allowed.

    An UNREADABLE heartbeat is neither: it aborts. This is the first code in
    the store that parses a stored timestamp -- check_shape validates the
    claim's shape no further than "outcomes[i] is an object", so heartbeat_at
    arrives here straight off a hand-editable file and goals._parse_iso raises
    ValueError on anything it cannot read. Returning False there (the shape the
    brief for this task proposed) would have been fail-open on a safety check:
    a corrupted timestamp would read as "dead", and the next session would take
    an outcome someone may well still be working, silently. So the unreadable
    case is loud, names the field, and points at the escape hatch. Missing or
    non-string stays False -- that is absence, not corruption, and a record
    written before this field existed must not become unusable.
    """
    if not isinstance(claim, dict):
        return False
    stamp = claim.get("heartbeat_at")
    if not isinstance(stamp, str):
        return False
    try:
        beat = goals._parse_iso(stamp)
    except Exception as exc:
        # Broad on purpose: _parse_iso raises ValueError for a malformed string,
        # but datetime.timestamp() can raise OverflowError/OSError on an extreme
        # year, and a narrow handler here would repeat the wrong-exception-family
        # mistake goals._pid_alive already had to be fixed for. Nothing but an
        # Abort may leave goals.main() -- the comment there on the line after
        # `args = parser.parse_args(argv)` states that contract ("nothing but
        # an `Abort` escapes"). Cited by anchor, not by line number: the bare
        # number this comment first carried pointed at unrelated code as soon
        # as the same commit inserted twelve lines above it.
        raise goals.Abort(
            2, f"claim.heartbeat_at {stamp!r} is not a readable timestamp "
               f"({exc}); this outcome cannot be judged held or free. Take it "
               f"anyway with: outcome-release --outcome <id> --session <yours> "
               f"--steal")
    return (now_epoch - beat) * 1000 <= CLAIM_EXPIRY_MS


def cmd_outcome_claim(outcome_id: str, session: str) -> dict:
    assert_not_in_worktree()
    # fullmatch, not match - the same reason goals._check_slug and
    # goals.session_live both use fullmatch, and both document: `$` matches
    # before a trailing "\n" too, so .match() would let "sess-b\n" through as
    # if it were the plain token "sess-b", and a claim keyed on that never
    # matches the release that follows. (Anchored on the function names rather
    # than a line number, which an insertion above silently invalidates.)
    if not isinstance(session, str) or not goals._SESSION_ID_RE.fullmatch(session):
        raise goals.Abort(2, f"{session!r} is not a usable session id")
    now_epoch = _time_now()
    with outcomes_lock():
        data = read_outcomes()
        rec = find_outcome(data, outcome_id)
        # Normalised to None if it is not an object: check_shape validates the
        # claim no further than "outcomes[i] is an object", so anything at all
        # can be sitting in this field.
        held = rec.get("claim") if isinstance(rec.get("claim"), dict) else None
        held_by = held.get("session_id") if held else None
        if claim_is_live(held, now_epoch) and held_by != session:
            raise goals.Abort(
                2, f"{outcome_id} is claimed by session "
                   f"{held_by} (heartbeat {held.get('heartbeat_at')}); "
                   f"it frees itself {CLAIM_EXPIRY_MS // 3600000}h after that "
                   f"heartbeat, or now with: outcome-release --outcome "
                   f"{outcome_id} --session {session} --steal")
        now = goals._now_iso()
        rec["claim"] = {"session_id": session, "heartbeat_at": now}
        rec["last_seen"] = now
        if held is not None and held_by != session:
            # Reaching here with a claim already present means it was expired
            # (a live one either aborted above or belongs to us). This is the
            # ORDINARY contested handover -- a session that died hours ago --
            # and it is the most common one, so it cannot be the only
            # transition with no trace. `claim-taken`, not `claim-stolen`: an
            # expiry and a forced takeover must stay tellable apart.
            rec["history"].append({"at": now, "event": "claim-taken",
                                   "from": held_by, "by": session})
        write_outcomes(data)
    return {"id": outcome_id, "claimed": True, "claim": rec["claim"],
            "station": rec["station"], "next_station": next_station(rec["station"])}


def cmd_outcome_release(outcome_id: str, session: str,
                        steal: bool = False) -> dict:
    """steal=True force-releases another session's live claim. It exists because
    the heartbeat is the only automatic signal: a session that crashes without
    releasing holds its outcome for up to CLAIM_EXPIRY_MS, and someone has to be
    able to say "that session is gone, give it back" without waiting. The theft
    is recorded on the outcome, never silent."""
    assert_not_in_worktree()
    # The same guard, the same fullmatch and the same message as
    # cmd_outcome_claim -- see the comment there. No bypass rides on its
    # absence, since an invalid id can never HOLD a claim; but under --steal
    # the unvalidated string is written into history[].by, so a newline or a
    # control character would reach the ledger and everything that renders it.
    if not isinstance(session, str) or not goals._SESSION_ID_RE.fullmatch(session):
        raise goals.Abort(2, f"{session!r} is not a usable session id")
    now_epoch = _time_now()
    with outcomes_lock():
        data = read_outcomes()
        rec = find_outcome(data, outcome_id)
        held = rec.get("claim") if isinstance(rec.get("claim"), dict) else None
        stole = False
        try:
            live = claim_is_live(held, now_epoch)
        except goals.Abort:
            # --steal must never depend on parsing the record it overrides. If
            # the unreadable-heartbeat abort could reach here, the only command
            # able to clear a corrupt claim would be the one refusing to run,
            # and the outcome would be stuck for good. Unreadable therefore
            # counts as HELD (we cannot prove it dead), so the theft is recorded
            # like any other -- but only under --steal.
            if not steal:
                raise
            live = True
        held_by = held.get("session_id") if held else None
        if live and held_by != session:
            if not steal:
                raise goals.Abort(
                    2, f"{outcome_id} is claimed by {held_by}, "
                       f"not by {session}; pass --steal to take it anyway")
            # A flag, not `stolen_from = held_by`. Keying the history entry on
            # the id's truthiness conflated "nothing was stolen" with "stolen
            # from a holder we cannot name": a claim with a fresh heartbeat and
            # a missing session_id is LIVE, so --steal cleared something real
            # while the ledger and the receipt both said nothing happened. The
            # unknown holder is recorded as null below -- honestly unknown, and
            # not a sentinel like "unknown", which is itself a valid session id.
            stole = True
        rec["claim"] = None
        rec["last_seen"] = goals._now_iso()
        if stole:
            event = "claim-stolen"
        elif held is not None and held_by != session:
            # An expired claim released by someone who never held it. Not a
            # theft (there was nothing live to take), but still a change of
            # hands, and the audit trail exists for exactly this.
            event = "claim-cleared"
        else:
            event = None      # our own claim, or none at all: nothing changed hands
        if event:
            rec.setdefault("history", []).append({
                "at": rec["last_seen"], "event": event,
                "from": held_by, "by": session})
        write_outcomes(data)
    return {"id": outcome_id, "claimed": False, "station": rec["station"],
            "stolen_from": held_by if stole else None, "stole": stole}


# --- The spec pending block (section 5.1) ------------------------------------

PEND_BEGIN = "%%PENDINGS:BEGIN%%"
PEND_END = "%%PENDINGS:END%%"

# Section 3.4, minus `parked`: a row parks by its OUTCOME parking, never on its
# own. Deliberately a different set from VALID_STATUS above.
VALID_PENDING_STATUS = frozenset({"open", "active", "blocked", "done"})

# What check_shape accepts in a STORED row, as against what the parser will
# produce. `parked` is the whole difference: parse_pending_block refuses it, but
# unblocked() documents and handles a row that arrives carrying it from a
# hand-edited ledger, and check_shape now guards that same door. Rejecting it
# here would make a file one live function deliberately copes with unreadable to
# every other -- read_outcomes validates on every READ, not only before a write.
STORED_PENDING_STATUS = VALID_PENDING_STATUS | {"parked"}
_ROW_ID_RE = re.compile(r"^P\d{2,}$")
_NO_DEPS = {"", "-", "—", "--", "none", "n/a"}


def _is_sentence(text: str) -> bool:
    """Spec 5.1: a description is a full sentence, not a label. Two signals,
    both cheap: it ends in a full stop, and it has enough words to be a claim."""
    t = text.strip()
    return t.endswith(".") and len(t.split()) >= 5


def _fence_span(text: str, what: str, code: int) -> tuple:
    """Locate the ONE pending fence, line-anchored, or abort.

    Copied in approach from goals.cmd_dashboard, which solved this exact problem
    for its own `%%GOALS%%` sentinels: `re.finditer` with `^...$` under
    MULTILINE, and a hard abort on more than one hit.

    Two reasons a bare `text.find()` is wrong here, and the second is the
    dangerous one:

    1. A spec DOCUMENTS this format by showing the sentinels. The governing spec
       has an illustrative example at section 5.1 and the real P01-P22 table at
       section 11. `find()` returns the FIRST hit, so ingest would read the
       example -- a snippet with no data rows -- and abort "the pendings block
       is empty" while the real table sat 150 lines below, untouched.
    2. `_splice` uses the same locator, so had the ambiguity been resolved the
       wrong way it would have OVERWRITTEN the section 5.1 example with the
       generated table and left section 11 stale. A silent write to the wrong
       region of a governing spec is not something a later test would notice.

    Measured on that spec on 2026-08-27: line-anchoring alone disambiguates
    BEGIN (the example's BEGIN line carries a trailing annotation, so it is not
    a bare line) but NOT END, which is bare on both. Hence the abort rather than
    a guess: an illustration must annotate BOTH of its sentinel lines.
    """
    b = [m.start() for m in re.finditer(rf"^{re.escape(PEND_BEGIN)}$", text, re.MULTILINE)]
    e = [m.start() for m in re.finditer(rf"^{re.escape(PEND_END)}$", text, re.MULTILINE)]
    if len(b) > 1 or len(e) > 1:
        raise goals.Abort(
            code, f"ambiguous pending sentinels in {what}: "
                  f"{len(b)} line-anchored {PEND_BEGIN} and {len(e)} {PEND_END}. "
                  f"Refusing to guess which fence is the real one - a wrong "
                  f"guess here rewrites the wrong region of a spec. Give the "
                  f"illustrative one a trailing annotation on BOTH its lines so "
                  f"it is no longer bare, or remove it.")
    if not b or not e or e[0] < b[0]:
        raise goals.Abort(
            code, f"no {PEND_BEGIN} ... {PEND_END} block found in {what}; "
                  f"every spec in this system carries one (spec 5.1). The "
                  f"sentinels must each be alone on their own line.")
    return b[0], e[0]


def parse_pending_block(text: str) -> list:
    """Read the fenced markdown table into row dicts. Raises Abort(2) on any
    malformed row -- an unparseable block is a defect, not an empty list.

    Abort code 2 throughout: at parse time a bad fence is bad INPUT. _splice
    uses 4 for the same conditions, because there it is a refusal to write.
    """
    start, end = _fence_span(text, "the spec text", 2)

    rows = []
    seen = set()
    for raw in text[start + len(PEND_BEGIN):end].splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("|"):
            # Everything between the sentinels is REGENERATED from the rows
            # returned here, so a line this loop does not turn into a row is
            # deleted from the document on the next ingest. Two shapes reached
            # this branch and were lost in silence: a GFM-legal row written
            # without its outer pipes (`P02 | ... | open |` -- GFM makes them
            # optional), and any prose note a human left inside the fence.
            # Both are refused rather than guessed at: parsing the pipe-less
            # row would silently accept a second row grammar, and keeping the
            # note would need somewhere to put it in a generated table.
            # Blank lines stay legal -- render_pending_block emits one after
            # BEGIN and one before END, so refusing those would refuse the
            # parser's own output.
            raise goals.Abort(
                2, f"line inside the pendings fence is not a table row: "
                   f"{line!r}. The fence is regenerated from its rows, so a "
                   f"line that is not one would be deleted. Move prose outside "
                   f"the sentinels; give a row its leading '|'.")
        cells = [c.strip() for c in line.strip("|").split("|")]
        # Header and separator first, and only when the id cell is NON-EMPTY:
        # `set("") <= {"-", ":"}` is True, so an empty id cell would otherwise
        # be mistaken for a separator and dropped.
        if cells[0] and (cells[0].lower() == "id" or set(cells[0]) <= {"-", ":"}):
            continue
        # A row that is not four cells ABORTS. It must not be skipped: _splice
        # regenerates the whole fence from the rows parsed here, so a skipped
        # row is deleted from the spec file as well as missing from the ledger
        # -- silent data loss in two places, in a governing document.
        #
        # The message states the count and the line, and stops there. It used
        # to name an unescaped `|` as the cause, which is wrong twice: a `|`
        # written `\|` aborts identically (the split below is unaware of the
        # escape), and a plain 3- or 5-column row aborts with no stray pipe
        # involved at all. An abort may not assert a cause it cannot observe.
        if len(cells) != 4:
            raise goals.Abort(
                2, f"pending row must have exactly 4 cells "
                   f"(id | description | depends on | status), got {len(cells)}: "
                   f"{line!r}. Every '|' on the line is counted as a cell "
                   f"boundary, including one written '\\|' -- this parser does "
                   f"not honour that escape.")

        rid, desc, deps_raw, status = cells
        if not _ROW_ID_RE.match(rid):
            raise goals.Abort(2, f"row id {rid!r} is not PNN")
        if rid in seen:
            raise goals.Abort(2, f"duplicate row id {rid}")
        seen.add(rid)
        if not _is_sentence(desc):
            raise goals.Abort(
                2, f"{rid}: description must be a full sentence ending in '.', "
                   f"not a label -- got {desc!r}")
        if status not in VALID_PENDING_STATUS:
            raise goals.Abort(
                2, f"{rid}: status {status!r} not in {sorted(VALID_PENDING_STATUS)}")

        deps = ([] if deps_raw.lower() in _NO_DEPS
                else [d.strip() for d in deps_raw.split(",") if d.strip()])
        rows.append({"id": rid, "description": desc, "depends_on": deps,
                     "status": status})

    if not rows:
        raise goals.Abort(2, "the pendings block is empty")

    check_dependencies(rows)
    return rows


def render_pending_block(rows: list) -> str:
    """The inverse of parse_pending_block. Deterministic: same rows in, byte-
    identical text out (the A3 property, applied to this generator)."""
    out = [PEND_BEGIN, "", "| id | description | depends on | status |",
           "|---|---|---|---|"]
    for r in rows:
        deps = ", ".join(r["depends_on"]) if r["depends_on"] else "—"
        out.append(f"| {r['id']} | {r['description']} | {deps} | {r['status']} |")
    out += ["", PEND_END]
    return "\n".join(out) + "\n"


def check_dependencies(rows: list) -> None:
    """Cycles are a hard error; so is a row marked done while a row it depends
    on is not (spec 5.1).

    The unknown-id check lives HERE and runs BEFORE the walk, not in
    parse_pending_block. Two reasons. This function is public and reachable
    directly, and its walk indexes a colour map keyed by the rows it was given,
    so an id that is not a row raised KeyError -- an unhandled exception, so no
    controlled exit code, from the validator whose whole job is controlled
    rejection. And keeping the check in the caller meant a second caller would
    silently not get it.
    """
    graph = {r["id"]: list(r["depends_on"]) for r in rows}
    status = {r["id"]: r["status"] for r in rows}

    for rid, deps in graph.items():
        for dep in deps:
            if dep not in graph:
                raise goals.Abort(
                    2, f"{rid} depends on {dep}, which is not a row in this block")

    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(graph, WHITE)

    def visit(node, trail):
        if colour[node] == GREY:
            cycle = " -> ".join(trail[trail.index(node):] + [node])
            raise goals.Abort(2, f"dependency cycle: {cycle}")
        if colour[node] == BLACK:
            return
        colour[node] = GREY
        for dep in graph[node]:
            visit(dep, trail + [node])
        colour[node] = BLACK

    for node in graph:
        visit(node, [])

    for rid, deps in graph.items():
        if status[rid] != "done":
            continue
        unmet = [d for d in deps if status.get(d) != "done"]
        if unmet:
            raise goals.Abort(
                2, f"{rid} is marked done but depends on {', '.join(unmet)}, "
                   f"which {'is' if len(unmet) == 1 else 'are'} not done")


def unblocked(rows: list) -> list:
    """Rows the planner may turn into plan steps: not done, not authored
    not-workable, every dependency done. Blocked rows stay visible and
    explicitly unplanned (spec 5.1).

    `blocked` and `parked` are AUTHORED statuses in spec 3.4, not derived ones
    -- `blocked` maps to "GSD phase status: blocked / BLOCKER file present" and
    `parked` carries a wake condition. Excluding only `done` handed the planner
    a row whose own label says it cannot be planned, and cmd_pendings_next then
    counted that same row inside its derived complement, so one payload said
    both things at once.

    `parked` is excluded even though VALID_PENDING_STATUS omits it: this
    function is public and takes any rows it is handed, and the rows a ledger
    hands it are checked against STORED_PENDING_STATUS, which admits `parked`
    precisely because this function copes with it. A status the parser would
    never have produced therefore still arrives. Still a denylist rather than an
    allowlist of {open, active}: an unrecognised status stays VISIBLE as work, since
    silently disappearing is the failure this whole function was fixed for.
    """
    done = {r["id"] for r in rows if r["status"] == "done"}
    return [r for r in rows
            if r["status"] not in ("done", "blocked", "parked")
            and all(d in done for d in r["depends_on"])]


def next_pending(rows: list):
    ready = unblocked(rows)
    return ready[0] if ready else None


def _splice(text: str, block: str) -> str:
    """Replace the fenced region, leaving every byte outside it alone.

    Uses the SAME line-anchored locator as the parser, not a second bare find().
    Two different locators over one file is how the parser and the writer come
    to disagree about which fence they are working on - and here that
    disagreement means reading section 11 and overwriting section 5.1.

    Abort code 4, matching cmd_dashboard's refusal to write an ambiguous file:
    at this point the input already parsed, so a fence problem is a write
    failure, not bad input.
    """
    start, end = _fence_span(text, "the spec file being rewritten", 4)
    return text[:start] + block.rstrip("\n") + text[end + len(PEND_END):]


def cmd_pendings_ingest(outcome_id: str, spec_path: str) -> dict:
    """Read a spec's fenced block, validate it whole, attach the rows to the
    outcome, and rewrite the fence from the ledger.

    GUARANTEE, stated precisely because the obvious stronger claim is false:

    * A REJECTED ingest touches nothing. Validation is complete before the
      first write - parse_pending_block raises on a malformed row, an unknown
      dependency, a cycle, or an ambiguous fence, and it runs above the lock.

    * An ACCEPTED ingest is two writes, not one atom. The ledger is written
      inside outcomes_lock(); the spec is rewritten after the lock is released.
      A crash between them leaves the ledger updated and the spec's fence
      stale.

    True two-file atomicity needs a journal, and writing the spec to a temp
    first would only move the window, not close it - so the honest guarantee is
    documented rather than a stronger one being faked. What makes the window
    survivable is that this operation is IDEMPOTENT: re-running the same ingest
    re-reads the unchanged spec, produces identical rows, and rewrites both the
    ledger and the fence to the same values. RECOVERY IS: run the ingest again.

    * A CONCURRENT EDIT is the case re-running does NOT recover. The spec text
      is read at the top of this function and written back at the bottom; an
      edit another session makes to the fence in between is overwritten by the
      rows read before it existed. That is a lost update, not a stale fence,
      and the re-run recovers nothing because the edit is already gone. It
      needs a human, which is why it is named here rather than folded into the
      crash case above.
    """
    assert_not_in_worktree()
    path = Path(spec_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise goals.Abort(2, f"cannot read spec {spec_path}: {exc}")

    rows = parse_pending_block(text)          # validates; raises before any write
    rewritten = _splice(text, render_pending_block(rows))

    with outcomes_lock():
        data = read_outcomes()
        rec = find_outcome(data, outcome_id)
        rec["pendings"] = rows
        rec["last_seen"] = goals._now_iso()
        write_outcomes(data)

    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(rewritten, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise goals.Abort(4, f"cannot rewrite spec {spec_path}: {exc}")
    finally:
        tmp.unlink(missing_ok=True)

    return {"outcome": outcome_id, "ingested": len(rows), "spec": str(path),
            "next": next_pending(rows),
            "open": len([r for r in rows if r["status"] != "done"])}


def cmd_pendings_set(outcome_id: str, spec_path: str, row_id: str,
                     status: str) -> dict:
    """Set ONE row's status in the spec fence and in the ledger, in one call.

    Replaces the hand-edit-then-remember-to-re-ingest two-step, whose failure
    mode was forgetting the second half and leaving the fence and the ledger
    saying different things.

    It opens no second write path: it rewrites the one status cell and then
    calls cmd_pendings_ingest unchanged, inheriting the P10 check_dependencies
    guard (via parse_pending_block), outcomes_lock(), and the canonical fence
    render. The intermediate edit therefore only has to be PARSEABLE -- ingest
    re-serializes every row from a fixed template, so nothing here preserves
    spacing.

    Not the tool for a description rewrite (column 2). This writes column 4.
    """
    assert_not_in_worktree()      # per-verb guard; nothing central supplies it
    if status not in VALID_PENDING_STATUS:
        raise goals.Abort(
            2, f"status {status!r} not in {sorted(VALID_PENDING_STATUS)}")
    # The same shape parse_pending_block enforces on a row it reads. Without it
    # `--row id` matches the HEADER line -- four cells, first cell `id` -- and
    # the command exits 0 reporting {"row": "id"} having changed nothing, since
    # render_pending_block re-emits the header from its own template. A no-op
    # reported as success is worse than a refusal.
    if not _ROW_ID_RE.match(row_id):
        raise goals.Abort(2, f"row id {row_id!r} is not PNN")

    path = Path(spec_path)
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise goals.Abort(2, f"cannot read spec {spec_path}: {exc}")

    start, end = _fence_span(original, "the spec text", 2)   # the one locator
    head, fence, tail = original[:start], original[start:end], original[end:]
    lines = fence.split("\n")

    hits = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) == 4 and cells[0] == row_id:
            hits.append((i, cells))
    if len(hits) != 1:
        raise goals.Abort(
            2, f"no row {row_id} in the fence" if not hits
               else f"{len(hits)} rows in the fence carry the id {row_id}")

    i, cells = hits[0]
    cells[3] = status
    lines[i] = "| " + " | ".join(cells) + " |"

    # ponytail: written in place, not through the tmp+os.replace shape ingest
    # uses. This edit is transient -- ingest overwrites it with the canonical
    # render microseconds later -- and the spec is git-tracked, so the
    # truncation window is recoverable. In place also keeps the failure
    # ORDERING honest: a directory the process cannot write is a failure of
    # ingest's rewrite, after the ledger commits, which is the case the
    # discriminating rule below exists for and which tmp+os.replace here would
    # pre-empt into a harmless pre-write refusal.
    try:
        path.write_text(head + "\n".join(lines) + tail, encoding="utf-8")
    except OSError as exc:
        raise goals.Abort(4, f"cannot rewrite spec {spec_path}: {exc}")

    try:
        result = cmd_pendings_ingest(outcome_id, str(path))
    except goals.Abort as exc:
        # DISCRIMINATING RULE. Code 4 out of ingest means the ledger already
        # committed: check_dependencies and every other validation run above
        # the lock, and the only other Abort(4) in that path is _splice's
        # ambiguous-fence refusal, which is unreachable here because _splice
        # re-locates the fence in the same in-memory text parse_pending_block
        # has already accepted. So a blanket restore would revert the fence
        # under a committed ledger -- the exact desync this verb removes.
        if exc.code == 4:
            raise goals.Abort(
                4, f"the ledger committed {status} for {row_id} and the spec "
                   f"fence carries it, but the fence was not canonically "
                   f"re-rendered: {exc.message}. Do NOT hand-restore the spec; "
                   f"that would revert the fence under a committed ledger. "
                   f"Re-run: pendings-ingest --outcome {outcome_id} "
                   f"--spec {spec_path}")
        try:
            path.write_text(original, encoding="utf-8")
        except OSError as restore_exc:
            raise goals.Abort(
                4, f"{exc.message}; and the spec {spec_path} still carries the "
                   f"un-ingested edit because restoring it failed: "
                   f"{restore_exc}")
        raise

    return {**result, "row": row_id, "status": status}


def cmd_pendings_next(outcome_id: str) -> dict:
    """The one line a new session reads. Computed from the dependency graph on
    disk, never held in a session's head (spec 5.1)."""
    data = read_outcomes()
    rec = find_outcome(data, outcome_id)
    rows = rec.get("pendings", [])
    done = len([r for r in rows if r["status"] == "done"])
    ready = unblocked(rows) if rows else []
    return {"outcome": outcome_id, "statement": rec["statement"],
            "station": rec["station"], "next_station": next_station(rec["station"]),
            "next": ready[0] if ready else None,
            "blocked": len(rows) - len(ready) - done,
            "done": done, "total": len(rows)}
