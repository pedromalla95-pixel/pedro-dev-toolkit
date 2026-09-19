#!/usr/bin/env python3
"""Goal ledger for /progress-report — sole owner of ~/.claude/goal-ledger/<slug>.json.

Locking mirrors scripts/vault-append.cjs: a mkdir advisory lock, because mkdir is
atomic so exactly one process wins the grant. The lock lives on its OWN path, never
on the ledger file: the ledger is replaced by rename, so a lock held against the
ledger inode would stop protecting the path the moment the rename lands.

Exit codes (identical to vault-append.cjs):
  0 ok / no-op | 2 usage or bad input | 3 lock timeout | 4 write failure
  5 lock lost mid-write (ledger NOT modified)
"""

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
LEDGER_DIR = Path.home() / ".claude" / "goal-ledger"
LOCK_DIR = Path.home() / ".claude" / "cache"
RUN_DIR = Path.home() / ".claude" / "cache" / "goal-run"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
LOCK_POLL_MS = 50
LOCK_CAP_MS = 5000
LOCK_ABANDON_MS = 3600000
PARK_AFTER_DAYS = 14
LIVE_SESSION_HOURS = 24

# Cross-project dashboard. The lock name deliberately does NOT start with
# "goal-ledger-", which is the prefix every slug-derived lock carries, so no
# project name can ever collide with it.
DASHBOARD_LOCK_NAME = "goals-dashboard.lock"
DASHBOARD_MANAGED_BY = "goals-dashboard"
SENT_BEGIN = "%%GOALS:BEGIN%%"
SENT_END = "%%GOALS:END%%"

_HELD: dict[str, str] = {}

# Errors that mean "another process moved first" — expected during a race, never a fault.
_RACE_ERRNOS = frozenset({errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST})


def _is_race(exc: OSError) -> bool:
    return exc.errno in _RACE_ERRNOS


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Session ids are UUIDs or test fixtures like "sess-a" — always plain tokens.
# Used to sanitize a value before it reaches glob(): unrestricted, "/" walks
# the pattern outside PROJECTS_DIR (see session_live) and "*"/"?"/"[...]" turn
# a lookup into a wildcard instead of an exact match.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _check_slug(slug: str) -> str:
    # fullmatch, not match: `$` in the pattern matches before a trailing "\n"
    # too, so `.match()` would let "demo\n" through as a valid slug.
    if not _SLUG_RE.fullmatch(slug):
        raise Abort(2, f"invalid slug: {slug!r}")
    return slug


class Abort(Exception):
    """Carries the process exit code. May be raised while `ledger_lock` is held
    (`cmd_load` raises it from `read_ledger`, `autopark`, and `write_ledger`
    inside the `with` block) — the context manager's `finally` releases the
    lock regardless of how the block exits, so a caller never needs to."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


SLUG_MAX = 60
SLUG_HASH_CHARS = 8


def slugify(name: str) -> str:
    """Project name to ledger filename stem.

    Truncation at `SLUG_MAX` used to discard everything past the cap with
    nothing to distinguish the originals, so two projects whose names agree in
    the first 60 slug characters shared ONE ledger file — silently merging two
    projects' goals, which no surface in this system would report. A short
    hash of the FULL pre-truncation slug is appended when (and only when)
    truncation actually discarded something.

    Only on truncation, deliberately: a slug at or under the cap is already
    unambiguous, so suffixing it would make every ledger filename less
    readable to buy nothing. Nor can the two forms collide — a suffixed slug
    is always longer than `SLUG_MAX`, and an unsuffixed one never is.

    The hash covers the pre-truncation SLUG rather than the raw `name` so that
    the same project keeps the same ledger across the case and punctuation
    variations the slug already normalises away ("Foo Bar" and "foo-bar" have
    always mapped to one ledger; that is unchanged here). Two genuinely
    different names that normalise to one slug under the cap still collide,
    exactly as before — that is a separate, pre-existing property of
    normalisation, not something this cap fix silently changes.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise Abort(2, "project name produced an empty slug")
    if len(slug) <= SLUG_MAX:
        return slug
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:SLUG_HASH_CHARS]
    # `.rstrip("-")` first: the cut can land mid-separator, and `slug--a1b2c3d4`
    # is uglier than `slug-a1b2c3d4` while a LEADING "-" would fail `_check_slug`.
    return f"{slug[:SLUG_MAX].rstrip('-')}-{digest}"


def ledger_path(slug: str) -> Path:
    _check_slug(slug)
    return LEDGER_DIR / f"{slug}.json"


def lock_path(slug: str) -> Path:
    _check_slug(slug)
    return LOCK_DIR / f"goal-ledger-{slug}.lock"


def _pid_alive(pid: int) -> bool:
    # os.kill treats 0 as "my whole process group" and -1 as "every process I may
    # signal", so a corrupted owner file could make a dead holder look alive forever.
    # Only a positive PID names one process.
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        # `OverflowError` alongside `ProcessLookupError`: `os.kill` raises it
        # for a PID outside C `long` range, and it is an `ArithmeticError`,
        # not an `OSError` — so neither handler here caught it and it escaped
        # as a raw traceback, breaking the module contract that nothing but an
        # `Abort` ever leaves this file. `_evict_if_dead` parses the owner
        # file's PID with `int()`, which is unbounded in Python, so a
        # corrupted or hand-edited owner file reaches this call with exactly
        # such a value. Answering False is the correct reading and the safe
        # direction: a PID that cannot be represented cannot name a running
        # process, and treating it as dead lets the stale lock be reclaimed
        # rather than wedging the project's ledger until the one-hour backstop.
        return False
    except PermissionError:
        return True
    return True


def _new_owner_token() -> str:
    """`<pid>-<rand>`, mirroring vault-append.cjs: the random half is what stops a
    reused PID from being mistaken for the original holder."""
    return f"{os.getpid()}-{os.urandom(4).hex()}"


def _try_grant(lock: Path) -> str | None:
    """Returns our owner token on success, None if someone else holds the lock.

    The owner file is written under a guard: a lock directory with no owner inside
    is reclaimable only by the one-hour age backstop, so failing to write it would
    wedge this project for an hour. Roll back instead — fail loud, without the orphan.
    """
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Abort(4, f"lock directory unusable: {exc}")
    try:
        lock.mkdir()
    except FileExistsError:
        return None
    except OSError as exc:
        raise Abort(4, f"could not create lock: {exc}")
    token = _new_owner_token()
    try:
        (lock / "owner").write_text(token, encoding="utf-8")
    except OSError as exc:
        shutil.rmtree(lock, ignore_errors=True)
        raise Abort(4, f"could not claim lock: {exc}")
    return token


def _evict_if_dead(lock: Path) -> None:
    """Reclaim a lock nobody is holding. Two independent conditions, both from
    vault-append.cjs: the owner PID is not alive, OR the lock is older than
    LOCK_ABANDON_MS. The age backstop covers the case liveness cannot — a crashed
    holder whose PID was reused by an unrelated process, which would otherwise wedge
    this project's ledger forever. Eviction is one atomic rename, so exactly one
    racer wins and the losers re-poll."""
    owner = lock / "owner"
    evict = False
    try:
        pid = int(owner.read_text(encoding="utf-8").strip().split("-", 1)[0])
    except (OSError, ValueError):
        # Granting is two steps (mkdir, then write owner), so a missing owner file
        # may be a lock being legitimately acquired right now. Never steal on this
        # alone — fall through to the age backstop.
        pass
    else:
        evict = not _pid_alive(pid)
    if not evict:
        try:
            evict = (time.time() - lock.stat().st_mtime) * 1000 > LOCK_ABANDON_MS
        except OSError as exc:
            if _is_race(exc):
                return
            raise Abort(4, f"cannot stat lock: {exc}")
    if not evict:
        return
    grave_name = f"{lock.name}.dead-{_new_owner_token()}"
    try:
        lock.rename(lock.with_name(grave_name))
    except OSError as exc:
        if _is_race(exc):
            return          # another racer evicted it first
        raise Abort(4, f"cannot evict stale lock: {exc}")
    else:
        shutil.rmtree(lock.with_name(grave_name), ignore_errors=True)


@contextmanager
def _lock_at(lock: Path, key: str, label: str):
    """Acquire `lock`, tracking ownership under `key` in _HELD.

    `label` names the CALLING context and is used only to build the
    non-re-entrancy message — it never participates in _HELD keying or in path
    construction. Without it, a re-entrant dashboard acquisition would report
    "ledger_lock('dashboard') is not re-entrant", naming a function the caller
    never invoked, against a lock that is not a ledger lock.
    """
    if key in _HELD:
        raise Abort(2, f"{label}({key!r}) is not re-entrant")
    deadline = time.monotonic() + (LOCK_CAP_MS / 1000.0)
    while True:
        token = _try_grant(lock)
        if token:
            break
        _evict_if_dead(lock)
        if time.monotonic() >= deadline:
            raise Abort(3, f"lock timeout: another process holds {lock}")
        time.sleep(LOCK_POLL_MS / 1000.0)
    _HELD[key] = token
    try:
        yield
    finally:
        _HELD.pop(key, None)
        try:
            # Release only what we still own — never remove a lock that evicted us.
            if (lock / "owner").read_text(encoding="utf-8").strip() == token:
                shutil.rmtree(lock, ignore_errors=True)
        except OSError:
            pass


@contextmanager
def ledger_lock(slug: str):
    with _lock_at(lock_path(slug), slug, "ledger_lock"):
        yield


@contextmanager
def dashboard_lock():
    """Lock for the cross-project dashboard page.

    Deliberately NOT `ledger_lock("dashboard")`: that would build
    `goal-ledger-dashboard.lock`, and since `_check_slug` only regex-validates,
    a real project named "Dashboard" would slugify onto the same path and share
    it. Every slug-derived lock name begins `goal-ledger-`, so a name that does
    not makes collision structurally impossible rather than merely unlikely.
    """
    with _lock_at(LOCK_DIR / DASHBOARD_LOCK_NAME, "dashboard", "dashboard_lock"):
        yield


