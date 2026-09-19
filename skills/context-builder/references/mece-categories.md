# MECE Gap Categories (10)

Structured, exhaustive coverage. Every MODERATE+ interview must address each applicable category. Coverage Status reports (Rule 6) verify all 10 are either Covered, Still-Fuzzy, or explicitly Unexplored.

No category overlaps with another (Mutually Exclusive). Together they exhaust the gap space (Collectively Exhaustive).

---

## Category 1 — Domain Blindspots

**What:** Things you don't know you don't know about the business/domain.
**Probe questions:**
- "What's the one domain rule that a newcomer always gets wrong?"
- "What vocabulary do insiders use that outsiders misunderstand?"
- "What's considered obvious by practitioners here?"

---

## Category 2 — Technical Constraints

**What:** Performance, compatibility, platform limitations.
**Probe questions:**
- "What's the specific performance budget — latency p95, memory ceiling, concurrent users?"
- "What platforms or runtimes are excluded by existing decisions?"
- "Where is the scaling ceiling of the current stack?"

---

## Category 3 — Cross-Phase Dependencies

**What:** How this phase's decisions constrain future phases.
**Probe questions:**
- "What downstream phase reads this phase's outputs?"
- "What commitment are we making that locks future decisions?"
- "What must remain backward-compatible after this ships?"

---

## Category 4 — User Workflow Implications

**What:** How real users will interact with what's being built.
**Probe questions:**
- "Walk me through a single real user's workflow end-to-end."
- "Where does a user give up or get frustrated with the current flow?"
- "What does the user do immediately before and after this touchpoint?"

---

## Category 5 — Edge Cases

**What:** What happens when inputs are unexpected, missing, or malformed.
**Probe questions:**
- "What happens when the input is empty, null, or negative?"
- "What happens under concurrent writes or partial failures?"
- "What happens when an upstream service is slow, down, or returns garbage?"

---

## Category 6 — Assumption Validation

**What:** Things being taken for granted that might be wrong.
**Probe questions:**
- "You assumed {X}. Is {X} still accurate given the current state?"
- "What would have to be true for this approach to fail?"
- "When did you last verify {X}, and what changed since?"

---

## Category 7 — Stakeholder-Power Dimension (NEW)

**What:** Beyond RACI — who silently blocks, whose veto matters, who must be informed for political reasons.
**Probe questions:**
- "Who could kill this project with one email? Do they know about it?"
- "Who champions this internally? Are they still bought in?"
- "Whose approval isn't required but whose objection would stall execution?"

**Auto-trigger:** any project with >1 stakeholder or the word `approval` / `sign-off` / `launch` in scope.

---

## Category 8 — Temporal Dimension (NEW)

**What:** Deadlines, time-locked dependencies, calendar events, sequencing constraints.
**Probe questions:**
- "What is the hard deadline, and what drives it?"
- "What calendar events (tax period, year-end, release window) constrain sequencing?"
- "What must happen before this, and what must happen after?"

**Auto-trigger:** any project with a date, deadline, period, or schedule mention.

---

## Category 9 — Second-Order Effects (NEW)

**What:** What does success enable, disable, or create dependencies for.
**Probe questions:**
- "If this works, what's the next thing you'll be asked to build because of it?"
- "What systems will now depend on this being available?"
- "What does this make easier — and what does it make harder — downstream?"

**Auto-trigger:** COMPLEX tier, or any project labeled "foundation," "platform," or "infrastructure."

---

## Category 10 — Compliance / Ethics Dimension (NEW)

**What:** Regulatory, privacy, fairness, harm.
**Probe questions:**
- "What regulation or policy governs this data or interaction?"
- "Whose privacy could be affected, and how is consent captured?"
- "Who could be harmed if this works incorrectly — and who is accountable?"

**Auto-trigger regex (case-insensitive):** `health|finance|tax|user-data|child|children|minors|medical|HIPAA|GDPR|privacy|DGII|SOC2|PCI|biometric|PII`. Match regardless of case (`hipaa`, `Hipaa`, `HIPAA` all fire). When matched, this category is MANDATORY regardless of tier.

---

## Coverage Status report template

After every 3–5 questions, emit:

```
**Coverage Status:**
- Covered (HIGH confidence): {category numbers + topics}
- Still fuzzy (LOW/MED confidence): {categories + why still fuzzy}
- Unexplored: {categories not yet addressed + reason — e.g., "N/A for this scope" or "deferred"}
- MECE check: {PASS | FAIL — which category has overlap or gap}
```

MECE check FAILs when:
- Two categories claim the same topic (overlap) — reassign to one.
- A material topic is not in any category (gap) — flag and add.
