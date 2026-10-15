# Complexity Tiers — Self-Classification Criteria

The skill self-classifies every invocation into one of three tiers. Classification uses deterministic criteria and auto-elevation triggers. The tier determines which machinery runs.

---

## TRIVIAL

**Criteria (ALL must hold):**
- ≤1 concern / decision
- ≤1 file OR 1 config value
- Zero cross-phase / cross-module dependencies
- Fully reversible (two-way door)
- Zero external stakeholders (user only)
- Estimable in <30 minutes
- No compliance / safety / privacy implications

**Examples:**
- Rename a variable
- Tweak a CSS color
- Fix a typo
- Adjust a timeout value
- Update a copy string
- Add a missing import
- Bump a version pin

**Machinery:** 3-5 questions. Skip Phase 0, skip Rumsfeld, skip Phase 5. Produce a 1-line summary using the **terse palette only** (`specific`, `focused`, `precise`, `concise`, `clear`). Hand directly to plan-mode suggestion — no PRD, no PM.

---

## MODERATE

**Criteria (at least 2 hold):**
- 2–5 concerns / decisions
- Multi-file within ONE module / service
- Few cross-phase dependencies
- Mostly reversible (≤1 one-way door)
- 1–3 stakeholders
- Estimable in <1 day
- Low-to-medium compliance risk

**Examples:**
- Add an API endpoint
- Refactor a component
- Integrate a library
- Build a form with validation
- Add a table column with backfill
- Write a new skill with a well-known shape

**Machinery:** 5-15 questions. Phase 0 light (decompose only, no research unless triggered). Rumsfeld initial + final sweep only. Phase 5 = PRD only (skip PM). Balanced palette: `specific`, `focused`, `actionable`, `nuanced`, `structured`.

---

## COMPLEX

**Criteria (at least 2 hold):**
- 6+ concerns / decisions
- Spans multiple modules / services / systems
- Many cross-phase dependencies
- Contains one-way doors (irreversible decisions)
- 4+ stakeholders OR hidden stakeholders
- Multi-day to multi-week estimate
- High compliance / regulatory / safety / privacy risk
- Novel domain (user lacks prior art)

**Examples:**
- Design a new system
- Integrate a payment processor
- Migrate a database
- Build a new product from scratch
- Change an authentication scheme
- Regulatory pipeline (DGII, medical, finance)

**Machinery:** 15+ questions, no cap. Full Phase 0 + research + theory loop. Full Rumsfeld cadence with all triggers. Full Phase 5 (PRD + PM). Full palette: `thorough`, `comprehensive`, `rigorous`, `analytical`, `critical`, `systematic`, `step-by-step`.

---

## Auto-elevation triggers

Any of these = **minimum MODERATE, default COMPLEX**:

- **Keywords:** `auth`, `authentication`, `payment`, `migration`, `compliance`, `DGII`, `medical`, `HIPAA`, `privacy`, `GDPR`, `architecture`, `redesign`, `new product`, `new feature`, `refactor <module>`, `integrate <external service>`
- **Domain triggers:** health, finance, tax, child-related, medical, legal, regulatory → **minimum MODERATE**
- **Ambiguity signals:** user phrases like "figure out how to...", "design a...", "build a...", "I'm not sure how to..." → **minimum MODERATE**
- **Irreversibility signal:** any mention of deletion, migration, schema change, public deployment → **minimum MODERATE**
- **Reversibility audit flags a one-way door** → **auto-elevate to COMPLEX**

---

## Self-classification announcement template

At the start of every invocation, announce:

```
**Tier: <TRIVIAL|MODERATE|COMPLEX>**
**Rationale:** <which criteria matched, which auto-elevation triggers fired>
**Machinery enabled:** <Phase 0 steps / Rumsfeld components / Phase 5 scope>
**Override:** `--full` forces COMPLEX; `--quick` suppresses Phase 5.
```

This is transparent and auditable. The user can challenge the classification before the skill commits to the machinery.

**Classification step prose rule:** the classification step itself uses NEUTRAL words only (`specific`, `precise`, `structured`) — never Depth words. This prevents the prompt from priming toward a higher tier.
