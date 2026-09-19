# Knowledge Theories — Baked-in Defaults

10 theories baked into the skill. Each has a specific rule/phase it augments. No orphan theories — every entry maps to a concrete skill section.

Research is rigorous and systematic: this set was curated for the strongest completeness pair (Rumsfeld + MECE), surgical interrogation (Socratic + Five Whys), automatic escalation (Bloom), artifact quality (Pyramid + JTBD), and blindspot coverage (Pre-mortem + Counterfactual + Reversibility).

---

## Tier 1 — Mandatory (5)

### 1. Rumsfeld 4-Quadrant Matrix
**What:** Epistemic coverage. Four quadrants: Known Knowns (Q3 assumptions), Known Unknowns (Q2 resolvable gaps), Unknown Knowns (Q1 surfaced context), Unknown Unknowns (Q4 black swans).
**Augments:** Phase 0.4 (initial sweep), Rule 11 (trigger-based re-sweep), Phase 5.4 (final sweep).
**When to invoke:** always. See `rumsfeld-protocol.md` for cadence + gate classification.
**Example application:** After the user says "we'll integrate with Shopify," add Shopify to Q3 assumptions ("their API is stable") and Q2 known unknowns ("rate limits? webhook reliability?").

### 2. MECE (McKinsey)
**What:** Mutually Exclusive, Collectively Exhaustive. Gap-coverage discipline — verifies categories don't overlap and nothing material is missed.
**Augments:** Rule 5 (Cover All Gap Categories — 10 categories), Rule 6 (Coverage Status reports).
**When to invoke:** every Coverage Status report. Verify each category is either Covered or explicitly flagged Still-Fuzzy / Unexplored.
**Example application:** Coverage report verifies domain, technical, cross-phase, workflow, edge-cases, assumptions, stakeholder-power, temporal, second-order, compliance — all 10, no overlaps.

### 3. Socratic Method
**What:** Question-driven knowledge surfacing. Two specific techniques embedded: *elenchus* (refute by exposing contradiction) and *maieutics* (draw out latent knowledge the user hasn't articulated).
**Augments:** Rule 4 (Challenge Vague Answers) — provides the question-type rubric for Level 2 and Level 3 escalation.
**When to invoke:** any vague answer. Level 2 uses elenchus ("Earlier you said X, but this implies Y — which is it?"). Level 3 uses maieutics ("You mentioned the checkout flow. Walk me through what happens when the third-party payment webhook is delayed by 30 seconds.").
**Example application:** User says "we handle errors normally." Level 2 elenchus: "You said 'normally' — does that mean retry with exponential backoff, fail-fast, or silent-log?"

### 4. Five Whys (Toyota)
**What:** Root-cause drilling. Chain 5 "why" questions to reach the underlying cause, not the surface symptom.
**Augments:** Rule 4 Level 3 escalation — paired with Socratic elenchus.
**When to invoke:** when the user gives a surface answer to a question whose root matters.
**Example application:** User says "we need caching." Why? "Performance is slow." Why? "Database is slow." Why? "Queries aren't indexed." Why? "Schema was never optimized." Why? "Migration was rushed under deadline." → Real problem = schema optimization, not caching.

### 5. Bloom's Taxonomy
**What:** Six cognitive levels (Remember → Understand → Apply → Analyze → Evaluate → Create). Higher levels trigger deeper reasoning.
**Augments:** Question Layers 1–4. Layer 1 maps to Remember / Understand; Layer 2 to Apply; Layer 3 to Analyze; Layer 4 to Evaluate / Create.
**When to invoke:** every question — match verb choice to the layer. Use canonical Cognitive Verbs from the power-word palette.
**Example application:** Layer 4 Validation uses `assess` / `critique` / `justify` — not `list` or `describe`.

---

## Tier 2 — Recommended (2)

### 6. Pyramid Principle (Minto)
**What:** Answer-first writing structure. Lead with the conclusion, support with grouped, logically-ordered reasoning.
**Augments:** Phase 5 PRD and PM template prose. Each section states the answer up front, then supports.
**When to invoke:** every section in PRD.md and PM.md.
**Example application:** PRD problem statement opens with a one-sentence thesis ("The inventory system misreports stock because stock adjustments and sales events race"), then supports with evidence.

### 7. Jobs-to-be-Done (Christensen)
**What:** Frame user needs as "jobs" users hire a product to do, instead of personas or demographics.
**Augments:** PRD problem statement + goals sections.
**When to invoke:** PRD problem statement and user stories.
**Example application:** Instead of "Rosa is a kitchen manager who wants inventory reports," write: "When Rosa is closing the kitchen at night, she wants to know what to prep tomorrow, so she can leave confident the morning run won't stall."

---

## Blindspot Additions (3)

### 8. Pre-mortem (Gary Klein)
**What:** Imagine the project failed 6 months from now — write the failure narrative backward from that point.
**Augments:** Layer 4 Validation — mandatory round in every COMPLEX invocation.
**When to invoke:** late-Layer-4, before Rule 7 handoff.
**Example application:** "Imagine it's October 2026 and this inventory system was abandoned. Write the 3-paragraph narrative of why it failed."

### 9. Counterfactual / Premise Challenge
**What:** Challenge the project's premise itself. "Why not do nothing? Why not the opposite?"
**Augments:** Layer 1 Strategic — mandatory question class.
**When to invoke:** first Layer 1 question in every MODERATE+ invocation.
**Example application:** "What is the cost of doing nothing — letting the existing stock-count process stay as-is? What is the strongest argument against building this inventory system?"

### 10. Reversibility Audit (Bezos one-way / two-way doors)
**What:** Classify every decision by reversibility cost. One-way doors get extra scrutiny; two-way doors move faster.
**Augments:** Layer 4 — every surfaced decision tagged `[REVERSIBLE]` or `[ONE-WAY]`. One-way doors trigger extra Q4 sweep + pre-mortem round.
**When to invoke:** every architectural or data-model decision in Layer 4.
**Example application:** "Choosing Supabase = [REVERSIBLE] (can migrate off). Choosing to store plaintext DGII RNC in logs = [ONE-WAY] (data is already captured; can't un-capture downstream)."

---

## Opt-in (flag-gated)

### Cynefin framework — `--cynefin` flag
Complexity classification: Simple / Complicated / Complex / Chaotic. Use only when TRIVIAL/MODERATE/COMPLEX is too coarse (genuinely chaotic domains where probe-sense-respond beats analyze-respond).

### OKR (Objectives + Key Results) — opt-in via user request
KPI-heavy PM doc sections. Not every project warrants strict OKR framing.

### Toyota A3 — opt-in via user request
Single-page PM variant. Most projects want the full PM doc, not A3.

---

## Dropped (rationale)

- **OODA loop** — Rumsfeld trigger-loop already covers research re-entry. Redundant.
- **Polya's How To Solve It** — sequential-thinking MCP already provides decomposition. Redundant.
- **Wardley Mapping** — strategic positioning, overkill for context-building.
