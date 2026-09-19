# Question Bank — Reverse Prompt Interview

Questions organized by **Phase x Domain**. The interviewer selects dynamically based on:
1. Detected domain from the user's initial goal statement
2. Current interview phase (Strategic → Context → Tactical → Format)
3. What's already been answered (skip redundant questions)

**Priority markers**: Questions marked `[P]` are PRIMARY — ask these first unless already answered. The escape hatch guard clause requires PRIMARY questions in Strategic + one other phase before allowing early exit.

---

## Phase: Strategic (Why / What)

Purpose: Establish the real objective, challenge assumptions, surface XY problems.

### Universal (all domains)
- `[P]` What's the **end outcome** you need? Not the deliverable — what changes in your world when this is done?
- `[P]` Why now? What triggered this request today?
- What happens if we do nothing? What's the cost of inaction?
- `[P]` Who benefits from this and how will they use it?
- Have you tried solving this before? What worked / didn't?
- Is this a one-off or the start of a recurring need?
- What does "done well" look like vs "done badly"? Give me a concrete example of each.
- Do you have domain expertise in this area, or are you learning as you go? (This shapes how much I explain vs assume.)

### Writing
- `[P]` What's the single takeaway you want readers to walk away with?
- Is this meant to persuade, inform, entertain, or establish authority?
- What existing piece (article, post, doc) is closest to what you want?
- What's the strongest counterargument to your thesis?

### Code / Technical
- `[P]` What problem does this solve for the end user (not the developer)?
- Is this greenfield or modifying existing code? If existing, what's the current pain?
- `[P]` What are the hard constraints? (language, framework, infra, budget, timeline)
- What's the simplest version that would be useful?

### Strategy / Business
- `[P]` What decision does this support? Who's the decision-maker?
- What are the top 2-3 options you're already considering?
- What would make you choose option A over B?
- `[P]` What's the risk you're most worried about?

### Prompts / AI
- `[P]` What model and context will this prompt run in?
- What does the prompt need to handle that a human instruction wouldn't?
- `[P]` What failure modes have you seen with current prompts?
- Is this for a single task or a persistent system prompt?

### Operations / Food-Service
- `[P]` What's the operational problem? (workflow bottleneck, compliance gap, cost control, communication)
- Who executes this day-to-day? What's their tech comfort level?
- `[P]` Is this about improving an existing process or meeting a regulatory requirement?

---

## Phase: Context (Who / Where)

Purpose: Map the environment — audience, constraints, existing assets, stakeholders.

### Universal
- `[P]` Who's the primary audience? Describe them in one sentence.
- What do they already know? What can you assume vs must explain?
- `[P]` What tone/register fits this audience? (formal, casual, technical, friendly)
- Are there brand guidelines, style guides, or existing conventions to follow?
- What existing assets can we build on? (docs, data, templates, prior work)
- Who else has a stake in this? Anyone who needs to approve or will be affected?

### Writing
- `[P]` Where will this be published? (blog, internal doc, social, email, presentation)
- What's the reader's emotional state when they encounter this? (searching, browsing, assigned)
- Are there SEO keywords or topics you need to hit?
- What's the word count / length expectation?

### Code / Technical
- `[P]` What's the tech stack? (languages, frameworks, databases, deployment target)
- Who maintains this after you? What's their skill level?
- What's the testing strategy? (unit, integration, manual, none)
- Are there performance requirements? (latency, throughput, scale)

### Strategy / Business
- `[P]` What's the competitive landscape? Who else is doing this?
- What resources (budget, team, time) are available?
- What's the timeline? Hard deadline or flexible?
- Are there regulatory or compliance requirements?

### Prompts / AI
- `[P]` What's the expected input format and variability?
- What tools/capabilities does the model have access to?
- What's the volume? (one-off, daily, thousands/day)
- Who will maintain and iterate on this prompt?

### Operations / Food-Service
- `[P]` What type of business and what scale? (restaurant, catering, food production, retail)
- What systems are already in place? (POS, inventory, ERP, spreadsheets, paper-based)
- `[P]` Are there regulatory requirements? (DGII, HACCP, health department, labor law)
- Who needs to approve changes? (owner, manager, accountant, regulator)

---

## Phase: Tactical (How)

Purpose: Nail down approach, structure, sequence, and trade-offs.

### Universal
- `[P]` Of everything we've discussed, what's the #1 priority if we have to cut scope?
- What's the biggest risk or unknown in this project?
- Are there dependencies or blockers I should know about?
- What's your preferred approach — should I propose options or go with best judgment?

### Writing
- `[P]` What structure works best? (listicle, narrative, how-to, comparison, Q&A)
- Should I use examples, analogies, data, or stories to support points?
- Are there specific sources or references to cite?
- How much original analysis vs synthesis of existing knowledge?

### Code / Technical
- `[P]` What's the architecture preference? (monolith, microservice, serverless, script)
- How should errors be handled? (fail fast, graceful degradation, retry)
- What's the migration/rollback strategy if this breaks?
- Are there specific patterns or anti-patterns to follow/avoid?

### Strategy / Business
- `[P]` What metrics define success? How will you measure?
- What's the rollout plan? (pilot, phased, big bang)
- What's plan B if the primary approach fails?
- Who needs to be convinced and what evidence do they need?

### Prompts / AI
- `[P]` Should the prompt be rigid (predictable output) or flexible (creative range)?
- What output format? (JSON, markdown, plain text, structured)
- How should the prompt handle edge cases and ambiguity?
- What guardrails are needed? (safety, scope, hallucination prevention)

### Operations / Food-Service
- `[P]` What kind of deliverable? (process document, calculation tool, communication template, compliance form)
- Who in the approval chain needs to sign off?
- Does this need to integrate with existing systems? (ERP, accounting, POS, spreadsheets)
- What happens if this process breaks or gets skipped?