def read_ledger(slug: str, project: str) -> dict:
    path = ledger_path(slug)
    if not path.exists():
        return {"schema": SCHEMA, "project": project, "updated": None, "goals": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Abort(2, f"ledger unreadable ({exc}); refusing to repair it automatically")
    if not isinstance(data, dict) or "goals" not in data:
        raise Abort(2, "ledger is not a goal ledger object")
    try:
        schema = int(data.get("schema", 0))
    except (TypeError, ValueError):
        raise Abort(2, f"ledger schema is not a number: {data.get('schema')!r}")
    if schema > SCHEMA:
        raise Abort(2, f"ledger schema {schema} is newer than this script ({SCHEMA}); migrate first")
    return data


def _atomic_write(path, lock, key: str, data: dict, what: str = "ledger") -> None:
    """Ownership-checked atomic replace, shared by every JSON store this CLI owns.

    MUST be called while holding the lock whose `_HELD` entry is `key`. The lock
    can be evicted underneath us by `_evict_if_dead`, and writing after that
    would clobber another holder's work — so the token is re-checked here,
    immediately before the write, not merely at acquire time.

    `what` is the noun in the three abort messages. It is a parameter and not a
    hardcoded string on purpose: exit 5's contract is worded per store
    ("ledger NOT modified"), and test_goals.py asserts on the exact text
    "ledger write failed". A generic noun here would turn that test red.
    """
    token = _HELD.get(key)
    try:
        on_disk = (lock / "owner").read_text(encoding="utf-8").strip()
    except OSError:
        on_disk = None
    if token is None or on_disk != token:
        raise Abort(5, f"lock lost before write; {what} NOT modified")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Abort(4, f"{what} directory unusable: {exc}")
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        # `UnicodeEncodeError` alongside `OSError`: a lone surrogate that
        # reaches here (e.g. through `apply_decisions`'s `create`/`retext`
        # text, which only `.strip()`s its input and never runs it through
        # `_has_disallowed_display_chars`) is valid Python but not valid
        # UTF-8, and `encoding="utf-8"` raises `UnicodeEncodeError` — a
        # `ValueError` subclass an `except OSError` alone never catches,
        # letting it escape as an uncaught exception instead of an `Abort`.
        try:
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except (OSError, UnicodeEncodeError) as exc:
            raise Abort(4, f"{what} write failed: {exc}")
    finally:
        # Cleanup runs regardless of which exception fired (or none): a
        # no-op after a successful `os.replace` already moved `tmp` away,
        # and `missing_ok=True` makes that no-op silent instead of a second
        # failure stacked on the first.
        tmp.unlink(missing_ok=True)


def write_ledger(slug: str, data: dict) -> None:
    """Atomic replace. MUST be called while holding ledger_lock(slug)."""
    _atomic_write(ledger_path(slug), lock_path(slug), slug, data, what="ledger")


VALID_STATUS = {"open", "done", "dropped", "parked"}
VALID_LEVEL = {"goal", "subgoal"}
# `done` was added 2026-08-19 (Pedro): the vocabulary previously had no word
# for "this goal was completed". The only adjacent tag, `gone`, means "no
# support anywhere in the session" and Step 4b maps it to `status: "dropped"`
# — so a finished goal could only be recorded as abandoned, which is the
# opposite of what happened, and `_NOT_VISIBLE` then hides it from the
# rendered page instead of ticking its checkbox. The `done` STATUS was always
# legal (`VALID_STATUS` above) and `render_markdown` has always drawn `[x]`
# from it; this tag is the missing proposal-side route to it.
VALID_TAG = {"same", "new", "changed", "gone", "done"}

# `origin` is provenance, and the only two shapes that mean anything:
# `extracted` (inferred from the session) and `plan:<path>` (traceable to
# an approved plan). `render_markdown` marks the first `(inferred)` and the
# second not, so a value outside this vocabulary would render a claim the
# ledger cannot support.
ORIGIN_EXTRACTED = "extracted"
ORIGIN_PLAN_PREFIX = "plan:"


def _origin_is_valid(value: object) -> bool:
    """True for the two legal `origin` shapes.

    The bare prefix `plan:` is REJECTED: `render_markdown` tests
    `origin.startswith("plan:")`, so `plan:` alone renders as
    plan-authored while naming no plan at all — a provenance mark
    asserting traceability it cannot support. The extractor only ever
    emits the prefix when `active_plan` is non-empty, so requiring a
    non-empty remainder rejects nothing legitimate.

    Shared by `parse_extraction` (where `origin` is REQUIRED) and
    `apply_decisions` (where it is validated only if present) so the two
    layers cannot drift into disagreeing about what a legal origin is.
    """
    if not isinstance(value, str):
        return False
    if value == ORIGIN_EXTRACTED:
        return True
    return (value.startswith(ORIGIN_PLAN_PREFIX)
            and len(value) > len(ORIGIN_PLAN_PREFIX))


# ── Status sets: what a status means to each surface ─────────────────────────
# Both are EXCLUSION sets, deliberately, rather than the equivalent-looking
# `status == "open"`. For a well-formed ledger the two forms agree (VALID_STATUS
# has exactly four members), but they diverge on a record carrying a status no
# rule recognises — a hand-edited ledger, a partial write, future schema drift.
# The exclusion form keeps that record VISIBLE; `== "open"` would silently drop
# it from every surface a human reads, which is the same defect as the one these
# sets exist to fix. Fail toward showing too much, never toward showing nothing.
#
# Per status, decided explicitly rather than falling out of one literal:
#   open    — live and visible. In flight.
#   parked  — neither. Autopark's own housekeeping transition; the goal went
#             stale, and `render_markdown` accounts for it with `Parked: N`.
#             It is announced separately in the run it happens (see `autopark`).
#   dropped — neither. Pedro answered `gone` at the gate and meant it. Keeping
#             it live is what made approving `gone` a no-op end to end: the
#             goal stayed in `owned` (so the report drew it as an open bullet)
#             and stayed in the extractor's `stored_goals` (so `gone` was
#             re-proposed every run, training Pedro to click through the one
#             gate this whole feature rests on). Accounted for by `Dropped: N`.
#   done    — NOT live, but still visible. This is the one status where the two
#             sets differ, and the difference is the point. Not live: it is
#             finished, so the extractor must stop being asked about it for the
#             same reason `dropped` must. Still visible: `- [x]` is precisely
#             what a checkbox list is FOR, and a goal Pedro completed is a goal
#             he wants to read back. Rendering it in place (rather than hoisting
#             it into a `## Completed` section) also keeps a completed parent
#             parenting its still-open sub-goals, which a separate section would
#             turn into orphans.
# `_is_live` implies `_is_visible`; `test_live_implies_visible` pins that.
_NOT_LIVE = frozenset({"parked", "dropped", "done"})
_NOT_VISIBLE = frozenset({"parked", "dropped"})


def _is_live(goal: dict) -> bool:
    """Is this goal still work in flight? Governs the WORKING SET: what
    `cmd_load` returns as `owned`, what the extractor is handed as
    `stored_goals`, and what counts toward the other-sessions live count."""
    return goal.get("status") not in _NOT_LIVE


def _is_visible(goal: dict) -> bool:
    """Does this goal appear in the derived mirror? Broader than `_is_live` by
    exactly `done` — see `_NOT_LIVE`'s note for why the two differ."""
    return goal.get("status") not in _NOT_VISIBLE


def new_goal_id(existing: list[dict]) -> str:
    taken = {goal.get("id") for goal in existing}
    seed_value = len(existing)
    while True:
        candidate = "g-%04x" % (seed_value & 0xFFFF)
        if candidate not in taken:
            return candidate
        seed_value += 1


_GOAL_STR_FIELDS = ("text", "status", "last_seen")


def _find(data: dict, goal_id: object, index: int) -> dict:
    """Locate a goal by id AND prove its record has the shape the apply phase
    will blindly index into (`text`/`status`/`last_seen` as str, `history` as
    list). Checking shape here, in the validation phase, means a malformed
    pre-existing record (hand-edited ledger, partial write, future schema
    drift) aborts cleanly at exit 2 before any mutation — instead of surviving
    staging and then raising a bare KeyError mid-apply, after an earlier
    decision in the same batch may already have committed."""
    for goal in data["goals"]:
        if goal.get("id") == goal_id:
            if any(not isinstance(goal.get(field), str) for field in _GOAL_STR_FIELDS) \
                    or not isinstance(goal.get("history"), list):
                raise Abort(2, f"decision[{index}]: ledger record for {goal_id!r} is malformed")
            return goal
    raise Abort(2, f"decision[{index}]: unknown goal id: {goal_id!r}")


def _check_ledger_shape(data: dict) -> None:
    """Validate the WHOLE goal collection before anything is applied.

    Staging only validates records a decision names, but `new_goal_id` sweeps every
    record and runs in the apply phase — so a malformed record nothing referenced
    raises after earlier decisions are already written. That leaves a half-applied
    decision set with an append-only history entry that cannot be rolled back, which
    is precisely what this function exists to prevent.
    """
    if not isinstance(data, dict):
        # `read_ledger` already guarantees a dict today, but this function is pure and
        # Task 6 calls it directly — relying on a guarantee made outside our own
        # boundary is the exact assumption the whole-collection precondition exists
        # to remove. Without this, a non-dict escapes as a bare AttributeError.
        raise Abort(2, "ledger is not an object")
    goals_list = data.get("goals")
    if not isinstance(goals_list, list):
        raise Abort(2, "ledger `goals` is not a list")
    for position, goal in enumerate(goals_list):
        if not isinstance(goal, dict):
            raise Abort(2, f"ledger goal[{position}] is not an object")
        if not isinstance(goal.get("id"), str):
            raise Abort(2, f"ledger goal[{position}] has a non-string id")


def _find_batch_cycle(edges: dict[int, int]) -> int | None:
    """Detect a cycle in a same-batch parent graph, or return None if there
    isn't one.

    NOW DEFENSE-IN-DEPTH, not a live check. Since the two-level invariant is
    enforced against batch-local parents (M-1), every edge added by the caller
    runs subgoal -> goal, and a `create` at `goal` level can never carry a
    batch-local parent at all — so the edge set is always a single layer deep
    and no cycle is constructible through `apply_decisions`. This is kept
    because the property it guards ("a cycle must abort rather than recurse
    forever") is one the level check happens to imply rather than one it
    states, and a future relaxation of that check would silently re-open it.
    It is under direct unit test for the same reason, so it does not rot into
    untested code merely because nothing currently reaches it.

    `edges` maps a `create` decision's index to the index of the OTHER
    `create` decision it names as its same-batch parent — at most one
    outgoing edge per node, since a decision has exactly one `parent`. That
    makes this a functional graph, not a general one, so a cheap walk
    suffices: from every node that has an edge, follow successors up to
    `len(edges) + 1` steps. A cycle-free chain can visit at most
    `len(edges)` distinct nodes before it runs out of `edges` entries and
    terminates (`edges.get(node)` returns `None`), so one extra step is
    always enough headroom to prove termination — a chain that is STILL
    going after that many steps must have revisited a node, which is only
    possible if it looped. No separate "visited" bookkeeping is needed to
    tell termination from a cycle; running out of steps IS the signal.

    A direct self-reference (a decision naming itself as its own parent)
    is caught earlier, before an edge for it is ever added here — see the
    caller — so every edge this function walks is between two distinct
    decisions, and the shortest possible cycle it can find has length 2.
    """
    limit = len(edges) + 1
    for start in edges:
        node = start
        for _ in range(limit):
            nxt = edges.get(node)
            if nxt is None:
                break
            node = nxt
        else:
            return start
    return None


def _collapse_text(raw: str) -> str:
    """Normalise a goal's text to one physical line as it ENTERS the ledger.

    `create`/`retext` used to only `.strip()`, so a newline survived into
    storage. `render_markdown` collapses it on the way out via `_render_line`,
    but the report builder renders a literal `- [ ] <text>` bullet with no
    sanitiser of its own and would emit a second, forged bullet. Fixing it at
    ingestion closes it once for every consumer, including ones that do not
    exist yet — which is the difference between this and adding a second
    render-time guard.

    Reachability is narrow, not zero: `_has_disallowed_display_chars` rejects
    `\\n` as a `Cc` character at parse time, so the extractor path is already
    closed. The routes that remain are Pedro's own free-text correction at the
    gate (which is trusted input, and legitimately may contain a stray
    newline) and a hand-edited ledger.
    """
    return " ".join(raw.split())


def apply_decisions(data: dict, session: str, decisions: list[dict], now_iso: str) -> int:
    """Validate every decision, then apply. All-or-nothing: a single bad decision
    aborts with exit 2 and leaves `data` byte-identical.

    Every value the apply phase will use is validated and coerced ONCE, here in
    the validation phase, and the validated value is what gets staged and
    applied — never re-derived from the raw decision or re-trusted from an
    existing ledger record in the apply phase. A validate/apply split that
    re-reads a raw or unverified field in apply can let something that looks
    fine at a glance (a non-string `text`, a non-hashable `status`, a
    malformed pre-existing goal record) pass validation and then blow up with
    a bare exception once mutation is already under way — that is exactly the
    partial-application bug this function exists to prevent. The staging loop
    below only proves safe the records a decision actually names, though, and
    `new_goal_id` (apply phase) sweeps the WHOLE collection — so what makes
    the apply loop safe is the `_check_ledger_shape` precondition immediately
    below, which validates every record up front, before staging even starts.

    Note: a `create` decision still cannot be referenced by a later RETEXT,
    STATUS, or SEEN decision in the SAME batch (e.g.
    `[create, status(id=<the just-created id>)]` aborts with "unknown goal
    id") — `new_goal_id` only assigns an id during apply, and `_find` runs
    during staging, before any `create` has been applied. `retext`/`status`/
    `seen` only ever resolve against `data["goals"]` as it already exists on
    disk, never against a sibling decision.

    A `create` decision's OWN `parent`, however, may name a goal being
    created earlier or later in the SAME batch — this is the whole point of
    the feature: on a project's first-ever run the ledger is empty, so every
    goal a session proposes is new, and a sub-goal can only ever attach to
    its parent if that parent-to-be, still unwritten, can be named. A
    `create` decision opts in to being nameable this way by carrying an
    optional `id` field of its own — NOT a real ledger id (the ledger only
    ever assigns those, via `new_goal_id`, once the decision is actually
    applied), just a token unique to this one batch, scoped to this one
    call. A subgoal `create`'s `parent` is resolved against those batch-
    local `id`s FIRST; only a `parent` that matches none of them falls
    through to `_find` against the real ledger, exactly as before this
    feature existed. Every batch-local `id` is checked up front, before any
    `parent` is resolved against it, against two things: collision with a
    REAL ledger goal id (rejected outright — a batch-local token must never
    be mistaken for a live id) and duplication within the same batch
    (rejected outright — an ambiguous target is not a valid one). That
    up-front pass is also what makes ORDER irrelevant: a subgoal `create`
    may name a parent `create` appearing anywhere else in the batch, before
    or after it, because every batch-local `id` is already known before any
    `parent` is resolved. A `create` naming ITSELF as its own parent, or a
    longer cycle among several same-batch `create`s, aborts instead of
    looping or silently producing a corrupt tree — see `_find_batch_cycle`.
    The real, ledger-assigned parent id is substituted in during the apply
    phase (see `_apply_create` below), never the batch-local token — nothing
    batch-local is ever written to the ledger.
    """
    if not isinstance(session, str):
        raise Abort(2, "session must be a string")
    if not isinstance(now_iso, str):
        raise Abort(2, "now_iso must be a string")
    if not isinstance(decisions, list):
        raise Abort(2, "decisions is not a list")
    _check_ledger_shape(data)

    # Pass 1 of 2 for `create`: collect every batch-local `id` BEFORE any
    # `parent` is resolved against them, so a subgoal `create` may name a
    # parent `create` appearing anywhere else in the batch — earlier or
    # later — not just one already staged. Skips anything that isn't a
    # `{"op": "create", ...}` dict without raising; the main loop below is
    # what still rejects a malformed decision, with its own, more specific
    # message pointing at the actual problem.
    batch_ids: dict[str, int] = {}
    for i, decision in enumerate(decisions):
        if not isinstance(decision, dict) or decision.get("op") != "create":
            continue
        batch_id = decision.get("id")
        if batch_id is None:
            continue
        if not isinstance(batch_id, str) or not batch_id:
            raise Abort(2, f"decision[{i}]: create: id must be a non-empty string when present")
        if batch_id in batch_ids:
            raise Abort(2, f"decision[{i}]: create: id {batch_id!r} duplicates "
                           f"decision[{batch_ids[batch_id]}]'s id")
        if any(isinstance(g, dict) and g.get("id") == batch_id for g in data["goals"]):
            raise Abort(2, f"decision[{i}]: create: id {batch_id!r} collides with "
                           "an existing ledger goal id")
        batch_ids[batch_id] = i

    # Same-batch parent edges collected as the main loop resolves each
    # subgoal `create`'s `parent` — checked for cycles once staging finishes
    # (see below), never while it's still in progress: a cycle can only be
    # told apart from a long-but-valid chain once the WHOLE batch-local
    # graph is known, exactly the same reason `_check_ledger_shape` runs the
    # whole collection up front instead of checking as it goes.
    edges: dict[int, int] = {}

    staged: list[tuple] = []
    for i, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            raise Abort(2, f"decision[{i}]: not an object: {decision!r}")
        op = decision.get("op")
        if op == "create":
            level = decision.get("level")
            if not isinstance(level, str) or level not in VALID_LEVEL:
                raise Abort(2, f"decision[{i}]: create: bad level {level!r}")
            parent = decision.get("parent")
            batch_parent_idx: int | None = None
            if level == "subgoal":
                if not parent:
                    raise Abort(2, f"decision[{i}]: create: subgoal requires a parent")
                if not isinstance(parent, str):
                    # A dict/list `parent` would raise a bare TypeError on the
                    # `parent in batch_ids` membership check just below —
                    # unhashable types are never valid dict keys. `_find`
                    # itself only ever compares with `==`, which never raises
                    # for any type, so this check exists only to protect the
                    # membership test that's new in this function.
                    raise Abort(2, f"decision[{i}]: create: parent must be a string")
                if parent in batch_ids:
                    if batch_ids[parent] == i:
                        raise Abort(2, f"decision[{i}]: create: subgoal cannot "
                                       "name itself as its own parent")
                    batch_parent_idx = batch_ids[parent]
                    # The two-level invariant is enforced against real ledger
                    # parents (a subgoal's `_find`ed parent must be a goal)
                    # but was NOT enforced when the parent is a batch-local
                    # token — so `goal -> subgoal -> subgoal` applied cleanly
                    # and the grandchild then rendered under `## Unparented
                    # sub-goals`, a heading that is simply false: its parent
                    # exists and is not parked. Reject rather than mislabel.
                    parent_decision = decisions[batch_parent_idx]
                    if parent_decision.get("level") != "goal":
                        raise Abort(2, f"decision[{i}]: create: subgoal's same-batch "
                                       f"parent {parent!r} is not a top-level goal "
                                       "(two levels only)")
                    edges[i] = batch_parent_idx
                else:
                    _find(data, parent, i)
            elif parent is not None:
                raise Abort(2, f"decision[{i}]: create: a goal must have parent null")
            raw_text = decision.get("text", "")
            if not isinstance(raw_text, str):
                raise Abort(2, f"decision[{i}]: create: text must be a string")
            text = _collapse_text(raw_text)
            if not text:
                raise Abort(2, f"decision[{i}]: create: empty text")
            # Validated only if PRESENT, deliberately not required: the
            # apply path defaults an absent `origin` to `extracted` (see
            # `_apply_create`), and a decision set that omits it is
            # legitimate. What must not pass is a value outside the
            # vocabulary — `render_markdown` would render a provenance
            # mark the ledger cannot support. Same rule the parser
            # enforces one layer up, via the same helper.
            if "origin" in decision and not _origin_is_valid(decision["origin"]):
                raise Abort(2, f"decision[{i}]: create: bad origin "
                               f"{_bounded_repr(str(decision['origin']))} (expected "
                               f"{ORIGIN_EXTRACTED!r} or {ORIGIN_PLAN_PREFIX + '<path>'!r})")
            staged.append(("create", decision, text, batch_parent_idx))
        elif op == "retext":
            goal = _find(data, decision.get("id"), i)
            raw_text = decision.get("text", "")
            if not isinstance(raw_text, str):
                raise Abort(2, f"decision[{i}]: retext: text must be a string")
            text = _collapse_text(raw_text)
            if not text:
                raise Abort(2, f"decision[{i}]: retext: empty text")
            staged.append(("retext", decision, goal, text))
        elif op == "status":
            goal = _find(data, decision.get("id"), i)
            status = decision.get("status")
            if not isinstance(status, str) or status not in VALID_STATUS:
                raise Abort(2, f"decision[{i}]: status: bad status {status!r}")
            staged.append(("status", decision, goal))
        elif op == "seen":
            goal = _find(data, decision.get("id"), i)
            staged.append(("seen", decision, goal))
        else:
            raise Abort(2, f"decision[{i}]: unknown op: {op!r}")

    cycle_idx = _find_batch_cycle(edges)
    if cycle_idx is not None:
        raise Abort(2, f"decision[{cycle_idx}]: create: same-batch parent "
                       "references form a cycle")

    # Real, ledger-assigned ids for `create` entries that declared a
    # batch-local `id` — populated by `_apply_create` as each one is
    # actually applied, keyed by STAGED index (== decision index; every
    # validate-loop branch above appends exactly one staged entry per
    # decision, never zero or two, so the two index spaces coincide).
    assigned_ids: dict[int, str] = {}
    # Guards `_apply_create`'s recursion against re-entering an index still
    # partway through its own call — see that function's docstring.
    resolving: set[int] = set()

    def _apply_create(idx: int) -> str:
        """Apply staged `create` entry `idx`, first applying its same-batch
        parent (if it has one) so the real parent id exists to substitute
        in — recursing at most as many times as `_find_batch_cycle` already
        proved this batch-local graph is deep, since that check already ran
        and found no cycle. `assigned_ids` doubles as memoization: a parent
        `create` reached first via a child's recursion is not re-applied
        when the main loop below reaches that same index on its own — the
        `if idx in assigned_ids: return` guard immediately below is what
        makes calling this twice for the same `idx` safe.

        Increments the enclosing `applied` counter itself, via `nonlocal`,
        rather than leaving that to each caller: a `create` reached first
        through a child's recursion and a `create` reached directly by the
        main loop must both count exactly once, and the memoization guard
        above means only the FIRST call for a given `idx` ever reaches this
        far — counting here, at the one place that actually just created a
        goal, is what keeps that true regardless of which caller triggers it.

        `resolving` is a defensive guard against re-entering an index still
        partway through this call — should be unreachable given the cycle
        check above already ran clean, but a silent infinite recursion
        (Python's own `RecursionError`, not an `Abort`) is exactly the kind
        of contract violation this module's exit-code discipline exists to
        rule out, so it costs nothing to make the "this should never
        happen" case fail loud instead of fail silent-then-crash.
        """
        nonlocal applied
        if idx in assigned_ids:
            return assigned_ids[idx]
        if idx in resolving:
            raise Abort(2, f"decision[{idx}]: create: internal error — same-batch "
                           "parent cycle survived validation")
        resolving.add(idx)
        _, decision, text, batch_parent_idx = staged[idx]
        if batch_parent_idx is not None:
            parent_id: str | None = _apply_create(batch_parent_idx)
        else:
            parent_id = decision.get("parent")
        new_id = new_goal_id(data["goals"])
        data["goals"].append({
            "id": new_id,
            "text": text,
            "level": decision["level"],
            "parent": parent_id,
            "status": "open",
            "owner_session": session,
            "origin": decision.get("origin", "extracted"),
            "created": now_iso,
            "last_seen": now_iso,
            "history": [],
        })
        assigned_ids[idx] = new_id
        resolving.discard(idx)
        applied += 1
        return new_id

    applied = 0
    for idx, entry in enumerate(staged):
        op = entry[0]
        if op == "create":
            # `_apply_create` itself increments `applied` and is a no-op on
            # an `idx` already applied (via an earlier sibling's recursion)
            # — see its docstring. No increment belongs here.
            _apply_create(idx)
            continue
        if op == "retext":
            decision, goal, new_text = entry[1], entry[2], entry[3]
            if new_text == goal["text"]:
                continue
            goal["history"].append({"ts": now_iso, "from": goal["text"], "to": new_text,
                                    "reason": "approved-change", "session": session})
            goal["text"] = new_text
        else:
            decision, goal = entry[1], entry[2]
            if op == "status":
                if decision["status"] == goal["status"]:
                    continue
                goal["history"].append({"ts": now_iso, "from": goal["status"],
                                        "to": decision["status"], "reason": "approved-change",
                                        "session": session})
                goal["status"] = decision["status"]
            elif op == "seen":
                if goal["last_seen"] == now_iso:
                    continue
                goal["last_seen"] = now_iso
        applied += 1
    data["updated"] = now_iso
    return applied


def _parse_iso(value: str) -> float:
    """Parse an ISO-8601 timestamp to a UTC epoch float.

    A bare/naive timestamp (no offset, no "Z") is assumed UTC — that is the
    only case tzinfo may be forced onto the result. A timestamp that already
    carries an explicit offset (e.g. "+05:00") must keep it: blindly replacing
    it with UTC would silently shift the instant instead of preserving it.
    Both shapes reach this function, since `last_seen` is read straight off
    disk, where a hand-edited or older ledger can hold either.
    """
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def session_live(uuid: str, now_epoch: float) -> bool:
    """A session is live when its transcript exists and was touched recently.
    Goals owned by a dead session become claimable by whoever runs next.

    `uuid` is `owner_session` off a hand-editable ledger record, so it is
    untrusted: reject anything that isn't a plain session-id token before it
    reaches glob(). Confirmed empirically that pathlib does NOT stop at
    PROJECTS_DIR here — a "/" (e.g. "../evil") walks the pattern to a file
    outside the intended `PROJECTS_DIR/<project>/<uuid>.jsonl` shape, and a
    bare "*" turns the lookup into "any transcript matches", making a garbage
    owner_session register as always-live.

    This is a heuristic, not a true liveness check: a session that's merely
    idle past LIVE_SESSION_HOURS (closed laptop, a long pause) reads as dead
    too, and its goals become claimable by whoever runs next.
    """
    # fullmatch, not match: `$` matches before a trailing "\n" too, so `.match()`
    # would let "sess-b\n" through as if it were the plain token "sess-b".
    if not isinstance(uuid, str) or not _SESSION_ID_RE.fullmatch(uuid):
        return False
    for candidate in PROJECTS_DIR.glob(f"*/{uuid}.jsonl"):
        try:
            age_hours = (now_epoch - candidate.stat().st_mtime) / 3600.0
        except OSError:
            continue
        if age_hours < LIVE_SESSION_HOURS:
            return True
    return False


def _owner_key(owner: object) -> str | None:
    """Collapse any non-string `owner_session` (malformed ledger data — see
    session_live's own guard) to a single shared, hashable cache key. Every
    non-string value gets the same `session_live` answer (False), so sharing
    the key is correct, not just convenient."""
    return owner if isinstance(owner, str) else None


def autopark(data: dict, now_iso: str) -> list[str]:
    """Park goals untouched for PARK_AFTER_DAYS. Nothing is ever deleted.

    Records the park in the goal's `history`, reversing the original "this is
    housekeeping, not a user decision, so it writes no history" call. The
    premise was right and the conclusion wrong: a state change nothing records
    is exactly the silent decay this whole feature exists to fight, and it is
    worse here than elsewhere because a parked goal simultaneously drops out
    of the report AND out of the extractor's `stored_goals`, so the ledger
    afterwards asserts that a goal IS parked while nothing on disk says when
    or why. `reason: "autopark-14d"` is what keeps it distinguishable from an
    approved change, and `session` is null because no session decided it.

    The history append is best-effort, deliberately: `_check_ledger_shape`
    only guarantees `id` is a string, so a hand-edited record may carry no
    `history` list at all, and `goal["history"].append(...)` on one would
    raise a bare KeyError out of a function documented to fail only via
    `Abort`. The park is the durable half and must survive a malformed
    neighbouring field; a non-list `history` is left exactly as found rather
    than silently rewritten, since repairing a ledger automatically is
    precisely what `read_ledger` refuses to do.

    Validates the whole collection shape up front — a narrower reason than
    `apply_decisions`'s: nothing here persists until this function returns
    cleanly, so there's no partial-write corruption to prevent, only a bare
    TypeError/AttributeError (iterating `data["goals"]` and indexing
    `goal["id"]` directly against a malformed record — hand-edited ledger,
    partial write) that would otherwise escape the documented Abort(2)
    exit-code contract instead of failing clean.
    """
    _check_ledger_shape(data)
    if not isinstance(now_iso, str):
        raise Abort(2, "now_iso must be a string")
    try:
        cutoff = _parse_iso(now_iso) - PARK_AFTER_DAYS * 86400
    except ValueError as exc:
        raise Abort(2, f"now_iso is not a valid ISO-8601 timestamp: {exc}")
    parked: list[str] = []
    for goal in data["goals"]:
        if goal.get("status") != "open":
            continue
        try:
            last_seen = _parse_iso(goal["last_seen"])
        except (KeyError, AttributeError, ValueError):
            # Missing, or present but not a well-formed ISO string (shape
            # checking above only guarantees `id`, not `last_seen`'s type).
            continue
        if last_seen < cutoff:
            if isinstance(goal.get("history"), list):
                goal["history"].append({"ts": now_iso, "from": goal["status"],
                                        "to": "parked", "reason": "autopark-14d",
                                        "session": None})
            goal["status"] = "parked"
            parked.append(goal["id"])
    return parked


def cmd_load(project: str, session: str, read_only: bool = False) -> dict:
    """Split a project's open goals into what this session owns, what it could
    claim from a dead session, and a count of everything else ("others" is
    always a count, never a listing).

    `read_only=True` makes this function incapable of writing. That matters
    because `cmd_load` is otherwise a WRITE path whenever anything is due to
    be parked, and `--dry-run` is mandated to call it — so a run whose entire
    purpose is to inspect state without changing it could silently and
    permanently park a live goal, and (since a park is only announced by the
    run that performs it) say nothing about having done so. The guarantee is
    structural rather than procedural: under this flag the `write_ledger` call
    below is not reached, instead of `load` being listed in prose as a command
    a dry run must avoid — a list is a thing to forget, which is exactly how
    criteria `c6` and `c9` both came to enumerate what a dry run must not do
    and both miss this.

    `autopark` still RUNS in read-only mode, against the in-memory `data`
    only. Skipping it entirely would report an `owned` set a real run would
    never produce (it would still contain the goals about to be parked); this
    way a dry run previews the same split the real run would return, and the
    single difference between the two modes is whether the result is
    persisted. The pending parks come back as `would_park`, and `parked_now`
    is empty — truthfully, since nothing was parked. The two keys are always
    both present so the shape does not shift between modes.

    The lock is taken in read-only mode too. Nothing is written under it, so
    it is not needed for correctness (`cmd_render` reads without one, and
    documents why), but keeping one concurrency posture for this function is
    worth more than the contention a second one would save.

    `project`/`session` are validated as non-empty strings up front, mirroring
    `apply_decisions`. Without this, a non-string `project` raises a bare
    AttributeError out of `slugify` instead of a documented Abort — and a
    `session` of `None` would not crash at all, it would silently corrupt the
    result: `_check_ledger_shape` permits a goal record with a null
    `owner_session`, so `g.get("owner_session") == None` is true for it, and
    an unvalidated caller bug would put those goals in `owned` with no error.

    The lock is held only for the read + autopark-mutate + write. The
    owned/claimable/others split below runs entirely against the `data`
    already captured in memory during that critical section — it never
    re-reads the ledger — so computing it after the lock is released is safe,
    and keeps the critical section short: `session_live` globs PROJECTS_DIR,
    which can be slow with many sessions, and there is no reason to make every
    other lock waiter sit through that. `session_live` is also called at most
    once per distinct `owner_session`, not once per goal — a project with many
    goals owned by a handful of dead sessions would otherwise turn into that
    many redundant directory scans.

    `claimable` is advisory only: this module has no claim/write path that
    atomically transfers ownership, so two callers can compute the same
    claimable set concurrently. Whoever adds a claim operation must
    re-validate ownership under `ledger_lock` at claim time, not trust this
    snapshot.

    A goal with a missing or null `owner_session` is intentionally claimable:
    `session_live(None, ...)` returns False (it isn't a valid session-id
    token), so "nobody has ever claimed this" and "the session that claimed
    this is gone" both are claimable — that is a routing decision, not a
    claim the two states are indistinguishable. They aren't: one is
    unambiguously unclaimed, the other rests on a 24-hour mtime heuristic
    this module already treats as fuzzy (an idle-but-alive session reads as
    dead too — see `session_live`'s own docstring). Nothing is lost for a
    caller that cares about the difference: each returned goal dict still
    carries its own `owner_session`.
    """
    if not isinstance(project, str) or not project:
        raise Abort(2, "project must be a non-empty string")
    if not isinstance(session, str) or not session:
        raise Abort(2, "session must be a non-empty string")
    slug = slugify(project)
    now_iso = _now_iso()
    now_epoch = time.time()
    with ledger_lock(slug):
        data = read_ledger(slug, project)
        parked = autopark(data, now_iso)
        if parked and not read_only:
            data["updated"] = now_iso
            write_ledger(slug, data)
    # A parked goal drops out of `live`, so it reaches neither `owned` nor
    # `claimable` — a consumer handed bare ids would have no table to resolve
    # them against and could only print "Parked this run: g-0003". Resolve to
    # `{id, text}` here, where `data` is still in hand. `_render_line` is
    # applied because this is a new surface reaching the report builder, which
    # has no sanitiser of its own — the same reason `render_markdown` routes
    # every value through it rather than trusting the ledger.
    by_id = {g.get("id"): g for g in data["goals"] if isinstance(g, dict)}
    def _named(ids: list[str]) -> list[dict]:
        return [{"id": i,
                 "text": _render_line(by_id.get(i, {}).get("text", "(untitled goal)"))}
                for i in ids]

    # `parked_now` is what this call actually persisted; `would_park` is what
    # a non-read-only call would have. Exactly one is ever non-empty, so a
    # consumer that reports "parked this run" can trust `parked_now` without
    # also having to know which mode produced it.
    parked_now = [] if read_only else _named(parked)
    would_park = _named(parked) if read_only else []
    live = [g for g in data["goals"] if _is_live(g)]
    owned = [g for g in live if g.get("owner_session") == session]
    # `done` goals this session owns, returned on their own key rather than
    # inside `owned`. They must leave the working set (the extractor has to
    # stop proposing `gone` for a goal that is finished) but must NOT leave
    # the report — a goal completed this run vanishing from the report is the
    # same "no surface reflects it" defect as the one `_is_live` fixes.
    completed = [g for g in data["goals"]
                 if g.get("owner_session") == session and g.get("status") == "done"]
    unowned_by_us = [g for g in live if g.get("owner_session") != session]
    # `owner_session` is unchecked by `_check_ledger_shape` (it only guarantees
    # `id`), so a malformed ledger can hold a non-hashable value there (a list,
    # a dict). Cache on `_owner_key`, not the raw value, or that goal's dict
    # lookup below raises a bare TypeError instead of the documented contract.
    live_by_owner: dict = {}
    for g in unowned_by_us:
        key = _owner_key(g.get("owner_session"))
        if key not in live_by_owner:
            live_by_owner[key] = session_live(g.get("owner_session"), now_epoch)
    claimable = [g for g in unowned_by_us
                 if not live_by_owner[_owner_key(g.get("owner_session"))]]
    # A set of ids assumes ids are unique, which `_check_ledger_shape` never
    # guarantees (it only checks that `id` is a string). Two goals sharing an
    # id would collapse to one entry in `claimable_ids`, undercounting
    # `others` for its duplicate. Arithmetic on the two list lengths has no
    # such assumption.
    others = len(unowned_by_us) - len(claimable)
    return {"schema": SCHEMA, "project": project, "slug": slug,
            "owned": owned, "completed": completed, "claimable": claimable,
            "others": others, "parked_now": parked_now, "would_park": would_park}


MAX_GOALS = 6
MAX_SUBGOALS = 12
MAX_PROPOSALS = MAX_GOALS + MAX_SUBGOALS
MAX_TEXT_LENGTH = 200_000  # generous upper bound for a goal-extractor's entire output
MAX_VALUE_LENGTH = 2000    # generous upper bound for any single field value
# Generous upper bounds on the JSON payload's own structure, checked before
# json.loads (depth) and right after it (array length) — see
# `_check_json_depth`/`_check_json_bounds` for why each needs its own guard
# instead of trusting MAX_TEXT_LENGTH to cover both implicitly.
# A legitimate payload never nests past 3 bracket-opens (top object ->
# "proposed" array -> one proposal object) — 20 is ~6x that, headroom for
# a future field of one more nesting level without ever being a workable
# depth for a hostile payload trying to blow the recursion limit instead.
MAX_JSON_DEPTH = 20
# A legitimate `proposed` array never exceeds MAX_PROPOSALS (18) items —
# 1000 is a structural ceiling one order of magnitude above that, checked
# generically on every array in the payload (not just `proposed`) before
# any semantic count check gets a chance to produce its friendlier abort.
MAX_JSON_ARRAY_LENGTH = 1000

_OPEN_MARKER = "<<<GOAL_EXTRACTION>>>"
_END_MARKER = "<<<END>>>"
# Tolerates exactly one well-formed markdown code fence around the JSON
# object — models add one unprompted even when not asked to, and rejecting
# an otherwise-well-formed payload over a fence is a false rejection that
# costs the user a run's goal update. A malformed or absent fence is left
# untouched and falls through to json.loads for its own, more specific error.
# No `\n` is required immediately before the closing fence: a real model
# just as often emits ```json\n{...}``` (closing backticks right after the
# JSON's own trailing brace, no blank line) as it does one with the
# closing fence on its own line — `_strip_code_fence` strips either shape
# the same way. Trailing prose after the closing fence ("```\nthanks!") is
# tolerated too, for the same false-rejection reason — see `_find_fence_close`.
_CODE_FENCE_OPEN_RE = re.compile(r"^```[^\n]*\n")

# The eight fields the contract requires. Seven of them are unchanged in
# meaning since the line format; `origin` was added 2026-08-13, closing a
# contract break that made the whole feature non-functional: the extractor
# agent had always been INSTRUCTED to emit `origin` (`agents/goal-extractor.md`
# Rules), `apply_decisions` had always accepted it, and `render_markdown` had
# always USED it — only this tuple omitted it, so every real extraction was
# rejected here as carrying "an unrecognized field: 'origin'" and no goal
# could ever reach the ledger.
#
# Unlike the retired line format's `_REQUIRED_FIELDS`, this tuple carries no
# security weight of its own — a JSON object's keys can't be reordered to
# smuggle structure the way an unescaped line could, so this is only
# iteration order for deterministic abort messages (see `_validate_proposal`).
# The all-fields-REQUIRED rule below does carry security weight, and gains
# rather than loses it here: because the genuine `origin` key is now always
# present, an injected one is a duplicate key the parser rejects outright,
# not a lone forged value that satisfies a completeness check.
_FIELD_NAMES = ("id", "level", "parent", "tag", "was", "text", "evidence", "origin")
_KNOWN_FIELDS = frozenset(_FIELD_NAMES)
# `null` is legal ONLY on these two — everywhere else a JSON `null` is a
# type-confusion abort like any other wrong type (blind spot: JSON carries
# real types the line format never could, so every field's type must be
# checked explicitly instead of assumed to be a string).
_NULLABLE_FIELDS = frozenset({"parent", "was"})

# The payload object itself gets the same unrecognized-key rejection
# `_validate_proposal` applies to each proposal — deliberately, not by
# oversight: an extractor that hallucinates or renames a top-level field
# (e.g. "proposals" for "proposed") fails loud here instead of silently
# proceeding with an empty/absent `proposed` list, which would otherwise
# surface only as the much less specific "contained no proposals" abort
# several checks later, with no hint of the typo that actually caused it.
_TOP_LEVEL_KNOWN_FIELDS = frozenset({"proposed"})

# Explicit BiDi directional-override and isolate characters — the specific
# threat this module actually needs to stop, not the whole Unicode "format"
# category: U+200C/U+200D (ZWNJ/ZWJ) are also category Cf but are ordinary
# script/emoji-joining characters, not display attacks, and stay allowed.
#
# The three MARKS below (LRM/RLM/ALM) were originally excluded as "weaker" —
# they carry no scope and no nesting, unlike the embeddings and isolates. That
# is true and does not matter: they are the same class of invisible
# directional character, they were verified to pass both ingest and render,
# and the one file this guard protects exists to be trusted at a glance.
# Blocking them costs one line and no legitimate goal text.
_BIDI_CONTROL_CHARS = frozenset(
    "‪‫‬‭‮"   # LRE RLE PDF LRO RLO
    "⁦⁧⁨⁩"          # LRI RLI FSI PDI
    "‎‏؜"                # LRM RLM ALM
)
# A handful of stacked diacritics is normal orthography; dozens on one base
# character ("zalgo" text) is a display attack. This caps legitimate use
# generously rather than trying to pin an exact "normal" count.
_MAX_COMBINING_MARKS = 4
_ABORT_PREVIEW_CHARS = 120


def _has_disallowed_display_chars(value: str) -> bool:
    """True if `value` could misrepresent itself once the Task 8
    AskUserQuestion gate displays it verbatim to a human — or crash
    something downstream that assumes a valid Unicode string.

    Deliberately narrower than `str.isprintable()`: that stdlib check also
    rejects a plain tab or a non-breaking space (category Zs) — both
    ordinary content an extractor may legitimately quote (a tab-indented
    snippet, text copied from a web page) — while saying nothing at all
    about stacked combining marks, which `isprintable()` treats as
    printable no matter how many pile onto one base character.

    Category `Cs` (surrogate) is rejected outright, unconditionally — the
    JSON contract is what makes this reachable at all: a `\\uD800`-range
    escape with no matching low-surrogate partner decodes, via
    `json.loads`, into a genuine lone-surrogate Python character. A
    correctly PAIRED surrogate escape (a real emoji, any astral-plane
    character) decodes into one ordinary, non-`Cs` code point and is
    unaffected — only an unpaired half survives as `Cs`, and that is never
    valid standalone text. Left unchecked, a lone surrogate sails through
    every other guard here, gets accepted as a proposal value, and later
    blows up `write_ledger`'s `encoding="utf-8"` write with an uncaught
    `UnicodeEncodeError` — not an `OSError`, so it escapes the surrounding
    `except OSError` and breaks the module's documented every-failure-is-
    an-`Abort` contract. The retired line format could never deliver a
    lone surrogate here in the first place (its values were literal
    characters read straight off a line, with no escape syntax of its
    own), so this is a new attack surface the JSON contract itself opens,
    not a pre-existing gap the format change merely inherited.
    """
    combining_run = 0
    for ch in value:
        if ch in _BIDI_CONTROL_CHARS:
            return True
        category = unicodedata.category(ch)
        if category in ("Cc", "Cs") and ch != "\t":
            return True
        if category in ("Mn", "Me"):
            combining_run += 1
            if combining_run > _MAX_COMBINING_MARKS:
                return True
        else:
            combining_run = 0
    return False


def _bounded_repr(text: str) -> str:
    """`repr()` a value for interpolation into a one-line Abort message,
    bounded on the OUTPUT length, not the input length. `repr()` escapes
    every non-printable byte as a 4-character `\\xNN` sequence, so
    truncating the raw string first still lets a run of control bytes blow
    the message far past its intended bound — bound the string actually
    being interpolated instead.
    """
    shown = repr(text)
    if len(shown) > _ABORT_PREVIEW_CHARS:
        shown = shown[:_ABORT_PREVIEW_CHARS] + "...'"
    return shown


def _find_fence_close(body: str) -> int:
    """Return the index in `body` of the first ` ``` ` that sits OUTSIDE any
    JSON string literal, or -1 if none exists.

    String/escape tracking mirrors `_check_json_depth`'s scan: a literal
    "```" that happens to sit INSIDE a JSON string value (a proposal
    quoting a code snippet, say) is just three ordinary characters and must
    never be mistaken for the fence boundary — treating it as the close
    would truncate the JSON mid-string. Only a "```" the decoder would
    never see as string content — because it isn't inside an open quote —
    can be the real close.

    Scanning for the FIRST such occurrence (not requiring it to be the end
    of `body`) is what lets trailing prose after the fence ("```\\nthanks!")
    fall through as ordinary, ignored tail content instead of making the
    whole fence look unterminated.
    """
    in_string = False
    escaped = False
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
        elif ch == "`" and body[i:i + 3] == "```":
            return i
        i += 1
    return -1


def _strip_code_fence(payload: str) -> str:
    """Strip exactly one well-formed markdown code fence (```` ```json `` … `` ``` ````)
    wrapped around the JSON object. Returns the payload UNCHANGED (just
    whitespace-trimmed) when no opening fence is present, or when an
    opening fence is present but no closing fence can be found — either
    way the result still goes to `json.loads`, and `parse_extraction`
    recognizes the still-fenced text on decode failure to give a fence-
    specific error rather than a confusing "not valid JSON" pointed at a
    backtick (see the `payload_text.startswith("```")` check there).

    Content AFTER the closing fence (trailing prose — "thanks!", "let me
    know if you want changes") is tolerated and discarded: `_find_fence_close`
    locates the first close, not the last, so it no longer matters whether
    anything follows it.
    """
    stripped = payload.strip()
    open_match = _CODE_FENCE_OPEN_RE.match(stripped)
    if not open_match:
        return stripped
    body = stripped[open_match.end():]
    close_idx = _find_fence_close(body)
    if close_idx == -1:
        return stripped
    return body[:close_idx].strip()


class _DuplicateKeyTracker:
    """`object_pairs_hook` for `json.loads`. Plain `json.loads` silently
    keeps the LAST occurrence of a repeated key — `{"tag": "same", "tag":
    "changed"}` parses clean and yields `"changed"` with no sign the input
    was ambiguous. That is a silent-precedence rule sitting inside a trust
    boundary, so every JSON object in the payload (not just a proposal's
    own object — this hook fires for the top-level object too) must reject
    a duplicate key outright rather than pick a winner.

    Never raises from inside the hook — only records the FIRST duplicate
    found (`self.found = (call_index, key)`) and lets parsing finish.
    json's decoder calls this once per JSON object it closes, bottom-up,
    and the outer/top-level object's own call always comes LAST (it closes
    only after everything nested inside it already has) — so whether a
    given call belongs to a proposal or to the top-level object can only be
    told once the total call count is known, which isn't until `json.loads`
    returns. `parse_extraction` turns `self.found` into a located `Abort`
    right after, using the same `proposed[{index}]` convention
    `_validate_proposal` already established for everything else in this
    module: call index N is `proposed[N]` for every call except the last,
    and the last call is always the top-level object.

    That mapping is exact for this module's own schema — a flat top-level
    object holding one `proposed` array of flat objects, nothing else ever
    nested — and only approximate if a payload nests an object somewhere
    that schema never expects (e.g. a `text` field holding `{"a": 1, "a":
    2}` instead of a string). In that case the locator names the enclosing
    proposal (`proposed[N]`), not the nested field the duplicate actually
    lives in (`proposed[N].text`) — the duplicate-key abort fires right
    after `json.loads` returns, before `_validate_proposal`'s field-by-
    field type check ever runs, so nothing downstream corrects or refines
    this locator's guess. The abort still fires, still names the right
    proposal and the right key, and the payload is still rejected either
    way — only the precision of "which nested position" is approximate for
    this one unexpected-nesting case.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.found: tuple[int, str] | None = None

    def __call__(self, pairs: list[tuple[str, object]]) -> dict:
        index = self.calls
        self.calls += 1
        seen: dict = {}
        for key, value in pairs:
            if key in seen and self.found is None:
                self.found = (index, key)
            seen[key] = value
        return seen


def _check_json_depth(payload: str) -> None:
    """Reject a payload whose bracket nesting exceeds `MAX_JSON_DEPTH`
    BEFORE `json.loads` ever sees it. json's C-accelerated decoder recurses
    once per nesting level it descends into; a pathologically deep payload
    (trivial to construct — `"[" * 100_000`  fits easily under
    `MAX_TEXT_LENGTH`) can blow the interpreter's C stack before Python's
    own `RecursionError` machinery gets a chance to fire at all, which is
    exactly the crash `parse_extraction` exists to turn into a documented
    `Abort(2)` instead. Runs as a plain, non-recursive character scan for
    that reason — a recursive depth-checker would have the very problem
    it's trying to prevent.

    String literals are tracked (quote + backslash-escape awareness) so a
    `{`/`[` that's just ordinary text inside a JSON string value is never
    mistaken for real structure. Malformed JSON can desync this tracking,
    but that's harmless: anything this scan under- or over-counts on
    genuinely broken input still fails `json.loads` moments later with its
    own, more specific error — this function's only job is to keep VALID
    but pathological JSON from ever reaching the recursive decoder.
    """
    depth = 0
    in_string = False
    escaped = False
    for ch in payload:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise Abort(2, f"goal-extractor JSON nesting exceeds the maximum depth ({MAX_JSON_DEPTH})")
        elif ch in "}]":
            depth -= 1


def _check_json_bounds(value: object, path: str = "") -> None:
    """Cap every array's length, anywhere in the ALREADY-parsed structure —
    not just the top-level `proposed` list. Safe to recurse in plain Python
    here specifically because `_check_json_depth` already bounded the
    source text to `MAX_JSON_DEPTH` before `json.loads` ever ran, so this
    can only ever be called against a structure a handful of levels deep.
    Runs only once the top-level payload is already confirmed to be an
    object (see `parse_extraction`) — checking bounds before that shape is
    established let a giant top-level array report "array exceeds the
    maximum length" instead of the real problem, "is not an object".

    Threads a best-effort JSON path through the walk (`proposed[3].evidence`,
    matching the `proposed[{index}].{key}` convention `_validate_proposal`
    already established) so an abort on a block carrying a dozen proposals
    doesn't leave the reader re-parsing the whole payload by hand to find
    the offending array. `path` names the container being walked INTO, one
    level per recursive call, before the length check on it ever runs — so
    the array that actually blows the cap is named precisely, not guessed
    at afterward.
    """
    if isinstance(value, list):
        if len(value) > MAX_JSON_ARRAY_LENGTH:
            where = f" at {path}" if path else ""
            raise Abort(2, f"goal-extractor JSON array exceeds the maximum length "
                           f"({MAX_JSON_ARRAY_LENGTH}){where}")
        for i, item in enumerate(value):
            _check_json_bounds(item, f"{path}[{i}]")
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_json_bounds(v, f"{path}.{k}" if path else str(k))


def _validate_proposal(raw: dict, index: int) -> dict:
    """Validate one already-parsed proposal object. Checked in this order:
    unrecognized keys, then missing keys, then each field's own type/length/
    display-safety, then the cross-field semantic rules — each stage only
    trusts what the stage before it already proved.

    Iterates `_FIELD_NAMES` in a fixed order purely for a deterministic
    abort message (which field gets named first when several are wrong) —
    unlike the retired line format's `_REQUIRED_FIELDS`, this carries no
    security weight: a JSON object's keys can't be reordered to smuggle
    anything past this function the way an unescaped line could.
    """
    for key in raw:
        if key not in _KNOWN_FIELDS:
            raise Abort(2, f"proposed[{index}] has an unrecognized field: {_bounded_repr(_render_line(key))}")
    for key in _FIELD_NAMES:
        if key not in raw:
            raise Abort(2, f"proposed[{index}] is missing a required field: {key!r} "
                           "(every field must be present explicitly; use `null` for "
                           "`parent`/`was` when they do not apply)")

    item: dict = {}
    for key in _FIELD_NAMES:
        value = raw[key]
        nullable = key in _NULLABLE_FIELDS
        if value is None:
            if not nullable:
                # `null` is legal ONLY on `parent`/`was` — JSON carries real
                # types, so every field's type is checked explicitly instead
                # of assuming the line format's "it's always a string" world.
                raise Abort(2, f"proposed[{index}].{key} must be a string, got null")
            item[key] = None
            continue
        if not isinstance(value, str):
            raise Abort(2, f"proposed[{index}].{key} must be a string"
                           f"{' or null' if nullable else ''}, got {type(value).__name__}")
        if len(value) > MAX_VALUE_LENGTH:
            raise Abort(2, f"proposed[{index}].{key} exceeds the maximum length ({MAX_VALUE_LENGTH} chars)")
        if _has_disallowed_display_chars(value):
            # Guards the DECODED value, after json.loads has already turned
            # a source-level escape like `‮` into the real BiDi
            # character it names — checking the raw JSON text instead would
            # only ever see the harmless six-character escape sequence and
            # let the actual override character walk straight through.
            raise Abort(2, f"proposed[{index}].{key} contains a disallowed "
                           "control or display-override character")
        item[key] = value

    for key in ("id", "text", "level", "tag", "origin"):
        if not item[key]:
            raise Abort(2, f"proposed[{index}].{key} must not be empty")
    if not _origin_is_valid(item["origin"]):
        # Checked with the cross-field semantic rules rather than in the
        # type loop above so the abort names the VALUE that is wrong, not
        # just the field — `origin` is the one field whose legal values are
        # a vocabulary (`extracted` / `plan:<path>`) instead of a type.
        raise Abort(2, f"proposed[{index}] has an invalid origin "
                       f"(expected {ORIGIN_EXTRACTED!r} or "
                       f"{ORIGIN_PLAN_PREFIX + '<path>'!r}): {_bounded_repr(item['origin'])}")
    if item["tag"] not in VALID_TAG:
        raise Abort(2, f"proposed[{index}] has an invalid tag: {_bounded_repr(item['tag'])}")
    if item["level"] not in VALID_LEVEL:
        raise Abort(2, f"proposed[{index}] has an invalid level: {_bounded_repr(item['level'])}")
    if item["tag"] == "changed" and not item["was"]:
        raise Abort(2, f"proposed[{index}] tagged `changed` requires `was`: {_bounded_repr(item['id'])}")
    if item["level"] == "subgoal" and not item["parent"]:
        raise Abort(2, f"proposed[{index}] subgoal requires a parent: {_bounded_repr(item['text'])}")
    return item


def parse_extraction(text: str) -> list[dict]:
    """Parse the goal-extractor's return block into structured proposals.

    Strict on purpose: this is the last place a hallucinated or malformed
    proposal can be stopped before it reaches the ledger's decision-applying
    path (`apply_decisions`), which trusts everything this function returns.
    Every failure raises `Abort(2)` with a message naming exactly which check
    fired, never a silent drop — this input comes from a language model, so
    a misspelled or hallucinated field is the expected failure shape, not an
    exotic one.

    Contract as of 2026-08-13 (Pedro's decision): the block between the
    delimiters is a JSON object, `{"proposed": [...]}`, not the original
    indented-line format. The root problem that format could never fully
    close was structural, not a missing check: in an unescaped line format,
    a newline inside a value becomes a real line, so a quoted value can
    always try to impersonate a field. JSON closes that by construction —
    a newline inside a JSON string stays inside the string, and no quoted
    content can ever become structure. Two gaps the line format left open
    are closed here as a direct consequence, not by any check added in this
    pass: a `text`/`evidence` value that embeds what looks like a complete
    second proposal is just string content now, never a sibling array
    element (see `test_a_same_indent_fully_well_formed_phantom_proposal_is_now_inert`);
    and a value that embeds what looks like a trailing `evidence:` line can
    no longer forge that field once the genuine proposal omits it — the
    fake text sits inside `text`'s own string, and the JSON object's real
    `evidence` KEY is still simply absent (see
    `test_an_injection_riding_text_can_no_longer_forge_a_missing_evidence_field`).

    Both closures assume the extractor emits GENUINELY well-formed JSON —
    via a real serializer (`json.dumps` or equivalent), not hand-assembled
    text that string-interpolates untrusted content into a `"key": "..."`
    shape. That assumption is not this function's to enforce or verify: a
    naively-interpolated payload that happens to produce syntactically
    valid JSON (an unescaped `"` in session content closing the `text`
    string early, followed by a forged `"evidence": "..."` key) is, by the
    time it reaches this function, indistinguishable from JSON a
    well-behaved extractor wrote on purpose — the same bytes either way.
    This is exactly parameterized-query-vs-string-concatenation, one layer
    up: the fix lives in HOW the extractor builds its output, not in how
    this function reads it. Flagged here for whoever implements Task 7
    (the goal-extractor agent) to close at that boundary — by construction,
    via a real serializer — not something a stricter `parse_extraction`
    could ever close on its own.

    The payload object's own keys are exactly as strict as each proposal's:
    `_TOP_LEVEL_KNOWN_FIELDS` rejects anything besides `proposed`, decided
    deliberately rather than left open — an extractor that hallucinates or
    renames the top-level key should fail loud at the field that's actually
    wrong, not fall through to the far less specific "contained no
    proposals" abort several checks later.

    The delimiters themselves have no escaping mechanism, but that no
    longer matters the way it did for the line format: `<<<GOAL_EXTRACTION>>>`
    is located by its FIRST occurrence and `<<<END>>>` by its LAST — so a
    literal mention of either marker inside a JSON string value (the tool
    is self-hosting; a session it summarizes may well discuss its own
    delimiters) sits between the two anchors and is simply part of the JSON
    text being parsed, not mistaken for a real boundary. This is a
    fail-safe posture, not a fail-correct one: content between a genuine
    open marker and a LATER, unrelated mention of the close marker (an
    extractor explaining the format before its real answer, the decoy shape
    `test_a_decoy_block_before_the_real_one_fails_safe_not_silently_parsed_instead`
    exercises) still gets sliced into the candidate payload — but unlike
    the line format, where that shape could produce a plausible, silently
    wrong result, splicing two objects (or an object and free prose)
    together is essentially never valid JSON, so `json.loads` itself
    rejects it. Every known failure shape here still ends in `Abort(2)`;
    none produces a value `apply_decisions` would trust.
    """
    if not isinstance(text, str):
        raise Abort(2, "goal-extractor output must be a string")
    if len(text) > MAX_TEXT_LENGTH:
        # Checked before any scanning or parsing work runs, so an oversized
        # payload cannot burn CPU/memory before being rejected.
        raise Abort(2, f"goal-extractor output exceeds the maximum length ({MAX_TEXT_LENGTH} chars)")

    open_idx = text.find(_OPEN_MARKER)
    end_idx = text.rfind(_END_MARKER)
    if open_idx == -1 or end_idx == -1:
        raise Abort(2, "goal-extractor returned no <<<GOAL_EXTRACTION>>> block")
    if end_idx < open_idx + len(_OPEN_MARKER):
        # Both markers exist somewhere in `text`, but the last `<<<END>>>`
        # sits before the first `<<<GOAL_EXTRACTION>>>` — a distinct
        # extractor bug from "no markers at all" above, so it gets its own
        # wording rather than reusing that message.
        raise Abort(2, "goal-extractor markers are out of order "
                       "(<<<END>>> appears before <<<GOAL_EXTRACTION>>>)")
    block_content = text[open_idx + len(_OPEN_MARKER):end_idx]
    payload_text = _strip_code_fence(block_content)

    _check_json_depth(payload_text)
    dup_tracker = _DuplicateKeyTracker()
    try:
        payload = json.loads(payload_text, object_pairs_hook=dup_tracker)
    except json.JSONDecodeError as exc:
        if payload_text.startswith("```"):
            # `_strip_code_fence` only ever leaves a leading "```" in place
            # when it could not find a well-formed close for an opening
            # fence it did find — naming the fence as the suspect here
            # instead of surfacing the raw decoder error (which always
            # points at column 1, a backtick, with no hint a fence was
            # involved) is the fallback the audit asked for.
            raise Abort(2, "goal-extractor block looks like it starts with a markdown "
                           "code fence that never closes, or doesn't wrap valid JSON "
                           "once stripped — check for a missing closing ``` or malformed "
                           "JSON inside the fence") from None
        # JSONDecodeError's own message reports a line/column/character
        # position, never a slice of the offending text itself, so it's
        # already bounded regardless of how large or pathological the input
        # was — no `_bounded_repr()` needed on `exc` here.
        raise Abort(2, f"goal-extractor block is not valid JSON: {exc}") from None

    if dup_tracker.found is not None:
        index, key = dup_tracker.found
        locator = "top-level object" if index == dup_tracker.calls - 1 else f"proposed[{index}]"
        raise Abort(2, f"goal-extractor JSON has a duplicate key in {locator}: {_bounded_repr(_render_line(key))}")

    if not isinstance(payload, dict):
        # A bare list, a string, or `null` at the top level — malformed in
        # a way `_DuplicateKeyTracker` (which only ever fires for a real
        # JSON object) never gets a chance to see. Checked BEFORE
        # `_check_json_bounds` walks the structure: a giant top-level array
        # used to report "array exceeds the maximum length" instead of the
        # actual problem, "is not an object", because the bounds walk ran
        # first and didn't yet know what shape it was looking at.
        raise Abort(2, "goal-extractor JSON payload is not an object")
    for key in payload:
        if key not in _TOP_LEVEL_KNOWN_FIELDS:
            # Same rejection `_validate_proposal` applies one level down —
            # see `_TOP_LEVEL_KNOWN_FIELDS` for why the payload object
            # itself is not left permissive.
            raise Abort(2, f"goal-extractor JSON payload has an unrecognized "
                           f"top-level field: {_bounded_repr(_render_line(key))}")

    _check_json_bounds(payload)

    proposed = payload.get("proposed")
    if not isinstance(proposed, list):
        raise Abort(2, "goal-extractor JSON payload has no `proposed` list")
    if len(proposed) > MAX_PROPOSALS:
        # Checked against the raw list length, before validating a single
        # proposal's own fields — a block proposing thousands of items is
        # rejected up front, not after fully validating all of them.
        raise Abort(2, f"goal-extractor block proposes too many proposals (cap {MAX_PROPOSALS})")

    items: list[dict] = []
    for i, raw_item in enumerate(proposed):
        if not isinstance(raw_item, dict):
            raise Abort(2, f"proposed[{i}] is not an object")
        items.append(_validate_proposal(raw_item, i))
    if not items:
        raise Abort(2, "goal-extractor block contained no proposals")

    # Cross-proposal uniqueness. `_validate_proposal` only ever sees ONE
    # proposal, so a repeated `id` — an ordinary generation slip, and one
    # `goal-extractor.md`'s anti-patterns explicitly warn against, which is
    # evidence it is expected rather than exotic — used to parse cleanly and
    # was caught only by `apply_decisions`'s batch-local collision check.
    # That is the WRONG SIDE OF THE GATE: by then the user has already
    # reviewed and approved a batch that was never committable, and the
    # abort is the all-or-nothing one that discards every unrelated `seen`
    # op with it, on a call site that (unlike the parse-abort path) has no
    # fallback. Failing here instead makes the same slip recoverable, and
    # costs the user nothing but a re-run.
    seen_ids: dict[str, int] = {}
    for i, item in enumerate(items):
        if item["id"] in seen_ids:
            raise Abort(2, f"proposed[{i}]: id {_bounded_repr(item['id'])} already "
                           f"used by proposed[{seen_ids[item['id']]}]")
        seen_ids[item["id"]] = i

    goal_count = sum(1 for item in items if item["level"] == "goal")
    subgoal_count = sum(1 for item in items if item["level"] == "subgoal")
    if goal_count > MAX_GOALS:
        raise Abort(2, f"goal-extractor block proposes too many top-level goals "
                       f"({goal_count} > {MAX_GOALS})")
    if subgoal_count > MAX_SUBGOALS:
        raise Abort(2, f"goal-extractor block proposes too many sub-goals "
                       f"({subgoal_count} > {MAX_SUBGOALS})")
    return items


_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _render_line(value: object) -> str:
    """Coerce a stored value to one safe physical line before it goes into
    the rendered mirror — the same trust boundary `_has_disallowed_display_chars`
    guards at ingestion (`parse_extraction`), applied again here because a
    goal already IN the ledger reaches this file by a different path
    (`cmd_commit`, or a hand-edited ledger) that never passed through that
    check.

    `apply_decisions`'s `create`/`retext` branches only `.strip()` the
    incoming text — they never reject an embedded newline, a BiDi
    override/isolate character, or a stacked-combining-mark run — and
    `_check_ledger_shape` never inspects `text`'s content, only that the
    record's `id` is a string. So a ledger can legitimately hold a goal
    whose `text` contains any of those. Left alone: a newline breaks the
    bullet across two physical lines with no `- ` prefix on the
    continuation; a BiDi override can make the rendered line *display*
    something other than what it contains, in the one file whose entire
    purpose is to be trusted at a glance; zalgo-stacked marks make it
    unreadable. Handling is uniformly non-aborting — collapse/strip, never
    refuse — because this is a DERIVED, best-effort mirror: escaping would
    show a literal `\\n` to a reader this file exists to be readable for,
    and refusing would abort the entire render over one goal's stray
    content. Nothing here rejects the ledger itself, only what reaches the
    page.

    `Cc` (control) and `Cs` (lone surrogate) are dropped, deliberately
    ASYMMETRIC with `_has_disallowed_display_chars`'s all-`Cc`-except-`\\t`
    abort: that guard REFUSES at ingestion, where a false rejection only
    costs a re-run of `parse_extraction`; this one COLLAPSES at render
    time, where the same refusal would abort a whole rendered file over one
    goal's stray character, discarding every other goal in it too. A
    whitespace-classed `Cc` (`\\n`, `\\t`, `\\r`) is let through here so the
    line-collapse regex below still turns it into a single space the same
    way it always has — dropping it outright would just remove the
    boundary the regex is collapsing, changing behavior with no safety
    gain. A non-whitespace `Cc` (e.g. `\\x07`, a bell) and any `Cs` (an
    unpaired surrogate — see `_has_disallowed_display_chars`'s docstring
    for how one reaches Python code at all) are dropped outright: the
    former can misrepresent the line in a terminal, the latter cannot be
    UTF-8 encoded and would otherwise reach `cmd_render`'s
    `encoding="utf-8"` write unfiltered.
    """
    text = str(value)
    kept = []
    combining_run = 0
    for ch in text:
        if ch in _BIDI_CONTROL_CHARS:
            continue
        category = unicodedata.category(ch)
        if category in ("Cc", "Cs") and not ch.isspace():
            continue
        if category in ("Mn", "Me"):
            combining_run += 1
            if combining_run > _MAX_COMBINING_MARKS:
                continue
        else:
            combining_run = 0
        kept.append(ch)
    return _WHITESPACE_RUN_RE.sub(" ", "".join(kept)).strip()


def render_markdown(data: dict) -> str:
    """Derived, human-readable mirror. NEVER a source of truth — goals.py
    regenerates this file wholesale, so a hand edit is lost on the next run.

    Validates the whole `goals` collection up front, the same
    `_check_ledger_shape` precondition `apply_decisions`/`autopark` already
    require. `data` here is not guaranteed to have come through
    `read_ledger` (this function is pure and Task 6 calls it directly), and
    even when it did, a hand-edited or partially-migrated ledger can hold a
    record `_check_ledger_shape` doesn't fully validate — in particular
    `origin` (Task 3 left it unvalidated) and `text` (never even checked
    for presence). A ledger holding `origin: null` would reach
    `.startswith()` on `None` and blow up with a bare `AttributeError`; a
    record missing `text` entirely would blow up on the dict subscript that
    used to sit here. Both are guarded explicitly below instead of trusted,
    for the same reason `_render_line` re-guards display safety: a
    malformed record must degrade this file's output, never crash the
    render outright.
    """
    _check_ledger_shape(data)
    # `project` is not validated by `_check_ledger_shape` (it only checks
    # `goals`) and is not necessarily the caller-supplied, already-slugified
    # string — a hand-edited ledger can hold anything json.loads accepts,
    # including a lone-surrogate escape. Routed through `_render_line` like
    # every other untrusted value in this file, for the same reason: this is
    # the only guard standing between that value and `cmd_render`'s
    # `encoding="utf-8"` write.
    lines = [f"# Goals — {_render_line(data.get('project', 'unknown'))}", ""]
    live = [g for g in data["goals"] if _is_visible(g)]
    tops = [g for g in live if g.get("level") == "goal"]
    claimed: set = set()
    any_inferred = False
    for goal in tops:
        box = "x" if goal.get("status") == "done" else " "
        origin = goal.get("origin")
        is_plan = isinstance(origin, str) and origin.startswith("plan:")
        mark = "" if is_plan else "  (inferred)"
        any_inferred = any_inferred or not is_plan
        lines.append(f"- [{box}] {_render_line(goal.get('text', '(untitled goal)'))}{mark}")
        for sub in [g for g in live if g.get("parent") == goal["id"]]:
            claimed.add(sub.get("id"))
            sub_box = "x" if sub.get("status") == "done" else " "
            lines.append(f"  - [{sub_box}] {_render_line(sub.get('text', '(untitled goal)'))}")
    # A sub-goal whose `parent` names a goal that is parked, dropped, or was
    # never a real id matches no top-level goal in the loop above, so without
    # this it vanishes from the rendered file with no trace — the file the
    # user actually reads. Surface it instead of silently dropping it.
    orphans = [g for g in live if g.get("level") == "subgoal" and g.get("id") not in claimed]
    if orphans:
        lines += ["", "## Unparented sub-goals"]
        for sub in orphans:
            sub_box = "x" if sub.get("status") == "done" else " "
            lines.append(f"- [{sub_box}] {_render_line(sub.get('text', '(untitled goal)'))}")
    # `level` is only ever checked against {"goal", "subgoal"} above — a
    # record holding anything else (hand-edited, future schema drift) is
    # neither a `top` nor an `orphan` and would otherwise vanish from this
    # file exactly like the orphan case above, with no trace at all.
    unrecognized = [g for g in live if g.get("level") not in ("goal", "subgoal")]
    if unrecognized:
        lines += ["", "## Records with an unrecognized level (ledger may be malformed)"]
        for rec in unrecognized:
            lines.append(f"- {_render_line(rec.get('text', '(untitled goal)'))} "
                         f"(level: {_render_line(rec.get('level'))!r})")
    parked = len([g for g in data["goals"] if g.get("status") == "parked"])
    if parked:
        lines += ["", f"Parked: {parked} goal(s) untouched for over {PARK_AFTER_DAYS} days."]
    # Mirrors the `Parked: N` line above, for the same reason: a goal that
    # left the tree must still be accounted for somewhere on the page, or the
    # only signal that approving `gone` did anything is the bullet's absence
    # — which is indistinguishable from the goal never having existed.
    dropped = len([g for g in data["goals"] if g.get("status") == "dropped"])
    if dropped:
        lines += ["", f"Dropped: {dropped} goal(s) closed out at the approval gate."]
    if any_inferred:
        lines += ["", "(inferred) = this file cannot confirm the goal was explicitly planned "
                       "ahead of time; it does not mean the goal is uncertain or wrong."]
    lines += ["", "<!-- generated by goals.py from the ledger; do not edit by hand -->"]
    return "\n".join(lines) + "\n"


def _require_project(project: str) -> None:
    """Same guard `cmd_load` already applies, factored out so every command
    that eventually calls `slugify(project)` fails with the documented
    `Abort(2)` instead of a bare `AttributeError` from `.lower()` on a
    non-string. `cmd_load` validates this inline (it predates this helper);
    `cmd_commit` and `cmd_render` did not, until this pass closed the gap.
    """
    if not isinstance(project, str) or not project:
        raise Abort(2, "project must be a non-empty string")


def _bounded_read_text(path_str: str, what: str) -> str:
    """Bound a file read at a CLI boundary before touching its contents.
    `parse_extraction` already rejects a string longer than `MAX_TEXT_LENGTH`,
    but only after the caller has already read the whole file into memory to
    produce that string — this checks the file's size on disk FIRST, so an
    oversized file is rejected before it is ever read, not after. Byte size
    is a conservative proxy for character count (UTF-8 multi-byte encoding
    only ever makes bytes >= chars), so this can reject a file that would
    have decoded to fewer than `MAX_TEXT_LENGTH` characters; that is an
    acceptable trade for never reading an unbounded file into memory at a
    boundary. `what` names the file in the abort message (e.g. "decision
    set", "extractor output file") so two different CLI subcommands sharing
    this helper still produce distinguishable errors.

    A non-UTF-8 file raises `UnicodeDecodeError` from `read_text`, which is
    not an `OSError` — caught explicitly alongside it, or it would escape as
    a bare traceback instead of the documented `Abort(2)` contract every
    other failure in this module honors.
    """
    path = Path(path_str)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise Abort(2, f"cannot stat {what}: {exc}")
    if size > MAX_TEXT_LENGTH:
        raise Abort(2, f"{what} exceeds the maximum length ({MAX_TEXT_LENGTH} bytes on disk)")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Abort(2, f"cannot read {what}: {exc}")


def cmd_commit(project: str, session: str, decisions_path: str) -> dict:
    """Apply a decision batch and persist it — but only when something
    actually changed. `apply_decisions` can legitimately apply zero of a
    batch's decisions (every one was already true of the current state), and
    unconditionally calling `write_ledger` in that case would restamp
    `data["updated"]` for a ledger that did not move. `updated` feeds
    staleness reads elsewhere in this system (autopark's own cutoff is
    computed from `now_iso`, not `updated`, but a future "last touched"
    display is exactly the kind of read a lying timestamp would corrupt) —
    so a no-op batch takes the lock, computes `applied == 0`, and returns
    without writing anything.
    """
    _require_project(project)
    slug = slugify(project)
    try:
        payload = json.loads(_bounded_read_text(decisions_path, "decision set"))
    except json.JSONDecodeError as exc:
        raise Abort(2, f"decision set unreadable: {exc}")
    if not isinstance(payload, dict):
        raise Abort(2, "decision set is not an object")
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise Abort(2, "decision set has no `decisions` list")
    now_iso = _now_iso()
    with ledger_lock(slug):
        data = read_ledger(slug, project)
        applied = apply_decisions(data, session, decisions, now_iso)
        if applied:
            write_ledger(slug, data)
    return {"ok": True, "applied": applied, "ledger": str(ledger_path(slug))}


VAULT_PROJECTS = Path.home() / "Documents" / "Necronomicon" / "01-Projects"
_UUID8_RE = re.compile(r"^[0-9a-f]{8}$")
_H2_TIME_RE = re.compile(r"^## \d{2}:\d{2} — ", re.MULTILINE)
_FM_SESSION_RE = re.compile(r"^session_uuid:\s*(\S+)", re.MULTILINE)
_RESUME_FIELDS = ("Pick up at", "Active blocker", "Reopen first", "First command", "Branch")


def _ledger_fingerprint() -> dict:
    """mtime_ns per ledger file — the input-freshness signal for the stale-render
    guard. `write_ledger` ends in os.replace, which renames a freshly written tmp
    file over the target, so a ledger's mtime reliably advances on every write."""
    out = {}
    try:
        for p in LEDGER_DIR.glob("*.json"):
            try:
                out[p.name] = p.stat().st_mtime_ns
            except OSError:
                out[p.name] = -1
    except OSError:
        pass
    return out


def _fmt_session(uuid: str, live: bool, last_seen: str) -> str:
    short = _render_line(uuid)[:8] if uuid else "unknown"
    return f"`{short}` {'**LIVE**' if live else 'dead'}"


def _handoff_card(project: str, uuid: str) -> list:
    """Locate this session's newest report under the project's progress/ folder
    and lift its FIRST `### Resume Here` block (newest block is on top).

    Two lookups, in order of trust:
      1. uuid8 in the FILENAME — exact, cheap, and only possible because reports
         are now per-session.
      2. frontmatter `session_uuid:` — the legacy fallback, and trustworthy ONLY
         on a file with exactly one `## HH:MM` block. Frontmatter is frozen to
         the file's first writer, so a multi-block legacy day-file attributes
         every block to whoever wrote first. Guessing there would invent history.
    """
    if not uuid or len(uuid) < 8:
        return []
    short = uuid[:8]
    folder = VAULT_PROJECTS / project / "progress"
    if not folder.is_dir():
        return []
    try:
        cands = sorted(folder.glob(f"*{short}*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        cands = []
    if not cands:
        # Fallback: scan for a frontmatter match on single-block files only.
        try:
            others = sorted(folder.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        for p in others:
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = _FM_SESSION_RE.search(txt)
            if not m or not m.group(1).startswith(short):
                continue
            if len(_H2_TIME_RE.findall(txt)) != 1:
                continue  # not session-attributable — never guess
            cands = [p]
            break
    if not cands:
        return []
    try:
        text = cands[0].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    idx = text.find("### Resume Here")
    if idx == -1:
        return []
    body = text[idx + len("### Resume Here"):]
    nxt = re.search(r"^#{2,3} ", body, re.MULTILINE)
    if nxt:
        body = body[:nxt.start()]
    lines = []
    for raw in body.splitlines():
        s = raw.strip()
        if not s.startswith("-"):
            continue
        for field in _RESUME_FIELDS:
            if f"**{field}:**" in s:
                lines.append(f"  {_render_line(s.lstrip('- '))}")
                break
    return lines[:6]  # validate_report.py caps Resume Here at 6 bullets


def _render_goal_line(goal: dict, live_of) -> str:
    status = goal.get("status")
    status_txt = _render_line(status) if isinstance(status, str) else "?"
    box = "[x]" if status_txt == "done" else "[ ]"
    text = _render_line(goal.get("text", "")) or "_(no text)_"
    owner = goal.get("owner_session")
    owner = owner if isinstance(owner, str) else ""
    bits = [f"- {box} {text} · `{status_txt}`"]
    if owner:
        bits.append(f"created by {_fmt_session(owner, live_of(owner), '')}")
    touched = sorted({h.get("session") for h in goal.get("history", [])
                      if isinstance(h, dict) and isinstance(h.get("session"), str)})
    touched = [t for t in touched if t != owner]
    if touched:
        bits.append("touched by " + ", ".join(_fmt_session(t, live_of(t), "") for t in touched))
    origin = goal.get("origin")
    if not (isinstance(origin, str) and origin.startswith("plan:")):
        bits.append("_(inferred)_")
    last = goal.get("last_seen")
    if isinstance(last, str) and last:
        bits.append(f"last seen {_render_line(last)}")
    return " · ".join(bits)


def _render_project_section(project: str, data: dict, live_of) -> list:
    goals = [g for g in data.get("goals", []) if isinstance(g, dict)]
    buckets = {"open": [], "done": [], "parked": [], "dropped": []}
    for g in goals:
        st = g.get("status")
        buckets.get(st if isinstance(st, str) and st in buckets else "open", buckets["open"]).append(g)

    out = [f"## {_render_line(project)}", ""]
    if not goals:
        out += ["_no live goals_", ""]
        return out

    # open + done in the main body. Deliberately NOT filtered through
    # `_is_visible`: PARK_AFTER_DAYS auto-parks at 14 days, so an _is_visible
    # filter would silently empty this page as ledgers age.
    for name in ("open", "done"):
        if buckets[name]:
            out.append(f"**{name.capitalize()}** ({len(buckets[name])})")
            out += [_render_goal_line(g, live_of) for g in buckets[name]]
            out.append("")
    # parked and dropped are SEPARATE, never merged: the ledger keeps the
    # distinction on purpose — parked is autopark's housekeeping, dropped is a
    # deliberate closure. Filing a deliberately-closed goal under a heading that
    # reads "temporarily shelved" is a lie about its state.
    for name in ("parked", "dropped"):
        if buckets[name]:
            out.append(f"### {name.capitalize()} ({len(buckets[name])})")
            out += [_render_goal_line(g, live_of) for g in buckets[name]]
            out.append("")

    owners = sorted({g.get("owner_session") for g in goals
                     if isinstance(g.get("owner_session"), str) and g.get("owner_session")})
    for uuid in owners:
        card = _handoff_card(project, uuid)
        head = f"**Session {_fmt_session(uuid, live_of(uuid), '')}**"
        out.append(head)
        out += card if card else ["  _no handoff recorded_"]
        out.append("")
    return out


def _projects_without_ledger() -> tuple:
    """(projects with no ledger, total vault projects).

    Compares every folder under `01-Projects/` against the ledger slugs on disk.
    Returns (0, 0) when the vault is unreadable — a count that cannot be taken is
    omitted from the header rather than reported as zero, which would read as
    "every project is covered".
    """
    try:
        dirs = [p for p in VAULT_PROJECTS.iterdir()
                if p.is_dir() and not p.name.startswith(".")]
    except OSError:
        return (0, 0)
    try:
        slugs = {p.stem for p in LEDGER_DIR.glob("*.json")}
    except OSError:
        return (0, 0)
    return (sum(1 for p in dirs if slugify(p.name) not in slugs), len(dirs))


def _build_dashboard_region() -> tuple:
    """Assemble the managed region. Runs OUTSIDE the dashboard lock — it reads 5
    ledgers and parses up to ~120 report files, which is seconds of work, and the
    lock's own budget (LOCK_CAP_MS) is 5s. Holding it across this would make a
    concurrent session time out for no benefit: this lock protects the PAGE, and
    ledger writers serialise on their own goal-ledger-<slug>.lock."""
    now = time.time()
    _live_cache = {}

    def live_of(uuid):
        if uuid not in _live_cache:
            try:
                _live_cache[uuid] = session_live(uuid, now)
            except Abort:
                _live_cache[uuid] = False
        return _live_cache[uuid]

    sections, stats = [], {"projects": 0, "unreadable": 0,
                           "open": 0, "done": 0, "parked": 0, "dropped": 0}
    for path in sorted(LEDGER_DIR.glob("*.json")):
        slug = path.stem
        try:
            # read_ledger's signature is (slug: str, project: str) — NOT a Path.
            # `project` only synthesises an empty ledger for a MISSING file
            # (which the glob rules out), so passing slug twice is inert.
            # _check_slug rejects a hand-dropped bad filename with Abort(2),
            # caught here by design.
            #
            # BOTH calls must sit inside this try. A ledger that parses as JSON
            # but whose `goals` is a dict sails past read_ledger and aborts in
            # _check_ledger_shape; guarding only the parse lets that Abort reach
            # main() and blank all five projects — the exact failure this
            # per-ledger degrade exists to prevent, one line later.
            data = read_ledger(slug, slug)
            _check_ledger_shape(data)
        except Abort as exc:
            stats["unreadable"] += 1
            sections += [f"## {_render_line(slug)}", "",
                         f"_ledger unreadable: {_render_line(str(exc))}_", ""]
            continue
        project = data.get("project") or slug
        if not isinstance(project, str):
            project = slug
        stats["projects"] += 1
        for g in data.get("goals", []):
            if isinstance(g, dict):
                st = g.get("status")
                if isinstance(st, str) and st in stats:
                    stats[st] += 1
        sections += _render_project_section(project, data, live_of)

    live_sessions = sum(1 for v in _live_cache.values() if v)
    summary = (f"**{stats['projects']} projects** · {stats['open']} open · "
               f"{stats['done']} done · {stats['parked']} parked · "
               f"{stats['dropped']} dropped · {live_sessions} live "
               f"session{'' if live_sessions == 1 else 's'}")
    if stats["unreadable"]:
        summary += f" · {stats['unreadable']} unreadable ledger(s)"
    # A vault project with no ledger is simply ABSENT from this page. Absent and
    # empty must not look identical, and a page that silently covers 7 of 24
    # projects reads as complete. State the gap in the header instead.
    no_ledger, vault_total = _projects_without_ledger()
    stats["no_ledger"] = no_ledger
    if vault_total:
        summary += (f" · {no_ledger} of {vault_total} vault projects have no ledger "
                    f"(not shown)")

    preamble = [
        summary, "",
        "_Derived from the per-project goal ledgers. `created by` and `touched by` are what the",
        "ledger records — there is no assignment mechanism, so this is who last had hands on a",
        "goal, not who is on it now. A session that read a goal without changing it leaves no",
        "trace, so `touched by` is a floor. Handoff cards come from each report's `### Resume",
        "Here`; legacy multi-block day-files are not session-attributable and read `_no handoff",
        "recorded_` by design — never a guess._", "",
    ]
    return "\n".join(preamble + sections).rstrip() + "\n", stats


def cmd_dashboard(out_path: str) -> dict:
    """Regenerate the cross-project Goals & To-Do page.

    Ordering is the whole of it — parse everything FIRST, touch the target LAST:

      1. outside the lock: fingerprint the ledgers, read them, render the region
      2. take dashboard_lock()
      3. inside: stale-render check -> stat+read target -> ownership gate ->
         sentinel splice -> no-op compare -> changed-underfoot re-stat ->
         lock-ownership re-check -> os.replace
      4. release

    Step 3 is milliseconds, which is what keeps the 5s lock cap generous and the
    read..rename window (the one changed-underfoot guards) genuinely small.
    """
    out = Path(out_path)
    fingerprint_before = _ledger_fingerprint()
    region_body, stats = _build_dashboard_region()
    region = f"{SENT_BEGIN}\n\n{region_body}\n{SENT_END}"

    with dashboard_lock():
        # STALE-RENDER GUARD. Lock order follows who finishes rendering, not who
        # started, so a slow render that began early can acquire the lock AFTER a
        # fast one that began late — and overwrite it with older data. Neither the
        # changed-underfoot check (target mtime across our own ms-long hold) nor
        # the ownership re-check (our own token) can see that: both guard the tail
        # window, while the staleness accrued during the RENDER. Skipping is the
        # same "next run reconciles" behaviour used everywhere else here.
        if _ledger_fingerprint() != fingerprint_before:
            sys.stderr.write(
                "goals.py: NOTE-STALE-RENDER — a ledger changed during this render; "
                "write skipped (next run reconciles)\n")
            return {"ok": True, "skipped": "stale render", "written": None}

        cur = None
        cur_mtime_ns = 0  # 0 = fresh create
        if out.exists():
            try:
                cur_mtime_ns = out.stat().st_mtime_ns
                cur = out.read_text(encoding="utf-8")
            except OSError as exc:
                raise Abort(4, f"dashboard unreadable: {out} ({exc})")

        if cur is None:
            # FIRST RUN. This branch precedes the ownership gate and the sentinel
            # search on purpose: on a brand-new page there is no frontmatter to
            # own and no sentinel pair to find, so gating a create on ownership is
            # how a generated page never gets generated.
            text = (f"---\ntype: dashboard\nmanaged_by: {DASHBOARD_MANAGED_BY}\n"
                    f"updated: {datetime.now().strftime('%Y-%m-%d')}\n---\n\n"
                    f"# Goals & To-Do\n\n"
                    f"Auto-derived from the per-project goal ledgers. Do not hand-edit inside the\n"
                    f"sentinels — edits there are overwritten on the next /progress-report run.\n\n"
                    f"{region}\n")
        else:
            fm_match = re.match(r"^---\n(.*?)\n---\n", cur, re.DOTALL)
            managed = fm_match and re.search(
                rf"^managed_by:\s*{re.escape(DASHBOARD_MANAGED_BY)}\s*$",
                fm_match.group(1), re.MULTILINE)
            if not managed:
                raise Abort(4, f"refusing to overwrite unmanaged file "
                               f"(frontmatter managed_by != {DASHBOARD_MANAGED_BY}): {out}")
            b_hits = [m.start() for m in re.finditer(rf"^{re.escape(SENT_BEGIN)}$", cur, re.MULTILINE)]
            e_hits = [m.start() for m in re.finditer(rf"^{re.escape(SENT_END)}$", cur, re.MULTILINE)]
            if len(b_hits) > 1 or len(e_hits) > 1:
                raise Abort(4, f"ambiguous sentinels (multiple line-anchored occurrences) "
                               f"— refusing to write: {out}")
            if b_hits and e_hits and e_hits[0] > b_hits[0]:
                # Replace the managed region only — everything outside survives.
                text = cur[:b_hits[0]] + region + cur[e_hits[0] + len(SENT_END):]
            else:
                sys.stderr.write(f"goals.py: WARNING {out} lost its sentinels — "
                                 f"rewriting whole (managed_by confirmed)\n")
                text = re.sub(r"(?m)^(# Goals & To-Do\n)(.*)$", r"\1", cur, count=1)
                text = cur.split(SENT_BEGIN)[0].rstrip() + "\n\n" + region + "\n"

        # NO-OP GATE. An unchanged render must not bump the page's mtime or
        # thrash Obsidian's reload. Note `updated:` is only ever stamped on the
        # create branch above, never refreshed on a splice — refreshing it from
        # the clock would make every run differ by one line and this gate would
        # never fire, which is what makes it decorative instead of real.
        if cur is not None and text == cur:
            return {"ok": True, "skipped": "unchanged", "written": None, "stats": stats}

        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise Abort(4, f"dashboard output directory unusable: {exc}")
        tmp = out.with_name(f".{out.name}.tmp-{os.getpid()}")
        try:
            tmp.write_text(text, encoding="utf-8")
            # CHANGED UNDERFOOT — re-stat as late as possible. Catches a writer
            # that never takes this lock at all (Obsidian's autosave, Pedro
            # typing); orthogonal to the ownership check below, which catches
            # lock-taking writers.
            if cur is not None and out.stat().st_mtime_ns != cur_mtime_ns:
                sys.stderr.write(f"goals.py: NOTE-CHANGED-UNDERFOOT — {out} changed since it "
                                 f"was read; write discarded (next run reconciles)\n")
                return {"ok": True, "skipped": "changed underfoot", "written": None}
            # Holding the lock at the START of a write is not holding it at the
            # END. Mirrors write_ledger.
            token = _HELD.get("dashboard")
            owner = (LOCK_DIR / DASHBOARD_LOCK_NAME / "owner")
            if not token or owner.read_text(encoding="utf-8").strip() != token:
                raise Abort(5, "lock lost before write; dashboard NOT modified")
            os.replace(tmp, out)
        except (OSError, UnicodeEncodeError) as exc:
            raise Abort(4, f"dashboard write failed: {exc}")
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    return {"ok": True, "written": str(out), "stats": stats}


def cmd_render(project: str, out_path: str) -> dict:
    """Regenerate the derived markdown mirror. Deliberately does NOT take
    `ledger_lock`: this function only reads the ledger and writes a
    completely different file, and `write_ledger`'s atomic `os.replace`
    means any concurrent commit is never observed half-written — a
    concurrent reader here sees either the pre-commit ledger or the
    post-commit one, never a torn one. Taking the lock would only serialize
    every render behind every in-flight commit for no correctness gain.

    Writes NOTHING when the project has no ledger file at all. `read_ledger`
    returns a fresh empty schema for a missing file, which rendered to an
    89-byte `Goals.md` — a heading and the generated-by comment — created in
    the vault for a project with no goals, in the one file Pedro is meant to
    trust as the mirror of his goals. A ledger that EXISTS but holds nothing
    visible (every goal dropped) still renders: the mirror must be able to
    show that everything was closed out, and skipping there would leave a
    stale file asserting goals the user already retired. Skipping never
    deletes an existing mirror either — a file already in the vault is not
    this command's to remove.

    The existence check is deliberately not atomic with the read that
    follows it, for the same reason this function takes no lock: a commit
    landing in that window means this run skips a render it could have
    performed, and the next run performs it. Nothing is lost and nothing is
    torn — the failure mode is one stale render cycle, not a wrong file.
    """
    _require_project(project)
    slug = slugify(project)
    if not ledger_path(slug).exists():
        return {"ok": True, "skipped": "no ledger for this project", "rendered": None}
    data = read_ledger(slug, project)
    out = Path(out_path)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Abort(4, f"render output directory unusable: {exc}")
    tmp = out.with_name(f".{out.name}.tmp-{os.getpid()}")
    try:
        # Same `UnicodeEncodeError`-alongside-`OSError` fix as `write_ledger`,
        # and the same cleanup-in-`finally` reasoning — see there. `_render_line`
        # now strips lone surrogates out of every field it touches (`text`,
        # `level`, and `project` via `render_markdown`'s header line), so this
        # is defense-in-depth rather than the only thing standing between a
        # hand-edited ledger and an uncaught crash here — but it costs nothing
        # to keep the two atomic-write call sites in this module consistent.
        try:
            tmp.write_text(render_markdown(data), encoding="utf-8")
            os.replace(tmp, out)
        except (OSError, UnicodeEncodeError) as exc:
            raise Abort(4, f"render write failed: {exc}")
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "rendered": str(out)}


# ── The thin-session skip ───────────────────────────────────────────────────
#
# THE ONE PLACE THE THIN CONDITION IS DEFINED. `SKILL.md` Step 2 sets
# `THIN_SESSION = true when .event_count < 5 and .active_msg_count < 30` from
# the digest, Step 4b skips the goal step on it, and `validate_report.py` checks
# a `thin-session` declaration against the metrics the skip recorded. Those are
# three readers of ONE comparison: the validator imports `is_thin_session` from
# this module through the same by-path loader `project_slug` already uses for
# `slugify`, rather than restating `< 5` and `< 30` a second time. Two copies
# that agree today and drift later is a defect this project has already shipped
# (the `SLUG_MAX` truncation), and the only structural defense is that there be
# exactly one comparison.
#
# The SKILL.md prose is still a fourth statement of the numbers, in English, and
# nothing mechanically ties it to these constants. That is a documented residual,
# not a claim.
THIN_SESSION_CMD = "thin-session"
THIN_EVENT_COUNT_MAX = 5
THIN_ACTIVE_MSG_COUNT_MAX = 30


def is_thin_session(event_count: int, active_msg_count: int) -> bool:
    """Whether these two digest metrics satisfy the thin condition.

    `<`, not `<=`, on both: a session with exactly 5 events or exactly 30 active
    messages is NOT thin, and `AND`, not `OR` — one metric under the line does
    not make a session thin on its own.
    """
    return event_count < THIN_EVENT_COUNT_MAX and active_msg_count < THIN_ACTIVE_MSG_COUNT_MAX


def cmd_thin_session(project: str, event_count: int, active_msg_count: int) -> dict:
    """Record that this run skipped the goal step because the session was thin.

    Writes NO ledger entry and takes NO lock, deliberately and permanently. A
    thin session must remain a session in which the ledger is not touched —
    SKILL.md Step 4b's "no gate, no ledger write" stays literally true. The only
    artifact this produces is the run receipt `main` writes on its way out, and a
    receipt write is not a ledger write: `--dry-run` already leaves one while
    promising the same thing.

    The two metrics are RECORDED AS THE CALLER PASSED THEM. This function does
    not recompute them from a transcript, a digest, or anything else — it has no
    access to the transcript and inventing a second derivation of a number the
    validator compares against is the exact defect (`SLUG_MAX`, the two slug
    derivations) this project has already shipped.

    It does NOT reject metrics that fail `is_thin_session`, and that is a choice
    rather than an omission. Rejecting would mean aborting, an abort records a
    non-zero exit for this project, and `validate_report.py`'s `ledger-error`
    branch accepts any non-zero exit for this project as corroboration — so a
    guard here would hand a drifting caller a NEW cheap path into a different
    declaration. The honest division is: this command records what it was told,
    and the validator refuses a `thin-session` declaration whose recorded metrics
    do not satisfy the condition. The check lives where the claim is made.
    """
    _require_project(project)
    slug = slugify(project)
    return {
        "ok": True,
        "project": slug,
        "event_count": event_count,
        "active_msg_count": active_msg_count,
        # Computed by the single shared comparison, so a caller that wants to
        # know whether its own numbers qualify never writes the test itself.
        "thin": is_thin_session(event_count, active_msg_count),
    }


RUN_RECEIPT_MAX_ENTRIES = 20

# A session id becomes a receipt FILENAME here, exactly as it becomes a stamp
# filename in `validate_report.py`'s `cmd_result`. The same bounded token is
# used deliberately: the validator reads `<RUN_DIR>/<--stamp-session>.json`
# after accepting that flag against `[A-Za-z0-9_-]{1,128}`, so a wider pattern
# here would let this module write receipts under names the validator can never
# look up — the receipt would exist and the report would still be rejected.
# Fails closed: an id that does not match gets NO receipt file (never a
# sanitized or truncated one, which would silently attribute this run to a
# different session's receipt).
_RECEIPT_SESSION_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _read_receipt_entries(path: Path) -> list[dict]:
    """Existing entries from a receipt, or `[]` for anything unusable.

    A receipt is telemetry, not ledger state: an unreadable or malformed one
    is REPLACED by a fresh valid receipt carrying this run's entry, not
    repaired and not treated as a fault. Refusing to write over a corrupt
    receipt would be strictly worse than resetting it — the run's own entry
    would be lost, and losing it is what makes `validate_report.py` reject a
    report whose goal step actually ran.

    `read_text` on a truncated or non-UTF-8 file raises `UnicodeDecodeError`,
    and `json.loads` on a partial write raises `JSONDecodeError`. Neither is an
    `OSError`; both are `ValueError` subclasses, which is why `ValueError` is
    caught alongside `OSError` rather than an `OSError`-only handler that four
    earlier defects in this module were written with.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _receipt_project(project: object) -> str | None:
    """The slug this run was about, or `None` when there is none to record.

    Derived by `slugify` — the SAME function that built the ledger path this
    run actually wrote to, never a second derivation. `validate_report.py`
    reads this field back and compares it against a slug it obtains from this
    very function (it imports it rather than reimplementing it), so the two
    sides cannot agree today and drift apart later.

    Returns `None` rather than raising, on every path. Two callers legitimately
    have no project: `parse-extraction`, whose `--project` is optional and may
    simply be omitted, and any subcommand that aborted inside
    `_require_project`/`slugify` before it ever resolved one. Neither may cost
    the run its receipt — a receipt is written on abort paths precisely so a
    loud ledger failure is distinguishable from a goal step that never ran, and
    raising here would delete that signal exactly when it matters most. `None`
    reads downstream as "not this project", which fails closed at the
    validator; that is the safe direction for an unknown.
    """
    if not isinstance(project, str) or not project:
        return None
    try:
        return slugify(project)
    except Abort:
        return None


def _record_run(session: object, command: str, exit_code: int,
                project: object = None, effect: object = None,
                metrics: object = None) -> None:
    """Append one entry to `<RUN_DIR>/<session>.json` recording that this
    subcommand ran, with what exit code, against which project, and to what
    effect.

    This is the only artifact a run that SKIPPED the goal step cannot produce,
    which is the whole reason it exists: `validate_report.py result` refuses to
    validate a report whose session has no such receipt. It records that
    `goals.py` RAN and what that run DID — it is not, and must never be
    described as, evidence that a human was asked anything.

    `project` and `effect` exist because "a subprocess ran" was too weak a
    claim to hang a report on. Without `project`, one session touching project
    A then project B wrote a single receipt covering both, and A's entry
    validated B's report. Without `effect`, `commit` over a caller-authored
    `{"decisions": []}` produced an entry indistinguishable from a real one, so
    any declared count at all rode through on it. `effect` is the count the
    command itself returned (`applied` for `commit`, the number of proposals
    for `parse-extraction`) — captured from the actual return value, never
    recomputed here and never inferred from the arguments. `None` on `load`,
    which reaches no decision and therefore has no effect to state.

    `type(effect) is int` rather than `isinstance`: `bool` subclasses `int`, so
    `isinstance` would write `True` into a numeric field the validator compares
    against a declared count. Same reasoning as `_entry_exit_code`'s in
    `validate_report.py`.

    `metrics` is written ONLY when the caller passed a dict, and only by
    `thin-session`, which is the only command whose declaration is checked
    against numbers it did not itself produce. It is deliberately NOT added as a
    `null` field to the other four commands: `effect` already carries the
    "recorded no value" case for them, and the reader distinguishes an absent key
    from a null one, so a uniform-shape field would change four entry shapes to
    buy nothing. `effect` is NOT overloaded to carry the pair — it has been an
    integer or null on every entry ever written and the validator compares it
    against a declared count, so putting two metrics in it would give one field
    two meanings.

    The real exit code is recorded, including a non-zero one. A ledger that
    failed loudly is a legitimate reason to finish a run without a commit, and
    the validator can only tell that apart from "the step never ran" if the
    failure itself is on the receipt.

    BEST-EFFORT BY CONTRACT: nothing raised in here may change the exit code of
    the subcommand the caller actually asked for. A lost receipt degrades this
    guarantee for one run; aborting the ledger command over it would break the
    report the ledger exists to serve. Hence the deliberately broad `except
    Exception` — this function has no failure mode worth propagating, and a
    narrow handler here would re-run the exact wrong-exception-family mistake
    (`UnicodeEncodeError` under `except OSError`) that this file has now been
    patched for four times. `BaseException` is NOT caught: a KeyboardInterrupt
    or SystemExit passing through must stay fatal.
    """
    try:
        if not isinstance(session, str) or not _RECEIPT_SESSION_RE.fullmatch(session):
            raise ValueError(f"unusable session id {session!r}")
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        path = RUN_DIR / (session + ".json")
        entries = _read_receipt_entries(path)
        entry = {
            "cmd": command,
            "exit": int(exit_code),
            "ts": _now_iso(),
            "project": _receipt_project(project),
            "effect": effect if type(effect) is int else None,
        }
        if isinstance(metrics, dict):
            entry["metrics"] = metrics
        entries.append(entry)
        # Cap AFTER appending, keeping the most recent: same-session re-runs
        # append to one file, so an uncapped list grows without bound.
        payload = {"session": session, "entries": entries[-RUN_RECEIPT_MAX_ENTRIES:]}
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    except Exception as exc:
        # One line, on stderr, and then out of the way. This is additional to
        # the single abort line a non-zero exit already prints; it never
        # replaces it.
        print(f"goals.py: run receipt not written: {exc}", file=sys.stderr)


# Subcommands that must leave NO run receipt, whatever `--session` they carry.
# `--session` on a claim identifies the session HOLDING an outcome, not a goal
# step, so a claim has no business in the receipt window: `validate_report.py`
# accepts a report only if that window still contains a `commit` or
# `parse-extraction` entry, and `_record_run` keeps only the last
# `RUN_RECEIPT_MAX_ENTRIES`. A sitting that re-claims to refresh its heartbeat --
# the documented pattern, `outcomes.CLAIM_EXPIRY_MS` is four hours -- otherwise
# evicts its own goal step and gets a correct report rejected.
#
# Every outcome/pendings subcommand is listed, not only the two that define
# `--session` today: `getattr` yields `None` for the rest, so those entries are
# inert now and are the difference between a safe and an unsafe flag addition
# later. Raising the cap instead would treat the symptom -- the cap is what keeps
# an append-only-across-runs receipt bounded.
NO_RECEIPT_COMMANDS = frozenset({
    "outcome-create", "outcome-list", "outcome-attach", "outcome-advance",
    "outcome-status", "outcome-note", "outcome-claim", "outcome-release",
    "pendings-ingest", "pendings-set", "pendings-next",
})


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Reassigns the module globals `LEDGER_DIR`/`LOCK_DIR` from the
    environment for the duration of this call, then restores whatever they
    were before. This is what lets `test_cli_exit_code_reaches_the_shell`
    isolate a real subprocess to a test's scratch directory via
    `GOALS_LEDGER_DIR`/`GOALS_LOCK_DIR` — a real process has no other way to
    inherit a test's patched paths. It is a no-op for any caller that
    doesn't set the env var: `os.environ.get(name, LEDGER_DIR)` then falls
    back to whatever `LEDGER_DIR` already is, so a library caller that
    patches `goals.LEDGER_DIR` directly (every test in this suite that does
    not go through a subprocess) is never overwritten by an in-process call
    to `main()`. The restore in `finally` matters for a caller that DOES set
    the env var and calls `main()` in-process (embedding script, a future
    test, a REPL): without it, that one call would permanently repoint
    every other function's `LEDGER_DIR` for the rest of the process, and a
    later unrelated `cmd_commit(...)` call in the same process would
    silently write to the wrong ledger.

    `RUN_DIR` (receipts, `GOALS_RUN_DIR`) is repointed and restored by exactly
    the same mechanism, for exactly the same reason.
    """
    global LEDGER_DIR, LOCK_DIR, RUN_DIR
    prev_ledger_dir, prev_lock_dir, prev_run_dir = LEDGER_DIR, LOCK_DIR, RUN_DIR
    LEDGER_DIR = Path(os.environ.get("GOALS_LEDGER_DIR", LEDGER_DIR))
    LOCK_DIR = Path(os.environ.get("GOALS_LOCK_DIR", LOCK_DIR))
    RUN_DIR = Path(os.environ.get("GOALS_RUN_DIR", RUN_DIR))
    try:
        parser = argparse.ArgumentParser(
            prog="goals.py", description="Read and write /progress-report's per-project goal ledger.")
        subs = parser.add_subparsers(dest="command", required=True)

        p_load = subs.add_parser("load", help="load this session's owned/claimable open goals")
        p_load.add_argument("--project", required=True, help="project name (any string; slugified internally)")
        p_load.add_argument("--session", required=True, help="this session's id (any plain token)")
        p_load.add_argument("--read-only", action="store_true",
                             help="never write the ledger: skip the autopark persist and report "
                                  "the pending parks as `would_park` instead. Required on --dry-run.")

        p_commit = subs.add_parser("commit", help="apply a decision batch to the ledger")
        p_commit.add_argument("--project", required=True, help="project name (any string; slugified internally)")
        p_commit.add_argument("--session", required=True, help="this session's id (any plain token)")
        p_commit.add_argument("--decisions", required=True,
                               help="path to a JSON file: "
                                    '{"session": "...", "decisions": [{"op": "create|retext|status|seen", ...}]}')

        p_render = subs.add_parser("render", help="regenerate the derived markdown mirror")
        p_render.add_argument("--project", required=True, help="project name (any string; slugified internally)")
        p_render.add_argument("--out", required=True, help="path to write the rendered markdown file to")

        p_dashboard = subs.add_parser(
            "dashboard", help="regenerate the cross-project Goals & To-Do page")
        p_dashboard.add_argument("--out", required=True,
                                 help="path to the dashboard page (sentinel-spliced, not overwritten)")

        # A FIFTH SUBCOMMAND rather than a flag on an existing one. `load`,
        # `commit` and `render` all touch the ledger, and a thin session must not
        # — hanging the skip off any of them would put the ledger one forgotten
        # branch away from a run that promises never to reach it. It also gives
        # the receipt its own `cmd` value, which is what lets the validator ask
        # "did THIS run record a thin skip" instead of inferring it from the
        # absence of something else. The cost is one more parser; every other
        # shape costs a conditional inside a ledger-writing path.
        p_thin = subs.add_parser(
            THIN_SESSION_CMD,
            help="record that this run skipped the goal step as a thin session (no ledger write)")
        p_thin.add_argument("--project", required=True,
                            help="project name (any string; slugified internally)")
        p_thin.add_argument("--session", required=True, help="this session's id (any plain token)")
        # `type=int` and nothing more. A stricter type function could reject a
        # negative count, but any rejection here becomes an `Abort` with a
        # non-zero exit recorded against THIS project — and `validate_report.py`
        # accepts any such entry as corroboration for a `ledger-error`
        # declaration, so a guard added here would open a cheaper path into a
        # different declaration than the one it closed. argparse's own type
        # failure is safe (it exits before a receipt is written at all), but a
        # value that parses as an integer and fails the thin condition is the
        # validator's to refuse, not this command's.
        p_thin.add_argument("--event-count", required=True, type=int,
                            help="the digest's `.event_count` for this session, passed through "
                                 "verbatim: recorded on the receipt and compared against the "
                                 "thin threshold by validate_report.py")
        p_thin.add_argument("--active-msg-count", required=True, type=int,
                            help="the digest's `.active_msg_count` for this session, on exactly "
                                 "the same terms as --event-count")

        p_parse = subs.add_parser("parse-extraction", help="parse a goal-extractor agent's raw return block")
        p_parse.add_argument("--file", required=True,
                              help="path to a text file containing one <<<GOAL_EXTRACTION>>>...<<<END>>> block")
        p_parse.add_argument("--session", default=None,
                              help="this session's id; used ONLY to record a run receipt. Optional here "
                                   "(this subcommand needs no session to do its work), but /progress-report "
                                   "must pass it — see SKILL.md Step 4b.")
        p_parse.add_argument("--project", default=None,
                              help="project name; used ONLY to record a run receipt, on exactly the same "
                                   "terms as --session. Nothing in the parse reads it and the returned "
                                   "payload is identical with or without it. Optional here, but "
                                   "/progress-report must pass it — see SKILL.md Step 4b: without it the "
                                   "entry records no project, and a counts declaration checked against a "
                                   "project-less entry is rejected in Step 6.")

        p_oc = subs.add_parser("outcome-create",
                               help="create a cross-project outcome")
        p_oc.add_argument("--statement", required=True,
                          help="one user-visible sentence: what is true when this is done")
        p_oc.add_argument("--projects", required=True,
                          help="comma-separated project names this outcome spans")

        p_ol = subs.add_parser("outcome-list", help="list outcomes")
        p_ol.add_argument("--station", default=None,
                          help="filter: captured|specced|planned|executing|verifying|closed")
        p_ol.add_argument("--status", default=None,
                          help="filter: open|active|blocked|parked|done")

        p_oa = subs.add_parser("outcome-attach",
                               help="link a per-project goal to an outcome")
        p_oa.add_argument("--outcome", required=True, help="outcome-NNNN")
        p_oa.add_argument("--project", required=True, help="project name")
        p_oa.add_argument("--goal", required=True, help="goal id, g-NNNN")

        p_oadv = subs.add_parser("outcome-advance",
                                 help="move an outcome to the next station")
        p_oadv.add_argument("--outcome", required=True, help="outcome-NNNN")
        p_oadv.add_argument("--to", default=None,
                            help="target station; omit to step forward by one")
        p_oadv.add_argument("--reopen", action="store_true",
                            help="required to move an outcome backwards")

        p_ost = subs.add_parser("outcome-status", help="set an outcome's status")
        p_ost.add_argument("--outcome", required=True, help="outcome-NNNN")
        p_ost.add_argument("--status", required=True,
                           help="open|active|blocked|parked|done")
        p_ost.add_argument("--wake", default=None,
                           help="required with --status parked: what reactivates it")

        p_onote = subs.add_parser(
            "outcome-note",
            help="append one free-text, timestamped entry to an outcome's history")
        p_onote.add_argument("--outcome", required=True, help="outcome-NNNN")
        p_onote.add_argument("--text", required=True,
                             help="the note itself; one or more sentences")
        p_onote.add_argument("--event", default="note",
                             help="note (a person wrote it) or session-end (the "
                                  "SessionEnd hook recorded where it stood)")

        p_ocl = subs.add_parser("outcome-claim",
                                help="claim an outcome for this session")
        p_ocl.add_argument("--outcome", required=True, help="outcome-NNNN")
        p_ocl.add_argument("--session", required=True, help="this session's id")

        p_orl = subs.add_parser("outcome-release", help="release a claim")
        p_orl.add_argument("--outcome", required=True, help="outcome-NNNN")
        p_orl.add_argument("--session", required=True, help="this session's id")
        p_orl.add_argument("--steal", action="store_true",
                           help="force-release another session's live claim "
                                "(recorded in the outcome's history)")

        p_pi = subs.add_parser(
            "pendings-ingest",
            help="read a spec's PENDINGS fence into an outcome")
        p_pi.add_argument("--outcome", required=True, help="outcome-NNNN")
        p_pi.add_argument("--spec", required=True,
                          help="path to the spec file carrying the fenced block")

        p_ps = subs.add_parser(
            "pendings-set",
            help="set one row's status in the spec fence AND the ledger")
        p_ps.add_argument("--outcome", required=True, help="outcome-NNNN")
        p_ps.add_argument("--spec", required=True,
                          help="path to the spec file carrying the fenced block")
        p_ps.add_argument("--row", required=True, help="row id, PNN")
        p_ps.add_argument("--status", required=True,
                          help="open|active|blocked|done")

        p_pn = subs.add_parser("pendings-next",
                               help="print the single next unblocked pending")
        p_pn.add_argument("--outcome", required=True, help="outcome-NNNN")

        args = parser.parse_args(argv)

        # Everything below is bound only after `parse_args` has succeeded. A
        # bad-args `SystemExit` must not reach a `finally` that reads a name
        # argparse never got far enough to bind — that would escape as an
        # `UnboundLocalError` and break the "nothing but an `Abort` escapes"
        # contract at the one boundary that exists to uphold it.
        receipt_session = (None if args.command in NO_RECEIPT_COMMANDS
                           else getattr(args, "session", None))
        # `--project` is REQUIRED on `load`/`commit`/`render`, which need it to
        # do their work, and OPTIONAL on `parse-extraction`, which does not.
        # This carried the opposite instruction for one round — "`parse-extraction`
        # has no `--project` and must not gain one" — and that left the
        # validator's `proposed` half bound to neither project nor time, so any
        # earlier entry in the session validated any later report whose count
        # happened to coincide. The reasoning was right about a REQUIRED
        # argument and wrong about an optional one: `--session` is already
        # exactly this shape, doing nothing for the parse and existing only so
        # the run leaves a receipt, and `--project` joins it on those terms.
        # Nothing in `parse_extraction` reads either. `getattr` still yields
        # `None` when the flag is omitted, which the receipt records as
        # `project: null` and the validator's counts check refuses to match.
        receipt_project = getattr(args, "project", None)
        # Any escape that is neither a clean return nor an `Abort` reaches the
        # shell as an uncaught exception, which exits 1 — so 1 is the honest
        # default for a receipt written on that path.
        exit_code = 1
        # Bound BEFORE the try, for the same reason `exit_code` is: the
        # `finally` below runs on every path, including one that aborted before
        # a result existed, and a name assigned only inside the try would
        # escape from there as an `UnboundLocalError` — breaking the "nothing
        # but an `Abort` escapes" contract this boundary exists to uphold.
        # `None` is the honest value for a run that produced no effect.
        receipt_effect = None
        # Same pre-binding, same reason. `None` here means "this run recorded no
        # metrics", which every command except `thin-session` legitimately is —
        # and which a `thin-session` that aborted before returning also is.
        receipt_metrics = None
        try:
            if args.command == "load":
                result = cmd_load(args.project, args.session, read_only=args.read_only)
            elif args.command == "commit":
                result = cmd_commit(args.project, args.session, args.decisions)
                # The value the command actually returned, read straight off
                # its own result — not a re-count of the decisions file, which
                # would be a second derivation of the number the validator
                # compares against, free to disagree with the ledger's.
                receipt_effect = result["applied"]
            elif args.command == "render":
                result = cmd_render(args.project, args.out)
            elif args.command == "dashboard":
                # This branch is not optional bookkeeping: the chain's `else`
                # falls through to parse-extraction, so a missing branch would
                # not raise — it would silently run the wrong command.
                result = cmd_dashboard(args.out)
            elif args.command == THIN_SESSION_CMD:
                result = cmd_thin_session(args.project, args.event_count, args.active_msg_count)
                # The values the CALLER passed, read straight off the parsed
                # arguments — never re-derived from the result, and never
                # recomputed here. Bound only after `cmd_thin_session` returned,
                # so a run that aborted on an unusable project records no metrics
                # and cannot justify a `thin-session` declaration.
                receipt_metrics = {"event_count": args.event_count,
                                   "active_msg_count": args.active_msg_count}
            elif args.command == "outcome-create":
                import outcomes
                result = outcomes.cmd_outcome_create(args.statement, args.projects)
                receipt_effect = 1
            elif args.command == "outcome-list":
                import outcomes
                result = outcomes.cmd_outcome_list(args.station, args.status)
            elif args.command == "outcome-attach":
                import outcomes
                result = outcomes.cmd_outcome_attach(args.outcome, args.project,
                                                     args.goal)
                receipt_effect = 1
            elif args.command == "outcome-advance":
                import outcomes
                result = outcomes.cmd_outcome_advance(args.outcome, args.to,
                                                      args.reopen)
                receipt_effect = 1
            elif args.command == "outcome-status":
                import outcomes
                result = outcomes.cmd_outcome_status(args.outcome, args.status,
                                                     args.wake)
                receipt_effect = 1
            elif args.command == "outcome-note":
                import outcomes
                result = outcomes.cmd_outcome_note(args.outcome, args.text,
                                                   args.event)
                receipt_effect = 1
            elif args.command == "outcome-claim":
                import outcomes
                result = outcomes.cmd_outcome_claim(args.outcome, args.session)
                receipt_effect = 1
            elif args.command == "outcome-release":
                import outcomes
                result = outcomes.cmd_outcome_release(args.outcome, args.session,
                                                      steal=args.steal)
                receipt_effect = 1
            elif args.command == "pendings-ingest":
                import outcomes
                result = outcomes.cmd_pendings_ingest(args.outcome, args.spec)
                receipt_effect = result["ingested"]
            elif args.command == "pendings-set":
                import outcomes
                result = outcomes.cmd_pendings_set(args.outcome, args.spec,
                                                   args.row, args.status)
                receipt_effect = 1
            elif args.command == "pendings-next":
                import outcomes
                result = outcomes.cmd_pendings_next(args.outcome)
            else:
                result = {"proposed": parse_extraction(_bounded_read_text(args.file, "extractor output file"))}
                receipt_effect = len(result["proposed"])
            print(json.dumps(result, indent=2, ensure_ascii=False))
            exit_code = 0
            return 0
        except Abort as abort:
            exit_code = abort.code
            raise
        finally:
            # On the way OUT, carrying the real exit code, on every path. This
            # `finally` is nested INSIDE the env-override restore below so the
            # receipt still lands in the `GOALS_RUN_DIR` this call was given.
            if receipt_session is not None:
                _record_run(receipt_session, args.command, exit_code,
                            receipt_project, receipt_effect, receipt_metrics)
    finally:
        LEDGER_DIR, LOCK_DIR, RUN_DIR = prev_ledger_dir, prev_lock_dir, prev_run_dir


if __name__ == "__main__":
    # outcomes.py does `import goals`. Without this line that import loads goals.py
    # a SECOND time, and the copy never sees main()'s GOALS_LEDGER_DIR repoint - so
    # every test subprocess would write the real ~/.claude/goal-ledger. Aliasing the
    # running module makes `import goals` return this one. Do not remove.
    sys.modules.setdefault("goals", sys.modules["__main__"])
    try:
        sys.exit(main())
    except Abort as abort:
        print(abort.message, file=sys.stderr)
        sys.exit(abort.code)
