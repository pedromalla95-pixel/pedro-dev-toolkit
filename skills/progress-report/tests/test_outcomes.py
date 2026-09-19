#!/usr/bin/env python3
"""Tests for outcomes.py. Every case runs against a scratch dir; the nine real
ledgers under ~/.claude/goal-ledger/ are never opened."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# VERSION FLOOR: Python 3.11+, for contextlib.chdir (new in 3.11). It replaces
# six try/finally os.chdir blocks in TestWorktreeRefusal. Bare, the failure is
# an ImportError naming a name, with no hint that a version is the reason; this
# turns it into a sentence a reader can act on. The floor is checked rather
# than only documented precisely because the unguarded failure carries no
# version in it. (The other floor in this feature, git 2.31/2.13, is documented
# instead of checked -- see assert_not_in_worktree in scripts/outcomes.py,
# where too old a git degrades to a clean "cannot tell" rather than an error.)
try:
    from contextlib import chdir
except ImportError:                       # Python 3.10 and older
    raise SystemExit(
        f"test_outcomes.py needs Python 3.11+ for contextlib.chdir; this "
        f"interpreter is {sys.version.split()[0]} at {sys.executable}. "
        f"Re-run it with a 3.11+ python3.")

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goals      # noqa: E402
import outcomes   # noqa: E402


class OutcomeTestCase(unittest.TestCase):
    """Repoints goals.py's three directories at a scratch dir, exactly as
    LedgerTestCase in test_goals.py does. outcomes.py reads goals.LEDGER_DIR at
    call time, so this repoint reaches it too."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="outcomes-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = (goals.LEDGER_DIR, goals.LOCK_DIR, goals.RUN_DIR)
        goals.LEDGER_DIR = self.tmp / "goal-ledger"
        goals.LOCK_DIR = self.tmp / "cache"
        goals.RUN_DIR = self.tmp / "cache" / "goal-run"
        goals.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        goals.LOCK_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        goals.LEDGER_DIR, goals.LOCK_DIR, goals.RUN_DIR = self._orig

    def seed(self, *records: dict) -> None:
        data = {"schema": outcomes.SCHEMA,
                "updated": goals._now_iso(),
                "outcomes": list(records)}
        outcomes.outcomes_path().write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8")


def make(**over) -> dict:
    """A valid outcome record; override any field."""
    rec = {
        "id": "outcome-0001",
        "statement": "Rosa can count stock on a phone without paper",
        "projects": ["cucina-inventory"],
        "station": "captured",
        "status": "open",
        "wake": None,
        "claim": None,
        "created": "2026-08-27T12:00:00Z",
        "last_seen": "2026-08-27T12:00:00Z",
        "pendings": [],
        "history": [],
    }
    rec.update(over)
    return rec


class TestReadWrite(OutcomeTestCase):

    def test_missing_file_reads_as_empty_schema(self):
        data = outcomes.read_outcomes()
        self.assertEqual(data["schema"], outcomes.SCHEMA)
        self.assertEqual(data["outcomes"], [])

    def test_write_then_read_roundtrips(self):
        data = {"schema": outcomes.SCHEMA, "updated": goals._now_iso(),
                "outcomes": [make()]}
        with outcomes.outcomes_lock():
            outcomes.write_outcomes(data)
        back = outcomes.read_outcomes()
        self.assertEqual(len(back["outcomes"]), 1)
        self.assertEqual(back["outcomes"][0]["id"], "outcome-0001")

    def test_write_without_lock_aborts_5(self):
        data = {"schema": outcomes.SCHEMA, "updated": goals._now_iso(),
                "outcomes": []}
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.write_outcomes(data)
        self.assertEqual(ctx.exception.code, 5)

    def test_write_is_atomic_leaves_no_tmp_file(self):
        with outcomes.outcomes_lock():
            outcomes.write_outcomes({"schema": outcomes.SCHEMA,
                                     "updated": goals._now_iso(),
                                     "outcomes": [make()]})
        leftovers = [p.name for p in goals.LEDGER_DIR.iterdir()
                     if p.name.startswith(".")]
        self.assertEqual(leftovers, [])


class TestIds(OutcomeTestCase):

    def test_first_id_is_0001(self):
        self.assertEqual(outcomes.new_outcome_id([]), "outcome-0001")

    def test_id_skips_used_numbers(self):
        existing = [make(id="outcome-0001"), make(id="outcome-0003")]
        self.assertEqual(outcomes.new_outcome_id(existing), "outcome-0004")

    def test_id_ignores_malformed_ids(self):
        existing = [make(id="outcome-0001"), make(id="junk")]
        self.assertEqual(outcomes.new_outcome_id(existing), "outcome-0002")


class TestShape(OutcomeTestCase):

    def _bad(self, **over):
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.check_shape({"schema": outcomes.SCHEMA, "updated": "x",
                                  "outcomes": [make(**over)]})
        self.assertEqual(ctx.exception.code, 2)
        return str(ctx.exception.message)

    def test_valid_record_passes(self):
        outcomes.check_shape({"schema": outcomes.SCHEMA, "updated": "x",
                              "outcomes": [make()]})

    def test_unknown_station_rejected(self):
        self.assertIn("station", self._bad(station="halfway"))

    def test_unknown_status_rejected(self):
        self.assertIn("status", self._bad(status="pending"))

    def test_empty_statement_rejected(self):
        self.assertIn("statement", self._bad(statement="   "))

    def test_projects_must_be_a_list_of_strings(self):
        self.assertIn("projects", self._bad(projects="cucina-inventory"))

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.check_shape({"schema": outcomes.SCHEMA, "updated": "x",
                                  "outcomes": [make(), make()]})
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("duplicate", str(ctx.exception.message).lower())

    def test_wrong_schema_version_rejected(self):
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.check_shape({"schema": 99, "updated": "x", "outcomes": []})
        self.assertEqual(ctx.exception.code, 2)


class TestPendingRowShape(OutcomeTestCase):
    """The rows inside `pendings`, which check_shape validated no further than
    "is a list". cmd_pendings_next and unblocked() index `status` and
    `depends_on` straight off a stored row, so a hand-edited ledger raised
    KeyError -- exit 1, outside the fixed code set, out of a main() whose
    contract is that nothing but an Abort escapes it.
    """

    def _bad(self, row):
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.check_shape({"schema": outcomes.SCHEMA, "updated": "x",
                                  "outcomes": [make(pendings=[row])]})
        self.assertEqual(ctx.exception.code, 2)
        return str(ctx.exception.message)

    def test_a_row_without_status_is_rejected_by_id(self):
        msg = self._bad({"id": "P01", "description": "A row typed by hand.",
                         "depends_on": []})
        self.assertIn("P01", msg)
        self.assertIn("status", msg)

    def test_a_row_with_an_unknown_status_is_rejected_by_id(self):
        msg = self._bad({"id": "P02", "description": "A row typed by hand.",
                         "depends_on": [], "status": "finished"})
        self.assertIn("P02", msg)
        self.assertIn("finished", msg)

    def test_a_row_that_is_not_an_object_is_rejected_by_position(self):
        """No id to name, so the position is the most the message can honestly
        carry -- and it is enough to find the row in the file."""
        msg = self._bad("P03")
        self.assertIn("pendings[0]", msg)
        self.assertIn("object", msg)

    def test_a_row_whose_depends_on_is_not_a_list_is_rejected_by_id(self):
        msg = self._bad({"id": "P04", "description": "A row typed by hand.",
                         "depends_on": "P01", "status": "open"})
        self.assertIn("P04", msg)
        self.assertIn("depends_on", msg)

    def test_a_row_without_an_id_is_rejected_by_position(self):
        msg = self._bad({"description": "A row typed by hand.",
                         "depends_on": [], "status": "open"})
        self.assertIn("pendings[0]", msg)

    def test_a_parked_row_is_accepted(self):
        """The one status the PARSER refuses and the STORE accepts. unblocked()
        documents coping with a parked row from a hand-edited ledger; rejecting
        it here would make a file that function handles unreadable to every
        other, since check_shape runs on every read."""
        outcomes.check_shape({
            "schema": outcomes.SCHEMA, "updated": "x",
            "outcomes": [make(pendings=[{"id": "P01",
                                         "description": "A row typed by hand.",
                                         "depends_on": [], "status": "parked"}])]})

    def test_a_well_formed_row_still_passes(self):
        """Non-vacuity: the rejections above are about the defect in each row,
        not about the guard refusing every row it is shown."""
        rows = outcomes.parse_pending_block(BLOCK)
        outcomes.check_shape({"schema": outcomes.SCHEMA, "updated": "x",
                              "outcomes": [make(pendings=rows)]})


class TestCrossProjectOutcomeIsStoredOnce(OutcomeTestCase):
    """Spec acceptance A5."""

    def test_two_project_outcome_is_one_record(self):
        self.seed(make(projects=["cucina-inventory", "cucina-pos"]))
        data = outcomes.read_outcomes()
        self.assertEqual(len(data["outcomes"]), 1)
        self.assertEqual(sorted(data["outcomes"][0]["projects"]),
                         ["cucina-inventory", "cucina-pos"])
        raw = outcomes.outcomes_path().read_text(encoding="utf-8")
        self.assertEqual(raw.count('"id": "outcome-0001"'), 1)


class TestPerProjectOutcomeField(OutcomeTestCase):
    """P05 second half: an existing per-project goal links upward by id."""

    def test_goal_without_outcome_field_still_loads(self):
        """Backwards compatibility: the nine live ledgers have no such field."""
        slug = "fixture-proj"
        goals.ledger_path(slug).write_text(json.dumps({
            "schema": goals.SCHEMA, "project": slug, "updated": goals._now_iso(),
            "goals": [{"id": "g-0000", "text": "t", "level": "goal",
                       "parent": None, "status": "open", "owner_session": None,
                       "origin": "extracted", "created": "2026-08-27T12:00:00Z",
                       "last_seen": "2026-08-27T12:00:00Z", "history": []}],
        }, indent=2) + "\n", encoding="utf-8")
        data = goals.read_ledger(slug, slug)
        self.assertIsNone(outcomes.goal_outcome(data["goals"][0]))

    def test_attach_sets_the_field(self):
        goal = {"id": "g-0000"}
        outcomes.set_goal_outcome(goal, "outcome-0007")
        self.assertEqual(outcomes.goal_outcome(goal), "outcome-0007")

    def test_attach_rejects_a_malformed_id(self):
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.set_goal_outcome({"id": "g-0000"}, "outcome-7")
        self.assertEqual(ctx.exception.code, 2)


GOALS_PY = SCRIPTS / "goals.py"


