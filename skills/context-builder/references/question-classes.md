# Mandatory Question Classes (3)

Three question classes added per the blindspot audit. Each has a canonical script and a specific Layer where it fires.

---

## 1. Pre-mortem Narrative (Gary Klein)

**Fires in:** Layer 4 Validation — mandatory for every MODERATE+ invocation.

**Purpose:** Surfaces failure modes that Rumsfeld Q4 probe alone misses. By asking for a backward-written failure story, the user's imagination generates specific failure paths the forward-thinking Q4 sweep often skips.

**Canonical script:**

```
**Pre-mortem:** Imagine it's {DATE + 6 months} and we're looking back at this project. It failed — we abandoned it, or it caused a problem that forced a rollback. In 3 short paragraphs, write the failure narrative: what broke first, what cascaded, and what we wish we'd known at the start.
```

**What to do with the output:**
1. Extract each failure cause → add to Q4 Unknown Unknowns as a now-surfaced risk.
2. Tag each cause by MECE category (technical, compliance, stakeholder, etc.).
3. For causes already covered by existing questions, mark as "previously covered."
4. For new causes, add to the open-question list and ask the user about the mitigation.

**Do NOT accept:** "nothing would go wrong." Escalate per Rule 4 — "I need concrete failure narratives. Even successful projects have near-misses. What are the 3 most plausible ways this could fail?"

---

## 2. Counterfactual / Premise Challenge

**Fires in:** Layer 1 Strategic — first or second question in every MODERATE+ invocation.

**Purpose:** All 10 MECE categories assume the project *should happen*. This class challenges the premise itself. If doing nothing is actually better, building the right thing wrong is worse than not building.

**Canonical script (two-part):**

```
**Counterfactual challenge:**
(a) What is the cost of doing nothing — leaving the current state as-is for the next 6 months?
(b) What is the strongest argument against this project? What would a thoughtful critic say is wrong about the premise?
```

**What to do with the output:**
- If the cost of doing nothing is ≤ the cost of building → flag and ask if the project should be deferred or re-scoped.
- If the strongest counter-argument has no answer → flag as a premise risk and add to Q2 Known Unknowns.
- If both (a) and (b) get shallow answers → escalate per Rule 4. Don't move past Layer 1 until the premise is defensible.

**Do NOT accept:** "we just need to build this" without a reason. The skill's job is to make the user defend the premise, not endorse it.

---

## 3. Reversibility Audit (Bezos)

**Fires in:** Layer 4 Validation — applied to every architectural or data-model decision surfaced in the interview.

**Purpose:** Classify each decision by reversibility cost. One-way doors warrant extra scrutiny (full Q4 sweep + Pre-mortem round). Two-way doors can be decided faster and revisited.

**Canonical script:**

```
**Reversibility audit:** For each decision surfaced in this interview, tag it:
- [REVERSIBLE] — can be undone in <1 day without data loss or external rollback
- [ONE-WAY] — irreversible or reversible only at significant cost (data migration, public deployment, schema destruction, third-party integration)

For every [ONE-WAY] decision:
  1. Run a targeted Pre-mortem focused on that specific decision.
  2. Extend the Rumsfeld Q4 sweep with this decision as a probe anchor.
  3. Confirm explicitly: "This is a one-way door. Are you ready to commit, or should we keep it two-way for now?"
```

**What to do with the output:**
- [REVERSIBLE] decisions move forward without extra scrutiny.
- [ONE-WAY] decisions get the extra treatment above, and the tag is preserved into the PRD risk section.
- Hidden [ONE-WAY] doors (decisions that looked reversible but aren't) become a Q4 Unknown Unknown to probe further.

**Do NOT accept:** "everything is reversible." Most decisions have a reversibility cost; the skill's job is to surface the real cost, not let the user wave it away.

---

## When to skip

- **TRIVIAL tier:** skip all three classes. The criteria for TRIVIAL already exclude one-way doors, stakeholders, and deadlines that would make these questions load-bearing.
- **MODERATE tier:** Pre-mortem and Counterfactual are mandatory. Reversibility audit runs only if any [ONE-WAY] candidate is surfaced.
- **COMPLEX tier:** all three are mandatory. Skipping any of them = Rule 7 handoff BLOCKED.
