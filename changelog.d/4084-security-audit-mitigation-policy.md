<!-- changelog: changed -->
- **Security-pass findings must recommend automated controls, not human gates.** The security-audit prompt, the exhaustion-judge prompt, the consolidated fix-issue body, and the advisory follow-up body now carry a mitigation policy that forbids recommending a human approval step, an operator-run command, or a manual sign-off; a recommendation may involve a person only when it starts with `HUMAN GATE REQUIRED:` and names the narrowest trigger condition.

Project #3965's security pass reported `prompt-injection-authorizes-pr-merge` (issue #4017) with the recommendation "require deterministic policy and independently authenticated human approval before merging or closing PRs". The fix issue carried that text verbatim, the implementer followed it (PR #4029), and every terminal review-blocked judge decision on the integration branch then waited for a maintainer to post `/review-blocked-approve`. PR #4079 sat pending for eleven hours behind that gate while the review sweep re-ran the judge every 30 minutes. The prompts now state the automation-bias rule the planner and implementer are already bound by (unattended_system_instructions.md §20), so the auditor phrases mitigations as identity- and provenance-scoped authorization, fail-closed validation, least-privilege tokens, or sandboxing instead.

| The numbers that matter | Value |
| --- | --- |
| Prompts changed | `prompts/mode-security-audit.txt`, `prompts/mode-judge-security-pass-exhaustion.txt` (and their `_templates/` sources) |
| Issue bodies changed | consolidated `[security-pass] … fix cycle N` issue, `[security-pass] Advisory: …` follow-up |
| Escape prefix | `HUMAN GATE REQUIRED:` |
| Regression test | `tests/test_security_audit_prompt_policy.py` |

What this means for operators: security-pass fix cycles keep the pipeline unattended by default. A finding that genuinely needs a person shows up with the `HUMAN GATE REQUIRED:` prefix in the findings table, so the gate is scoped to that condition rather than to every terminal decision.

### For contributors

Findings, severities, and the findings-JSON schema are unchanged; only the shape of `recommendation` text is constrained. The follow-up plan to narrow the existing gate on `orchestrator/project-3965` is `docs/plans/review-blocked-approval-gate-trust-scope-plan.md`.
