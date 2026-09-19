import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goals  # noqa: E402


class LedgerTestCase(unittest.TestCase):
    """Redirects goals.py at a scratch dir so no real ledger is ever touched."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="goals-test-"))
        self._orig_ledger = goals.LEDGER_DIR
        self._orig_lock = goals.LOCK_DIR
        self._orig_run = goals.RUN_DIR
        goals.LEDGER_DIR = self.tmp / "goal-ledger"
        goals.LOCK_DIR = self.tmp / "cache"
        # Redirected for the same reason as the other two, and not only for the
        # receipt tests: any test that reaches `main()` in-process writes a
        # receipt on its way out, and the default RUN_DIR is Pedro's live
        # ~/.claude/cache/goal-run.
        goals.RUN_DIR = self.tmp / "goal-run"
        goals.LEDGER_DIR.mkdir(parents=True)
        goals.LOCK_DIR.mkdir(parents=True)
        # Registered first, so LIFO runs it LAST — after any worker reaper a test
        # adds later. tearDown runs before doCleanups, so removing the tree there
        # would race a still-live worker that recreates its lock directory.
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def tearDown(self) -> None:
        goals.LEDGER_DIR = self._orig_ledger
        goals.LOCK_DIR = self._orig_lock
        goals.RUN_DIR = self._orig_run


class TestLedgerCore(LedgerTestCase):
    def test_read_missing_ledger_returns_empty_schema_1(self) -> None:
        data = goals.read_ledger("cucina-inventory", "Cucina Inventory")
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["project"], "Cucina Inventory")
        self.assertEqual(data["goals"], [])

    def test_write_then_read_round_trips(self) -> None:
        data = goals.read_ledger("demo", "Demo")
        data["goals"].append({"id": "g-0001", "text": "ship it"})
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        again = goals.read_ledger("demo", "Demo")
        self.assertEqual(again["goals"][0]["text"], "ship it")

    def test_lock_path_is_never_the_ledger_path(self) -> None:
        self.assertNotEqual(goals.lock_path("demo"), goals.ledger_path("demo"))

    def test_schema_from_the_future_is_refused(self) -> None:
        goals.ledger_path("demo").write_text(
            json.dumps({"schema": 99, "project": "Demo", "goals": []}), encoding="utf-8"
        )
        with self.assertRaises(goals.Abort) as ctx:
            goals.read_ledger("demo", "Demo")
        self.assertEqual(ctx.exception.code, 2)

    def test_second_holder_times_out_with_exit_3(self) -> None:
        lock = goals.lock_path("demo")
        # Simulate a lock held by another process: grant it directly, bypassing
        # this process's _HELD bookkeeping, so ledger_lock() below hits real
        # mkdir contention instead of the same-process re-entrancy guard.
        self.assertIsNotNone(goals._try_grant(lock))
        saved_cap = goals.LOCK_CAP_MS
        goals.LOCK_CAP_MS = 200  # keep the test fast; restored below
        try:
            with self.assertRaises(goals.Abort) as ctx:
                with goals.ledger_lock("demo"):
                    pass
        finally:
            goals.LOCK_CAP_MS = saved_cap
        self.assertEqual(ctx.exception.code, 3)

    def test_write_without_the_lock_exits_5(self) -> None:
        data = goals.read_ledger("demo", "Demo")
        with self.assertRaises(goals.Abort) as ctx:
            goals.write_ledger("demo", data)
        self.assertEqual(ctx.exception.code, 5)
        self.assertFalse(goals.ledger_path("demo").exists())

    def test_write_after_the_lock_was_stolen_exits_5(self) -> None:
        data = goals.read_ledger("demo", "Demo")
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
            before = goals.ledger_path("demo").read_bytes()
            # Simulate an eviction followed by another process re-granting itself.
            (goals.lock_path("demo") / "owner").write_text(
                "999999-deadbeef", encoding="utf-8")
            data["goals"].append({"id": "g-lost", "text": "must not land"})
            with self.assertRaises(goals.Abort) as ctx:
                goals.write_ledger("demo", data)
        self.assertEqual(ctx.exception.code, 5)
        self.assertEqual(goals.ledger_path("demo").read_bytes(), before)

    def test_lock_older_than_abandon_ms_is_evicted_even_with_a_live_owner(self) -> None:
        lock = goals.lock_path("demo")
        lock.mkdir(parents=True)
        # Our own PID: unambiguously alive, so only the age backstop can free this.
        (lock / "owner").write_text(f"{os.getpid()}-cafebabe", encoding="utf-8")
        stale = time.time() - (goals.LOCK_ABANDON_MS / 1000.0) - 60
        os.utime(lock, (stale, stale))
        with goals.ledger_lock("demo"):
            self.assertEqual(
                (goals.lock_path("demo") / "owner").read_text(encoding="utf-8").split("-")[0],
                str(os.getpid()),
            )

    def test_lock_is_released_on_normal_exit(self) -> None:
        with goals.ledger_lock("demo"):
            self.assertTrue(goals.lock_path("demo").exists())
        self.assertFalse(goals.lock_path("demo").exists())

    def test_lock_is_released_when_the_body_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            with goals.ledger_lock("demo"):
                raise RuntimeError("boom")
        self.assertFalse(goals.lock_path("demo").exists())

    def test_a_non_positive_pid_is_never_treated_as_alive(self) -> None:
        # os.kill(0, 0) checks the caller's whole process group and os.kill(-1, 0)
        # every signalable process, so both would report "alive" and wedge the lock.
        self.assertFalse(goals._pid_alive(0))
        self.assertFalse(goals._pid_alive(-1))

    def test_an_unusable_ledger_directory_exits_4(self) -> None:
        goals.LEDGER_DIR = self.tmp / "blocked" / "goal-ledger"
        (self.tmp / "blocked").write_text("not a directory", encoding="utf-8")
        with goals.ledger_lock("demo"):
            with self.assertRaises(goals.Abort) as ctx:
                goals.write_ledger("demo", {"schema": 1, "project": "Demo", "goals": []})
        self.assertEqual(ctx.exception.code, 4)

    def test_a_lone_surrogate_in_ledger_data_aborts_4_not_a_bare_unicode_error(self) -> None:
        # Real, reachable path (not hypothetical): apply_decisions's
        # create/retext branches only .strip() incoming text, so a
        # \ud800-range escape can land in ledger data without ever going
        # through parse_extraction's guard, and json.dumps(..., encoding=
        # "utf-8") raises UnicodeEncodeError — a ValueError subclass a bare
        # `except OSError` never catches. Proven directly against
        # write_ledger, independent of any caller.
        data = {"schema": 1, "project": "Demo", "goals": [
            {"id": "g-1", "level": "goal", "parent": None, "status": "open",
             "text": "surrogate: \ud800 here"},
        ]}
        with goals.ledger_lock("demo"):
            with self.assertRaises(goals.Abort) as ctx:
                goals.write_ledger("demo", data)
        self.assertEqual(ctx.exception.code, 4)
        self.assertIn("ledger write failed", ctx.exception.message)
        leftover = list(goals.LEDGER_DIR.glob(".*.tmp-*"))
        self.assertEqual(leftover, [], f"leftover tmp file(s): {leftover}")
        self.assertFalse(goals.ledger_path("demo").exists())

    def test_lock_held_by_a_dead_pid_is_evicted_immediately(self) -> None:
        lock = goals.lock_path("demo")
        lock.mkdir(parents=True)
        # A PID that cannot be running: os.kill(pid, 0) raises ProcessLookupError.
        (lock / "owner").write_text("999999-deadbeef", encoding="utf-8")
        started = time.monotonic()
        with goals.ledger_lock("demo"):
            pass
        self.assertLess(time.monotonic() - started, 1.0,
                        "a dead owner must be evicted immediately, not waited out")

    def test_a_non_numeric_schema_exits_2(self) -> None:
        goals.ledger_path("demo").write_text(
            json.dumps({"schema": "abc", "project": "Demo", "goals": []}), encoding="utf-8")
        with self.assertRaises(goals.Abort) as ctx:
            goals.read_ledger("demo", "Demo")
        self.assertEqual(ctx.exception.code, 2)

    def test_a_failed_owner_write_leaves_no_ownerless_lock(self) -> None:
        lock = goals.lock_path("demo")
        original = goals.Path.write_text

        def refuse(self, *args, **kwargs):          # only the owner file fails
            if self.name == "owner":
                raise OSError(28, "No space left on device")
            return original(self, *args, **kwargs)

        goals.Path.write_text = refuse
        try:
            with self.assertRaises(goals.Abort) as ctx:
                with goals.ledger_lock("demo"):
                    pass
        finally:
            goals.Path.write_text = original
        self.assertEqual(ctx.exception.code, 4)
        self.assertFalse(lock.exists(), "a lock with no owner would wedge the ledger for an hour")

    def test_a_path_traversal_slug_is_rejected(self) -> None:
        with self.assertRaises(goals.Abort) as ctx:
            goals.ledger_path("../../etc/x")
        self.assertEqual(ctx.exception.code, 2)

    def test_a_slug_with_a_trailing_newline_is_rejected(self) -> None:
        # `$` in `_SLUG_RE` matches before a trailing "\n" too, so `.match()`
        # let "demo\n" through as if it were the plain slug "demo".
        with self.assertRaises(goals.Abort) as ctx:
            goals.ledger_path("demo\n")
        self.assertEqual(ctx.exception.code, 2)

    def test_ledger_lock_is_not_reentrant(self) -> None:
        with goals.ledger_lock("demo"):
            with self.assertRaises(goals.Abort) as ctx:
                with goals.ledger_lock("demo"):
                    pass
        self.assertEqual(ctx.exception.code, 2)

    def test_a_real_fault_during_eviction_exits_4_not_3(self) -> None:
        # A dead-PID owner makes _evict_if_dead decide to evict immediately, so this
        # reaches the rename() call on the first poll instead of waiting out the cap.
        lock = goals.lock_path("demo")
        lock.mkdir(parents=True)
        (lock / "owner").write_text("999999-deadbeef", encoding="utf-8")
        original_rename = goals.Path.rename

        def refuse(self, *args, **kwargs):           # only the lock's own rename faults
            if self == lock:
                raise OSError(errno.EACCES, "Permission denied")
            return original_rename(self, *args, **kwargs)

        goals.Path.rename = refuse
        try:
            with self.assertRaises(goals.Abort) as ctx:
                with goals.ledger_lock("demo"):
                    pass
        finally:
            goals.Path.rename = original_rename
        self.assertEqual(ctx.exception.code, 4,
                          "a real fault during eviction must fail loud (4), not be "
                          "mistaken for a race and reported as lock contention (3)")


WORKER = textwrap.dedent(
    """
    import json, os, sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    import goals
    goals.LEDGER_DIR = Path(sys.argv[2])
    goals.LOCK_DIR = Path(sys.argv[3])
    goal_id = sys.argv[4]
    gate = Path(sys.argv[5])

    # Barrier. Both workers contend from the same instant, so their critical
    # sections genuinely overlap. Without it, a slow interpreter start lets one
    # worker finish before the other begins — and a completely broken lock then
    # passes this test, which was confirmed empirically with a 0.5s stagger.
    (gate.parent / ("ready-" + goal_id)).write_text("1", encoding="utf-8")
    deadline = time.monotonic() + 60
    while not gate.exists():
        if time.monotonic() > deadline:
            sys.exit("barrier never opened")
        time.sleep(0.01)

    with goals.ledger_lock("demo"):
        data = goals.read_ledger("demo", "Demo")
        time.sleep(0.30)                      # widen the window a lost update needs
        data["goals"].append({"id": goal_id, "text": goal_id})
        goals.write_ledger("demo", data)
    """
)

HANG = """
import sys, time
from pathlib import Path
sys.path.insert(0, {scripts!r})
import goals
goals.LEDGER_DIR = Path({ledger!r})
goals.LOCK_DIR = Path({lock!r})
assert goals._try_grant(goals.lock_path("demo")), "worker failed to take the lock"
time.sleep(60)
"""


class TestMinorHardening(LedgerTestCase):
    def test_a_three_deep_batch_local_chain_is_rejected(self) -> None:
        # M-1. `apply_decisions` enforces "two levels only" against REAL
        # ledger parents but applied no level check when the parent was a
        # batch-local token, so `goal -> subgoal -> subgoal` applied cleanly
        # and the grandchild then rendered under `## Unparented sub-goals` —
        # a heading that is simply wrong, since its parent exists and is not
        # parked. Rejecting it in the validate phase keeps the ledger's
        # two-level invariant true rather than mislabelling the result.
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        decisions = [
            {"op": "create", "id": "new-1", "level": "goal", "parent": None, "text": "Top"},
            {"op": "create", "id": "new-2", "level": "subgoal", "parent": "new-1", "text": "Mid"},
            {"op": "create", "id": "new-3", "level": "subgoal", "parent": "new-2", "text": "Deep"},
        ]
        with self.assertRaises(goals.Abort) as ctx:
            goals.apply_decisions(data, "sess-a", decisions, NOW)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("decision[2]", ctx.exception.message)
        self.assertIn("two levels", ctx.exception.message)
        self.assertEqual(data["goals"], [], "the batch must apply nothing at all")

    def test_a_two_level_batch_local_chain_still_applies(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        applied = goals.apply_decisions(data, "sess-a", [
            {"op": "create", "id": "new-1", "level": "goal", "parent": None, "text": "Top"},
            {"op": "create", "id": "new-2", "level": "subgoal", "parent": "new-1", "text": "Child"},
        ], NOW)
        self.assertEqual(applied, 2)
        self.assertEqual(data["goals"][1]["parent"], data["goals"][0]["id"])

    def test_stored_text_is_collapsed_to_one_line_at_ingestion(self) -> None:
        # M-6. `create`/`retext` only `.strip()`, so a newline survived into
        # the ledger. `goals.py` collapses it at render via `_render_line`,
        # but the report builder's literal `- [ ] <text>` rule has no
        # sanitiser and would emit a second, forged bullet. Closing it at
        # ingestion fixes every consumer at once, including ones that do not
        # exist yet.
        #
        # Reachability is narrow — `_has_disallowed_display_chars` rejects
        # `\n` as a Cc character at parse, so the extractor path is already
        # closed; the routes in are Pedro's own free-text correction at the
        # gate and a hand-edited ledger.
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        goals.apply_decisions(data, "sess-a", [
            {"op": "create", "level": "goal", "parent": None,
             "text": "Fix login\n- [ ] FORGED SECOND BULLET"},
        ], NOW)
        self.assertEqual(data["goals"][0]["text"], "Fix login - [ ] FORGED SECOND BULLET")

    def test_retext_also_collapses_whitespace(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [seed("g-1111")]}
        goals.apply_decisions(data, "sess-a", [
            {"op": "retext", "id": "g-1111", "text": "New\ttext\n\nhere"},
        ], NOW)
        self.assertEqual(data["goals"][0]["text"], "New text here")

    def test_two_projects_agreeing_in_60_slug_chars_get_distinct_ledgers(self) -> None:
        # M-7. `slugify` truncated at 60 characters with nothing to
        # distinguish the originals, so two projects whose paths agree that
        # far shared ONE ledger file — silently merging two projects' goals.
        # Free to change today because no ledger exists on this machine yet;
        # expensive once one does.
        base = "a-very-long-project-name-that-keeps-going-and-going-and-goes"
        self.assertEqual(len(base), 60)
        first = goals.slugify(base + "-alpha")
        second = goals.slugify(base + "-beta")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith(base), "the readable prefix must survive")
        goals.ledger_path(first)   # must still satisfy _check_slug
        goals.ledger_path(second)

    def test_a_short_project_name_keeps_a_clean_unsuffixed_slug(self) -> None:
        # The hash is appended ONLY when truncation actually discarded
        # something. Suffixing every project would make every ledger filename
        # unreadable to buy nothing — a slug at or under the cap is already
        # unambiguous, and no suffixed slug (61+ chars) can collide with one.
        self.assertEqual(goals.slugify("Cucina Inventory"), "cucina-inventory")

    def test_slugify_is_stable_across_calls(self) -> None:
        long_name = "z" * 80
        self.assertEqual(goals.slugify(long_name), goals.slugify(long_name))


class TestPidLiveness(LedgerTestCase):
    def test_an_out_of_range_pid_is_not_alive(self) -> None:
        # `os.kill` raises OverflowError for a PID outside C `long` range.
        # OverflowError is an ArithmeticError, NOT an OSError, so neither the
        # ProcessLookupError nor the PermissionError handler caught it and it
        # escaped as a raw traceback — violating this module's "nothing but
        # an Abort ever escapes" contract, with no exit-code discipline and no
        # single-stderr-line guarantee.
        #
        # Fourth instance of the wrong-exception-family class on this branch;
        # the first three were UnicodeDecodeError/UnicodeEncodeError under an
        # `except OSError`. `_parse_iso` was probed for the same gap at both
        # datetime extremes (year 0001 and 9999, with and without an offset)
        # and does NOT raise OverflowError on this platform, so it is left
        # alone rather than given a speculative handler.
        self.assertIs(goals._pid_alive(2 ** 63), False)
        self.assertIs(goals._pid_alive(-(2 ** 63)), False)

    def test_a_lock_owner_with_an_out_of_range_pid_is_evicted_not_a_traceback(self) -> None:
        # The reachable path: `_evict_if_dead` parses the owner file's PID with
        # `int()` (unbounded in Python) and hands it straight to `_pid_alive`.
        # A corrupted or hand-edited owner file therefore reaches the raising
        # call with a value `int()` accepted and `os.kill` cannot represent.
        lock = goals.lock_path("demo")
        lock.mkdir(parents=True)
        (lock / "owner").write_text(f"{2 ** 63}-deadbeef", encoding="utf-8")
        # No PID that large can be alive, so the lock is stale and must be
        # reclaimed — reached only if the OverflowError no longer escapes.
        with goals.ledger_lock("demo"):
            pass


class TestConcurrentCommit(LedgerTestCase):
    """The only tests here that run real OS processes. Two sequential in-process
    calls would pass with no lock at all, so they prove nothing about mutual
    exclusion; these do."""

    def _spawn(self, argv: list[str]) -> subprocess.Popen:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._reap, proc)
        return proc

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    def test_two_processes_both_land_their_goal(self) -> None:
        worker = self.tmp / "worker.py"
        worker.write_text(WORKER, encoding="utf-8")
        gate = self.tmp / "go"
        args = [str(SCRIPTS), str(goals.LEDGER_DIR), str(goals.LOCK_DIR)]
        procs = {
            gid: self._spawn([sys.executable, str(worker), *args, gid, str(gate)])
            for gid in ("g-aaaa", "g-bbbb")
        }

        deadline = time.monotonic() + 30
        while not all((self.tmp / f"ready-{gid}").exists() for gid in procs):
            for gid, proc in procs.items():
                if proc.poll() is not None:
                    self.fail(f"{gid} died before the barrier: {proc.stderr.read()}")
            self.assertLess(time.monotonic(), deadline,
                            "workers never reached the barrier; nothing was contended")
            time.sleep(0.01)
        gate.write_text("go", encoding="utf-8")

        for gid, proc in procs.items():
            _, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, f"{gid} exited {proc.returncode}: {err}")

        data = goals.read_ledger("demo", "Demo")
        ids = sorted(goal["id"] for goal in data["goals"])
        self.assertEqual(ids, ["g-aaaa", "g-bbbb"], "a lost update dropped one goal")

    def test_lock_is_released_when_a_holder_is_killed(self) -> None:
        hang = self.tmp / "hang.py"
        hang.write_text(
            HANG.format(scripts=str(SCRIPTS), ledger=str(goals.LEDGER_DIR),
                        lock=str(goals.LOCK_DIR)),
            encoding="utf-8",
        )
        proc = self._spawn([sys.executable, str(hang)])

        # Handshake, not a sleep: kill only once the child's own PID is in the
        # owner file. A fixed delay that fires before the grant kills a process
        # holding nothing, and the eviction path is never exercised.
        owner = goals.lock_path("demo") / "owner"
        deadline = time.monotonic() + 30
        while True:
            try:
                if owner.read_text(encoding="utf-8").strip().startswith(f"{proc.pid}-"):
                    break
            except OSError:
                pass
            if proc.poll() is not None:
                self.fail(f"worker died before taking the lock: {proc.stderr.read()}")
            self.assertLess(time.monotonic(), deadline,
                            "worker never took the lock; killing it would prove nothing")
            time.sleep(0.02)

        proc.kill()
        proc.wait(timeout=10)

        started = time.monotonic()
        with goals.ledger_lock("demo"):
            held = (goals.lock_path("demo") / "owner").read_text(encoding="utf-8")
        self.assertTrue(held.startswith(f"{os.getpid()}-"), "we did not end up owning the lock")
        self.assertLess(time.monotonic() - started, 1.0,
                        "a dead owner must be evicted immediately, not waited out to the cap")


NOW = "2026-08-12T19:00:00Z"


def seed(goal_id: str = "g-1111", **over) -> dict:
    base = {
        "id": goal_id,
        "text": "Fix login bug",
        "level": "goal",
        "parent": None,
        "status": "open",
        "owner_session": "sess-a",
        "origin": "extracted",
        "created": "2026-08-01T10:00:00Z",
        "last_seen": "2026-08-01T10:00:00Z",
        "history": [],
    }
    base.update(over)
    return base


class TestApplyDecisions(LedgerTestCase):
    def test_create_assigns_id_owner_and_timestamps(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        applied = goals.apply_decisions(
            data, "sess-a",
            [{"op": "create", "text": "Add auth tests", "level": "goal",
              "parent": None, "origin": "extracted"}],
            NOW,
        )
        self.assertEqual(applied, 1)
        goal = data["goals"][0]
        self.assertTrue(goal["id"].startswith("g-"))
        self.assertEqual(goal["owner_session"], "sess-a")
        self.assertEqual(goal["created"], NOW)
        self.assertEqual(goal["last_seen"], NOW)
        self.assertEqual(goal["history"], [])

    def test_retext_keeps_old_wording_in_history(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [seed()]}
        goals.apply_decisions(
            data, "sess-a",
            [{"op": "retext", "id": "g-1111", "text": "Fix the login redirect loop"}],
            NOW,
        )
        goal = data["goals"][0]
        self.assertEqual(goal["text"], "Fix the login redirect loop")
        self.assertEqual(goal["history"][0]["from"], "Fix login bug")
        self.assertEqual(goal["history"][0]["to"], "Fix the login redirect loop")
        self.assertEqual(goal["history"][0]["reason"], "approved-change")

    def test_status_change_is_recorded_in_history(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [seed()]}
        goals.apply_decisions(
            data, "sess-a", [{"op": "status", "id": "g-1111", "status": "done"}], NOW
        )
        self.assertEqual(data["goals"][0]["status"], "done")
        self.assertEqual(data["goals"][0]["history"][0]["to"], "done")

    def test_seen_bumps_last_seen_and_writes_no_history(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [seed()]}
        goals.apply_decisions(data, "sess-a", [{"op": "seen", "id": "g-1111"}], NOW)
        self.assertEqual(data["goals"][0]["last_seen"], NOW)
        self.assertEqual(data["goals"][0]["history"], [])

    def test_unknown_id_aborts_with_2_and_applies_nothing(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [seed()]}
        before = json.dumps(data, sort_keys=True)
        with self.assertRaises(goals.Abort) as ctx:
            goals.apply_decisions(
                data, "sess-a",
                [{"op": "seen", "id": "g-1111"},
                 {"op": "retext", "id": "g-nope", "text": "ghost"}],
                NOW,
            )
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(json.dumps(data, sort_keys=True), before,
                         "partial application is never allowed")

    def test_a_malformed_record_aborts_before_anything_is_applied(self) -> None:
        # The reviewer's reproduction: nothing references goal[1], so only the
        # whole-collection precondition can catch it — and it must catch it BEFORE
        # the retext lands.
        for broken in ({"id": ["weird"]}, "junk", {"id": 7}):
            with self.subTest(broken=broken):
                data = {"schema": 1, "project": "Demo", "updated": None,
                        "goals": [seed(), broken]}
                before = json.dumps(data, sort_keys=True, default=str)
                with self.assertRaises(goals.Abort) as ctx:
                    goals.apply_decisions(
                        data, "sess-a",
                        [{"op": "retext", "id": "g-1111", "text": "changed"},
                         {"op": "create", "text": "new", "level": "goal",
                          "parent": None, "origin": "extracted"}],
                        NOW,
                    )
                self.assertEqual(ctx.exception.code, 2)
                # M-8: `goals.py` has 70+ Abort(2) sites, so a code-only
                # assertion passes when the function aborts for entirely the
                # wrong reason. Pin the located message too.
                self.assertIn("ledger goal[1]", ctx.exception.message)
                self.assertEqual(json.dumps(data, sort_keys=True, default=str), before)

    def test_a_non_list_goals_collection_aborts(self) -> None:
        for bad in ({"a": 1}, None, "goals"):
            with self.subTest(bad=bad):
                data = {"schema": 1, "project": "Demo", "updated": None, "goals": bad}
                with self.assertRaises(goals.Abort) as ctx:
                    goals.apply_decisions(data, "sess-a", [], NOW)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("`goals` is not a list", ctx.exception.message)

    def test_a_non_dict_ledger_aborts(self) -> None:
        # apply_decisions is pure and callable directly; read_ledger's isinstance
        # check is a guarantee made outside this function's boundary.
        for bad in (None, [], "x", 7):
            with self.subTest(bad=bad):
                with self.assertRaises(goals.Abort) as ctx:
                    goals.apply_decisions(bad, "sess-a", [], NOW)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("ledger is not an object", ctx.exception.message)

    def test_a_non_list_decision_set_aborts(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [seed()]}
        for bad in (None, "seen", {"op": "seen"}):
            with self.subTest(bad=bad):
                with self.assertRaises(goals.Abort) as ctx:
                    goals.apply_decisions(data, "sess-a", bad, NOW)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("decisions is not a list", ctx.exception.message)

    def test_a_non_hashable_level_or_status_aborts(self) -> None:
        # Set membership on an unhashable value raises TypeError, not a clean Abort.
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [seed()]}
        cases = [
            ([{"op": "create", "text": "x", "level": ["goal"], "parent": None}],
             "create: bad level"),
            ([{"op": "status", "id": "g-1111", "status": ["done"]}],
             "status: bad status"),
        ]
        for decisions, expected in cases:
            with self.subTest(decisions=decisions):
                with self.assertRaises(goals.Abort) as ctx:
                    goals.apply_decisions(data, "sess-a", decisions, NOW)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn(expected, ctx.exception.message)

    def test_a_non_string_text_aborts_on_both_create_and_retext(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [seed()]}
        before = json.dumps(data, sort_keys=True)
        cases = [
            ([{"op": "create", "text": None, "level": "goal", "parent": None}],
             "create: text must be a string"),
            ([{"op": "create", "text": 42, "level": "goal", "parent": None}],
             "create: text must be a string"),
            ([{"op": "retext", "id": "g-1111", "text": None}],
             "retext: text must be a string"),
            ([{"op": "retext", "id": "g-1111", "text": ["a"]}],
             "retext: text must be a string"),
        ]
        for decisions, expected in cases:
            with self.subTest(decisions=decisions):
                with self.assertRaises(goals.Abort) as ctx:
                    goals.apply_decisions(data, "sess-a", decisions, NOW)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn(expected, ctx.exception.message)
        self.assertEqual(json.dumps(data, sort_keys=True), before)

    def test_a_target_record_missing_required_fields_aborts(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [{"id": "g-1111"}]}
        with self.assertRaises(goals.Abort) as ctx:
            goals.apply_decisions(
                data, "sess-a", [{"op": "retext", "id": "g-1111", "text": "x"}], NOW)
        self.assertEqual(ctx.exception.code, 2)

    def test_subgoal_with_missing_parent_aborts(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        with self.assertRaises(goals.Abort) as ctx:
            goals.apply_decisions(
                data, "sess-a",
                [{"op": "create", "text": "sub", "level": "subgoal",
                  "parent": "g-ghost", "origin": "extracted"}],
                NOW,
            )
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(data["goals"], [])

    def test_applying_the_same_decisions_twice_is_idempotent(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [seed()]}
        decisions = [{"op": "retext", "id": "g-1111", "text": "Fix the login redirect loop"}]
        goals.apply_decisions(data, "sess-a", decisions, NOW)
        first = json.dumps(data, sort_keys=True)
        goals.apply_decisions(data, "sess-a", decisions, NOW)
        self.assertEqual(json.dumps(data, sort_keys=True), first,
                         "a no-change retext must not append a second history entry")


class TestApplyDecisionsSameBatchParent(LedgerTestCase):
    """A `create` decision for a subgoal may name its parent's own
    batch-local `id` (a caller-chosen token, never a real ledger id — see
    `apply_decisions`'s docstring) so a session's very first run — empty
    ledger, everything `new` — can still create a goal and a subgoal under
    it in the same batch, instead of the subgoal only becoming possible a
    run later once its parent has a real id.
    """

    def test_first_run_batch_creates_a_goal_and_its_subgoal_together(self) -> None:
        # The motivating case: an empty ledger (first-ever run), one batch
        # proposing both a brand-new goal and a brand-new subgoal under it.
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        applied = goals.apply_decisions(
            data, "sess-a",
            [{"op": "create", "id": "new-goal", "level": "goal", "parent": None,
              "text": "Ship the feature", "origin": "extracted"},
             {"op": "create", "level": "subgoal", "parent": "new-goal",
              "text": "Write the tests", "origin": "extracted"}],
            NOW,
        )
        self.assertEqual(applied, 2)
        goal = next(g for g in data["goals"] if g["level"] == "goal")
        sub = next(g for g in data["goals"] if g["level"] == "subgoal")
        self.assertEqual(sub["parent"], goal["id"])
        self.assertNotEqual(
            sub["parent"], "new-goal",
            "the batch-local token must never leak into the ledger — only the "
            "real, ledger-assigned parent id may be written")

    def test_subgoal_may_reference_a_parent_declared_later_in_the_batch(self) -> None:
        # Order-independence: the subgoal's `create` comes BEFORE the parent's
        # own `create` in the decision list.
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        applied = goals.apply_decisions(
            data, "sess-a",
            [{"op": "create", "level": "subgoal", "parent": "new-goal",
              "text": "Write the tests"},
             {"op": "create", "id": "new-goal", "level": "goal", "parent": None,
              "text": "Ship the feature"}],
            NOW,
        )
        self.assertEqual(applied, 2)
        goal = next(g for g in data["goals"] if g["level"] == "goal")
        sub = next(g for g in data["goals"] if g["level"] == "subgoal")
        self.assertEqual(sub["parent"], goal["id"])

    def test_several_new_goals_route_each_subgoal_to_its_own_parent(self) -> None:
        # A batch creating up to MAX_GOALS new goals with subgoals under
        # DIFFERENT parents — the exact ambiguity a shared literal "new"
        # token could never resolve.
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        applied = goals.apply_decisions(
            data, "sess-a",
            [{"op": "create", "id": "a", "level": "goal", "parent": None, "text": "Goal A"},
             {"op": "create", "id": "b", "level": "goal", "parent": None, "text": "Goal B"},
             {"op": "create", "level": "subgoal", "parent": "a", "text": "Sub of A"},
             {"op": "create", "level": "subgoal", "parent": "b", "text": "Sub of B"}],
            NOW,
        )
        self.assertEqual(applied, 4)
        goal_a = next(g for g in data["goals"] if g["text"] == "Goal A")
        goal_b = next(g for g in data["goals"] if g["text"] == "Goal B")
        sub_a = next(g for g in data["goals"] if g["text"] == "Sub of A")
        sub_b = next(g for g in data["goals"] if g["text"] == "Sub of B")
        self.assertNotEqual(goal_a["id"], goal_b["id"])
        self.assertEqual(sub_a["parent"], goal_a["id"])
        self.assertEqual(sub_b["parent"], goal_b["id"])

    def test_a_subgoal_naming_another_subgoal_as_batch_parent_is_rejected(self) -> None:
        # Was `test_a_valid_multi_hop_same_batch_chain_is_not_mistaken_for_a_
        # cycle`, which asserted that subgoal-of-subgoal applies cleanly. M-1
        # reverses that: the two-level invariant was enforced against real
        # ledger parents but not against batch-local ones, so a three-deep
        # chain applied and the grandchild then rendered under `## Unparented
        # sub-goals` — a heading that is false, since its parent exists and is
        # not parked. The behaviour this test named is the defect.
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        with self.assertRaises(goals.Abort) as ctx:
            goals.apply_decisions(
                data, "sess-a",
                [{"op": "create", "id": "root", "level": "goal", "parent": None, "text": "Root"},
                 {"op": "create", "id": "mid", "level": "subgoal", "parent": "root", "text": "Mid"},
                 {"op": "create", "level": "subgoal", "parent": "mid", "text": "Leaf"}],
                NOW,
            )
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("two levels", ctx.exception.message)
        self.assertEqual(data["goals"], [])

    def test_find_batch_cycle_still_distinguishes_a_long_chain_from_a_cycle(self) -> None:
        # `_find_batch_cycle` is now unreachable through `apply_decisions`:
        # after M-1 every batch-local edge runs subgoal -> goal, and a goal
        # can never carry a batch-local parent, so no cycle is constructible.
        # It is kept as defense-in-depth against a future relaxation of the
        # level check, and tested directly so that guard does not rot into
        # untested code just because nothing currently reaches it.
        self.assertIsNone(goals._find_batch_cycle({1: 0, 2: 1, 3: 2}))
        self.assertIsNotNone(goals._find_batch_cycle({0: 1, 1: 0}))
        self.assertIsNotNone(goals._find_batch_cycle({0: 1, 1: 2, 2: 0}))
        self.assertIsNone(goals._find_batch_cycle({}))

    def test_subgoal_create_against_a_real_existing_parent_still_works(self) -> None:
        # Regression: a parent that's already a real ledger goal (no
        # batch-local id involved at all) must still resolve exactly as it
        # did before this feature existed.
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [seed("g-1111")]}
        applied = goals.apply_decisions(
            data, "sess-a",
            [{"op": "create", "level": "subgoal", "parent": "g-1111", "text": "Sub of existing"}],
            NOW,
        )
        self.assertEqual(applied, 1)
        sub = next(g for g in data["goals"] if g["level"] == "subgoal")
        self.assertEqual(sub["parent"], "g-1111")

    def test_subgoal_naming_itself_as_its_own_parent_aborts(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        with self.assertRaises(goals.Abort) as ctx:
            goals.apply_decisions(
                data, "sess-a",
                [{"op": "create", "id": "x", "level": "subgoal", "parent": "x", "text": "Loop"}],
                NOW,
            )
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("cannot name itself as its own parent", ctx.exception.message)
        self.assertEqual(data["goals"], [])

    def test_two_subgoals_naming_each_other_as_parent_abort(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        with self.assertRaises(goals.Abort) as ctx:
            goals.apply_decisions(
                data, "sess-a",
                [{"op": "create", "id": "x", "level": "subgoal", "parent": "y", "text": "X"},
                 {"op": "create", "id": "y", "level": "subgoal", "parent": "x", "text": "Y"}],
                NOW,
            )
        self.assertEqual(ctx.exception.code, 2)
        # Reaches the two-level check (M-1) before the cycle check now: both
        # decisions are subgoals naming a subgoal, which is rejected on its
        # own terms. The cycle itself is no longer constructible through this
        # entry point — see
        # `test_find_batch_cycle_still_distinguishes_a_long_chain_from_a_cycle`,
        # which keeps `_find_batch_cycle` under direct test.
        self.assertIn("two levels", ctx.exception.message)
        self.assertEqual(data["goals"], [])

    def test_forward_reference_to_a_parent_whose_own_creation_is_invalid_applies_nothing(self) -> None:
        # The specific corruption this feature must never allow: a subgoal
        # stages cleanly (its named parent's batch-local id is known), but
        # the parent's OWN `create` later fails its own validation (empty
        # text) — the whole batch must still abort with NOTHING written,
        # not a subgoal left pointing at a parent that was never created.
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        before = json.dumps(data, sort_keys=True)
        with self.assertRaises(goals.Abort) as ctx:
            goals.apply_decisions(
                data, "sess-a",
                [{"op": "create", "level": "subgoal", "parent": "bad-parent", "text": "Sub"},
                 {"op": "create", "id": "bad-parent", "level": "goal", "parent": None, "text": ""}],
                NOW,
            )
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("empty text", ctx.exception.message)
        self.assertEqual(json.dumps(data, sort_keys=True), before)

    def test_batch_id_colliding_with_a_real_ledger_id_aborts(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [seed("g-1111")]}
        before = json.dumps(data, sort_keys=True)
        with self.assertRaises(goals.Abort) as ctx:
            goals.apply_decisions(
                data, "sess-a",
                [{"op": "create", "id": "g-1111", "level": "goal", "parent": None,
                  "text": "Collide"}],
                NOW,
            )
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("collides with an existing ledger goal id", ctx.exception.message)
        self.assertEqual(json.dumps(data, sort_keys=True), before)

    def test_duplicate_batch_id_within_the_same_batch_aborts(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        with self.assertRaises(goals.Abort) as ctx:
            goals.apply_decisions(
                data, "sess-a",
                [{"op": "create", "id": "dup", "level": "goal", "parent": None, "text": "One"},
                 {"op": "create", "id": "dup", "level": "goal", "parent": None, "text": "Two"}],
                NOW,
            )
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("duplicates decision[0]'s id", ctx.exception.message)
        self.assertEqual(data["goals"], [])

    def test_non_string_or_empty_batch_id_aborts(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        for bad_id in (7, "", ["a"], {"a": 1}):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(goals.Abort) as ctx:
                    goals.apply_decisions(
                        data, "sess-a",
                        [{"op": "create", "id": bad_id, "level": "goal", "parent": None,
                          "text": "x"}],
                        NOW,
                    )
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("id must be a non-empty string when present",
                              ctx.exception.message)
        self.assertEqual(data["goals"], [])

    def test_non_string_parent_aborts_instead_of_a_bare_typeerror(self) -> None:
        # A dict/list `parent` would raise a bare TypeError on the
        # `parent in batch_ids` membership check without this guard —
        # unhashable types are never valid dict keys.
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        for bad_parent in (["g-1111"], {"id": "g-1111"}):
            with self.subTest(bad_parent=bad_parent):
                with self.assertRaises(goals.Abort) as ctx:
                    goals.apply_decisions(
                        data, "sess-a",
                        [{"op": "create", "level": "subgoal", "parent": bad_parent,
                          "text": "x"}],
                        NOW,
                    )
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("parent must be a string", ctx.exception.message)


class TestLoad(LedgerTestCase):
    # cmd_load has no clock-injection parameter (mandated signature: only
    # `project`/`session`), so it reads the wall clock through `_now_iso()`.
    # `seed()` hard-codes last_seen/created to a fixed calendar date, so any
    # test that drives autopark through cmd_load (instead of calling
    # autopark() directly with an explicit now_iso, as the boundary tests
    # below do) implicitly depends on how far the real "now" has drifted from
    # that fixed date — a time bomb: PARK_AFTER_DAYS days after the fixture
    # date, `cmd_load` starts auto-parking every fixture goal out from under
    # the owned/claimable/others assertions.
    #
    # Freezing `goals._now_iso()` for the duration of every TestLoad test
    # removes the real wall clock from the picture entirely. The frozen value
    # is chosen to reproduce the gap the fixtures were written against:
    # ~11 days after seed()'s default last_seen (2026-08-01), same as
    # test_load_persists_autopark_before_returning's explicit last_seen
    # (2026-07-20) is ~23 days before it, so that test's own auto-park
    # assertion still exercises a real park. Tests that call autopark()
    # directly with an explicit now_iso (test_goal_untouched_15_days_is_parked,
    # test_goal_untouched_13_days_is_not_parked) never go through _now_iso()
    # and keep exercising the real 14-day boundary untouched.
    FROZEN_NOW = "2026-08-12T10:00:00Z"

    def setUp(self) -> None:
        super().setUp()
        self._orig_projects = goals.PROJECTS_DIR
        goals.PROJECTS_DIR = self.tmp / "projects"
        (goals.PROJECTS_DIR / "-home-pedro").mkdir(parents=True)
        self._orig_now_iso = goals._now_iso
        goals._now_iso = lambda: self.FROZEN_NOW
        self.addCleanup(setattr, goals, "_now_iso", self._orig_now_iso)

    def tearDown(self) -> None:
        goals.PROJECTS_DIR = self._orig_projects
        super().tearDown()

    def _transcript(self, uuid: str, age_hours: float) -> None:
        path = goals.PROJECTS_DIR / "-home-pedro" / f"{uuid}.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        stamp = time.time() - age_hours * 3600
        os.utime(path, (stamp, stamp))

    def test_recent_transcript_means_session_is_live(self) -> None:
        self._transcript("sess-b", age_hours=1)
        self.assertTrue(goals.session_live("sess-b", time.time()))

    def test_old_transcript_means_session_is_not_live(self) -> None:
        self._transcript("sess-b", age_hours=48)
        self.assertFalse(goals.session_live("sess-b", time.time()))

    def test_missing_transcript_means_session_is_not_live(self) -> None:
        self.assertFalse(goals.session_live("sess-ghost", time.time()))

    def test_goal_untouched_15_days_is_parked(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed(last_seen="2026-07-28T10:00:00Z")]}
        parked = goals.autopark(data, "2026-08-12T10:00:00Z")
        self.assertEqual(parked, ["g-1111"])
        self.assertEqual(data["goals"][0]["status"], "parked")
        # Reversed from the original "auto-park writes no history" decision.
        # It is true that a park is housekeeping rather than a user decision,
        # but an unrecorded state change in a ledger whose whole purpose is
        # auditability is the same defect class as the silent decay this
        # feature exists to fight: afterwards the ledger says a goal IS
        # parked and nothing says when or why. `reason` distinguishes it from
        # an approved change, and `session` is null because no session
        # decided it.
        self.assertEqual(data["goals"][0]["history"], [{
            "ts": "2026-08-12T10:00:00Z", "from": "open", "to": "parked",
            "reason": "autopark-14d", "session": None,
        }])

    def test_autopark_still_parks_a_record_whose_history_is_malformed(self) -> None:
        # `_check_ledger_shape` only guarantees `id` is a string, so a
        # hand-edited ledger can hold a record with no `history` list at all
        # — and `goal["history"].append(...)` on it would raise a bare
        # KeyError/AttributeError straight out of a function documented to
        # fail only via Abort. The park is the durable, important half and
        # must survive; the history entry is best-effort.
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed(last_seen="2026-07-28T10:00:00Z", history="not-a-list")]}
        parked = goals.autopark(data, "2026-08-12T10:00:00Z")
        self.assertEqual(parked, ["g-1111"])
        self.assertEqual(data["goals"][0]["status"], "parked")
        self.assertEqual(data["goals"][0]["history"], "not-a-list",
                         "a malformed field is left as found, never silently rewritten")

    def test_goal_untouched_13_days_is_not_parked(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed(last_seen="2026-07-30T10:00:00Z")]}
        self.assertEqual(goals.autopark(data, "2026-08-12T10:00:00Z"), [])
        self.assertEqual(data["goals"][0]["status"], "open")

    def test_load_splits_owned_from_others_and_hides_parked(self) -> None:
        self._transcript("sess-b", age_hours=1)
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [
            seed("g-1111", owner_session="sess-a"),
            seed("g-2222", owner_session="sess-b"),
            seed("g-3333", owner_session=None, status="parked"),
        ]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual([g["id"] for g in out["owned"]], ["g-1111"])
        self.assertEqual(out["others"], 1, "others is a count, never a listing")

    def test_goal_owned_by_a_dead_session_becomes_claimable(self) -> None:
        self._transcript("sess-b", age_hours=48)
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed("g-2222", owner_session="sess-b")]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual([g["id"] for g in out["claimable"]], ["g-2222"])

    def test_an_open_goal_with_no_owner_is_claimable(self) -> None:
        # owner_session=None means "nobody has ever claimed this goal" — it
        # must be claimable, the same as one owned by a session that's gone
        # dead. Pinned per peer review: this fell out of session_live(None,
        # ...) returning False correctly, but wasn't itself documented/tested.
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed("g-1111", owner_session=None)]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual([g["id"] for g in out["claimable"]], ["g-1111"])

    # --- Regression tests below: each pins a defect found and fixed against
    # the plan's own sample code (or against this task's first-pass fix). ---

    def test_parse_iso_honors_an_explicit_non_utc_offset(self) -> None:
        # fromisoformat parses "+05:00" correctly; blindly overwriting tzinfo
        # with UTC afterward (the plan's sample code) silently shifts the
        # instant by 5 hours instead of preserving it.
        naive_utc = goals._parse_iso("2026-08-01T05:00:00Z")
        offset = goals._parse_iso("2026-08-01T10:00:00+05:00")
        self.assertEqual(offset, naive_utc)

    def test_parse_iso_treats_a_bare_naive_timestamp_as_utc(self) -> None:
        self.assertEqual(goals._parse_iso("2026-08-01T05:00:00"),
                          goals._parse_iso("2026-08-01T05:00:00Z"))

    def test_autopark_on_a_malformed_goal_record_aborts_cleanly(self) -> None:
        # The plan's sample code iterates data["goals"] and calls goal.get(...)
        # directly. A goal record that isn't a dict at all — a hand-edited or
        # partially-written ledger — must raise the documented Abort(2), not a
        # bare AttributeError.
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed(), "not-a-dict"]}
        with self.assertRaises(goals.Abort) as ctx:
            goals.autopark(data, "2026-08-12T10:00:00Z")
        self.assertEqual(ctx.exception.code, 2)

    def test_load_on_a_malformed_ledger_aborts_and_releases_the_lock(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": ["not-a-dict"]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        with self.assertRaises(goals.Abort) as ctx:
            goals.cmd_load("Demo", "sess-a")
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse(goals.lock_path("demo").exists(),
                         "an aborted load must not leave the lock held")

    def test_load_persists_autopark_before_returning(self) -> None:
        # Confirms the design call on where owned/claimable/others is computed:
        # by the time cmd_load returns, the autopark mutation is already
        # committed to disk, not just reflected in the in-memory return value.
        self._transcript("sess-b", age_hours=1)
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed(last_seen="2026-07-20T10:00:00Z")]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual(out["parked_now"], [{"id": "g-1111", "text": "Fix login bug"}])
        on_disk = goals.read_ledger("demo", "Demo")
        self.assertEqual(on_disk["goals"][0]["status"], "parked",
                         "autopark must be committed to disk, not just reflected "
                         "in the return value")

    def test_a_dropped_goal_leaves_the_working_set(self) -> None:
        # C-1: every live-set filter tested `status != "parked"` and nothing
        # else, so approving `gone` changed nothing a human reads — the goal
        # stayed in `owned`, so the builder rendered it as an open bullet AND
        # it stayed in the extractor's `stored_goals`, which re-proposed
        # `gone` for it on that run and every run after.
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed("g-1111", text="Ship the ledger", status="dropped")]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual(out["owned"], [],
                         "a dropped goal must not stay in the working set")
        self.assertEqual(out["completed"], [],
                         "dropped is not done — it belongs in neither surface")

    def test_a_done_goal_leaves_owned_and_is_returned_as_completed(self) -> None:
        # `done` leaves the working set for the same reason `dropped` does
        # (it is finished; the extractor must stop proposing `gone` for it),
        # but unlike `dropped` it is still something Pedro wants to SEE. It
        # comes back on its own key rather than silently vanishing.
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed("g-1111", text="Ship the ledger", status="done")]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual(out["owned"], [], "a done goal must leave the working set")
        self.assertEqual([g["id"] for g in out["completed"]], ["g-1111"],
                         "a done goal must still reach the report")

    def test_a_goal_with_an_unrecognized_status_stays_in_the_working_set(self) -> None:
        # The predicate is an EXCLUSION set, not `status == "open"`, and the
        # difference is exactly this record. A status no rule recognises
        # (hand-edited ledger, future schema drift) stays visible instead of
        # being silently dropped from every surface — silently dropping it is
        # the same defect class as C-1 itself.
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed("g-1111", text="Weird status", status="wip")]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual([g["id"] for g in out["owned"]], ["g-1111"])

    def test_done_and_dropped_goals_are_not_counted_as_live_elsewhere(self) -> None:
        # `claimable`/`others` derive from the same live set, so the report's
        # "Also live on this project: N goals owned by other sessions" line
        # counted finished and abandoned goals as live work too.
        self._transcript("sess-b", age_hours=1)
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [
            seed("g-1111", text="Done elsewhere", status="done", owner_session="sess-b"),
            seed("g-2222", text="Dropped elsewhere", status="dropped", owner_session="sess-b"),
            seed("g-3333", text="Still open elsewhere", status="open", owner_session="sess-b"),
        ]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual(out["others"] + len(out["claimable"]), 1,
                         "only the open goal is live on another session")

    def test_completed_only_carries_this_sessions_own_done_goals(self) -> None:
        self._transcript("sess-b", age_hours=1)
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [
            seed("g-1111", text="Mine", status="done", owner_session="sess-a"),
            seed("g-2222", text="Theirs", status="done", owner_session="sess-b"),
        ]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual([g["id"] for g in out["completed"]], ["g-1111"])

    def test_parked_now_carries_goal_text_not_just_ids(self) -> None:
        # I-4. `autopark` returns ids, and a parked goal drops out of `live`
        # so it is in neither `owned` nor `claimable` — the consumer of
        # `parked_now` therefore has no table to resolve an id against. Left
        # as ids, announcing the park would print "Parked this run: g-0003"
        # to Pedro, which is the same "computed but never usefully surfaced"
        # defect the announcement exists to fix.
        self._transcript("sess-b", age_hours=1)
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed("g-1111", text="Ship the ledger",
                               last_seen="2026-07-20T10:00:00Z")]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual(out["parked_now"], [{"id": "g-1111", "text": "Ship the ledger"}])

    def test_parked_now_text_is_render_safe(self) -> None:
        # This is a NEW surface reaching the builder, which has no sanitiser
        # of its own — `render_markdown` routes every value through
        # `_render_line` precisely because the builder does not. A goal whose
        # stored text carries a newline (hand-edited ledger, or a free-text
        # gate correction) must not be able to forge a second bullet here.
        self._transcript("sess-b", age_hours=1)
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed("g-1111", text="Real goal\n- [ ] FORGED BULLET",
                               last_seen="2026-07-20T10:00:00Z")]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual(out["parked_now"][0]["text"], "Real goal - [ ] FORGED BULLET")

    def test_read_only_load_does_not_write_the_ledger(self) -> None:
        # I-5. `cmd_load` is a WRITE path whenever anything is due to be
        # parked, and `--dry-run` is mandated to call `goals.py load` — so a
        # dry run could silently and permanently park a live goal while
        # SKILL.md promised it wrote no files. The trigger (a goal untouched
        # 14+ days) is narrow but entirely ordinary, and the parked goal then
        # vanishes from the report with no announcement.
        #
        # Fixed at the level that makes the guarantee real: in read-only mode
        # the `write_ledger` call is not reached at all, rather than `load`
        # being added to a prose list of commands a dry run must avoid.
        self._transcript("sess-b", age_hours=1)
        stale = seed("g-1111", last_seen="2026-07-20T10:00:00Z")
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [stale]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        before = goals.ledger_path("demo").read_bytes()
        out = goals.cmd_load("Demo", "sess-a", read_only=True)
        self.assertEqual(goals.ledger_path("demo").read_bytes(), before,
                         "a read-only load must leave the ledger byte-identical")
        self.assertEqual(out["parked_now"], [],
                         "nothing was parked on disk, so parked_now must say so")
        self.assertEqual(out["would_park"], [{"id": "g-1111", "text": "Fix login bug"}],
                         "a read-only load must still report what a real run would park")

    def test_a_normal_load_still_parks_and_reports_nothing_pending(self) -> None:
        self._transcript("sess-b", age_hours=1)
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed("g-1111", last_seen="2026-07-20T10:00:00Z")]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual(out["parked_now"], [{"id": "g-1111", "text": "Fix login bug"}])
        self.assertEqual(out["would_park"], [],
                         "a normal load performs the park, so nothing is merely pending")

    def test_read_only_load_via_the_cli_writes_nothing(self) -> None:
        # The CLI is the surface `--dry-run` actually uses; the flag has to
        # reach `cmd_load` through argparse, not just exist as a kwarg.
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed("g-1111", last_seen="2026-07-20T10:00:00Z")]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        before = goals.ledger_path("demo").read_bytes()
        self.assertEqual(goals.main(["load", "--read-only", "--project", "Demo",
                                     "--session", "sess-a"]), 0)
        self.assertEqual(goals.ledger_path("demo").read_bytes(), before)

    def test_session_live_rejects_a_uuid_that_escapes_the_project_subdirectory(self) -> None:
        # A legitimate transcript is always PROJECTS_DIR/<project>/<uuid>.jsonl.
        # Confirmed empirically that pathlib's glob honors ".." components, so
        # an unsanitized uuid can walk the pattern to a file living directly
        # under PROJECTS_DIR — one level above where transcripts actually live.
        decoy = goals.PROJECTS_DIR / "evil.jsonl"
        decoy.write_text("{}\n", encoding="utf-8")
        self.assertFalse(goals.session_live("../evil", time.time()))

    def test_session_live_rejects_glob_metacharacters_in_the_uuid(self) -> None:
        # A uuid of "*" must not become "match every transcript" — that would
        # make a garbage/malicious owner_session field register as always-live.
        self._transcript("real-session", age_hours=1)
        self.assertFalse(goals.session_live("*", time.time()))

    def test_load_rejects_a_non_string_project(self) -> None:
        with self.assertRaises(goals.Abort) as ctx:
            goals.cmd_load(None, "sess-a")
        self.assertEqual(ctx.exception.code, 2)

    def test_load_rejects_a_non_string_session_instead_of_silently_matching_null_owners(self) -> None:
        # A None session must abort, not silently succeed: owner_session=None
        # is a legal (if unusual) ledger value, and `== session` would make an
        # unvalidated None session falsely "own" every such goal with no error.
        data = {"schema": 1, "project": "Demo", "updated": None,
                "goals": [seed(owner_session=None)]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        with self.assertRaises(goals.Abort) as ctx:
            goals.cmd_load("Demo", None)
        self.assertEqual(ctx.exception.code, 2)

    def test_load_dedupes_session_live_lookups_per_distinct_owner(self) -> None:
        # session_live() globs PROJECTS_DIR; cmd_load must call it once per
        # distinct owner_session among the goals it needs to classify, not
        # once per goal.
        self._transcript("sess-b", age_hours=1)
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [
            seed("g-1111", owner_session="sess-b"),
            seed("g-2222", owner_session="sess-b"),
            seed("g-3333", owner_session="sess-b"),
        ]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        calls = []
        original = goals.session_live

        def counting(uuid, now_epoch):
            calls.append(uuid)
            return original(uuid, now_epoch)

        goals.session_live = counting
        try:
            out = goals.cmd_load("Demo", "sess-a")
        finally:
            goals.session_live = original
        self.assertEqual(calls, ["sess-b"], "three goals share one owner; one lookup suffices")
        self.assertEqual(out["others"], 3)

    def test_load_dedup_survives_a_non_hashable_owner_session(self) -> None:
        # Regression: caching session_live results keyed on the raw
        # owner_session value crashes with a bare TypeError when a malformed
        # ledger record holds a non-hashable owner_session (a list). The dedup
        # cache must key on _owner_key(...), not the raw value.
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [
            seed("g-1111", owner_session=["not", "hashable"]),
            seed("g-2222", owner_session=["also", "not", "hashable"]),
        ]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual(sorted(g["id"] for g in out["claimable"]), ["g-1111", "g-2222"])

    def test_autopark_rejects_a_non_string_now_iso(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [seed()]}
        with self.assertRaises(goals.Abort) as ctx:
            goals.autopark(data, None)
        self.assertEqual(ctx.exception.code, 2)

    def test_autopark_rejects_a_malformed_now_iso_string(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [seed()]}
        with self.assertRaises(goals.Abort) as ctx:
            goals.autopark(data, "not-a-timestamp")
        self.assertEqual(ctx.exception.code, 2)

    def test_others_counts_correctly_when_two_goals_share_an_id(self) -> None:
        # `_check_ledger_shape` only requires `id` to be a string, never that
        # ids are unique. `claimable_ids = {g["id"] for g in claimable}`
        # collapses two same-id goals into one set entry, so a `not in
        # claimable_ids` filter undercounts `others` for the duplicate that
        # isn't itself claimable. Reproduced against the plan's own sample
        # code: claimable=1, others=0 before the fix (correct is others=1).
        self._transcript("sess-live", age_hours=1)
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": [
            seed("g-dupe", owner_session="sess-dead"),   # dead owner: claimable
            seed("g-dupe", owner_session="sess-live"),   # live owner: an "other"
        ]}
        with goals.ledger_lock("demo"):
            goals.write_ledger("demo", data)
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual(len(out["claimable"]), 1)
        self.assertEqual(out["others"], 1, "one of the two duplicate-id goals "
                          "has a live owner and is not claimable")

    def test_session_live_rejects_a_uuid_with_a_trailing_newline(self) -> None:
        # `$` in `_SESSION_ID_RE` matches before a trailing "\n" too, so
        # `.match()` let "sess-b\n" through as if it were the plain token
        # "sess-b".
        self._transcript("sess-b", age_hours=1)
        self.assertFalse(goals.session_live("sess-b\n", time.time()))

    def test_load_on_a_project_with_no_ledger_file_creates_nothing(self) -> None:
        # The most common real invocation: first run against a project with
        # no ledger on disk yet. Must not crash, and a pure load must not
        # create a ledger file as a side effect.
        out = goals.cmd_load("Demo", "sess-a")
        self.assertEqual(out, {"schema": goals.SCHEMA, "project": "Demo", "slug": "demo",
                                "owned": [], "completed": [], "claimable": [],
                                "others": 0, "parked_now": [], "would_park": []})
        self.assertFalse(goals.ledger_path("demo").exists(),
                          "a pure load must not create a ledger file")


GOOD_BLOCK = textwrap.dedent(
    """
    Some preamble the agent wrote that must be ignored.

    <<<GOAL_EXTRACTION>>>
    {
      "proposed": [
        {
          "id": "g-1111",
          "level": "goal",
          "parent": null,
          "tag": "changed",
          "was": "Fix login bug",
          "text": "Fix the login redirect loop",
          "evidence": "edited src/auth/redirect.ts twice",
          "origin": "plan:/home/pedro/.claude/plans/login.md"
        },
        {
          "id": "new",
          "level": "subgoal",
          "parent": "g-1111",
          "tag": "new",
          "was": null,
          "text": "Reproduce it locally",
          "evidence": "ran the failing spec",
          "origin": "extracted"
        }
      ]
    }
    <<<END>>>
    """
)


def _proposal(**over) -> dict:
    """A minimal, fully-compliant proposal dict — every one of the eight
    known fields present, all eight's simplest legal value. Tests build
    their own malformed shape by overriding one field, or by deleting a
    key entirely (`del proposal["evidence"]`) to exercise "missing", which
    `**over` alone can't express.

    Deliberately NOT derived from `goals._FIELD_NAMES`: a helper that
    generates itself from the parser's own field tuple would keep passing
    through exactly the kind of contract break this file exists to catch.
    `TestOriginContract` builds its own shape a third time, independently
    of both, for the same reason."""
    base = {"id": "new", "level": "goal", "parent": None, "tag": "new",
            "was": None, "text": "A goal", "evidence": "e", "origin": "extracted"}
    base.update(over)
    return base


def _block(proposals: list, *, wrap: bool = True) -> str:
    """Build a `<<<GOAL_EXTRACTION>>>...<<<END>>>` block from a list of
    already-shaped proposal dicts, JSON-encoded through the real
    `json.dumps`/`json.loads` round trip — not a mocked Python structure —
    so every test here exercises the same boundary a real extractor
    response crosses. `wrap=False` returns the bare JSON body, for tests
    that build a malformed outer shell (a code fence, a decoy) by hand.
    """
    payload = json.dumps({"proposed": proposals})
    return f"<<<GOAL_EXTRACTION>>>\n{payload}\n<<<END>>>" if wrap else payload


class TestParseExtraction(LedgerTestCase):
    # --- Control cases: prove the happy path still works. Guard work that
    # quietly stops accepting valid input is the failure mode that matters
    # most here — the line format came within one review of shipping
    # exactly that. ---

    def test_parses_every_field(self) -> None:
        items = goals.parse_extraction(GOOD_BLOCK)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["tag"], "changed")
        self.assertEqual(items[0]["was"], "Fix login bug")
        self.assertIsNone(items[0]["parent"])
        self.assertEqual(items[1]["id"], "new")
        self.assertEqual(items[1]["parent"], "g-1111")
        self.assertIsNone(items[1]["was"])
        # Both `origin` shapes, in the one block that claims to exercise
        # every field: a goal traceable to the approved plan, and one only
        # inferred from the session.
        self.assertEqual(items[0]["origin"], "plan:/home/pedro/.claude/plans/login.md")
        self.assertEqual(items[1]["origin"], "extracted")

    def test_a_duplicate_proposal_id_is_rejected_at_parse(self) -> None:
        # I-2. `_validate_proposal` checks each proposal in isolation, so
        # nothing saw a repeated `id` across the array. The collision was
        # caught only by `apply_decisions`, i.e. AFTER the gate, at the commit
        # step — where the all-or-nothing abort discards the whole batch
        # INCLUDING every unrelated `seen` op, and where the skill has no
        # fallback. The parse-abort path twelve lines earlier in SKILL.md does
        # have one. Moving the check here puts the same class of extractor
        # slip on the side of the gate that can recover from it, and stops
        # Pedro reviewing and approving a batch that was never committable.
        #
        # `goal-extractor.md` explicitly warns against emitting a shared id,
        # which is evidence the slip is expected rather than exotic.
        block = _block([
            _proposal(id="new-1", text="First goal"),
            _proposal(id="new-1", text="Second goal"),
        ])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("proposed[1]", ctx.exception.message)
        self.assertIn("already used by proposed[0]", ctx.exception.message)

    def test_a_duplicate_id_abort_message_is_length_bounded(self) -> None:
        # `id` is capped at MAX_VALUE_LENGTH (2000), not at something a
        # terminal can hold, and this message interpolates it.
        long_id = "n" * 500
        block = _block([
            _proposal(id=long_id, text="First goal"),
            _proposal(id=long_id, text="Second goal"),
        ])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertLess(len(ctx.exception.message), 400,
                        "abort messages must stay bounded regardless of input size")

    def test_distinct_ids_across_proposals_still_parse(self) -> None:
        items = goals.parse_extraction(_block([
            _proposal(id="new-1", text="First goal"),
            _proposal(id="new-2", text="Second goal"),
        ]))
        self.assertEqual([i["id"] for i in items], ["new-1", "new-2"])

    def test_a_markdown_code_fence_around_the_json_object_is_tolerated(self) -> None:
        # Models add a ```json fence unprompted even when not asked to —
        # rejecting an otherwise-well-formed payload over a fence is a
        # false rejection that costs the user a run's goal update.
        payload = _block([_proposal(id="g-1", text="Fenced goal")], wrap=False)
        block = f"<<<GOAL_EXTRACTION>>>\n```json\n{payload}\n```\n<<<END>>>"
        items = goals.parse_extraction(block)
        self.assertEqual(items[0]["text"], "Fenced goal")

    def test_a_plain_unlabeled_code_fence_is_also_tolerated(self) -> None:
        payload = _block([_proposal(id="g-1", text="Fenced goal")], wrap=False)
        block = f"<<<GOAL_EXTRACTION>>>\n```\n{payload}\n```\n<<<END>>>"
        items = goals.parse_extraction(block)
        self.assertEqual(items[0]["text"], "Fenced goal")

    def test_a_code_fence_with_no_newline_before_the_closing_backticks_is_tolerated(self) -> None:
        # Found by a multi-persona audit of this task's own diff (Hostile
        # User persona): a real model just as often closes the fence right
        # after the JSON's own trailing brace as it does on its own line —
        # the original regex required a "\n" immediately before the
        # closing ``` and silently fell through to a confusing "not valid
        # JSON" error on this shape.
        payload = _block([_proposal(id="g-1", text="Fenced goal")], wrap=False)
        block = f"<<<GOAL_EXTRACTION>>>\n```json\n{payload}```\n<<<END>>>"
        items = goals.parse_extraction(block)
        self.assertEqual(items[0]["text"], "Fenced goal")

    def test_a_code_fence_with_trailing_prose_after_the_close_is_tolerated(self) -> None:
        # Follow-up fix (2026-08-13 review): "Here is the extraction:
        # ```json ... ``` Let me know if you want changes" is completely
        # ordinary model output. At HEAD this was a FALSE rejection — the
        # old regex required the closing ``` to sit at the very end of the
        # string (`\s*$`), so any prose after it made the whole fence look
        # unmatched and fell through to a "not valid JSON" error pointed at
        # a backtick. `_find_fence_close` locates the FIRST close, so
        # trailing content after it is simply ignored.
        payload = _block([_proposal(id="g-1", text="Fenced goal")], wrap=False)
        block = f"<<<GOAL_EXTRACTION>>>\n```json\n{payload}\n```\nthanks!\n<<<END>>>"
        items = goals.parse_extraction(block)
        self.assertEqual(items[0]["text"], "Fenced goal")

    def test_a_triple_backtick_inside_a_json_string_value_does_not_truncate_the_fence(self) -> None:
        # The control for the trailing-prose fix above: a looser "first
        # close" scan must not treat a literal ``` INSIDE a JSON string
        # value as the fence boundary, or it would truncate the payload
        # mid-string. `_find_fence_close` tracks string/escape state (the
        # same way `_check_json_depth` does) so this backtick run, sitting
        # inside `text`'s own quotes, is skipped, and the real closing
        # fence — after the JSON's genuine trailing brace — is what's found.
        payload = _block([_proposal(id="g-1", text="see the ```snippet``` above")], wrap=False)
        block = f"<<<GOAL_EXTRACTION>>>\n```json\n{payload}\n```\n<<<END>>>"
        items = goals.parse_extraction(block)
        self.assertEqual(items[0]["text"], "see the ```snippet``` above")

    def test_a_fence_that_never_closes_gives_a_fence_specific_abort_message(self) -> None:
        # The fallback the follow-up review asked for: when `json.loads`
        # still fails after fence-stripping was attempted and gave up
        # (no closing ``` anywhere), name the fence as the suspect instead
        # of surfacing the raw decoder error, which always points at
        # column 1 — a backtick — with no hint a fence was ever involved.
        payload = _block([_proposal(id="g-1", text="Fenced goal")], wrap=False)
        block = f"<<<GOAL_EXTRACTION>>>\n```json\n{payload}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("code fence", ctx.exception.message)
        self.assertNotIn("column 1", ctx.exception.message)

    # --- Delimiter handling. ---

    def test_missing_block_aborts_with_2(self) -> None:
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction("the agent forgot the block entirely")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("returned no", ctx.exception.message)

    def test_markers_out_of_order_is_a_distinct_abort_from_markers_missing_entirely(self) -> None:
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction("<<<END>>> stuff <<<GOAL_EXTRACTION>>>")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("out of order", ctx.exception.message)
        self.assertNotIn("returned no", ctx.exception.message)

    def test_a_stray_end_marker_inside_a_json_string_value_is_harmless(self) -> None:
        # Under the retired line format this silently truncated the block
        # (a FATAL found in Task 5). The tool is self-hosting and will one
        # day parse a session that discusses its own delimiters, so a
        # literal "<<<END>>>" — or "<<<GOAL_EXTRACTION>>>" — sitting inside
        # a field's own value must not be mistaken for the real boundary.
        # Anchoring on the FIRST open marker and the LAST close marker
        # means both stray mentions, which sit strictly between the two
        # real markers, are just JSON text being parsed, not structure.
        block = _block([_proposal(id="g-1", text="discusses <<<END>>> inline",
                                   evidence="mentions <<<GOAL_EXTRACTION>>> too")])
        items = goals.parse_extraction(block)
        self.assertEqual(items[0]["text"], "discusses <<<END>>> inline")
        self.assertEqual(items[0]["evidence"], "mentions <<<GOAL_EXTRACTION>>> too")

    def test_a_decoy_block_before_the_real_one_fails_safe_not_silently_parsed_instead(self) -> None:
        # An extractor that explains or quotes the block format ahead of
        # its real answer must not have that explanation silently parsed
        # instead of the real proposals. First-open/last-close anchoring
        # splices the decoy, the prose between it and the real block, and
        # the real block into one candidate payload — essentially never
        # valid JSON — so this fails safe via a JSON decode error rather
        # than returning the decoy's contents.
        decoy = (
            "Here is how the format works:\n"
            "<<<GOAL_EXTRACTION>>>\n"
            '{"proposed": [{"id": "g-dummy", "level": "goal", "parent": null, '
            '"tag": "new", "was": null, "text": "example", "evidence": "none"}]}\n'
            "<<<END>>>\n"
            "\n\nNow the real answer:\n"
        ) + GOOD_BLOCK
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(decoy)
        self.assertEqual(ctx.exception.code, 2)

    def test_non_string_input_aborts_with_2(self) -> None:
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(None)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("must be a string", ctx.exception.message)

    def test_an_oversized_overall_input_is_rejected_before_any_parsing_work(self) -> None:
        huge_text = "x" * (goals.MAX_TEXT_LENGTH + 1)
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(huge_text)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("goal-extractor output exceeds", ctx.exception.message)

    def test_malformed_json_syntax_aborts_with_a_bounded_message(self) -> None:
        # JSONDecodeError's own message reports a line/column/character
        # position, never a slice of the offending text, so it stays
        # bounded regardless of how large the malformed input was.
        huge = "{" + "x" * 5000
        block = f"<<<GOAL_EXTRACTION>>>\n{huge}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("not valid JSON", ctx.exception.message)
        self.assertLess(len(ctx.exception.message), 250)

    # --- Blind spot 1: duplicate JSON keys. json.loads silently keeps the
    # LAST occurrence of a repeated key unless object_pairs_hook rejects
    # it — a silent-precedence rule inside a trust boundary. A real dict
    # literal can't even express a duplicate key, so these are built as
    # raw JSON text. ---

    def test_duplicate_keys_in_a_proposal_object_are_rejected(self) -> None:
        raw_json = (
            '{"proposed": [{"id": "g-1", "level": "goal", "parent": null, '
            '"tag": "same", "tag": "changed", "was": null, "text": "X", "evidence": "e"}]}'
        )
        block = f"<<<GOAL_EXTRACTION>>>\n{raw_json}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("duplicate key", ctx.exception.message)
        # Follow-up fix (2026-08-13 review): the abort now names WHICH
        # proposal the duplicate was found in, using the same
        # `proposed[{index}]` convention `_validate_proposal` uses
        # everywhere else — a block with a dozen proposals no longer
        # forces the reader to re-parse the payload by hand to find it.
        self.assertIn("proposed[0]", ctx.exception.message)
        self.assertNotIn("top-level", ctx.exception.message)

    def test_duplicate_keys_in_the_top_level_object_are_rejected(self) -> None:
        raw_json = (
            '{"proposed": [], "proposed": [{"id": "g-1", "level": "goal", "parent": null, '
            '"tag": "new", "was": null, "text": "X", "evidence": "e"}]}'
        )
        block = f"<<<GOAL_EXTRACTION>>>\n{raw_json}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("duplicate key", ctx.exception.message)
        self.assertIn("top-level object", ctx.exception.message)

    def test_duplicate_key_locator_names_the_right_proposal_among_several(self) -> None:
        # The call-order locator maps call index N to proposed[N] — proven
        # here against a THIRD proposal (index 2), not just the lone
        # proposal the other two tests use, where index 0 could pass by
        # coincidence even with a broken mapping.
        raw_json = (
            '{"proposed": ['
            '{"id": "g-0", "level": "goal", "parent": null, "tag": "new", "was": null, '
            '"text": "A", "evidence": "e"},'
            '{"id": "g-1", "level": "goal", "parent": null, "tag": "new", "was": null, '
            '"text": "B", "evidence": "e"},'
            '{"id": "g-2", "level": "goal", "parent": null, "tag": "same", "tag": "changed", '
            '"was": null, "text": "C", "evidence": "e"}'
            ']}'
        )
        block = f"<<<GOAL_EXTRACTION>>>\n{raw_json}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("proposed[2]", ctx.exception.message)

    # --- Blind spot 2: a character that only becomes dangerous after
    # decoding. A `\\u202e` escape in the JSON SOURCE is six harmless ASCII
    # characters; it only becomes the actual BiDi override once json.loads
    # decodes it, so the display-safety guard must run on the DECODED
    # value, never the raw JSON text. ---

    def test_a_bidi_override_expressed_as_a_json_unicode_escape_is_rejected_after_decoding(self) -> None:
        raw_json = (
            '{"proposed": [{"id": "g-1", "level": "goal", "parent": null, "tag": "new", '
            '"was": null, "text": "X", "evidence": "ran the failing spec\\u202e", "origin": "extracted"}]}'
        )
        block = f"<<<GOAL_EXTRACTION>>>\n{raw_json}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("disallowed control or display-override", ctx.exception.message)

    def test_an_unpaired_surrogate_escape_is_rejected(self) -> None:
        # A new attack surface the JSON contract itself opens, found by a
        # multi-persona audit of this task's own diff (two personas
        # independently converged on it): a `\ud800`-range escape with no
        # matching low-surrogate partner decodes into a genuine
        # lone-surrogate character — something the retired line format's
        # plain-text values, with no escape syntax of their own, could
        # never produce. Left unchecked, it sails through every other
        # guard and later crashes `write_ledger`'s UTF-8 write with an
        # uncaught `UnicodeEncodeError`, breaking the module's
        # every-failure-is-an-`Abort` contract.
        raw_json = (
            '{"proposed": [{"id": "g-1", "level": "goal", "parent": null, "tag": "new", '
            '"was": null, "text": "X", "evidence": "surrogate: \\ud800 here", "origin": "extracted"}]}'
        )
        block = f"<<<GOAL_EXTRACTION>>>\n{raw_json}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("disallowed control or display-override", ctx.exception.message)

    def test_a_correctly_paired_surrogate_escape_is_accepted(self) -> None:
        # The positive control: a properly PAIRED surrogate escape (any
        # real emoji or other astral-plane character) decodes into one
        # ordinary, non-surrogate code point and must not be caught by the
        # same net — only an unpaired half is ever actually a `Cs`
        # character.
        raw_json = (
            '{"proposed": [{"id": "g-1", "level": "goal", "parent": null, "tag": "new", '
            '"was": null, "text": "X", "evidence": "emoji: \\ud83d\\ude00 here", "origin": "extracted"}]}'
        )
        block = f"<<<GOAL_EXTRACTION>>>\n{raw_json}\n<<<END>>>"
        items = goals.parse_extraction(block)
        self.assertIn("😀", items[0]["evidence"])

    def test_an_ansi_escape_in_a_field_value_is_rejected(self) -> None:
        block = _block([_proposal(evidence="ran the failing spec\x1b[31m")])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("disallowed control or display-override", ctx.exception.message)

    def test_the_weaker_directional_marks_are_also_rejected(self) -> None:
        # M-4. LRM (U+200E), RLM (U+200F) and ALM (U+061C) passed both ingest
        # and render. They are weaker than the overrides already blocked (no
        # scope, no nesting) but they are the same class of invisible
        # directional character, in the one file whose entire purpose is to
        # be trusted at a glance. ZWJ passing remains deliberate and correct
        # — it is a legitimate part of ordinary text (emoji sequences,
        # Indic scripts), not a display-direction control.
        for mark, name in (("‎", "LRM"), ("‏", "RLM"), ("؜", "ALM")):
            with self.subTest(mark=name):
                block = _block([_proposal(text=f"Fix the login{mark} redirect")])
                with self.assertRaises(goals.Abort) as ctx:
                    goals.parse_extraction(block)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("disallowed", ctx.exception.message)

    def test_a_hostile_json_key_is_display_checked_before_it_is_quoted_back(self) -> None:
        # M-5. Values pass through `_has_disallowed_display_chars`; KEYS never
        # did, and three abort messages interpolate a key via `_bounded_repr`.
        #
        # The review framed this as covering BiDi characters AND stacked
        # combining marks. Probed directly: BiDi characters are category Cf,
        # `str.isprintable()` is False for them, and `repr()` therefore
        # ALREADY escapes them to `‮` — so that half does not reproduce.
        # Combining marks are category Mn and printable, and survive
        # `_bounded_repr` verbatim, so an unbounded zalgo run in a key really
        # does reach the terminal. The finding is real; the character class
        # is narrower than stated. Fail-safe either way (the payload is
        # rejected regardless) — only the message text is affected.
        hostile_key = "ev" + "́" * 40 + "idence"
        payload = json.dumps({"proposed": [{
            "id": "g-1", "level": "goal", "parent": None, "tag": "same",
            "was": None, "text": "X", "evidence": "y", hostile_key: "z",
        }]})
        block = f"<<<GOAL_EXTRACTION>>>\n{payload}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unrecognized field", ctx.exception.message)
        self.assertLessEqual(
            ctx.exception.message.count("́"), goals._MAX_COMBINING_MARKS,
            "an abort message must not carry an unbounded combining-mark run")

    def test_an_ordinary_key_is_quoted_back_unchanged(self) -> None:
        # The display guard must not garble the common case: a plain
        # misspelled field name is what this message exists to report.
        block = _block([dict(_proposal(), evidence2="oops")])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertIn("'evidence2'", ctx.exception.message)

    def test_a_bidi_override_in_a_field_value_is_rejected(self) -> None:
        block = _block([_proposal(evidence="ran the failing spec‮")])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("disallowed control or display-override", ctx.exception.message)

    def test_stacked_combining_marks_beyond_the_cap_are_rejected(self) -> None:
        block = _block([_proposal(evidence="ran the failing spec e" + "́" * 20)])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("disallowed control or display-override", ctx.exception.message)

    # --- Regression tests below pin defects found by a multi-persona
    # adversarial audit of an earlier, line-format fix pass — the
    # underlying "isprintable() is the wrong net" defect and its fixes
    # survive the JSON rewrite unchanged, so these are kept, adapted. ---

    def test_a_non_breaking_space_in_a_field_value_is_accepted(self) -> None:
        block = _block([_proposal(evidence="ran the failing spec (confirmed)")])
        items = goals.parse_extraction(block)
        self.assertIn(" ", items[0]["evidence"])

    def test_a_tab_in_a_field_value_is_accepted(self) -> None:
        block = _block([_proposal(evidence="ran the failing spec\tsecond column")])
        items = goals.parse_extraction(block)
        self.assertIn("\t", items[0]["evidence"])

    def test_a_zwj_joined_emoji_in_a_field_value_is_accepted(self) -> None:
        block = _block([_proposal(evidence="ran the failing spec \U0001F9D1‍\U0001F4BB")])
        items = goals.parse_extraction(block)
        self.assertIn("‍", items[0]["evidence"])

    # --- Blind spot 3: type confusion. JSON carries real types, so a field
    # can arrive as a number, boolean, list, or object where the line
    # format could only ever produce a string — every field's type must be
    # checked explicitly, and `null` is legal ONLY on `parent`/`was`. ---

    def test_a_wrong_type_on_a_string_field_is_rejected(self) -> None:
        for bad_value in (42, True, ["x"], {"y": 1}):
            with self.subTest(bad_value=bad_value):
                block = _block([_proposal(text=bad_value)])
                with self.assertRaises(goals.Abort) as ctx:
                    goals.parse_extraction(block)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("proposed[0].text must be a string", ctx.exception.message)

    def test_a_wrong_type_on_a_nullable_field_is_rejected(self) -> None:
        block = _block([_proposal(parent=42)])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("proposed[0].parent must be a string or null", ctx.exception.message)

    def test_null_is_illegal_on_every_field_except_parent_and_was(self) -> None:
        for field in ("id", "level", "tag", "text", "evidence"):
            with self.subTest(field=field):
                block = _block([_proposal(**{field: None})])
                with self.assertRaises(goals.Abort) as ctx:
                    goals.parse_extraction(block)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn(f"proposed[0].{field} must be a string, got null", ctx.exception.message)

    def test_null_is_legal_on_parent_and_was(self) -> None:
        # The positive control for the check above: GOOD_BLOCK already
        # proves this implicitly (test_parses_every_field), but a future
        # change that made null illegal everywhere deserves a test named
        # for exactly that regression.
        block = _block([_proposal(parent=None, was=None)])
        items = goals.parse_extraction(block)
        self.assertIsNone(items[0]["parent"])
        self.assertIsNone(items[0]["was"])

    # --- Blind spot 4: structural bounds. A pathologically nested or wide
    # payload must abort on a stated bound, not on a RecursionError (or,
    # for json's C-accelerated decoder, an outright interpreter crash). ---

    def test_json_nesting_beyond_the_depth_cap_aborts_instead_of_a_recursion_error(self) -> None:
        # Trivially fits under MAX_TEXT_LENGTH (each level costs one byte)
        # — must be caught by a pre-parse scan, not by whatever json.loads
        # or Python's own recursion machinery happens to raise once it's
        # too deep to recurse into safely.
        depth = goals.MAX_JSON_DEPTH + 50
        nested = "[" * depth + "]" * depth
        block = f"<<<GOAL_EXTRACTION>>>\n{nested}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("nesting exceeds the maximum depth", ctx.exception.message)

    def test_a_json_array_beyond_the_length_cap_aborts(self) -> None:
        # The array-length cap fires on ANY array in the payload, not just
        # the top-level `proposed` list — a huge array masquerading as a
        # scalar field (here, `evidence`) is caught structurally, before
        # the type check would otherwise be the first thing to notice it.
        huge_array = list(range(goals.MAX_JSON_ARRAY_LENGTH + 1))
        block = _block([_proposal(evidence=huge_array)])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("array exceeds the maximum length", ctx.exception.message)
        # Follow-up fix (2026-08-13 review): named by JSON path, same
        # `proposed[{index}].{key}` convention `_validate_proposal` uses.
        self.assertIn("proposed[0].evidence", ctx.exception.message)

    def test_a_json_array_beyond_the_cap_locates_the_right_field_among_several_proposals(self) -> None:
        # The path-threading equivalent of the duplicate-key locator test:
        # proven against the SECOND proposal (index 1), not the lone
        # proposal the test above uses, where index 0 could pass even with
        # a broken path.
        huge_array = list(range(goals.MAX_JSON_ARRAY_LENGTH + 1))
        block = _block([_proposal(id="g-0", text="A"),
                         _proposal(id="g-1", text="B", evidence=huge_array)])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("proposed[1].evidence", ctx.exception.message)

    # --- Blind spot 5: shape of the top level. `proposed` must exist and
    # be a list of objects; a bare list, a string, null, or a missing key
    # each need a defined abort, not an AttributeError. ---

    def test_proposed_missing_aborts(self) -> None:
        block = "<<<GOAL_EXTRACTION>>>\n{}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("no `proposed` list", ctx.exception.message)

    def test_an_unrecognized_top_level_field_aborts_instead_of_being_ignored(self) -> None:
        # Follow-up fix (2026-08-13 review): at HEAD, `{"proposed": [...],
        # "extra": 1}` parsed clean — every proposal object rejects an
        # unrecognized key, but the payload object one level up silently
        # accepted anything. Decided deliberately (see
        # `_TOP_LEVEL_KNOWN_FIELDS`), not left open: the same rejection now
        # applies at the top level too.
        raw_json = json.dumps({"proposed": [_proposal(id="g-1")], "extra": 1})
        block = f"<<<GOAL_EXTRACTION>>>\n{raw_json}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unrecognized", ctx.exception.message)
        self.assertIn("top-level field", ctx.exception.message)
        self.assertIn("'extra'", ctx.exception.message)

    def test_an_unrecognized_top_level_fields_key_is_bounded_in_the_abort_message(self) -> None:
        huge_key = "x" * 5000
        raw_json = json.dumps({"proposed": [], huge_key: 1})
        block = f"<<<GOAL_EXTRACTION>>>\n{raw_json}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unrecognized", ctx.exception.message)
        self.assertLess(len(ctx.exception.message), 250)

    def test_proposed_is_a_string_not_a_list_aborts(self) -> None:
        block = '<<<GOAL_EXTRACTION>>>\n{"proposed": "oops"}\n<<<END>>>'
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("no `proposed` list", ctx.exception.message)

    def test_proposed_is_null_aborts(self) -> None:
        block = '<<<GOAL_EXTRACTION>>>\n{"proposed": null}\n<<<END>>>'
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("no `proposed` list", ctx.exception.message)

    def test_top_level_payload_is_a_bare_list_aborts(self) -> None:
        block = "<<<GOAL_EXTRACTION>>>\n[]\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("is not an object", ctx.exception.message)

    def test_a_top_level_bare_list_over_the_array_cap_still_reports_not_an_object(self) -> None:
        # Follow-up fix (2026-08-13 review): `_check_json_bounds` used to
        # run BEFORE the `isinstance(payload, dict)` check, so a top-level
        # payload that was both the wrong SHAPE (a list, not an object) and
        # oversized reported the size problem ("array exceeds the maximum
        # length") instead of the real one ("is not an object") — the type
        # was never established before something started consuming the
        # value. The `isinstance` check now runs first.
        huge_list = list(range(goals.MAX_JSON_ARRAY_LENGTH + 1))
        block = f"<<<GOAL_EXTRACTION>>>\n{json.dumps(huge_list)}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("is not an object", ctx.exception.message)
        self.assertNotIn("array exceeds", ctx.exception.message)

    def test_top_level_payload_is_a_string_aborts(self) -> None:
        block = '<<<GOAL_EXTRACTION>>>\n"just a string"\n<<<END>>>'
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("is not an object", ctx.exception.message)

    def test_a_proposal_that_is_not_an_object_aborts(self) -> None:
        block = '<<<GOAL_EXTRACTION>>>\n{"proposed": ["not an object"]}\n<<<END>>>'
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("proposed[0] is not an object", ctx.exception.message)

    # --- Field presence, cap, and semantic rules — unchanged in meaning
    # from the retired line format, adapted to a JSON fixture. ---

    def test_unrecognized_field_name_aborts_instead_of_being_dropped(self) -> None:
        proposal = _proposal()
        proposal["evidenc"] = proposal.pop("evidence")
        block = _block([proposal])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unrecognized field", ctx.exception.message)

    def test_an_unrecognized_fields_key_is_bounded_in_the_abort_message(self) -> None:
        huge_key = "x" * 5000
        proposal = _proposal()
        proposal[huge_key] = "y"
        block = _block([proposal])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unrecognized field", ctx.exception.message)
        self.assertLess(len(ctx.exception.message), 250)

    def test_a_field_omitted_entirely_aborts_as_missing_not_as_the_domain_check(self) -> None:
        # Distinguishes "field absent" from "field present and null" — an
        # explicit `"was": null` on a `changed` proposal is present, not
        # omitted (see test_changed_without_was_aborts for that case), but
        # a proposal that never writes `evidence` at all must abort on
        # completeness, not on any domain rule.
        proposal = _proposal()
        del proposal["evidence"]
        block = _block([proposal])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("missing a required field: 'evidence'", ctx.exception.message)

    def test_a_pathological_block_is_rejected_before_the_malformed_tail_is_parsed(self) -> None:
        # Proves the proposal cap is enforced against the raw list length,
        # not after fully validating every item: the 19th proposal is
        # missing its required `text`/`was` fields, but a block that
        # already has 18 proposals (MAX_GOALS + MAX_SUBGOALS) must be
        # rejected the moment the 19th brings the count over the cap —
        # before any of its own fields are even looked at.
        good_items = [_proposal(id="new", level="subgoal", parent="g-1111", text=f"sub {n}")
                      for n in range(18)]
        bad_tail = {"id": "new", "level": "subgoal", "parent": "g-1111",
                    "tag": "new", "evidence": "e"}
        block = _block(good_items + [bad_tail])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("too many proposals", ctx.exception.message)
        self.assertNotIn("required field", ctx.exception.message)

    def test_bad_tag_aborts(self) -> None:
        block = _block([_proposal(tag="maybe")])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("invalid tag", ctx.exception.message)

    def test_done_is_in_the_tag_vocabulary(self) -> None:
        # `done` is the vocabulary's word for "this goal was completed".
        # Without it the extractor's only way to report a finished goal is
        # `gone` — defined as "no support anywhere in the session" and mapped
        # by SKILL.md Step 4b to `status: "dropped"`, i.e. abandoned. Writing
        # `dropped` for a goal that was actually achieved records the opposite
        # of what happened, and `render_markdown` then hides it entirely
        # (`_NOT_VISIBLE`) instead of ticking its checkbox.
        self.assertIn("done", goals.VALID_TAG)
        # The status this tag maps to must already be legal, or the tag is a
        # dead end: it would parse here and then abort at commit.
        self.assertIn("done", goals.VALID_STATUS)

    def test_done_tag_parses(self) -> None:
        block = _block([_proposal(tag="done", id="g-1111")])
        parsed = goals.parse_extraction(block)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["tag"], "done")

    def test_done_tag_needs_no_was(self) -> None:
        # Only `changed` requires `was` (it replaces stored wording). A `done`
        # proposal reports a status transition and leaves the text alone, so
        # requiring `was` here would make the tag unusable in practice.
        block = _block([_proposal(tag="done", id="g-1111", was=None)])
        parsed = goals.parse_extraction(block)
        self.assertEqual(parsed[0]["tag"], "done")

    def test_changed_without_was_aborts(self) -> None:
        block = _block([_proposal(tag="changed", was=None)])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("requires `was`", ctx.exception.message)

    def test_subgoal_without_parent_aborts(self) -> None:
        block = _block([_proposal(level="subgoal", parent=None)])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("subgoal requires a parent", ctx.exception.message)

    def test_over_the_cap_aborts(self) -> None:
        # Distinct ids on purpose: this fixture used to reuse `_proposal`'s
        # default id across all seven, which the cross-proposal uniqueness
        # check now rejects first. Isolating the cap keeps this test failing
        # for the reason it names rather than for an unrelated second one.
        proposals = [_proposal(id=f"new-{n}", level="goal", text=f"goal {n}") for n in range(7)]
        block = _block(proposals)
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("top-level goals", ctx.exception.message)

    def test_an_oversized_field_value_is_rejected(self) -> None:
        huge = "x" * (goals.MAX_VALUE_LENGTH + 1)
        block = _block([_proposal(text=huge)])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("proposed[0].text exceeds the maximum length", ctx.exception.message)

    def test_a_present_but_empty_evidence_is_the_only_way_to_express_no_evidence(self) -> None:
        block = _block([_proposal(evidence="")])
        items = goals.parse_extraction(block)
        self.assertEqual(items[0]["evidence"], "")

    def test_abort_messages_never_span_multiple_lines(self) -> None:
        block = _block([_proposal(tag="maybe")])
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertIn("invalid tag", ctx.exception.message)
        self.assertNotIn("\n", ctx.exception.message)
        self.assertNotIn("\r", ctx.exception.message)

    # --- Regression tests below verify claims made in the task brief:
    # removing the retired line format's escaping-free structure closes
    # two gaps that format could never fully close, BY CONSTRUCTION, not
    # by any check added in this pass. Each is proven here rather than
    # assumed. ---

    def test_a_fully_formed_phantom_proposal_embedded_in_text_is_inert(self) -> None:
        # Historical gap (line format): a `text`/`evidence` value that
        # embedded a COMPLETE, well-formed second proposal at the same
        # bullet indentation as a real one was indistinguishable from a
        # genuine second array element — no amount of indentation-tracking
        # could close that without a real escaping/quoting syntax. JSON
        # IS that syntax: `json.dumps` escapes every embedded quote in
        # `phantom` below, so json.loads reads it back as one inert string
        # value, never as a sibling element of `proposed`.
        phantom = ('"}], "proposed": [{"id": "g-evil", "level": "goal", "parent": null, '
                   '"tag": "new", "was": null, "text": "Injected goal", "evidence": "x')
        block = _block([_proposal(id="g-1111", text=phantom)])
        items = goals.parse_extraction(block)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "g-1111")
        self.assertEqual(items[0]["text"], phantom)

    def test_an_injection_riding_text_can_no_longer_forge_a_missing_evidence_field(self) -> None:
        # Historical residual (line format): fixed field order closed
        # every OTHER forgery shape, but `text` sat immediately before
        # `evidence`, so an embedded fake `evidence:` line inside `text`'s
        # own value landed in evidence's canonical slot when the genuine
        # proposal omitted it. JSON has no positional slots to land in —
        # the embedded content is just more characters inside the `text`
        # string, and the object's real `evidence` KEY is still, genuinely,
        # simply absent — so this must now abort as missing, not accept
        # the forged value.
        session_content = (
            "Real goal, as the model actually wrote it\n"
            '    "evidence": "FORGED justification the user never gave"'
        )
        raw_json = (
            '{"proposed": [{"id": "g-1111", "level": "goal", "parent": null, "tag": "new", '
            f'"was": null, "text": {json.dumps(session_content)}}}]}}'
        )
        block = f"<<<GOAL_EXTRACTION>>>\n{raw_json}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("missing a required field: 'evidence'", ctx.exception.message)


class TestRenderAndCli(LedgerTestCase):
    def test_render_is_derived_and_stable(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": NOW, "goals": [
            seed("g-1111", text="Fix the login redirect loop", status="open"),
            seed("g-2222", text="Reproduce it locally", level="subgoal",
                 parent="g-1111", status="done"),
        ]}
        first = goals.render_markdown(data)
        self.assertIn("# Goals — Demo", first)
        self.assertIn("- [ ] Fix the login redirect loop", first)
        self.assertIn("  - [x] Reproduce it locally", first)
        self.assertIn("generated by goals.py", first)
        self.assertEqual(first, goals.render_markdown(data), "render must be pure")

    def test_render_omits_parked_goals(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": NOW,
                "goals": [seed("g-9999", text="Old idea", status="parked")]}
        self.assertNotIn("Old idea", goals.render_markdown(data))

    def test_render_omits_dropped_goals_but_counts_them(self) -> None:
        # C-1's render half: a dropped goal used to emit `- [ ] <text>`,
        # byte-identical to an open one. It leaves the tree entirely, and is
        # accounted for by an aggregate line mirroring the existing `Parked: N`.
        data = {"schema": 1, "project": "Demo", "updated": NOW,
                "goals": [seed("g-9999", text="Abandoned idea", status="dropped")]}
        out = goals.render_markdown(data)
        self.assertNotIn("Abandoned idea", out)
        self.assertIn("Dropped: 1 goal(s)", out)

    def test_render_keeps_a_done_goal_in_the_tree_as_a_checked_box(self) -> None:
        # `done` is deliberately NOT hidden the way `dropped` is: `- [x]` in
        # place, under its own parent, is what a checkbox list is for, and
        # hoisting completed items into a separate section would orphan the
        # still-open sub-goals of a completed parent.
        data = {"schema": 1, "project": "Demo", "updated": NOW, "goals": [
            seed("g-1111", text="Finished goal", status="done"),
            seed("g-2222", text="Still open child", level="subgoal", parent="g-1111"),
        ]}
        out = goals.render_markdown(data)
        self.assertIn("- [x] Finished goal", out)
        self.assertIn("  - [ ] Still open child", out)
        self.assertNotIn("Unparented sub-goals", out,
                         "a done parent must still parent its open sub-goals")

    def test_live_implies_visible(self) -> None:
        # The two predicates are not independent: anything still in flight
        # must also appear in the mirror. A future status added to the
        # visible-set exclusions but not the live-set exclusions would make a
        # goal that the extractor is still asked about invisible to Pedro —
        # exactly the "computed but never surfaced" shape this branch keeps
        # producing. Pinned as an invariant rather than left to review.
        for status in sorted(goals.VALID_STATUS | {"wip", ""}):
            with self.subTest(status=status):
                record = seed("g-1111", status=status)
                if goals._is_live(record):
                    self.assertTrue(goals._is_visible(record),
                                    f"status {status!r} is live but not visible")

    def test_render_treats_a_null_origin_as_not_plan_authored(self) -> None:
        # Task 3 left `origin` unvalidated, so a ledger can hold `origin:
        # null` — `.get("origin", "")`'s default only applies when the key
        # is ABSENT, so a naive `.startswith("plan:")` reaches None and
        # raises. This must not crash; a null origin is simply not
        # plan-authored.
        data = {"schema": 1, "project": "Demo", "updated": NOW,
                "goals": [seed("g-5555", text="Untitled goal", origin=None)]}
        out = goals.render_markdown(data)
        self.assertIn("- [ ] Untitled goal", out)

    def test_render_surfaces_a_subgoal_whose_parent_is_missing(self) -> None:
        # A sub-goal naming a parent id that does not exist matched no
        # top-level goal and used to vanish from the file with no trace.
        data = {"schema": 1, "project": "Demo", "updated": NOW, "goals": [
            seed("g-6666", text="Orphaned subgoal", level="subgoal", parent="g-nonexistent"),
        ]}
        out = goals.render_markdown(data)
        self.assertIn("Orphaned subgoal", out)

    def test_render_surfaces_a_subgoal_whose_parent_is_parked(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": NOW, "goals": [
            seed("g-7777", text="Parent goal", status="parked"),
            seed("g-8888", text="Child of parked parent", level="subgoal", parent="g-7777"),
        ]}
        out = goals.render_markdown(data)
        self.assertIn("Child of parked parent", out)

    def test_render_collapses_an_embedded_newline_in_goal_text(self) -> None:
        # `apply_decisions` only `.strip()`s incoming text; it never rejects
        # an embedded newline, and `_check_ledger_shape` never inspects
        # `text`'s content — so a ledger can legitimately hold this. Left
        # alone, the newline would break the bullet across two physical
        # lines with no `- ` prefix on the continuation.
        data = {"schema": 1, "project": "Demo", "updated": NOW, "goals": [
            seed("g-2020", text="Fix login\nand also logout"),
        ]}
        out = goals.render_markdown(data)
        self.assertNotIn("Fix login\nand also logout", out)
        self.assertIn("- [ ] Fix login and also logout", out)

    def test_render_markdown_rejects_a_malformed_goals_list(self) -> None:
        with self.assertRaises(goals.Abort) as ctx:
            goals.render_markdown({"schema": 1, "project": "Demo", "updated": NOW,
                                    "goals": "not-a-list"})
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("goals` is not a list", ctx.exception.message)

    def test_cli_commit_applies_and_reports_json(self) -> None:
        decisions = self.tmp / "decisions.json"
        decisions.write_text(json.dumps({
            "session": "sess-a",
            "decisions": [{"op": "create", "text": "Ship the ledger",
                           "level": "goal", "parent": None, "origin": "extracted"}],
        }), encoding="utf-8")
        out = goals.cmd_commit("Demo", "sess-a", str(decisions))
        self.assertTrue(out["ok"])
        self.assertEqual(out["applied"], 1)
        reread = goals.read_ledger("demo", "Demo")
        self.assertEqual(reread["goals"][0]["text"], "Ship the ledger")

    def test_cmd_commit_noop_does_not_rewrite_the_ledger(self) -> None:
        # A batch whose every decision is already true of the current state
        # (here: a `status` decision setting a goal to the status it already
        # has) applies zero changes. `write_ledger` must not run in that
        # case — restamping `updated` for a ledger that did not move would
        # corrupt any future staleness read against that field.
        with goals.ledger_lock("demo"):
            data = goals.read_ledger("demo", "Demo")
            data["goals"].append(seed("g-3333", status="open"))
            data["updated"] = "2026-01-01T00:00:00Z"
            goals.write_ledger("demo", data)
        path = goals.ledger_path("demo")
        before = path.read_bytes()

        decisions = self.tmp / "noop.json"
        decisions.write_text(json.dumps({
            "session": "sess-a",
            "decisions": [{"op": "status", "id": "g-3333", "status": "open"}],
        }), encoding="utf-8")
        out = goals.cmd_commit("Demo", "sess-a", str(decisions))

        self.assertTrue(out["ok"])
        self.assertEqual(out["applied"], 0)
        self.assertEqual(path.read_bytes(), before,
                         "a no-op commit batch must not rewrite the ledger")

    def test_cmd_render_writes_a_markdown_mirror_derived_from_the_ledger(self) -> None:
        decisions = self.tmp / "decisions.json"
        decisions.write_text(json.dumps({
            "session": "sess-a",
            "decisions": [{"op": "create", "text": "Ship the CLI",
                           "level": "goal", "parent": None, "origin": "extracted"}],
        }), encoding="utf-8")
        goals.cmd_commit("Demo", "sess-a", str(decisions))

        out_path = self.tmp / "mirror" / "GOALS.md"
        result = goals.cmd_render("Demo", str(out_path))
        self.assertTrue(result["ok"])
        rendered = out_path.read_text(encoding="utf-8")
        self.assertIn("Ship the CLI", rendered)
        self.assertEqual(rendered, goals.render_markdown(goals.read_ledger("demo", "Demo")))

    def test_cmd_render_sanitizes_a_lone_surrogate_in_a_hand_edited_ledgers_project_field(self) -> None:
        # Follow-up fix (2026-08-13 review): render_markdown's title line
        # used to interpolate data.get("project", ...) directly, bypassing
        # _render_line entirely. `_check_ledger_shape` never validates
        # `project`, and a hand-edited ledger file (not written through
        # write_ledger, which now rejects a lone surrogate at the source)
        # can hold a \ud800-range escape there. Proven directly against the
        # ledger file, not through cmd_commit, since no in-contract write
        # path can put a surrogate in `project` anymore.
        path = goals.ledger_path("demo")
        path.write_text(
            json.dumps({"schema": 1, "project": "Demo \ud800 Corp", "goals": []}),
            encoding="utf-8",
        )
        out_path = self.tmp / "mirror" / "GOALS.md"
        result = goals.cmd_render("Demo", str(out_path))
        self.assertTrue(result["ok"])
        rendered = out_path.read_text(encoding="utf-8")
        self.assertIn("Demo", rendered)
        self.assertNotIn("\ud800", rendered)

    def test_cmd_render_widened_except_catches_a_unicode_encode_error_and_leaves_no_tmp_file(self) -> None:
        # Defense-in-depth for the same class of bug as write_ledger's:
        # `_render_line` now strips every lone surrogate render_markdown
        # could put in its own output (including `project`, fixed above),
        # so there is currently no in-contract path that reaches this
        # except clause — proven here by forcing it directly, the way the
        # write_ledger test proves write_ledger's, so a future
        # render_markdown change that reintroduces an unguarded
        # interpolation still fails safely (Abort(4)) instead of crashing.
        goals.ledger_path("demo").write_text(
            json.dumps({"schema": 1, "project": "Demo", "goals": []}), encoding="utf-8")
        out_path = self.tmp / "mirror" / "GOALS.md"
        original = goals.render_markdown
        goals.render_markdown = lambda data: "surrogate: \ud800 here"
        try:
            with self.assertRaises(goals.Abort) as ctx:
                goals.cmd_render("Demo", str(out_path))
        finally:
            goals.render_markdown = original
        self.assertEqual(ctx.exception.code, 4)
        self.assertIn("render write failed", ctx.exception.message)
        leftover = list(out_path.parent.glob(".*.tmp-*"))
        self.assertEqual(leftover, [], f"leftover tmp file(s): {leftover}")
        self.assertFalse(out_path.exists())

    def test_corrupt_ledger_aborts_2_and_leaves_the_file_untouched(self) -> None:
        path = goals.ledger_path("demo")
        path.write_text('{"schema": 1, "goals": [', encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(goals.Abort) as ctx:
            goals.cmd_load("Demo", "sess-a")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("ledger unreadable", ctx.exception.message)
        self.assertEqual(path.read_bytes(), before,
                         "a corrupt ledger is never repaired automatically")

    def test_cli_exit_code_reaches_the_shell(self) -> None:
        env = dict(os.environ, GOALS_LEDGER_DIR=str(goals.LEDGER_DIR),
                   GOALS_LOCK_DIR=str(goals.LOCK_DIR), GOALS_RUN_DIR=str(goals.RUN_DIR))
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "goals.py"), "load",
             "--project", "", "--session", "sess-a"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(proc.stderr.strip(), "a non-zero exit must print one stderr line")
        self.assertIn("project must be a non-empty string", proc.stderr)

    def test_cli_render_subcommand_writes_the_markdown_file(self) -> None:
        env = dict(os.environ, GOALS_LEDGER_DIR=str(goals.LEDGER_DIR),
                   GOALS_LOCK_DIR=str(goals.LOCK_DIR), GOALS_RUN_DIR=str(goals.RUN_DIR))
        with goals.ledger_lock("demo"):
            data = goals.read_ledger("demo", "Demo")
            data["goals"].append(seed("g-4444", text="Ship the CLI wiring"))
            goals.write_ledger("demo", data)
        out_path = self.tmp / "GOALS.md"
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "goals.py"), "render",
             "--project", "Demo", "--out", str(out_path)],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Ship the CLI wiring", out_path.read_text(encoding="utf-8"))

    def test_cli_parse_extraction_rejects_an_oversized_file_at_the_cli_boundary(self) -> None:
        # This must be caught by the CLI's own size check on the file
        # (before it ever reads the file into memory), not by
        # `parse_extraction`'s internal length check — assert on the
        # CLI-boundary message specifically, not just on exit code 2, so a
        # regression that silently fell back to reading the whole file
        # first would still be caught here.
        big = self.tmp / "huge.txt"
        big.write_text("x" * (goals.MAX_TEXT_LENGTH + 1), encoding="utf-8")
        env = dict(os.environ, GOALS_LEDGER_DIR=str(goals.LEDGER_DIR),
                   GOALS_LOCK_DIR=str(goals.LOCK_DIR), GOALS_RUN_DIR=str(goals.RUN_DIR))
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "goals.py"), "parse-extraction",
             "--file", str(big)],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("extractor output file exceeds", proc.stderr)

    # --- Audit remediation: findings from the multipersona review of this
    # task's own diff, before it was committed. Each test below pins the
    # specific FATAL/SERIOUS finding it closes.

    def test_render_does_not_crash_on_a_goal_record_missing_text(self) -> None:
        # FATAL (Ruthless Critic + Skeptical Blogger, converged): `_check_ledger_shape`
        # only validates `id`; a hand-edited or partially-migrated record can
        # legitimately be missing `text` entirely. This must degrade the
        # render, not raise a bare KeyError.
        data = {"schema": 1, "project": "Demo", "updated": NOW,
                "goals": [{"id": "g-0001", "level": "goal", "parent": None,
                          "status": "open", "owner_session": "sess-a", "origin": "extracted",
                          "created": NOW, "last_seen": NOW, "history": []}]}
        out = goals.render_markdown(data)
        self.assertIn("(untitled goal)", out)

    def test_render_strips_bidi_override_characters_from_goal_text(self) -> None:
        # SERIOUS (Harsh Reviewer): the display-safety gate `parse_extraction`
        # applies at ingestion (`_has_disallowed_display_chars`) was never
        # re-applied at the render boundary — a goal committed via
        # `cmd_commit`, or a hand-edited ledger, can carry a BiDi override
        # straight into the file a human reads and trusts.
        rtlo = "‮"  # RIGHT-TO-LEFT OVERRIDE
        data = {"schema": 1, "project": "Demo", "updated": NOW, "goals": [
            seed("g-4141", text=f"safe text{rtlo}evil suffix"),
        ]}
        out = goals.render_markdown(data)
        self.assertNotIn(rtlo, out)
        self.assertIn("safe textevil suffix", out)

    def test_render_caps_stacked_combining_marks_in_goal_text(self) -> None:
        zalgo = "e" + "́" * 20  # 20 stacked acute accents on one "e"
        data = {"schema": 1, "project": "Demo", "updated": NOW, "goals": [
            seed("g-4242", text=f"weird {zalgo} word"),
        ]}
        out = goals.render_markdown(data)
        self.assertLessEqual(out.count("́"), goals._MAX_COMBINING_MARKS)

    def test_render_surfaces_a_record_with_an_unrecognized_level(self) -> None:
        # SERIOUS (Domain Expert): a record whose `level` is neither "goal"
        # nor "subgoal" (hand-edited, future schema drift) matches neither
        # `tops` nor `orphans` and used to vanish with no trace — the same
        # failure class the orphan fix exists to close, for a different field.
        data = {"schema": 1, "project": "Demo", "updated": NOW, "goals": [
            seed("g-5151", text="Mystery record", level="epic"),
        ]}
        out = goals.render_markdown(data)
        self.assertIn("Mystery record", out)
        self.assertIn("unrecognized level", out)

    def test_render_shows_an_inferred_legend_only_when_a_goal_is_inferred(self) -> None:
        # SERIOUS (Hostile User): "(inferred)" is the default outcome for
        # almost every real goal (default `origin="extracted"` never starts
        # with "plan:"), and nothing explained what it meant. A short legend
        # is cheap and does not change the marking semantics itself.
        inferred = {"schema": 1, "project": "Demo", "updated": NOW,
                    "goals": [seed("g-6161", origin="extracted")]}
        self.assertIn("(inferred) =", goals.render_markdown(inferred))

        planned = {"schema": 1, "project": "Demo", "updated": NOW,
                   "goals": [seed("g-6262", origin="plan:PLAN.md#g1")]}
        self.assertNotIn("(inferred)", goals.render_markdown(planned))

    def test_cmd_commit_rejects_a_non_string_project(self) -> None:
        # SERIOUS (Ruthless Critic): `cmd_load` validates `project` before
        # calling `slugify`; `cmd_commit`/`cmd_render` did not, so a
        # non-string `project` raised a bare AttributeError instead of the
        # documented Abort(2).
        decisions = self.tmp / "decisions.json"
        decisions.write_text(json.dumps({"session": "sess-a", "decisions": []}), encoding="utf-8")
        with self.assertRaises(goals.Abort) as ctx:
            goals.cmd_commit(None, "sess-a", str(decisions))
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("project must be a non-empty string", ctx.exception.message)

    def test_cmd_render_rejects_an_empty_project(self) -> None:
        with self.assertRaises(goals.Abort) as ctx:
            goals.cmd_render("", str(self.tmp / "out.md"))
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("project must be a non-empty string", ctx.exception.message)

    def test_cmd_commit_rejects_an_oversized_decisions_file(self) -> None:
        # SERIOUS (Harsh Reviewer): `cmd_commit` read `--decisions` with no
        # size bound at all, unlike the CLI-boundary bound this same task
        # added for `parse-extraction`. Both now share `_bounded_read_text`.
        big = self.tmp / "huge-decisions.json"
        big.write_text("x" * (goals.MAX_TEXT_LENGTH + 1), encoding="utf-8")
        with self.assertRaises(goals.Abort) as ctx:
            goals.cmd_commit("Demo", "sess-a", str(big))
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("decision set exceeds", ctx.exception.message)

    def test_cli_parse_extraction_rejects_a_non_utf8_file(self) -> None:
        # FATAL (Ruthless Critic): `_read_extraction_file` (now
        # `_bounded_read_text`) only caught `OSError`; a non-UTF-8 file
        # raises `UnicodeDecodeError` from `read_text`, which is not an
        # `OSError` and used to escape as a bare traceback.
        bad = self.tmp / "not-utf8.txt"
        bad.write_bytes(b"\xff\xfe\x00garbage")
        env = dict(os.environ, GOALS_LEDGER_DIR=str(goals.LEDGER_DIR),
                   GOALS_LOCK_DIR=str(goals.LOCK_DIR), GOALS_RUN_DIR=str(goals.RUN_DIR))
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "goals.py"), "parse-extraction", "--file", str(bad)],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(proc.stderr.strip(), "a non-zero exit must print one stderr line")
        self.assertIn("cannot read extractor output file", proc.stderr)

    def test_main_restores_ledger_dir_after_an_in_process_call(self) -> None:
        # SERIOUS (Domain Expert): `main()` used to reassign the module
        # globals from the environment with no restoration, so an in-process
        # caller that set the env var once would permanently repoint every
        # other function's `LEDGER_DIR` for the rest of the process.
        other = Path(tempfile.mkdtemp(prefix="goals-other-ledger-"))
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        before = goals.LEDGER_DIR
        os.environ["GOALS_LEDGER_DIR"] = str(other)
        self.addCleanup(os.environ.pop, "GOALS_LEDGER_DIR", None)
        try:
            goals.main(["load", "--project", "Demo", "--session", "sess-a"])
        finally:
            del os.environ["GOALS_LEDGER_DIR"]
        self.assertEqual(goals.LEDGER_DIR, before,
                         "main() must restore LEDGER_DIR, not leave it permanently repointed")


class TestOriginContract(LedgerTestCase):
    """`origin` is the eighth required proposal field.

    The defect this class exists to close: `agents/goal-extractor.md` has
    always instructed the extractor to emit `origin` (its Rules section:
    "A goal traceable to `active_plan` gets `origin: plan:<path>`;
    everything else gets `origin: extracted`"), `apply_decisions` has
    always accepted it, and `render_markdown` has always USED it to mark a
    goal `(inferred)` — but `parse_extraction` sat in the middle rejecting
    the whole extraction over an "unrecognized field: 'origin'". Pedro's
    first real (non-dry) run hit exactly that and no goal could ever be
    stored. The chain is completed here rather than amputated: `origin` is
    how a goal traceable to an approved plan is told apart from one
    inferred from the session, which is a real distinction the mirror
    already renders.
    """

    def _shaped_as_the_agent_instructs(self, **over) -> dict:
        """A proposal carrying exactly the field set the agent file's
        return contract demands, built here INDEPENDENTLY of `_proposal`
        so this test cannot be satisfied by a helper that drifted."""
        base = {"id": "new-1", "level": "goal", "parent": None, "tag": "new",
                "was": None, "text": "Add rate limiting", "evidence": "added rateLimit.ts",
                "origin": "extracted"}
        base.update(over)
        return base

    def test_a_proposal_shaped_exactly_as_the_agent_instructs_parses(self) -> None:
        # THE assertion whose absence let this ship. Everything else in
        # this class is a boundary around it.
        items = goals.parse_extraction(_block([self._shaped_as_the_agent_instructs()]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["origin"], "extracted")

    def test_a_plan_traceable_origin_is_accepted_and_preserved(self) -> None:
        block = _block([self._shaped_as_the_agent_instructs(
            origin="plan:/home/pedro/.claude/plans/some-plan.md")])
        items = goals.parse_extraction(block)
        self.assertEqual(items[0]["origin"], "plan:/home/pedro/.claude/plans/some-plan.md")

    def test_a_missing_origin_is_rejected(self) -> None:
        proposal = self._shaped_as_the_agent_instructs()
        del proposal["origin"]
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(_block([proposal]))
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("missing a required field", ctx.exception.message)
        self.assertIn("'origin'", ctx.exception.message)

    def test_a_null_origin_is_rejected(self) -> None:
        # `null` stays legal ONLY on `parent`/`was`. A ledger record may
        # hold `origin: null` (hand-edited, pre-validation) and
        # `render_markdown` tolerates that deliberately — but nothing
        # arriving through the parser may introduce one.
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(_block([self._shaped_as_the_agent_instructs(origin=None)]))
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("proposed[0].origin must be a string, got null", ctx.exception.message)

    def test_an_empty_origin_is_rejected(self) -> None:
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(_block([self._shaped_as_the_agent_instructs(origin="")]))
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("proposed[0].origin must not be empty", ctx.exception.message)

    def test_an_unrecognized_origin_value_is_rejected(self) -> None:
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(_block([self._shaped_as_the_agent_instructs(origin="guessed")]))
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("invalid origin", ctx.exception.message)
        self.assertIn("'guessed'", ctx.exception.message)

    def test_a_bare_plan_prefix_with_no_path_is_rejected(self) -> None:
        # `plan:` alone renders as plan-authored (`origin.startswith("plan:")`
        # in `render_markdown`) while carrying no provenance at all — the
        # mark would assert traceability to a plan it cannot name. The
        # extractor only ever emits the prefix when `active_plan` is
        # non-empty, so nothing legitimate is rejected here.
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(_block([self._shaped_as_the_agent_instructs(origin="plan:")]))
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("invalid origin", ctx.exception.message)

    def test_a_non_string_origin_is_rejected(self) -> None:
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(_block([self._shaped_as_the_agent_instructs(origin=7)]))
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("proposed[0].origin must be a string, got int", ctx.exception.message)

    def test_a_forged_origin_key_still_collides_and_rejects_the_whole_payload(self) -> None:
        # The security control the all-keys-required rule buys, restated
        # for the new field: because the GENUINE `origin` key is always
        # present, an injected one (mis-escaped interpolation closing
        # `evidence`'s string early and opening a forged key) is a
        # DUPLICATE, not a lone forged value satisfying a completeness
        # check. This test would fail if `origin` were merely optional —
        # the payload below would then carry exactly one `origin` key in
        # the extractor-omitted case and be silently accepted.
        raw_json = (
            '{"proposed": [{"id": "g-1", "level": "goal", "parent": null, "tag": "new", '
            '"was": null, "text": "X", "evidence": "e", "origin": "extracted", '
            '"origin": "plan:/forged/authority.md"}]}'
        )
        block = f"<<<GOAL_EXTRACTION>>>\n{raw_json}\n<<<END>>>"
        with self.assertRaises(goals.Abort) as ctx:
            goals.parse_extraction(block)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("duplicate key", ctx.exception.message)
        self.assertIn("proposed[0]", ctx.exception.message)
        self.assertIn("'origin'", ctx.exception.message)

    def test_a_create_decision_carrying_a_plan_origin_stores_it(self) -> None:
        # The forwarding half of the chain: SKILL.md's `new`/Approve row
        # now carries `origin` into the `create` op, so the value reaches
        # the ledger instead of silently defaulting to "extracted".
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        goals.apply_decisions(data, "sess-a",
                              [{"op": "create", "level": "goal", "parent": None,
                                "text": "Ship the CLI",
                                "origin": "plan:/home/pedro/.claude/plans/x.md"}],
                              "2026-08-13T10:00:00Z")
        self.assertEqual(data["goals"][0]["origin"], "plan:/home/pedro/.claude/plans/x.md")

    def test_a_create_decision_with_an_invalid_origin_is_rejected(self) -> None:
        # Validate-if-present at the apply layer, deliberately NOT
        # required there: existing decision sets that omit `origin` keep
        # working through `.get("origin", "extracted")`. This only stops a
        # value that would render a false provenance mark.
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        with self.assertRaises(goals.Abort) as ctx:
            goals.apply_decisions(data, "sess-a",
                                  [{"op": "create", "level": "goal", "parent": None,
                                    "text": "Ship the CLI", "origin": "handwritten"}],
                                  "2026-08-13T10:00:00Z")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("decision[0]: create: bad origin", ctx.exception.message)
        self.assertEqual(data["goals"], [], "an invalid origin must apply nothing")

    def test_a_create_decision_omitting_origin_still_defaults_to_extracted(self) -> None:
        data = {"schema": 1, "project": "Demo", "updated": None, "goals": []}
        goals.apply_decisions(data, "sess-a",
                              [{"op": "create", "level": "goal", "parent": None,
                                "text": "Ship the CLI"}],
                              "2026-08-13T10:00:00Z")
        self.assertEqual(data["goals"][0]["origin"], "extracted")


class TestRenderWithoutALedger(LedgerTestCase):
    """`goals.py render` against a project that has no ledger at all.

    Pedro's first real run wrote an 89-byte `Goals.md` into the vault —
    a heading and a generated-by comment, nothing else — for a project
    whose ledger did not exist. That file is the mirror he is meant to
    trust; a mirror of nothing should not be created in the first place.
    """

    def test_render_skips_the_write_when_no_ledger_exists(self) -> None:
        out_path = self.tmp / "mirror" / "Goals.md"
        result = goals.cmd_render("Demo", str(out_path))
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("skipped"))
        self.assertFalse(out_path.exists(),
                         "no ledger means there is nothing to mirror — write no file")

    def test_render_leaves_an_existing_mirror_untouched_when_it_skips(self) -> None:
        # Skipping must never DELETE. A file already in the vault is
        # Pedro's, not this command's to remove.
        out_path = self.tmp / "mirror" / "Goals.md"
        out_path.parent.mkdir(parents=True)
        out_path.write_text("# Goals — Demo\n\n- [ ] something older\n", encoding="utf-8")
        result = goals.cmd_render("Demo", str(out_path))
        self.assertTrue(result["ok"])
        self.assertIn("something older", out_path.read_text(encoding="utf-8"))

    def test_render_still_writes_when_a_ledger_exists_with_no_visible_goals(self) -> None:
        # The distinction that makes the skip safe: a ledger that exists
        # but holds nothing visible (every goal dropped) MUST still
        # regenerate the mirror, or the file keeps showing goals the user
        # already closed out.
        goals.ledger_path("demo").write_text(
            json.dumps({"schema": 1, "project": "Demo",
                        "goals": [seed("g-1111", status="dropped")]}),
            encoding="utf-8")
        out_path = self.tmp / "mirror" / "Goals.md"
        result = goals.cmd_render("Demo", str(out_path))
        self.assertTrue(result["ok"])
        self.assertFalse(result.get("skipped"))
        rendered = out_path.read_text(encoding="utf-8")
        self.assertNotIn("Fix login bug", rendered)
        self.assertIn("Dropped: 1 goal(s)", rendered)


class TestRunReceipt(LedgerTestCase):
    """Task 11 Part 1: the run receipt — the one artifact a /progress-report run
    that SKIPPED the goal step cannot produce.

    It records that `goals.py` RAN. It is not evidence that a human was asked
    anything, and nothing here should be read as claiming that.
    """

    def _cli(self, *argv: str, run_dir: Path | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ, GOALS_LEDGER_DIR=str(goals.LEDGER_DIR),
                   GOALS_LOCK_DIR=str(goals.LOCK_DIR),
                   GOALS_RUN_DIR=str(goals.RUN_DIR if run_dir is None else run_dir))
        return subprocess.run([sys.executable, str(SCRIPTS / "goals.py"), *argv],
                              capture_output=True, text=True, env=env)

    def _receipt(self, session: str) -> dict:
        return json.loads((goals.RUN_DIR / (session + ".json")).read_text(encoding="utf-8"))

    _EXTRACTION_BODY = ('{"proposed": [{"id": "g-1111", "level": "goal", "parent": null, '
                        '"tag": "new", "was": null, "text": "Ship the receipt", '
                        '"evidence": "none", "origin": "extracted"}]}')

    def _extraction_file(self) -> str:
        path = self.tmp / "extraction.txt"
        path.write_text(f"<<<GOAL_EXTRACTION>>>\n{self._EXTRACTION_BODY}\n<<<END>>>\n",
                        encoding="utf-8")
        return str(path)

    def test_load_writes_a_receipt_with_the_documented_shape(self) -> None:
        proc = self._cli("load", "--project", "Demo", "--session", "sess-a")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = self._receipt("sess-a")
        self.assertEqual(receipt["session"], "sess-a")
        self.assertEqual(len(receipt["entries"]), 1)
        entry = receipt["entries"][0]
        self.assertEqual(entry["cmd"], "load")
        self.assertEqual(entry["exit"], 0)
        self.assertRegex(entry["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_commit_writes_a_receipt(self) -> None:
        decisions = self.tmp / "decisions.json"
        decisions.write_text(json.dumps({"session": "sess-c", "decisions": []}), encoding="utf-8")
        proc = self._cli("commit", "--project", "Demo", "--session", "sess-c",
                         "--decisions", str(decisions))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([e["cmd"] for e in self._receipt("sess-c")["entries"]], ["commit"])

    def test_parse_extraction_writes_a_receipt_when_given_a_session(self) -> None:
        proc = self._cli("parse-extraction", "--file", self._extraction_file(),
                         "--session", "sess-p")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([e["cmd"] for e in self._receipt("sess-p")["entries"]],
                         ["parse-extraction"])

    def test_parse_extraction_without_a_session_writes_nothing_and_still_works(self) -> None:
        # --session is optional on this subcommand: it needs none to do its
        # work. SKILL.md Step 4b must pass it anyway, which is a separate
        # assertion in tests/test_skill_goal_wiring.sh.
        proc = self._cli("parse-extraction", "--file", self._extraction_file())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(goals.RUN_DIR.exists(), "no session, no receipt, no directory")

    def test_render_writes_no_receipt(self) -> None:
        goals.ledger_path("demo").write_text(
            json.dumps({"schema": 1, "project": "Demo", "goals": [seed("g-1111")]}),
            encoding="utf-8")
        proc = self._cli("render", "--project", "Demo", "--out", str(self.tmp / "Goals.md"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(goals.RUN_DIR.exists(), "render names no session")

    def test_a_failing_command_still_records_its_real_exit_code(self) -> None:
        # The distinction the validator depends on: a ledger that failed LOUDLY
        # is a legitimate reason to finish a run without a commit, and only a
        # recorded non-zero exit tells that apart from "the step never ran".
        bad = self.tmp / "not-json.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        proc = self._cli("commit", "--project", "Demo", "--session", "sess-fail",
                         "--decisions", str(bad))
        self.assertEqual(proc.returncode, 2)
        entry = self._receipt("sess-fail")["entries"][0]
        self.assertEqual(entry["cmd"], "commit")
        self.assertEqual(entry["exit"], 2)

    def test_abort_still_prints_its_one_stderr_line_alongside_the_receipt(self) -> None:
        proc = self._cli("load", "--project", "", "--session", "sess-abort")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("project must be a non-empty string", proc.stderr)
        self.assertEqual(self._receipt("sess-abort")["entries"][0]["exit"], 2)

    def test_entries_are_capped_at_twenty_keeping_the_most_recent(self) -> None:
        for _ in range(25):
            self.assertEqual(
                self._cli("load", "--project", "Demo", "--session", "sess-cap").returncode, 0)
        entries = self._receipt("sess-cap")["entries"]
        self.assertEqual(len(entries), goals.RUN_RECEIPT_MAX_ENTRIES)
        self.assertEqual(len(entries), 20)
        self.assertTrue(all(e["cmd"] == "load" for e in entries))

    def test_a_receipt_write_failure_does_not_change_the_command_exit_code(self) -> None:
        # GOALS_RUN_DIR pointed at a path that cannot be a directory: the file
        # below already occupies it, so mkdir raises NotADirectoryError/EEXIST.
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("occupied", encoding="utf-8")
        proc = self._cli("load", "--project", "Demo", "--session", "sess-nowrite",
                         run_dir=blocker / "goal-run")
        self.assertEqual(proc.returncode, 0, "the subcommand's own result is unchanged")
        self.assertIn('"owned"', proc.stdout, "the load result still reached stdout")
        receipt_lines = [line for line in proc.stderr.strip().splitlines() if line.strip()]
        self.assertEqual(len(receipt_lines), 1, proc.stderr)
        self.assertIn("run receipt not written", receipt_lines[0])

    def test_a_receipt_failure_beside_an_abort_prints_both_lines_and_keeps_the_code(self) -> None:
        # The one case the two stderr contracts overlap: an aborting subcommand
        # whose receipt ALSO cannot be written. Two lines is the accepted
        # behaviour, not a regression — "one stderr line per non-zero exit"
        # governs the abort, and the receipt line is additional to it rather
        # than a replacement (see `_record_run`'s docstring). What must not
        # happen is either line swallowing the other, or the receipt failure
        # rewriting the exit code the caller actually asked about.
        blocker = self.tmp / "not-a-dir-abort"
        blocker.write_text("occupied", encoding="utf-8")
        proc = self._cli("load", "--project", "", "--session", "sess-bothfail",
                         run_dir=blocker / "goal-run")
        self.assertEqual(proc.returncode, 2, "the abort's own code survives the receipt failure")
        lines = [line for line in proc.stderr.strip().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2, proc.stderr)
        # ORDER is pinned, not just membership: the receipt line prints FIRST
        # (it is written in the `finally` on the way out, before the abort
        # handler at `__main__` prints its message). Anything reading this
        # stderr as a single line — `head -1`, or SKILL.md Step 4b's "surface
        # the stderr line", singular — therefore picks the receipt line and not
        # the abort that actually explains the exit code. Pinned here so the
        # trap is documented at the place that produces it.
        self.assertIn("run receipt not written", lines[0], proc.stderr)
        self.assertIn("project must be a non-empty string", lines[1], proc.stderr)

    def test_an_unusable_session_id_gets_no_receipt_and_no_traversal(self) -> None:
        # Fails closed: no file at all, never a sanitized one attributing this
        # run to some other session's receipt.
        proc = self._cli("load", "--project", "Demo", "--session", "../evil")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("run receipt not written", proc.stderr)
        self.assertFalse((goals.RUN_DIR.parent / "evil.json").exists())
        self.assertEqual(list(goals.RUN_DIR.glob("*")) if goals.RUN_DIR.exists() else [], [])

    def test_an_over_long_session_id_gets_no_receipt(self) -> None:
        # Bounded at 128 to match what validate_report.py will accept for
        # --stamp-session: a receipt under a name the validator can never look
        # up is worse than no receipt, because it reads as success here.
        proc = self._cli("load", "--project", "Demo", "--session", "s" * 129)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("run receipt not written", proc.stderr)

    def test_a_corrupt_receipt_is_replaced_rather_than_aborted_on(self) -> None:
        # UnicodeDecodeError/JSONDecodeError are ValueError subclasses, not
        # OSError. A truncated receipt must cost this run its history, never
        # its own entry — losing the entry is what makes the validator reject a
        # report whose goal step actually ran.
        goals.RUN_DIR.mkdir(parents=True, exist_ok=True)
        (goals.RUN_DIR / "sess-corrupt.json").write_bytes(b'{"entries": [\xff\xfe')
        proc = self._cli("load", "--project", "Demo", "--session", "sess-corrupt")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr.strip(), "")
        self.assertEqual([e["cmd"] for e in self._receipt("sess-corrupt")["entries"]], ["load"])

    def test_receipt_entries_survive_across_separate_invocations(self) -> None:
        self._cli("load", "--project", "Demo", "--session", "sess-multi")
        self._cli("parse-extraction", "--file", self._extraction_file(), "--session", "sess-multi")
        decisions = self.tmp / "d.json"
        decisions.write_text(json.dumps({"session": "sess-multi", "decisions": []}),
                             encoding="utf-8")
        self._cli("commit", "--project", "Demo", "--session", "sess-multi",
                  "--decisions", str(decisions))
        self.assertEqual([e["cmd"] for e in self._receipt("sess-multi")["entries"]],
                         ["load", "parse-extraction", "commit"])

    def test_bad_cli_args_exit_without_an_unbound_local(self) -> None:
        # The receipt bookkeeping is bound only after parse_args succeeds. A
        # flat try/finally spanning parse_args would turn argparse's SystemExit
        # into an UnboundLocalError, breaking the "nothing but an Abort
        # escapes" contract at the boundary that exists to uphold it.
        proc = self._cli("load", "--project", "Demo")
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("UnboundLocalError", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    # ------------------------------------------------------- project + effect
    # The receipt used to record only that a subprocess RAN. It now records what
    # the run was ABOUT (`project`) and what it DID (`effect`), because
    # "a subprocess ran" was too weak a claim to hang a report on.

    def test_commit_records_the_slug_and_the_applied_count(self) -> None:
        decisions = self.tmp / "d.json"
        decisions.write_text(json.dumps({"decisions": [
            {"op": "create", "level": "goal", "parent": None,
             "text": "Ship the receipt", "origin": "extracted"}]}), encoding="utf-8")
        proc = self._cli("commit", "--project", "Demo Project", "--session", "sess-eff",
                         "--decisions", str(decisions))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self._receipt("sess-eff")["entries"][0]
        # The SLUG, not the raw --project string: the validator compares against
        # a slug it derives with this module's own `slugify`.
        self.assertEqual(entry["project"], "demo-project")
        self.assertEqual(entry["project"], goals.slugify("Demo Project"))
        # The value the command actually returned, not the batch size.
        self.assertEqual(entry["effect"], json.loads(proc.stdout)["applied"])
        self.assertEqual(entry["effect"], 1)

    def test_a_zero_effect_commit_is_recorded_as_zero_not_as_absent(self) -> None:
        # The Red Teamer's forgery at the writer boundary: `{"decisions": []}`
        # exits 0 and is a perfectly legal no-op. It must be recorded as the
        # zero-effect run it is, so a report declaring any non-zero count cannot
        # ride on it.
        decisions = self.tmp / "noop.json"
        decisions.write_text('{"decisions": []}', encoding="utf-8")
        proc = self._cli("commit", "--project", "Demo", "--session", "sess-noop",
                         "--decisions", str(decisions))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self._receipt("sess-noop")["entries"][0]
        self.assertEqual(entry["effect"], 0)
        self.assertIn("effect", entry)

    def test_parse_extraction_records_the_proposal_count_and_the_project(self) -> None:
        # `--project` is OPTIONAL on this subcommand and exists for the receipt
        # alone, on exactly the terms `--session` already had. This test
        # previously asserted the opposite — that the flag did not and must not
        # exist — and that left the validator's `proposed` half bound to no
        # project at all, so any earlier or foreign entry with a coinciding
        # count validated a report. The reasoning it rested on was right about a
        # REQUIRED argument and wrong about an optional one.
        proc = self._cli("parse-extraction", "--file", self._extraction_file(),
                         "--session", "sess-pe", "--project", "Demo")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self._receipt("sess-pe")["entries"][0]
        # The resolved SLUG, not the raw string: `validate_report.py` compares
        # against a slug it derives with goals.py's own `slugify`, and two
        # derivations that agree today and drift later is a defect this project
        # has already shipped once.
        self.assertEqual(entry["project"], "demo")
        self.assertEqual(entry["effect"], len(json.loads(proc.stdout)["proposed"]))
        self.assertEqual(entry["effect"], 1)

    def test_parse_extraction_project_is_slugified_not_stored_raw(self) -> None:
        # The same derivation the ledger path uses, exercised on a name whose
        # slug differs from it — `entry["project"] == "Demo Project"` would pass
        # a raw-string comparison and fail every real validation.
        proc = self._cli("parse-extraction", "--file", self._extraction_file(),
                         "--session", "sess-peslug", "--project", "Demo Project")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._receipt("sess-peslug")["entries"][0]["project"],
                         goals.slugify("Demo Project"))
        self.assertEqual(self._receipt("sess-peslug")["entries"][0]["project"], "demo-project")

    def test_parse_extraction_without_a_project_records_null_and_still_parses(self) -> None:
        # `--project` is receipt enrichment, not a new requirement on the parse:
        # omitting it still parses, still exits 0, and records `project: null`.
        # What it does NOT do is corroborate a counts declaration — the
        # validator fails closed on a null-project entry, which is pinned in
        # test_progress_report_scripts.py::
        #   test_a_null_project_parse_extraction_entry_fails_closed
        proc = self._cli("parse-extraction", "--file", self._extraction_file(),
                         "--session", "sess-penp")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self._receipt("sess-penp")["entries"][0]
        self.assertIsNone(entry["project"])
        self.assertEqual(entry["effect"], 1)

    def test_the_project_flag_does_not_change_what_parse_extraction_returns(self) -> None:
        # The load-bearing property of an argument added for a guard's benefit:
        # nothing in the parse reads it. Asserted on the WHOLE payload, not on
        # the exit code or a field of it — a check that compared only `proposed`
        # length would pass for a flag that quietly rewrote a proposal's text.
        source = self._extraction_file()
        without = self._cli("parse-extraction", "--file", source, "--session", "sess-pw1")
        with_project = self._cli("parse-extraction", "--file", source,
                                 "--session", "sess-pw2", "--project", "Demo")
        self.assertEqual(without.returncode, 0, without.stderr)
        self.assertEqual(with_project.returncode, 0, with_project.stderr)
        self.assertEqual(json.loads(without.stdout), json.loads(with_project.stdout))

    def test_an_unslugifiable_project_costs_parse_extraction_neither_exit_nor_receipt(self) -> None:
        # `_receipt_project` returns None rather than raising, on every path.
        # A `--project` that slugifies to nothing must not abort a parse that
        # never needed the value, and must not cost the run its receipt.
        proc = self._cli("parse-extraction", "--file", self._extraction_file(),
                         "--session", "sess-pebad", "--project", "!!!")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        entry = self._receipt("sess-pebad")["entries"][0]
        self.assertIsNone(entry["project"])
        self.assertEqual(entry["effect"], 1)

    def test_a_proposal_less_extraction_records_a_null_effect_never_zero(self) -> None:
        # This is the writer-side fact the validator's `proposed=0` reasoning
        # rests on, pinned where it is actually observable. `parse-extraction`
        # ABORTS on a block carrying no proposals, so its entry is exit 2 with
        # a null effect. There is therefore no way for goals.py to emit a clean
        # (exit 0) `parse-extraction` entry carrying `effect: 0`, which is what
        # makes a `proposed=0` declaration uncorroborable by construction.
        #
        # The guarantee rests on a structural property, checked by reading
        # `parse_extraction`: between the `if not items` guard and `return
        # items`, nothing filters, drops, or reassigns `items` — the remaining
        # checks (duplicate id, goal/subgoal caps) only ever raise. So a
        # non-raising return always carries >= 1 item. ADDING ANY POST-GUARD
        # FILTER WOULD BREAK THIS: two proposals that both get dropped would
        # exit 0 with `effect: 0`, and `check_goal_accounting` would then need
        # an explicit zero-count check, because it has none today.
        empty = self.tmp / "empty-extraction.txt"
        empty.write_text('<<<GOAL_EXTRACTION>>>\n{"proposed": []}\n<<<END>>>\n',
                         encoding="utf-8")
        proc = self._cli("parse-extraction", "--file", str(empty), "--session", "sess-zp")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        entry = self._receipt("sess-zp")["entries"][0]
        self.assertEqual(entry["exit"], 2)
        self.assertIsNone(entry["effect"],
                          "a proposal-less abort must not record effect 0")

    def test_load_records_its_project_but_no_effect(self) -> None:
        # `load` reaches no decision, so it has no effect to state. Null, not 0
        # — 0 is a real count and would read as "committed nothing".
        proc = self._cli("load", "--project", "Demo", "--session", "sess-ld")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self._receipt("sess-ld")["entries"][0]
        self.assertEqual(entry["project"], "demo")
        self.assertIsNone(entry["effect"])

    def test_an_aborted_commit_still_records_its_project_with_a_null_effect(self) -> None:
        # A loud ledger failure must stay distinguishable from a goal step that
        # never ran, and it must still say which project it was about.
        proc = self._cli("commit", "--project", "Demo", "--session", "sess-ab",
                         "--decisions", "/nonexistent/path.json")
        self.assertEqual(proc.returncode, 2)
        entry = self._receipt("sess-ab")["entries"][0]
        self.assertEqual(entry["exit"], 2)
        self.assertEqual(entry["project"], "demo")
        self.assertIsNone(entry["effect"], "an abort produced no effect to record")

    def test_a_project_that_cannot_be_slugified_still_leaves_a_receipt(self) -> None:
        # `slugify` aborts on a name that produces an empty slug. Losing the
        # receipt over it would delete the signal that this run failed loudly —
        # the abort is recorded, with a null project, which reads downstream as
        # "not this project" and fails closed.
        decisions = self.tmp / "d.json"
        decisions.write_text('{"decisions": []}', encoding="utf-8")
        # `"!!!"` and not `"---"`: argparse rejects a leading-dash value as an
        # option before this module ever sees it, which would test argparse
        # rather than the receipt. Both slugify to the empty string.
        proc = self._cli("commit", "--project", "!!!", "--session", "sess-slug",
                         "--decisions", str(decisions))
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)
        entry = self._receipt("sess-slug")["entries"][0]
        self.assertIsNone(entry["project"])

    def test_a_receipt_write_failure_still_leaves_loads_exit_code_alone(self) -> None:
        # The pre-existing contract, re-verified with the new fields in play: a
        # lost receipt degrades the guarantee for one run; it may never change
        # the exit code of the subcommand the caller actually asked for.
        proc = self._cli("load", "--project", "Demo", "--session", "s" * 129)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("run receipt not written", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_env_override_is_restored_after_an_in_process_main_call(self) -> None:
        original = goals.RUN_DIR
        elsewhere = self.tmp / "other-run"
        os.environ["GOALS_RUN_DIR"] = str(elsewhere)
        try:
            goals.main(["load", "--project", "Demo", "--session", "sess-env"])
        finally:
            os.environ.pop("GOALS_RUN_DIR", None)
        self.assertIs(goals.RUN_DIR, original, "RUN_DIR must be restored like LEDGER_DIR is")
        self.assertTrue((elsewhere / "sess-env.json").is_file(),
                        "the receipt lands in the overridden dir, not the restored one")


class TestThinSessionSubcommand(LedgerTestCase):
    """Task 11 round 4: the recorded thin skip.

    `thin-session` exists so that skipping the goal step leaves an artifact like
    every other path through Step 4b does. It records — it does not decide: the
    metrics are the caller's, written down verbatim, and `validate_report.py` is
    what refuses a declaration whose metrics do not satisfy the condition.

    Nothing here is evidence that a human was asked anything.
    """

    def _cli(self, *argv: str) -> subprocess.CompletedProcess:
        # HOME is redirected alongside the three GOALS_* vars, not because this
        # subcommand reads it — it reaches neither `session_live` nor
        # `PROJECTS_DIR` — but because a test that redirects only the vars it
        # believes are used is how a probe in this project leaked a real stamp
        # into ~/.claude/cache. The isolation is asserted, not reasoned about.
        env = dict(os.environ, HOME=str(self.tmp / "home"),
                   GOALS_LEDGER_DIR=str(goals.LEDGER_DIR),
                   GOALS_LOCK_DIR=str(goals.LOCK_DIR),
                   GOALS_RUN_DIR=str(goals.RUN_DIR))
        return subprocess.run([sys.executable, str(SCRIPTS / "goals.py"), *argv],
                              capture_output=True, text=True, env=env)

    def _receipt(self, session: str) -> dict:
        return json.loads((goals.RUN_DIR / (session + ".json")).read_text(encoding="utf-8"))

    @staticmethod
    def _snapshot(*directories: Path) -> list[tuple[str, int, int]]:
        """Every file under these directories as `(path, size, mtime_ns)`.

        Size AND mtime, not a bare listing: a rewrite that happened to produce
        the same length would be invisible to a name-only comparison, and this is
        the assertion standing behind "a thin session does not touch the ledger".
        """
        found = []
        for directory in directories:
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    stat = path.stat()
                    found.append((str(path), stat.st_size, stat.st_mtime_ns))
        return found

    def _seed_ledger(self) -> None:
        """A real ledger file for the project the thin run names. Without it the
        before/after comparison is two empty directories, which is satisfied by
        code that could not write anything at all."""
        goals.ledger_path("demo").write_text(
            json.dumps({"schema": 1, "project": "Demo", "goals": [seed("g-1111")]}),
            encoding="utf-8")

    def test_it_records_the_skip_with_the_project_and_both_metrics(self) -> None:
        proc = self._cli("thin-session", "--project", "Demo", "--session", "sess-thin",
                         "--event-count", "3", "--active-msg-count", "12")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = self._receipt("sess-thin")["entries"][0]
        self.assertEqual(entry["cmd"], "thin-session")
        self.assertEqual(entry["cmd"], goals.THIN_SESSION_CMD, "the name the reader matches on")
        self.assertEqual(entry["exit"], 0)
        self.assertEqual(entry["project"], "demo", "slugified by the same slugify every entry uses")
        self.assertEqual(entry["metrics"], {"event_count": 3, "active_msg_count": 12})
        self.assertIsNone(entry["effect"], "effect is not overloaded to carry the metrics")
        self.assertRegex(entry["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_it_writes_no_ledger_file_and_takes_no_lock(self) -> None:
        # [EDGE] The promise SKILL.md Step 4b makes about a thin session — "no
        # gate, no ledger write" — asserted rather than described. A receipt
        # write is not a ledger write; dry runs already do one.
        self._seed_ledger()
        before = self._snapshot(goals.LEDGER_DIR, goals.LOCK_DIR)
        self.assertTrue(before, "the comparison must not be over an empty tree")
        proc = self._cli("thin-session", "--project", "Demo", "--session", "sess-noledger",
                         "--event-count", "1", "--active-msg-count", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._snapshot(goals.LEDGER_DIR, goals.LOCK_DIR), before,
                         "ledger and lock directories are byte-identical after the call")
        self.assertTrue((goals.RUN_DIR / "sess-noledger.json").is_file(),
                        "the receipt is the ONLY thing this command writes")

    def test_it_records_metrics_that_do_not_satisfy_the_condition(self) -> None:
        # The division of labour, pinned: this command records, the validator
        # judges. Rejecting here would make the run exit non-zero, and a non-zero
        # entry for this project is exactly what `validate_report.py` accepts as
        # corroboration for a `ledger-error` declaration — a guard that opened a
        # cheaper path into a different value than the one it closed.
        proc = self._cli("thin-session", "--project", "Demo", "--session", "sess-fat",
                         "--event-count", "900", "--active-msg-count", "4")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(json.loads(proc.stdout)["thin"], False,
                      "the result says plainly that these metrics are not thin")
        entry = self._receipt("sess-fat")["entries"][0]
        self.assertEqual(entry["exit"], 0, "a recorded non-thin claim is not a ledger failure")
        self.assertEqual(entry["metrics"], {"event_count": 900, "active_msg_count": 4})

    def test_the_thin_verdict_uses_the_one_shared_comparison(self) -> None:
        # `is_thin_session` is the single definition of the condition:
        # `validate_report.py` imports it rather than restating `< 5` and `< 30`,
        # and this command's own `thin` field is computed by it too.
        self.assertEqual((goals.THIN_EVENT_COUNT_MAX, goals.THIN_ACTIVE_MSG_COUNT_MAX), (5, 30))
        self.assertTrue(goals.is_thin_session(4, 29))
        self.assertFalse(goals.is_thin_session(5, 29), "strict <, so 5 events is not thin")
        self.assertFalse(goals.is_thin_session(4, 30), "strict <, so 30 messages is not thin")
        self.assertFalse(goals.is_thin_session(5, 30))

    def test_an_unusable_project_aborts_and_records_no_metrics(self) -> None:
        # The abort still leaves a receipt — that is the existing contract — but
        # the entry carries no metrics, so it cannot justify a thin declaration.
        # A run that failed before it resolved a project recorded nothing about
        # the session's size.
        proc = self._cli("thin-session", "--project", "", "--session", "sess-badproj",
                         "--event-count", "1", "--active-msg-count", "1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("project must be a non-empty string", proc.stderr)
        entry = self._receipt("sess-badproj")["entries"][0]
        self.assertEqual(entry["exit"], 2)
        self.assertNotIn("metrics", entry)

    def test_a_non_integer_count_is_rejected_before_anything_is_recorded(self) -> None:
        # argparse's own `type=int` failure exits before the receipt machinery is
        # reachable, so it leaves NO entry. That is the right place for this
        # rejection: an abort raised inside the command body would record a
        # non-zero exit for this project, which the validator's `ledger-error`
        # branch accepts as corroboration.
        proc = self._cli("thin-session", "--project", "Demo", "--session", "sess-nonint",
                         "--event-count", "many", "--active-msg-count", "1")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse((goals.RUN_DIR / "sess-nonint.json").exists())

    def test_both_metrics_are_required(self) -> None:
        # Neither flag is optional. A thin skip recorded without its numbers is
        # the artifact this round exists to stop accepting.
        for missing, argv in (
                ("--active-msg-count", ["--event-count", "1"]),
                ("--event-count", ["--active-msg-count", "1"])):
            with self.subTest(missing=missing):
                proc = self._cli("thin-session", "--project", "Demo",
                                 "--session", "sess-missing", *argv)
                self.assertEqual(proc.returncode, 2)
                self.assertIn(missing, proc.stderr)

    def test_the_receipt_cap_and_the_exit_code_contract_still_hold(self) -> None:
        # Every existing `_record_run` rule binds on the new path too: the
        # 20-entry cap, and a receipt write failure that leaves the subcommand's
        # own exit code alone with one stderr line.
        for _ in range(22):
            self.assertEqual(
                self._cli("thin-session", "--project", "Demo", "--session", "sess-cap",
                          "--event-count", "1", "--active-msg-count", "1").returncode, 0)
        entries = self._receipt("sess-cap")["entries"]
        self.assertEqual(len(entries), goals.RUN_RECEIPT_MAX_ENTRIES)
        self.assertTrue(all(e["cmd"] == "thin-session" for e in entries))

    def test_a_receipt_write_failure_does_not_change_this_commands_exit_code(self) -> None:
        blocker = self.tmp / "not-a-dir-thin"
        blocker.write_text("occupied", encoding="utf-8")
        env = dict(os.environ, HOME=str(self.tmp / "home"),
                   GOALS_LEDGER_DIR=str(goals.LEDGER_DIR), GOALS_LOCK_DIR=str(goals.LOCK_DIR),
                   GOALS_RUN_DIR=str(blocker / "goal-run"))
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "goals.py"), "thin-session", "--project", "Demo",
             "--session", "sess-nowrite", "--event-count", "1", "--active-msg-count", "1"],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, "the subcommand's own result is unchanged")
        self.assertIn('"thin"', proc.stdout, "the result still reached stdout")
        lines = [line for line in proc.stderr.strip().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, proc.stderr)
        self.assertIn("run receipt not written", lines[0])

    def test_other_subcommands_gained_no_metrics_field(self) -> None:
        # The field is scoped to the one command whose declaration is checked
        # against numbers it did not produce. Adding `"metrics": null` to the
        # other four would change four entry shapes to buy nothing — the reader
        # already distinguishes an absent key from a null one.
        self.assertEqual(
            self._cli("load", "--project", "Demo", "--session", "sess-load").returncode, 0)
        self.assertNotIn("metrics", self._receipt("sess-load")["entries"][0])


if __name__ == "__main__":
    unittest.main()
