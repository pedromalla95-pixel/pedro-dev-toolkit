## Criteria

- [c1] unconditional: The skill MUST read the target plan file — a Read tool_use whose input.file_path is the plan path (or a Bash/Glob path-resolution call followed by a Read of the resolved path) — BEFORE the first challenge fan-out spawn (Workflow tool_use name="plan-challenge", or any plan-* Agent/Task tool_use on the legacy fallback path) and before any verdict text block, whichever comes first.
  matcher: requires Read.file_path regex "."
- [c2] conditional-on: The window contains NO plan-challenge fan-out tool_result carrying an `error` key and NO codex-verify Bash tool_result with RC=3 / CODEX_UNAVAILABLE — i.e. neither of the two paths on which SKILL.md mandates STOP-without-verdict (Step 3a "surface the message verbatim and STOP... never emit a verdict from a partial fan-out"; Exit-code policy RC=3 "do NOT run any verification this turn"). — The skill MUST emit a terminal verdict line in an assistant text block containing one of the literal tokens PROCEED, REVISE, or RETHINK (the abbreviated `PROCEED (trivial)` satisfies this on the Step-2 TRIVIAL branch).
  matcher: requires assistant_text regex "\\b(PROCEED|REVISE|RETHINK)\\b"
  matcher-absence: exhaustive
- [c3] conditional-on: The window contains a challenge fan-out spawn: a Workflow tool_use identifying "plan-challenge", OR one or more Agent/Task tool_use events with subagent_type in {plan-challenger, plan-researcher, plan-devils-advocate, plan-granularity-checker, plan-domain-expert}. — An assistant text block stating the complexity assessment and team sizing — a numeric complexity score AND the literal token BASE or SCALED, per the template `Complexity: N/14. Team: BASE|SCALED` — MUST appear BEFORE the first fan-out spawn event.
  matcher: requires assistant_text regex "Complexity:\\s*\\d+/14"
  matcher-absence: exhaustive
- [c4] conditional-on: The window contains a challenge fan-out spawn (as defined in c3) that returned a tool_result with no `error` key. — At least one synthesizer spawn MUST occur — an Agent/Task tool_use whose prompt references `challenger-charters/plan-synthesizer.md` (or subagent_type plan-synthesizer on the legacy fallback path) — positioned AFTER a fan-out tool_result and BEFORE the final verdict text block.
  matcher: requires Agent.prompt contains "plan-synthesizer"
  matcher: requires Task.prompt contains "plan-synthesizer"
  matcher: requires Agent.subagent_type equals "plan-synthesizer"
  matcher: requires Task.subagent_type equals "plan-synthesizer"
  matcher-absence: exhaustive
- [c5] conditional-on: Plan content returned in a Read tool_result matches at least one § Infra Triggers keyword — case-insensitive, `\b`-guarded, inflections explicit: `secrets?`, `credentials?`, `env var`, `service role`, `vercel`, `supabase`, `aws`, `gcp`, `cloudflare`, `deployments?`, `production`, `prod`, `staging`, `migrat(e|es|ed|ing|ion|ions)`, `database url`, `project ref`, `connection string`, `feature flag`, `rollout`, `invoices?`, `itbis`, `fiscal(es)?`, `ncfs?`, `rnc`, `tax(es)?`, `payroll`, `suplidor(es)?`, `ledgers?`, `backfill(s|ed|ing)?`, `inventory`, `inventario`, `system(d|ctl)`, `daemons?`; plus `.env`/`.env.local`/`.env.production` with a `(?:^|[^\w.])` left-guard (a `\b` cannot guard a leading dot). Apply the regex — do not eyeball it; it is mirrored executably at ~/.claude/hooks/plan-verifier.js § INFRA_RE. — The verification output MUST contain a section titled exactly `## Empirical Probe Findings`.
  matcher: requires assistant_text contains "## Empirical Probe Findings"
  matcher-absence: exhaustive
- [c6] conditional-on: The plan is infra-flagged per the c5 keyword regex AND the final verdict text block contains PROCEED. — The `## Empirical Probe Findings` section MUST contain at least one bullet that is not the literal `unknown — tool unavailable`.
  matcher: requires assistant_text contains "## Empirical Probe Findings"
