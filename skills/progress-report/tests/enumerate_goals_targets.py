"""Do not "correct" this snippet to honour the env vars — reading the real paths is deliberate. `goals.py` applies its `GOALS_LEDGER_DIR` / `GOALS_LOCK_DIR` / `GOALS_RUN_DIR` overrides **inside `main()`** (currently `:2610-2614`, under `global LEDGER_DIR, LOCK_DIR, RUN_DIR`), **not at import time**; `LEDGER_DIR` binds at `:29` and `VAULT_PROJECTS` at `:1908`, both to real home paths. This snippet imports `goals` and never calls `main()`, so it reads the **REAL** ledger dir and the **REAL** vault whatever `GOALS_LEDGER_DIR` or `PORTFOLIO_VAULT` are exported to. That is correct here: it is a read-only enumeration of *names*, step 4 `cp -a`s the real vault so the directory names are identical in the copy, and the script itself builds the `$WORK/vault/...` output paths. Repointing it at the copy would change what is enumerated for no gain. `goals.py`'s own `if __name__ == "__main__"` block (currently `:2869-2872`) already carries a comment warning about this same import-vs-`main()` repoint class of bug — read it before touching this.
"""
import sys; sys.path.insert(0, "/home/pedro/.claude/skills/progress-report/scripts")
import goals
led = {p.stem for p in goals.LEDGER_DIR.glob("*.json")} - {"outcomes"}
for d in sorted(goals.VAULT_PROJECTS.iterdir()):
    if d.is_dir() and (d / "Goals.md").exists() and goals.slugify(d.name) in led:
        print(f"{goals.slugify(d.name)}\t{d.name}")
