# CHANGELOG — context-builder skill

Every edit to `SKILL.md` or any file in `references/` MUST append an entry here (Decision 18 — CHANGELOG discipline).

Entry format:

```markdown
## <YYYY-MM-DD> — v<N.N>

### Summary
<one-sentence headline>

### Changes
- <file edited>: <specific change>
- ...

### Rationale
<why — references decision ID or issue>

### Breaking
<yes/no + migration notes if yes>
```

---

## 2026-04-15 — v2.0

### Summary
Full rewrite — adds Phase 0 (decomposition + research + Rumsfeld), expanded interview spine (11 rules + 10 MECE categories + 4 question layers + 3 mandatory question classes), Phase 5 (PRD + PM in Obsidian), user-mediated plan-mode handoff, power-word palette enforcement with lint.

### Changes
- `SKILL.md`: full rewrite. Preserves Rules 1–10 as spine; adds Rule 11 (research loop). Adds Phase 0, Phase 5, Phase 6. Tier-segmented prose to prevent TRIVIAL bias.
- `SKILL.md.v1.bak`: created — verbatim backup of previous v1 SKILL.md.
- `references/` (new subdir):
  - `complexity-tiers.md`: TRIVIAL / MODERATE / COMPLEX criteria + auto-elevation triggers.
  - `knowledge-theories.md`: 10 baked-in theories with rule-mapping (Rumsfeld, MECE, Socratic, Five Whys, Bloom, Pyramid, JTBD, Pre-mortem, Counterfactual, Reversibility-audit).
  - `rumsfeld-protocol.md`: 4-quadrant cadence + hard/soft gates + deadlock override (`--force`).
  - `mece-categories.md`: 10 gap categories with probe questions and auto-triggers.
  - `question-classes.md`: 3 mandatory question classes (pre-mortem, counterfactual, reversibility audit).
  - `powerword-palette.md`: canonical HIGH-word list (14 words) + full 9-category palette + density rules + anti-pattern bans.
  - `powerword-placement-matrix.md`: skill-section slots + PRD/PM section slots + `--rewrite` table.
  - `research-protocol.md`: ctx7 + WebSearch + WebFetch + seqthink waterfall + budget caps + graceful degrade.
  - `prd-template.md`: PRD Obsidian template with power-word anchors + frontmatter.
  - `pm-template.md`: PM Obsidian template with execution-flavor sections + power-word anchors.
  - `research-log-template.md`: audit-trail sidecar format.
  - `interview-state-schema.md`: resume-support JSON schema.
  - `KNOWN-LIMITS.md`: B1–B7 edge cases + defaults.
  - `CHANGELOG.md`: this file.

### Rationale
Pedro's request: context-builder should decompose the idea, apply sequential + critical thinking, conduct thorough research, apply Rumsfeld, loop back on unknowns, then produce a solid PRD + PM in Obsidian using power words throughout. Discuss-phase decisions 1–19 locked in plan `~/.claude/plans/nuanced-scaffolding-beacon.md`. Plan verified via `/plan-perfector` (cycle 1 REVISE → cycle 2 PROCEED).

### Breaking
Yes — the v1 skill had 10 rules; v2 has 11. v1 had no Phase 0 or Phase 5; v2 does. v1 did not produce PRD/PM artifacts; v2 does. Rollback: `cp SKILL.md.v1.bak SKILL.md && rm -rf references/`.

### Verification
`py ~/.claude/skills/context-builder/verify.py` exits 0. Checks: line count, rule/layer/phase count, power-word density, anti-pattern word absence, tier-segmented prose, trigger keywords in frontmatter description.

---

## 2026-06-09 — v2.1

### Summary
Pair-3 graft: 4 additive/trim/rewrite edits from mattpocock/skills grill-with-docs comparison (SHA aaf2453).

### Changes

**EDIT 1 — cherry-pick: recommended-answer + explore-instead-of-ask (License Case 3)**
- `SKILL.md`: Rule 2 — added RECOMMENDED-answer paragraph (labeled inference, defend then defer). Rule 3 — added explore-instead-of-ask paragraph (read before asking; pair finding with question).
- Attribution comment `<!-- Adapted from mattpocock/skills grill-with-docs @ aaf2453 -->` added to both insertions.

