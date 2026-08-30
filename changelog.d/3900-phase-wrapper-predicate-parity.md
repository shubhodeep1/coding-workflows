<!-- changelog: fixed -->
- **Clarify, plan, implement, and orchestrator clarify-response wrappers now enforce the same trigger predicates as their reusable workflows.**

The `internal-{clarify,plan,implement,orchestrate-clarify-respond}.yml` callers and corresponding `workflow-templates/ai-*.yml` templates now reject unrelated issue comments and untrusted user or bot commands before dispatching reusable workflows. Eligible issue openings, trusted maintainer commands, established GitHub Actions bot markers, tracker exclusions, and non-PR guards remain available. A contract test now keeps each internal and consumer predicate aligned with its reusable workflow counterpart.

What this means for operators: consumer repositories receive fewer immediately skipped workflow runs without losing supported issue-to-PR automation entry points.