class CliTestCase(OutcomeTestCase):
    """Drives goals.py as a real subprocess through the env repoint, which is the
    mechanism test_goal_ledger_e2e.sh already uses."""

    def cli(self, *args: str, expect: int = 0) -> dict:
        """On the expect==0 path, returns the parsed JSON stdout (unchanged
        behavior). On a non-zero expect, returns the completed process itself
        so callers can assert on proc.stderr — the exit code alone can't tell
        "my validation fired" from "argparse rejected the command"."""
        env = dict(os.environ,
                   GOALS_LEDGER_DIR=str(goals.LEDGER_DIR),
                   GOALS_LOCK_DIR=str(goals.LOCK_DIR),
                   GOALS_RUN_DIR=str(goals.RUN_DIR))
        proc = subprocess.run([sys.executable, str(GOALS_PY), *args],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, expect,
                         f"stdout={proc.stdout}\nstderr={proc.stderr}")
        if expect != 0:
            return proc
        return json.loads(proc.stdout) if proc.stdout.strip() else {}


class TestOutcomeCreate(CliTestCase):

    def test_create_mints_an_id_and_persists(self):
        out = self.cli("outcome-create",
                       "--statement", "Rosa can count stock on a phone without paper",
                       "--projects", "cucina-inventory,cucina-pos")
        self.assertEqual(out["id"], "outcome-0001")
        self.assertEqual(out["station"], "captured")
        self.assertEqual(out["status"], "open")
        data = outcomes.read_outcomes()
        self.assertEqual(len(data["outcomes"]), 1)

    def test_second_create_gets_the_next_id(self):
        self.cli("outcome-create", "--statement", "first thing", "--projects", "a")
        out = self.cli("outcome-create", "--statement", "second thing",
                       "--projects", "a")
        self.assertEqual(out["id"], "outcome-0002")

    def test_empty_statement_exits_2(self):
        proc = self.cli("outcome-create", "--statement", "   ", "--projects", "a",
                        expect=2)
        self.assertIn("--statement must be a non-empty sentence", proc.stderr)

    def test_no_projects_exits_2(self):
        proc = self.cli("outcome-create", "--statement", "a thing", "--projects", "",
                        expect=2)
        self.assertIn("--projects needs at least one project name", proc.stderr)


class TestOutcomeList(CliTestCase):

    def test_list_filters_by_station(self):
        self.cli("outcome-create", "--statement", "first thing", "--projects", "a")
        self.cli("outcome-create", "--statement", "second thing", "--projects", "b")
        allout = self.cli("outcome-list")
        self.assertEqual(len(allout["outcomes"]), 2)
        none = self.cli("outcome-list", "--station", "executing")
        self.assertEqual(none["outcomes"], [])

    def test_list_rejects_an_unknown_station(self):
        proc = self.cli("outcome-list", "--station", "halfway", expect=2)
        self.assertIn("unknown station 'halfway'", proc.stderr)


class TestOutcomeAttach(CliTestCase):

    def test_attach_links_a_goal_upward(self):
        self.cli("outcome-create", "--statement", "a thing", "--projects", "proj")
        goals.ledger_path("proj").write_text(json.dumps({
            "schema": goals.SCHEMA, "project": "proj", "updated": goals._now_iso(),
            "goals": [{"id": "g-0000", "text": "t", "level": "goal", "parent": None,
                       "status": "open", "owner_session": None, "origin": "extracted",
                       "created": "2026-08-27T12:00:00Z",
                       "last_seen": "2026-08-27T12:00:00Z", "history": []}],
        }, indent=2) + "\n", encoding="utf-8")

        self.cli("outcome-attach", "--outcome", "outcome-0001",
                 "--project", "proj", "--goal", "g-0000")

        data = goals.read_ledger("proj", "proj")
        self.assertEqual(outcomes.goal_outcome(data["goals"][0]), "outcome-0001")

    def test_attach_to_a_missing_outcome_exits_2(self):
        proc = self.cli("outcome-attach", "--outcome", "outcome-0099",
                        "--project", "proj", "--goal", "g-0000", expect=2)
        self.assertIn("no such outcome: outcome-0099", proc.stderr)