**EDIT 2 — add-capability: `--inline-docs` living-doc mode (License Case 3)**
- `SKILL.md`: Added Step 5.4 "Living-doc mode (OPT-IN: `--inline-docs`)" with inline decision-rationale block format, ADR 3-true test (consequential AND has alternatives AND not easily reversible), and single-source-of-truth rule. Added `--inline-docs` to Override Flags. Renumbered old 5.4→5.3 (power-word lint had already been at that position) and old announce step to 5.5.
- Attribution comment `<!-- Adapted from mattpocock/skills grill-with-docs @ aaf2453 — inline living-doc + ADR 3-true test -->` added.

**EDIT 3 — trim-bloat: remove power-word subsystem**
- `references/powerword-palette.md`: DELETED.
- `references/powerword-placement-matrix.md`: DELETED.
- `references/prd-template.md`: Full rewrite — removed all `{{POWER_WORDS: ...}}` slot markers, header note about power-word anchors, fill-instruction steps referencing power words and `powerword_audit.md` lint, and metadata mention of power-word lint sidecar. Template now has plain prose sections.
- `SKILL.md`: Removed Step 5.3 power-word lint, Phase 6.1 `powerword_audit.md` output line, `--rewrite` flag from Override Flags, powerword-placement-matrix reference in Step 5.1 tier-scaled note.
- `verify.py`: Removed `CANONICAL_HIGH` set, `distinct_high_density()` helper, Check #10 (canonical HIGH list in palette), Check #12 (distinct HIGH density), Check #14 (plan file pinned list), old Check #13 exceptions for deleted files (`powerword-palette.md`, `powerword-placement-matrix.md`). `EXPECTED_REFERENCES` reduced from 14 to 12 entries.
- Verify gate: `py verify.py` exits 0 (31 checks, 0 warnings) after trim.

**EDIT 4 — CSO-rewrite: trigger-first `description:` field**
- `SKILL.md`: L3 `description:` field rewritten from mechanism-first ("Force thorough, structured context-building…") to trigger-first ("Use when: entering plan mode…"). Preserved "Does NOT apply to:" clause verbatim. Added domain-model/glossary/stress-test/ADR trigger phrases. All 5 baseline trigger prompts from PREFLIGHT.md §4 still covered.

### Rationale
Pair-3 graft ratified in EDIT-PLAN.md. EDIT 1–2: additive improvements to interview quality (RECOMMENDED answers reduce blank-slate responses; explore-instead-of-ask reduces unnecessary questions; inline-doc mode adds lightweight ADR support). EDIT 3: power-word subsystem served style enforcement, not context coverage; it created maintenance surface without measurable quality gain; deletion simplifies the reference set from 14 to 12 files and the verify harness from 14 to 12 checks. EDIT 4: CSO rule (superpowers:writing-skills) requires descriptions to lead with "Use when…" conditions for reliable trigger matching; mechanism recitation in descriptions does not help the router decide.

### Breaking
No — no skill API changes. `--rewrite` flag removed (was power-word-only, no other callers). `powerword_audit.md` sidecar no longer produced. Template `{{POWER_WORDS}}` slots removed (were aspirational, never populated at runtime). Rollback: restore deleted files from git, revert SKILL.md and verify.py changes.

### Verification
`py ~/.claude/skills/context-builder/verify.py` exits 0 — 31 checks, 0 warnings.

---

## 2026-06-13 — v3.0

### Summary
Elicitation Trio canonicalization — context-builder becomes the canonical standalone elicitation brain. Adds Assumptions-First Mode (`--assume`), a Canonical Elicitation Contract, a deeper pattern-detection codebase scout, 8 documented weakness fixes, and loosens verify.py from frozen counts to named-structure assertions.

