#!/usr/bin/env bash
# The example block inside agents/goal-extractor.md MUST parse with goals.py,
# AND the prose above it must demand exactly the field set the parser enforces.
#
# Why the second half exists: this test already existed and still PASSED
# through the defect that made the whole goal ledger non-functional. The
# agent file's Rules section instructed the extractor to emit `origin`; the
# parser's `_FIELD_NAMES` did not list it, so every real extraction was
# rejected as carrying "an unrecognized field"; and the example block below
# happened to omit `origin`, so parsing it proved nothing about the shape the
# prose was actually demanding. A guard that validates the example while the
# prose says something else is a guard that cannot see the drift it was built
# to catch. Every assertion below is therefore derived from `goals._FIELD_NAMES`
# at run time — never from a field list hardcoded here, which would be a third
# copy free to drift with the other two.
set -u
AGENT=/home/pedro/.claude/agents/goal-extractor.md
SCRIPTS=/home/pedro/.claude/skills/progress-report/scripts
GOALS="$SCRIPTS/goals.py"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
# The `parse-extraction` call below names no session, so it writes no run
# receipt today. Exported anyway: the day someone adds `--session` to that call
# is the day this suite starts writing into the live ~/.claude/cache/goal-run,
# and an isolation that only holds while nobody edits the file is not one.
export GOALS_RUN_DIR="$TMP/goal-run"
FAIL=0

if [ ! -f "$AGENT" ]; then echo "FAIL: $AGENT missing"; exit 1; fi

awk '/<<<GOAL_EXTRACTION>>>/,/<<<END>>>/' "$AGENT" > "$TMP/block.txt"
if [ ! -s "$TMP/block.txt" ]; then
  echo "FAIL: no <<<GOAL_EXTRACTION>>> example found in $AGENT"; exit 1
fi

if python3 "$GOALS" parse-extraction --file "$TMP/block.txt" > "$TMP/out.json"; then
  echo "PASS: example block parses"
else
  echo "FAIL: example block in $AGENT does not parse"; FAIL=1
fi

if grep -q '"goal-extractor"' /home/pedro/.claude/agents/registry.json; then
  echo "PASS: registered"
else
  echo "FAIL: goal-extractor missing from registry.json"; FAIL=1
fi

# --- Prose-vs-parser contract. Runs against the live `_FIELD_NAMES` tuple. ---
AGENT="$AGENT" SCRIPTS="$SCRIPTS" BLOCK="$TMP/block.txt" python3 - <<'PY'
import json
import os
import re
import sys

sys.path.insert(0, os.environ["SCRIPTS"])
import goals  # noqa: E402

fields = set(goals._FIELD_NAMES)
text = open(os.environ["AGENT"], encoding="utf-8").read()
fail = 0


def check(ok, name, detail=""):
    global fail
    if ok:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name}{(' — ' + detail) if detail else ''}")
        fail = 1


# 1. Every proposal object in the example carries EXACTLY the parser's field
#    set — all of them, not just the first. A drifted example is how a
#    contract test ends up validating a shape nobody is asked to produce.
body = open(os.environ["BLOCK"], encoding="utf-8").read()
body = body.split("<<<GOAL_EXTRACTION>>>", 1)[1].rsplit("<<<END>>>", 1)[0]
proposals = json.loads(body)["proposed"]
check(len(proposals) >= 2, "example carries more than one proposal")
for i, proposal in enumerate(proposals):
    check(set(proposal) == fields, f"example proposed[{i}] carries exactly the parser's fields",
          f"example has {sorted(set(proposal))}, parser wants {sorted(fields)}")

# 2. Both `origin` shapes appear in the example, so each branch is exercised
#    through the live parser rather than assumed.
origins = [p.get("origin", "") for p in proposals]
check(any(o == "extracted" for o in origins), "example shows an `extracted` origin")
check(any(o.startswith("plan:") and len(o) > len("plan:") for o in origins),
      "example shows a `plan:<path>` origin")

# 3. The requirement bullet must list exactly those same fields. This is the
#    assertion that would have caught the shipped defect: the bullet said
#    "all seven keys" and named seven while the Rules section above demanded
#    an eighth. The field list is the backticked run between the bullet's
#    em-dash and the first period — keep it in that shape when editing.
bullet = [ln for ln in text.splitlines() if ln.startswith("- **Every proposal carries all ")]
check(len(bullet) == 1, "exactly one field-set requirement bullet", f"found {len(bullet)}")
if len(bullet) == 1:
    segment = bullet[0].split("—", 1)[1].split(".", 1)[0]
    named = set(re.findall(r"`([^`]+)`", segment))
    check(named == fields, "requirement bullet names exactly the parser's fields",
          f"bullet names {sorted(named)}, parser wants {sorted(fields)}")

    # 4. The spelled-out count must match, and no stale count may survive
    #    anywhere in the file — line 88's "all-seven-keys" outlived line 78's
    #    edit once already.
    words = {5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
    want = words.get(len(fields), "")
    # Self-audit of this guard: past the map's range the count check would
    # silently degrade into asserting the nonsense string "all  keys" and
    # fail with a message naming no cause. A guard that fails illegibly is
    # the same class of defect as one that passes wrongly — say what to do.
    check(bool(want), "the number-word map covers the parser's field count",
          f"{len(fields)} fields — extend `words` in this test")
    if want:
        check(f"all {want} keys" in bullet[0], f"bullet says 'all {want} keys'")
    stale = [w for n, w in words.items() if n != len(fields)
             and (f" {w} keys" in text or f"all-{w}-keys" in text)]
    check(not stale, "no stale field-count wording anywhere in the file", f"found {stale}")

# 5. Every backticked `name: value` mention in the prose must name a field the
#    parser actually knows. This is the exact drift that shipped — the Rules
#    section demanded `origin: plan:<path>` from a parser that rejected the
#    key — and it is invisible to any check that only reads the example block.
mentioned = set(re.findall(r"`([a-z_]+): ", text))
check(mentioned <= fields, "every `field: value` mention names a real parser field",
      f"unknown: {sorted(mentioned - fields)}")

sys.exit(fail)
PY
[ $? -eq 0 ] || FAIL=1

exit $FAIL
