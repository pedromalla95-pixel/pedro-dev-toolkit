#!/usr/bin/env python3
"""Deterministic transcript resolver and digest for the progress-report skill."""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PROJECTS_ROOT = Path(r"/home/pedro/.claude/projects")
DEFAULT_VAULT_PROJECTS_ROOT = Path("/home/pedro/Documents/Necronomicon/01-Projects")
AMBIGUITY_WINDOW_SECONDS = 180
MAX_CANDIDATES = 8

# Repo cwd last-segment -> vault project folder name, for cases the normalized
# match cannot derive (the vault folder name diverges from the repo dir name).
# Derivable cases are listed too as explicit belt-and-suspenders; the resolver
# still confirms the target folder exists on disk before emitting it.
VAULT_PROJECT_ALIASES = {
    "cucina-inventory": "Cucina Inventory",
    "cucina-inventory-ship": "Cucina Inventory",
    "cucina-pos": "POS",
    "adm-cloud-mcp": "ADM Cloud MCP",
    "cucina-inventory-conteo": "Cucina Inventory Conteo",
    "contabilidad-2025": "Contabilidad",
}

RESEARCH_TOOLS = {"WebSearch", "WebFetch"}
RESEARCH_TOOL_PREFIXES = (
    "mcp__plugin_context7_context7__query-docs",
    "mcp__tavily",
    "mcp__firecrawl",
    "mcp__exa",
)
RESEARCH_AGENTS = {
    "gsd-phase-researcher",
    "gsd-project-researcher",
    "gsd-domain-researcher",
    "gsd-ai-researcher",
    "gsd-research-synthesizer",
    "plan-researcher",
}
CORRECTION_RE = re.compile(
    r"\b(instead|revert|undo|roll ?back|different approach|that('?s| is) wrong|don'?t do)\b",
    re.I,
)
DECISION_RE = re.compile(
    r"\b(decided|chose|chosen|going with|will use|opted for|approach:|trade-?off|because)\b",
    re.I,
)


class ProgressReportError(RuntimeError):
    """Raised when deterministic resolution or parsing cannot continue safely."""


@dataclass(frozen=True)
class TranscriptCandidate:
    path: Path
    mtime: float
    session_id: str | None = None
    cwd: str | None = None
    git_branch: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "mtime": datetime.fromtimestamp(self.mtime, timezone.utc).isoformat(),
            "session_id": self.session_id,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
        }


def norm_path(value: str | os.PathLike[str] | None) -> str:
    if value is None:
        return ""
    return str(value).strip().strip('"')


def transcript_dir_name(cwd: str) -> str:
    """Slug a cwd the way Claude Code names its project transcript folder:
    every run of non-alphanumeric chars collapses to a single "-" (the leading
    "/" of an absolute POSIX path becomes a leading "-").
    e.g. /home/pedro/Projects/X -> -home-pedro-Projects-X
    """
    return re.sub(r"[^A-Za-z0-9]+", "-", norm_path(cwd))


def resolve_vault_project(
    cwd: str,
    *,
    vault_projects_root: str | os.PathLike[str] = DEFAULT_VAULT_PROJECTS_ROOT,
) -> dict[str, Any]:
    """Deterministically map a project cwd to its Obsidian vault folder name.

    Lists the real 01-Projects/ folders and matches the cwd's last path segment
    against them by a normalized key (lowercase, non-alphanumerics stripped),
    consulting VAULT_PROJECT_ALIASES first for known non-derivable names. Emits
    exactly one match, or fails closed with candidates (never guesses).
    """
    root = Path(vault_projects_root)
    if not root.is_dir():
        return {"vault_project": None, "vault_project_candidates": []}

    existing = sorted(entry.name for entry in root.iterdir() if entry.is_dir())

    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    by_norm: dict[str, list[str]] = collections.defaultdict(list)
    for name in existing:
        by_norm[norm(name)].append(name)

    last_segment = Path(norm_path(cwd)).name
    if not last_segment:
        return {"vault_project": None, "vault_project_candidates": existing}

    alias = VAULT_PROJECT_ALIASES.get(last_segment)
    if alias is not None:
        alias_matches = by_norm.get(norm(alias), [])
        if len(alias_matches) == 1:
            return {"vault_project": alias_matches[0], "vault_project_candidates": []}
        if alias_matches:
            return {"vault_project": None, "vault_project_candidates": sorted(alias_matches)}
        # Aliased folder not on disk yet -> surface the alias as the sole candidate.
        return {"vault_project": None, "vault_project_candidates": [alias]}

    matches = by_norm.get(norm(last_segment), [])
    if len(matches) == 1:
        return {"vault_project": matches[0], "vault_project_candidates": []}
    if len(matches) > 1:
        return {"vault_project": None, "vault_project_candidates": sorted(matches)}
    # Zero matches -> fail closed, surfacing the full folder list for the skill to ask.
    return {"vault_project": None, "vault_project_candidates": existing}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def candidate_metadata(path: Path) -> TranscriptCandidate:
    session_id: str | None = None
    cwd: str | None = None
    branch: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if session_id is None:
                    session_id = entry.get("sessionId") or entry.get("session_id")
                if entry.get("cwd"):
                    cwd = str(entry["cwd"])
                if entry.get("gitBranch"):
                    branch = str(entry["gitBranch"])
    except OSError:
        pass
    return TranscriptCandidate(path=path, mtime=path.stat().st_mtime, session_id=session_id, cwd=cwd, git_branch=branch)


