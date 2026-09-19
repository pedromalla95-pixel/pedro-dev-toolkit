#!/usr/bin/env python3
"""Validation helpers for progress-report outputs and builder result blocks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RESULT_START = "<<<PROGRESS_REPORT_RESULT>>>"
RESULT_END = "<<<END>>>"

# A session id becomes a cache/receipt FILENAME on both sides of this module —
# the freshness stamp it writes and the run receipt it reads. One pattern, used
# by both, and by `goals.py`'s `_RECEIPT_SESSION_RE` when it writes that
# receipt: a mismatch between writer and reader means a receipt that exists and
# is never found.
SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")

GOAL_ACCOUNTING_LINE = "goal_accounting"
# `committed=<n> proposed=<n>`, both non-negative integers. `[0-9]`, not `\d`:
# Python's `\d` matches every Unicode decimal digit, so `committed=٣ proposed=٣`
# would parse as a well-formed declaration. Nothing downstream reads the numbers
# today, which is exactly why a loose pattern here would go unnoticed until
# something did.
# Grouped so `check_goal_accounting` can read the two numbers back and compare
# them against the receipt. `parse_result_block` uses the same object as a bare
# `fullmatch` grammar check; capture groups do not change what it accepts.
_GOAL_COUNTS_RE = re.compile(r"committed=([0-9]+) proposed=([0-9]+)")
# Values carrying no counts. `not-supplied` is deliberately LEGAL grammar that
# always fails validation (see `check_goal_accounting`): the builder must be
# able to state the truth, and this particular truth must stop the run.
_GOAL_ACCOUNTING_WORDS = ("ledger-error", "thin-session", "not-supplied")

# The literal the builder renders into the REPORT BODY when it has no goal data
# to show, and the literal this module greps for. Pinned from both sides on
# purpose: `goal_accounting` lives only in the ephemeral result block, so
# without a line in the durable artifact a `ledger-error` run produces a report
# that silently omits its Goals section — byte-for-byte the 2026-08-13 artifact
# this whole task exists to prevent.
#
# Deliberately NOT `- **Goals:**`: that literal is the Resume Here goal bullet
# `check_goal_sections` keys on, and reusing it would make every stated-reason
# report fail the "bullet present but `### Goals` missing" check.
REPORT_GOAL_REASON_MARKER = "**Goals not recorded this run:**"

# The accounting values that mean "this report has no goals to show". Only
# these owe the reader a stated reason in the body.
_GOAL_ACCOUNTING_NO_DATA = ("ledger-error", "thin-session")


# Cache for the `goals.py` module object, resolved on first use by
# `goals_module`. Everything this file needs from the writer — `slugify`, the
# thin-session command name, the thin threshold — comes through this ONE loader,
# so there is one place where the writer is found and one place where failing to
# find it is handled.
_GOALS_MODULE = None


def goals_module():
    """`goals.py` as a module object, or `None` when it cannot be loaded.

    Loaded by path, not by `import goals`: this module is run as a script from
    arbitrary working directories, so it cannot rely on `goals.py` being on
    `sys.path`, and inserting it there would leak a very generic module name
    into every caller's namespace. `goals.py` executes no I/O at import — its
    module level only constructs `Path` objects — so loading it here is free of
    side effects.

    Deliberately broad `except`, and deliberately NOT cached as a failure: an
    import error here is an environment problem, not a report problem, and it
    must surface as a stated rejection rather than a traceback out of a
    validation run. Every caller treats `None` as "this check cannot run" and
    rejects.
    """
    global _GOALS_MODULE
    if _GOALS_MODULE is None:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "_progress_report_goals", Path(__file__).resolve().parent / "goals.py")
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _GOALS_MODULE = module
        except Exception:
            return None
    return _GOALS_MODULE


def project_slug(project: str) -> str | None:
    """This run's project slug, or `None` when it cannot be derived.

    Uses `goals.py`'s OWN `slugify` rather than reimplementing it. The
    receipt's `project` field is written by that function; this compares
    against it. Two slug derivations that agree today and drift later is a
    defect this project has already shipped once (`SLUG_MAX` truncation), and
    the only structural defense is that there be exactly one derivation.

    `None` on any failure, and every caller treats `None` as "this check cannot
    run" and rejects. Fail closed: a slug this module cannot derive must not
    become a comparison that silently passes.
    """
    module = goals_module()
    if module is None:
        return None
    try:
        return module.slugify(project)
    except Exception:
        # `slugify` raises `Abort` on a name that produces an empty slug. That
        # is a caller error, not a pass.
        return None


def thin_session_cmd() -> str | None:
    """The receipt `cmd` value `goals.py` writes for a recorded thin skip, read
    from the writer itself. The name exists on both sides of this artifact, so
    it gets the same single-place treatment as the slug and the threshold — a
    hardcoded second copy here would make a rename produce a receipt this reader
    silently never matches, which reads exactly like a run that never recorded
    its skip."""
    module = goals_module()
    return None if module is None else getattr(module, "THIN_SESSION_CMD", None)


def run_dir() -> Path:
    """Where `goals.py` writes its run receipts. Resolved per call rather than
    at import so `GOALS_RUN_DIR` behaves for this reader exactly as it does for
    the writer — a module-level constant would freeze the default into any test
    that imports this module before setting the variable."""
    return Path(os.environ.get(
        "GOALS_RUN_DIR",
        os.path.join(os.path.expanduser("~"), ".claude", "cache", "goal-run")))


def stamp_dir() -> Path:
    """Where the per-session freshness stamp lives. Resolved per call for the
    same reason `run_dir` is: `write_fresh_stamp` writes here and
    `check_goal_accounting` now READS here, and a reader that froze the path at
    import would look somewhere the writer never wrote."""
    return Path(os.path.expanduser("~")) / ".claude" / "cache" / "progress-report-fresh"


def run_marker_dir() -> Path:
    """Where the per-session RUN-START marker lives — the file `cmd_run_start`
    writes at SKILL.md Step 2 and `check_goal_accounting` reads as the other half
    of the freshness floor.

    HOME-derived with NO environment variable, deliberately, and NOT modelled on
    `run_dir()`: a fourth `GOALS_*` variable would make every existing test's
    isolation preamble silently incomplete, and this project has already leaked a
    real stamp into `~/.claude/cache/progress-report-fresh/` in exactly that way.
    It is `stamp_dir()` this follows, and for `stamp_dir()`'s own reason — one
    resolver used by both the writer and the reader, because a writer and a
    reader that disagree about where a floor lives produce a floor that is never
    found, which reads exactly like a run that never marked its start.
    """
    return Path(os.path.expanduser("~")) / ".claude" / "cache" / "progress-report-runstart"


class ValidationError(RuntimeError):
    pass


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_result_block(text: str) -> dict[str, Any]:
    start = text.find(RESULT_START)
    end = text.find(RESULT_END, start + len(RESULT_START))
    if start < 0 or end < 0:
        raise ValidationError("missing PROGRESS_REPORT_RESULT block")
    block = text[start + len(RESULT_START) : end].strip().splitlines()

    result: dict[str, Any] = {"paths_written": [], "tldr": []}
    mode: str | None = None
    for raw_line in block:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "paths_written:":
            mode = "paths"
            continue
        if stripped == "tldr:":
            mode = "tldr"
            continue
        if stripped.startswith("one_line_summary:"):
            result["one_line_summary"] = stripped.split(":", 1)[1].strip()
            mode = None
            continue
        if stripped.startswith("research_doc:"):
            result["research_doc"] = stripped.split(":", 1)[1].strip()
            mode = None
            continue
        if stripped.startswith(GOAL_ACCOUNTING_LINE + ":"):
            # Reject a SECOND occurrence rather than last-wins. Plain assignment
            # let a later line silently override an earlier one, so a block
            # carrying an honest `ledger-error` followed by anything else parsed
            # as the anything-else — a one-line override of the very
            # declaration this grammar exists to pin down.
            if GOAL_ACCOUNTING_LINE in result:
                raise ValidationError(
                    "result block declares `goal_accounting:` more than once. Exactly one "
                    "declaration is allowed: a second line would silently override the first. "
                    "Contract: /home/pedro/.claude/agents/progress-report-builder.md, "
                    "Return contract.")
            result[GOAL_ACCOUNTING_LINE] = stripped.split(":", 1)[1].strip()
            mode = None
            continue
        if stripped.startswith("- ") and mode == "paths":
            result["paths_written"].append(stripped[2:].strip())
            continue
        if stripped.startswith("- ") and mode == "tldr":
            result["tldr"].append(stripped[2:].strip())
            continue
        raise ValidationError(f"unparseable result block line: {line}")

    if not result["paths_written"]:
        raise ValidationError("result block has no paths_written")
    if not result.get("one_line_summary"):
        raise ValidationError("result block missing one_line_summary")
    if len(result["tldr"]) != 3:
        raise ValidationError("result block must contain exactly 3 TL;DR bullets")
    # REQUIRED, and named in the error: the reader of this message may have no
    # part of SKILL.md in context (compaction can evict the skill body before a
    # single step of it runs — that is the failure this line exists for), so
    # each message below carries the file and the step to go read. What the
    # MISSING-line message deliberately does NOT carry is the accepted values:
    # see its own comment. The unrecognised-VALUE message still lists them,
    # because its reader has already engaged with the contract and needs to
    # know which spellings are legal to correct a typo.
    accounting = result.get(GOAL_ACCOUNTING_LINE)
    if accounting is None:
        # Names the AUTHOR and the step to re-run — never the accepted values.
        # This message used to end "...with one of: <the four values>", which
        # made the cheapest literal compliance pasting the one value that
        # returns clean with no receipt required. A rejection that prints its
        # own bypass is worse than no rejection: it converts a stuck run into a
        # confidently wrong one. The `not-supplied` message below is written the
        # same way, and this is now consistent with it.
        raise ValidationError(
            "result block is missing the required `goal_accounting:` line. That line is "
            "written by the report builder from the value the skill passes it — it is not "
            "something to fill in by hand here. Read "
            "/home/pedro/.claude/agents/progress-report-builder.md (Return contract) and "
            "/home/pedro/.claude/skills/progress-report/SKILL.md Step 4b and Step 5, make the "
            "goal step set its accounting and Step 5 pass it into the builder's input "
            "contract, then re-run the report.")
    if accounting not in _GOAL_ACCOUNTING_WORDS and not _GOAL_COUNTS_RE.fullmatch(accounting):
        raise ValidationError(
            f"unrecognised goal_accounting value: {accounting!r}. Accepted: "
            "`committed=<n> proposed=<n>` (both integers >= 0) | ledger-error | "
            "thin-session | not-supplied. Contract: "
            "/home/pedro/.claude/agents/progress-report-builder.md, Return contract.")
    return result


def extract_section(text: str, heading: str) -> str:
    pattern = re.compile(rf"^### {re.escape(heading)}\s*$", re.M)
    match = pattern.search(text)
    if not match:
        return ""
    next_match = re.search(r"^### .+$", text[match.end() :], re.M)
    if not next_match:
        return text[match.end() :]
    return text[match.end() : match.end() + next_match.start()]


def newest_block(text: str) -> str | None:
    """Return only the newest H2 block of a day-file — its first `## ` line
    through the line before the next one, or EOF.

    The day-file is append-on-top: a same-day re-run inserts a new `## <hh:mm>`
    block above the previous newest and marks that one superseded (builder
    contract, "Writing the report"). So anything checked against the WHOLE file
    is inherited — run 1's line answers for run 2, and for any other session
    writing the same day-file. That is the same "present somewhere is not
    present for this run" shape as the stale-receipt bypass, and it has to be
    closed in the same way: scope the question to this run's block.

    None means there is no `## ` block at all. Callers fail closed on it: a file
    whose newest block cannot be located is a file this check cannot answer for.
    """
    first = re.search(r"^## .*$", text, re.M)
    if not first:
        return None
    rest = text[first.end():]
    following = re.search(r"^## .*$", rest, re.M)
    return text[first.start():] if not following else text[first.start():first.end() + following.start()]


def extract_sections(text: str, heading: str) -> list[str]:
    pattern = re.compile(rf"^### {re.escape(heading)}\s*$", re.M)
    matches = list(pattern.finditer(text))
    sections: list[str] = []
    for match in matches:
        next_match = re.search(r"^### .+$", text[match.end() :], re.M)
        if not next_match:
            sections.append(text[match.end() :])
        else:
            sections.append(text[match.end() : match.end() + next_match.start()])
    return sections


def bullet_paths(section: str) -> set[str]:
    paths: set[str] = set()
    for match in re.finditer(r"^\s*-\s+`([^`]+)`", section, re.M):
        paths.add(match.group(1).strip())
    return paths


def resume_here_bullet_count(text: str) -> int:
    section = extract_section(text, "Resume Here")
    if not section:
        raise ValidationError("missing Resume Here section")
    return len(re.findall(r"^\s*-\s+", section, re.M))


def check_goal_sections(text: str) -> list[str]:
    """A `### Goals` section and the Resume Here goal bullet travel together.
    One without the other means the builder rendered half the contract, which is
    how a stale goal count silently outlives the goals it counted.

    Scoped to THIS RUN's block, not the whole file. The day-file is
    append-on-top, so an unscoped `"### Goals" in text` let an EARLIER run's
    section — or another session's, on the same day-file — satisfy the check for
    a later run that rendered no Goals section at all. That is the same
    "present somewhere is not present for this run" defect `8a1ee7cb` fixed in
    `check_report_states_its_reason`, and it is closed the same way, by reusing
    the same `newest_block` rather than growing a second scoping mechanism.

    All three sub-checks read the SAME scope, deliberately. Scoping only some of
    them would be a new defect rather than a fix: an earlier block's
    `Planned vs actual` measured against the newest block's missing Goals
    section would manufacture a rejection for a report that is in fact correct.

    A text with no `## ` block at all falls back to the whole text. That is not
    a fail-open gap: the inheritance this scoping prevents requires at least two
    blocks to inherit BETWEEN, so a file with none has nothing an earlier run
    could answer for. The fallback also keeps this check meaningful for the
    block-less fragments `validate_report_text` is exercised against directly.
    """
    errors: list[str] = []
    scope = newest_block(text)
    if scope is None:
        scope = text
    has_section = "### Goals" in scope
    has_bullet = "- **Goals:**" in scope
    if has_section and not has_bullet:
        errors.append("`### Goals` present but the Resume Here `Goals:` bullet is missing")
    if has_bullet and not has_section:
        errors.append("Resume Here `Goals:` bullet present but the `### Goals` section is missing")
    if "**Planned vs actual:**" in scope and not has_section:
        errors.append("`Planned vs actual` requires a `### Goals` section")
    return errors


def validate_report_text(text: str) -> list[str]:
    errors: list[str] = []

    technical_sections = extract_sections(text, "Technical Status")
    if not technical_sections:
        errors.append("missing Technical Status section")
    else:
        for index, modified_section in enumerate(technical_sections, start=1):
            modified_match = re.search(r"\*\*Modified:\*\*(.*?)(?:\n\*\*Working on \(read, not modified\):\*\*|$)", modified_section, re.S)
            working_match = re.search(r"\*\*Working on \(read, not modified\):\*\*(.*)", modified_section, re.S)
            modified_paths = bullet_paths(modified_match.group(1) if modified_match else "")
            working_paths = bullet_paths(working_match.group(1) if working_match else "")
            overlap = sorted(modified_paths & working_paths)
            if overlap:
                errors.append(f"Technical Status block {index}: modified files also listed under Working on: " + ", ".join(overlap))

    resume_sections = extract_sections(text, "Resume Here")
    if not resume_sections:
        errors.append("missing Resume Here section")
    for index, section in enumerate(resume_sections, start=1):
        resume_count = len(re.findall(r"^\s*-\s+", section, re.M))
        if resume_count > 6:
            errors.append(f"Resume Here block {index} has {resume_count} bullets; maximum is 6")

    frontmatter_count = len(re.findall(r"(?m)^---\s*$", text))
    h1_count = len(re.findall(r"(?m)^# Session Progress Report\b", text))
    if frontmatter_count < 2:
        errors.append("report appears to be missing YAML frontmatter")
    if h1_count != 1:
        errors.append(f"report must have exactly one H1; found {h1_count}")

    errors.extend(check_goal_sections(text))

    return errors


def validate_paths(result: dict[str, Any], *, local_target: str = "", vault_target: str = "") -> list[str]:
    errors: list[str] = []
    paths = [path for path in result.get("paths_written", []) if path]
    for path in paths:
        if not Path(path).is_file():
            errors.append(f"reported path was not written: {path}")

    if local_target == "":
        if any(path.strip() == "" for path in result.get("paths_written", [])):
            errors.append("paths_written contains blank local target")
    elif local_target and local_target not in paths:
        errors.append("local_target is enabled but absent from paths_written")

    if vault_target and vault_target not in paths:
        errors.append("vault_target absent from paths_written")

    if local_target and vault_target and Path(local_target).is_file() and Path(vault_target).is_file():
        if sha256(local_target) != sha256(vault_target):
            errors.append("local and vault reports are not byte-identical")

    if "research_doc" in result:
        research_doc = result["research_doc"]
        if not research_doc or not Path(research_doc).is_file():
            errors.append("research_doc reported but file does not exist")

    return errors


def _load_receipt(path: Path) -> tuple[list[dict] | None, str]:
    """`(entries, problem)` for a run receipt. Exactly one of the two is
    meaningful: `entries` is `None` whenever `problem` is non-empty.

    A truncated or non-UTF-8 receipt raises `UnicodeDecodeError` from
    `read_text`, and a partially-written one raises `JSONDecodeError` from
    `json.loads`. Neither is an `OSError`; both are `ValueError` subclasses.
    Catching `OSError` alone here would turn a corrupt receipt into a traceback
    instead of a stated validation failure — the same wrong-exception-family
    mistake this project has now corrected four times.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError) as exc:
        return None, f"unreadable ({exc})"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"is not valid JSON ({exc})"
    if not isinstance(data, dict):
        return None, f"has a {type(data).__name__} at the top level, expected an object"
    entries = data.get("entries")
    if not isinstance(entries, list):
        return None, "has no `entries` list"
    return [entry for entry in entries if isinstance(entry, dict)], ""