### Changes
- `references/assumptions-mode.md`: NEW — inverted-flow doctrine. Scout 5–15 files → surface 3–5 assumptions (Evidence / If wrong / Confidence) → confirm only the Unclear ones (~3 interactions vs ~15). Includes confidence rubric, interaction-budget contract, guardrails (no-guess-as-Confident, evidence-cites-files, reversibility, compliance, contradiction-at-point-of-arising).
- `SKILL.md`: added `## Assumptions-First Mode (--assume)` section (1a) + `--assume` to Override Flags. Added `## Canonical Elicitation Contract` section (1d) — shared behaviors, per-face separation rules, drift rule (context-builder = canonical owner). Rule 3 (1b) strengthened with pattern-detection scout (reusable assets / established patterns / integration points; annotate options with code context). 8 weakness fixes (1c): Rule 6 coverage-cadence trigger (new-UNEXPLORED or every-5-questions), Rule 2 async/silence state-save + resume hint, Rule 10 tier auto-recalibration, Rule 5 case-insensitive compliance + expanded set, Rule 4 + Anti-Patterns contradiction-at-point-of-arising, Phase 5.4 active decision-log Glob detection, Step 0.2 MCP-failure prose fallback + log, Tier Quick Reference concrete TRIVIAL example.
- `references/mece-categories.md`: Category 10 auto-trigger regex made case-insensitive; added `children`, `minors`, `biometric`, `PII`.
- `references/research-protocol.md`: MCP param-discipline section — ToolSearch/param-fetch failure falls back to prose decomposition, logs degrade, never stalls.
- `verify.py`: Rule-count check (== 11) replaced with named-heading presence (each `### Rule N:` 1–11). Layer-count check (== 4) replaced with named-heading presence (each `### Layer N:` 1–4). Added `assumptions-mode.md` to `EXPECTED_REFERENCES`. Added presence checks for `## Assumptions-First Mode`, `## Canonical Elicitation Contract`, `--assume`. Fixed stale comment referencing deleted powerword files.

### Rationale
Plan `~/.claude/plans/system-reminder-message-sent-at-fri-async-salamander.md` ("Elicitation Trio — One Brain, Three Faces"), Changeset 1. Codex adversarial review findings: #3 (frozen counts hide complexity → loosen to named structure), #1/#2/#10 (canonical contract as documentation, not a parity harness), #4 (write the assumptions-mode doctrine as a spec before wiring the section). New capability lands in `##` sections to preserve the protected 11-rule / 4-layer structure.

### Breaking
No — additive. `--assume` is opt-in; all existing flows unchanged. verify.py is stricter on named structure but no longer brittle to count changes.

### Verification
`py ~/.claude/skills/context-builder/verify.py` exits 0.

---

## 2026-06-13 — v3.1

### Summary
Trigger collision fix + Research-Before-Asking (Currency Mandate) — context-builder cedes "stress-test an existing plan" to grill-me, and codifies the shared mandate that all three faces refresh current state (Context7 + web) before asking, so questions and RECOMMENDED answers reflect the domain as it is now, not training-era.

### Changes
- `SKILL.md`: description `Does NOT apply to:` list — added anti-trigger "stress-testing an existing plan or design (use grill-me)" (R2-0; closes the only Round-1 trigger collision). `## Canonical Elicitation Contract` — added the **Research-Before-Asking (Currency Mandate)** clause as a shared behavior all three faces inherit (R2-A). Phase 0 Step 0.3 retitled "Research loop (Currency Mandate)" and reframed to serve staleness-beating, not only unknown-filling; added MODERATE-must-not-skip-when-libs-in-scope rule + TRIVIAL-skip-only-if-nothing-researchable (R2-B). Rule 11 reframed to cite the Currency Mandate, extended to fire on stale-domain triggers (not only undefined terms), and to require questions + RECOMMENDED answers reflect researched current best-practices (R2-B).
- `verify.py`: added presence checks in the 7b block for the Currency Mandate clause markers "Research-Before-Asking" and "Currency Mandate".

### Rationale
Plan `~/.claude/plans/system-reminder-message-sent-at-fri-async-salamander.md`, Round 2 (R2-0 + R2-A + R2-B). R2-0: the Round-1 trigger eval found "stress-test my plan" matched both context-builder and grill-me; the anti-trigger routes it cleanly to grill-me. R2-A/R2-B (Currency Mandate): training is a snapshot and the domain moves; refreshing current state before asking keeps questions and recommended answers authoritative and accurate. Codified once in the Canonical Elicitation Contract so the three faces inherit it proportionately.

