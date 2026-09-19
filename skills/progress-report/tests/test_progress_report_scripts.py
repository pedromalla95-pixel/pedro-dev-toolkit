import json
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import digest  # noqa: E402
import validate_report  # noqa: E402


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")


def user(uuid: str, parent: str | None, text: str, *, cwd: str = r"/home/pedro/Projects/Cucina-Nostra/cucina-inventory") -> dict:
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": "user",
        "timestamp": "2026-06-17T10:00:00Z",
        "cwd": cwd,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def assistant(uuid: str, parent: str | None, blocks: list[dict], *, branch: str = "main") -> dict:
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "type": "assistant",
        "timestamp": "2026-06-17T10:01:00Z",
        "gitBranch": branch,
        "message": {"role": "assistant", "content": blocks},
    }


def attachment(uuid: str, parent: str | None) -> dict:
    return {"uuid": uuid, "parentUuid": parent, "type": "attachment"}


class DigestTests(unittest.TestCase):
    def test_project_cwd_resolves_to_cucina_transcript_folder(self) -> None:
        self.assertEqual(
            digest.transcript_dir_name(r"/home/pedro/Projects/Cucina-Nostra/cucina-inventory"),
            "-home-pedro-Projects-Cucina-Nostra-cucina-inventory",
        )

    def test_linear_digest_tracks_edits_reads_errors_and_todos(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "linear.jsonl"
            entries = [
                {"sessionId": "s1"},
                user("u1", None, "Implement the feature"),
                assistant(
                    "a1",
                    "u1",
                    [
                        {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "src/a.ts"}},
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                        {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "src/b.ts"}},
                        {"type": "tool_use", "id": "t3", "name": "TodoWrite", "input": {"todos": [{"content": "verify", "status": "pending"}]}},
                        {"type": "tool_result", "tool_use_id": "t3", "is_error": True, "content": "boom"},
                        {"type": "text", "text": "We chose the direct approach because it is smaller."},
                    ],
                ),
            ]
            write_jsonl(transcript, entries)
            data = digest.digest_transcript(str(transcript))

        self.assertEqual(data["first_user"], "Implement the feature")
        self.assertEqual(data["files_touched"], [["src/a.ts", 1]])
        self.assertEqual(data["files_read_only"], [["src/b.ts", 1]])
        self.assertEqual(data["errors"][0]["snippet"], "boom")
        self.assertEqual(data["todos_seen"], [{"content": "verify", "status": "pending"}])
        self.assertEqual(data["decision_signals"][0][0], "assistant")

    def test_rewound_branch_excludes_abandoned_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "rewound.jsonl"
            entries = [
                {"sessionId": "s2"},
                user("u1", None, "Start"),
                assistant("a1", "u1", [{"type": "tool_use", "id": "old", "name": "Edit", "input": {"file_path": "old.ts"}}]),
                user("u2", "a1", "Continue abandoned"),
                assistant("a2", "u2", [{"type": "tool_use", "id": "old2", "name": "Read", "input": {"file_path": "old.ts"}}]),
                user("u3", "u1", "New branch"),
                assistant("a3", "u3", [{"type": "tool_use", "id": "new", "name": "Edit", "input": {"file_path": "new.ts"}}]),
            ]
            write_jsonl(transcript, entries)
            data = digest.digest_transcript(str(transcript))

        self.assertEqual(data["files_touched"], [["new.ts", 1]])
        self.assertEqual(data["rewind_excluded"], 3)

    def test_attachment_parent_chain_keeps_prior_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "attachment.jsonl"
            entries = [
                {"sessionId": "s3"},
                user("u1", None, "Goal through attachment"),
                attachment("att1", "u1"),
                assistant("a1", "att1", [{"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "x.ts"}}]),
            ]
            write_jsonl(transcript, entries)
            data = digest.digest_transcript(str(transcript))

        self.assertEqual(data["first_user"], "Goal through attachment")
        self.assertEqual(data["active_msg_count"], 2)

    def test_research_trigger_requires_opt_in_later_but_digest_detects_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "research.jsonl"
            entries = [
                {"sessionId": "s4"},
                user("u1", None, "Research docs"),
                assistant(
                    "a1",
                    "u1",
                    [
                        {"type": "tool_use", "id": "w1", "name": "WebSearch", "input": {"query": "a"}},
                        {"type": "tool_use", "id": "w2", "name": "WebFetch", "input": {"url": "https://example.com"}},
                        {"type": "tool_use", "id": "w3", "name": "WebSearch", "input": {"query": "b"}},
                    ],
                ),
            ]
            write_jsonl(transcript, entries)
            data = digest.digest_transcript(str(transcript), r"C:\vault\Research")

        self.assertTrue(data["research"]["triggered"])
        self.assertEqual(data["research"]["signal_count"], 3)

    def test_resolve_transcript_uses_project_folder_and_fails_closed_on_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "-home-pedro-Projects-Cucina-Nostra-cucina-inventory"
            folder.mkdir()
            first = folder / "first.jsonl"
            second = folder / "second.jsonl"
            write_jsonl(first, [{"sessionId": "s1"}])
            write_jsonl(second, [{"sessionId": "s2"}])
            now = time.time()
            os.utime(first, (now, now))
            os.utime(second, (now - 1, now - 1))

            result = digest.resolve_transcript(
                r"/home/pedro/Projects/Cucina-Nostra/cucina-inventory",
                projects_root=root,
                ambiguity_window_seconds=180,
            )

        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)

    def test_resolve_transcript_accepts_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "explicit.jsonl"
            write_jsonl(transcript, [{"sessionId": "s1"}])
            result = digest.resolve_transcript("ignored", transcript_override=str(transcript), projects_root=temp)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(Path(result["transcript"]), transcript)


class ValidateReportTests(unittest.TestCase):
    def test_report_validator_rejects_modified_working_overlap(self) -> None:
        text = """---
session_uuid: x
---
# Session Progress Report - 2026-06-17

## 10:00 - summary

### Resume Here
- one

### Technical Status
**Modified:**
- `src/a.ts` - 1 edit

**Working on (read, not modified):**
- `src/a.ts` - read 1x
"""
        errors = validate_report.validate_report_text(text)
        self.assertTrue(any("modified files also listed" in error for error in errors))

    def test_report_validator_checks_every_appended_snapshot(self) -> None:
        text = """---
session_uuid: x
---
# Session Progress Report - 2026-06-17

## 10:00 - summary

### Resume Here
- one

### Technical Status
**Modified:**
- `src/a.ts` - 1 edit

**Working on (read, not modified):**
- `src/b.ts` - read 1x

## 11:00 - summary

### Resume Here
- one

### Technical Status
**Modified:**
- `src/c.ts` - 1 edit

**Working on (read, not modified):**
- `src/c.ts` - read 1x
"""
        errors = validate_report.validate_report_text(text)
        self.assertTrue(any("Technical Status block 2" in error for error in errors))

    def test_report_validator_accepts_clean_report_shape(self) -> None:
        text = """---
session_uuid: x
---
# Session Progress Report - 2026-06-17

## 10:00 - summary

### Resume Here
- one
- two

### Technical Status
**Modified:**
- `src/a.ts` - 1 edit

**Working on (read, not modified):**
- `src/b.ts` - read 1x
"""
        self.assertEqual(validate_report.validate_report_text(text), [])

    def test_result_block_omits_local_target_when_disabled(self) -> None:
        block = """<<<PROGRESS_REPORT_RESULT>>>
paths_written:
  - C:\\vault\\2026-06-17.md
one_line_summary: wrote report
goal_accounting: committed=2 proposed=3
tldr:
  - one
  - two
  - three
<<<END>>>
"""
        result = validate_report.parse_result_block(block)
        self.assertEqual(result["paths_written"], [r"C:\vault\2026-06-17.md"])


class TestGoalAccountingGrammar(unittest.TestCase):
    """Part 2: the report declares its goal accounting, and the declaration is
    part of the result-block grammar rather than prose the builder may drop."""

    @staticmethod
    def _block(accounting_line: str) -> str:
        return ("<<<PROGRESS_REPORT_RESULT>>>\n"
                "paths_written:\n"
                "  - /tmp/x.md\n"
                "one_line_summary: wrote report\n"
                f"{accounting_line}"
                "tldr:\n  - one\n  - two\n  - three\n"
                "<<<END>>>\n")

    def test_accounting_line_parses(self) -> None:
        # [RED] before Part 2 this exact block was rejected by the parser with
        # `unparseable result block line: goal_accounting: committed=2 proposed=3`
        # — captured against unmodified HEAD (6bfe1a83) before the parser was
        # touched. That rejection IS the coupling: a builder cannot emit the
        # accounting line without this validator having been taught it, so the
        # two cannot drift apart silently.
        result = validate_report.parse_result_block(
            self._block("goal_accounting: committed=2 proposed=3\n"))
        self.assertEqual(result["goal_accounting"], "committed=2 proposed=3")

    def test_missing_accounting_line_is_rejected_by_name(self) -> None:
        with self.assertRaises(validate_report.ValidationError) as caught:
            validate_report.parse_result_block(self._block(""))
        self.assertIn("goal_accounting", str(caught.exception))
        # Self-describing: SKILL.md may be out of context when this is read.
        self.assertIn("progress-report-builder.md", str(caught.exception))

    def test_missing_line_message_prints_no_pasteable_value(self) -> None:
        # [RED] this message used to end "...with one of: `committed=<n>
        # proposed=<n>` | ledger-error | thin-session | not-supplied", which
        # made the cheapest literal compliance appending `goal_accounting:
        # thin-session` — a value that returns clean with no receipt required.
        # A rejection may not print its own bypass. Like the `not-supplied`
        # message, it names the builder as the author and points at a re-run.
        with self.assertRaises(validate_report.ValidationError) as caught:
            validate_report.parse_result_block(self._block(""))
        message = str(caught.exception)
        for value in ("thin-session", "ledger-error", "not-supplied", "committed="):
            self.assertNotIn(value, message)
        self.assertIn("Step 4b", message)
        self.assertIn("Step 5", message)

    def test_a_duplicate_accounting_line_is_rejected(self) -> None:
        # [RED] `result[...] = ...` last-wins, so a second line silently
        # overrode the first: an honest `ledger-error` followed by
        # `thin-session` parsed as `thin-session`.
        with self.assertRaises(validate_report.ValidationError) as caught:
            validate_report.parse_result_block(
                self._block("goal_accounting: ledger-error\n"
                            "goal_accounting: thin-session\n"))
        self.assertIn("more than once", str(caught.exception))

    def test_duplicate_and_missing_are_different_rejections(self) -> None:
        # Both name the same line; only the message says which mistake was made.
        with self.assertRaises(validate_report.ValidationError) as missing:
            validate_report.parse_result_block(self._block(""))
        with self.assertRaises(validate_report.ValidationError) as duplicate:
            validate_report.parse_result_block(
                self._block("goal_accounting: ledger-error\ngoal_accounting: ledger-error\n"))
        self.assertNotIn("more than once", str(missing.exception))
        self.assertIn("more than once", str(duplicate.exception))

    def test_each_word_value_is_accepted(self) -> None:
        for value in ("ledger-error", "thin-session", "not-supplied"):
            with self.subTest(value=value):
                result = validate_report.parse_result_block(
                    self._block(f"goal_accounting: {value}\n"))
                self.assertEqual(result["goal_accounting"], value)

    def test_unrecognised_value_is_rejected_with_the_accepted_set(self) -> None:
        for value in ("ok", "committed=2", "committed=x proposed=1", "committed=-1 proposed=0",
                      "committed=1 proposed=2 extra"):
            with self.subTest(value=value):
                with self.assertRaises(validate_report.ValidationError) as caught:
                    validate_report.parse_result_block(self._block(f"goal_accounting: {value}\n"))
                self.assertIn("unrecognised goal_accounting value", str(caught.exception))

    def test_the_reason_marker_is_pinned_to_the_builders_own_contract(self) -> None:
        # Pinned from the CODE side. The wiring suite greps the builder file for
        # a hard-coded copy of this literal, which cannot notice the constant
        # below being renamed; this can. If the two ever disagree, every
        # `ledger-error` and `thin-session` report is rejected for a line the
        # builder was never told to write.
        builder = Path("/home/pedro/.claude/agents/progress-report-builder.md")
        self.assertTrue(builder.is_file(), builder)
        self.assertIn(validate_report.REPORT_GOAL_REASON_MARKER,
                      builder.read_text(encoding="utf-8"),
                      "the builder's durable contract must name the literal the validator greps")

    def test_zero_counts_are_a_legal_declaration(self) -> None:
        # A gate that ran and produced nothing is a real outcome, not an error.
        result = validate_report.parse_result_block(
            self._block("goal_accounting: committed=0 proposed=0\n"))
        self.assertEqual(result["goal_accounting"], "committed=0 proposed=0")

    def test_resume_here_limit_is_enforced(self) -> None:
        text = """---
x: y
---
# Session Progress Report - 2026-06-17

## 10:00 - summary

### Resume Here
- one
- two
- three
- four
- five
- six
- seven

### Technical Status
**Modified:**
- none

**Working on (read, not modified):**
- none
"""
        errors = validate_report.validate_report_text(text)
        self.assertTrue(any("Resume Here block 1 has 7 bullets" in error for error in errors))


# Distinguishes "build the metrics from the two count arguments" (the default)
# from "write these metrics verbatim" and from "omit the key entirely" (`None`).
# A plain default cannot express all three.
_DERIVE_METRICS = object()

# The same three-way problem for the run-start marker's timestamp: the default
# time, a caller-supplied one (including an unparseable one), and no `ts` key at
# all (`None`). Named for the sentinel it is, not for the value it stands for —
# the VALUE is `GoalReceiptHarness.DEFAULT_MARKER_TS`.
_MARKER_DEFAULT = object()


class GoalReceiptHarness(unittest.TestCase):
    """--stamp-session: the freshness stamp may ONLY appear via a passing `result`
    validation (c4 structural enforcement, AUDIT-2026-07-17)."""

    def setUp(self) -> None:
        # HOME is redirected because `write_fresh_stamp` resolves the stamp dir
        # through `expanduser("~")`; GOALS_RUN_DIR because `run_dir()` reads it.
        # Both must be isolated or the tests below write into Pedro's live
        # ~/.claude/cache — and `--stamp-session` is now required, so every
        # passing test in this class stamps for real without the first.
        self._home = tempfile.mkdtemp()
        self._run_dir = Path(self._home) / "goal-run"
        self._old_env = {key: os.environ.get(key) for key in ("HOME", "GOALS_RUN_DIR")}
        os.environ["HOME"] = self._home
        os.environ["GOALS_RUN_DIR"] = str(self._run_dir)

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        import shutil
        shutil.rmtree(self._home, ignore_errors=True)

    def _stamp_path(self, uuid: str) -> Path:
        return Path(self._home) / ".claude" / "cache" / "progress-report-fresh" / (uuid + ".json")

    def _marker_path(self, uuid: str) -> Path:
        """Where SKILL.md Step 2's run-start marker lands. Under HOME, like the
        stamp and for the same reason — it resolves through `expanduser("~")`
        with no environment variable of its own, so redirecting HOME is the whole
        of its isolation."""
        return Path(self._home) / ".claude" / "cache" / "progress-report-runstart" / (uuid + ".json")

    # When `_run` writes a run-start marker of its own accord, this is the time
    # it records. Deliberately EARLIER than the 18:00:00Z every `_write_stamp`
    # call in this file uses: it keeps the STAMP the binding floor throughout the
    # pre-existing tests, so those keep testing what they were written to test
    # and the marker side of `max` is exercised only by the tests that set out to.
    # An equal default would have made that separation an accident of two
    # timestamps happening to match.
    DEFAULT_MARKER_TS = "2026-08-13T17:30:00Z"

    # The project every test in this class validates for, unless it says
    # otherwise. Its slug is itself (`slugify("demo") == "demo"`), so a fixture
    # can write the slug directly without the test having to model slugify.
    PROJECT = "demo"

    # The time an ordinary fixture entry records. Every real receipt entry
    # carries one (`goals.py` writes `_now_iso` on every path), and every entry
    # must now clear a floor, so a fixture that omits it is testing an entry of
    # unknown age — which is its own rejection, asserted by name in
    # `test_an_entry_with_no_timestamp_cannot_clear_the_floor`. Later than
    # `DEFAULT_MARKER_TS` and than the 18:00:00Z the stamp fixtures use, so an
    # ordinary entry clears both floors.
    ENTRY_TS = "2026-08-13T18:41:07Z"

    def _clean_entries(self, committed: int = 1, proposed: int = 1, *,
                       project: str | None = PROJECT,
                       ts: str | None = ENTRY_TS) -> list[dict]:
        """The receipt an HONEST run leaves: a clean `commit` for this project
        carrying the number of decisions it applied, and a clean
        `parse-extraction` FOR THE SAME PROJECT carrying the number of proposals
        it parsed.

        Both are required by `check_goal_accounting`, and BOTH must name this
        project, so a fixture that omits either — or that leaves either without
        a project — is testing a rejection, not a pass.

        The `parse-extraction` entry recorded `project: None` here for one
        round, because the subcommand had no `--project` to pass. That was the
        writer's real output at the time and it is why the `proposed` half was
        bound to no project at all: thirteen tests in this file were passing on
        a project-less entry. `parse-extraction` now takes an optional
        `--project` on the same terms as `--session`, SKILL.md Step 4b passes
        it, and this fixture records what that call actually writes. The
        project-less entry has not disappeared — the flag is still optional —
        so it is exercised by name in `_clean_entries_without_parse_project`.

        `ts=None` omits the timestamp KEY entirely rather than writing a null,
        which is what an entry of unknown age actually looks like to the
        freshness floor.
        """
        entries = [
            {"cmd": "commit", "exit": 0, "project": project, "effect": committed},
            {"cmd": "parse-extraction", "exit": 0, "project": project, "effect": proposed},
        ]
        if ts is not None:
            for entry in entries:
                entry["ts"] = ts
        return entries

    def _clean_entries_without_parse_project(
            self, committed: int = 1, proposed: int = 1, *,
            ts: str = "2026-08-13T18:41:07Z") -> list[dict]:
        """An otherwise-honest receipt whose `parse-extraction` entry records
        `project: null` — what a caller that omits the now-optional `--project`
        writes, and byte-for-byte what EVERY entry written before that flag
        existed looks like. Kept as its own fixture so the backward-compatibility
        decision is exercised on purpose rather than inherited by accident.
        """
        entries = self._clean_entries(committed, proposed, ts=ts)
        for entry in entries:
            if entry["cmd"] == "parse-extraction":
                entry["project"] = None
        return entries

    def _thin_entry(self, *, event_count: int = 3, active_msg_count: int = 12,
                    project: str | None = PROJECT, ts: str | None = ENTRY_TS,
                    exit_code: int = 0, metrics: object = _DERIVE_METRICS) -> dict:
        """The receipt entry `goals.py thin-session` leaves behind: this run
        recorded that it skipped the goal step, for this project, with the two
        digest metrics that justify the skip.

        The `cmd` value is read from the validator's own accessor rather than
        spelled here, so a fixture can never test a command name the code stopped
        using. `metrics=None` omits the key entirely — what a legacy entry, or an
        entry from any other subcommand, actually looks like.
        """
        entry: dict = {"cmd": validate_report.thin_session_cmd(), "exit": exit_code,
                       "project": project, "effect": None}
        if metrics is _DERIVE_METRICS:
            entry["metrics"] = {"event_count": event_count, "active_msg_count": active_msg_count}
        elif metrics is not None:
            entry["metrics"] = metrics
        if ts is not None:
            entry["ts"] = ts
        return entry

    def _write_receipt(self, session: str, entries: list[dict] | object) -> Path:
        """Stand in for `goals.py`'s receipt writer. `entries` is passed through
        untouched so a malformed shape — or a LEGACY entry carrying neither
        `project` nor `effect` — can be exercised."""
        self._run_dir.mkdir(parents=True, exist_ok=True)
        path = self._run_dir / (session + ".json")
        path.write_text(json.dumps({"session": session, "entries": entries}), encoding="utf-8")
        return path

    def _write_response(self, vault: str, accounting: str = "committed=1 proposed=1") -> str:
        block = (
            "<<<PROGRESS_REPORT_RESULT>>>\n"
            "paths_written:\n"
            f"  - {vault}\n"
            "one_line_summary: wrote report\n"
            f"goal_accounting: {accounting}\n"
            "tldr:\n  - one\n  - two\n  - three\n"
            "<<<END>>>\n"
        )
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write(block)
        return path

    def _write_stamp(self, session: str, ts: object) -> Path:
        """Stand in for a PREVIOUS run's accepted report. `ts` is passed through
        untouched so an unparseable or absent one can be exercised."""
        stamp_dir = Path(self._home) / ".claude" / "cache" / "progress-report-fresh"
        stamp_dir.mkdir(parents=True, exist_ok=True)
        path = stamp_dir / (session + ".json")
        payload: dict = {"vault_target": "", "local_target": ""}
        if ts is not None:
            payload["ts"] = ts
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _write_marker(self, session: str, ts: object = _MARKER_DEFAULT) -> Path:
        """Stand in for SKILL.md Step 2's `validate_report.py run-start` call.
        `ts` is passed through untouched so an unparseable one can be exercised,
        and `ts=None` omits the KEY entirely — the same spelling `_write_stamp`
        uses, so the two fixtures cannot mean opposite things by the same
        argument. The sentinel default writes `DEFAULT_MARKER_TS`.

        Written by hand rather than by calling the real command, for the reason
        every fixture in this class is: the command stamps `datetime.now()`, and
        a floor pinned to wall-clock cannot be ordered against fixtures. The real
        writer is exercised end-to-end by its own seam test.
        """
        marker_dir = Path(self._home) / ".claude" / "cache" / "progress-report-runstart"
        marker_dir.mkdir(parents=True, exist_ok=True)
        path = marker_dir / (session + ".json")
        payload: dict = {}
        if ts is _MARKER_DEFAULT:
            payload["ts"] = self.DEFAULT_MARKER_TS
        elif ts is not None:
            payload["ts"] = ts
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _args(self, response: str, vault: str, stamp: str,
              local: str = "", project: str | None = None) -> "argparse.Namespace":
        import argparse
        return argparse.Namespace(
            agent_response=response, vault_target=vault, local_target=local, stamp_session=stamp,
            project=self.PROJECT if project is None else project)

    def _run(self, response: str, vault: str, stamp: str,
             project: str | None = None, marker: object = _MARKER_DEFAULT) -> tuple[int, dict]:
        """Run `cmd_result` and hand back BOTH the exit code and the payload.
        Asserting on the code alone passes for the wrong reason here — this
        command accumulates errors in a list, so every one of these failures
        exits 1 and only the message says which check fired.

        A run-start marker is written FIRST unless the caller passes
        `marker=None`, because SKILL.md Step 2 writes one on every run before
        anything else touches `goals.py`: a fixture omitting it would describe a
        run that cannot happen, and — since an absent marker fails closed — would
        report the marker's own rejection in place of the one the test meant to
        assert. `marker=None` is therefore an explicit statement that a run
        without a marker is the thing under test, never an oversight."""
        if marker is not None:
            self._write_marker(stamp, marker)
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = validate_report.cmd_result(self._args(response, vault, stamp, project=project))
        return rc, json.loads(buffer.getvalue())

    @staticmethod
    def _vault_file(content: str = "") -> str:
        fd, vault = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        return vault

    @classmethod
    def _vault_stating_its_reason(cls) -> str:
        """A report body carrying the stated-reason line the builder is required
        to render when it has no goal data. Pinned to the validator's own
        literal so the two cannot drift apart silently."""
        return cls._vault_file(
            "## 19:00 — session\n\n### Resume Here\n"
            f"- {validate_report.REPORT_GOAL_REASON_MARKER} the goal ledger errored.\n")

    @classmethod
    def _day_file(cls, *blocks: str) -> str:
        """A day-file as dayfile-write.cjs builds it: frontmatter, H1, then H2
        blocks NEWEST FIRST. Pass blocks in the order they appear on disk."""
        body = "---\nsession_uuid: sess-day\n---\n\n# Session Progress Report — 2026-08-13\n\n"
        return cls._vault_file(body + "\n".join(blocks))

    @staticmethod
    def _block(hh_mm: str, *, states_reason: bool) -> str:
        block = f"## {hh_mm} — a run\n\n### Resume Here\n- **Branch:** main\n"
        if states_reason:
            block += f"- {validate_report.REPORT_GOAL_REASON_MARKER} the goal ledger errored.\n"
        return block



class TestFreshStamp(GoalReceiptHarness):
    """The freshness stamp and the goal-ledger receipt check."""

    def test_an_older_blocks_stated_reason_does_not_answer_for_this_run(self) -> None:
        # [RED] The day-file is append-on-top: a same-day re-run inserts its
        # block ABOVE the previous one. Run 1 stated its reason; run 2 says
        # nothing. Searching the whole file would find run 1's line and accept a
        # run that is silent — the exact "present somewhere is not present for
        # this run" bypass closed on the receipt side.
        vault = self._day_file(self._block("20:00", states_reason=False),
                               self._block("19:00", states_reason=True))
        self._write_receipt("sess-day", [{"cmd": "load", "exit": 3,
                                          "ts": "2026-08-13T20:00:00Z"}])
        rc, out = self._run(self._write_response(vault, accounting="ledger-error"),
                            vault, "sess-day")
        self.assertEqual(rc, 1, out)
        self.assertTrue(any("does not state why it carries no goals" in error
                            for error in out["errors"]), out)
        self.assertTrue(any("earlier block in the same day-file does not answer" in error
                            for error in out["errors"]), out)

    def test_this_runs_block_answers_regardless_of_what_sits_below_it(self) -> None:
        # [GREEN] Opposite polarity of the test above: the newest block states
        # the reason and the older one does not. Nothing below the newest block
        # may make an honest run fail.
        vault = self._day_file(self._block("20:00", states_reason=True),
                               self._block("19:00", states_reason=False))
        self._write_receipt("sess-day", [{"cmd": "load", "exit": 3,
                                          "ts": "2026-08-13T20:00:00Z",
                                          "project": self.PROJECT, "effect": None}])
        rc, out = self._run(self._write_response(vault, accounting="ledger-error"),
                            vault, "sess-day")
        self.assertEqual(rc, 0, out)

    def test_a_report_body_with_no_h2_block_fails_closed(self) -> None:
        # [EDGE] Every report block opens with `## <hh:mm> — <summary>`. A body
        # with none was not written by the locked writer, so there is no "this
        # run's block" to hold to the contract -> reject, never pass by being
        # unable to locate it.
        vault = self._vault_file(
            "### Resume Here\n"
            f"- {validate_report.REPORT_GOAL_REASON_MARKER} the goal ledger errored.\n")
        self._write_receipt("sess-day", [{"cmd": "load", "exit": 3,
                                          "ts": "2026-08-13T20:00:00Z"}])
        rc, out = self._run(self._write_response(vault, accounting="ledger-error"),
                            vault, "sess-day")
        self.assertEqual(rc, 1, out)
        self.assertTrue(any("carries no `## ` block" in error for error in out["errors"]), out)

    def test_newest_block_stops_at_the_next_block(self) -> None:
        # [EDGE] Unit-level pin on the scoping itself: the returned slice starts
        # at the first `## ` and ends before the second, so a later `## ` cannot
        # drag inherited text into this run's block.
        text = "# H1\n\n## 20:00 — new\nalpha\n\n## 19:00 — old\nbeta\n"
        block = validate_report.newest_block(text)
        self.assertIn("alpha", block)
        self.assertNotIn("beta", block)
        self.assertTrue(block.startswith("## 20:00"))
        self.assertIsNone(validate_report.newest_block("# H1\n\n### only an h3\n"))

    def test_pass_with_stamp_session_writes_stamp(self) -> None:
        # [GREEN] valid accounting + a receipt whose entries include a `commit`
        # at exit 0 -> exit 0, stamp written.
        vault = self._vault_file()
        self._write_receipt("sess-aaa", [{"cmd": "load", "exit": 0, "ts": "2026-08-13T18:41:00Z",
                                          "project": self.PROJECT, "effect": None}]
                            + self._clean_entries(1, 1))
        rc, out = self._run(self._write_response(vault), vault, "sess-aaa")
        self.assertEqual(rc, 0, out)
        stamp = self._stamp_path("sess-aaa")
        self.assertTrue(stamp.is_file())
        payload = json.loads(stamp.read_text())
        self.assertEqual(payload["vault_target"], vault)

    def test_fail_never_writes_stamp(self) -> None:
        self._write_receipt("sess-bbb", [{"cmd": "commit", "exit": 0}])
        rc, out = self._run(
            self._write_response("/nonexistent/report.md"), "/nonexistent/report.md", "sess-bbb")
        self.assertEqual(rc, 1)
        self.assertTrue(any("was not written" in error for error in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-bbb").exists())

    def test_cli_seam_pass_writes_stamp(self) -> None:
        # Through the real parser — pins the --stamp-session flag name and wiring
        # (a hand-built Namespace stays green through a parser drift; this doesn't).
        vault = self._vault_file()
        self._write_marker("sess-cli")
        self._write_receipt("sess-cli", self._clean_entries(1, 1))
        parser = validate_report.build_parser()
        args = parser.parse_args(["result", self._write_response(vault),
                                  "--vault-target", vault, "--stamp-session", "sess-cli",
                                  "--project", self.PROJECT])
        rc = args.func(args)
        self.assertEqual(rc, 0)
        self.assertTrue(self._stamp_path("sess-cli").is_file())

    def test_traversal_session_id_rejected_no_stamp(self) -> None:
        # `marker=None`: a traversing id must not be turned into a path by
        # anything, and the FIXTURE writing one would put `evil.json` outside the
        # marker directory itself — proving nothing about the code under test
        # while leaving exactly the artifact this test asserts is absent. The id
        # check fires before the marker is ever consulted, so nothing here
        # depends on a marker existing.
        vault = self._vault_file()
        rc, out = self._run(self._write_response(vault), vault, "../evil", marker=None)
        self.assertEqual(rc, 1)
        self.assertTrue(any("invalid --stamp-session id" in error for error in out["errors"]), out)
        # The marker path is built from the same id and gets the same treatment.
        self.assertFalse(any("run-start marker" in error for error in out["errors"]), out)
        self.assertFalse((Path(self._home) / ".claude" / "cache" / "evil.json").exists())
        # The goal check must not build a receipt path out of a rejected id: a
        # second error naming a traversed receipt path would mean it did.
        self.assertFalse(any("goal-ledger activity" in error for error in out["errors"]), out)
        self.assertFalse((Path(self._home) / ".claude" / "cache" / "evil.json").exists())
        self.assertFalse(self._stamp_path("../evil").exists())

    def test_missing_stamp_session_fails_closed(self) -> None:
        # [EDGE] --stamp-session omitted entirely -> exit 1. Deliberate: the
        # goal check is keyed on the session, so a caller who drops the flag
        # must not thereby skip the check. This test replaces the pre-Task-11
        # `test_no_flag_writes_no_stamp`, which asserted the opposite (rc 0)
        # because no check depended on the flag then.
        vault = self._vault_file()
        rc, out = self._run(self._write_response(vault), vault, "")
        self.assertEqual(rc, 1)
        self.assertTrue(any("--stamp-session is required" in error for error in out["errors"]), out)
        self.assertFalse((Path(self._home) / ".claude" / "cache" / "progress-report-fresh").exists())

    def test_no_receipt_is_rejected_and_no_stamp_is_written(self) -> None:
        # [RED] the whole point of Task 11: a report whose goal step never ran.
        vault = self._vault_file()
        rc, out = self._run(self._write_response(vault), vault, "sess-noreceipt")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no goal-ledger activity recorded" in error for error in out["errors"]), out)
        # Self-describing abort: absolute path, named step, stated next action.
        message = next(e for e in out["errors"] if "no goal-ledger activity recorded" in e)
        self.assertIn("/home/pedro/.claude/skills/progress-report/SKILL.md", message)
        self.assertIn("Step 4b", message)
        self.assertFalse(self._stamp_path("sess-noreceipt").exists())

    def test_load_only_receipt_never_reached_a_decision(self) -> None:
        # [EDGE] the ledger was opened and nothing was ever decided.
        vault = self._vault_file()
        self._write_receipt("sess-loadonly", [{"cmd": "load", "exit": 0, "ts": self.ENTRY_TS}])
        rc, out = self._run(self._write_response(vault), vault, "sess-loadonly")
        self.assertEqual(rc, 1)
        self.assertTrue(any("never reached a decision" in error for error in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-loadonly").exists())

    def test_a_parse_extraction_alone_no_longer_satisfies_the_counts(self) -> None:
        # [EDGE] This test previously asserted the OPPOSITE — that a lone
        # `parse-extraction` entry was enough to accept a counts declaration.
        # It is not: `committed=<n>` is answered only by a `commit`, and letting
        # an extraction answer for it is how a single command over a
        # caller-authored file validated any counts at all. Both halves are now
        # required, so this fixture is a rejection.
        vault = self._vault_file()
        self._write_receipt("sess-parsed", [
            {"cmd": "load", "exit": 0, "project": self.PROJECT, "effect": None,
             "ts": self.ENTRY_TS},
            {"cmd": "parse-extraction", "exit": 0, "project": None, "effect": 1,
             "ts": self.ENTRY_TS}])
        rc, out = self._run(self._write_response(vault), vault, "sess-parsed")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no `commit` run recorded" in e for e in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-parsed").exists())

    def test_a_commit_alone_no_longer_satisfies_the_counts(self) -> None:
        # [EDGE] The mirror image, and the brief's stated edge case: matching
        # project and effect on the commit, but NO `parse-extraction` entry at
        # all. `proposed=<m>` describes a run that never happened.
        vault = self._vault_file()
        self._write_receipt("sess-commitonly", [
            {"cmd": "commit", "exit": 0, "project": self.PROJECT, "effect": 1,
             "ts": self.ENTRY_TS}])
        rc, out = self._run(self._write_response(vault), vault, "sess-commitonly")
        self.assertEqual(rc, 1)
        self.assertTrue(
            any("no `parse-extraction` run recorded" in e for e in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-commitonly").exists())

    def test_not_supplied_fails_even_with_a_valid_commit_receipt(self) -> None:
        # [EDGE] `not-supplied` is legal grammar that ALWAYS fails validation.
        vault = self._vault_file()
        self._write_receipt("sess-nosupply", [{"cmd": "commit", "exit": 0}])
        rc, out = self._run(self._write_response(vault, "not-supplied"), vault, "sess-nosupply")
        self.assertEqual(rc, 1)
        self.assertTrue(any("`not-supplied`" in error for error in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-nosupply").exists())

    def test_thin_session_with_no_receipt_is_rejected(self) -> None:
        # [RED] THE COUPLING PROOF. This test asserted `rc == 0` up to round 3:
        # a `thin-session` declaration passed with no artifact of any kind, which
        # made it the cheapest of the four accounting values and the one path a
        # drifting run could take for free. The skip now leaves its own receipt
        # entry, so its absence is a rejection like every other value's.
        vault = self._vault_stating_its_reason()
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-thin")
        self.assertEqual(rc, 1)
        message = next(e for e in out["errors"] if "thin-session" in e)
        self.assertIn("recorded no goals.py run at all", message)
        self.assertIn("'demo'", message, "the message names the project")
        self.assertFalse(self._stamp_path("sess-thin").exists())

    def test_ledger_error_requires_a_recorded_failure(self) -> None:
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-lederr", [{"cmd": "load", "exit": 3,
                                             "project": self.PROJECT, "effect": None,
                                             "ts": self.ENTRY_TS}])
        rc, out = self._run(self._write_response(vault, "ledger-error"), vault, "sess-lederr")
        self.assertEqual(rc, 0, out)

    def test_ledger_error_over_a_non_integer_exit_is_rejected(self) -> None:
        # An `exit` this reader cannot interpret as an integer is not evidence
        # of the failure `ledger-error` declares — fail closed rather than let
        # a bare `!= 0` inequality wave a string through.
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-strexit", [{"cmd": "load", "exit": "3",
                                              "project": self.PROJECT, "effect": None,
                                              "ts": self.ENTRY_TS}])
        rc, out = self._run(self._write_response(vault, "ledger-error"), vault, "sess-strexit")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no recorded goals.py run for project" in e for e in out["errors"]), out)

    def test_ledger_error_declared_over_a_clean_receipt_is_rejected(self) -> None:
        # `ledger-error` asserts goals.py exited non-zero. A load-only receipt
        # at exit 0 contradicts that, and accepting it would make the value a
        # free bypass of the decision requirement.
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-lieerr", [{"cmd": "load", "exit": 0,
                                             "project": self.PROJECT, "effect": None,
                                             "ts": self.ENTRY_TS}])
        rc, out = self._run(self._write_response(vault, "ledger-error"), vault, "sess-lieerr")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no recorded goals.py run for project" in e for e in out["errors"]), out)

    def test_ledger_error_with_no_receipt_at_all_is_rejected(self) -> None:
        vault = self._vault_stating_its_reason()
        rc, out = self._run(self._write_response(vault, "ledger-error"), vault, "sess-lednone")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no goal-ledger activity recorded" in e for e in out["errors"]), out)

    def test_truncated_receipt_is_a_defined_failure_not_a_traceback(self) -> None:
        vault = self._vault_file()
        self._run_dir.mkdir(parents=True, exist_ok=True)
        (self._run_dir / "sess-trunc.json").write_text('{"session": "sess-trunc", "entr',
                                                       encoding="utf-8")
        rc, out = self._run(self._write_response(vault), vault, "sess-trunc")
        self.assertEqual(rc, 1)
        self.assertTrue(any("is not valid JSON" in error for error in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-trunc").exists())

    def test_non_utf8_receipt_is_a_defined_failure(self) -> None:
        # UnicodeDecodeError is a ValueError, NOT an OSError. An OSError-only
        # handler here would surface as a traceback instead of a rejection.
        vault = self._vault_file()
        self._run_dir.mkdir(parents=True, exist_ok=True)
        (self._run_dir / "sess-binary.json").write_bytes(b'{"entries": [\xff\xfe]}')
        rc, out = self._run(self._write_response(vault), vault, "sess-binary")
        self.assertEqual(rc, 1)
        self.assertTrue(any("unreadable" in error for error in out["errors"]), out)

    def test_receipt_with_wrong_top_level_type_is_a_defined_failure(self) -> None:
        vault = self._vault_file()
        self._run_dir.mkdir(parents=True, exist_ok=True)
        (self._run_dir / "sess-list.json").write_text('["load"]', encoding="utf-8")
        rc, out = self._run(self._write_response(vault), vault, "sess-list")
        self.assertEqual(rc, 1)
        self.assertTrue(any("top level" in error for error in out["errors"]), out)

    def test_receipt_entries_of_the_wrong_type_are_a_defined_failure(self) -> None:
        vault = self._vault_file()
        self._write_receipt("sess-notlist", {"cmd": "commit"})
        rc, out = self._run(self._write_response(vault), vault, "sess-notlist")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no `entries` list" in error for error in out["errors"]), out)

    def test_non_dict_entries_are_ignored_not_crashed_on(self) -> None:
        vault = self._vault_file()
        self._write_receipt("sess-mixed", ["load", None] + self._clean_entries(1, 1))
        rc, out = self._run(self._write_response(vault), vault, "sess-mixed")
        self.assertEqual(rc, 0, out)

    # ---------------------------------------------------------------- CRITICAL 2
    # A recorded FAILURE to reach a decision is not a decision. Before this,
    # a single `parse-extraction` that aborted at exit 2 — one command, on a
    # file the caller writes itself, no ledger and no gate involved — validated
    # a block declaring `committed=7 proposed=9`.

    def test_counts_over_a_failed_decision_entry_are_rejected(self) -> None:
        # [RED] the exact reproduction: one `parse-extraction` at exit 2 on a
        # proposal-less block, declaring seven commits and nine proposals.
        vault = self._vault_file()
        self._write_receipt("sess-failparse", [{"cmd": "parse-extraction", "exit": 2,
                                                "ts": self.ENTRY_TS}])
        rc, out = self._run(
            self._write_response(vault, "committed=7 proposed=9"), vault, "sess-failparse")
        self.assertEqual(rc, 1)
        self.assertTrue(any("every recorded decision run failed" in e for e in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-failparse").exists())

    def test_counts_accept_a_failure_followed_by_a_success(self) -> None:
        # The predicate is "there EXISTS a decision entry at exit 0", not "the
        # latest decision entry succeeded". A run that retried after an abort
        # is honest and must not lose.
        vault = self._vault_file()
        self._write_receipt("sess-retry", [{"cmd": "parse-extraction", "exit": 2,
                                            "project": None, "effect": None}]
                            + self._clean_entries(3, 4))
        rc, out = self._run(
            self._write_response(vault, "committed=3 proposed=4"), vault, "sess-retry")
        self.assertEqual(rc, 0, out)

    def test_counts_over_a_non_integer_exit_are_rejected(self) -> None:
        # Same reason the `ledger-error` branch uses `isinstance(..., int)`: an
        # `exit` this reader cannot interpret as an integer is not evidence of
        # the clean decision the counts declare. `"0"` == 0 is False in Python,
        # but a bare `== 0` over a bool would still be a hole — fail closed.
        vault = self._vault_file()
        self._write_receipt("sess-strok", [{"cmd": "commit", "exit": "0", "ts": self.ENTRY_TS}])
        rc, out = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-strok")
        self.assertEqual(rc, 1)
        self.assertTrue(any("every recorded decision run failed" in e for e in out["errors"]), out)

    def test_no_decision_entry_and_a_failed_one_give_different_errors(self) -> None:
        # The two counts-path rejections must be distinguishable: "never got
        # there" and "got there and failed" call for different next actions.
        vault = self._vault_file()
        self._write_receipt("sess-msg-a", [{"cmd": "load", "exit": 0, "ts": self.ENTRY_TS}])
        self._write_receipt("sess-msg-b", [{"cmd": "commit", "exit": 4, "ts": self.ENTRY_TS}])
        _, none_at_all = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-msg-a")
        _, failed = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-msg-b")
        self.assertTrue(any("never reached a decision" in e for e in none_at_all["errors"]))
        self.assertFalse(any("never reached a decision" in e for e in failed["errors"]), failed)
        self.assertTrue(any("every recorded decision run failed" in e for e in failed["errors"]))

    # ---------------------------------------------------------------- CRITICAL 1
    # Receipts are per-session and append-only across runs, so without a
    # freshness floor ONE receipt satisfies the check for every later run of
    # that session. The floor is the previous run's freshness stamp: the goal
    # step must have run since the last accepted report.

    def test_a_stale_receipt_no_longer_satisfies_a_later_run(self) -> None:
        # [RED] run 1 was accepted at 18:00 and stamped. Run 2 skips the goal
        # step entirely; the only receipt entries predate the stamp.
        vault = self._vault_file()
        self._write_stamp("sess-stale", "2026-08-13T18:00:00Z")
        self._write_receipt("sess-stale", [{"cmd": "commit", "exit": 0,
                                            "ts": "2026-08-13T17:59:00Z"}])
        rc, out = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-stale")
        self.assertEqual(rc, 1)
        self.assertTrue(
            any("since this run's freshness floor" in e for e in out["errors"]), out)
        # The previous run's stamp must survive untouched — a rejected run may
        # not refresh the very floor it failed to clear.
        self.assertEqual(
            json.loads(self._stamp_path("sess-stale").read_text())["ts"], "2026-08-13T18:00:00Z")

    def test_a_dry_run_receipt_does_not_inoculate_a_later_run(self) -> None:
        # The reported bypass, in its own words: `--dry-run` reaches both `load`
        # and `parse-extraction`, so a dry run leaves a decision entry behind.
        # Once a report has been accepted, those entries are below the floor.
        vault = self._vault_file()
        self._write_stamp("sess-dry", "2026-08-13T18:30:00Z")
        self._write_receipt("sess-dry", [{"cmd": "load", "exit": 0, "ts": "2026-08-13T18:00:00Z"},
                                         {"cmd": "parse-extraction", "exit": 0,
                                          "ts": "2026-08-13T18:00:01Z"}])
        rc, out = self._run(
            self._write_response(vault, "committed=0 proposed=0"), vault, "sess-dry")
        self.assertEqual(rc, 1)
        self.assertTrue(any("since this run's freshness floor" in e for e in out["errors"]), out)

    def test_a_second_run_passes_on_a_receipt_newer_than_the_stamp(self) -> None:
        # [GREEN] the honest second run: the goal step ran again after the last
        # accepted report, so the stamp advances.
        vault = self._vault_file()
        self._write_stamp("sess-fresh", "2026-08-13T18:00:00Z")
        self._write_receipt("sess-fresh", self._clean_entries(2, 2, ts="2026-08-13T18:00:01Z"))
        rc, out = self._run(
            self._write_response(vault, "committed=2 proposed=2"), vault, "sess-fresh")
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(
            json.loads(self._stamp_path("sess-fresh").read_text())["ts"], "2026-08-13T18:00:00Z")

    def test_a_sessions_first_report_is_held_to_this_runs_start_marker(self) -> None:
        # [RED] THE COUPLING PROOF for the run-start marker, and the defect this
        # round exists for. This test asserted `rc == 0` up to round 4, under the
        # name `test_first_run_of_a_session_is_unaffected_by_the_freshness_floor`
        # and the reasoning "no stamp means no accepted report yet, so there is
        # no floor to clear". That reasoning was sound and its consequence was
        # not: the receipt is per-session and append-only ACROSS runs, so on a
        # session's first report an entry from ANY earlier run of that session —
        # a dry run's included — was accepted as evidence for this one. The sixth
        # appearance of "present somewhere" standing in for "present for THIS
        # run", on the one run that had nothing behind it to compare against.
        #
        # No stamp, and entries written BEFORE this run's marker. Nothing about
        # the counts is wrong; it is the age of the corroboration that is.
        vault = self._vault_file()
        self._write_receipt("sess-first",
                            self._clean_entries(1, 1, ts="2026-08-13T17:00:00Z"))
        rc, out = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-first")
        self.assertEqual(rc, 1, out)
        message = next(e for e in out["errors"] if "freshness floor" in e)
        # WHICH error fired, not the exit code: this command accumulates errors,
        # and the counts path has four other rejections that also exit 1. The
        # floor must be the marker's, since there is no stamp to supply one.
        self.assertIn("no goal-ledger activity recorded", message)
        self.assertIn(self.DEFAULT_MARKER_TS, message)
        self.assertIn("from this run's start marker", message)
        self.assertFalse(self._stamp_path("sess-first").exists())

    def test_a_first_report_passes_on_entries_that_postdate_its_marker(self) -> None:
        # [GREEN] The other half of the same fixture, and what keeps the test
        # above from being satisfied by a validator that simply rejects every
        # first report: the same session, the same absent stamp, the same
        # declaration — with the goal step having run after this run started.
        vault = self._vault_file()
        self._write_receipt("sess-firstok", self._clean_entries(1, 1))
        rc, out = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-firstok")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self._stamp_path("sess-firstok").is_file())

    def test_an_entry_with_no_timestamp_cannot_clear_the_floor(self) -> None:
        # Fail closed: an entry whose age is unknowable is not proof it ran
        # after the last accepted report.
        vault = self._vault_file()
        self._write_stamp("sess-nots", "2026-08-13T18:00:00Z")
        self._write_receipt("sess-nots", [{"cmd": "commit", "exit": 0}])
        rc, out = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-nots")
        self.assertEqual(rc, 1)
        self.assertTrue(any("since this run's freshness floor" in e for e in out["errors"]), out)

    def test_an_entry_at_exactly_the_stamp_second_does_not_clear_the_floor(self) -> None:
        # Strictly newer. Two full report runs inside one second is not a real
        # workflow; a stale entry sharing the stamp's second is.
        vault = self._vault_file()
        self._write_stamp("sess-eq", "2026-08-13T18:00:00Z")
        self._write_receipt("sess-eq", [{"cmd": "commit", "exit": 0,
                                         "ts": "2026-08-13T18:00:00Z"}])
        rc, out = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-eq")
        self.assertEqual(rc, 1)
        self.assertTrue(any("since this run's freshness floor" in e for e in out["errors"]), out)

    def test_freshness_and_exit_code_are_one_predicate(self) -> None:
        # The cross-product. Each half passing alone is exactly how the two
        # criticals leaked together: a FRESH failed decision and a STALE clean
        # one must both be rejected, and only fresh-and-clean may pass.
        cases = {
            ("fresh", 0): 0,
            ("fresh", 2): 1,
            ("stale", 0): 1,
            ("stale", 2): 1,
        }
        for (age, exit_code), expected in cases.items():
            with self.subTest(age=age, exit_code=exit_code):
                session = f"sess-x-{age}-{exit_code}"
                vault = self._vault_file()
                self._write_stamp(session, "2026-08-13T18:00:00Z")
                ts = "2026-08-13T18:00:01Z" if age == "fresh" else "2026-08-13T17:00:00Z"
                entries = self._clean_entries(1, 1, ts=ts)
                for entry in entries:
                    entry["exit"] = exit_code
                self._write_receipt(session, entries)
                rc, out = self._run(
                    self._write_response(vault, "committed=1 proposed=1"), vault, session)
                self.assertEqual(rc, expected, out)

    def test_ledger_error_is_held_to_the_same_floor(self) -> None:
        # `ledger-error` reads the same receipt, so it inherits the same bypass
        # unless it inherits the same floor.
        vault = self._vault_stating_its_reason()
        self._write_stamp("sess-lestale", "2026-08-13T18:00:00Z")
        self._write_receipt("sess-lestale", [{"cmd": "load", "exit": 3,
                                              "ts": "2026-08-13T17:00:00Z",
                                              "project": self.PROJECT, "effect": None}])
        rc, out = self._run(
            self._write_response(vault, "ledger-error"), vault, "sess-lestale")
        self.assertEqual(rc, 1)
        self.assertTrue(any("since this run's freshness floor" in e for e in out["errors"]), out)

    def test_an_unparseable_stamp_timestamp_fails_closed(self) -> None:
        # A floor this reader cannot evaluate is not a floor. Rejecting says so
        # and names the file to delete, exactly as the corrupt-receipt path does.
        vault = self._vault_file()
        self._write_stamp("sess-badts", "not-a-timestamp")
        self._write_receipt("sess-badts", [{"cmd": "commit", "exit": 0,
                                            "ts": "2026-08-13T18:00:01Z"}])
        rc, out = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-badts")
        self.assertEqual(rc, 1)
        self.assertTrue(any("freshness stamp" in e for e in out["errors"]), out)

    def test_a_corrupt_stamp_file_fails_closed(self) -> None:
        vault = self._vault_file()
        stamp_dir = Path(self._home) / ".claude" / "cache" / "progress-report-fresh"
        stamp_dir.mkdir(parents=True, exist_ok=True)
        (stamp_dir / "sess-badstamp.json").write_text('{"ts": "2026-08', encoding="utf-8")
        self._write_receipt("sess-badstamp", [{"cmd": "commit", "exit": 0,
                                               "ts": "2026-08-13T18:00:01Z"}])
        rc, out = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-badstamp")
        self.assertEqual(rc, 1)
        self.assertTrue(any("freshness stamp" in e for e in out["errors"]), out)

    def test_a_stamp_with_no_ts_field_fails_closed(self) -> None:
        vault = self._vault_file()
        self._write_stamp("sess-nostampts", None)
        self._write_receipt("sess-nostampts", [{"cmd": "commit", "exit": 0,
                                                "ts": "2026-08-13T18:00:01Z"}])
        rc, out = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-nostampts")
        self.assertEqual(rc, 1)
        self.assertTrue(any("freshness stamp" in e for e in out["errors"]), out)

    # --------------------------------------------------------------- IMPORTANT 4
    # "Either the ledger recorded activity, or the report says on its face why
    # it did not." The second half was prose until now: `goal_accounting` lives
    # only in the ephemeral result block, and on the `ledger-error` path the
    # builder omits the Goals section entirely — which is precisely the
    # 2026-08-13 artifact this task exists for.

    def test_ledger_error_report_must_state_the_reason_in_its_body(self) -> None:
        # [RED] a report whose body says nothing about the missing goals.
        vault = self._vault_file("## 19:00 — session\n\n### Resume Here\n- **Branch:** main\n")
        self._write_receipt("sess-silent", [{"cmd": "load", "exit": 3}])
        rc, out = self._run(self._write_response(vault, "ledger-error"), vault, "sess-silent")
        self.assertEqual(rc, 1)
        self.assertTrue(any("does not state why" in e for e in out["errors"]), out)
        # Self-describing: names the literal to render and where it is specified.
        message = next(e for e in out["errors"] if "does not state why" in e)
        self.assertIn(validate_report.REPORT_GOAL_REASON_MARKER, message)
        self.assertIn("/home/pedro/.claude/agents/progress-report-builder.md", message)
        self.assertFalse(self._stamp_path("sess-silent").exists())

    def test_thin_session_report_must_state_the_reason_in_its_body(self) -> None:
        # The receipt is deliberately VALID here so the only guard that can fire
        # is the stated-reason one this test is named for. Without it the run
        # would also be rejected for the missing thin entry, and the assertion
        # below would pass while proving nothing about the report body.
        vault = self._vault_file("## 19:00 — session\n\n### Resume Here\n- **Branch:** main\n")
        self._write_receipt("sess-thinsilent", [self._thin_entry()])
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-thinsilent")
        self.assertEqual(rc, 1)
        self.assertTrue(any("does not state why" in e for e in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-thinsilent").exists())

    def test_a_counts_report_needs_no_stated_reason(self) -> None:
        # The reason line answers "why are there no goals here". A run that
        # reached a decision has goals to show and owes no such line.
        vault = self._vault_file()
        self._write_receipt("sess-counts", self._clean_entries(1, 1))
        rc, out = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-counts")
        self.assertEqual(rc, 0, out)

    def test_the_reason_is_read_from_the_local_mirror_when_no_vault_target(self) -> None:
        # `--out` aside, a caller may pass only a local target. The check must
        # follow the report, not one specific flag.
        local = self._vault_stating_its_reason()
        self._write_marker("sess-localonly")
        self._write_receipt("sess-localonly", [self._thin_entry()])
        response = self._write_response(local, "thin-session")
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = validate_report.cmd_result(
                self._args(response, "", "sess-localonly", local=local))
        self.assertEqual(rc, 0, buffer.getvalue())

    def test_an_unreadable_report_body_fails_closed(self) -> None:
        # The reason check may never pass by being unable to look. Receipt kept
        # valid so the unreadable body is the only thing that can reject this.
        vault = self._vault_file()
        self._write_receipt("sess-noread", [self._thin_entry()])
        os.chmod(vault, 0)
        self.addCleanup(os.chmod, vault, 0o600)
        if os.access(vault, os.R_OK):  # running as root: the premise cannot hold
            self.skipTest("cannot make a file unreadable for this user")
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-noread")
        self.assertEqual(rc, 1)
        self.assertTrue(any("could not be read" in e for e in out["errors"]), out)


class TestRunStartMarkerIsHalfTheFloor(GoalReceiptHarness):
    """The run-start marker: every run carries its own freshness floor.

    The freshness stamp answers "since the last ACCEPTED REPORT" and a session's
    first report has none, so for one round that run had no floor at all and an
    entry from any earlier run of the session satisfied it. The marker answers
    "since THIS RUN started", which every run can answer. The floor is the later
    of the two, and each test below pins one direction of that `max` — a
    one-sided implementation passes half of them.

    Every test here gets its own session id and its own fixture. Two guards can
    reject the same input on this path (the stamp's floor and the marker's), and
    a case that lets the wrong one fire is a pass for the wrong reason — the
    shape a round-3 probe in this project produced by running a control first.
    """

    def test_an_entry_postdating_the_stamp_but_predating_the_marker_is_rejected(self) -> None:
        # [EDGE] THE REASON `max` IS THE RULE. Run 1 was accepted at 18:00 and
        # stamped. The goal step then ran at 18:10 — after that stamp, so the
        # stamp alone lets it through — and run 2 started at 18:30. Those entries
        # belong to whatever happened between the two runs, not to run 2.
        vault = self._vault_file()
        self._write_stamp("sess-mk-a", "2026-08-13T18:00:00Z")
        self._write_receipt("sess-mk-a", self._clean_entries(1, 1, ts="2026-08-13T18:10:00Z"))
        rc, out = self._run(self._write_response(vault, "committed=1 proposed=1"),
                            vault, "sess-mk-a", marker="2026-08-13T18:30:00Z")
        self.assertEqual(rc, 1, out)
        message = next(e for e in out["errors"] if "freshness floor" in e)
        self.assertIn("no goal-ledger activity recorded", message)
        self.assertIn("2026-08-13T18:30:00Z", message)
        self.assertIn("from this run's start marker", message)
        # Run 1's stamp is still on disk; a rejected run may not advance it.
        self.assertEqual(
            json.loads(self._stamp_path("sess-mk-a").read_text())["ts"], "2026-08-13T18:00:00Z")

    def test_an_entry_postdating_the_marker_but_predating_the_stamp_is_rejected(self) -> None:
        # [EDGE] The other direction, and without it a validator that simply
        # REPLACED the stamp floor with the marker floor passes everything else
        # in this class. This run started at 17:00 and its goal step ran at
        # 17:30 — but the last accepted report was stamped at 19:00, so that
        # activity is older than a report already on disk.
        vault = self._vault_file()
        self._write_stamp("sess-mk-b", "2026-08-13T19:00:00Z")
        self._write_receipt("sess-mk-b", self._clean_entries(1, 1, ts="2026-08-13T17:30:00Z"))
        rc, out = self._run(self._write_response(vault, "committed=1 proposed=1"),
                            vault, "sess-mk-b", marker="2026-08-13T17:00:00Z")
        self.assertEqual(rc, 1, out)
        message = next(e for e in out["errors"] if "freshness floor" in e)
        self.assertIn("2026-08-13T19:00:00Z", message)
        self.assertIn("from the last accepted report's freshness stamp", message)
        # A rejected run may not refresh the floor it failed to clear.
        self.assertEqual(
            json.loads(self._stamp_path("sess-mk-b").read_text())["ts"], "2026-08-13T19:00:00Z")

    def test_a_second_report_passes_when_entries_postdate_both_floors(self) -> None:
        # [GREEN] The ordinary honest second run, with both files present: the
        # goal step ran after the last accepted report AND after this run began.
        vault = self._vault_file()
        self._write_stamp("sess-mk-c", "2026-08-13T18:00:00Z")
        self._write_receipt("sess-mk-c", self._clean_entries(2, 2, ts="2026-08-13T18:40:00Z"))
        rc, out = self._run(self._write_response(vault, "committed=2 proposed=2"),
                            vault, "sess-mk-c", marker="2026-08-13T18:30:00Z")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self._marker_path("sess-mk-c").is_file())
        self.assertNotEqual(
            json.loads(self._stamp_path("sess-mk-c").read_text())["ts"], "2026-08-13T18:00:00Z")

    def test_a_thin_first_report_is_held_to_this_runs_marker(self) -> None:
        # [RED] The thin-session instance of the same hole, and the residual the
        # controller documented after round 4: a stale `thin-session` entry whose
        # metrics DO satisfy the condition was a complete pass on a session's
        # first report, because a thin declaration has no declared number to
        # check and no floor stood between it and this run. The entry is clean,
        # this project's, and genuinely thin — only its age disqualifies it.
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-mk-thin", [self._thin_entry(ts="2026-08-13T17:00:00Z")])
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-mk-thin")
        self.assertEqual(rc, 1, out)
        message = next(e for e in out["errors"] if "thin-session" in e)
        self.assertIn("recorded nothing since this run's freshness floor", message)
        self.assertIn("from this run's start marker", message)
        self.assertFalse(self._stamp_path("sess-mk-thin").exists())

    def test_a_thin_first_report_passes_on_an_entry_that_postdates_its_marker(self) -> None:
        # [GREEN] The same fixture with an in-run entry, so the test above cannot
        # be satisfied by rejecting every thin first report.
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-mk-thinok", [self._thin_entry()])
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-mk-thinok")
        self.assertEqual(rc, 0, out)

    def test_an_entry_in_the_markers_own_second_clears_the_marker_floor(self) -> None:
        # [EDGE] THE BOUNDARY, and it goes the OPPOSITE way from the stamp's.
        # Step 2 writes the marker, Step 4b runs the goal step, and on a short
        # session both can land inside one second — the timestamps carry second
        # precision only. A strict `>` here rejects a run that did everything
        # right, on exactly the thin sessions this skill is cheapest to run on.
        # Named against its mirror image,
        # `test_an_entry_at_exactly_the_stamp_second_does_not_clear_the_floor`:
        # the stamp is written at a run's END, the marker at its START, so an
        # entry sharing the stamp's second belongs to the previous run and one
        # sharing the marker's second belongs to this one.
        vault = self._vault_file()
        self._write_receipt("sess-mk-eq", self._clean_entries(1, 1, ts="2026-08-13T18:30:00Z"))
        rc, out = self._run(self._write_response(vault, "committed=1 proposed=1"),
                            vault, "sess-mk-eq", marker="2026-08-13T18:30:00Z")
        self.assertEqual(rc, 0, out)

    def test_the_stamp_boundary_stays_exclusive_when_the_marker_is_inclusive(self) -> None:
        # [EDGE] The other half of the same decision, and the reason it is stated
        # as two predicates rather than one floor: relaxing the comparison for
        # the marker must not relax it for the stamp. Entry exactly at the
        # stamp's second, marker older — the stamp still rejects.
        vault = self._vault_file()
        self._write_stamp("sess-mk-eqstamp", "2026-08-13T18:30:00Z")
        self._write_receipt("sess-mk-eqstamp",
                            self._clean_entries(1, 1, ts="2026-08-13T18:30:00Z"))
        rc, out = self._run(self._write_response(vault, "committed=1 proposed=1"),
                            vault, "sess-mk-eqstamp", marker="2026-08-13T18:00:00Z")
        self.assertEqual(rc, 1, out)
        message = next(e for e in out["errors"] if "freshness floor" in e)
        self.assertIn("from the last accepted report's freshness stamp", message)

    def test_an_entry_one_second_before_the_marker_is_still_rejected(self) -> None:
        # [EDGE] Inclusive at the boundary is not the same as open below it. One
        # second earlier than the marker is an earlier run's entry and must not
        # count — without this, "inclusive" could quietly become "off by one".
        vault = self._vault_file()
        self._write_receipt("sess-mk-lt", self._clean_entries(1, 1, ts="2026-08-13T18:29:59Z"))
        rc, out = self._run(self._write_response(vault, "committed=1 proposed=1"),
                            vault, "sess-mk-lt", marker="2026-08-13T18:30:00Z")
        self.assertEqual(rc, 1, out)
        message = next(e for e in out["errors"] if "freshness floor" in e)
        self.assertIn("from this run's start marker", message)

    def test_a_missing_marker_fails_closed(self) -> None:
        # [EDGE] FAIL CLOSED. If a missing marker meant "no floor", the bypass
        # would be "do not write the marker" — strictly worse than the hole this
        # closes, since it would hand every run the exemption that used to belong
        # only to a session's first report. The receipt here is otherwise
        # impeccable: fresh entries, right project, matching counts.
        vault = self._vault_file()
        self._write_receipt("sess-mk-none", self._clean_entries(1, 1))
        rc, out = self._run(self._write_response(vault, "committed=1 proposed=1"),
                            vault, "sess-mk-none", marker=None)
        self.assertEqual(rc, 1, out)
        message = next(e for e in out["errors"] if "run-start marker" in e)
        self.assertIn("wrote no run-start marker", message)
        self.assertIn(str(self._marker_path("sess-mk-none")), message)
        # Self-describing abort: absolute path, named step, stated next action.
        self.assertIn("/home/pedro/.claude/skills/progress-report/SKILL.md", message)
        self.assertIn("Step 2", message)
        self.assertFalse(self._stamp_path("sess-mk-none").exists())

    def test_a_marker_with_no_readable_ts_takes_the_problem_path(self) -> None:
        # [EDGE] A floor this reader cannot evaluate is not a floor — the same
        # treatment an unreadable STAMP gets, asserted by message rather than by
        # exit code, because the fail-closed branch above exits 1 as well and the
        # two call for different next actions.
        vault = self._vault_file()
        self._write_receipt("sess-mk-nots", self._clean_entries(1, 1))
        self._write_marker("sess-mk-nots", ts=None)
        rc, out = self._run(self._write_response(vault, "committed=1 proposed=1"),
                            vault, "sess-mk-nots", marker=None)
        self.assertEqual(rc, 1, out)
        message = next(e for e in out["errors"] if "run-start marker" in e)
        self.assertIn("carries no readable `ts`", message)
        self.assertNotIn("wrote no run-start marker", message)
        self.assertFalse(self._stamp_path("sess-mk-nots").exists())

    def test_an_unparseable_marker_timestamp_takes_the_problem_path(self) -> None:
        # [EDGE] The other unreadable shape: a `ts` that is present and is not a
        # timestamp. Same branch, and it must not be read as "no floor".
        vault = self._vault_file()
        self._write_receipt("sess-mk-badts", self._clean_entries(1, 1))
        rc, out = self._run(self._write_response(vault, "committed=1 proposed=1"),
                            vault, "sess-mk-badts", marker="not-a-timestamp")
        self.assertEqual(rc, 1, out)
        message = next(e for e in out["errors"] if "run-start marker" in e)
        self.assertIn("carries no readable `ts`", message)
        self.assertFalse(self._stamp_path("sess-mk-badts").exists())

    def test_a_corrupt_marker_file_takes_the_problem_path(self) -> None:
        # [EDGE] Truncated JSON, the shape a killed writer would leave. A defined
        # rejection, never a traceback out of a validation run.
        vault = self._vault_file()
        self._write_receipt("sess-mk-trunc", self._clean_entries(1, 1))
        marker = self._marker_path("sess-mk-trunc")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"ts": "2026-08', encoding="utf-8")
        rc, out = self._run(self._write_response(vault, "committed=1 proposed=1"),
                            vault, "sess-mk-trunc", marker=None)
        self.assertEqual(rc, 1, out)
        message = next(e for e in out["errors"] if "run-start marker" in e)
        self.assertIn("is not valid JSON", message)

    def test_no_marker_message_tells_the_reader_to_delete_or_bypass_it(self) -> None:
        # Round 2's rule binds on this new file too, and it binds hardest here:
        # the marker's own absence is a rejection, so a message that said
        # "delete the marker" would be printing the bypass it just refused.
        vault = self._vault_file()
        cases = {"sess-mk-d1": None, "sess-mk-d2": "not-a-timestamp"}
        for session, marker in cases.items():
            with self.subTest(session=session):
                self._write_receipt(session, self._clean_entries(1, 1))
                if marker is not None:
                    self._write_marker(session, marker)
                rc, out = self._run(self._write_response(vault, "committed=1 proposed=1"),
                                    vault, session, marker=None)
                self.assertEqual(rc, 1)
                message = next(e for e in out["errors"] if "run-start marker" in e)
                for forbidden in ("delete", "Delete", "remove the", "rm "):
                    self.assertNotIn(forbidden, message)

    def test_the_marker_command_rejects_a_traversing_session_id(self) -> None:
        # The id becomes a filename here exactly as it does for the stamp and the
        # receipt, so it gets the same check. Asserted by the artifact, not only
        # by the exit code: a marker written outside its directory is one the
        # reader never finds, which reads as a run that never marked its start.
        import argparse
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = validate_report.cmd_run_start(argparse.Namespace(session="../evil"))
        self.assertEqual(rc, 1)
        self.assertIn("invalid --session id", buffer.getvalue())
        self.assertFalse((Path(self._home) / ".claude" / "cache" / "evil.json").exists())

    def test_the_real_marker_command_writes_a_floor_this_validator_accepts(self) -> None:
        # THE SEAM. Every other test in this class writes the marker by hand, so
        # all of them would keep passing if the writer and the reader disagreed
        # about where it lives or what it holds — a marker that exists and is
        # never found is indistinguishable from a run that never wrote one, and
        # this file's own history includes a stamp leaking into the live cache
        # for exactly that class of reason. Runs the REAL subcommand into the
        # harness's own HOME and validates against what it actually wrote.
        import subprocess
        env = dict(os.environ, HOME=self._home, GOALS_RUN_DIR=str(self._run_dir))
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "validate_report.py"), "run-start",
             "--session", "sess-mk-seam"],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        marker = self._marker_path("sess-mk-seam")
        self.assertEqual(json.loads(proc.stdout)["marker"], str(marker))
        self.assertTrue(marker.is_file())
        # The entries must postdate a marker stamped from the wall clock, so the
        # fixture cannot use a fixed time here — it reads the marker back.
        written = json.loads(marker.read_text())["ts"]
        self.assertIsNotNone(validate_report._parse_iso_z(written))
        later = datetime.strptime(written, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc) + timedelta(seconds=1)
        vault = self._vault_file()
        self._write_receipt("sess-mk-seam", self._clean_entries(
            1, 1, ts=later.strftime("%Y-%m-%dT%H:%M:%SZ")))
        rc, out = self._run(self._write_response(vault, "committed=1 proposed=1"),
                            vault, "sess-mk-seam", marker=None)
        self.assertEqual(rc, 0, out)

    def test_the_marker_command_overwrites_rather_than_appends(self) -> None:
        # [EDGE] Run twice in one session, the marker means "this run started
        # at", so the second invocation REPLACES the first. Asserted on what is
        # observable: one file in the directory holding one object with one `ts`,
        # and a second timestamp not earlier than the first. Deliberately not
        # `ts2 > ts1` — the stamp is second-precision and produced inside the
        # command, so two back-to-back runs legitimately record the same second
        # and a strict comparison would fail intermittently for no defect.
        import subprocess
        env = dict(os.environ, HOME=self._home)
        argv = [sys.executable, str(SCRIPTS / "validate_report.py"), "run-start",
                "--session", "sess-mk-twice"]
        first = subprocess.run(argv, capture_output=True, text=True, env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        ts1 = json.loads(self._marker_path("sess-mk-twice").read_text())["ts"]
        second = subprocess.run(argv, capture_output=True, text=True, env=env)
        self.assertEqual(second.returncode, 0, second.stderr)
        payload = json.loads(self._marker_path("sess-mk-twice").read_text())
        self.assertEqual(list(payload), ["ts"], "the marker holds one `ts`, not a history")
        self.assertGreaterEqual(payload["ts"], ts1)
        # One file, and no temporary left behind: `os.replace` renames onto the
        # marker, so a second file here would mean an interrupted or appending
        # writer.
        self.assertEqual([p.name for p in self._marker_path("sess-mk-twice").parent.iterdir()],
                         ["sess-mk-twice.json"])

    def test_an_earlier_runs_marker_is_replaced_so_its_entries_stop_counting(self) -> None:
        # The property the overwrite exists for, stated as behavior rather than
        # as file contents: run 1 marked its start and ran the goal step; run 2
        # marked its own start and did not. Run 1's entries are below run 2's
        # floor even though no report was ever accepted in between — which is the
        # dry-run residual, killed without a stamp being involved at all.
        vault = self._vault_file()
        self._write_receipt("sess-mk-prev", self._clean_entries(1, 1, ts="2026-08-13T18:00:00Z"))
        rc, out = self._run(self._write_response(vault, "committed=1 proposed=1"),
                            vault, "sess-mk-prev", marker="2026-08-13T17:00:00Z")
        self.assertEqual(rc, 0, f"run 1 marked its start before its goal step: {out}")
        self._stamp_path("sess-mk-prev").unlink()  # isolate: no accepted report behind run 2
        rc, out = self._run(self._write_response(vault, "committed=1 proposed=1"),
                            vault, "sess-mk-prev", marker="2026-08-13T19:00:00Z")
        self.assertEqual(rc, 1, out)
        message = next(e for e in out["errors"] if "freshness floor" in e)
        self.assertIn("2026-08-13T19:00:00Z", message)
        self.assertIn("from this run's start marker", message)


GOAL_REPORT = """---
session_uuid: sess-a
session_date: 2026-08-12
branch: main
project: /home/pedro/Projects/Demo
thin_session: false
linked_plan: none
linked_gsd_phase: none
session_window: 18:00-19:00
tool_calls: 12
files_modified: 2
---

# Session Progress Report — 2026-08-12

## 19:00 — Wired the goal ledger

### Resume Here
- **Branch:** main  |  **Phase:** mid-implementation  |  **Plan:** none
- **Pick up at:** finish the render subcommand
- **Active blocker:** none
- **Reopen first:** `scripts/goals.py`
- **Goals:** 1/2 done  |  now: Wire the goal ledger

### Goals
- [x] Rebuild the session ledger  (approved)
- [ ] Wire the goal ledger  (approved)

### Project Overview
**Main goal:** wire the goal ledger

**Current State:** mid-implementation — 1/2 goals done. no blocker

### Progress & Execution
**What changed:**
- Added render_markdown — Edit(scripts/goals.py) — verified

**Planned vs actual:**
- Advanced: Wire the goal ledger — 2 files, 31 tests passing
- Untouched: none
- Outside any goal: `README.md`

### Technical Status
**Modified:**
- `scripts/goals.py` — 3 edits

**Working on (read, not modified):**
- none

### Session Metrics
- Window: 18:00-19:00 (1h)
- Tool calls: 12 — top: Edit×6, Bash×4, Read×2
- Files: 2 modified, 1 read  |  Agents dispatched: 1
- Errors: 0  |  User turns: 4

### Confidence
Report quality: HIGH — explicit goal and concrete actions.
"""


class TestReceiptEffectAndProject(GoalReceiptHarness):
    """Task 11 round 2: the receipt records EFFECT and PROJECT, not invocation.

    What these pin, stated no wider than it is: the goal step ran, for this
    session, since the last accepted report, against THIS project, and produced
    the effect the report declares. Nothing here establishes that a human was
    asked anything, and a forger who reproduces the whole per-project cycle with
    matching counts still passes. At that point they have run the goal step,
    which is the property the feature actually wants.

    `thin-session` was described here as "caller-declared with no artifact" for
    two rounds. That was wrong twice over — it always required the stated-reason
    line in the durable report, and its condition always had a deterministic
    definition — and round 4 closed the part that was real: the declaration is
    now checked against the digest metrics that define it. See
    `TestThinSessionIsCheckedAgainstItsMetrics`.
    """

    def test_the_red_teamers_zero_effect_forgery_is_rejected(self) -> None:
        # [RED] Verbatim: `goals.py commit` over a caller-authored
        # `{"decisions": []}` exits 0 and records `effect: 0`. Before this
        # change that entry validated a block declaring any counts at all.
        vault = self._vault_file()
        self._write_receipt("sess-forge", [
            {"cmd": "commit", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": self.PROJECT, "effect": 0}])
        rc, out = self._run(
            self._write_response(vault, "committed=3 proposed=4"), vault, "sess-forge")
        self.assertEqual(rc, 1)
        # Assert WHICH error fired: this command accumulates errors, so a
        # code-only assertion passes for the wrong reason.
        self.assertTrue(any("committed=3" in e and "applied 3 decision(s)" in e
                            for e in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-forge").exists())

    def test_a_commit_for_another_project_does_not_validate_this_report(self) -> None:
        # [RED] FATAL 1. `PROJECT` is re-resolved from cwd on every invocation,
        # so one session touching alpha then beta writes ONE receipt covering
        # both. Alpha's entry must not answer for beta's report.
        vault = self._vault_file()
        self._write_receipt("sess-ab", [
            {"cmd": "commit", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": "alpha", "effect": 1},
            {"cmd": "parse-extraction", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": None, "effect": 1}])
        rc, out = self._run(self._write_response(vault), vault, "sess-ab", project="beta")
        self.assertEqual(rc, 1)
        self.assertTrue(any("'beta'" in e and "no `commit` run recorded" in e
                            for e in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-ab").exists())

    def test_the_same_receipt_validates_its_own_project(self) -> None:
        # [GREEN] The other polarity of the test above, from the identical
        # fixture: the project is what discriminates, not anything incidental.
        vault = self._vault_file()
        self._write_receipt("sess-ab2", [
            {"cmd": "commit", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": "alpha", "effect": 1},
            {"cmd": "parse-extraction", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": "alpha", "effect": 1}])
        rc, out = self._run(self._write_response(vault), vault, "sess-ab2", project="alpha")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self._stamp_path("sess-ab2").is_file())

    def test_the_project_is_matched_on_its_slug_not_its_raw_string(self) -> None:
        # [EDGE] The receipt stores the slug. A caller passing the project NAME
        # must still match, and must do so through goals.py's own `slugify` —
        # two derivations that agree today and drift later is a defect this
        # project has already shipped once.
        self.assertEqual(validate_report.project_slug("Demo Project"), "demo-project")
        vault = self._vault_file()
        self._write_receipt("sess-slug", self._clean_entries(1, 1, project="demo-project"))
        rc, out = self._run(self._write_response(vault), vault, "sess-slug",
                            project="Demo Project")
        self.assertEqual(rc, 0, out)

    def test_a_wrong_proposed_count_is_rejected(self) -> None:
        # [EDGE] The parse-extraction half is checked independently of the
        # commit half; a correct `committed` does not carry a wrong `proposed`.
        vault = self._vault_file()
        self._write_receipt("sess-mprop", self._clean_entries(1, 2))
        rc, out = self._run(
            self._write_response(vault, "committed=1 proposed=9"), vault, "sess-mprop")
        self.assertEqual(rc, 1)
        self.assertTrue(any("proposed=9" in e and "parsed 9 proposal(s)" in e
                            for e in out["errors"]), out)

    def test_a_wrong_committed_count_is_rejected(self) -> None:
        # [EDGE] and the mirror image.
        vault = self._vault_file()
        self._write_receipt("sess-mcom", self._clean_entries(1, 2))
        rc, out = self._run(
            self._write_response(vault, "committed=7 proposed=2"), vault, "sess-mcom")
        self.assertEqual(rc, 1)
        self.assertTrue(any("applied 7 decision(s)" in e for e in out["errors"]), out)

    def test_a_legacy_entry_without_project_or_effect_fails_closed(self) -> None:
        # [EDGE] DECISION, named in this test's own name: a legacy entry —
        # written before receipts carried these fields — is treated as
        # NON-MATCHING, not as matching. It cannot say which project it was
        # about or what it did, and reading "cannot tell" as "matches" would let
        # exactly the receipts this change replaces keep validating any counts.
        # The cost is one extra goal-step re-run for a session that straddles
        # the change; the receipt is per-session telemetry, not durable state.
        vault = self._vault_file()
        self._write_receipt("sess-legacy", [
            {"cmd": "commit", "exit": 0, "ts": "2026-08-13T18:41:07Z"},
            {"cmd": "parse-extraction", "exit": 0, "ts": "2026-08-13T18:41:07Z"}])
        rc, out = self._run(self._write_response(vault), vault, "sess-legacy")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no `commit` run recorded" in e for e in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-legacy").exists())

    def test_a_null_effect_is_not_read_as_zero(self) -> None:
        # [EDGE] `effect: null` means "this command reached no decision", which
        # is different from "it committed nothing". Collapsing the two would let
        # an aborted commit validate `committed=0`.
        vault = self._vault_file()
        self._write_receipt("sess-nulleff", [
            {"cmd": "commit", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": self.PROJECT, "effect": None},
            {"cmd": "parse-extraction", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": None, "effect": 1}])
        rc, out = self._run(
            self._write_response(vault, "committed=0 proposed=1"), vault, "sess-nulleff")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no `commit` run recorded" in e for e in out["errors"]), out)

    def test_a_boolean_effect_is_not_read_as_a_count(self) -> None:
        # [EDGE] `bool` subclasses `int`, so an `isinstance` read would accept
        # `{"effect": true}` as the count 1. Same trap `_entry_exit_code`
        # documents on the exit field.
        vault = self._vault_file()
        self._write_receipt("sess-booleff", [
            {"cmd": "commit", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": self.PROJECT, "effect": True},
            {"cmd": "parse-extraction", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": None, "effect": 1}])
        rc, out = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-booleff")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no `commit` run recorded" in e for e in out["errors"]), out)

    def test_an_honest_zero_committed_run_still_validates(self) -> None:
        # [EDGE] `committed=0` is legal and is the right answer for a gate that
        # ran and changed nothing (every `new` item rejected). It must not be
        # collateral damage of rejecting the zero-effect FORGERY: what
        # discriminates the two is the `parse-extraction` entry, which a real
        # extraction always leaves and the forgery does not.
        vault = self._vault_file()
        self._write_receipt("sess-zero", self._clean_entries(0, 3))
        rc, out = self._run(
            self._write_response(vault, "committed=0 proposed=3"), vault, "sess-zero")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self._stamp_path("sess-zero").is_file())

    def test_proposed_zero_passes_only_on_a_receipt_goals_cannot_write(self) -> None:
        # [EDGE] Names the reader-side limit honestly: `check_goal_accounting`
        # has NO special rule for `proposed=0`, so a receipt carrying a clean
        # `parse-extraction` with `effect: 0` does satisfy it — as asserted
        # below. What closes the hole is upstream, not here: goals.py cannot
        # produce that entry, because `parse-extraction` aborts (exit 2, null
        # effect) on a proposal-less block. That writer-side fact is pinned in
        # test_goals.py::
        #   test_a_proposal_less_extraction_records_a_null_effect_never_zero
        # The receipt below is hand-forged into a shape goals.py never writes,
        # so this test documents where the guarantee comes from rather than
        # claiming the validator supplies it.
        vault = self._vault_file()
        self._write_receipt("sess-p0", [
            {"cmd": "commit", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": self.PROJECT, "effect": 0},
            {"cmd": "parse-extraction", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": self.PROJECT, "effect": 0}])
        rc, out = self._run(
            self._write_response(vault, "committed=0 proposed=0"), vault, "sess-p0")
        # Passes, and that is the point being recorded: the validator does not
        # reject this, so the zero-count guarantee is not a reader-side one.
        self.assertEqual(rc, 0, out)

    def test_missing_project_flag_fails_closed(self) -> None:
        # [EDGE] Same treatment as the missing --stamp-session above: the
        # project check cannot run without it, so a caller who drops the flag
        # must not thereby skip the check.
        vault = self._vault_file()
        self._write_receipt("sess-noproj", self._clean_entries(1, 1))
        rc, out = self._run(self._write_response(vault), vault, "sess-noproj", project="")
        self.assertEqual(rc, 1)
        self.assertTrue(any("--project is required" in e for e in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-noproj").exists())

    def test_the_project_flag_is_required_on_the_ledger_error_path_too(self) -> None:
        # [EDGE] The precondition applies on every path, not only the one that
        # consumes it. A precondition that applies sometimes is one a caller
        # learns to route around.
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-lenoproj", [
            {"cmd": "load", "exit": 3, "project": self.PROJECT, "effect": None}])
        rc, out = self._run(
            self._write_response(vault, "ledger-error"), vault, "sess-lenoproj", project="")
        self.assertEqual(rc, 1)
        self.assertTrue(any("--project is required" in e for e in out["errors"]), out)

    def test_ledger_error_is_not_corroborated_by_another_projects_failure(self) -> None:
        # [EDGE] The `ledger-error` branch keeps its existing corroboration and
        # additionally requires the failure to be THIS project's, where the
        # entry says which project it was.
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-leother", [
            {"cmd": "commit", "exit": 4, "project": "somewhere-else", "effect": None,
             "ts": self.ENTRY_TS}])
        rc, out = self._run(
            self._write_response(vault, "ledger-error"), vault, "sess-leother")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no recorded goals.py run for project" in e
                            for e in out["errors"]), out)

    def test_ledger_error_accepts_a_recorded_null_project_failure(self) -> None:
        # [EDGE] A recorded `null` project is accepted here: `parse-extraction`
        # legitimately has none, and so does a commit that aborted before
        # resolving one — that abort is itself a real ledger error. Only a
        # DIFFERENT named project, or a legacy entry, fails to corroborate.
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-lenull", [
            {"cmd": "parse-extraction", "exit": 2, "project": None, "effect": None,
             "ts": self.ENTRY_TS}])
        rc, out = self._run(
            self._write_response(vault, "ledger-error"), vault, "sess-lenull")
        self.assertEqual(rc, 0, out)

    def test_no_message_tells_the_reader_to_delete_a_guard_file(self) -> None:
        # SERIOUS 1, three-persona convergence. The corrupt-stamp message used
        # to say "Delete <stamp>, then re-run" — and deleting the stamp
        # collapses the freshness floor to None, reopening the hole the floor
        # closed. The corrupt-RECEIPT message next door had the same shape. No
        # message in this module may instruct deleting, moving, or bypassing a
        # guard file, so this sweeps the whole source rather than the two.
        import ast
        source = Path(validate_report.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Module-level string constants, so a message built by concatenating one
        # in (`"..." + _MARKER_RECOVERY`) is audited as the reader sees it. This
        # sweep used to collect inline literals ONLY, which made it blind to
        # exactly the half where a delete instruction most naturally lives: the
        # RECOVERY text, whose whole job is to tell the reader what to do next.
        # Both `_THIN_RECOVERY` and `_MARKER_RECOVERY` reach their messages by
        # name and so contributed nothing, while the test's name claimed it
        # "sweeps the whole source". The blind spot predates the marker; adding
        # a second name-referenced constant is what made it worth closing.
        constants: dict[str, str] = {}
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                text = " ".join(
                    child.value for child in ast.walk(node.value)
                    if isinstance(child, ast.Constant) and isinstance(child.value, str))
                if text:
                    constants[node.targets[0].id] = text

        def literal_text(node: ast.AST) -> str:
            """Every string literal under a node, joined, plus the text of any
            module-level string constant referenced by name. Covers the adjacent
            implicit concatenation and f-strings these messages are built from,
            and — unlike a regex over the raw source — cannot stray into the
            comments that DISCUSS the old wording."""
            parts: list[str] = []
            for child in ast.walk(node):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    parts.append(child.value)
                elif isinstance(child, ast.Name) and child.id in constants:
                    parts.append(constants[child.id])
            return " ".join(parts)

        messages: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_append = (isinstance(func, ast.Attribute) and func.attr == "append"
                         and isinstance(func.value, ast.Name) and func.value.id == "errors")
            is_raise = isinstance(func, ast.Name) and func.id == "ValidationError"
            if is_append or is_raise:
                messages.append(literal_text(node))
        # Also the list-literal returns `check_report_states_its_reason` uses.
        for node in ast.walk(tree):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.List):
                messages.append(literal_text(node.value))

        self.assertTrue(messages, "found no rejection messages to audit")
        # Negation-aware: a message that says "must NOT be deleted" or "do not
        # hand-edit ... to remove" is the desired shape, not the banned one, and
        # a bare substring search flags exactly the messages that got this
        # right. Only an unnegated imperative counts.
        negators = ("not ", "never ", "n't ", "nothing ")
        for message in messages:
            for match in re.finditer(r"\b(delete|remove|rm)\b", message, re.I):
                window = message[max(0, match.start() - 40):match.start()].lower()
                if any(negator in window for negator in negators):
                    continue
                self.fail(
                    "a rejection message tells the reader to remove a guard file "
                    f"({match.group(0)!r}): {message!r}")

    def test_the_corrupt_stamp_message_says_not_to_delete_it(self) -> None:
        vault = self._vault_file()
        stamp_dir = Path(self._home) / ".claude" / "cache" / "progress-report-fresh"
        stamp_dir.mkdir(parents=True, exist_ok=True)
        (stamp_dir / "sess-cs.json").write_text('{"ts": "2026-08', encoding="utf-8")
        self._write_receipt("sess-cs", self._clean_entries(1, 1))
        rc, out = self._run(self._write_response(vault), vault, "sess-cs")
        self.assertEqual(rc, 1)
        message = next(e for e in out["errors"] if "freshness stamp" in e)
        self.assertIn("must not be deleted", message)
        self.assertIn("Step 4b", message)

    def test_the_corrupt_receipt_message_does_not_say_to_delete_it(self) -> None:
        vault = self._vault_file()
        self._run_dir.mkdir(parents=True, exist_ok=True)
        (self._run_dir / "sess-cr.json").write_text('{"session": "x", "entr', encoding="utf-8")
        rc, out = self._run(self._write_response(vault), vault, "sess-cr")
        self.assertEqual(rc, 1)
        message = next(e for e in out["errors"] if "goal-run receipt" in e)
        self.assertNotIn("Delete", message)
        self.assertIn("nothing to remove by hand", message)

    # ── The `proposed` half is bound to the project, not merely to the session ──
    #
    # For one round it was bound to neither project nor time. `_record_run`
    # wrote `project: null` for every `parse-extraction`, and this check accepted
    # any entry whose `effect` matched — so an extraction run for a DIFFERENT
    # project, or from an EARLIER run of the same session, validated this report
    # whenever the counts coincided. Thirteen tests in this file were passing on
    # a project-less entry when that was fixed; see `_clean_entries`.
    #
    # Every test below keeps the `commit` half honest and varies only the
    # `parse-extraction` entry, so a failure can only be the half under test. All
    # of them assert WHICH error fired: `cmd_result` accumulates errors in a
    # list, so a bare `rc == 1` passes for the wrong reason.

    def test_a_parse_extraction_for_another_project_does_not_validate_this_report(self) -> None:
        # [RED] The direct instance. `commit` is beta's and correct; only the
        # extraction belongs to alpha. Before the fix this passed — that pass IS
        # the coupling proof, and it is reproduced at HEAD in the report.
        vault = self._vault_file()
        self._write_receipt("sess-pab", [
            {"cmd": "commit", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": "beta", "effect": 1},
            {"cmd": "parse-extraction", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": "alpha", "effect": 1}])
        rc, out = self._run(self._write_response(vault), vault, "sess-pab", project="beta")
        self.assertEqual(rc, 1)
        self.assertTrue(any("'beta'" in e and "no `parse-extraction` run recorded" in e
                            for e in out["errors"]), out)
        # The `commit` half must NOT have fired: it is correct in this fixture,
        # and a test that passes because the other half rejected is testing
        # nothing about the half it names.
        self.assertFalse(any("no `commit` run recorded" in e for e in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-pab").exists())

    def test_an_earlier_runs_parse_extraction_for_another_project_does_not_validate(self) -> None:
        # [RED] The likelier real failure, and the reason this hole was wider
        # than a two-project race. The receipt is per-session and append-only
        # ACROSS runs, so an earlier run's entry sits in the same file as this
        # run's. Here the session touched alpha first, then beta; both parsed 1
        # proposal, which for small integers they often do. Only beta's own
        # extraction may answer beta's report — and beta never ran one.
        vault = self._vault_file()
        self._write_receipt("sess-two", [
            {"cmd": "parse-extraction", "exit": 0, "ts": "2026-08-13T18:20:00Z",
             "project": "alpha", "effect": 1},
            {"cmd": "commit", "exit": 0, "ts": "2026-08-13T18:20:05Z",
             "project": "alpha", "effect": 1},
            {"cmd": "commit", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": "beta", "effect": 1}])
        rc, out = self._run(self._write_response(vault), vault, "sess-two", project="beta")
        self.assertEqual(rc, 1)
        self.assertTrue(any("'beta'" in e and "no `parse-extraction` run recorded" in e
                            for e in out["errors"]), out)
        self.assertFalse(any("no `commit` run recorded" in e for e in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-two").exists())

    def test_both_halves_matching_this_project_validate_and_stamp(self) -> None:
        # [GREEN] The honest run, with the two counts DIFFERENT so neither
        # entry could be answering for the other's number.
        vault = self._vault_file()
        self._write_receipt("sess-ok2", self._clean_entries(2, 5))
        rc, out = self._run(
            self._write_response(vault, "committed=2 proposed=5"), vault, "sess-ok2")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self._stamp_path("sess-ok2").is_file())

    def test_a_failed_parse_extraction_for_this_project_is_still_rejected(self) -> None:
        # [EDGE] Right project, right effect, non-zero exit. A recorded FAILURE
        # to reach a decision is not a decision, and adding the project clause
        # must not have displaced the exit-code clause beside it.
        vault = self._vault_file()
        self._write_receipt("sess-pfail", [
            {"cmd": "commit", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": self.PROJECT, "effect": 1},
            {"cmd": "parse-extraction", "exit": 2, "ts": "2026-08-13T18:41:07Z",
             "project": self.PROJECT, "effect": 1}])
        rc, out = self._run(self._write_response(vault), vault, "sess-pfail")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no `parse-extraction` run recorded" in e
                            for e in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-pfail").exists())

    def test_a_null_project_parse_extraction_entry_fails_closed(self) -> None:
        # [EDGE] THE BACKWARD-COMPATIBILITY DECISION, named in the test name:
        # a `parse-extraction` entry recording `project: null` — every entry
        # written before the flag existed, and any written by a caller that
        # omits it — does NOT match, and the report is rejected.
        #
        # Fail closed, deliberately. The receipt is per-session, ephemeral and
        # capped at 20 entries, so nothing durable is lost; the cost is one
        # rejected report that re-runs the goal step. Failing OPEN would have
        # meant accepting the exact entries this change exists to replace, which
        # is the hole rather than a concession to it.
        #
        # It costs no code: `_entry_project` returns `None` here, and `None`
        # already compares unequal to a non-empty slug. The decision is visible
        # as the ABSENCE of a special case, which is why it is pinned by a test
        # — there is no line to read it off.
        vault = self._vault_file()
        self._write_receipt("sess-pnull", self._clean_entries_without_parse_project(1, 1))
        rc, out = self._run(self._write_response(vault), vault, "sess-pnull")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no `parse-extraction` run recorded" in e
                            for e in out["errors"]), out)
        self.assertFalse(any("no `commit` run recorded" in e for e in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-pnull").exists())

    def test_a_legacy_parse_extraction_entry_with_no_project_key_fails_closed(self) -> None:
        # [EDGE] The other legacy shape: the KEY is absent, not null.
        # `_entry_project` distinguishes the two, and both must fail closed here.
        vault = self._vault_file()
        self._write_receipt("sess-pleg", [
            {"cmd": "commit", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "project": self.PROJECT, "effect": 1},
            {"cmd": "parse-extraction", "exit": 0, "ts": "2026-08-13T18:41:07Z",
             "effect": 1}])
        rc, out = self._run(self._write_response(vault), vault, "sess-pleg")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no `parse-extraction` run recorded" in e
                            for e in out["errors"]), out)
        self.assertFalse(self._stamp_path("sess-pleg").exists())

    def test_the_parse_extraction_project_is_matched_on_its_slug(self) -> None:
        # [EDGE] The same slug derivation as the `commit` half — goals.py's own
        # `slugify`, imported rather than reimplemented. A caller passing the
        # project NAME must match an entry storing the SLUG, on both halves.
        self.assertEqual(validate_report.project_slug("Demo Project"), "demo-project")
        vault = self._vault_file()
        self._write_receipt("sess-pslug", self._clean_entries(1, 1, project="demo-project"))
        rc, out = self._run(self._write_response(vault), vault, "sess-pslug",
                            project="Demo Project")
        self.assertEqual(rc, 0, out)

    def test_the_rejection_names_the_project_and_routes_to_the_goal_step(self) -> None:
        # [EDGE] The message must carry the same load the `commit` half's does:
        # which project, which file, which step. Asserted here rather than left
        # to the greps above, which only check that a message exists.
        vault = self._vault_file()
        self._write_receipt("sess-pmsg", self._clean_entries_without_parse_project(1, 1))
        rc, out = self._run(self._write_response(vault), vault, "sess-pmsg")
        self.assertEqual(rc, 1)
        message = next(e for e in out["errors"] if "no `parse-extraction` run recorded" in e)
        self.assertIn(repr(self.PROJECT), message)
        self.assertIn("/home/pedro/.claude/skills/progress-report/SKILL.md", message)
        self.assertIn("Step 4b", message)


class TestThinSessionIsCheckedAgainstItsMetrics(GoalReceiptHarness):
    """Task 11 round 4: `thin-session` costs what every other accounting value
    costs.

    Up to round 3 this value was accepted on the word of the result block alone —
    the only one of the four whose truth was never checked, and therefore the
    cheapest path through Step 6 for a run that had lost the plot. The skip now
    leaves a receipt entry carrying this project and the two digest metrics that
    DEFINE the condition, and the entry's presence is not enough: the recorded
    numbers must actually satisfy `is_thin_session`.

    What that establishes, stated no wider than it is: this run recorded a skip,
    for this project, since the last accepted report, with metrics that justify
    it. The metrics are still the caller's word for what `digest.py` computed —
    nothing here re-reads the transcript — and nothing here establishes that a
    human was asked anything.
    """

    def test_metrics_that_fail_the_threshold_do_not_justify_the_claim(self) -> None:
        # [RED] THE FINDING THAT MATTERS MOST. A thin-session entry that is
        # present, clean, this project's and fresh — and records 900 events. If
        # presence alone were the check, this would pass, and "an entry exists"
        # would once again stand in for "the condition holds for THIS run".
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-fat", [self._thin_entry(event_count=900, active_msg_count=4)])
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-fat")
        self.assertEqual(rc, 1)
        message = next(e for e in out["errors"] if "thin-session" in e)
        self.assertIn("do not justify the claim", message)
        self.assertIn("event_count=900 active_msg_count=4", message,
                      "the message quotes what was actually recorded")
        self.assertIn("'demo'", message, "the message names the project")
        self.assertFalse(self._stamp_path("sess-fat").exists())

    def test_a_thin_entry_for_another_project_does_not_justify_this_report(self) -> None:
        # [RED] The receipt is per-SESSION and `$PROJECT` is re-resolved from cwd
        # on every invocation, so one session can hold a thin skip recorded for a
        # different project. It describes that project's run, not this report.
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-otherproj", [self._thin_entry(project="other-project")])
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-otherproj")
        self.assertEqual(rc, 1)
        message = next(e for e in out["errors"] if "thin-session" in e)
        self.assertIn("'other-project'", message, "the message names what was recorded")
        self.assertIn("'demo'", message, "and the project this report is about")
        self.assertFalse(self._stamp_path("sess-otherproj").exists())

    def test_an_identity_less_thin_entry_is_named_in_words_not_a_memory_address(self) -> None:
        # [EDGE] The wrong-project message quotes what the receipt recorded. Two
        # of the three things `_entry_project` can return are not strings: `None`
        # for a recorded null, and the module-level sentinel for a legacy entry
        # with no `project` key. `repr` of that sentinel is `<object object at
        # 0x7f...>` — an address, different on every run, naming nothing to the
        # person reading the rejection. Unreachable from the writer today;
        # reachable from a hand-edited receipt, which is precisely the input this
        # branch exists to reject.
        null_project = self._thin_entry(project=None)
        no_project_key = self._thin_entry()
        del no_project_key["project"]
        for name, entry, expected in (
                ("null", null_project, "no project recorded"),
                ("absent", no_project_key, "no project field")):
            with self.subTest(project=name):
                vault = self._vault_stating_its_reason()
                session = f"sess-anon-{name}"
                self._write_receipt(session, [entry])
                rc, out = self._run(self._write_response(vault, "thin-session"), vault, session)
                self.assertEqual(rc, 1)
                message = next(e for e in out["errors"] if "thin-session" in e)
                self.assertIn(expected, message)
                self.assertNotIn("0x", message, "no memory address in a user-facing rejection")
                self.assertNotIn("object at", message)
                self.assertIn("'demo'", message, "the project this report is about is still named")
                self.assertFalse(self._stamp_path(session).exists())

    def test_a_genuine_thin_run_passes_and_stamps(self) -> None:
        # [GREEN] Metrics under both thresholds, this project, and the report
        # body carrying the stated-reason line. This is the case that must keep
        # working: thin sessions are common, and rejecting them would make
        # /progress-report unusable on exactly the runs it is cheapest to do.
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-genuine",
                            [self._thin_entry(event_count=4, active_msg_count=29)])
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-genuine")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self._stamp_path("sess-genuine").is_file())

    # ── The boundary, asserted against SKILL.md Step 2's exact comparison ──
    # `event_count < 5 AND active_msg_count < 30`. Strict `<` on both, so 4 and
    # 29 are the last thin values and 5 and 30 are the first non-thin ones; `AND`
    # means either metric over the line disqualifies on its own. An off-by-one
    # here silently changes which sessions may skip the goal step, which is why
    # each case names the side of the boundary it expects.
    def _boundary(self, session: str, event_count: int, active_msg_count: int) -> tuple[int, dict]:
        vault = self._vault_stating_its_reason()
        self._write_receipt(session, [self._thin_entry(event_count=event_count,
                                                       active_msg_count=active_msg_count)])
        return self._run(self._write_response(vault, "thin-session"), vault, session)

    def test_event_count_4_is_thin_and_passes(self) -> None:
        rc, out = self._boundary("sess-e4", 4, 10)
        self.assertEqual(rc, 0, out)

    def test_event_count_5_is_not_thin_and_is_rejected(self) -> None:
        rc, out = self._boundary("sess-e5", 5, 10)
        self.assertEqual(rc, 1)
        self.assertTrue(any("do not justify the claim" in e for e in out["errors"]), out)

    def test_active_msg_count_29_is_thin_and_passes(self) -> None:
        rc, out = self._boundary("sess-a29", 2, 29)
        self.assertEqual(rc, 0, out)

    def test_active_msg_count_30_is_not_thin_and_is_rejected(self) -> None:
        rc, out = self._boundary("sess-a30", 2, 30)
        self.assertEqual(rc, 1)
        self.assertTrue(any("do not justify the claim" in e for e in out["errors"]), out)

    def test_the_condition_is_AND_events_over_messages_under_is_rejected(self) -> None:
        rc, out = self._boundary("sess-and1", 5, 1)
        self.assertEqual(rc, 1)
        self.assertTrue(any("do not justify the claim" in e for e in out["errors"]), out)

    def test_the_condition_is_AND_messages_over_events_under_is_rejected(self) -> None:
        rc, out = self._boundary("sess-and2", 1, 30)
        self.assertEqual(rc, 1)
        self.assertTrue(any("do not justify the claim" in e for e in out["errors"]), out)

    def test_the_boundary_is_the_one_goals_py_defines(self) -> None:
        # The numbers above are asserted a second time against the writer's own
        # constants, so a threshold change moves the code and these tests
        # together or fails loudly here — rather than leaving four cases pinned
        # to a boundary the system no longer uses.
        import goals
        self.assertEqual((goals.THIN_EVENT_COUNT_MAX, goals.THIN_ACTIVE_MSG_COUNT_MAX), (5, 30))
        self.assertTrue(goals.is_thin_session(4, 29))
        self.assertFalse(goals.is_thin_session(5, 29))
        self.assertFalse(goals.is_thin_session(4, 30))

    # ── Freshness, legacy, and the shapes that must fail closed ──

    def test_a_valid_thin_entry_predating_the_freshness_floor_is_rejected(self) -> None:
        # [EDGE] The floor is the previous run's accepted report. An entry from
        # before it belongs to that run, not this one — receipts are per-session
        # and append-only across runs, so without the floor one thin skip would
        # excuse every later run of the session.
        vault = self._vault_stating_its_reason()
        self._write_stamp("sess-oldthin", "2026-08-13T19:00:00Z")
        self._write_receipt("sess-oldthin", [self._thin_entry(ts="2026-08-13T18:41:07Z")])
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-oldthin")
        self.assertEqual(rc, 1)
        message = next(e for e in out["errors"] if "thin-session" in e)
        self.assertIn("recorded nothing since this run's freshness floor", message)
        self.assertIn("2026-08-13T19:00:00Z", message)
        # The STAMP is the binding floor here, not the marker, and the message
        # says which — a floor that names the wrong file sends its reader to
        # look in the wrong place. `_run`'s default marker is deliberately older
        # than this stamp, so the two are distinguishable.
        self.assertIn("from the last accepted report's freshness stamp", message)
        # The previous run's stamp must survive a rejected run untouched.
        self.assertEqual(
            json.loads(self._stamp_path("sess-oldthin").read_text())["ts"], "2026-08-13T19:00:00Z")

    def test_a_legacy_receipt_with_no_thin_entry_fails_closed(self) -> None:
        # [EDGE] BACKWARD COMPATIBILITY IS FAIL CLOSED, and this test says so in
        # its name. No receipt on disk carried a thin-session entry before this
        # change, so a session already in flight that goes thin is rejected once
        # and passes on the re-run that records the skip. The alternative —
        # accepting a receipt that carries no thin entry — is indistinguishable
        # from accepting the value on its word, which is the whole defect.
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-legacythin", [{"cmd": "load", "exit": 0, "ts": self.ENTRY_TS}])
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-legacythin")
        self.assertEqual(rc, 1)
        message = next(e for e in out["errors"] if "thin-session" in e)
        self.assertIn("no clean `thin-session` run is recorded", message)
        self.assertFalse(self._stamp_path("sess-legacythin").exists())

    def test_a_thin_entry_with_no_metrics_key_fails_closed(self) -> None:
        # An entry that records no metrics cannot say the session was thin. This
        # is the shape a future writer regression would produce — the entry still
        # there, the numbers gone — and it must reject rather than fall back to
        # the presence check this round removed.
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-nometrics", [self._thin_entry(metrics=None)])
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-nometrics")
        self.assertEqual(rc, 1)
        message = next(e for e in out["errors"] if "thin-session" in e)
        self.assertIn("no usable metrics", message)

    def test_boolean_metrics_are_not_counts(self) -> None:
        # `bool` subclasses `int`, so an `isinstance` check would read `True` as
        # the count 1 — under both thresholds. Same trap `_entry_exit_code` and
        # `_entry_effect` are spelled `type(...) is int` for.
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-boolmetrics", [self._thin_entry(
            metrics={"event_count": True, "active_msg_count": True})])
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-boolmetrics")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no usable metrics" in e for e in out["errors"]), out)

    def test_a_failed_thin_recording_does_not_corroborate(self) -> None:
        # A `thin-session` run that aborted recorded no skip — it recorded a
        # failure. Same rule the counts path applies to a failed decision.
        vault = self._vault_stating_its_reason()
        self._write_receipt("sess-thinfail", [self._thin_entry(exit_code=2)])
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-thinfail")
        self.assertEqual(rc, 1)
        self.assertTrue(any("no clean `thin-session` run is recorded" in e
                            for e in out["errors"]), out)

    def test_a_thin_entry_does_not_validate_a_counts_declaration(self) -> None:
        # The new entry answers for `thin-session` and for nothing else. A run
        # that skipped the goal step cannot then declare what the gate committed.
        vault = self._vault_file()
        self._write_receipt("sess-thincounts", [self._thin_entry()])
        rc, out = self._run(
            self._write_response(vault, "committed=1 proposed=1"), vault, "sess-thincounts")
        self.assertEqual(rc, 1)
        self.assertTrue(any("never reached a decision" in e for e in out["errors"]), out)

    def test_the_rejection_never_says_to_delete_a_guard_file(self) -> None:
        # Standing rule for every message in this module: the remedy for a guard
        # that rejected you is never removing the guard. Checked across all four
        # thin rejection paths, since each carries its own message.
        vault = self._vault_stating_its_reason()
        self._write_stamp("sess-nodel4", "2026-08-13T19:00:00Z")
        cases = {
            "sess-nodel1": None,
            "sess-nodel2": [self._thin_entry(project="other-project")],
            "sess-nodel3": [self._thin_entry(event_count=900)],
            "sess-nodel4": [self._thin_entry(ts="2026-08-13T18:00:00Z")],
        }
        for session, entries in cases.items():
            with self.subTest(session=session):
                if entries is not None:
                    self._write_receipt(session, entries)
                rc, out = self._run(
                    self._write_response(vault, "thin-session"), vault, session)
                self.assertEqual(rc, 1)
                message = next(e for e in out["errors"] if "thin-session" in e)
                for forbidden in ("delete", "Delete", "remove the", "rm "):
                    self.assertNotIn(forbidden, message)

    def test_the_real_goals_py_writes_an_entry_this_validator_accepts(self) -> None:
        # THE SEAM. Every other test in this class writes the receipt by hand, so
        # all of them would keep passing if the writer and the reader disagreed
        # about the entry's shape — a receipt that exists and is never matched
        # looks exactly like a run that never recorded its skip. This one runs
        # the REAL subcommand into the harness's own run directory and validates
        # what it actually wrote.
        import subprocess
        env = dict(os.environ, HOME=self._home, GOALS_RUN_DIR=str(self._run_dir),
                   GOALS_LEDGER_DIR=str(Path(self._home) / "ledger"),
                   GOALS_LOCK_DIR=str(Path(self._home) / "lock"))
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "goals.py"), "thin-session",
             "--project", self.PROJECT, "--session", "sess-seam",
             "--event-count", "3", "--active-msg-count", "12"],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        vault = self._vault_stating_its_reason()
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-seam")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self._stamp_path("sess-seam").is_file())

    def test_the_real_goals_py_records_metrics_the_validator_refuses(self) -> None:
        # The same seam in the failing polarity: the writer records what it was
        # told even when the numbers are not thin, and the validator is what
        # refuses them. Pins the division of responsibility — a writer that
        # started rejecting non-thin metrics itself would turn this into a
        # non-zero entry, which the `ledger-error` branch would then accept as
        # corroboration for a different declaration.
        import subprocess
        env = dict(os.environ, HOME=self._home, GOALS_RUN_DIR=str(self._run_dir),
                   GOALS_LEDGER_DIR=str(Path(self._home) / "ledger"),
                   GOALS_LOCK_DIR=str(Path(self._home) / "lock"))
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "goals.py"), "thin-session",
             "--project", self.PROJECT, "--session", "sess-seamfat",
             "--event-count", "900", "--active-msg-count", "12"],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(json.loads(proc.stdout)["thin"], False)
        vault = self._vault_stating_its_reason()
        rc, out = self._run(self._write_response(vault, "thin-session"), vault, "sess-seamfat")
        self.assertEqual(rc, 1)
        self.assertTrue(any("do not justify the claim" in e for e in out["errors"]), out)

    def test_the_three_real_commands_run_in_skill_order_and_are_accepted(self) -> None:
        # THE WHOLE SEAM, in SKILL.md's order and with nothing written by hand:
        # Step 2 `run-start`, then the goal step, then Step 6 `result`, each a
        # real subprocess. The two seam tests above each fixture ONE half — the
        # first hand-writes the marker, the second hand-writes nothing but still
        # reaches the validator through the in-process harness — so both would
        # keep passing if the marker the real command writes and the floor the
        # real validator applies disagreed. Nothing else in this suite runs all
        # three writers against each other.
        #
        # It also exercises the tie-break against a real clock, which is the
        # only place that boundary can be observed honestly. `run-start` runs
        # FIRST, so the receipt entry's `ts` is always >= the marker's and this
        # test cannot flake in either direction. What it CANNOT promise is which
        # side of the boundary it lands on: the two timestamps are equal only
        # when both commands finish inside one second, so a strict `>` on the
        # marker turns this red on a fast machine and leaves it green on a slow
        # one. Treat it as a live-clock smoke test of the seam, NOT as the guard
        # against a strict marker boundary — the deterministic boundary tests in
        # TestRunStartMarkerIsHalfTheFloor are what pin that, and they pin it at
        # a fixed timestamp precisely because wall-clock cannot.
        import subprocess
        env = dict(os.environ, HOME=self._home, GOALS_RUN_DIR=str(self._run_dir),
                   GOALS_LEDGER_DIR=str(Path(self._home) / "ledger"),
                   GOALS_LOCK_DIR=str(Path(self._home) / "lock"))
        validator = str(SCRIPTS / "validate_report.py")
        session = "sess-e2e"

        start = subprocess.run(
            [sys.executable, validator, "run-start", "--session", session],
            capture_output=True, text=True, env=env)
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertTrue(self._marker_path(session).is_file(),
                        "Step 2 left no marker for the validator to read")

        goal = subprocess.run(
            [sys.executable, str(SCRIPTS / "goals.py"), "thin-session",
             "--project", self.PROJECT, "--session", session,
             "--event-count", "3", "--active-msg-count", "12"],
            capture_output=True, text=True, env=env)
        self.assertEqual(goal.returncode, 0, goal.stderr)

        vault = self._vault_stating_its_reason()
        response = self._write_response(vault, "thin-session")
        result = subprocess.run(
            [sys.executable, validator, "result", response,
             "--vault-target", vault, "--stamp-session", session,
             "--project", self.PROJECT],
            capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0,
                         f"stdout={result.stdout} stderr={result.stderr}")
        self.assertTrue(self._stamp_path(session).is_file(),
                        "an accepted run left no freshness stamp behind")


class TestGoalSections(unittest.TestCase):
    def test_clean_goal_report_passes(self) -> None:
        self.assertEqual(validate_report.validate_report_text(GOAL_REPORT), [])

    def test_an_earlier_blocks_goals_section_does_not_answer_for_this_run(self) -> None:
        # [EDGE] SERIOUS 4, fifth instance of the standing defect class: the
        # day-file is append-on-top, so a whole-text `"### Goals" in text`
        # search let run 1's section satisfy run 2. The newest block here has
        # the Resume Here bullet but NO Goals section; an older block below has
        # one. Scoped to this run's block, that is the half-rendered contract
        # the check exists to catch.
        text = (
            "---\ndate: 2026-08-13\n---\n\n# Session Progress Report\n\n"
            "## 20:00 — newest run\n\n"
            "### Resume Here\n- **Goals:** 1/2 done\n\n"
            "### Technical Status\n**Modified:**\n- `a.py`\n\n"
            "## 19:00 — earlier run\n\n"
            "### Goals\n- [ ] something\n\n"
            "### Resume Here\n- **Goals:** 1/2 done\n\n"
            "### Technical Status\n**Modified:**\n- `a.py`\n")
        errors = validate_report.check_goal_sections(text)
        self.assertTrue(
            any("bullet present but the `### Goals` section is missing" in e for e in errors),
            errors)

    def test_all_three_subchecks_read_the_same_scope(self) -> None:
        # [EDGE] Scoping only SOME of the three would be a new defect rather
        # than a fix: an earlier block's `Planned vs actual` measured against
        # the newest block's missing Goals section would manufacture a
        # rejection for a report that is in fact correct. The newest block here
        # is self-consistent and must produce no errors at all.
        text = (
            "# Session Progress Report\n\n"
            "## 20:00 — newest run\n\n"
            "### Resume Here\n- nothing to resume\n\n"
            "## 19:00 — earlier run\n\n"
            "### Goals\n- [ ] something\n\n"
            "**Planned vs actual:**\n- Advanced: something\n\n"
            "### Resume Here\n- **Goals:** 1/2 done\n")
        self.assertEqual(validate_report.check_goal_sections(text), [])

    def test_a_block_less_text_still_gets_checked(self) -> None:
        # [EDGE] No `## ` block means no earlier run to inherit FROM, so the
        # whole text is the right scope — the fallback is not a fail-open gap.
        self.assertTrue(validate_report.check_goal_sections(
            "### Goals\n- [ ] something\n"))

    def test_goals_section_without_resume_goal_bullet_fails(self) -> None:
        broken = GOAL_REPORT.replace(
            "- **Goals:** 1/2 done  |  now: Wire the goal ledger\n", "")
        errors = validate_report.validate_report_text(broken)
        self.assertTrue(any("Goals" in e for e in errors))

    def test_planned_vs_actual_without_goals_section_fails(self) -> None:
        broken = GOAL_REPORT.replace("### Goals\n", "### Notes\n")
        errors = validate_report.validate_report_text(broken)
        self.assertTrue(any("Planned vs actual" in e for e in errors))

    def test_resume_here_still_capped_at_six_bullets(self) -> None:
        broken = GOAL_REPORT.replace(
            "- **Goals:** 1/2 done  |  now: Wire the goal ledger",
            "- **First command:** `python3 tests/test_goals.py`\n"
            "- **Extra:** one too many\n"
            "- **Goals:** 1/2 done  |  now: Wire the goal ledger",
        )
        errors = validate_report.validate_report_text(broken)
        self.assertTrue(any("Resume Here" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