def _parse_iso_z(value: object) -> datetime | None:
    """A UTC timestamp written by `goals.py`'s `_now_iso` or by
    `write_fresh_stamp`, or `None` for anything this reader cannot interpret.

    Both writers emit second-precision `YYYY-MM-DDTHH:MM:SSZ`, but the two
    produce it by different routes (`isoformat().replace(...)` and `strftime`),
    so this parses rather than compares strings — a formatting drift on either
    side would otherwise silently make every comparison wrong in whichever
    direction the lexical order happened to fall. Returns tz-aware datetimes
    only, so a naive value can never be compared against an aware one.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _entry_exit_code(entry: dict) -> int | None:
    """The entry's exit code as an integer, or `None` when it is not one.

    `type(...) is int` rather than `isinstance(..., int)`: `bool` is a subclass
    of `int`, so `isinstance` would accept `{"exit": false}` as a clean exit
    (`False == 0`) and `{"exit": true}` as a failure. Neither is an exit code.
    `goals.py` only ever writes `int(exit_code)`, so nothing legitimate is lost
    and both readers below get one predicate instead of two spellings.
    """
    exit_code = entry.get("exit")
    return exit_code if type(exit_code) is int else None


def _load_ts_file(path: Path) -> tuple[datetime | None, str]:
    """`(ts, problem)` for a JSON floor file carrying a `ts` — the freshness
    stamp or the run-start marker, read by the SAME code.

    `(None, "")` means the file is ABSENT, and nothing more than that. What
    absence MEANS is the caller's decision and the two callers decide
    differently: a missing stamp is a session with no accepted report behind it,
    while a missing run-start marker fails closed, because a marker that can be
    made to mean "no floor" by not existing turns "delete the marker" into the
    bypass. Any other unreadable state is a `problem` for both: a floor this
    reader cannot evaluate is not a floor, and treating it as absent would fail
    open on exactly the file that gates the check.

    ONE reader for both files, not two: they are written in the same shape by two
    functions in this module, and a second reader is a second place free to drift
    on what counts as unreadable — the same argument `project_slug` makes about
    there being exactly one slug derivation.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ""
    except (OSError, ValueError) as exc:
        # UnicodeDecodeError is a ValueError, not an OSError — the same
        # wrong-exception-family trap `_load_receipt` documents.
        return None, f"could not be read ({exc})"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"is not valid JSON ({exc})"
    if not isinstance(data, dict):
        return None, f"has a {type(data).__name__} at the top level, expected an object"
    parsed = _parse_iso_z(data.get("ts"))
    if parsed is None:
        return None, f"carries no readable `ts` (found {data.get('ts')!r})"
    return parsed, ""