### Breaking
No — additive. Description anti-trigger only narrows over-trigger; the Currency Mandate is a research-before-asking discipline layered on the existing Phase 0 / Rule 11 research machinery. No flag or API changes.

### Verification
`py ~/.claude/skills/context-builder/verify.py` exits 0.

---

## 2026-07-16 — v3.2

### Summary
Contract now documents `/ask` as explicitly NOT a face — path-clearing, not elicitation — leaving "one question at a time, never batch" absolute.

### Changes
- `SKILL.md`: added a **Not a face** paragraph to the Canonical Elicitation Contract, between the separation rules and the drift rule. Names `/ask` (doctrine `~/.claude/ask.md`), states it is not bound by this contract, and records that it batches ranked blockers by design.

### Rationale
Plan `~/.claude/plans/i-want-to-create-sunny-island.md`, challenge C3. The plan originally proposed a batching CARVE-OUT inside the contract. Rejected: `/ask` picks *which* work to do and then routes; it is not requirements elicitation and was never a projection of this brain, so it needs no exemption from a contract that does not govern it. A carve-out would have weakened "never batch" for the three faces where it IS load-bearing. Documenting the boundary instead keeps the rule absolute while preventing the undocumented-drift failure mode that stranded the `grill-me` route.

### Breaking
No — additive documentation. The contract's core bullets are untouched; no behavior change for any of the three faces.

### Verification
`python3 ~/.claude/skills/context-builder/verify.py` exits 0 (38 checks), confirmed across two consecutive runs. RED→GREEN proven: injecting a banned word into the new paragraph produced `FAIL: SKILL.md body contains anti-pattern words: ['robust']` (exit 1); removing it returned exit 0.

### Finding — the CHANGELOG discipline is convention-enforced, not machine-enforced
`verify.py` never reads this file's content. Line 17 lists `CHANGELOG.md` in `EXPECTED_REFERENCES` (an existence check only — the file always exists), and line 246 excludes it from the anti-pattern scan. verify.py therefore exits 0 whether or not this entry was written. The plan's original `[RED]` test assumed the opposite and could never have gone red; it was replaced with the anti-pattern test above, which guards the text actually being edited. Recorded rather than papered over, per the plan's own instruction. Closing the gap would mean asserting that the newest `## <date>` heading postdates `SKILL.md`'s mtime — deliberately not done here (out of scope; SURGICAL).

---

## 2026-07-31 — v3.3

### Summary
`py` → `python3` in the two live executable instructions; the file no longer contradicts itself.

### Changes
- `SKILL.md:350`: CHANGELOG-discipline gate now says `python3 ~/.claude/skills/context-builder/verify.py`.
- `references/CHANGELOG.md:167` (Discipline rule step 2): same substitution.

### Rationale
Context corpus audit 2026-07-31, W5 / Tier 0 #9. Constitution rule 5 mandates `python3`; `py` is a
transitional Windows-launcher shim at `~/.local/bin/py`. This file previously used `python3` in its
2026-07-16 verification note and `py` in its own discipline rule — an instruction disagreeing with
its own recorded practice. The historical "verification performed" lines in past entries (`:57`,
`:82`, `:94`, `:117`, `:137`) are left verbatim: they record what was actually run at the time and
rewriting them would falsify the record.

### Breaking
No — `py` still resolves via the shim. Documentation-only correction.

### Verification
`python3 ~/.claude/skills/context-builder/verify.py` → `RESULT: PASS (38 check(s), 0 warning(s))`, exit 0.

---

## Discipline rule

Any future edit to this skill MUST:
1. Append a new entry to this file (date, summary, changes, rationale, breaking).
2. Run `python3 ~/.claude/skills/context-builder/verify.py` and confirm exit 0 before closing the edit.
3. If breaking, document migration steps.

The discipline rule exists to prevent `references/*` drift from `SKILL.md`. 13 reference files are a maintenance cliff without it.
