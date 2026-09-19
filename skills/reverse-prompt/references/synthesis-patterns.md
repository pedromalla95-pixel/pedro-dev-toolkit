# Synthesis Patterns — Mega-Prompt Templates

Templates for assembling interview answers into structured mega-prompts.
The interviewer selects a base template by deliverable type, then fills slots from collected answers.

---

## Base XML Structure (all deliverables)

```xml
<mega-prompt>
  <role>
    <!-- WHO Claude should be for this task -->
    You are a {expertise_descriptor} with deep experience in {domain}.
    Your approach: {style_descriptors}.
  </role>

  <context>
    <!-- SITUATION and background -->
    <objective>{end_outcome — the real WHY}</objective>
    <audience>{primary_audience} — {what_they_know}, {their_emotional_state}</audience>
    <constraints>{hard_constraints — timeline, budget, tech stack, regulations}</constraints>
    <existing_assets>{what_we_can_build_on}</existing_assets>
    <stakeholders>{who_else_cares_and_why}</stakeholders>
  </context>

  <task>
    <!-- WHAT to produce -->
    <deliverable>{exact_format_and_type}</deliverable>
    <scope>{must_haves vs nice_to_haves}</scope>
    <priority>{the_one_thing_that_matters_most}</priority>
  </task>

  <requirements>
    <!-- HOW to approach it -->
    <approach>{structure, methodology, or pattern}</approach>
    <tone>{register — formal/casual/technical/friendly}</tone>
    <quality_bar>{what_good_vs_bad_looks_like}</quality_bar>
    <risks>{known_risks_and_how_to_handle}</risks>
  </requirements>

  <output_spec>
    <!-- EXACT deliverable format -->
    <format>{file_type, structure, sections}</format>
    <length>{word_count, file_count, page_count}</length>
    <includes>{rationale, options, commentary — yes/no}</includes>
    <iteration>{outline_first vs full_draft, versioning}</iteration>
  </output_spec>

  <assumptions>
    <!-- Three-tier assumption taxonomy -->
    <!-- CONVENTION: Domain default, low risk — can proceed silently -->
    <convention reason="{why this is standard}">{assumption_text}</convention>
    <!-- INFERRED: Derived from context, medium risk — surface for review -->
    <inferred reason="{what context supports this}">{assumption_text}</inferred>
    <!-- GUESSED: No signal, high risk — must confirm or convert to NEEDS INPUT -->
    <guessed reason="{why this guess was made}">{assumption_text}</guessed>
    <!-- NEEDS INPUT: Zero signal, cannot even guess responsibly -->
    <!-- [NEEDS INPUT: specific question to resolve this slot] -->
  </assumptions>
</mega-prompt>
```

### Assumption Tiers

| Tier | Tag | Risk | Action |
|------|-----|------|--------|
| Convention | `<convention reason="...">` | Low | Proceed silently. These are standard domain defaults (e.g., "professional tone for business doc"). |
| Inferred | `<inferred reason="...">` | Medium | Surface for review in the presentation. User should confirm or correct. |
| Guessed | `<guessed reason="...">` | High | **Blocker.** Must be confirmed before proceeding. Counts against the assumption budget. |
| Needs Input | `[NEEDS INPUT: question]` | Critical | Cannot proceed without answer. Triggers follow-up (see below). |

**NEEDS INPUT handling:**
- 1-2 `[NEEDS INPUT]` markers → Ask a mini follow-up before presenting the mega-prompt. Don't re-interview — just ask the specific questions.
- 3+ `[NEEDS INPUT]` markers → The interview was insufficient. Return to Phase 1 and ask the remaining questions. Don't force synthesis on incomplete data.

---

## Domain Variants

### Writing Deliverable

```xml
<role>
  You are an expert {content_type} writer specializing in {topic_area}.
  Voice: {tone_descriptors}. Style model: {reference_piece_if_provided}.
</role>

<context>
  <objective>{persuade | inform | entertain | establish_authority}</objective>
  <audience>{reader_profile} reading on {platform}</audience>
  <seo>{keywords_if_applicable}</seo>
  <brand_voice>{style_guide_notes}</brand_voice>
</context>

<task>
  <deliverable>{article | blog_post | email | doc | social}</deliverable>
  <thesis>{single_takeaway}</thesis>
  <counterargument>{strongest_objection_to_address}</counterargument>
</task>

<requirements>
  <structure>{listicle | narrative | how-to | comparison}</structure>
  <evidence>{examples | analogies | data | stories}</evidence>
  <sources>{specific_references}</sources>
</requirements>

<output_spec>
  <length>{word_count}</length>
  <sections>{headers, intro, conclusion, CTA}</sections>
  <placeholders>{where_user_adds_own_content}</placeholders>
</output_spec>
```