def _load_stamp_ts(stamp_session: str) -> tuple[datetime | None, str]:
    """`(floor, problem)` for the session's existing freshness stamp.

    `(None, "")` means NO stamp exists — the first run of a session, which has no
    accepted report behind it. That USED to mean the run had no floor at all, and
    it is why a session's first report accepted a receipt entry from any earlier
    run of the same session, a dry run's included. It no longer does: the
    run-start marker is a second floor that every run has, so the stamp's absence
    now removes one of two floors rather than all of them.
    """
    return _load_ts_file(stamp_dir() / (stamp_session + ".json"))


_RECEIPT_FIELD_ABSENT = object()


def _entry_effect(entry: dict) -> object:
    """The entry's recorded effect: an `int`, `None` when the writer recorded
    no effect for that command, or `_RECEIPT_FIELD_ABSENT` when the key is not
    there at all.

    The three are genuinely different and collapsing any two loses the
    distinction the check needs. A LEGACY entry — written before receipts
    carried effects — has no key, and must never be read as "effect 0"; that
    would make every pre-change receipt validate a `committed=0` declaration.

    `type(...) is int`, not `isinstance`: `bool` subclasses `int`, so
    `isinstance` would accept `{"effect": true}` as the count 1. Same
    deliberate spelling as `_entry_exit_code`.
    """
    if "effect" not in entry:
        return _RECEIPT_FIELD_ABSENT
    effect = entry["effect"]
    return effect if type(effect) is int else None


def _entry_project(entry: dict) -> object:
    """The entry's recorded project slug, `None` when the writer recorded none
    (a `parse-extraction` run whose optional `--project` was omitted, or any
    run that aborted before resolving one), or `_RECEIPT_FIELD_ABSENT` for a
    legacy entry with no such key.

    Same three-way distinction as `_entry_effect`, for the same reason: a
    legacy entry must not be mistaken for one that recorded "no project".
    """
    if "project" not in entry:
        return _RECEIPT_FIELD_ABSENT
    project = entry["project"]
    return project if isinstance(project, str) else None


def _entry_metrics(entry: dict) -> tuple[int, int] | None:
    """The `(event_count, active_msg_count)` pair a `thin-session` entry
    recorded, or `None` when the entry carries no usable pair.

    ONE return value for "no metrics", unlike `_entry_effect`'s three-way split:
    nothing downstream needs to tell a legacy entry from a malformed one here,
    because both mean the same thing — this entry cannot justify a thin claim.
    Fails closed on every unusable shape (key absent, not an object, either value
    missing or not an integer).

    `type(...) is int` rather than `isinstance`, for the third time in this file
    and for the same reason: `bool` subclasses `int`, so `isinstance` would read
    `{"event_count": true}` as the count 1 — a value under both thresholds, which
    would make `metrics: {"event_count": true, "active_msg_count": true}` justify
    a thin-session declaration.
    """
    metrics = entry.get("metrics")
    if not isinstance(metrics, dict):
        return None
    event_count = metrics.get("event_count")
    active_msg_count = metrics.get("active_msg_count")
    if type(event_count) is not int or type(active_msg_count) is not int:
        return None
    return event_count, active_msg_count


def _metrics_are_thin(metrics: tuple[int, int] | None) -> bool:
    """Whether a recorded pair satisfies the thin condition, decided by
    `goals.py`'s own `is_thin_session` — the same comparison `SKILL.md` Step 2
    specifies and the only one in the system. This module deliberately does not
    contain the numbers `5` and `30`.

    Fails closed when the pair is unusable or the writer module cannot be
    loaded: a condition this reader cannot evaluate is not a condition that
    passed.
    """
    if metrics is None:
        return False
    module = goals_module()
    is_thin = None if module is None else getattr(module, "is_thin_session", None)
    if is_thin is None:
        return False
    try:
        return bool(is_thin(metrics[0], metrics[1]))
    except Exception:
        return False