def resolve_transcript(
    cwd: str,
    *,
    transcript_override: str = "",
    projects_root: str | os.PathLike[str] = DEFAULT_PROJECTS_ROOT,
    ambiguity_window_seconds: int = AMBIGUITY_WINDOW_SECONDS,
) -> dict[str, Any]:
    override = norm_path(transcript_override or os.environ.get("CLAUDE_TRANSCRIPT_PATH") or os.environ.get("TRANSCRIPT_PATH"))
    if override:
        override_path = Path(override)
        if not override_path.is_file():
            raise ProgressReportError(f"explicit transcript not found: {override_path}")
        candidate = candidate_metadata(override_path)
        return {"status": "ok", "transcript": str(candidate.path), "source": "explicit", "candidate": candidate.to_json()}

    if not cwd:
        raise ProgressReportError("cwd is required when no explicit transcript is provided")

    folder = Path(projects_root) / transcript_dir_name(cwd)
    if not folder.is_dir():
        raise ProgressReportError(f"derived transcript folder not found: {folder}")

    jsonls = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_CANDIDATES]
    if not jsonls:
        raise ProgressReportError(f"no transcript JSONL files found in {folder}")

    candidates = [candidate_metadata(path) for path in jsonls]
    newest = candidates[0]
    plausible = [
        candidate
        for candidate in candidates
        if newest.mtime - candidate.mtime <= ambiguity_window_seconds
    ]
    if len(plausible) > 1:
        return {
            "status": "ambiguous",
            "source": "derived",
            "folder": str(folder),
            "message": "multiple recent transcripts are plausible; pass --transcript explicitly",
            "candidates": [candidate.to_json() for candidate in plausible],
        }

    return {"status": "ok", "transcript": str(newest.path), "source": "derived", "candidate": newest.to_json()}