### Code / Technical Deliverable

```xml
<role>
  You are a senior {language/framework} developer with expertise in {domain}.
  Coding style: {patterns, conventions}. Priority: {correctness | performance | readability}.
</role>

<context>
  <objective>{problem_being_solved_for_end_user}</objective>
  <codebase>{greenfield | existing — description}</codebase>
  <stack>{languages, frameworks, databases, infra}</stack>
  <maintainer>{who_maintains — skill_level}</maintainer>
</context>

<task>
  <deliverable>{script | module | API | full_app | refactor}</deliverable>
  <mvp>{simplest_useful_version}</mvp>
  <scope>{must_have_features} | nice-to-have: {stretch_features}</scope>
</task>

<requirements>
  <architecture>{monolith | microservice | serverless | script}</architecture>
  <error_handling>{fail_fast | graceful_degradation | retry}</error_handling>
  <testing>{unit | integration | manual | none}</testing>
  <performance>{latency, throughput, scale targets}</performance>
</requirements>

<output_spec>
  <structure>{single_file | multi_file — layout}</structure>
  <includes>{tests | docs | comments | setup_instructions}</includes>
  <ready_state>{production | prototype | pseudocode}</ready_state>
</output_spec>
```

### Strategy / Business Deliverable

```xml
<role>
  You are a {strategy | operations | finance} advisor with experience in {industry}.
  Approach: {data-driven | framework-based | narrative}. Rigor: {executive_summary | deep_analysis}.
</role>

<context>
  <objective>{decision_to_support}</objective>
  <decision_maker>{who_decides — what_they_value}</decision_maker>
  <landscape>{competition, market, constraints}</landscape>
  <resources>{budget, team, timeline}</resources>
</context>

<task>
  <deliverable>{analysis | proposal | plan | comparison | memo}</deliverable>
  <options>{top_options_being_evaluated}</options>
  <success_metrics>{how_success_is_measured}</success_metrics>
</task>

<requirements>
  <evidence>{what_data_supports_recommendation}</evidence>
  <risk_analysis>{plan_b, failure_modes}</risk_analysis>
  <rollout>{pilot | phased | big_bang}</rollout>
</requirements>

<output_spec>
  <format>{deck | memo | one-pager | detailed_report}</format>
  <visuals>{charts | diagrams | matrices — yes/no}</visuals>
  <includes>{recommendations | findings_only | both}</includes>
</output_spec>
```

### Prompt / AI Deliverable

```xml
<role>
  You are a prompt engineer specializing in {model_family} with expertise in {use_case_domain}.
  Approach: {structured | minimal | conversational}. Priority: {reliability | creativity | safety}.
  Tone: {warm | professional | technical | casual}.
</role>

<context>
  <objective>{what_the_prompt_needs_to_accomplish}</objective>
  <model>{target_model — capabilities_and_limits}</model>
  <deployment>{one-off | daily | high-volume}</deployment>
  <maintainer>{who_iterates_on_prompt}</maintainer>
</context>

<task>
  <deliverable>{system_prompt | task_prompt | agent_config | skill}</deliverable>
  <input_spec>{expected_input_format_and_variability}</input_spec>
  <failure_modes>{known_edge_cases_and_hallucination_risks}</failure_modes>
</task>

<requirements>
  <rigidity>{predictable_output | creative_range}</rigidity>
  <output_format>{JSON | markdown | plain_text | structured}</output_format>
  <guardrails>{safety, scope, hallucination_prevention}</guardrails>
  <tools>{available_tools_and_capabilities}</tools>
</requirements>

<output_spec>
  <format>{raw_text | XML | JSON}</format>
  <includes>{test_cases | usage_guide | version_notes}</includes>
</output_spec>
```

### Operations / Food-Service Deliverable