def _thin_condition_text() -> str:
    """The thin condition in words, built from `goals.py`'s constants so the
    rejection message cannot state a threshold the check does not use."""
    module = goals_module()
    event_max = getattr(module, "THIN_EVENT_COUNT_MAX", None) if module else None
    msg_max = getattr(module, "THIN_ACTIVE_MSG_COUNT_MAX", None) if module else None
    if event_max is None or msg_max is None:
        return "the thin condition in SKILL.md Step 2"
    return f"event_count < {event_max} and active_msg_count < {msg_max}"


# The action a rejected `thin-session` run has to take. Its own literal because
# it is the ONE recovery instruction in this module that is not "run the goal
# step through to a decision" — a thin session deliberately never reaches a
# decision, and telling its operator to produce one would be telling them to
# undo the skip the value declares. Instructs no deletion, no move, and no
# bypass of any guard file.
_THIN_RECOVERY = (
    "Read /home/pedro/.claude/skills/progress-report/SKILL.md Step 4b: a thin session must "
    "record its skip with `goals.py thin-session --project \"$PROJECT\" --session "
    "\"$SESSION_UUID\" --event-count \"$TOTAL_TOOL_USES\" --active-msg-count "
    "\"$TOTAL_ENTRIES\"`, using this run's own digest metrics. If those metrics do not "
    "satisfy the thin condition, this session is not thin: run the goal step and declare "
    "what it produced. Then re-run this validation.")


# The action a run whose start marker is missing or unreadable has to take. Its
# own literal because its recovery starts EARLIER than every other message in
# this module: not at Step 4b, but at Step 2, which is where a run marks its own
# start. Instructs no deletion, no move, and no bypass — re-running from Step 2
# overwrites the marker in place, so there is nothing to remove by hand, and
# telling a reader to delete this file would hand them the one bypass the
# fail-closed branch below exists to refuse.
_MARKER_RECOVERY = (
    "Read /home/pedro/.claude/skills/progress-report/SKILL.md Step 2: every run writes its own "
    "start marker with `validate_report.py run-start --session \"$SESSION_UUID\"` before any "
    "goals.py call, and that marker is half the freshness floor the goal-run receipt is measured "
    "against. Re-run the report from Step 2 so this run marks its start — that rewrites the "
    "marker in place — then run the goal step and re-run this validation.")


