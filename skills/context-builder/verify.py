"""
verify.py — context-builder skill v2 verification script.

Runs all post-rewrite checks. Exit 0 on pass, non-zero on fail.
Platform: Windows / Git Bash (use `py` or `py -3`).
"""
import pathlib
import re
import sys

SKILL_DIR = pathlib.Path(__file__).parent
SKILL_MD = SKILL_DIR / "SKILL.md"
BACKUP_MD = SKILL_DIR / "SKILL.md.v1.bak"
REFERENCES_DIR = SKILL_DIR / "references"

EXPECTED_REFERENCES = [
    "CHANGELOG.md",
    "KNOWN-LIMITS.md",
    "knowledge-theories.md",
    "rumsfeld-protocol.md",
    "mece-categories.md",
    "question-classes.md",
    "complexity-tiers.md",
    "research-protocol.md",
    "prd-template.md",
    "pm-template.md",
    "research-log-template.md",
    "interview-state-schema.md",
    "assumptions-mode.md",
]

# Word-boundary anti-patterns (single-word or must-have-word-boundary)
BANNED_WORDS = [
    "robust", "leverage", "delve", "facilitate",
]
# Multi-word phrases that need word-boundary on the first word to prevent substring matches
# (e.g. "try to" must not match "entry to")
BANNED_PHRASES_WB = [
    r"try\s+to",
    r"please\b",
]
# Literal phrases (substring match is fine — these are idioms that won't have common false positives)
BANNED_LITERAL = [
    "in today's ever-evolving",
    "it's important to note",
    "certainly!",
]

TRIGGER_KEYWORDS = [
    "planning", "design", "new feature", "spec", "PRD", "requirements",
    "context-gathering", "building", "architecture",
]

TRIVIAL_DEPTH_WORDS_FORBIDDEN = ["thorough", "comprehensive", "exhaustive", "rigorous"]


class Report:
    def __init__(self):
        self.failures = []
        self.warnings = []
        self.passes = []

    def fail(self, msg):
        self.failures.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def ok(self, msg):
        self.passes.append(msg)

    def exit_code(self):
        return 1 if self.failures else 0

    def print(self):
        for p in self.passes:
            print(f"  PASS: {p}")
        for w in self.warnings:
            print(f"  WARN: {w}")
        for f in self.failures:
            print(f"  FAIL: {f}")
        print()
        if self.failures:
            print(f"RESULT: FAIL ({len(self.failures)} failure(s), {len(self.warnings)} warning(s))")
        else:
            print(f"RESULT: PASS ({len(self.passes)} check(s), {len(self.warnings)} warning(s))")


def read_text(path):
    return path.read_text(encoding="utf-8")


def tokenize(text):
    # Lowercase words incl hyphens (for "step-by-step")
    return re.findall(r"[a-z][a-z0-9\-']*", text.lower())


def find_banned(text):
    found = []
    lowered = text.lower()
    for w in BANNED_WORDS:
        if re.search(r"\b" + re.escape(w) + r"\b", lowered):
            found.append(w)
    for pat in BANNED_PHRASES_WB:
        if re.search(r"\b" + pat, lowered):
            found.append(pat.replace(r"\s+", " ").replace(r"\b", ""))
    for lit in BANNED_LITERAL:
        if lit in lowered:
            found.append(lit)
    return found


def check_file_exists(r, path, label):
    if path.is_file():
        r.ok(f"{label} exists: {path}")
        return True
    else:
        r.fail(f"{label} missing: {path}")
        return False