def active_branch(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    uuid_entries: list[dict[str, Any]] = []
    by_uuid: dict[str, dict[str, Any]] = {}
    for entry in entries:
        uuid = entry.get("uuid")
        if uuid:
            uuid_entries.append(entry)
            by_uuid[str(uuid)] = entry

    active: set[str] = set()
    if uuid_entries:
        current: dict[str, Any] | None = uuid_entries[-1]
        iterations = 0
        while current is not None and iterations < 500000:
            iterations += 1
            uuid = current.get("uuid")
            if not uuid or uuid in active:
                break
            active.add(str(uuid))
            # A compaction severs the chain: the `compact_boundary` entry carries
            # parentUuid: null and records the real predecessor in logicalParentUuid.
            # Without this fallback the walk stops at the boundary and every
            # pre-compaction turn is counted as rewound — a 12-hour session then
            # digests as "thin". Only compact_boundary entries carry the field
            # (verified across a real 3,235-entry transcript), so this cannot bridge
            # a genuine rewind, which has no boundary to follow.
            parent = current.get("parentUuid") or current.get("logicalParentUuid")
            current = by_uuid.get(str(parent)) if parent else None

    active_messages = [
        entry
        for entry in uuid_entries
        if entry.get("uuid") in active and entry.get("type") in ("user", "assistant")
    ]
    all_messages = [entry for entry in uuid_entries if entry.get("type") in ("user", "assistant")]
    return active_messages, len(all_messages) - len(active_messages)


def content_blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    content = entry.get("message", {}).get("content", [])
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def digest_transcript(path: str, research_folder: str = "") -> dict[str, Any]:
    entries = read_jsonl(Path(norm_path(path)))
    active_messages, rewind_excluded = active_branch(entries)
    session_id: str | None = None
    transcript_cwd: str | None = None
    transcript_branch: str | None = None
    for entry in entries:
        if session_id is None:
            session_id = entry.get("sessionId") or entry.get("session_id")
        if entry.get("cwd"):
            transcript_cwd = str(entry["cwd"])
        if entry.get("gitBranch"):
            transcript_branch = str(entry["gitBranch"])

    events: list[dict[str, Any]] = []
    files_touched: collections.Counter[str] = collections.Counter()
    files_read: collections.Counter[str] = collections.Counter()
    errors: list[dict[str, Any]] = []
    todos_seen: list[Any] = []
    user_messages: list[tuple[str | None, str]] = []
    first_user: str | None = None
    last_assistant_text = ""
    recent_tools: list[str] = []
    correction_signals: list[tuple[str, str]] = []
    decision_signals: list[tuple[str, str]] = []
    agents_dispatched: list[dict[str, Any]] = []
    askuser_count = 0
    first_ts: str | None = None
    last_ts: str | None = None
    tool_use_order: dict[str, int] = {}

    research_signal_count = 0
    research_agent_dispatch = 0
    deep_research_invoked = False
    explore_dispatch = 0
    research_sources: list[str] = []
    research_queries: list[str] = []
    research_findings: list[str] = []
    last_research_idx = -1

    for entry in active_messages:
        role = entry.get("message", {}).get("role")
        timestamp = entry.get("timestamp")
        if timestamp:
            if first_ts is None:
                first_ts = str(timestamp)
            last_ts = str(timestamp)

        for block in content_blocks(entry):
            block_type = block.get("type")
            if block_type == "text":
                text = str(block.get("text", ""))
                if role == "user":
                    user_messages.append((timestamp, text))
                    if first_user is None:
                        first_user = text
                    if CORRECTION_RE.search(text):
                        correction_signals.append(("user", text[:240]))
                    if DECISION_RE.search(text):
                        decision_signals.append(("user", text[:240]))
                elif role == "assistant":
                    last_assistant_text = text
                    if CORRECTION_RE.search(text):
                        correction_signals.append(("assistant", text[:240]))
                    if DECISION_RE.search(text):
                        decision_signals.append(("assistant", text[:240]))
                    if last_research_idx >= 0 and len(events) > last_research_idx:
                        research_findings.append(text[:600])
            elif block_type == "tool_use":
                name = str(block.get("name", ""))
                tool_input = block.get("input", {}) or {}
                if not isinstance(tool_input, dict):
                    tool_input = {}
                tool_id = block.get("id")
                if tool_id:
                    tool_use_order[str(tool_id)] = len(events)

                if name in ("Edit", "Write", "NotebookEdit"):
                    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
                    if file_path:
                        files_touched[str(file_path)] += 1
                elif name in ("Read", "Grep", "Glob"):
                    file_path = tool_input.get("file_path") or tool_input.get("path")
                    if file_path:
                        files_read[str(file_path)] += 1
                if name == "TodoWrite":
                    todos_seen.append(tool_input.get("todos", []))
                if name in ("Task", "Agent"):
                    agent_type = tool_input.get("subagent_type")
                    desc = str(tool_input.get("description") or "")[:240]
                    agents_dispatched.append({"subagent_type": agent_type, "description": desc})
                    if agent_type in RESEARCH_AGENTS:
                        research_agent_dispatch += 1
                        if desc:
                            research_sources.append(f"agent: {agent_type} - {desc}")
                    if agent_type == "Explore":
                        explore_dispatch += 1
                is_research = name in RESEARCH_TOOLS or any(name.startswith(prefix) for prefix in RESEARCH_TOOL_PREFIXES)
                if name != "ToolSearch" and is_research:
                    research_signal_count += 1
                    last_research_idx = len(events)
                    query = (
                        tool_input.get("query")
                        or tool_input.get("url")
                        or tool_input.get("libraryName")
                        or tool_input.get("context7CompatibleLibraryID")
                    )
                    if query:
                        research_sources.append(str(query)[:200])
                        research_queries.append(str(query)[:120])
                if name == "Skill" and "deep-research" in str(tool_input.get("skill", "")):
                    deep_research_invoked = True
                if name == "AskUserQuestion":
                    askuser_count += 1

                recent_tools.append(name)
                events.append({"ts": timestamp, "tool": name, "input_keys": list(tool_input.keys())[:6]})
            elif block_type == "tool_result" and block.get("is_error"):
                tool_use_id = str(block.get("tool_use_id", ""))
                errors.append(
                    {
                        "id": tool_use_id,
                        "tool_order": tool_use_order.get(tool_use_id),
                        "snippet": str(block.get("content", ""))[:300],
                    }
                )

    research_triggered = (
        research_signal_count >= 3
        or research_agent_dispatch >= 1
        or deep_research_invoked
        or explore_dispatch >= 2
    )
    research_sources_dedup = list(dict.fromkeys(research_sources))

    touched_paths = set(files_touched)
    read_only = [(path, count) for path, count in files_read.most_common(40) if path not in touched_paths]

    vault_info = resolve_vault_project(transcript_cwd or "")

    def pairs(counter: collections.Counter[str], limit: int = 40) -> list[list[Any]]:
        return [[path, count] for path, count in counter.most_common(limit)]

    return {
        "session_id": session_id,
        "cwd": transcript_cwd,
        "git_branch": transcript_branch,
        "vault_project": vault_info["vault_project"],
        "vault_project_candidates": vault_info["vault_project_candidates"],
        "first_user": first_user,
        "user_msg_count": len(user_messages),
        "user_msgs_tail": [text[:400] for _, text in user_messages[-5:]],
        "last_assistant_text": last_assistant_text[:1200],
        "files_touched": pairs(files_touched),
        "files_read": pairs(files_read),
        "files_read_only": [[path, count] for path, count in read_only],
        "repeat_edited_files": [[path, count] for path, count in files_touched.most_common() if count >= 2],
        "errors": errors[:25],
        "correction_signals": correction_signals[:15],
        "todos_seen": todos_seen[-1] if todos_seen else [],
        "event_count": len(events),
        "recent_tools": recent_tools[-15:],
        "tool_histogram": dict(collections.Counter(event["tool"] for event in events).most_common()),
        "session_window": {"start": first_ts, "end": last_ts},
        "decision_signals": [list(signal) for signal in decision_signals[:12]],
        "agents_dispatched": agents_dispatched[:20],
        "askuser_count": askuser_count,
        "active_msg_count": len(active_messages),
        "rewind_excluded": rewind_excluded,
        "research": {
            "triggered": research_triggered,
            "signal_count": research_signal_count,
            "agent_dispatch": research_agent_dispatch,
            "deep_research": deep_research_invoked,
            "explore_dispatch": explore_dispatch,
            "sources": research_sources_dedup[:30],
            "queries": research_queries[:15],
            "findings": research_findings[:8],
            "research_folder": norm_path(research_folder),
        },
    }


def cmd_resolve(args: argparse.Namespace) -> int:
    try:
        result = resolve_transcript(
            args.cwd,
            transcript_override=args.transcript or "",
            projects_root=args.projects_root,
            ambiguity_window_seconds=args.ambiguity_window_seconds,
        )
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") == "ok" else 2
    except ProgressReportError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, indent=2))
        return 1


def cmd_digest(args: argparse.Namespace) -> int:
    result = digest_transcript(args.transcript, args.research_folder or "")
    print(json.dumps(result, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve_parser = subparsers.add_parser("resolve", help="resolve the active transcript path")
    resolve_parser.add_argument("--cwd", required=True)
    resolve_parser.add_argument("--transcript", default="")
    resolve_parser.add_argument("--projects-root", default=str(DEFAULT_PROJECTS_ROOT))
    resolve_parser.add_argument("--ambiguity-window-seconds", type=int, default=AMBIGUITY_WINDOW_SECONDS)
    resolve_parser.set_defaults(func=cmd_resolve)

    digest_parser = subparsers.add_parser("digest", help="digest an explicit transcript")
    digest_parser.add_argument("transcript")
    digest_parser.add_argument("research_folder", nargs="?", default="")
    digest_parser.set_defaults(func=cmd_digest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
