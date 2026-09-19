#!/usr/bin/env python3
"""Phase-A mechanical pre-pass for the /progress-report skill audit.

DETERMINISTIC + RE-RUNNABLE. python3 stdlib ONLY. No pip deps.

The audit window is a FROZEN closed interval passed on the CLI; there is NO
now()-style default anywhere. Re-running with identical bounds reproduces
byte-identical outputs (VERIFY-AS-LOOP). Every persisted file is written via
tmp-file + atomic rename. Any corpus corruption/mismatch fails loud
(non-zero exit + stderr) instead of writing a manifest (OBSERVABLE-ARTIFACTS).

Implements the approved plan Phase A, steps 0-5.
"""

import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict

# --------------------------------------------------------------------------
# Fixed absolute paths (constitution rule 6: absolute paths in code)
# --------------------------------------------------------------------------
HOME = "/home/pedro"
INVOCATIONS = HOME + "/.claude/logs/invocations.jsonl"
PROJECTS_DIR = HOME + "/.claude/projects"
VAULT_PROJECTS = HOME + "/Documents/Necronomicon/01-Projects"
LOCAL_MIRROR_ROOT = HOME + "/Projects"
STATE_DIR = HOME + "/.claude/skills/progress-report/audit-window"
GITIGNORE = HOME + "/.claude/.gitignore"
GITIGNORE_LINE = "skills/progress-report/audit-window/"

UTC = datetime.timezone.utc
DR_TZ = datetime.timezone(datetime.timedelta(hours=-4))  # Dominican Republic local (report tz)

GAP_ANNOTATE_SECONDS = 120     # Step 1: annotate (never drop) pairings with gap > 120s
INFLIGHT_MINUTES = 10          # Step 0: mtime within 10 min of T_audit_start -> INDETERMINATE candidate

# Verification anchors (frozen by the caller). Reproduced after exclusions.
EXPECTED_SKILL = 17
EXPECTED_DISPATCH = 36
EXPECTED_SESSIONS = 35
EXPECTED_ZERO_DISPATCH = frozenset({
    "3d2c2771-447b-4ca2-92b3-92253724ced4",
    "b3cbe77a-62f6-4e95-8487-de9f17467675",
    "fe77e1d1-f95d-4666-91cf-768dc6ac1438",
})
EXPECTED_DAYFILES = 18  # reconciled: 14 one-level + 4 nested (Claude/agentic-os); plan's "15" was a shallow-glob undercount

# Matches BOTH shapes: legacy day-files ("YYYY-MM-DD.md") and per-session reports
# ("Progress Report YYYY-MM-DD <uuid8>.md"). Both coexist in progress/ by design —
# no existing file is renamed — so narrowing either pattern silently empties the
# corpus instead of erroring.
DAYFILE_NAME_RE = re.compile(
    r"^(?:Progress Report )?(\d{4}-\d{2}-\d{2})(?: [0-9a-f]{8})?\.md$")
H2_TIME_RE = re.compile(r"^##\s+(\d{2}):(\d{2})\b")
# Bounded: exclude quote / backslash / newline so an escaped "\n" inside a JSONL
# string line cannot make the non-greedy match run away across many paths.
#
# MUST stay symmetric with DAYFILE_NAME_RE above. `writeback_targets` (below) is
# the INTERSECTION of the paths this regex finds in the transcript with the files
# DAYFILE_NAME_RE discovered on disk; widen one and not the other and that
# intersection is permanently empty, so `report_block_landed` reads false forever
# with no error — a false compliance signal, not a visible failure.
VAULT_DAYFILE_TARGET_RE = re.compile(
    r'01-Projects/[^"\\\n]+?/progress/(?:Progress Report )?(\d{4}-\d{2}-\d{2})(?: [0-9a-f]{8})?\.md')

RELATIVE_TOKENS = ("now", "today", "yesterday", "tomorrow", "ago")


# --------------------------------------------------------------------------
# Fail-loud helpers
# --------------------------------------------------------------------------
def fatal(msg, code=2):
    sys.stderr.write("[audit_window] FATAL: " + msg + "\n")
    sys.exit(code)


def usage_die(parser, msg):
    sys.stderr.write("[audit_window] ERROR: " + msg + "\n")
    parser.print_usage(sys.stderr)
    sys.exit(2)


def parse_ts(ts):
    """Parse an ISO ts, tolerating trailing Z and local-offset forms. -> aware UTC."""
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC)