---

## Phase: Format (What Exactly)

Purpose: Pin down the exact deliverable spec — no ambiguity about what gets produced.

### Universal
- `[P]` What format should the final deliverable be? (doc, code, spreadsheet, presentation, prompt)
- What's the expected length/size?
- `[P]` Are there sections or components that are must-haves vs nice-to-haves?
- Do you want a single polished output or options to choose from?
- Should I include rationale/commentary or just the deliverable?

### Writing
- `[P]` Do you need an outline first or go straight to full draft?
- Should I include a title, subtitle, meta description?
- Any specific formatting? (headers, bullets, callouts, images placeholders)
- Should I flag places where you need to add your own examples/data?

### Code / Technical
- `[P]` Single file or multi-file? What's the file structure?
- Include tests? Documentation? Comments?
- Ready to run or pseudocode/architecture first?
- Should I include setup instructions?

### Strategy / Business
- `[P]` Executive summary or detailed analysis?
- Include recommendations or just findings?
- Visual aids? (charts, diagrams, matrices)
- What's the presentation format? (deck, memo, one-pager)

### Prompts / AI
- `[P]` Deliver as raw text, XML-structured, or JSON?
- Include test cases / example inputs-outputs?
- Should I include a usage guide with the prompt?
- Version it or deliver as final?

### Operations / Food-Service
- `[P]` Who's the primary reader? (kitchen staff, management, regulators, accountant)
- Does it need to be printable for posting/display, or digital-only?
- `[P]` What language should the deliverable be in? (match the audience, not the interview)
- Should it include visual aids? (checklists, flowcharts, tables, photos)

---

## Follow-Up Probes (for vague answers)

Use the Elicit-Provide-Elicit pattern when an answer lacks specificity. First draw out what they know, then offer context or options, then draw out their preference:

| Vague Signal | Elicit | Provide | Elicit Again |
|-------------|--------|---------|--------------|
| "I don't know" | "What's your gut feeling, even if uncertain?" | Offer 2-3 concrete options with trade-offs | "Which of these resonates, or what would you change?" |
| "Everything" / "All of it" | "If you could only pick ONE aspect, which matters most?" | Explain why prioritizing helps quality | "Given that, what's your top priority?" |
| "Whatever you think" | "What would make you reject my suggestion?" | Propose a specific default with reasoning | "Does this work, or does something feel off?" |
| "It depends" | "What's the most common case?" | Acknowledge complexity, suggest handling the common case first | "Should I optimize for that case and note exceptions?" |
| "Something like X" | "What specifically about X appeals to you?" | Identify the principle behind X | "Is it the [principle] you want, or the exact format of X?" |
| "Make it good" | "Show me an example of 'good' — a link, screenshot, or description" | If they can't, propose quality criteria | "Would you judge quality by [criteria]?" |
| "ASAP" | "Is this blocking other work? What's the actual deadline?" | Explain how timeline affects scope | "Given [timeline], should I cut scope or cut polish?" |
| One-word answer | "Can you expand? I want to capture the nuance." | Reflect back what the one word might mean | "Am I reading this right, or is there more to it?" |

---

## Domain Detection Heuristics

Match user's goal statement against these keywords to select domain-specific questions.

**Important**: Domain detection is a starting signal, not a commitment. If domain-specific questions feel wrong after 1-2 questions, pivot to Universal. The goal is relevant questions, not category fidelity.

| Domain | Keywords |
|--------|----------|
| Writing | blog, article, post, copy, content, write, draft, newsletter, email, compose, author, editorial, manuscript, copywriting, ghostwrite, prose |
| Code | build, implement, create app, fix bug, refactor, API, function, script, develop, engineer, architect, deploy, debug, program, automate |
| Strategy | plan, strategy, decide, evaluate, compare, analyze, roadmap, proposal, assessment, benchmark, forecast, due diligence, market research, competitive analysis |
| Prompts | prompt, system prompt, instructions, persona, chatbot, agent, LLM, fine-tune, few-shot, chain-of-thought, guardrails, model config |
| Operations | cocina, restaurante, menú, receta, inventario, DGII, HACCP, kitchen, restaurant, menu, recipe, inventory, food safety, compliance, operations, workflow, process, SOP, social media, social, Instagram, Facebook, TikTok, redes sociales |

**Keyword matching notes:**
- **Compound required**: "formulario 606", "formulario 607" — bare "606"/"607" are too ambiguous to trigger Operations alone.
- **Co-occurrence required**: "proveedor" / "supplier" only triggers Operations when paired with another Operations keyword (e.g., "proveedor de cocina", "supplier for the restaurant").
- **Bare keywords OK**: "cocina", "restaurante", "DGII", "HACCP" — unambiguous enough to trigger alone.

### Tiebreaker Rules

When multiple domains match:
1. **Deliverable domain wins** over subject domain. (Writing *about* code → Writing. Code *for* a strategy tool → Code.)
2. If still ambiguous after applying rule 1, **ask the user**: "I'm seeing elements of [A], [B], (and [C]) — which best describes what you need built?" (Works for 2-way or n-way ties.)
3. Use **Universal + primary domain** questions. Add 1-2 questions from the secondary domain only if they're clearly relevant.

### No-Match Fallback

If zero domains match the goal statement:
1. Use **Universal questions only** for all phases.
2. Add a domain-clarification question in Strategic: "This doesn't fit my usual categories — what field or discipline does this fall under?"
3. Use the **Base XML template** in synthesis (not a domain variant).
4. If the user's answer to the clarification question suggests a domain, pivot to that domain's questions for remaining phases.