def check_goal_accounting(result: dict[str, Any], stamp_session: str,
                          project: str) -> list[str]:
    """Cross-check the builder's declared goal accounting against the receipt
    `goals.py` leaves behind when it actually runs.

    What THIS function establishes, stated precisely and no wider: the goal step
    ran for THIS session, since this run started AND since the last accepted
    report, against THIS project, and produced the effect this report declares —
    or this run recorded metrics that justify having skipped it. It does NOT
    establish that a human was asked anything. The receipt records what
    `goals.py` did, not who approved it.

    "Since this run started" is the run-start marker's contribution, and it holds
    on every run rather than only on the ones with an accepted report behind
    them. A forger who runs the full per-project cycle WITHIN this run still
    passes — but at that point they have run the goal step, which is the property
    this feature actually wants.

    `thin-session` is checked on the same terms as every other value, and by the
    same route: the skip leaves its own receipt entry, carrying this project and
    the two digest metrics that define the condition, and this function compares
    those metrics against `goals.py`'s `is_thin_session`. An entry that merely
    EXISTS proves nothing — "present somewhere" standing in for "true for this
    run" is the defect class this project has shipped five times — so the
    recorded numbers have to actually satisfy the condition. It remains true
    that the metrics are the caller's word for what the digest said; what is no
    longer true is that the word costs nothing to give.

    A forger who reproduces the full per-project cycle with matching counts
    still passes — but at that point they have run the goal step, which is the
    property this feature actually wants.

    The other half of the guarantee — "or the report says on its face why it did
    not" — is `check_report_states_its_reason`, which reads the written report.
    Neither half is worth much alone: this one lets a run declare
    `ledger-error`/`thin-session` and ship a report that mentions neither, and
    that one cannot tell a stated reason from a true one. `cmd_result` runs both.
    """
    errors: list[str] = []
    accounting = str(result.get(GOAL_ACCOUNTING_LINE, ""))

    if not stamp_session:
        # Fail closed. The goal check cannot run without a session id, so a
        # caller who drops the flag would otherwise skip the check entirely by
        # omission — the cheapest possible bypass of the thing being enforced.
        errors.append(
            "REPORT REJECTED: --stamp-session is required for `validate_report.py result`. "
            "Without it the goal-ledger check cannot run and the freshness stamp cannot be "
            "written. Read /home/pedro/.claude/skills/progress-report/SKILL.md Step 6 and "
            "re-run this validation with --stamp-session \"$SESSION_UUID\".")
        return errors
    if not SESSION_ID_RE.fullmatch(stamp_session):
        # Already reported by the caller's own id check; building a receipt
        # path out of a value that failed traversal validation is exactly what
        # that check exists to prevent.
        return errors
    if not project:
        # Fail closed, exactly as the missing `--stamp-session` above does and
        # for the same reason: the receipt's project check cannot run without
        # this value, so a caller who drops the flag would skip that check by
        # omission. Checked here, before the `thin-session` and `not-supplied`
        # branches return, so the flag is required on every path rather than
        # only the one that happens to consume it — a precondition that applies
        # sometimes is a precondition a caller learns to route around.
        errors.append(
            "REPORT REJECTED: --project is required for `validate_report.py result`. "
            "Without it the goal-ledger receipt cannot be tied to the project this report "
            "is about, so a receipt from another project would validate this report. Read "
            "/home/pedro/.claude/skills/progress-report/SKILL.md Step 6 and re-run this "
            "validation with --project \"$PROJECT\".")
        return errors
    this_slug = project_slug(project)
    if this_slug is None:
        errors.append(
            f"REPORT REJECTED: --project {project!r} could not be resolved to a ledger slug, "
            "so the goal-ledger receipt cannot be tied to this project. Pass the same "
            "--project value the goal step used. Read "
            "/home/pedro/.claude/skills/progress-report/SKILL.md Step 4b and Step 6, then "
            "re-run this validation.")
        return errors

    if accounting == "not-supplied":
        errors.append(
            f"REPORT REJECTED: goal_accounting is `not-supplied` for session {stamp_session} — "
            "the builder was handed no goal data, so this report cannot state what happened to "
            "the goals. Read /home/pedro/.claude/skills/progress-report/SKILL.md Step 4b and "
            "Step 5, pass the goal accounting into the builder's input contract, and re-run. "
            "Re-spawning the builder APPENDS a new dated block to the day-file rather than "
            "replacing the one already written, so expect two blocks for this run and let the "
            "newest stand — do not hand-edit the day-file to remove the first.")
        return errors

    is_thin_declaration = accounting == "thin-session"

    receipt_path = run_dir() / (stamp_session + ".json")
    entries, problem = _load_receipt(receipt_path)
    if problem == "missing":
        if is_thin_declaration:
            # A thin session skips the ledger work, NOT the receipt. Its own
            # message because its recovery is the opposite of the one below:
            # this run must record the skip it took, not reach a decision it
            # deliberately never reaches.
            errors.append(
                f"REPORT REJECTED: goal_accounting says `thin-session` for project "
                f"{this_slug!r}, but session {stamp_session} recorded no goals.py run at all "
                f"— the thin skip was never recorded, so nothing corroborates the claim that "
                f"this session was thin. " + _THIN_RECOVERY +
                f" (Expected a run receipt at {receipt_path}.)")
            return errors
        errors.append(
            f"REPORT REJECTED: no goal-ledger activity recorded for session {stamp_session}. "
            "The goal step did not run. Read "
            "/home/pedro/.claude/skills/progress-report/SKILL.md Step 4b and run the goal step "
            "THROUGH TO A DECISION — a bare `goals.py load` leaves a receipt with no "
            "`commit`/`parse-extraction` and is rejected by the next check, so recovering one "
            "step at a time takes two passes. Then re-run this validation. (Expected a run "
            f"receipt at {receipt_path}.)")
        return errors
    if problem:
        if is_thin_declaration:
            # Same "never say delete" rule as the branch below, same reasoning:
            # re-recording the skip replaces an unusable receipt on its own.
            errors.append(
                f"REPORT REJECTED: the goal-run receipt for session {stamp_session} {problem}. "
                f"It cannot be used to confirm that this run recorded a thin skip for project "
                f"{this_slug!r}. " + _THIN_RECOVERY + " That rewrites the receipt at "
                f"{receipt_path} in place, so there is nothing to remove by hand.")
            return errors
        # Recovery is "run the goal step again", NOT "delete the receipt". This
        # message used to open with `Delete {receipt_path}`, which told the
        # reader to remove the very artifact the check depends on. Re-running
        # the goal step replaces an unusable receipt on its own — `_record_run`
        # treats a corrupt one as replaceable telemetry and writes a fresh
        # valid file carrying this run's entry — so the deletion was never
        # needed even as a mechanical step. No message in this module may
        # instruct deleting, moving, or bypassing a guard file.
        errors.append(
            f"REPORT REJECTED: the goal-run receipt for session {stamp_session} {problem}. "
            "It cannot be used to confirm the goal step ran. Read "
            "/home/pedro/.claude/skills/progress-report/SKILL.md Step 4b and run the goal step "
            "THROUGH TO A DECISION — that rewrites the receipt at "
            f"{receipt_path} in place, so there is nothing to remove by hand. Then re-run this "
            "validation.")
        return errors

    # CRITICAL: receipts are per-session and append-only ACROSS runs, so one
    # entry would otherwise satisfy this check for every later run of the same
    # session — and `--dry-run` reaches both `load` and `parse-extraction`, so a
    # single dry run left a decision entry that inoculated the runs after it.
    #
    # The floor is the LATER of two timestamps, and it takes both to be a floor
    # at all:
    #
    #   the freshness stamp — the goal step must have run since the last
    #   ACCEPTED REPORT. A session's first report has no stamp, and for one round
    #   that meant it had no floor whatsoever, so an entry from any earlier run
    #   of that session (a dry run's included) satisfied it. That was this
    #   project's standing defect in its sixth form: "present somewhere in the
    #   session" standing in for "present for THIS run", on precisely the run
    #   with nothing behind it to compare against.
    #
    #   the run-start marker — the goal step must have run since the newest
    #   run-start marker for this session, which SKILL.md Step 2 writes on every
    #   run. Unlike the stamp, that exists on a session's first report, which is
    #   what closes the hole above; and because a later run overwrites it, an
    #   earlier run's entries fall below it even when no report was accepted in
    #   between.
    #
    # STATED NO WIDER THAN IT HOLDS. The marker is per-SESSION and outlives the
    # run that wrote it, so "since the newest marker" equals "since THIS run
    # started" exactly when this run wrote one. A second run of a session that
    # skipped Step 2 inherits the first run's marker and silently falls back to
    # the stamp — no worse than the behavior before the marker existed, and no
    # better. Absence fails closed below, but absence is only reachable on a
    # session whose FIRST run skipped Step 2; after that there is always a marker
    # to inherit. What keeps Step 2 honest is criterion `c14`, which grades the
    # call — post-hoc grading, not a mechanism, and this comment says so rather
    # than letting a later reader assume the stronger property. Closing it means
    # a single-use marker the passing `result` consumes, which changes the
    # marker's contract and is deliberately not attempted here.
    #
    # `max` rather than either alone: the stamp alone lets an entry that
    # postdates the last accepted report but predates this run through, and the
    # marker alone would drop the "since the last accepted report" guarantee that
    # a mid-run marker rewrite cannot supply.
    stamp_floor, stamp_problem = _load_stamp_ts(stamp_session)
    if stamp_problem:
        # NEVER "delete the stamp". Deleting it collapses the freshness floor to
        # `None`, which reopens the exact hole the floor was added to close: with
        # no floor, an entry from any earlier run of this session — a dry run
        # included — satisfies the check for this one. The stated remedy for a
        # broken guard must not be removing the guard. Re-running the goal step
        # produces a receipt entry that clears any floor this stamp could carry,
        # and a passing validation rewrites the stamp in place.
        errors.append(
            f"REPORT REJECTED: the freshness stamp for session {stamp_session} {stamp_problem}. "
            "Its timestamp is the floor the goal-ledger receipt is measured against, so it "
            "cannot be skipped, and it must not be deleted — removing it removes the floor. "
            "Read /home/pedro/.claude/skills/progress-report/SKILL.md Step 4b, run the goal "
            "step THROUGH TO A DECISION, then re-run this validation: a passing run rewrites "
            f"the stamp at {stamp_dir() / (stamp_session + '.json')} in place.")
        return errors

    marker_path = run_marker_dir() / (stamp_session + ".json")
    marker_floor, marker_problem = _load_ts_file(marker_path)
    if marker_problem:
        # Same treatment an unreadable STAMP gets, and for the same one-line
        # reason: a floor this reader cannot evaluate is not a floor. Reading it
        # as absent would be worse here than there, because absence on this file
        # fails closed below and a `problem` routed to "no floor" would fail
        # open on the very file that supplies the floor.
        errors.append(
            f"REPORT REJECTED: the run-start marker for session {stamp_session} {marker_problem}. "
            f"Its timestamp is half the freshness floor the goal-run receipt is measured "
            f"against, so this validation cannot tell which receipt entries belong to THIS run. "
            + _MARKER_RECOVERY + f" (Marker path: {marker_path}.)")
        return errors
    if marker_floor is None:
        # FAIL CLOSED on absence. The alternative — no marker means no floor —
        # would make "do not write the marker" a complete bypass of the floor,
        # which is strictly worse than the hole this marker was added to close:
        # it would hand every run the exemption that previously belonged only to
        # a session's first report.
        errors.append(
            f"REPORT REJECTED: session {stamp_session} wrote no run-start marker, so there is "
            f"nothing to distinguish this run's goal-ledger activity from an earlier run's. The "
            f"run receipt is per-session and append-only across runs, so without this run's own "
            f"start time an entry from any earlier run of this session — a dry run's included — "
            f"would satisfy this check. " + _MARKER_RECOVERY +
            f" (Expected a marker at {marker_path}.)")
        return errors

    if stamp_floor is None or marker_floor > stamp_floor:
        # Includes the case the marker exists for: stamp absent, marker present
        # — the first report of a session, where the marker alone is the floor.
        # Written as an explicit `None` test rather than `max(...)`, which raises
        # on a `None` operand rather than treating it as "no floor".
        floor, floor_source = marker_floor, "this run's start marker"
    else:
        floor, floor_source = stamp_floor, "the last accepted report's freshness stamp"

    def _clears_both_floors(entry_ts: datetime) -> bool:
        """THE TWO FLOORS TIE-BREAK IN OPPOSITE DIRECTIONS, because they are
        written at opposite ends of a run and both carry only second precision.

        The STAMP is written when a report is ACCEPTED, at the end of a run, so
        an entry sharing its second is almost certainly the run that just
        finished — strictly newer, as it has always been. Two full report runs
        inside one second is not a real workflow; a stale entry sharing the
        stamp's second is.

        The MARKER is written when a run BEGINS, so an entry sharing its second
        is almost certainly this run's own goal step: Step 2 writes the marker
        and Step 4b's `goals.py thin-session` can follow it inside the same
        second on a short session. Strict `>` there would reject an honest run —
        and reject it on exactly the thin sessions where this skill is cheapest
        to run, which is the operational failure the thin path was warned about.
        Inclusive, and the cost is named: an entry from an earlier run that
        landed in the very second this run started is admitted. That needs the
        earlier run to have been REJECTED (an accepted one leaves a later stamp,
        which then binds instead) and to have written its entry inside that one
        second. A one-second window on an already-failed run, traded against
        rejecting correct runs.
        """
        if stamp_floor is not None and entry_ts <= stamp_floor:
            return False
        return entry_ts >= marker_floor

    entries = [entry for entry in entries
               if (entry_ts := _parse_iso_z(entry.get("ts"))) is not None
               and _clears_both_floors(entry_ts)]
    if not entries:
        # The message names the floor VALUE and which of the two files produced
        # it. It used to say "since the last accepted report" unconditionally,
        # which is now false whenever the marker binds — including on every first
        # report of a session, where there is no accepted report at all. A
        # rejection that misnames the thing it measured against sends its reader
        # to look at the wrong file.
        floor_text = f"{floor.strftime('%Y-%m-%dT%H:%M:%SZ')}, from {floor_source}"
        if is_thin_declaration:
            errors.append(
                f"REPORT REJECTED: goal_accounting says `thin-session` for project "
                f"{this_slug!r}, but session {stamp_session} recorded nothing since this run's "
                f"freshness floor ({floor_text}). The receipt carries only entries from earlier "
                "runs, so the thin skip this report declares was never recorded for THIS "
                "report. " + _THIN_RECOVERY)
            return errors
        errors.append(
            f"REPORT REJECTED: no goal-ledger activity recorded for session "
            f"{stamp_session} since this run's freshness floor ({floor_text}). The receipt "
            "carries only entries from earlier runs, so the goal step did not run for THIS "
            "report. Read /home/pedro/.claude/skills/progress-report/SKILL.md Step 4b, run the "
            "goal step, then re-run this validation.")
        return errors

    if is_thin_declaration:
        # THE METRICS MUST SATISFY THE CONDITION, NOT MERELY EXIST.
        #
        # A `thin-session` entry that is present, clean, this project's and fresh
        # says only that this run CLAIMED to be thin. The claim has a
        # deterministic definition — `SKILL.md` Step 2's `event_count < 5 and
        # active_msg_count < 30`, computed by `digest.py` — so the numbers behind
        # it are recorded and compared. Accepting the entry's presence alone
        # would be "present somewhere" standing in for "true for THIS run", which
        # is the shape five defects in this project have taken.
        #
        # The branches below are ordered narrowest-last so the message names the
        # condition that actually failed rather than the first one checked: no
        # entry at all, an entry for another project, then an entry whose metrics
        # do not qualify. Staleness is already gone — the freshness floor above
        # dropped any entry at or before the last accepted report, and a receipt
        # left with nothing was rejected there with its own message.
        thin_cmd = thin_session_cmd()
        if thin_cmd is None:
            # The writer module could not be loaded, so the receipt's own command
            # name is unknown and no comparison here can mean anything. Fails
            # closed, exactly as `project_slug` does — and in practice
            # unreachable, since `this_slug` came from the same loader.
            errors.append(
                "REPORT REJECTED: goal_accounting says `thin-session` but this validator could "
                "not load /home/pedro/.claude/skills/progress-report/scripts/goals.py, so the "
                "declaration cannot be checked against the run receipt. Fix the script path or "
                "permissions, then re-run this validation.")
            return errors
        thin_entries = [entry for entry in entries
                        if entry.get("cmd") == thin_cmd and _entry_exit_code(entry) == 0]
        if not thin_entries:
            errors.append(
                f"REPORT REJECTED: goal_accounting says `thin-session` for project "
                f"{this_slug!r}, but no clean `{thin_cmd}` run is recorded for session "
                f"{stamp_session} — the receipt carries no record that this run skipped the "
                f"goal step, so the skip is asserted by the report and by nothing else. "
                + _THIN_RECOVERY)
            return errors
        for_this_project = [entry for entry in thin_entries
                            if _entry_project(entry) == this_slug]
        if not for_this_project:
            # `_entry_project` returns `None` for a recorded null and
            # `_RECEIPT_FIELD_ABSENT` for a legacy entry; both compare unequal to
            # a non-empty slug, so both land here. Deliberate, and the same
            # fail-closed direction the counts path takes: an entry that cannot
            # say which project it was about cannot say it was about this one.
            # Rendered three ways, never `repr` of the raw value: the sentinel is
            # a bare `object()`, whose repr is a memory address — a rejection
            # message that names `<object object at 0x7f...>` names nothing, and
            # differs between runs on the same receipt.
            recorded = sorted({
                "no project recorded" if (value := _entry_project(entry)) is None
                else "no project field" if value is _RECEIPT_FIELD_ABSENT
                else repr(value)
                for entry in thin_entries})
            errors.append(
                f"REPORT REJECTED: goal_accounting says `thin-session` for project "
                f"{this_slug!r}, but the only recorded thin skips for session {stamp_session} "
                f"name {', '.join(recorded)}. Another project's thin session does not describe "
                f"this report. " + _THIN_RECOVERY)
            return errors
        if not any(_metrics_are_thin(_entry_metrics(entry)) for entry in for_this_project):
            recorded = "; ".join(
                "no usable metrics" if (pair := _entry_metrics(entry)) is None
                else f"event_count={pair[0]} active_msg_count={pair[1]}"
                for entry in for_this_project)
            errors.append(
                f"REPORT REJECTED: goal_accounting says `thin-session` for project "
                f"{this_slug!r}, but the metrics recorded for session {stamp_session} do not "
                f"justify the claim: the thin condition is `{_thin_condition_text()}` and the "
                f"receipt records {recorded}. A session that does not meet the condition may "
                f"not skip the goal step. " + _THIN_RECOVERY)
        return errors

    if accounting == "ledger-error":
        # `ledger-error` asserts `goals.py` exited non-zero and the skill took
        # its documented fallback. The receipt carries real exit codes, so that
        # assertion is checkable: require the failure it declares to actually
        # be on the receipt. Without this, `ledger-error` would be a free pass
        # any run could declare over a load-only receipt.
        # `_entry_exit_code` rather than `!= 0`: a receipt carrying
        # `"exit": "3"` would satisfy a bare inequality against 0 and wave the
        # declaration through on a string that is not an exit code at all.
        # Fails closed — an exit field this reader cannot interpret as an
        # integer is not evidence of the failure the block declares.
        # The failure must ALSO be this project's, where the entry says which
        # project it was. A recorded `project` that names a DIFFERENT project is
        # another project's failure and cannot corroborate this report.
        # A recorded `null` is accepted HERE and nowhere else: a `commit` that
        # aborted inside `_require_project` before resolving one records `null`,
        # and that abort is itself the real ledger error being corroborated, so
        # rejecting it would refuse the very failure this branch exists to
        # confirm. A `parse-extraction` that omitted its optional `--project`
        # also records `null` and is accepted on this branch only. The counts
        # path below fails closed on `null` for both commands. A
        # LEGACY entry (no `project` key at all) is not accepted, for the same
        # fail-closed reason as the counts path below.
        # This does NOT close the cheaper `--decisions /nonexistent/path`
        # forgery, which aborts at exit 2 before touching the ledger and still
        # records a non-zero entry carrying this project. That remains open.
        def _corroborates(entry: dict) -> bool:
            code = _entry_exit_code(entry)
            if code is None or code == 0:
                return False
            entry_project = _entry_project(entry)
            return entry_project == this_slug or entry_project is None

        if not any(_corroborates(entry) for entry in entries):
            errors.append(
                f"REPORT REJECTED: goal_accounting says `ledger-error` for session "
                f"{stamp_session}, but no recorded goals.py run for project {this_slug!r} "
                "failed — every entry this run could corroborate against exited 0, named a "
                "different project, or predates the fields this check reads. Read "
                "/home/pedro/.claude/skills/progress-report/SKILL.md Step 4b and "
                "declare the accounting the run actually produced.")
        return errors

    # Counts declared: the gate ran, so the run must have reached a decision.
    # `load` alone means the ledger was opened and nothing was ever decided.
    decisions = [entry for entry in entries
                 if entry.get("cmd") in ("commit", "parse-extraction")]
    if not decisions:
        errors.append(
            f"REPORT REJECTED: the goal step never reached a decision for session "
            f"{stamp_session} — the ledger was loaded, but no `commit` or `parse-extraction` "
            "run was recorded. Read "
            "/home/pedro/.claude/skills/progress-report/SKILL.md Step 4b, run the goal step "
            "through to its commit, then re-run this validation.")
        return errors
    # CRITICAL: a recorded FAILURE to reach a decision is not a decision. A
    # single `parse-extraction` aborting at exit 2 on a proposal-less block
    # writes `{cmd: parse-extraction, exit: 2}` — one command, over a file the
    # caller writes itself, with no ledger and no gate involved — and that
    # alone used to validate a block declaring `committed=7 proposed=9`.
    # Existence, not recency: an abort followed by a successful retry is an
    # honest run and must not lose, so this asks whether ANY decision entry
    # succeeded rather than whether the last one did.
    if not any(_entry_exit_code(entry) == 0 for entry in decisions):
        errors.append(
            f"REPORT REJECTED: every recorded decision run failed for session {stamp_session} "
            f"— goal_accounting declares `{accounting}`, but no `commit` or `parse-extraction` "
            "run was recorded at exit 0, so no decision was actually reached and those counts "
            "describe nothing. Read "
            "/home/pedro/.claude/skills/progress-report/SKILL.md Step 4b, run the goal step "
            "through to a clean commit, then re-run this validation.")
        return errors

    # THE COUNTS MUST DESCRIBE SOMETHING THAT HAPPENED.
    #
    # Everything above establishes that a decision run occurred. None of it
    # looks at the numbers, so `committed=3 proposed=4` rode through on a
    # `commit` over a caller-authored `{"decisions": []}` — one command, on a
    # file the caller writes itself, against a project the caller names. The
    # receipt now records what each run was ABOUT (`project`) and what it DID
    # (`effect`), so the declaration can be compared against it instead of
    # merely coexisting with it.
    #
    # Both halves are required, not either: `committed` is answered only by a
    # `commit` and `proposed` only by a `parse-extraction`, so accepting one for
    # the other would let a single command answer for a step that has two.
    counts = _GOAL_COUNTS_RE.fullmatch(accounting)
    if counts is None:
        # Unreachable via `parse_result_block`, which rejects any other value
        # before this function is ever called. Fails closed rather than falling
        # through, so a future caller reaching this function directly cannot
        # skip the comparison by passing an unparsed value.
        errors.append(
            f"REPORT REJECTED: goal_accounting {accounting!r} could not be read as "
            "`committed=<n> proposed=<m>`, so its counts cannot be checked against the "
            "goal-run receipt. Contract: "
            "/home/pedro/.claude/agents/progress-report-builder.md, Return contract.")
        return errors
    committed, proposed = int(counts.group(1)), int(counts.group(2))

    # A legacy entry — written before receipts carried `project`/`effect` — has
    # neither key, so it matches nothing here and the report is rejected. Fail
    # closed, deliberately: a legacy entry cannot say which project it was about
    # or what it did, and treating "cannot tell" as "matches" would let exactly
    # the receipts this change exists to replace keep validating any counts at
    # all. The cost is one extra goal-step re-run for a session that straddles
    # this change; the receipt is per-session telemetry, not durable state.
    #
    # A `parse-extraction` entry recording `project: null` — one written before
    # the flag existed, or by a caller that omits it — fails closed here on the
    # same reasoning, and by the same route: `_entry_project` returns `None` or
    # `_RECEIPT_FIELD_ABSENT`, and both already compare unequal to a non-empty
    # `this_slug`. That decision is visible as the ABSENCE of a special case, so
    # do not add an `or _entry_project(entry) is None` here; opening it would
    # restore the hole below. The `ledger-error` branch above does accept
    # `None`, and that asymmetry is deliberate: there, `None` marks a run that
    # aborted before it could resolve a project, which is itself the failure
    # being corroborated.
    if not any(entry.get("cmd") == "commit"
               and _entry_exit_code(entry) == 0
               and _entry_project(entry) == this_slug
               and _entry_effect(entry) == committed
               for entry in entries):
        errors.append(
            f"REPORT REJECTED: goal_accounting declares `committed={committed}` for project "
            f"{this_slug!r}, but no `commit` run recorded for session {stamp_session} matches "
            f"that: the receipt carries no clean `commit` for {this_slug!r} that applied "
            f"{committed} decision(s). A commit for another project, or one that applied a "
            "different number, does not describe this report. Read "
            "/home/pedro/.claude/skills/progress-report/SKILL.md Step 4b, run the goal step "
            "for THIS project through to its commit, and declare the `applied` count that "
            "commit actually returned. Then re-run this validation.")
    # The SAME three clauses as the `commit` half above, against the same
    # `this_slug` and through the same `_entry_project` helper — one comparison
    # written once and applied twice, not two comparisons free to drift.
    #
    # This half carried no project check for one round, and that was the wider
    # hole of the two. A cross-project race needs two projects in one session;
    # this needed only one, because the receipt is per-session and append-only
    # ACROSS runs (that is what the 20-entry cap exists for), so a
    # `parse-extraction` entry from any earlier run in the session satisfied a
    # later report whenever the counts coincided — and for small integers they
    # coincide often.
    if not any(entry.get("cmd") == "parse-extraction"
               and _entry_exit_code(entry) == 0
               and _entry_project(entry) == this_slug
               and _entry_effect(entry) == proposed
               for entry in entries):
        errors.append(
            f"REPORT REJECTED: goal_accounting declares `proposed={proposed}` for project "
            f"{this_slug!r}, but no `parse-extraction` run recorded for session "
            f"{stamp_session} matches that: the receipt carries no clean `parse-extraction` "
            f"for {this_slug!r} that parsed {proposed} proposal(s). An extraction for another "
            "project, one that recorded no project at all, or one that parsed a different "
            "number, does not describe this report. Read "
            "/home/pedro/.claude/skills/progress-report/SKILL.md Step 4b, run the goal step's "
            "extraction for THIS project with --project, and declare the count it actually "
            "returned. Then re-run this validation.")
    return errors