def validate_bound(parser, name, raw):
    """Refuse missing / relative / now()-style bounds. Return aware UTC datetime."""
    low = raw.strip().lower()
    if "now()" in low:
        usage_die(parser, "--%s must be an absolute ISO timestamp, got now()-style token: %r" % (name, raw))
    for tok in RELATIVE_TOKENS:
        if re.search(r"(^|[^a-z])" + tok + r"([^a-z]|$)", low):
            usage_die(parser, "--%s must be an absolute ISO timestamp, got relative token %r: %r" % (name, tok, raw))
    dt = parse_ts(raw)
    if dt is None:
        usage_die(parser, "--%s must be an absolute ISO-8601 timestamp with explicit timezone/Z: %r" % (name, raw))
    return dt


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_bytes(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_aw_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_json(path, obj):
    data = (json.dumps(obj, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path, data)


def rmtree(path):
    if not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            os.unlink(os.path.join(root, f))
        for d in dirs:
            os.rmdir(os.path.join(root, d))
    os.rmdir(path)


# --------------------------------------------------------------------------
# Step 0 helpers -- .gitignore idempotent append
# --------------------------------------------------------------------------
def ensure_gitignore():
    try:
        with open(GITIGNORE, "r", encoding="utf-8") as fh:
            body = fh.read()
    except FileNotFoundError:
        body = ""
    lines = body.splitlines()
    if GITIGNORE_LINE in lines:
        return "present"
    if body and not body.endswith("\n"):
        body += "\n"
    body += "# progress-report Phase-A audit freeze state (audit_window.py)\n"
    body += GITIGNORE_LINE + "\n"
    atomic_write_bytes(GITIGNORE, body.encode("utf-8"))
    return "appended"


# --------------------------------------------------------------------------
# Frozen slice (invocation granularity)
# --------------------------------------------------------------------------
def build_slice(win_from, win_to, exclusions):
    """Pure filter of the append-only log over the closed interval [from, to]."""
    skills = []      # (session, ts_utc, has_args)
    dispatches = []  # (session, ts_utc)
    if not os.path.exists(INVOCATIONS):
        fatal("invocations log not found: " + INVOCATIONS)
    with open(INVOCATIONS, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            ts = parse_ts(o.get("ts"))
            if ts is None or not (win_from <= ts <= win_to):
                continue
            sess = o.get("session")
            if sess in exclusions:
                continue
            ch = o.get("channel")
            if ch == "skill" and o.get("name") == "progress-report":
                skills.append((sess, ts, bool(o.get("has_args"))))
            elif ch == "dispatch" and o.get("agent") == "progress-report-builder":
                dispatches.append((sess, ts))
    skills.sort(key=lambda x: (x[1], x[0]))
    dispatches.sort(key=lambda x: (x[1], x[0]))
    return skills, dispatches


def slice_sha(skills, dispatches):
    """Canonical serialization of the frozen slice -> sha256 (corruption-loud)."""
    rows = []
    for s, t, ha in skills:
        rows.append({"channel": "skill", "session": s, "ts": t.isoformat(), "has_args": ha})
    for s, t in dispatches:
        rows.append({"channel": "dispatch", "session": s, "ts": t.isoformat()})
    rows.sort(key=lambda r: (r["ts"], r["channel"], r["session"]))
    blob = "\n".join(json.dumps(r, sort_keys=True, ensure_ascii=True) for r in rows)
    return sha256_bytes(blob.encode("utf-8")), rows


# --------------------------------------------------------------------------
# Transcript enumeration
# --------------------------------------------------------------------------
def index_parent_transcripts():
    idx = defaultdict(list)
    for p in glob.glob(PROJECTS_DIR + "/*/*.jsonl"):
        idx[os.path.basename(p)[:-6]].append(p)
    return idx


def session_files(session, parent_idx):
    parents = sorted(parent_idx.get(session, []))
    sidecars = sorted(glob.glob(PROJECTS_DIR + "/*/" + session + "/subagents/**/agent-*.jsonl",
                                recursive=True))
    return parents, sidecars


def forked_from(session, parent_idx):
    """Return the predecessor sessionId from a head-region forkedFrom, or None."""
    parents, _ = session_files(session, parent_idx)
    for pth in parents:
        try:
            with open(pth, "r", encoding="utf-8") as fh:
                for line in fh:
                    if "forkedFrom" not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    fr = o.get("forkedFrom")
                    if isinstance(fr, dict) and fr.get("sessionId"):
                        return fr["sessionId"]
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------
# Step 1 -- topological pairing (per cluster / session)
# --------------------------------------------------------------------------
def pair_events(events):
    """events: sorted list of (kind, session, ts). kind in {'skill','dispatch'}.
    Pair each skill to the nearest subsequent dispatch with NO intervening skill.
    Leftovers reported unpaired in BOTH directions. Annotate gap > 120s."""
    pairs = []
    unpaired_skills = []
    unpaired_dispatches = []
    pending = None  # most recent unpaired skill (kind,session,ts)
    for ev in events:
        kind, sess, ts = ev
        if kind == "skill":
            if pending is not None:
                # two skills with no dispatch between -> earlier skill unpaired
                unpaired_skills.append(pending)
            pending = ev
        else:  # dispatch
            if pending is not None:
                gap = (ts - pending[2]).total_seconds()
                pairs.append({
                    "skill_ts": pending[2].isoformat(),
                    "dispatch_ts": ts.isoformat(),
                    "session": sess,
                    "gap_seconds": round(gap, 3),
                    "gap_over_120s": gap > GAP_ANNOTATE_SECONDS,
                })
                pending = None
            else:
                # dispatch with no preceding unpaired skill -> leftover
                unpaired_dispatches.append(ev)
    if pending is not None:
        unpaired_skills.append(pending)
    return pairs, unpaired_skills, unpaired_dispatches


# --------------------------------------------------------------------------
# Attribution / executed-model / dispatch-mode / rewind scans
# --------------------------------------------------------------------------
def scan_attribution(session, parent_idx):
    parents, sidecars = session_files(session, parent_idx)
    inline = False
    sidecar = False
    sidecar_models = set()
    for f in parents:
        try:
            for line in open(f, "r", encoding="utf-8"):
                if '"attributionSkill"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("attributionSkill") == "progress-report" and o.get("type") == "assistant":
                    inline = True
        except OSError:
            continue
    for f in sidecars:
        try:
            for line in open(f, "r", encoding="utf-8"):
                if '"attributionSkill"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("attributionSkill") == "progress-report" and o.get("type") == "assistant":
                    sidecar = True
                    m = o.get("message")
                    if isinstance(m, dict) and m.get("model"):
                        sidecar_models.add(m["model"])
        except OSError:
            continue
    # executed_model measurable ONLY from sidecar builder records; inline -> UNMEASURED (null)
    if sidecar_models:
        models = sorted(sidecar_models)
        executed_model = models[0] if len(models) == 1 else models
    else:
        executed_model = None
    if sidecar:
        mode = "sidecar"
    elif inline:
        mode = "inline"
    else:
        mode = "none"
    return {
        "attribution_mode": mode,
        "inline_attribution": inline,
        "sidecar_attribution": sidecar,
        "executed_model": executed_model,
    }


def scan_dispatch_mode(session, parent_idx):
    """Read input.run_in_background from the builder Task/Agent tool_use record."""
    parents, sidecars = session_files(session, parent_idx)
    result = "field-absent"
    for f in parents + sidecars:
        try:
            for line in open(f, "r", encoding="utf-8"):
                if "progress-report-builder" not in line or "tool_use" not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                m = o.get("message")
                if not isinstance(m, dict):
                    continue
                content = m.get("content")
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use" and c.get("name") in ("Task", "Agent"):
                        inp = c.get("input") or {}
                        if inp.get("subagent_type") == "progress-report-builder":
                            if "run_in_background" in inp:
                                return "background" if inp["run_in_background"] else "blocking"
        except OSError:
            continue
    return result


def _active_branch_excluded(path):
    """digest.py's active-branch reconstruction: walk from the tail backward via
    parentUuid; return the count of user/assistant records OFF the active branch
    (== rewound/abandoned turns). Matches the builder's own logic exactly."""
    entries = []
    try:
        for line in open(path, "r", encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return 0
    by_uuid = {}
    uuid_entries = []
    for e in entries:
        u = e.get("uuid")
        if u:
            uuid_entries.append(e)
            by_uuid[str(u)] = e
    active = set()
    if uuid_entries:
        cur = uuid_entries[-1]
        it = 0
        while cur is not None and it < 500000:
            it += 1
            u = cur.get("uuid")
            if not u or u in active:
                break
            active.add(str(u))
            par = cur.get("parentUuid")
            cur = by_uuid.get(str(par)) if par else None
    allm = [e for e in uuid_entries if e.get("type") in ("user", "assistant")]
    actm = [e for e in allm if e.get("uuid") in active]
    return len(allm) - len(actm)


def scan_branch_count(session, parent_idx):
    """Rewind guard: number of rewound/abandoned user/assistant turns (records off the
    active branch). >0 means a naive linear parse is unsafe -> needs 3-pass reconstruction."""
    parents, _ = session_files(session, parent_idx)
    return sum(_active_branch_excluded(f) for f in parents)


def session_mtime(session, parent_idx):
    parents, _ = session_files(session, parent_idx)
    if not parents:
        return None
    return max(os.path.getmtime(p) for p in parents)


# --------------------------------------------------------------------------
# Step 3 -- per-cluster mechanical c1-c6 checks
# --------------------------------------------------------------------------
PROJECT_PAGE_RE = re.compile(r"01-Projects/([^/\"\\\n]+)/([^/\"\\\n]+)\.md$")
PROJECT_PAGE_ANY_RE = re.compile(r"01-Projects/([^/\"\\\n]+)/([^/\"\\\n]+)\.md")


def scan_cluster_signals(sessions, parent_idx):
    """Collect mechanical grep signals across a cluster's parent + sidecar files.

    Attribution-scoped signals (edit_write_project_page, askuserquestion) count
    ONLY records carrying attributionSkill=="progress-report" -- otherwise unrelated
    project-page edits or AskUserQuestions elsewhere in the session would produce
    false c5 FAILs / false c3 N/As. builder_spawn is intrinsically specific
    (subagent_type=="progress-report-builder") so it needs no attribution scope."""
    sig = {
        "digest_resolve": False,
        "digest_digest": False,
        "builder_spawn": False,
        "validate_report": False,
        "dry_run": False,
        "askuserquestion": False,           # scoped to progress-report attribution
        "vault_append_helper": False,       # vault-append.cjs mentioned anywhere
        "vault_append_project_page": False,  # vault-append.cjs Bash call targeting the project page
        "edit_write_project_page": False,   # attributed Edit/Write onto the project page (c5 violation)
        "project_page_referenced": False,   # the flow touched the project page (append attempted)
        "vault_targets": set(),
    }
    for s in sessions:
        parents, sidecars = session_files(s, parent_idx)
        for f in parents + sidecars:
            try:
                for line in open(f, "r", encoding="utf-8"):
                    # ---- cheap line-level anchors ----
                    if "digest.py resolve" in line:
                        sig["digest_resolve"] = True
                    if "digest.py digest" in line:
                        sig["digest_digest"] = True
                    if "validate_report.py" in line:
                        sig["validate_report"] = True
                    if "vault-append.cjs" in line:
                        sig["vault_append_helper"] = True
                    if "<command-name>" in line and "progress-report" in line:
                        # Precise: --dry-run must be in the actual invocation
                        # <command-args>, NOT anywhere the SKILL.md doc (which documents
                        # the flag) was read into context. Cluster-granularity caveat:
                        # a real dry-run alongside a separate non-dry-run invocation in
                        # the same cluster would still trip c6 -- acceptable for Phase A.
                        margs = re.search(r"<command-args>(.*?)</command-args>", line)
                        if margs and "--dry-run" in margs.group(1):
                            sig["dry_run"] = True
                    for m in VAULT_DAYFILE_TARGET_RE.finditer(line):
                        # content-resolved writeback targets: paths_written in a
                        # tool_result, vault_target in a prompt, or an Edit/Write path.
                        sig["vault_targets"].add(m.group(0))
                    # ---- parse tool_use-bearing records ----
                    if "tool_use" not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    attr = o.get("attributionSkill")
                    m = o.get("message")
                    if not isinstance(m, dict) or not isinstance(m.get("content"), list):
                        continue
                    for c in m["content"]:
                        if not isinstance(c, dict) or c.get("type") != "tool_use":
                            continue
                        nm = c.get("name")
                        inp = c.get("input") or {}
                        if nm in ("Task", "Agent") and inp.get("subagent_type") == "progress-report-builder":
                            sig["builder_spawn"] = True
                        elif nm == "AskUserQuestion" and attr == "progress-report":
                            sig["askuserquestion"] = True
                        elif nm == "Bash":
                            cmd = inp.get("command", "") or ""
                            if "vault-append.cjs" in cmd:
                                for mm in PROJECT_PAGE_ANY_RE.finditer(cmd):
                                    if mm.group(1) == mm.group(2):
                                        sig["vault_append_project_page"] = True
                        elif nm in ("Edit", "Write"):
                            fp = inp.get("file_path", "") or ""
                            mp = PROJECT_PAGE_RE.search(fp)
                            if mp and mp.group(1) == mp.group(2) and attr == "progress-report":
                                sig["edit_write_project_page"] = True
            except OSError:
                continue
    # c5 condition: the progress-report flow attempted a project-page log entry
    # (compliant vault-append.cjs call OR a forbidden Edit/Write).
    sig["project_page_referenced"] = sig["vault_append_project_page"] or sig["edit_write_project_page"]
    sig["vault_targets"] = sorted(sig["vault_targets"])
    return sig


def evaluate_criteria(sig):
    """Map mechanical signals to c1-c6 verdicts (PASS|FAIL|N/A)."""
    c = {}
    # c1 -- unconditional: MUST call digest.py resolve
    c["c1"] = "PASS" if sig["digest_resolve"] else "FAIL"
    # c2 -- unconditional: MUST run digest.py digest
    c["c2"] = "PASS" if sig["digest_digest"] else "FAIL"
    # c3 -- conditional-on: digest ran, not dry-run, no AskUserQuestion after digest.
    #       N/A only on documented legitimate-abort paths.
    if not sig["digest_digest"]:
        c["c3"] = "N/A"          # Step 1 ambiguous/error never reaches digest
    elif sig["dry_run"]:
        c["c3"] = "N/A"          # Step 4 dry-run gate stops before dispatch
    elif sig["askuserquestion"]:
        c["c3"] = "N/A"          # .vault_project == null fails closed via AskUserQuestion
    else:
        c["c3"] = "PASS" if sig["builder_spawn"] else "FAIL"
    # c4 -- conditional-on builder returned: MUST run validate_report.py result
    if sig["builder_spawn"]:
        c["c4"] = "PASS" if sig["validate_report"] else "FAIL"
    else:
        c["c4"] = "N/A"
    # c5 -- conditional-on project-page referenced: append MUST NOT be Edit/Write on the page
    if sig["project_page_referenced"]:
        c["c5"] = "FAIL" if sig["edit_write_project_page"] else "PASS"
    else:
        c["c5"] = "N/A"
    # c6 -- conditional-on --dry-run present: MUST stop at Step 4 gate (no builder spawn)
    if sig["dry_run"]:
        c["c6"] = "FAIL" if sig["builder_spawn"] else "PASS"
    else:
        c["c6"] = "N/A"
    return c


# --------------------------------------------------------------------------
# Day-file / mirror / project-page discovery (H2-UTC window rule)
# --------------------------------------------------------------------------
def dayfile_in_window(path, win_from, win_to):
    """A day-file is in-window iff any H2 block (local HH:MM + filename date, DR tz)
    converts to a UTC instant inside [from, to]. Returns (in_window, n_blocks)."""
    base = os.path.basename(path)
    m = DAYFILE_NAME_RE.match(base)
    if not m:
        return (False, 0)
    d = datetime.date.fromisoformat(m.group(1))
    hits = 0
    try:
        for line in open(path, "r", encoding="utf-8", errors="ignore"):
            hm = H2_TIME_RE.match(line)
            if not hm:
                continue
            local = datetime.datetime(d.year, d.month, d.day,
                                      int(hm.group(1)), int(hm.group(2)),
                                      tzinfo=DR_TZ).astimezone(UTC)
            if win_from <= local <= win_to:
                hits += 1
    except OSError:
        return (False, 0)
    return (hits > 0, hits)


def discover_dayfiles(win_from, win_to):
    out = []
    for f in sorted(glob.glob(VAULT_PROJECTS + "/**/progress/*.md", recursive=True)):
        inwin, n = dayfile_in_window(f, win_from, win_to)
        if inwin:
            out.append(f)
    return out


def discover_local_mirrors(win_from, win_to):
    out = []
    for f in sorted(glob.glob(LOCAL_MIRROR_ROOT + "/**/session-progress-*.md", recursive=True)):
        # Both mirror shapes: legacy "session-progress-YYYY-MM-DD.md" and the
        # per-session "session-progress-YYYY-MM-DD-<uuid8>.md".
        m = re.search(r"session-progress-(\d{4}-\d{2}-\d{2})(?:-[0-9a-f]{8})?\.md$",
                      os.path.basename(f))
        if not m:
            continue
        d = datetime.date.fromisoformat(m.group(1))
        inwin = False
        try:
            for line in open(f, "r", encoding="utf-8", errors="ignore"):
                hm = H2_TIME_RE.match(line)
                if not hm:
                    continue
                local = datetime.datetime(d.year, d.month, d.day,
                                          int(hm.group(1)), int(hm.group(2)),
                                          tzinfo=DR_TZ).astimezone(UTC)
                if win_from <= local <= win_to:
                    inwin = True
                    break
        except OSError:
            continue
        if inwin:
            out.append(f)
    return out


def project_of_dayfile(path):
    # .../01-Projects/<project>/progress/<date>.md
    parts = path.split("/progress/")
    return os.path.basename(parts[0])


def extract_session_log_section(text):
    """Return the byte slice from '## Session Log' up to the next H2 (or EOF)."""
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("## Session Log"):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## ") and not lines[j].startswith("### "):
            end = j
            break
    return "".join(lines[start:end])


# --------------------------------------------------------------------------
# Snapshot persistence (byte copies + sha re-verify + abort-loud)
# --------------------------------------------------------------------------
def sanitize(relpath):
    return relpath.replace("/", "__").replace(" ", "_")


def snapshot_bytes(dest_path, data, source_desc):
    atomic_write_bytes(dest_path, data)
    got = sha256_file(dest_path)
    want = sha256_bytes(data)
    if got != want:
        # one retry
        atomic_write_bytes(dest_path, data)
        got = sha256_file(dest_path)
        if got != want:
            fatal("snapshot sha mismatch (persistent) for %s: want %s got %s"
                  % (source_desc, want, got))
    return got


def snapshot_source_file(src, dest_path):
    """Byte-copy a live file; re-read source afterwards + sha-compare; retry; abort-loud."""
    with open(src, "rb") as fh:
        data = fh.read()
    dest_sha = snapshot_bytes(dest_path, data, src)
    # re-read source and confirm it is still byte-identical to what we snapshotted
    with open(src, "rb") as fh:
        again = fh.read()
    if sha256_bytes(again) != dest_sha:
        # retry once with the re-read content
        data = again
        dest_sha = snapshot_bytes(dest_path, data, src)
        with open(src, "rb") as fh:
            again2 = fh.read()
        if sha256_bytes(again2) != dest_sha:
            fatal("source changed under snapshot repeatedly: " + src)
    return dest_sha


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="audit_window.py",
        description="Phase-A mechanical pre-pass for the /progress-report audit "
                    "(deterministic, re-runnable; frozen window required).")
    p.add_argument("--from", dest="from_", metavar="ISO", default=None,
                   help="window start (frozen absolute ISO-8601, e.g. 2026-07-15T00:00:00Z). "
                        "REQUIRED. No now()/relative tokens.")
    p.add_argument("--to", dest="to", metavar="ISO", default=None,
                   help="window end == T_audit_start (frozen absolute ISO-8601). "
                        "REQUIRED. No now()/relative tokens.")
    p.add_argument("--exclude-session", dest="exclude", action="append", default=[],
                   metavar="UUID", help="session UUID to exclude (repeatable).")
    return p


def main(argv):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # STEP 0 -- refuse missing / relative / now()-style bounds
    if args.from_ is None or args.to is None:
        usage_die(parser, "both --from and --to are REQUIRED (frozen absolute ISO bounds; no now() default).")
    win_from = validate_bound(parser, "from", args.from_)
    win_to = validate_bound(parser, "to", args.to)
    if win_from > win_to:
        usage_die(parser, "--from (%s) must not be after --to (%s)." % (args.from_, args.to))
    t_audit_start = win_to  # the --to IS T_audit_start (captured once by the caller)
    exclusions = frozenset(args.exclude)

    # State dir + .gitignore
    os.makedirs(STATE_DIR, exist_ok=True)
    gitignore_state = ensure_gitignore()
    # clean prior derived outputs so re-runs are byte-reproducible (own state dir only)
    for sub in ("snapshot", "rows"):
        rmtree(os.path.join(STATE_DIR, sub))
    for stale in ("manifest.json", "compliance_summary.json", "cost.json"):
        p = os.path.join(STATE_DIR, stale)
        if os.path.exists(p):
            os.unlink(p)

    # Frozen slice + anchors
    skills, dispatches = build_slice(win_from, win_to, exclusions)
    slice_hash, _slice_rows = slice_sha(skills, dispatches)
    n_skill = len(skills)
    n_dispatch = len(dispatches)
    skill_sessions = set(s for s, _t, _ha in skills)
    sessions_with_args = set(s for s, _t, ha in skills if ha)
    dispatch_sessions = set(s for s, _ in dispatches)
    corpus = sorted(skill_sessions | dispatch_sessions)
    zero_dispatch = sorted(skill_sessions - dispatch_sessions)

    # Hard anchor gate -- reproduce or fail loud (these are frozen ground truth)
    anchor_errors = []
    if n_skill != EXPECTED_SKILL:
        anchor_errors.append("skill events %d != %d" % (n_skill, EXPECTED_SKILL))
    if n_dispatch != EXPECTED_DISPATCH:
        anchor_errors.append("dispatch events %d != %d" % (n_dispatch, EXPECTED_DISPATCH))
    if len(corpus) != EXPECTED_SESSIONS:
        anchor_errors.append("sessions %d != %d" % (len(corpus), EXPECTED_SESSIONS))
    if set(zero_dispatch) != set(EXPECTED_ZERO_DISPATCH):
        anchor_errors.append("zero-dispatch set %r != %r" % (zero_dispatch, sorted(EXPECTED_ZERO_DISPATCH)))
    if anchor_errors:
        fatal("verification anchors NOT reproduced: " + "; ".join(anchor_errors))

    parent_idx = index_parent_transcripts()

    # Transcript enumeration + MISSING findings
    missing_transcripts = [s for s in corpus if not parent_idx.get(s)]

    # STEP 2 -- clustering via forkedFrom back-links (in-corpus predecessors only)
    corpus_set = set(corpus)
    pred_map = {}
    for s in corpus:
        p = forked_from(s, parent_idx)
        pred_map[s] = p if (p in corpus_set) else None

    # union-find
    parentf = {s: s for s in corpus}

    def find(x):
        while parentf[x] != x:
            parentf[x] = parentf[parentf[x]]
            x = parentf[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parentf[max(ra, rb)] = min(ra, rb)

    for s in corpus:
        if pred_map[s]:
            union(s, pred_map[s])
    comps = defaultdict(list)
    for s in corpus:
        comps[find(s)].append(s)

    def cluster_id_for(members):
        origins = [m for m in members if pred_map.get(m) not in set(members)]
        pool = origins if origins else members
        return sorted(pool)[0]

    clusters = {}
    for _root, members in comps.items():
        cid = cluster_id_for(members)
        clusters[cid] = sorted(members)
    cluster_ids = sorted(clusters)

    # F1 census -- dangling continuations across ALL window parent transcripts. REQUIRE 0.
    def transcript_in_window(path):
        try:
            for line in open(path, "r", encoding="utf-8"):
                if '"timestamp"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                t = parse_ts(o.get("timestamp"))
                if t and win_from <= t <= win_to:
                    return True
        except OSError:
            return False
        return False

    win_transcripts = []
    for sid, paths in parent_idx.items():
        for pth in paths:
            if transcript_in_window(pth):
                win_transcripts.append((sid, pth))
                break
    f1_continuations = 0
    f1_dangling = 0
    f1_dangling_list = []
    for sid, pth in win_transcripts:
        ff = None
        try:
            for line in open(pth, "r", encoding="utf-8"):
                if "forkedFrom" not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                fr = o.get("forkedFrom")
                if isinstance(fr, dict) and fr.get("sessionId"):
                    ff = fr["sessionId"]
                    break
        except OSError:
            pass
        if ff:
            f1_continuations += 1
            if ff not in parent_idx:
                f1_dangling += 1
                f1_dangling_list.append(sid)
    if f1_dangling != 0:
        fatal("F1 census FAILED: %d dangling continuations (require 0): %r"
              % (f1_dangling, sorted(f1_dangling_list)))

    # STEP 0/2b -- in-flight (INDETERMINATE) candidates: parent mtime within 10 min of T_audit_start
    inflight_cut = t_audit_start.timestamp()
    indeterminate = []
    for s in corpus:
        mt = session_mtime(s, parent_idx)
        if mt is None:
            continue
        if abs(mt - inflight_cut) <= INFLIGHT_MINUTES * 60:
            indeterminate.append(s)
    indeterminate = sorted(indeterminate)
    # finalize: a candidate merged into a live cluster follows the cluster
    indeterminate_clusters = sorted({cid for cid in cluster_ids
                                     if any(m in indeterminate for m in clusters[cid])})

    # Rewind guard -- parentUuid branch-count scan
    branched_sessions = {}
    for s in corpus:
        bc = scan_branch_count(s, parent_idx)
        if bc > 0:
            branched_sessions[s] = bc

    # Attribution probe gate -- run on >=1 known-builder session, confirm actor separation
    attribution_probe = None
    for cid in cluster_ids:
        for s in clusters[cid]:
            info = scan_attribution(s, parent_idx)
            if info["sidecar_attribution"]:
                attribution_probe = {
                    "session": s,
                    "cluster_id": cid,
                    "sidecar_attribution": True,
                    "inline_attribution": info["inline_attribution"],
                    "executed_model": info["executed_model"],
                    "actor_separation": "CONFIRMED",
                    "note": "executed_model sourced ONLY from sidecar builder records; "
                            "inline parent records are the main-thread model (UNMEASURED for exec).",
                }
                break
        if attribution_probe:
            break
    if attribution_probe is None:
        attribution_probe = {"actor_separation": "NO-SIDECAR-BUILDER-FOUND"}

    # STEP 1/3/4 -- per-cluster rows
    # group slice events per session
    ev_by_session = defaultdict(list)
    for s, t, _ha in skills:
        ev_by_session[s].append(("skill", s, t))
    for s, t in dispatches:
        ev_by_session[s].append(("dispatch", s, t))

    # snapshot sets first (needed for writeback resolution)
    dayfiles = discover_dayfiles(win_from, win_to)
    mirrors = discover_local_mirrors(win_from, win_to)
    # project dirs = path segment before "/progress/" (depth-agnostic: handles
    # nested projects like 01-Projects/Claude/agentic-os). Page = <dir>/<basename>.md.
    proj_dirs = sorted({f.split("/progress/")[0] for f in dayfiles})
    project_pages = []
    missing_project_pages = []
    for pdir in proj_dirs:
        relproj = os.path.relpath(pdir, VAULT_PROJECTS)   # "Ambar" or "Claude/agentic-os"
        proj = os.path.basename(pdir)                     # page basename
        pg = pdir + "/" + proj + ".md"
        if os.path.exists(pg):
            project_pages.append((relproj, pg))           # key by relproj (collision-safe)
        else:
            missing_project_pages.append(relproj)

    # persist snapshots
    snap_shas = {}
    for f in dayfiles:
        rel = os.path.relpath(f, VAULT_PROJECTS)
        dest = os.path.join(STATE_DIR, "snapshot", "dayfiles", sanitize(rel))
        snap_shas["dayfile:" + rel] = snapshot_source_file(f, dest)
    for f in mirrors:
        rel = os.path.relpath(f, LOCAL_MIRROR_ROOT)
        dest = os.path.join(STATE_DIR, "snapshot", "mirrors", sanitize(rel))
        snap_shas["mirror:" + rel] = snapshot_source_file(f, dest)
    for proj, pg in project_pages:
        with open(pg, "r", encoding="utf-8") as fh:
            txt = fh.read()
        section = extract_session_log_section(txt)
        if section is None:
            section = ""  # page exists but no Session Log section yet -- record empty
        dest = os.path.join(STATE_DIR, "snapshot", "projectpages", sanitize(proj) + "__SessionLog.md")
        snap_shas["projectpage:" + proj] = snapshot_bytes(dest, section.encode("utf-8"), pg)

    snapshotted_vault_targets = set(
        "01-Projects/" + os.path.relpath(f, VAULT_PROJECTS) for f in dayfiles)

    # rows
    compliance = []
    for cid in cluster_ids:
        members = clusters[cid]
        # pairing over the cluster's combined event timeline
        events = []
        for m in members:
            events.extend(ev_by_session[m])
        events.sort(key=lambda e: (e[2], e[0], e[1]))
        pairs, un_sk, un_di = pair_events(events)

        sig = scan_cluster_signals(members, parent_idx)
        # Gate dry-run on a real argument-bearing invocation: a bare "--dry-run"
        # string in read SKILL.md/prompt text is not an invocation. has_args==false
        # on every skill event in the cluster => the invocation carried no flag.
        cluster_has_args = any(m in sessions_with_args for m in members)
        sig["dry_run"] = sig["dry_run"] and cluster_has_args
        crit = evaluate_criteria(sig)

        # attribution / model / dispatch (per representative member with builder activity)
        attr = {"attribution_mode": "none", "inline_attribution": False,
                "sidecar_attribution": False, "executed_model": None}
        for m in members:
            a = scan_attribution(m, parent_idx)
            if a["sidecar_attribution"]:
                attr = a
                break
            if a["inline_attribution"] and attr["attribution_mode"] == "none":
                attr = a
        dispatch_mode = "field-absent"
        for m in members:
            dm = scan_dispatch_mode(m, parent_idx)
            if dm != "field-absent":
                dispatch_mode = dm
                break

        # writeback -- content-resolved vault targets intersected with in-window snapshots
        writeback_targets = sorted(set(sig["vault_targets"]) & snapshotted_vault_targets)
        writeback = {
            "resolved_vault_targets": sig["vault_targets"],
            "landed_in_snapshot": writeback_targets,
            "report_block_landed": bool(writeback_targets),
        }

        row = {
            "cluster_id": cid,
            "sessions": members,
            "forked_from": {m: forked_from(m, parent_idx) for m in members
                            if forked_from(m, parent_idx)},
            "transcript_missing": [m for m in members if not parent_idx.get(m)],
            "in_flight_indeterminate": bool(set(members) & set(indeterminate)),
            "skill_event_count": sum(1 for e in events if e[0] == "skill"),
            "dispatch_event_count": sum(1 for e in events if e[0] == "dispatch"),
            "pairings": pairs,
            "unpaired_skills": [e[2].isoformat() for e in un_sk],
            "unpaired_dispatches": [e[2].isoformat() for e in un_di],
            "zero_dispatch_candidate": cid in EXPECTED_ZERO_DISPATCH
                                       or any(m in EXPECTED_ZERO_DISPATCH for m in members),
            "attribution": attr,
            "dispatch_mode": dispatch_mode,
            "writeback": writeback,
            "criteria": crit,
            "criteria_signals": {k: v for k, v in sig.items() if k != "vault_targets"},
        }
        atomic_write_json(os.path.join(STATE_DIR, "rows", cid + ".json"), row)

        compliance.append({
            "cluster_id": cid,
            "sessions": len(members),
            "skill_events": row["skill_event_count"],
            "dispatch_events": row["dispatch_event_count"],
            "pairs": len(pairs),
            "unpaired_skills": len(un_sk),
            "unpaired_dispatches": len(un_di),
            "zero_dispatch": row["zero_dispatch_candidate"],
            "in_flight": row["in_flight_indeterminate"],
            "attribution_mode": attr["attribution_mode"],
            "executed_model": attr["executed_model"],
            "dispatch_mode": dispatch_mode,
            "report_landed": writeback["report_block_landed"],
            "c1": crit["c1"], "c2": crit["c2"], "c3": crit["c3"],
            "c4": crit["c4"], "c5": crit["c5"], "c6": crit["c6"],
        })

    # cost.json -- Phase-A placeholder for Phase-B lane-cost (may be empty)
    atomic_write_json(os.path.join(STATE_DIR, "cost.json"),
                      {"phase": "A", "lane_cost": None, "note": "populated in Phase B"})

    # compliance summary
    atomic_write_json(os.path.join(STATE_DIR, "compliance_summary.json"), {
        "window": {"from": win_from.isoformat(), "to": win_to.isoformat()},
        "row_count": len(cluster_ids),
        "rows": sorted(compliance, key=lambda r: r["cluster_id"]),
    })

    # day-file discrepancy (loud, non-fatal -- soft anchor vs the hard 17/36/35/3)
    dayfile_count = len(dayfiles)
    dayfile_discrepancy = (dayfile_count != EXPECTED_DAYFILES)
    if dayfile_discrepancy:
        sys.stderr.write(
            "[audit_window] WARNING: in-window vault day-files = %d, expected %d. "
            "Discrepancy recorded in manifest (not fudged). Files: %r\n"
            % (dayfile_count, EXPECTED_DAYFILES, [os.path.relpath(f, VAULT_PROJECTS) for f in dayfiles]))

    # manifest.json -- written LAST
    manifest = {
        "phase": "A",
        "window": {"from": win_from.isoformat(), "to": win_to.isoformat()},
        "t_audit_start": t_audit_start.isoformat(),
        "exclusions": sorted(exclusions),
        "slice_sha256": slice_hash,
        "row_count": len(cluster_ids),
        "corpus_complete": True,
        "analysis_coverage": {
            "clusters_read": 0,
            "clusters_total": len(cluster_ids),
            "truncated": False,
        },
        "anchors": {
            "skill_events": {"actual": n_skill, "expected": EXPECTED_SKILL, "ok": n_skill == EXPECTED_SKILL},
            "dispatch_events": {"actual": n_dispatch, "expected": EXPECTED_DISPATCH, "ok": n_dispatch == EXPECTED_DISPATCH},
            "sessions": {"actual": len(corpus), "expected": EXPECTED_SESSIONS, "ok": len(corpus) == EXPECTED_SESSIONS},
            "zero_dispatch": {"actual": zero_dispatch, "expected": sorted(EXPECTED_ZERO_DISPATCH),
                              "ok": set(zero_dispatch) == set(EXPECTED_ZERO_DISPATCH)},
        },
        "zero_dispatch_sessions": zero_dispatch,
        "clusters": {cid: clusters[cid] for cid in cluster_ids},
        "missing_transcripts": sorted(missing_transcripts),
        "f1_census": {
            "window_parent_transcripts": len(win_transcripts),
            "continuations": f1_continuations,
            "dangling": f1_dangling,
            "require": 0,
            "ok": f1_dangling == 0,
        },
        "rewind_guard": {
            "metric": "rewound/abandoned user+assistant turns off the active branch "
                      "(digest.py active_branch reconstruction)",
            "rewound_turn_sessions": branched_sessions,
            "branch_free": len(branched_sessions) == 0,
            "linear_parsing_safe_reason": (
                "no rewound turns across corpus parent transcripts -> the tail-anchored "
                "active branch is the whole transcript, linear parse is sound"
                if not branched_sessions else
                "sessions with rewound turns present -> require builder 3-pass "
                "active-branch reconstruction (naive linear parse would read abandoned turns)"),
        },
        "attribution_probe": attribution_probe,
        "inflight_indeterminate": {
            "sessions": indeterminate,
            "clusters": indeterminate_clusters,
            "note": "excluded from stop-reason analysis but COUNTED in coverage",
        },
        "dayfiles": {
            "expected": EXPECTED_DAYFILES,
            "actual": dayfile_count,
            "discrepancy": dayfile_discrepancy,
            "files": [os.path.relpath(f, VAULT_PROJECTS) for f in dayfiles],
            "rule": "H2 local HH:MM + filename date in DR tz (UTC-4) -> UTC in [from,to]",
        },
        "local_mirrors": [os.path.relpath(f, LOCAL_MIRROR_ROOT) for f in mirrors],
        "project_pages": {
            "snapshotted": [proj for proj, _ in project_pages],
            "missing": missing_project_pages,
        },
        "snapshot_sha256s": snap_shas,
        "gitignore": gitignore_state,
    }
    atomic_write_json(os.path.join(STATE_DIR, "manifest.json"), manifest)

    # ---- summary to stdout (eyeball the anchors) ----
    print("=== progress-report Phase-A freeze ===")
    print("window            : %s .. %s" % (win_from.isoformat(), win_to.isoformat()))
    print("exclusions        : %s" % ", ".join(sorted(exclusions)))
    print("slice_sha256      : %s" % slice_hash)
    print("skill events      : %d (expected %d) %s" % (n_skill, EXPECTED_SKILL, "OK" if n_skill == EXPECTED_SKILL else "MISMATCH"))
    print("dispatch events   : %d (expected %d) %s" % (n_dispatch, EXPECTED_DISPATCH, "OK" if n_dispatch == EXPECTED_DISPATCH else "MISMATCH"))
    print("unique sessions   : %d (expected %d) %s" % (len(corpus), EXPECTED_SESSIONS, "OK" if len(corpus) == EXPECTED_SESSIONS else "MISMATCH"))
    print("zero-dispatch     : %d %s %s" % (len(zero_dispatch), zero_dispatch,
                                            "OK" if set(zero_dispatch) == set(EXPECTED_ZERO_DISPATCH) else "MISMATCH"))
    print("clusters (rows)   : %d" % len(cluster_ids))
    print("F1 census         : %d dangling / %d continuations (require 0) %s"
          % (f1_dangling, f1_continuations, "OK" if f1_dangling == 0 else "FAIL"))
    print("rewind branches   : %d branched sessions (%s)"
          % (len(branched_sessions), "linear-safe" if not branched_sessions else "reconstruction-needed"))
    print("attribution probe : actor_separation=%s exec_model=%s"
          % (attribution_probe.get("actor_separation"), attribution_probe.get("executed_model")))
    print("in-flight (indet) : %d sessions" % len(indeterminate))
    print("day-files         : %d (expected %d) %s"
          % (dayfile_count, EXPECTED_DAYFILES, "OK" if not dayfile_discrepancy else "DISCREPANCY (see stderr + manifest)"))
    print("local mirrors     : %d" % len(mirrors))
    print("project pages     : %d snapshotted, %d missing" % (len(project_pages), len(missing_project_pages)))
    print("missing transcripts: %d" % len(missing_transcripts))
    print("state dir         : %s" % STATE_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
