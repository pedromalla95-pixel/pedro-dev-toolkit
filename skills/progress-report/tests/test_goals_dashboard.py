#!/usr/bin/env python3
"""Tests for `goals.py dashboard` — the cross-project Goals & To-Do page.

Scratch paths only; never touches the live vault or the live ledger dir.
Every test that asserts a REFUSAL also asserts the target is byte-unchanged —
a refusal that still wrote something is the failure mode worth catching.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
GOALS = HERE.parent / "scripts" / "goals.py"
VAULT = pathlib.Path.home() / "Documents" / "Necronomicon"

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
    spec = importlib.util.spec_from_file_location("g", GOALS)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    return g


def ledger(project, goals):
    return {"schema": 1, "project": project, "updated": "2026-08-18T00:00:00Z",
            "goals": goals}


def goal(gid, text, status="open", owner="aaaaaaaa-1111-1111-1111-111111111111",
         origin="inferred", history=None):
    return {"id": gid, "text": text, "level": "goal", "parent": None,
            "status": status, "owner_session": owner, "origin": origin,
            "created": "2026-08-18T00:00:00Z", "last_seen": "2026-08-18T00:00:00Z",
            "history": history or []}


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="dash-test-"))
    assert VAULT not in tmp.parents, "FATAL: scratch landed inside the live vault"
    g = load()
    led = tmp / "ledgers"
    led.mkdir()
    g.LEDGER_DIR = led
    # LOCK_DIR must be redirected too, not just LEDGER_DIR. Groups 12-15 and 20
    # mkdir/rmtree the dashboard lock and overwrite its `owner` token; against the
    # real ~/.claude/cache that evicts a concurrent session's LIVE lock mid-write
    # and drives it into Abort(5). goals.py reads both globals at call time, so
    # rebinding here is sufficient.
    locks = tmp / "locks"
    locks.mkdir()
    g.LOCK_DIR = locks
    out = tmp / "Goals & To-Do.md"

    (led / "alpha.json").write_text(json.dumps(ledger("Alpha", [
        goal("g1", "open one"), goal("g2", "done one", status="done"),
        goal("g3", "parked one", status="parked"),
        goal("g4", "dropped one", status="dropped")])))
    (led / "beta.json").write_text(json.dumps(ledger("Beta", [goal("g5", "beta goal")])))

    print("== [GREEN] 1. first run CREATES the page ==")
    r = g.cmd_dashboard(str(out))
    check("exit ok", r["ok"], True)
    check("file created", out.exists(), True)
    txt = out.read_text()
    check("managed_by stamped", "managed_by: goals-dashboard" in txt, True)
    check("sentinel pair present", txt.count("%%GOALS:BEGIN%%") == 1 and txt.count("%%GOALS:END%%") == 1, True)
    check("both projects rendered", ("## Alpha" in txt and "## Beta" in txt), True)

    print("\n== [EDGE] 2. parked and dropped are SEPARATE headings ==")
    check("Parked heading", "### Parked (1)" in txt, True)
    check("Dropped heading", "### Dropped (1)" in txt, True)
    check("not merged under one", "### Parked (2)" in txt, False)

    print("\n== [EDGE] 3. parked goals stay VISIBLE (guards the autopark decay) ==")
    check("parked goal text present", "parked one" in txt, True)

    print("\n== [GREEN] 4. an unchanged render does not touch the file ==")
    m1, s1 = out.stat().st_mtime_ns, out.read_bytes()
    r = g.cmd_dashboard(str(out))
    check("reports unchanged", r.get("skipped"), "unchanged")
    check("mtime not bumped", out.stat().st_mtime_ns, m1)
    check("bytes identical", out.read_bytes(), s1)

    print("\n== [GREEN] 5. a real change DOES write ==")
    (led / "beta.json").write_text(json.dumps(ledger("Beta", [
        goal("g5", "beta goal"), goal("g6", "brand new goal")])))
    r = g.cmd_dashboard(str(out))
    check("wrote", bool(r.get("written")), True)
    check("new goal present", "brand new goal" in out.read_text(), True)

    print("\n== [EDGE] 6. content outside the sentinels survives ==")
    t = out.read_text().replace("%%GOALS:BEGIN%%", "KEEP-ABOVE\n\n%%GOALS:BEGIN%%")
    out.write_text(t + "\n## Hand-written\n\nKEEP-BELOW\n")
    (led / "beta.json").write_text(json.dumps(ledger("Beta", [goal("g5", "beta goal")])))
    g.cmd_dashboard(str(out))
    t2 = out.read_text()
    check("text above sentinels kept", "KEEP-ABOVE" in t2, True)
    check("text below sentinels kept", "KEEP-BELOW" in t2, True)

    print("\n== [RED] 7. an unmanaged file is refused, not overwritten ==")
    un = tmp / "unmanaged.md"
    un.write_text("---\ntype: dashboard\nmanaged_by: someone-else\n---\n\n# Theirs\n")
    before = un.read_bytes()
    try:
        g.cmd_dashboard(str(un))
        check("refused with Abort", False, True)
    except g.Abort as exc:
        check("refused with Abort", "refusing to overwrite unmanaged" in str(exc), True)
    check("unmanaged file byte-unchanged", un.read_bytes(), before)

    print("\n== [RED] 8. ambiguous sentinels are refused ==")
    amb = tmp / "ambiguous.md"
    amb.write_text("---\ntype: dashboard\nmanaged_by: goals-dashboard\n---\n\n"
                   "%%GOALS:BEGIN%%\nx\n%%GOALS:END%%\n%%GOALS:BEGIN%%\ny\n%%GOALS:END%%\n")
    before = amb.read_bytes()
    try:
        g.cmd_dashboard(str(amb))
        check("refused with Abort", False, True)
    except g.Abort as exc:
        check("refused with Abort", "ambiguous sentinels" in str(exc), True)
    check("ambiguous file byte-unchanged", amb.read_bytes(), before)

    print("\n== [RED] 9. one INVALID-JSON ledger costs one section, not the page ==")
    (led / "gamma.json").write_text("not json {{{")
    out2 = tmp / "degrade.md"
    r = g.cmd_dashboard(str(out2))
    t = out2.read_text()
    check("still exits ok", r["ok"], True)
    check("healthy projects rendered", ("## Alpha" in t and "## Beta" in t), True)
    check("bad ledger flagged", "_ledger unreadable" in t, True)

    print("\n== [RED] 10. a JSON-VALID, SHAPE-INVALID ledger is caught too ==")
    # `goals` as a dict: read_ledger accepts it, _check_ledger_shape rejects it.
    # Fails against a try/except that wraps only read_ledger.
    (led / "gamma.json").unlink()
    (led / "delta.json").write_text(json.dumps(
        {"schema": 1, "project": "Delta", "updated": None, "goals": {"not": "a list"}}))
    out3 = tmp / "degrade2.md"
    r = g.cmd_dashboard(str(out3))
    t = out3.read_text()
    check("still exits ok", r["ok"], True)
    check("shape error reported", "is not a list" in t, True)
    check("healthy projects still rendered", ("## Alpha" in t and "## Beta" in t), True)
    check("unreadable counted", r["stats"]["unreadable"], 1)
    (led / "delta.json").unlink()

    print("\n== [RED] 11. a stale render never overwrites a fresher page ==")
    out4 = tmp / "stale.md"
    g.cmd_dashboard(str(out4))
    before = out4.read_bytes()
    real_build = g._build_dashboard_region

    def racing_build():
        res = real_build()
        p = led / "alpha.json"
        p.write_text(p.read_text())  # a concurrent session commits mid-render
        return res

    g._build_dashboard_region = racing_build
    r = g.cmd_dashboard(str(out4))
    g._build_dashboard_region = real_build
    check("write skipped", r.get("skipped"), "stale render")
    check("page byte-unchanged", out4.read_bytes(), before)

    print("\n== [EDGE] 12. dead lock holder is evicted by LIVENESS, fast ==")
    lock = g.LOCK_DIR / g.DASHBOARD_LOCK_NAME
    shutil.rmtree(lock, ignore_errors=True)
    lock.mkdir(parents=True)
    (lock / "owner").write_text("999999-deadbeef")
    t0 = time.monotonic()
    with g.dashboard_lock():
        pass
    dt = time.monotonic() - t0
    check(f"evicted in well under the {g.LOCK_CAP_MS}ms cap ({dt*1000:.0f}ms)", dt < 1.0, True)

    print("\n== [EDGE] 13. a LIVE lock holder is never robbed ==")
    shutil.rmtree(lock, ignore_errors=True)
    lock.mkdir(parents=True)
    (lock / "owner").write_text(f"{os.getpid()}-stillalive")
    try:
        with g.dashboard_lock():
            check("live holder NOT robbed", False, True)
    except g.Abort as exc:
        check("live holder NOT robbed -> Abort(3)", exc.code, 3)
        check("message names the timeout", "lock timeout" in str(exc), True)
    shutil.rmtree(lock, ignore_errors=True)

    print("\n== [GREEN] 14. re-entrancy names the CALLER, not ledger_lock ==")
    try:
        with g.dashboard_lock():
            with g.dashboard_lock():
                pass
        check("re-entrancy caught", False, True)
    except g.Abort as exc:
        check("names dashboard_lock", "dashboard_lock" in str(exc), True)
        check("does NOT say ledger_lock", "ledger_lock" in str(exc), False)

    print("\n== [GREEN] 15. ledger_lock is unchanged by the _lock_at extraction ==")
    try:
        with g.ledger_lock("scratch-probe"):
            with g.ledger_lock("scratch-probe"):
                pass
        check("ledger_lock re-entrancy caught", False, True)
    except g.Abort as exc:
        check("ledger_lock message preserved", "ledger_lock('scratch-probe')" in str(exc), True)
    with g.ledger_lock("scratch-probe"):
        held = g.lock_path("scratch-probe").exists()
    check("acquire then release", (held, g.lock_path("scratch-probe").exists()), (True, False))

    print("\n== [EDGE] 16. an empty ledger renders a section, not silence ==")
    (led / "empty.json").write_text(json.dumps(ledger("EmptyProj", [])))
    out5 = tmp / "empty.md"
    g.cmd_dashboard(str(out5))
    t = out5.read_text()
    check("empty project has a section", "## EmptyProj" in t, True)
    check("says no live goals", "_no live goals_" in t, True)

    print("\n== [EDGE] 17. malformed goal fields render without raising ==")
    (led / "weird.json").write_text(json.dumps(ledger("Weird", [
        {"id": "w1", "text": "ok text", "status": 42, "owner_session": None,
         "history": [{"session": None, "from": "free text", "to": "also free text",
                      "reason": "approved-change"}]},
        {"id": "w2", "text": "second", "status": "open", "owner_session": "",
         "history": "not even a list"}])))
    out6 = tmp / "weird.md"
    try:
        g.cmd_dashboard(str(out6))
        check("rendered without raising", True, True)
    except Exception as exc:  # noqa: BLE001 - the point is that nothing escapes
        check(f"rendered without raising ({type(exc).__name__}: {exc})", False, True)
    t = out6.read_text()
    check("non-string status not printed raw", "· `42`" in t, False)

    print("\n== [GREEN] 18. `updated:` is content-derived, NOT clock-derived ==")
    # Test 4 cannot see this: both its runs are same-day, so a clock-stamped
    # `updated:` and a carried-forward one are byte-identical. Seed a STALE date
    # and assert the no-op gate still fires — a clock stamp would rewrite it.
    out7 = tmp / "updated.md"
    g.cmd_dashboard(str(out7))
    stale = out7.read_text().replace(
        f"updated: {time.strftime('%Y-%m-%d')}", "updated: 2020-01-01")
    check("fixture actually went stale", "updated: 2020-01-01" in stale, True)
    out7.write_text(stale)
    m7, s7 = out7.stat().st_mtime_ns, out7.read_bytes()
    r = g.cmd_dashboard(str(out7))
    check("still reports unchanged", r.get("skipped"), "unchanged")
    check("stale date NOT refreshed", "updated: 2020-01-01" in out7.read_text(), True)
    check("mtime not bumped", out7.stat().st_mtime_ns, m7)
    check("bytes identical", out7.read_bytes(), s7)

    print("\n== [EDGE] 19. a target changed underfoot is not clobbered ==")
    # Touch the target between the writer's read and its re-stat, simulating an
    # Obsidian autosave. `out.parent.mkdir` runs in exactly that window.
    out8 = tmp / "underfoot.md"
    g.cmd_dashboard(str(out8))
    (led / "beta.json").write_text(json.dumps(ledger("Beta", [
        goal("g5", "beta goal"), goal("g9", "forces a real write")])))
    real_mkdir = pathlib.Path.mkdir
    fired = []

    def racing_mkdir(self, *a, **k):
        res = real_mkdir(self, *a, **k)
        if self == out8.parent and not fired:
            fired.append(1)
            out8.write_text(out8.read_text() + "\n<!-- obsidian autosave -->\n")
        return res

    pathlib.Path.mkdir = racing_mkdir
    try:
        r = g.cmd_dashboard(str(out8))
    finally:
        pathlib.Path.mkdir = real_mkdir
    check("race actually fired", bool(fired), True)
    check("write discarded", r.get("skipped"), "changed underfoot")
    check("outside edit survived", "obsidian autosave" in out8.read_text(), True)
    check("new goal NOT spliced in", "forces a real write" in out8.read_text(), False)
    # "next run reconciles" must be true — the page cannot wedge permanently.
    r = g.cmd_dashboard(str(out8))
    check("next run writes", bool(r.get("written")), True)
    check("new goal now present", "forces a real write" in out8.read_text(), True)
    check("no .tmp litter left behind",
          [p.name for p in tmp.iterdir() if ".tmp-" in p.name], [])

    print("\n== [RED] 20. a lock lost mid-write aborts the write ==")
    # Replace the lock's owner token in the same read..rename window.
    out9 = tmp / "lostlock.md"
    g.cmd_dashboard(str(out9))
    before = out9.read_bytes()
    (led / "beta.json").write_text(json.dumps(ledger("Beta", [
        goal("g5", "beta goal"), goal("g10", "must never land")])))
    owner = g.LOCK_DIR / g.DASHBOARD_LOCK_NAME / "owner"
    fired2 = []

    def robbing_mkdir(self, *a, **k):
        res = real_mkdir(self, *a, **k)
        if self == out9.parent and not fired2:
            fired2.append(1)
            owner.write_text("999999-someone-else")
        return res

    pathlib.Path.mkdir = robbing_mkdir
    try:
        g.cmd_dashboard(str(out9))
        check("refused with Abort(5)", False, True)
    except g.Abort as exc:
        check("refused with Abort(5)", exc.code, 5)
        check("message says NOT modified", "NOT modified" in str(exc), True)
    finally:
        pathlib.Path.mkdir = real_mkdir
        shutil.rmtree(g.LOCK_DIR / g.DASHBOARD_LOCK_NAME, ignore_errors=True)
    check("robbery actually fired", bool(fired2), True)
    check("page byte-unchanged", out9.read_bytes(), before)
    check("no .tmp litter left behind",
          [p.name for p in tmp.iterdir() if ".tmp-" in p.name], [])

    print("\n== [EDGE] 21. the header states how many projects have NO ledger ==")
    out10 = tmp / "noledger.md"
    r = g.cmd_dashboard(str(out10))
    t = out10.read_text()
    check("no-ledger count present", "vault projects have no ledger" in t, True)
    check("count is in stats", isinstance(r["stats"].get("no_ledger"), int), True)

    print("\n== [EDGE] 22. a multi-block legacy day-file is NOT attributed ==")
    # The frontmatter fallback is trustworthy only on single-block files:
    # frontmatter is frozen to the first writer, so a 2-block day-file would
    # attribute both blocks to whoever wrote first. That is inventing history.
    vroot = tmp / "vault-projects"
    (vroot / "Multi" / "progress").mkdir(parents=True)
    (vroot / "Single" / "progress").mkdir(parents=True)
    UU = "abcd1234-5555-5555-5555-555555555555"
    resume = ("\n### Resume Here\n\n- **Pick up at:** the real handoff\n"
              "- **Active blocker:** none\n")
    (vroot / "Multi" / "progress" / "2026-08-13.md").write_text(
        f"---\nsession_uuid: {UU}\n---\n\n# Session Progress Report\n\n"
        f"## 17:42 — later{resume}\n## 09:10 — earlier{resume}")
    (vroot / "Single" / "progress" / "2026-08-13.md").write_text(
        f"---\nsession_uuid: {UU}\n---\n\n# Session Progress Report\n\n"
        f"## 17:42 — only block{resume}")
    real_vault = g.VAULT_PROJECTS
    g.VAULT_PROJECTS = vroot
    try:
        multi = g._handoff_card("Multi", UU)
        single = g._handoff_card("Single", UU)
    finally:
        g.VAULT_PROJECTS = real_vault
    check("2-block file yields NO card", multi, [])
    check("1-block file yields a real card", bool(single), True)
    check("card carries the handoff text",
          any("the real handoff" in ln for ln in single), True)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 32)
    print(f"PASS: {PASS}  FAIL: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