def check_report_states_its_reason(
        result: dict[str, Any], *, vault_target: str, local_target: str) -> list[str]:
    """When the accounting says there are no goals to show, require the REPORT
    BODY to say so.

    This is the half of "either the ledger recorded activity, or the report says
    on its face why it did not" that was previously only prose. `goal_accounting`
    lives in the result block, which is ephemeral — it is never written to the
    artifact. On the `ledger-error` path the builder is handed `goals_json: ""`
    and omits the `### Goals` section, the Resume Here goal bullet and
    `Planned vs actual` together, so the reader of the vault file gets a report
    with no goals and no reason: byte-for-byte the 2026-08-13 artifact.

    Mechanical on purpose. `cmd_result` already holds both the accounting value
    and the path that was written, so the claim can be checked against the file
    rather than asserted in a doc.

    Scoped to THIS RUN's block (`newest_block`), not the whole file: the
    day-file is append-on-top, so an unscoped search would let an earlier
    block's line — or another session's, on the same day-file — stand in for a
    run that said nothing.
    """
    accounting = str(result.get(GOAL_ACCOUNTING_LINE, ""))
    if accounting not in _GOAL_ACCOUNTING_NO_DATA:
        return []
    # Follow the report, not one specific flag: `--out` and `--no-project-file`
    # each leave one of the two targets empty, and both write a real report.
    # The two are byte-identical when both exist, so either answers this.
    target = vault_target or local_target
    if not target:
        written = [path for path in result.get("paths_written", []) if path]
        target = written[0] if written else ""
    if not target:
        return [
            f"REPORT REJECTED: goal_accounting is `{accounting}` for this run, so the report "
            "must state that reason in its body, but no written report path was available to "
            "check. Read /home/pedro/.claude/agents/progress-report-builder.md and re-run."]
    try:
        body = Path(target).read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError) as exc:
        # Fail closed: this check may never pass by being unable to look.
        return [
            f"REPORT REJECTED: the report at {target} could not be read ({exc}), so it cannot "
            f"be confirmed to state why goal_accounting is `{accounting}`. Read "
            "/home/pedro/.claude/agents/progress-report-builder.md and re-run."]
    # Only THIS run's block counts. The day-file carries every earlier block for
    # the same day underneath, so an unscoped search would let run 1's stated
    # reason answer for a run 2 that says nothing.
    block = newest_block(body)
    if block is None:
        # Fail closed: the report body exists but carries no `## ` block, so
        # there is no "this run's block" to hold to the contract.
        return [
            f"REPORT REJECTED: goal_accounting is `{accounting}` for this run, so the report "
            f"must state that reason in its body, but the report at {target} carries no `## ` "
            "block to check — every report block opens with `## <hh:mm> — <summary>`. The "
            "day-file was not written by scripts/dayfile-write.cjs. Read "
            "/home/pedro/.claude/agents/progress-report-builder.md (Writing the report) and "
            "re-run the builder."]
    if REPORT_GOAL_REASON_MARKER not in block:
        return [
            f"REPORT REJECTED: goal_accounting is `{accounting}` for this run, but this run's "
            f"block in the report at {target} does not state why it carries no goals. An "
            "earlier block in the same day-file does not answer for this one. The builder must render a "
            f"line beginning `{REPORT_GOAL_REASON_MARKER}` into the report body — a report "
            "that omits its Goals section without saying why is indistinguishable from one "
            "whose goal step silently never ran. Read "
            "/home/pedro/.claude/agents/progress-report-builder.md (Goals not recorded) and "
            "re-run the builder."]
    return []