def run_checks():
    r = Report()

    # 1. SKILL.md exists + line count
    if check_file_exists(r, SKILL_MD, "SKILL.md"):
        lines = read_text(SKILL_MD).splitlines()
        if len(lines) < 500:
            r.ok(f"SKILL.md line count < 500: {len(lines)}")
        else:
            r.fail(f"SKILL.md line count >= 500 (bloat): {len(lines)}")

    # 2. Backup file intact
    if check_file_exists(r, BACKUP_MD, "SKILL.md.v1.bak"):
        bak_text = read_text(BACKUP_MD)
        if len(bak_text) > 100:
            r.ok("Backup file non-empty")
        else:
            r.fail(f"Backup file is suspiciously short: {len(bak_text)} chars")

    # 3. references/ dir exists
    if not REFERENCES_DIR.is_dir():
        r.fail(f"references/ dir missing: {REFERENCES_DIR}")
        return r

    # 4. Every reference file in EXPECTED_REFERENCES is present.
    # The power-word files (powerword-palette, powerword-placement-matrix) were deleted in v2.1;
    # assumptions-mode.md was added in v3.0. EXPECTED_REFERENCES is the single source of truth.
    for ref_name in EXPECTED_REFERENCES:
        check_file_exists(r, REFERENCES_DIR / ref_name, f"references/{ref_name}")

    if not SKILL_MD.is_file():
        return r

    skill_text = read_text(SKILL_MD)

    # 5. Frontmatter trigger keywords
    frontmatter_match = re.match(r"^---\n(.*?)\n---", skill_text, re.DOTALL)
    if frontmatter_match:
        fm = frontmatter_match.group(1).lower()
        missing = [kw for kw in TRIGGER_KEYWORDS if kw.lower() not in fm]
        if not missing:
            r.ok(f"Frontmatter contains all {len(TRIGGER_KEYWORDS)} trigger keywords")
        else:
            r.fail(f"Frontmatter missing trigger keywords: {missing}")
    else:
        r.fail("SKILL.md has no YAML frontmatter")

    # 6. All 11 named rules present (assert each heading exists, not a frozen total —
    #    adding a genuinely-new rule later should not require gaming a counter).
    missing_rules = [
        n for n in range(1, 12)
        if not re.search(rf"^### Rule {n}:", skill_text, re.MULTILINE)
    ]
    if not missing_rules:
        r.ok("All 11 named rule headings present (### Rule 1: .. ### Rule 11:)")
    else:
        r.fail(f"Missing named rule headings: Rule {missing_rules}")

    # 7. All 4 named layers present (named-presence, not a frozen count).
    missing_layers = [
        n for n in range(1, 5)
        if not re.search(rf"^### Layer {n}:", skill_text, re.MULTILINE)
    ]
    if not missing_layers:
        r.ok("All 4 named layer headings present (### Layer 1: .. ### Layer 4:)")
    else:
        r.fail(f"Missing named layer headings: Layer {missing_layers}")

    # 7b. New v3.0 capability sections + flag present, plus v3.1 Currency Mandate clause.
    for needle, label in [
        ("## Assumptions-First Mode", "Assumptions-First Mode section"),
        ("## Canonical Elicitation Contract", "Canonical Elicitation Contract section"),
        ("--assume", "--assume flag"),
        ("Research-Before-Asking", "Currency Mandate clause (Research-Before-Asking)"),
        ("Currency Mandate", "Currency Mandate marker"),
    ]:
        if needle in skill_text:
            r.ok(f"Present: {label} ({needle!r})")
        else:
            r.fail(f"Missing: {label} ({needle!r})")

    # 8. Phase 0, Phase 5, Phase 6 sections present
    for phase in ["## Phase 0", "## Phase 5", "## Phase 6"]:
        if phase in skill_text:
            r.ok(f"Section present: {phase}")
        else:
            r.fail(f"Section missing: {phase}")

    # 9. Anti-pattern words NOT in SKILL.md
    # Exception: the Anti-Patterns section itself enumerates banned words in quotes — we expect them there.
    # Split SKILL.md into "body" (excluding the Anti-Patterns section) and the Anti-Patterns section.
    anti_patterns_marker = "## Anti-Patterns"
    if anti_patterns_marker in skill_text:
        body, ap = skill_text.split(anti_patterns_marker, 1)
    else:
        body, ap = skill_text, ""

    banned_in_body = find_banned(body)
    if not banned_in_body:
        r.ok("SKILL.md body has no anti-pattern words (outside Anti-Patterns section)")
    else:
        r.fail(f"SKILL.md body contains anti-pattern words: {banned_in_body}")

    # 10. Tier Quick Reference TRIVIAL-path prose does NOT contain Depth words
    # Find the "TRIVIAL path" segment in SKILL.md Tier Quick Reference
    m = re.search(r"TRIVIAL path[^\n]*\n(?:(?!MODERATE path).)+", skill_text, re.IGNORECASE | re.DOTALL)
    if m:
        trivial_prose = m.group(0).lower()
        hits = [w for w in TRIVIAL_DEPTH_WORDS_FORBIDDEN if w in trivial_prose]
        if not hits:
            r.ok("TRIVIAL-path prose free of Depth words (tier segmentation enforced)")
        else:
            r.fail(f"TRIVIAL-path prose contains forbidden Depth words: {hits}")
    else:
        r.warn("TRIVIAL-path segment not found in Tier Quick Reference (non-blocking)")

    # 12. Anti-pattern scan on reference files
    for ref_name in EXPECTED_REFERENCES:
        path = REFERENCES_DIR / ref_name
        if not path.is_file():
            continue
        ref_text = read_text(path)
        # Exception: files that legitimately enumerate banned words in their verification sections
        if ref_name in (
            "CHANGELOG.md",
            "KNOWN-LIMITS.md",
            "prd-template.md",
            "pm-template.md",
            "research-log-template.md",
        ):
            continue
        banned_found = find_banned(ref_text)
        if not banned_found:
            r.ok(f"{ref_name} free of anti-pattern words")
        else:
            r.fail(f"{ref_name} contains anti-pattern words: {banned_found}")

    return r


if __name__ == "__main__":
    r = run_checks()
    r.print()
    sys.exit(r.exit_code())