```xml
<role>
  You are an operations specialist with expertise in {food_service | restaurant_management | regulatory_compliance}.
  Approach: practical, actionable, {operator_accessible | management_level | regulator_ready}.
  Priority: {compliance | efficiency | cost_control | communication}.
</role>

<context>
  <objective>{operational_outcome — what changes in daily work}</objective>
  <operator>{who_executes_daily — name_role_tech_comfort}</operator>
  <business>{type_and_scale — restaurant, catering, production}</business>
  <systems>{current_tools — POS, ERP, spreadsheets, paper}</systems>
  <regulatory>{applicable_regulations — DGII, HACCP, labor, health}</regulatory>
</context>

<task>
  <deliverable>{SOP | checklist | calculation | form | communication | report}</deliverable>
  <scope>{what_process_or_requirement_this_covers}</scope>
  <integration>{systems_this_connects_to}</integration>
</task>

<requirements>
  <usability>{operator_tech_level — must be usable by {operator_name}}</usability>
  <compliance>{specific_regulatory_requirements}</compliance>
  <approval_chain>{who_signs_off}</approval_chain>
  <failure_mode>{what_happens_if_process_breaks_or_gets_skipped}</failure_mode>
</requirements>

<output_spec>
  <format>{printable | digital | both}</format>
  <language>{deliverable_language}</language>
  <visuals>{checklists | flowcharts | tables | photos — yes/no}</visuals>
  <audience>{kitchen_staff | management | regulators | accountant}</audience>
</output_spec>
```

### Custom Template Guidance

When no domain variant fits the deliverable:
1. Start from the **Base XML Structure**.
2. Add a maximum of **3 domain-specific fields** that capture what the base template misses. Choose fields that would cause the biggest quality drop if left out.
3. Name custom fields descriptively — `<regulatory>` not `<field1>`.
4. Document your additions in the `<assumptions>` section so the user understands why you deviated from standard templates.
5. If you find yourself adding more than 3 fields, check if an existing domain variant is closer than you thought — adapt it instead of building from scratch.

---

## Pre-Synthesis Checklist

Before presenting the mega-prompt, verify each item. If a check fails, take the specified remediation action — don't skip it.

| Check | If Failed → |
|-------|-------------|
| Every `<guessed>` tag has clear reasoning | Add reasoning, or convert to `[NEEDS INPUT]` if you can't justify the guess |
| No slot is left as a placeholder (`{...}`) | Fill from state, mark as `<convention>`, `<inferred>`, or `[NEEDS INPUT]` |
| `<guessed>` count ≤ assumption budget | Return to interview for highest-risk gaps (those without even weak signal) |
| Priority / #1 concern is prominently reflected | Move it to `<priority>` field and ensure `<approach>` addresses it |
| Tone matches what the user described | Re-read the user's own words; replace "professional" with their actual descriptor |
| Scope is bounded — no unbounded "and more" | Replace vague scope with explicit must-have/nice-to-have split |
| Output spec is concrete enough to judge "done" | Add measurable criteria (word count, section list, file type) |
| Counterarguments / risks are addressed, not ignored | Add `<risks>` entries for any mentioned concerns |
| `[NEEDS INPUT]` count is ≤ 2 | If 3+, return to interview — synthesis is premature |

## Presentation Format

When presenting the synthesized prompt to the user, lead with a human-readable summary before the XML:

```
## Synthesized Mega-Prompt

### Quick Summary

| Field | Value |
|-------|-------|
| **Goal** | [one-sentence objective] |
| **Audience** | [primary audience] |
| **Deliverable** | [format and type] |
| **Priority** | [the #1 thing] |
| **Tone** | [register] |
| **Conventions** | [count] |
| **Inferred** | [count] — review below |
| **Guessed** | [count] — confirm below |
| **Needs Input** | [count] |

Here's the full mega-prompt I'll use to generate your deliverable:

[XML mega-prompt block]

### Assumptions I Made
**Conventions** (low risk, proceeding silently):
- [convention] — reasoning

**Inferred** (medium risk, please review):
- [inferred] — reasoning

**Guessed** (high risk, need your confirmation):
- [guessed] — reasoning

### Key Decisions
- Priority: {what I'm optimizing for}
- Trade-off: {what I'm deprioritizing and why}

**Does this capture your intent? I won't proceed until you approve (or adjust).**
```

### Revision Changelog

When presenting a **revised** mega-prompt (after user requested changes), add a "Changes Made" section before the XML block:

```
### Changes Made (from previous version)
- [Changed X from A to B — per your feedback on Y]
- [Added Z — you mentioned this was missing]
- [Removed W — conflicted with your priority of V]

Here's the updated mega-prompt:
[XML block]
```

This helps the user see exactly what changed without re-reading the entire prompt. Omit this section on first presentation.