def write_fresh_stamp(session_uuid: str, vault_target: str, local_target: str) -> str:
    """Atomically write the per-session freshness stamp precompact-report-guard.js
    dedups on. Lives here so the stamp can ONLY appear via a passing `result`
    validation — c4 compliance becomes structural, not prose (AUDIT-2026-07-17:
    7 clusters skipped the result check when it was a separate manual step)."""
    # One resolver, shared with the reader in `_load_stamp_ts`: the stamp this
    # writes is the freshness floor that reader measures receipts against, so
    # the two must never be able to disagree about where it lives.
    stamp_directory = str(stamp_dir())
    os.makedirs(stamp_directory, exist_ok=True)
    stamp_path = os.path.join(stamp_directory, session_uuid + ".json")
    payload = json.dumps({
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "vault_target": vault_target,
        "local_target": local_target,
    }) + "\n"
    # Unique tmp per invocation: a deterministic "<stamp>.tmp" races concurrent
    # same-session runs (the losing os.replace throws after validation passed).
    fd, tmp = tempfile.mkstemp(dir=stamp_directory, prefix=session_uuid + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp, stamp_path)
    return stamp_path


def write_run_start_marker(session_uuid: str) -> str:
    """Atomically write this run's start marker — half the freshness floor
    `check_goal_accounting` measures the goal-run receipt against.

    THE TIMESTAMP IS PRODUCED HERE AND CANNOT BE SUPPLIED. There is no `--ts`
    flag, no environment override and no test back door, because a floor is the
    one value in this system with nothing to check it against: `thin-session`
    can take its metrics as arguments only because the validator re-derives the
    thin condition from them, while an argument-supplied timestamp can be
    backdated to any moment and the floor simply collapses. A caller that wants
    a different marker time has to actually start a different run.

    Overwriting is intended, not a race to defend against: the marker means
    "this run started at", so a later run of the same session replaces it and
    the earlier run's receipt entries correctly fall below the new floor.

    This command is reachable by any caller, and that is safe in the only
    direction it can be abused: the timestamp is always `now`, so re-running it
    can only move the floor FORWARD, and a floor that moves forward rejects
    strictly more. There is no ordering of `run-start` calls that admits a
    receipt entry the guard would otherwise refuse — the worst a caller can do
    to themselves is invalidate their own honest goal step and have to re-run
    it. That asymmetry is why the command needs no caller check of its own.
    """
    marker_directory = str(run_marker_dir())
    os.makedirs(marker_directory, exist_ok=True)
    marker_path = os.path.join(marker_directory, session_uuid + ".json")
    payload = json.dumps({
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }) + "\n"
    # Unique tmp per invocation, exactly as `write_fresh_stamp` does it: a
    # deterministic "<marker>.tmp" races concurrent same-session runs, and the
    # losing `os.replace` would throw out of a command whose whole job is to
    # leave the file behind.
    fd, tmp = tempfile.mkstemp(dir=marker_directory, prefix=session_uuid + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp, marker_path)
    return marker_path