class TestSoleWriter(unittest.TestCase):
    """Spec acceptance A2, restated per this plan's deviation note: no file
    outside the progress-report skill's own scripts/ and tests/ writes
    goal-ledger JSON.

    This class is the ONE source of truth for the A2 gate. Step 7 does not
    re-express these patterns in shell - it runs this test. A parallel ERE in a
    bash loop is how the Python gate and the shell gate came to report different
    numbers (6 and 4) for the same tree.
    """

    # Two stages, and file-granular on purpose. A single line-scoped grep for
    # "goal-ledger AND a write primitive" misses the ordinary two-line shape:
    #
    #     path = LEDGER_DIR / f"{slug}.json"     <- has goal-ledger, no write
    #     path.write_text(json.dumps(data))      <- has a write, no goal-ledger
    #
    # So: stage 1 finds every FILE that mentions the ledger at all, stage 2 asks
    # whether that whole file contains a write primitive anywhere.
    ROOTS = ["/home/pedro/.claude/hooks", "/home/pedro/.claude/skills",
             "/home/pedro/.claude/scripts", "/home/pedro/.claude/commands"]

    # Every entry here was established by READING the file, and carries the
    # reason. Adding an entry is the sanctioned fix for a false positive;
    # loosening WRITES is not, because WRITES is what makes the gate mean
    # anything.
    ALLOWED = (
        # The writer itself: goals.py, and outcomes.py which it delegates to.
        "/home/pedro/.claude/skills/progress-report/scripts/",

        # The skill's own test suite. These four files (test_goals.py,
        # test_progress_report_scripts.py, test_goal_ledger_e2e.sh,
        # test_skill_goal_wiring.sh) do write ledger JSON - but only FIXTURE
        # ledgers, under tempfile.mkdtemp()/mktemp -d, reached through the
        # GOALS_LEDGER_DIR repoint. None of them opens one of the nine live
        # files. A fixture write is not a second writer: A2 is about who may
        # mutate Pedro's real ledger state, and a scratch dir that is deleted in
        # tearDown mutates none of it. Without this entry the gate reports its
        # own sibling suite as an A2 violation, and test_outcomes.py joins the
        # offender list the moment it exists - the gate would fail on the very
        # file that contains it.
        "/home/pedro/.claude/skills/progress-report/tests/",

        # Read + backup only, never a write. Verified by reading it:
        #   line 766/898  readJson(ledgerFile)      - reads, before and after
        #   line 869      copyFileSync(ledgerFile, BACKUP_DIR/...) - copies the
        #                 ledger OUT to ~/.claude/cache/ledger-backups/, which is
        #                 not goal-ledger/
        #   line 885      run("python3", [GOALS_PY, "commit", ...])  <- THIS is
        #                 its only mutation path, and it goes through goals.py
        # Its six writeFileSync calls target a lock file, a tmp file, a stamp,
        # a digest, a stored-goals file and a decisions file - all under cache/.
        # It is file-granular collateral, not a second writer.
        "/home/pedro/.claude/scripts/session-sync-run.js",
    )

    # No "> " here: it matches every Python `-> ` return annotation. Shell
    # redirection is caught by the two-char form plus tee.
    #
    # "json.dump(" keeps its parenthesis on purpose. Bare "json.dump" is a
    # SUBSTRING of "json.dumps", which is serialization to a string - what
    # print(json.dumps(payload)) does. That single missing character was the
    # whole reason scripts/goal-currency.py read as an A2 violation; it has no
    # open(), no write_text, no os.replace, and its docstring opens "Read-only
    # goal-ledger currency check" and states "Writes NOTHING. Takes no lock."
    #
    # Dropped from an earlier draft, and they must not come back:
    #   'open('   - matches every read in the codebase; not a write signal
    #   'w+'      - matches any two adjacent characters w and +, everywhere
    #   'openpyxl'- spreadsheets have nothing to do with the ledger
    WRITES = ("write_text(", "os.replace(", "json.dump(", "writeFileSync",
              ">>", "| tee")

    def _scan(self, roots, allowed) -> list:
        """The scan pipeline, in ONE place: the two stages above, returning
        `(path, hits)` for every offender. Shared with the negative control
        below so that control cannot drift from the gate it certifies — a second
        copy of this loop is the same mistake as the parallel ERE in shell.

        A list argv bypasses the shell, so this really is /usr/bin/grep - not
        the bash `grep` function that wraps ugrep and honours .gitignore. That
        distinction is the whole reason this reads as a gate.
        """
        listing = subprocess.run(
            ["grep", "-rIl", "--include=*.py", "--include=*.js",
             "--include=*.cjs", "--include=*.sh", "goal-ledger", *roots],
            capture_output=True, text=True)
        offenders = []
        for path in listing.stdout.splitlines():
            # Path-boundary match, not `startswith(allowed)`. A tuple
            # startswith is a STRING test: the one file-shaped entry
            # (session-sync-run.js, no trailing slash) would exempt anything
            # CONTINUING it, and `session-sync-run.js/child.py` is a path
            # --include=*.py scans. A file entry exempts only itself; a
            # directory entry (trailing slash) exempts what is under it.
            if not path or any(path == a or (a.endswith("/") and
                                             path.startswith(a))
                               for a in allowed):
                continue
            try:
                body = Path(path).read_text(errors="replace")
            except OSError:
                continue
            hits = sorted({w for w in self.WRITES if w in body})
            if hits:
                offenders.append((path, hits))
        return offenders

    def test_only_the_writer_dirs_write_ledger_json(self):
        offenders = [f"{path}  (writes: {', '.join(hits)})"
                     for path, hits in self._scan(self.ROOTS, self.ALLOWED)]
        self.assertEqual(offenders, [],
                         "files outside the writer dirs mention goal-ledger "
                         "and contain a write primitive:\n" + "\n".join(offenders))

    def test_the_gate_is_not_vacuous(self):
        """A gate that can never fire is decorative. The old version of this
        test only proved WRITES still matches a hardcoded string — it said
        nothing about ALLOWED, so a too-broad ALLOWED entry could silently
        swallow a real offender's path prefix and this test would still pass.

        Real negative control: plant a file that mentions goal-ledger and
        contains a genuine write primitive, under a scratch mkdtemp() tree
        (never the real repo) and outside any ALLOWED prefix, then run the
        exact scan pipeline test_only_the_writer_dirs_write_ledger_json uses
        — scoped to the scratch tree — and assert the planted file comes back
        as an offender."""
        tmp = Path(tempfile.mkdtemp(prefix="sole-writer-negctrl-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        rogue = tmp / "rogue_writer.py"
        rogue.write_text(
            'from pathlib import Path\n'
            'p = Path("goal-ledger") / "x.json"\n'
            'p.write_text(json.dumps(d))\n')

        offenders = [path for path, _ in self._scan([str(tmp)], self.ALLOWED)]
        self.assertIn(str(rogue), offenders,
                     "the scan did not flag a planted offender outside "
                     "ALLOWED — the real gate would not catch one either")

        # Second assertion, kept from the original: WRITES still matches the
        # ordinary two-line ledger-write shape.
        body = 'p = LEDGER_DIR / "x.json"\np.write_text(json.dumps(d))\n'
        self.assertTrue([w for w in self.WRITES if w in body])

    def test_an_allowed_directory_does_not_swallow_its_siblings(self):
        """The ALLOWED-breadth half, which the case above does not reach: its
        rogue sits outside every prefix, so it proves the pipeline fires and
        says nothing about how widely a prefix matches.

        `startswith` is a STRING test, not a path test. A directory entry
        carries a trailing slash, and this pins that the slash is what does the
        boundary work: `scripts-rogue/` shares the whole `scripts` prefix
        without being inside `scripts/`, and is exactly what would be swallowed
        if a future edit dropped the slash.

        Matched against a scratch MIRROR of ALLOWED rather than the real tuple,
        and that limit is real: every path sharing a prefix with a live ALLOWED
        entry is inside this repo, so planting one there would dirty the tree
        and fail the gate above. The mirror reproduces the entry SHAPES, not the
        entries. See test_an_allowed_file_entry_swallows_a_child_path for the
        other shape, which the real tuple also carries.
        """
        tmp = Path(tempfile.mkdtemp(prefix="sole-writer-sibling-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        writer = 'p = Path("goal-ledger") / "x.json"\np.write_text("{}")\n'

        (tmp / "scripts").mkdir()
        (tmp / "scripts" / "the_writer.py").write_text(writer)
        (tmp / "scripts-rogue").mkdir()
        sibling = tmp / "scripts-rogue" / "rogue_writer.py"
        sibling.write_text(writer)

        mirror = (str(tmp / "scripts") + "/",)
        offenders = [path for path, _ in self._scan([str(tmp)], mirror)]
        self.assertIn(str(sibling), offenders,
                      "a directory sharing a prefix with an ALLOWED entry was "
                      "swallowed by it")
        # Non-vacuity: the file that really is inside the allowed directory is
        # still exempt, so the assertion above is about the boundary and not
        # about the scan having stopped exempting anything at all.
        self.assertNotIn(str(tmp / "scripts" / "the_writer.py"), offenders)

    def test_an_allowed_file_entry_does_not_swallow_a_child_path(self):
        """The other entry shape in ALLOWED: a FILE path, no trailing slash.

        `/home/pedro/.claude/scripts/session-sync-run.js` exempts exactly one
        file. Under the tuple `startswith` this gate used to run, it also
        exempted anything CONTINUING that string -- including
        `session-sync-run.js/child.py`, which `--include=*.py` scans. Nothing
        occupied that path, so it was latent rather than live, but a gate with
        a hole in its matching rule certifies less than it claims. `_scan` now
        does a path-boundary match; this pins it, in the shape the real tuple
        has, so widening ALLOWED or relaxing the includes cannot quietly
        reopen it.
        """
        tmp = Path(tempfile.mkdtemp(prefix="sole-writer-childpath-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        allowed_file = tmp / "session-sync-run.js"
        allowed_file.mkdir()          # a directory named exactly like the entry
        child = allowed_file / "child_writer.py"
        child.write_text('p = Path("goal-ledger") / "x.json"\np.write_text("{}")\n')

        mirror = (str(allowed_file),)      # no trailing slash: the real shape
        offenders = [path for path, _ in self._scan([str(tmp)], mirror)]
        self.assertIn(str(child), offenders,
                      "a file-shaped ALLOWED entry swallowed a path that "
                      "merely CONTINUES it -- _scan is back to a string "
                      "startswith instead of a path-boundary match, and the "
                      "gate now exempts writers nobody allow-listed")


class TestStations(CliTestCase):

    def setUp(self):
        super().setUp()
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")

    def test_advance_moves_one_station_forward(self):
        out = self.cli("outcome-advance", "--outcome", "outcome-0001")
        self.assertEqual(out["station"], "specced")
        self.assertEqual(out["previous_station"], "captured")

    def test_advance_records_history(self):
        self.cli("outcome-advance", "--outcome", "outcome-0001")
        rec = outcomes.find_outcome(outcomes.read_outcomes(), "outcome-0001")
        self.assertEqual(len(rec["history"]), 1)
        self.assertEqual(rec["history"][0]["from"], "captured")
        self.assertEqual(rec["history"][0]["to"], "specced")

    def test_advance_past_closed_exits_2(self):
        for _ in range(5):
            self.cli("outcome-advance", "--outcome", "outcome-0001")
        rec = outcomes.find_outcome(outcomes.read_outcomes(), "outcome-0001")
        self.assertEqual(rec["station"], "closed")
        proc = self.cli("outcome-advance", "--outcome", "outcome-0001", expect=2)
        self.assertIn("outcome-0001 is at closed; there is no station after it",
                      proc.stderr)

    def test_skipping_a_station_exits_2(self):
        proc = self.cli("outcome-advance", "--outcome", "outcome-0001",
                        "--to", "executing", expect=2)
        self.assertIn("cannot skip from captured to executing", proc.stderr)

    def test_going_backwards_needs_reopen(self):
        self.cli("outcome-advance", "--outcome", "outcome-0001")
        proc = self.cli("outcome-advance", "--outcome", "outcome-0001",
                        "--to", "captured", expect=2)
        self.assertIn("moving back to captured needs --reopen", proc.stderr)
        out = self.cli("outcome-advance", "--outcome", "outcome-0001",
                       "--to", "captured", "--reopen")
        self.assertEqual(out["station"], "captured")

    def test_next_station_of_closed_is_none(self):
        self.assertIsNone(outcomes.next_station("closed"))

    # Not in the brief. The same-station case (there == here) had no branch of
    # its own, so it fell to the skip-forward else -- which indexes
    # STATIONS[here + 1]. At `closed` (index 5, last of six) that was an
    # IndexError escaping as an uncaught exception, not an Abort(2).
    def test_advancing_to_the_current_station_is_refused(self):
        for _ in range(5):
            self.cli("outcome-advance", "--outcome", "outcome-0001")
        proc = self.cli("outcome-advance", "--outcome", "outcome-0001",
                        "--to", "closed", expect=2)
        self.assertIn("outcome-0001 is already at closed", proc.stderr)


class TestOutcomeNote(CliTestCase):
    """The one free-text surface on an outcome. Every other history append is
    machine-generated with a fixed key set, so a note carries an explicit
    `event` rather than being told apart later by key-shape sniffing."""

    def setUp(self):
        super().setUp()
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")

    def _last_history(self) -> dict:
        rec = outcomes.find_outcome(outcomes.read_outcomes(), "outcome-0001")
        return rec["history"][-1]

    def test_note_appends_a_history_entry_with_event_note(self):
        self.cli("outcome-note", "--outcome", "outcome-0001",
                 "--text", "picked up the P17 thread")
        entry = self._last_history()
        self.assertEqual(entry["event"], "note")
        self.assertEqual(entry["text"], "picked up the P17 thread")
        self.assertIn("at", entry)

    def test_note_rejects_empty_text(self):
        proc = self.cli("outcome-note", "--outcome", "outcome-0001",
                        "--text", "   ", expect=2)
        self.assertIn("text is empty", proc.stderr)

    def test_note_rejects_unknown_outcome(self):
        # The stderr assertion is what keeps this test alive. argparse's own
        # usage error for an unregistered subcommand ALSO exits 2, so `expect=2`
        # on its own would pass against a goals.py that has no outcome-note
        # parser at all -- a test that is green before the feature exists and
        # stays green if it is later deleted.
        proc = self.cli("outcome-note", "--outcome", "outcome-9999",
                        "--text", "x", expect=2)
        self.assertIn("no such outcome: outcome-9999", proc.stderr)

    def test_note_accepts_an_explicit_event_label(self):
        self.cli("outcome-note", "--outcome", "outcome-0001",
                 "--event", "session-end",
                 "--text", "station captured, next unblocked row P17")
        self.assertEqual(self._last_history()["event"], "session-end")

    def test_note_rejects_an_unknown_event(self):
        """Two values, closed. An open vocabulary here would let a typo mint a
        new event name that no reader of `history` knows how to render."""
        proc = self.cli("outcome-note", "--outcome", "outcome-0001",
                        "--event", "banana", "--text", "x", expect=2)
        self.assertIn("unknown event 'banana'", proc.stderr)


class TestStatusVocabulary(CliTestCase):

    def setUp(self):
        super().setUp()
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")

    # Spec 3.4: a parked item with no wake condition is how a deadline rots.
    def test_park_without_wake_exits_2(self):
        proc = self.cli("outcome-status", "--outcome", "outcome-0001",
                        "--status", "parked", expect=2)
        self.assertIn("--status parked requires --wake", proc.stderr)

    def test_park_with_wake_succeeds(self):
        out = self.cli("outcome-status", "--outcome", "outcome-0001",
                       "--status", "parked",
                       "--wake", "when slice 2 closes")
        self.assertEqual(out["status"], "parked")
        self.assertEqual(out["wake"], "when slice 2 closes")

    def test_leaving_parked_clears_the_wake(self):
        self.cli("outcome-status", "--outcome", "outcome-0001",
                 "--status", "parked", "--wake", "when slice 2 closes")
        out = self.cli("outcome-status", "--outcome", "outcome-0001",
                       "--status", "active")
        self.assertIsNone(out["wake"])

    def test_unknown_status_exits_2(self):
        proc = self.cli("outcome-status", "--outcome", "outcome-0001",
                        "--status", "in-progress", expect=2)
        self.assertIn("unknown status 'in-progress'", proc.stderr)

    # Same abort branch as test_park_without_wake_exits_2 - whitespace input
    # rather than a missing flag, not a second check. Kept as a comment, not a
    # docstring: unittest -v prints a docstring INSTEAD of the case name.
    def test_blank_wake_is_rejected(self):
        proc = self.cli("outcome-status", "--outcome", "outcome-0001",
                        "--status", "parked", "--wake", "   ", expect=2)
        self.assertIn("--status parked requires --wake", proc.stderr)


import datetime as _dt
import time as _time


class TestClaiming(CliTestCase):

    def setUp(self):
        super().setUp()
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")

    def test_claim_records_the_session(self):
        out = self.cli("outcome-claim", "--outcome", "outcome-0001",
                       "--session", "sess-a")
        self.assertEqual(out["claim"]["session_id"], "sess-a")
        self.assertTrue(out["claimed"])

    def test_second_session_cannot_take_a_live_claim(self):
        self.cli("outcome-claim", "--outcome", "outcome-0001",
                 "--session", "sess-a")
        proc = self.cli("outcome-claim", "--outcome", "outcome-0001",
                        "--session", "sess-b", expect=2)
        # Not in the brief, which asserted the exit code alone. argparse exits 2
        # for an unknown subcommand, so `expect=2` on its own passes before
        # outcome-claim exists at all -- it proves nothing about this guard.
        self.assertIn("outcome-0001 is claimed by session sess-a", proc.stderr)
        self.assertIn("--steal", proc.stderr)

    def test_same_session_reclaiming_is_a_heartbeat_not_an_error(self):
        first = self.cli("outcome-claim", "--outcome", "outcome-0001",
                         "--session", "sess-a")
        _time.sleep(1.1)
        second = self.cli("outcome-claim", "--outcome", "outcome-0001",
                          "--session", "sess-a")
        self.assertTrue(second["claimed"])
        self.assertNotEqual(first["claim"]["heartbeat_at"],
                            second["claim"]["heartbeat_at"])

    def test_release_frees_it(self):
        self.cli("outcome-claim", "--outcome", "outcome-0001",
                 "--session", "sess-a")
        self.cli("outcome-release", "--outcome", "outcome-0001",
                 "--session", "sess-a")
        rec = outcomes.find_outcome(outcomes.read_outcomes(), "outcome-0001")
        self.assertIsNone(rec["claim"])

    def test_another_session_cannot_release_a_live_claim(self):
        self.cli("outcome-claim", "--outcome", "outcome-0001",
                 "--session", "sess-a")
        proc = self.cli("outcome-release", "--outcome", "outcome-0001",
                        "--session", "sess-b", expect=2)
        # Same reason as above: the exit code alone is argparse's too.
        self.assertIn("outcome-0001 is claimed by sess-a, not by sess-b",
                      proc.stderr)
        self.assertIn("pass --steal to take it anyway", proc.stderr)

    def test_steal_takes_a_live_claim_and_records_it(self):
        self.cli("outcome-claim", "--outcome", "outcome-0001",
                 "--session", "sess-a")
        out = self.cli("outcome-release", "--outcome", "outcome-0001",
                       "--session", "sess-b", "--steal")
        self.assertEqual(out["stolen_from"], "sess-a")
        self.assertFalse(out["claimed"])
        data = outcomes.read_outcomes()
        rec = outcomes.find_outcome(data, "outcome-0001")
        events = [h["event"] for h in rec.get("history", [])]
        self.assertIn("claim-stolen", events,
                      "a stolen claim must leave a trace")

    def test_steal_on_an_unclaimed_outcome_is_a_plain_release(self):
        out = self.cli("outcome-release", "--outcome", "outcome-0001",
                       "--session", "sess-b", "--steal")
        self.assertIsNone(out["stolen_from"])

    def test_a_session_id_with_a_trailing_newline_is_rejected(self):
        """Pins the fullmatch in cmd_outcome_claim.

        `_SESSION_ID_RE` is `^[A-Za-z0-9_-]+$`, and in Python `$` also matches
        immediately before a trailing newline -- so `.match()` accepts
        "sess-a\\n" as though it were "sess-a". goals._check_slug and
        goals.session_live both use .fullmatch for exactly this reason and both
        document it. This test fails if anyone downgrades it back to .match().
        """
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.cmd_outcome_claim("outcome-0001", "sess-a\n")
        self.assertEqual(ctx.exception.code, 2)
        # Not in the brief. Without this the case also passes on any OTHER
        # Abort(2) -- "no such outcome", say -- so it would stop pinning the
        # regex the moment the fixture changed.
        self.assertIn("is not a usable session id", str(ctx.exception.message))

    def test_release_rejects_the_same_unusable_session_id(self):
        """cmd_outcome_release validated nothing. No claim can ever be HELD by
        an invalid id, so there was no bypass -- but under --steal the string
        goes straight into history[].by, which puts a newline or a control
        character into the ledger and into anything that renders it. The
        outcome created in setUp is unclaimed, so without the guard this call
        succeeds and raises nothing at all.
        """
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.cmd_outcome_release("outcome-0001", "sess-a\n", steal=True)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("is not a usable session id", str(ctx.exception.message))


class TestClaimExpiry(OutcomeTestCase):
    """Spec acceptance A6, unit half: a dead or stale claim stops being live.
    The end-to-end half runs in the follow-on plan once a real session can
    hold one."""

    def test_a_fresh_heartbeat_is_live(self):
        claim = {"session_id": "sess-a", "heartbeat_at": goals._now_iso()}
        self.assertTrue(outcomes.claim_is_live(claim, _time.time()))

    def test_a_stale_heartbeat_is_not_live(self):
        claim = {"session_id": "sess-a", "heartbeat_at": "2020-01-01T00:00:00Z"}
        self.assertFalse(outcomes.claim_is_live(claim, _time.time()))

    def test_a_heartbeat_just_inside_the_window_is_live(self):
        # One minute short of the expiry. Built here rather than via
        # goals._now_iso(), which takes no argument - it always means "now".
        beat = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
            milliseconds=outcomes.CLAIM_EXPIRY_MS - 60_000)
        claim = {"session_id": "sess-a",
                 "heartbeat_at": beat.strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.assertTrue(outcomes.claim_is_live(claim, _time.time()))

    def test_a_missing_heartbeat_is_not_live(self):
        self.assertFalse(outcomes.claim_is_live({"session_id": "sess-a"},
                                                _time.time()))

    def test_no_claim_is_not_live(self):
        self.assertFalse(outcomes.claim_is_live(None, _time.time()))

    def test_expiry_is_its_own_number_longer_than_the_lock_backstop(self):
        """Global Constraints require this to be PARAMETERISED, not hardcoded.

        An earlier draft asserted the literal 14_400_000 here, which duplicated
        the constant into the test: changing the window in one line then meant
        changing it in two, and the test asserted nothing the constant did not
        already say. Spelling it `4 * 60 * 60 * 1000` would have been the same
        duplication in a longer form.

        What is actually worth pinning is the RELATIONSHIP the design note
        argues for - that a claim expiry is a different, longer thing than a
        write-lock backstop. That survives a change to the number, and it is the
        property that breaks if someone "simplifies" the two into one constant.
        The window arithmetic is pinned by the boundary cases above, which are
        already expressed as CLAIM_EXPIRY_MS - 60_000.
        """
        self.assertNotEqual(outcomes.CLAIM_EXPIRY_MS, goals.LOCK_ABANDON_MS,
                            "a claim expiry is not a lock backstop")
        self.assertGreater(outcomes.CLAIM_EXPIRY_MS, goals.LOCK_ABANDON_MS,
                           "an outcome is held across a working session; a "
                           "write lock is not, so the claim must outlast it")


class TestStaleClaimIsTakeable(CliTestCase):
    """The outcome returns to the queue at its recorded station (A6)."""

    def test_a_stale_claim_can_be_taken_and_the_station_survives(self):
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        self.cli("outcome-advance", "--outcome", "outcome-0001")   # -> specced
        self.cli("outcome-advance", "--outcome", "outcome-0001")   # -> planned
        self.cli("outcome-claim", "--outcome", "outcome-0001",
                 "--session", "sess-dead")

        # Simulate the session dying: the heartbeat stops advancing.
        with outcomes.outcomes_lock():
            data = outcomes.read_outcomes()
            rec = outcomes.find_outcome(data, "outcome-0001")
            rec["claim"] = {"session_id": "sess-dead",
                            "heartbeat_at": "2020-01-01T00:00:00Z"}
            outcomes.write_outcomes(data)

        out = self.cli("outcome-claim", "--outcome", "outcome-0001",
                       "--session", "sess-new")
        self.assertEqual(out["claim"]["session_id"], "sess-new")
        self.assertEqual(out["station"], "planned",
                         "the station must survive the claim changing hands")


class TestCorruptHeartbeat(CliTestCase):
    """Not in the brief. This task is the FIRST consumer of the outcome
    timestamps -- check_shape validates neither `created`/`last_seen` nor the
    claim, so `heartbeat_at` reaches goals._parse_iso straight off a
    hand-editable file. _parse_iso raises ValueError on anything it cannot
    read, and goals.main() contracts that nothing but an Abort escapes it (the
    comment just below its `parser.parse_args` call). These cases pin what
    happens instead of a traceback. Both citations are anchored on names, not
    line numbers: the numbers this docstring first carried were invalidated by
    an insertion in the same commit that wrote them.
    """

    CORRUPT = {"session_id": "sess-x", "heartbeat_at": "yesterday"}

    def plant(self):
        self.seed(make(claim=dict(self.CORRUPT)))

    def test_an_unparseable_heartbeat_refuses_the_claim_and_names_the_field(self):
        self.plant()
        proc = self.cli("outcome-claim", "--outcome", "outcome-0001",
                        "--session", "sess-b", expect=2)
        self.assertIn("claim.heartbeat_at", proc.stderr)
        self.assertIn("'yesterday'", proc.stderr)
        # The point of the whole case: an Abort, not a crash.
        self.assertNotIn("Traceback", proc.stderr)

    def test_release_without_steal_also_refuses_it(self):
        self.plant()
        proc = self.cli("outcome-release", "--outcome", "outcome-0001",
                        "--session", "sess-b", expect=2)
        self.assertIn("claim.heartbeat_at", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_steal_can_still_free_an_unparseable_claim(self):
        """The reason --steal must not consult the timestamp.

        If the abort above could reach --steal, a corrupt heartbeat_at would be
        unclearable by any command: the only escape from the refusal would be
        the command that refuses. This is the negative control for that -- it
        fails the moment --steal starts depending on claim_is_live succeeding.
        """
        self.plant()
        out = self.cli("outcome-release", "--outcome", "outcome-0001",
                       "--session", "sess-b", "--steal")
        self.assertEqual(out["stolen_from"], "sess-x")
        rec = outcomes.find_outcome(outcomes.read_outcomes(), "outcome-0001")
        self.assertIsNone(rec["claim"])
        self.assertIn("claim-stolen", [h.get("event") for h in rec["history"]])

    def test_a_stale_but_readable_claim_is_not_reported_as_stolen(self):
        """Guards the blast radius of the case above: only the UNREADABLE
        heartbeat is treated as still-held. A merely expired one is dead, and
        --steal over it records no theft (there was nothing live to steal).

        The `stolen_from is None` assertion alone was too weak to hold that
        line: it is also exactly what the F1 bug produced when a LIVE claim
        was cleared but its holder could not be named. The history assertions
        below are what actually tell "nothing was live" apart from "something
        live was taken and not recorded" -- and they pin that the handover
        still leaves the F2 trace.
        """
        self.seed(make(claim={"session_id": "sess-x",
                              "heartbeat_at": "2020-01-01T00:00:00Z"}))
        out = self.cli("outcome-release", "--outcome", "outcome-0001",
                       "--session", "sess-b", "--steal")
        self.assertIsNone(out["stolen_from"])
        rec = outcomes.find_outcome(outcomes.read_outcomes(), "outcome-0001")
        events = [h.get("event") for h in rec["history"]]
        self.assertNotIn("claim-stolen", events,
                         "an expired claim is not stolen")
        self.assertIn("claim-cleared", events,
                      "a non-holder clearing someone else's claim is still a "
                      "handover and must leave a trace")
        self.assertFalse(out["stole"], "an expired claim was not live to steal")


class TestClaimHandoverIsRecorded(CliTestCase):
    """Every contested change of hands leaves a trace, not just the forced one.

    The audit trail exists so that two sessions working one project can
    reconstruct who took what from whom. Recording only `claim-stolen` left
    the MOST COMMON contested transition -- a session that died hours ago
    being taken over -- with no record at all. The three events are kept
    distinct so a forced takeover stays tellable apart from an expiry:

        claim-stolen   a LIVE claim was taken by force (--steal)
        claim-taken    an EXPIRED claim was taken over by outcome-claim
        claim-cleared  an EXPIRED claim was released by a non-holder
    """

    STALE = "2020-01-01T00:00:00Z"

    def events(self) -> list:
        rec = outcomes.find_outcome(outcomes.read_outcomes(), "outcome-0001")
        # .get, not ["event"]: cmd_outcome_advance writes history entries with
        # no `event` key at all (it stores the station move in `from`/`to`), so
        # subscripting raises KeyError on any outcome that ever advanced.
        return rec["history"], [h.get("event") for h in rec["history"]]

    def only(self, event: str) -> dict:
        history, names = self.events()
        matches = [h for h in history if h.get("event") == event]
        self.assertEqual(len(matches), 1,
                         f"expected exactly one {event} entry, got {names}")
        return matches[0]

    def test_stealing_a_live_claim_whose_holder_is_unnamed_is_still_recorded(self):
        """F1. A claim with a fresh heartbeat and no session_id is LIVE, so
        --steal clears something real. Testing `stolen_from` truthiness instead
        of "was anything live" made this case indistinguishable from an
        unclaimed outcome: the claim was cleared, no entry was written, and the
        receipt said null. check_shape validates a claim no further than
        "outcomes[i] is an object", so this record round-trips fine.
        """
        self.seed(make(claim={"heartbeat_at": goals._now_iso()}))
        out = self.cli("outcome-release", "--outcome", "outcome-0001",
                       "--session", "sess-b", "--steal")
        entry = self.only("claim-stolen")
        self.assertIsNone(entry["from"], "the prior holder is honestly unknown")
        self.assertEqual(entry["by"], "sess-b")
        self.assertTrue(out["stole"], "a live claim was cleared by this call")
        # null, not a sentinel: "unknown" would itself be a valid session id.
        self.assertIsNone(out["stolen_from"])

    def test_taking_over_an_expired_claim_is_recorded(self):
        """F2. The ordinary contested handover: the holder crashed hours ago."""
        self.seed(make(claim={"session_id": "sess-dead",
                              "heartbeat_at": self.STALE}))
        self.cli("outcome-claim", "--outcome", "outcome-0001",
                 "--session", "sess-new")
        entry = self.only("claim-taken")
        self.assertEqual(entry["from"], "sess-dead")
        self.assertEqual(entry["by"], "sess-new")
        self.assertNotIn("claim-stolen", self.events()[1],
                         "an expired claim is a handover, not a theft")

    def test_a_non_holder_releasing_an_expired_claim_is_recorded(self):
        """F2, the release half -- and it needs no --steal, because an expired
        claim is not live."""
        self.seed(make(claim={"session_id": "sess-dead",
                              "heartbeat_at": self.STALE}))
        out = self.cli("outcome-release", "--outcome", "outcome-0001",
                       "--session", "sess-b")
        self.assertIsNone(out["stolen_from"])
        entry = self.only("claim-cleared")
        self.assertEqual(entry["from"], "sess-dead")
        self.assertEqual(entry["by"], "sess-b")

    def test_refreshing_your_own_expired_claim_is_not_a_handover(self):
        """The blast radius of the two branches above: nothing changed hands,
        so nothing is recorded. Without this, either branch could widen into
        an entry on every ordinary heartbeat."""
        self.seed(make(claim={"session_id": "sess-a",
                              "heartbeat_at": self.STALE}))
        self.cli("outcome-claim", "--outcome", "outcome-0001",
                 "--session", "sess-a")
        self.cli("outcome-release", "--outcome", "outcome-0001",
                 "--session", "sess-a")
        names = self.events()[1]
        for event in ("claim-taken", "claim-cleared", "claim-stolen"):
            self.assertNotIn(event, names, "one session, no handover")

    def test_an_unclaimed_outcome_records_nothing(self):
        """The `held` guard: a record whose claim is None must not reach
        held.get() in either new branch."""
        self.seed(make(claim=None))
        self.cli("outcome-release", "--outcome", "outcome-0001",
                 "--session", "sess-b", "--steal")
        self.assertEqual(self.events()[1], [])


def _fake_rev_parse(by_flag: dict):
    """A `subprocess.run` stand-in returning canned rc=0 stdout per rev-parse
    flag, so assert_not_in_worktree can be driven at git versions this machine
    does not have. Dispatches on exact argv membership: the two calls differ by
    `--absolute-git-dir` vs `--git-common-dir`, and neither string is an element
    of the other's argv."""
    def run(argv, **kwargs):
        for flag, out in by_flag.items():
            if flag in argv:
                return subprocess.CompletedProcess(argv, 0, out + "\n", "")
        raise AssertionError(f"unexpected command: {argv}")
    return run


class TestWorktreeRefusal(OutcomeTestCase):
    """Spec acceptance A1. Every tree here is built by the test in a scratch
    dir -- the nine live .lane-* worktrees under Cucina-Nostra are never
    touched, entered, or read."""

    def _git(self, cwd: Path, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=str(cwd),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _fixture_repo(self) -> Path:
        """A throwaway repo with one commit, so a worktree can be added to it."""
        repo = self.tmp / "fixture-repo"
        repo.mkdir()
        self._git(repo, "init", "-q", "-b", "main")
        self._git(repo, "config", "user.email", "fixture@example.invalid")
        self._git(repo, "config", "user.name", "Fixture")
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-qm", "init")
        return repo

    def _fixture_lane(self) -> tuple[Path, Path]:
        """(main tree, linked worktree). Both live under self.tmp and go away
        with OutcomeTestCase's rmtree cleanup."""
        repo = self._fixture_repo()
        lane = self.tmp / "lane-fixture"
        self._git(repo, "worktree", "add", "-q", "-b", "lane", str(lane))
        return repo, lane

    def test_main_tree_is_allowed(self):
        repo = self._fixture_repo()
        with chdir(repo):
            outcomes.assert_not_in_worktree()   # must not raise

    def test_outside_any_repo_is_allowed(self):
        """Running goals.py from a non-repo directory is normal and must not
        break: git exits non-zero there and the write is allowed."""
        loose = self.tmp / "not-a-repo"
        loose.mkdir()
        with chdir(loose):
            outcomes.assert_not_in_worktree()

    def test_linked_worktree_is_refused_with_exit_2(self):
        repo, lane = self._fixture_lane()
        with chdir(lane):
            with self.assertRaises(goals.Abort) as ctx:
                outcomes.assert_not_in_worktree()
        self.assertEqual(ctx.exception.code, 2)
        msg = str(ctx.exception.message)
        self.assertIn("worktree", msg.lower())
        self.assertIn(str(repo), msg,
                      "the refusal must name the canonical path")

    def test_a_mutating_command_is_refused_from_a_worktree(self):
        """Exit 2 alone is vacuous here -- cmd_outcome_create already aborts 2
        for a blank statement, an empty --projects and every check_shape
        failure. The message and the absent file are what pin the guard."""
        repo, lane = self._fixture_lane()
        with chdir(lane):
            with self.assertRaises(goals.Abort) as ctx:
                outcomes.cmd_outcome_create("a thing worth doing", "proj")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("worktree", str(ctx.exception.message).lower())
        self.assertFalse(outcomes.outcomes_path().exists(),
                         "the refusal must land before anything is written")

    def test_every_mutating_command_is_refused_from_a_worktree(self):
        """The plan's Step 5 grep counts occurrences of one identifier; it
        passes even if a call landed in the wrong function or somewhere
        unreachable. This is the behavioural cover: every mutating command in
        the module, driven from inside the lane.

        A valid outcome is seeded first, so the guard -- which is the first
        statement of each of these functions -- is what aborts them, not a
        missing record. It is NOT claimed that every argument below would be
        accepted from a main tree: `outcome-attach` names a goal in a project
        with no ledger and would abort there on its own. That is why the
        assertion on "worktree" in the message is load-bearing and the exit code
        alone is not.
        """
        repo, lane = self._fixture_lane()
        self.seed(make())
        spec = self.tmp / "worktree-spec.md"
        spec.write_text("# A spec\n\n" + BLOCK, encoding="utf-8")
        calls = {
            "outcome-create": lambda: outcomes.cmd_outcome_create(
                "a thing worth doing", "proj"),
            "outcome-attach": lambda: outcomes.cmd_outcome_attach(
                "outcome-0001", "proj", "goal-0001"),
            "outcome-advance": lambda: outcomes.cmd_outcome_advance(
                "outcome-0001"),
            "outcome-status": lambda: outcomes.cmd_outcome_status(
                "outcome-0001", "active"),
            "outcome-claim": lambda: outcomes.cmd_outcome_claim(
                "outcome-0001", "sess-a"),
            "outcome-release": lambda: outcomes.cmd_outcome_release(
                "outcome-0001", "sess-a"),
            # The seventh mutator, absent while this test carried the name
            # "every". Its fence is real rather than a stub path: the guard runs
            # before the spec is read, so a missing file would still abort 2 --
            # with "cannot read spec", and the message assertion below is the
            # only thing separating this from a vacuous pass.
            "pendings-ingest": lambda: outcomes.cmd_pendings_ingest(
                "outcome-0001", str(spec)),
            # The eighth mutator. The guard is per-verb -- `grep -n worktree
            # goals.py` returns nothing -- so a new verb inherits it from
            # nowhere and only a call driven from inside the lane proves it was
            # wired. The text is deliberately valid and non-empty: with the
            # guard absent this call would SUCCEED, append to history, and trip
            # the file-unchanged assertion at the end of this test.
            "outcome-note": lambda: outcomes.cmd_outcome_note(
                "outcome-0001", "a note written from the wrong tree"),
        }
        before_text = outcomes.outcomes_path().read_text(encoding="utf-8")
        before_files = sorted(p.name for p in goals.LEDGER_DIR.iterdir())
        with chdir(lane):
            for name, call in calls.items():
                with self.subTest(command=name):
                    with self.assertRaises(goals.Abort) as ctx:
                        call()
                    self.assertEqual(ctx.exception.code, 2)
                    self.assertIn(
                        "worktree", str(ctx.exception.message).lower(),
                        f"{name} aborted, but for some other reason")
        self.assertEqual(outcomes.outcomes_path().read_text(encoding="utf-8"),
                         before_text, "a refused command must not have written")
        self.assertEqual(sorted(p.name for p in goals.LEDGER_DIR.iterdir()),
                         before_files,
                         "a refused command must create no ledger file either")

    def test_a_read_is_still_allowed_from_a_worktree(self):
        """Spec 3.6: a worktree READS the ledger and never writes pending state.

        This one passes before the guard exists as well as after -- it is a
        regression pin, not a RED test. It fails only if someone later adds
        assert_not_in_worktree() to cmd_outcome_list.
        """
        repo, lane = self._fixture_lane()
        self.seed(make())
        with chdir(lane):
            out = outcomes.cmd_outcome_list()
        self.assertEqual(out["count"], 1)

    # The two cases below pin the difference between "determined not a worktree"
    # and "could not tell". Both patch subprocess.run for the duration of one
    # with-block; the guard is the first statement of every mutating command, so
    # the patch window closes long before outcomes_lock() or any other code that
    # might spawn a process.

    def test_a_git_that_times_out_refuses_instead_of_allowing(self):
        """A timeout is not a determination, and this guard exists to refuse a
        write. Never fail-open on a safety check -- downgrade autonomy instead.
        TimeoutExpired is its own exception class, so it is fully
        distinguishable from the two cases that DO allow (not a repo, no git).

        Driven from a main tree on purpose: without the split except this call
        returns silently and cmd_outcome_create writes the ledger, so the
        assertions below fail on the old behaviour rather than passing by
        coincidence.
        """
        repo = self._fixture_repo()

        def hangs(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5))

        with chdir(repo), mock.patch.object(outcomes.subprocess, "run", hangs):
            with self.assertRaises(goals.Abort) as ctx:
                outcomes.cmd_outcome_create("a thing worth doing", "proj")
        self.assertEqual(ctx.exception.code, 2)
        msg = str(ctx.exception.message)
        # Exit 2 alone is vacuous -- cmd_outcome_create aborts 2 for a blank
        # statement, an empty --projects and every check_shape failure.
        self.assertIn("cannot determine whether this is a linked git worktree",
                      msg)
        self.assertIn("TimeoutExpired", msg)
        self.assertFalse(outcomes.outcomes_path().exists(),
                         "a guard that could not decide must not have written")

    def test_a_git_too_old_for_path_format_still_allows_the_write(self):
        """The version floor, driven by canned stdout rather than an old binary.

        `git rev-parse` exits 0 on an option it does not recognise and echoes it
        back, so returncode cannot tell "the option worked" from "the option was
        ignored". Below git 2.31 the second call returns the two lines
        "--path-format=absolute" and ".git", which never equals the first call's
        absolute path -- so without the shape check every mutating command in
        every repo refuses, printing an empty canonical path. Allowing is the
        correct answer here; the alternative is a tool that never writes on an
        older git.
        """
        repo = self._fixture_repo()
        old_git = _fake_rev_parse({
            "--absolute-git-dir": str(repo / ".git"),
            "--git-common-dir": "--path-format=absolute\n.git",
        })
        with chdir(repo), mock.patch.object(outcomes.subprocess, "run", old_git):
            outcomes.assert_not_in_worktree()      # must not raise

        # Non-vacuity, and the load-bearing half: the SAME stand-in with an
        # absolute-but-different common dir still refuses. Without this, the
        # allow above would also pass if the stand-in had disabled detection
        # altogether instead of exercising the too-old-git shape.
        modern_lane = _fake_rev_parse({
            "--absolute-git-dir": str(repo / ".git" / "worktrees" / "lane"),
            "--git-common-dir": str(repo / ".git"),
        })
        with chdir(repo), mock.patch.object(outcomes.subprocess, "run",
                                            modern_lane):
            with self.assertRaises(goals.Abort) as ctx:
                outcomes.assert_not_in_worktree()
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("worktree", str(ctx.exception.message).lower())


BLOCK = """\
%%PENDINGS:BEGIN%%

| id | description | depends on | status |
|---|---|---|---|
| P01 | Delete the dashboard that has had no writer since 2026-08-18. | — | done |
| P02 | Triage the draft plans and report the counts kept and removed. | — | open |
| P03 | Define the outcomes.json schema and its per-project link field. | P01, P02 | open |

%%PENDINGS:END%%
"""


class TestPendingBlockParsing(OutcomeTestCase):

    def test_parses_rows(self):
        rows = outcomes.parse_pending_block(BLOCK)
        self.assertEqual([r["id"] for r in rows], ["P01", "P02", "P03"])
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(rows[2]["depends_on"], ["P01", "P02"])

    def test_em_dash_means_no_dependency(self):
        rows = outcomes.parse_pending_block(BLOCK)
        self.assertEqual(rows[0]["depends_on"], [])

    def test_missing_fence_is_a_defect(self):
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.parse_pending_block("no fence here\n")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("no %%PENDINGS:BEGIN%%", str(ctx.exception.message))

    def test_a_label_is_rejected_a_sentence_is_not(self):
        """Spec 5.1: 'Fix the counts' is rejected; a full sentence is a row."""
        bad = BLOCK.replace(
            "Delete the dashboard that has had no writer since 2026-08-18.",
            "Fix the counts")
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.parse_pending_block(bad)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("sentence", str(ctx.exception.message).lower())

    def test_unknown_dependency_id_is_rejected(self):
        bad = BLOCK.replace("| P01, P02 |", "| P99 |")
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.parse_pending_block(bad)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("P99", str(ctx.exception.message))

    def test_duplicate_row_id_is_rejected(self):
        bad = BLOCK.replace("| P02 |", "| P01 |")
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.parse_pending_block(bad)
        self.assertEqual(ctx.exception.code, 2)
        # Pinned to the DUPLICATE message specifically. Without this the case
        # would also pass on the sentence check or a done-before-dependency
        # abort, either of which a future reordering could make fire first.
        self.assertIn("duplicate", str(ctx.exception.message).lower())

    def test_a_row_that_is_not_four_cells_is_rejected_not_skipped(self):
        """A pipe inside a description must abort, never be silently dropped.

        The plan text skipped any row whose cell count was not four
        (`if len(cells) != 4: continue`). Combined with _splice, which
        regenerates the WHOLE fence from the parsed rows, that is silent data
        loss in two places at once: the row never reaches the ledger, and the
        rewrite deletes it from the governing spec. Nothing downstream would
        notice - the block still parses, the round trip is still stable, and
        the row count quietly drops by one.
        """
        bad = BLOCK.replace(
            "Triage the draft plans and report the counts kept and removed.",
            "Triage the draft plans | and report the counts kept and removed.")
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.parse_pending_block(bad)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("4 cells", str(ctx.exception.message))
        # Non-vacuity: the same block without the stray pipe parses to 3 rows,
        # so the abort above is caused by the pipe and not by the fixture.
        self.assertEqual(len(outcomes.parse_pending_block(BLOCK)), 3)

    def test_a_row_missing_its_leading_pipe_is_rejected_not_dropped(self):
        """GFM makes the outer pipes optional, so `P02 | ... | open |` is a
        legal row a human may well type. `if not line.startswith("|"): continue`
        ate it in silence, and because _splice regenerates the whole fence from
        the parsed rows, the next ingest DELETED it from the governing document.
        Same silent-data-loss class as the four-cell case above, different door.
        """
        bad = BLOCK.replace("| P02 | Triage", "P02 | Triage")
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.parse_pending_block(bad)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("not a table row", str(ctx.exception.message))
        self.assertIn("P02", str(ctx.exception.message))
        # Non-vacuity: the same block with the leading pipe restored parses to
        # 3 rows, so the abort is caused by the missing pipe, not the fixture.
        self.assertEqual(len(outcomes.parse_pending_block(BLOCK)), 3)

    def test_prose_inside_the_fence_is_rejected_not_deleted(self):
        """A note line between the sentinels parsed fine and was then erased by
        the next splice, with no error anywhere. Everything inside the fence is
        regenerated from the rows, so anything that is not a row is deleted."""
        noted = BLOCK.replace(
            "| P03 | Define",
            "> Note: P03 is deferred until Q4.\n| P03 | Define")
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.parse_pending_block(noted)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("not a table row", str(ctx.exception.message))
        self.assertIn("P03 is deferred", str(ctx.exception.message))
        self.assertEqual(len(outcomes.parse_pending_block(BLOCK)), 3)

    def test_blank_lines_inside_the_fence_are_still_allowed(self):
        """The refusal above must not tighten into "every line is a row":
        render_pending_block itself emits a blank line after BEGIN and before
        END, so a strict rule would refuse the parser's own output."""
        spaced = BLOCK.replace("| P01 |", "\n| P01 |").replace(
            "| P03 |", "\n   \n| P03 |")
        self.assertEqual(
            [r["id"] for r in outcomes.parse_pending_block(spaced)],
            ["P01", "P02", "P03"])

    def test_an_empty_row_id_is_rejected_not_treated_as_a_separator(self):
        """`set("") <= {"-", ":"}` is True, so an empty id cell looks exactly
        like a separator row to a naive check. It is a malformed row."""
        bad = BLOCK.replace("| P02 | Triage", "|  | Triage")
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.parse_pending_block(bad)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("is not PNN", str(ctx.exception.message))

    def test_ambiguous_fences_abort(self):
        """A spec that DOCUMENTS the format carries the sentinels twice.

        This is not hypothetical: the governing spec for this plan has an
        illustrative example at section 5.1 and the real P01-P22 table at
        section 11. A bare text.find() takes the FIRST, so ingest read the
        two-line example and died with 'the pendings block is empty' - and
        _splice, using the same find(), would have written the generated table
        over the EXAMPLE and left section 11 stale.

        test_ingest_preserves_prose_outside_the_fence cannot catch this: it
        asserts count(PEND_BEGIN) == 1 on a file that only ever had one fence.
        This case is the one that feeds two.
        """
        doubled = ("# A spec\n\nHere is the format:\n\n"
                   + BLOCK + "\nAnd here is the real one:\n\n" + BLOCK)
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.parse_pending_block(doubled)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("ambiguous", str(ctx.exception.message).lower())

    def test_splice_also_refuses_an_ambiguous_file(self):
        """The writer must refuse on exactly the same condition as the reader,
        with the write-side exit code."""
        doubled = BLOCK + "\n" + BLOCK
        with self.assertRaises(goals.Abort) as ctx:
            outcomes._splice(doubled, outcomes.render_pending_block(
                outcomes.parse_pending_block(BLOCK)))
        self.assertEqual(ctx.exception.code, 4)
        # Code 4 is shared by BOTH _fence_span refusals, so the code alone does
        # not say which one fired - a file with no fence at all would satisfy
        # it. The message pin is what makes this a test of the ambiguity.
        self.assertIn("ambiguous", str(ctx.exception.message).lower())

    def test_an_annotated_sentinel_line_is_not_a_fence(self):
        """The escape hatch the abort message offers, pinned.

        A sentinel with trailing text on its line is documentation, not a
        fence, because the locator is line-anchored. This is what lets a spec
        show the format and still carry exactly one real block.
        """
        example = ("%%PENDINGS:BEGIN%%   <- shown for illustration\n"
                   "| id | description | depends on | status |\n"
                   "%%PENDINGS:END%%     <- shown for illustration\n")
        rows = outcomes.parse_pending_block(example + "\n" + BLOCK)
        self.assertEqual([r["id"] for r in rows], ["P01", "P02", "P03"])

    def test_one_annotated_sentinel_is_still_ambiguous(self):
        """The live-spec shape, pinned: annotate only BEGIN and END is still
        doubled, so the locator must still refuse.

        Measured on docs/superpowers/specs/2026-08-27-outcome-ledger-design.md
        on 2026-08-27: line-anchored BEGIN = 1, line-anchored END = 2. Both
        sentinel lines of an illustration have to be annotated, not one.
        """
        half = ("%%PENDINGS:BEGIN%%   <- shown for illustration\n"
                "| id | description | depends on | status |\n"
                "%%PENDINGS:END%%\n")
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.parse_pending_block(half + "\n" + BLOCK)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("ambiguous", str(ctx.exception.message).lower())


class TestPendingDependencies(OutcomeTestCase):

    def rows(self, *triples):
        return [{"id": i, "description": "A full sentence describing the work.",
                 "depends_on": list(d), "status": s} for i, d, s in triples]

    def test_cycle_is_a_hard_error(self):
        rows = self.rows(("P01", ["P02"], "open"), ("P02", ["P01"], "open"))
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.check_dependencies(rows)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("cycle", str(ctx.exception.message).lower())

    def test_self_dependency_is_a_cycle(self):
        rows = self.rows(("P01", ["P01"], "open"))
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.check_dependencies(rows)
        self.assertIn("cycle", str(ctx.exception.message).lower())

    def test_an_unknown_dependency_aborts_rather_than_crashing(self):
        """check_dependencies is reachable directly, not only through
        parse_pending_block. Its DFS indexes a colour dict keyed by the rows it
        was given, so an id that is not a row used to raise KeyError - an
        unhandled exception, not an Abort, and so not a controlled exit code."""
        rows = self.rows(("P01", ["P99"], "open"))
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.check_dependencies(rows)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("P99", str(ctx.exception.message))

    def test_done_before_its_dependency_is_rejected(self):
        rows = self.rows(("P01", [], "open"), ("P02", ["P01"], "done"))
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.check_dependencies(rows)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("P02", str(ctx.exception.message))

    def test_a_valid_graph_passes(self):
        rows = self.rows(("P01", [], "done"), ("P02", ["P01"], "open"))
        outcomes.check_dependencies(rows)

    def test_unblocked_lists_only_satisfied_rows(self):
        rows = self.rows(("P01", [], "done"), ("P02", ["P01"], "open"),
                         ("P03", ["P02"], "open"))
        self.assertEqual([r["id"] for r in outcomes.unblocked(rows)], ["P02"])

    def test_next_pending_is_the_first_unblocked_row(self):
        rows = self.rows(("P01", [], "done"), ("P02", ["P01"], "open"),
                         ("P03", [], "open"))
        self.assertEqual(outcomes.next_pending(rows)["id"], "P02")

    def test_a_blocked_row_is_never_the_next_thing_to_work_on(self):
        """`blocked` is an AUTHORED status in spec 3.4 ("GSD phase status:
        blocked / BLOCKER file present"), not one derived from the dependency
        graph. A row that says blocked is not workable however satisfied its
        dependencies are; unblocked() excluded only `done`, so it handed the
        planner a row whose own label says it cannot be planned."""
        rows = self.rows(("P01", [], "blocked"), ("P02", [], "open"))
        self.assertEqual([r["id"] for r in outcomes.unblocked(rows)], ["P02"])
        self.assertEqual(outcomes.next_pending(rows)["id"], "P02")

    def test_a_parked_row_is_never_the_next_thing_to_work_on(self):
        """The fifth status in spec 3.4. VALID_PENDING_STATUS omits it, but that
        is not a guarantee it never arrives: check_shape validates a record's
        `pendings` no further than "is a list", and cmd_pendings_next feeds
        rec["pendings"] straight into unblocked() with no per-row revalidation,
        so a hand-edited ledger delivers one. Same authored-status reasoning."""
        rows = self.rows(("P01", [], "parked"), ("P02", [], "open"))
        self.assertEqual([r["id"] for r in outcomes.unblocked(rows)], ["P02"])

    def test_an_active_row_is_still_workable(self):
        """The exclusion is a denylist of the two authored not-workable
        statuses, not an allowlist of `open`. Work in progress is still the
        next thing to work on."""
        rows = self.rows(("P01", [], "active"))
        self.assertEqual(outcomes.next_pending(rows)["id"], "P01")

    def test_next_pending_is_none_when_everything_is_done(self):
        rows = self.rows(("P01", [], "done"))
        self.assertIsNone(outcomes.next_pending(rows))


class TestPendingBlockRoundTrip(OutcomeTestCase):

    def test_render_then_parse_is_stable(self):
        """A3-shaped: the generator is idempotent on unchanged input."""
        rows = outcomes.parse_pending_block(BLOCK)
        once = outcomes.render_pending_block(rows)
        twice = outcomes.render_pending_block(outcomes.parse_pending_block(once))
        self.assertEqual(once, twice)

    def test_no_row_is_lost_in_a_round_trip(self):
        """Idempotence alone does not prove completeness - a parser that drops
        the same row every time is idempotent too."""
        rows = outcomes.parse_pending_block(BLOCK)
        again = outcomes.parse_pending_block(outcomes.render_pending_block(rows))
        self.assertEqual(rows, again)
        self.assertEqual(len(rows), 3)


class TestPendingsCli(CliTestCase):

    def spec_file(self, body: str = BLOCK) -> Path:
        p = self.tmp / "a-spec.md"
        p.write_text("# A spec\n\nsome prose\n\n" + body + "\ntrailing prose\n",
                     encoding="utf-8")
        return p

    def test_ingest_attaches_rows_to_the_outcome(self):
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        out = self.cli("pendings-ingest", "--outcome", "outcome-0001",
                       "--spec", str(self.spec_file()))
        self.assertEqual(out["ingested"], 3)
        rec = outcomes.find_outcome(outcomes.read_outcomes(), "outcome-0001")
        self.assertEqual(len(rec["pendings"]), 3)
        self.assertEqual([r["id"] for r in rec["pendings"]],
                         ["P01", "P02", "P03"])

    def test_ingest_preserves_prose_outside_the_fence(self):
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        path = self.spec_file()
        before = path.read_text(encoding="utf-8")
        self.cli("pendings-ingest", "--outcome", "outcome-0001", "--spec", str(path))
        after = path.read_text(encoding="utf-8")
        self.assertEqual(after.count(outcomes.PEND_BEGIN), 1)

        # Byte-exact on BOTH sides of the fence. assertIn("# A spec") passes on
        # a file whose prose was mangled anywhere else, so it does not prove
        # "untouched"; slicing on the sentinels does.
        def outside(text):
            b = text.index(outcomes.PEND_BEGIN)
            e = text.index(outcomes.PEND_END) + len(outcomes.PEND_END)
            return text[:b], text[e:]
        self.assertEqual(outside(after), outside(before))
        self.assertEqual(outside(after), ("# A spec\n\nsome prose\n\n",
                                          "\n\ntrailing prose\n"))

    def test_next_prints_the_single_next_row(self):
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        self.cli("pendings-ingest", "--outcome", "outcome-0001",
                 "--spec", str(self.spec_file()))
        out = self.cli("pendings-next", "--outcome", "outcome-0001")
        self.assertEqual(out["next"]["id"], "P02")
        self.assertEqual(out["station"], "captured")
        self.assertEqual(out["next_station"], "specced")
        self.assertEqual((out["total"], out["done"], out["blocked"]), (3, 1, 1))

    def test_a_blocked_row_is_not_reported_as_the_next_one(self):
        """The user-visible half of the same defect: `next` named a row the
        same JSON counted inside its own derived complement, so the payload
        contradicted the row's label."""
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        blocked = BLOCK.replace(
            "counts kept and removed. | — | open |",
            "counts kept and removed. | — | blocked |")
        self.cli("pendings-ingest", "--outcome", "outcome-0001",
                 "--spec", str(self.spec_file(blocked)))
        out = self.cli("pendings-next", "--outcome", "outcome-0001")
        self.assertIsNone(out["next"])
        self.assertEqual((out["total"], out["done"], out["blocked"]), (3, 1, 2))

    def test_next_on_an_unknown_outcome_names_it(self):
        proc = self.cli("pendings-next", "--outcome", "outcome-0009", expect=2)
        self.assertIn("no such outcome: outcome-0009", proc.stderr)

    def test_ingest_of_a_missing_spec_file_exits_2(self):
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        proc = self.cli("pendings-ingest", "--outcome", "outcome-0001",
                        "--spec", str(self.tmp / "nope.md"), expect=2)
        self.assertIn("cannot read spec", proc.stderr)

    def test_ingest_of_a_cyclic_block_exits_2_and_writes_nothing(self):
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        cyclic = BLOCK.replace("| P01, P02 |", "| P02 |").replace(
            "| P02 | Triage the draft plans and report the counts kept and removed. | — |",
            "| P02 | Triage the draft plans and report the counts kept and removed. | P03 |")
        path = self.spec_file(cyclic)
        before = path.read_text(encoding="utf-8")
        # The exit code ALONE is vacuous here: argparse exits 2 on an unknown
        # subcommand, so this case passes before pendings-ingest exists at all.
        # Asserting the real message is what makes it a test of this feature.
        proc = self.cli("pendings-ingest", "--outcome", "outcome-0001",
                        "--spec", str(path), expect=2)
        self.assertIn("dependency cycle", proc.stderr)
        rec = outcomes.find_outcome(outcomes.read_outcomes(), "outcome-0001")
        self.assertEqual(rec["pendings"], [],
                         "a rejected ingest must leave the ledger untouched")
        self.assertEqual(path.read_text(encoding="utf-8"), before,
                         "a rejected ingest must leave the spec file untouched")

    def test_a_hand_edited_row_without_status_exits_2_not_1(self):
        """The user-visible half of the pending-row guard. `pendings-next`
        counted `r["status"] == "done"` straight off the stored row, so this
        ledger left main() as an uncaught KeyError: exit 1, which is not in the
        fixed set (0 ok, 2 bad input, 3 lock timeout, 4 write failure, 5 lock
        lost) and tells the caller nothing about which row to fix.
        """
        self.seed(make(pendings=[{"id": "P01",
                                  "description": "A row a human typed by hand.",
                                  "depends_on": []}]))
        proc = self.cli("pendings-next", "--outcome", "outcome-0001", expect=2)
        self.assertIn("P01", proc.stderr)
        self.assertIn("status", proc.stderr)


class TestPendingsSet(CliTestCase):
    """`pendings-set` writes the fence and the ledger in one call.

    The two-step it replaces -- hand-edit the fence, then remember to re-ingest
    -- desynced whenever the second half was forgotten, which is why every test
    here that asserts a status asserts it on BOTH sides.
    """

    # The fence fixture already exists on TestPendingsCli. Bound rather than
    # subclassed: subclassing would re-run that class's whole suite under a
    # second name, and a second builder would be a second thing to keep true.
    spec_file = TestPendingsCli.spec_file

    def test_set_updates_both_the_fence_and_the_ledger(self):
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        path = self.spec_file()
        self.cli("pendings-ingest", "--outcome", "outcome-0001", "--spec", str(path))
        out = self.cli("pendings-set", "--outcome", "outcome-0001",
                       "--spec", str(path), "--row", "P02", "--status", "done")
        self.assertEqual((out["row"], out["status"]), ("P02", "done"))

        row = [l for l in path.read_text(encoding="utf-8").splitlines()
               if l.startswith("| P02 |")]
        self.assertEqual(len(row), 1, row)
        self.assertTrue(row[0].rstrip().endswith("| done |"), row[0])

        rec = outcomes.find_outcome(outcomes.read_outcomes(), "outcome-0001")
        self.assertEqual([r["status"] for r in rec["pendings"] if r["id"] == "P02"],
                         ["done"])

    def test_set_refuses_done_while_a_dependency_is_open(self):
        """P03 depends on P01 (done) and P02 (open). The P10 guard is inherited
        from parse_pending_block, not reimplemented here."""
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        path = self.spec_file()
        self.cli("pendings-ingest", "--outcome", "outcome-0001", "--spec", str(path))
        proc = self.cli("pendings-set", "--outcome", "outcome-0001",
                        "--spec", str(path), "--row", "P03", "--status", "done",
                        expect=2)
        # Exit 2 alone is vacuous: argparse exits 2 on an unknown subcommand,
        # so the code check passes against a goals.py with no such parser.
        self.assertIn("depends on", proc.stderr)

    def test_set_leaves_the_fence_untouched_when_the_guard_fires(self):
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        path = self.spec_file()
        self.cli("pendings-ingest", "--outcome", "outcome-0001", "--spec", str(path))
        before = path.read_text(encoding="utf-8")
        proc = self.cli("pendings-set", "--outcome", "outcome-0001",
                        "--spec", str(path), "--row", "P03", "--status", "done",
                        expect=2)
        self.assertIn("depends on", proc.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), before,
                         "a pre-write refusal must restore the spec byte for byte")

    def test_set_does_not_roll_back_the_fence_after_the_ledger_commits(self):
        """The discriminating case for the conditional-rollback rule.

        A blanket "restore the original bytes on any exception" would revert the
        fence under an ALREADY-COMMITTED ledger -- manufacturing the desync this
        verb exists to remove. Asserting the ledger alone cannot catch that: a
        blanket rollback never touches the ledger, so the ledger assertion passes
        under both the correct and the buggy version. The assertion that
        discriminates is on the SPEC: the pre-edit must have survived.

        Forced by chmod-ing the spec's own directory read-only, which leaves the
        in-place pre-edit legal (write permission on the file, not the dir) and
        makes ingest's tmp+os.replace rewrite fail with EACCES -- i.e. after the
        ledger write.
        """
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        sub = self.tmp / "ro"           # its own dir: the chmod must not reach
        sub.mkdir()                     # goal-ledger/ or cache/
        path = sub / "a-spec.md"
        path.write_text(self.spec_file().read_text(encoding="utf-8"),
                        encoding="utf-8")
        self.cli("pendings-ingest", "--outcome", "outcome-0001", "--spec", str(path))
        original = path.read_text(encoding="utf-8")

        os.chmod(sub, 0o500)
        self.addCleanup(os.chmod, sub, 0o700)

        proc = self.cli("pendings-set", "--outcome", "outcome-0001",
                        "--spec", str(path), "--row", "P02", "--status", "active",
                        expect=4)
        # A phrase from THIS message, not ingest's own "cannot rewrite spec",
        # which is re-raised inside it and would pass under a blanket rollback.
        self.assertIn("not canonically re-rendered", proc.stderr)
        self.assertIn("pendings-ingest", proc.stderr)

        after = path.read_text(encoding="utf-8")
        self.assertNotEqual(after, original,
                            "the pre-edit must NOT be rolled back once the "
                            "ledger holds the new status")
        row = [l for l in after.splitlines() if l.startswith("| P02 |")]
        self.assertEqual(len(row), 1, row)
        self.assertTrue(row[0].rstrip().endswith("| active |"), row[0])

        rec = outcomes.find_outcome(outcomes.read_outcomes(), "outcome-0001")
        self.assertEqual([r["status"] for r in rec["pendings"] if r["id"] == "P02"],
                         ["active"])

    def test_set_rejects_an_unknown_row_id(self):
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        path = self.spec_file()
        self.cli("pendings-ingest", "--outcome", "outcome-0001", "--spec", str(path))
        before = path.read_text(encoding="utf-8")
        proc = self.cli("pendings-set", "--outcome", "outcome-0001",
                        "--spec", str(path), "--row", "P77", "--status", "done",
                        expect=2)
        self.assertIn("no row P77 in the fence", proc.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_set_rejects_a_row_id_that_is_not_a_row_id(self):
        """`--row id` matches the fence's HEADER line -- four cells, first cell
        `id`. The edit is harmless (render_pending_block re-emits the header
        from its own template, so the fence comes back byte-identical), which is
        precisely the problem: without this guard the command exits 0 and
        reports {"row": "id", "status": "done"} having changed nothing. A verb
        that reports success for a no-op is the failure this ledger exists to
        prevent."""
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        path = self.spec_file()
        self.cli("pendings-ingest", "--outcome", "outcome-0001", "--spec", str(path))
        before = path.read_text(encoding="utf-8")
        proc = self.cli("pendings-set", "--outcome", "outcome-0001",
                        "--spec", str(path), "--row", "id", "--status", "done",
                        expect=2)
        self.assertIn("not PNN", proc.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_set_rejects_an_unknown_status_value(self):
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        path = self.spec_file()
        self.cli("pendings-ingest", "--outcome", "outcome-0001", "--spec", str(path))
        proc = self.cli("pendings-set", "--outcome", "outcome-0001",
                        "--spec", str(path), "--row", "P02", "--status", "banana",
                        expect=2)
        self.assertIn("banana", proc.stderr)
        self.assertIn("status", proc.stderr)


class WholeFileRoundTrip(OutcomeTestCase):
    """Splicing a rendered parse back over its own source returns the source,
    byte for byte.

    `TestPendingBlockRoundTrip` proves the *block* round-trips and
    `test_ingest_preserves_prose_outside_the_fence` proves the prose either
    side survives -- but that one explicitly disclaims whole-file identity, so
    nothing today asserts the two halves join back into the original bytes.
    That property is what A3 rests on for the spec fence, and it is currently
    true and untested.
    """

    def fixture(self, block: str = BLOCK) -> str:
        """BLOCK is byte-exactly what render_pending_block emits, so the only
        thing under test is the splice. Hand-typing a second fixture would put
        a missing blank line after BEGIN on the same footing as a real
        regression."""
        return "# A spec\n\nsome prose\n\n" + block + "\ntrailing prose\n"

    def test_splice_of_rendered_parse_is_byte_identical_on_a_fixture(self):
        fixture = self.fixture()
        self.assertEqual(
            outcomes._splice(
                fixture,
                outcomes.render_pending_block(
                    outcomes.parse_pending_block(fixture))),
            fixture)

    def test_splice_of_rendered_parse_is_byte_identical_on_the_live_spec(self):
        """The same property on the real governing document, which carries an
        illustrative fence at 5.1 as well as the live P01-Pnn table -- so this
        also pins _fence_span's line-anchored locator against the shape that
        actually exists on disk.

        OUTCOME_LEDGER_SPEC_PATH exists so the negative control can repoint the
        test at a mutated copy without editing the test. The spec is only ever
        READ here.
        """
        path = os.environ.get(
            "OUTCOME_LEDGER_SPEC_PATH",
            "/home/pedro/.claude/docs/superpowers/specs/"
            "2026-08-27-outcome-ledger-design.md")
        if not os.path.exists(path):
            self.skipTest("governing spec not present")
        text = Path(path).read_text(encoding="utf-8")
        self.assertEqual(
            outcomes._splice(
                text,
                outcomes.render_pending_block(
                    outcomes.parse_pending_block(text))),
            text)

    def test_a_row_with_a_dependency_on_an_unknown_id_is_rejected(self):
        """check_dependencies' unknown-id guard, reached through the parser."""
        fixture = self.fixture(BLOCK.replace("| P01, P02 |", "| P99 |"))
        with self.assertRaises(goals.Abort) as ctx:
            outcomes.parse_pending_block(fixture)
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("P99", str(ctx.exception.message))

    def test_a_second_bare_end_sentinel_aborts_the_splice(self):
        """_fence_span aborts rather than guessing which fence to overwrite.
        Zero real vault files carry a duplicate bare sentinel today, so a
        fixture is the only way this guard is ever exercised."""
        fixture = self.fixture()
        block = outcomes.render_pending_block(
            outcomes.parse_pending_block(fixture))
        with self.assertRaises(goals.Abort) as ctx:
            outcomes._splice(fixture + "\n%%PENDINGS:END%%\n", block)
        self.assertEqual(ctx.exception.code, 4)
        self.assertIn("ambiguous", str(ctx.exception.message))


class TestClaimsDoNotEvictRunReceipts(CliTestCase):
    """A claim is not a goal step and must not spend a slot in the run-receipt
    window.

    `validate_report.py` accepts a report only while that session's receipt
    still carries a `commit` or `parse-extraction` entry, and `_record_run`
    keeps the last `RUN_RECEIPT_MAX_ENTRIES`. `--session` on outcome-claim
    identifies the holder of an outcome, not a run of the goal step, so a
    sitting that refreshes its heartbeat -- the documented pattern, the claim
    expires after four hours -- evicted its own goal step and got a correct
    progress report rejected.
    """

    def receipt_cmds(self, session: str) -> list:
        return [e.get("cmd") for e in
                goals._read_receipt_entries(goals.RUN_DIR / f"{session}.json")]

    def test_claim_heartbeats_do_not_evict_the_goal_step_receipt(self):
        session = "sess-a"
        self.cli("outcome-create", "--statement", "a thing worth doing",
                 "--projects", "proj")
        goals._record_run(session, "parse-extraction", 0, "proj", 3)

        # One more than the window, so the seeded entry is the first evicted if
        # these runs are recorded at all.
        for _ in range(goals.RUN_RECEIPT_MAX_ENTRIES + 1):
            self.cli("outcome-claim", "--outcome", "outcome-0001",
                     "--session", session)
        self.cli("outcome-release", "--outcome", "outcome-0001",
                 "--session", session)

        cmds = self.receipt_cmds(session)
        self.assertIn("parse-extraction", cmds,
                      "claim heartbeats evicted the entry validate_report.py "
                      "reads, and a correct report would be rejected")
        # The positive statement of the same fix: they are not merely surviving
        # a smaller window, no receipt is written for a claim at all.
        self.assertEqual(cmds, ["parse-extraction"])


if __name__ == "__main__":
    unittest.main()