def cmd_run_start(args: argparse.Namespace) -> int:
    """Mark the start of this run. Called once at SKILL.md Step 2, before any
    `goals.py` call — a marker written after a legitimate receipt entry would put
    that entry below the floor and reject an honest run."""
    if not SESSION_ID_RE.fullmatch(args.session):
        # The session id becomes a filename here exactly as it does for the
        # stamp and the receipt, so it gets the same traversal check. Fails
        # closed rather than writing: a marker outside the marker directory is a
        # marker the reader will never find, which reads as a run that never
        # marked its start.
        print(json.dumps({"ok": False, "errors": [
            f"invalid --session id: {args.session!r}"]}, indent=2))
        return 1
    marker = write_run_start_marker(args.session)
    print(json.dumps({"ok": True, "marker": marker}, indent=2))
    return 0


def cmd_result(args: argparse.Namespace) -> int:
    text = read_text(args.agent_response)
    result = parse_result_block(text)
    errors = validate_paths(result, local_target=args.local_target, vault_target=args.vault_target)
    # Session id becomes a cache filename — reject anything that could traverse out
    # (fail closed: a stamp the guard can't find beats a stamp outside the cache dir).
    if args.stamp_session and not SESSION_ID_RE.fullmatch(args.stamp_session):
        errors.append(f"invalid --stamp-session id: {args.stamp_session!r}")
    # Appended to the SAME `errors` list every other check feeds, deliberately:
    # the stamp is written below only when that list is empty, so a goal-check
    # failure suppresses the freshness stamp by the existing mechanism rather
    # than by a second early-return path that would have to be kept in step
    # with it.
    errors.extend(check_goal_accounting(result, args.stamp_session, args.project))
    # The artifact-side half of the same guarantee: the accounting the block
    # declares has to be visible to whoever later opens the report itself.
    errors.extend(check_report_states_its_reason(
        result, vault_target=args.vault_target, local_target=args.local_target))
    payload = {"ok": not errors, "result": result, "errors": errors}
    if not errors and args.stamp_session:
        payload["stamp"] = write_fresh_stamp(args.stamp_session, args.vault_target, args.local_target)
    print(json.dumps(payload, indent=2))
    return 0 if not errors else 1


def cmd_report(args: argparse.Namespace) -> int:
    errors = validate_report_text(read_text(args.report))
    payload = {"ok": not errors, "errors": errors}
    print(json.dumps(payload, indent=2))
    return 0 if not errors else 1


def cmd_pair(args: argparse.Namespace) -> int:
    errors: list[str] = []
    if sha256(args.vault) != sha256(args.local):
        errors.append("local and vault reports are not byte-identical")
    payload = {"ok": not errors, "errors": errors}
    print(json.dumps(payload, indent=2))
    return 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    result_parser = subparsers.add_parser("result", help="validate builder return block and written paths")
    result_parser.add_argument("agent_response")
    result_parser.add_argument("--vault-target", default="")
    result_parser.add_argument("--local-target", default="")
    result_parser.add_argument("--stamp-session", default="",
                               help="session UUID. REQUIRED in effect: `result` fails closed without it, "
                                    "because the goal-ledger receipt check is keyed on it. On PASS it also "
                                    "writes the freshness stamp the precompact guard dedups on. Left as an "
                                    "optional flag with an empty default so its absence produces this "
                                    "module's own stated rejection rather than an argparse usage error.")
    result_parser.add_argument("--project", default="",
                               help="the project this report is about — the SAME --project value "
                                    "the goal step passed to goals.py. REQUIRED in effect: "
                                    "`result` fails closed without it, because the goal-run "
                                    "receipt is matched on this project's slug and a receipt from "
                                    "another project would otherwise validate this report. Left as "
                                    "an optional flag with an empty default so its absence "
                                    "produces this module's own stated rejection rather than an "
                                    "argparse usage error.")
    result_parser.set_defaults(func=cmd_result)

    report_parser = subparsers.add_parser("report", help="validate report markdown invariants")
    report_parser.add_argument("report")
    report_parser.set_defaults(func=cmd_report)

    run_start_parser = subparsers.add_parser(
        "run-start", help="write this run's start marker (SKILL.md Step 2)")
    run_start_parser.add_argument(
        "--session", required=True,
        help="session UUID. The marker is per-session and is OVERWRITTEN on each run: it "
             "records when THIS run started, and `result` measures the goal-run receipt against "
             "the later of it and the last accepted report's freshness stamp. Required, unlike "
             "`result`'s flags, because there is no report to reject later — this command does "
             "one thing and cannot do it without the id.")
    run_start_parser.set_defaults(func=cmd_run_start)

    pair_parser = subparsers.add_parser("pair", help="validate local/vault byte identity")
    pair_parser.add_argument("vault")
    pair_parser.add_argument("local")
    pair_parser.set_defaults(func=cmd_pair)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ValidationError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
