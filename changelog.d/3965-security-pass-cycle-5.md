<!-- changelog: fixed -->
- **AI workflow model execution now fails closed against mutable code, unauthorized model spend, and evicted resolver state.** Custom Codex installers are pinned to a reviewed commit, reusable-workflow support runs from the defining immutable SHA, the provider broker enforces phase-specific model/token/price policy, and resolver retry discovery verifies every paginated comment before selecting the highest generation.

The broker's total output-token budget is settled against the provider-reported `usage` of each completed response instead of staying charged at the per-request ceiling, so an agentic editor or implementer session is bounded by tokens it actually generated rather than failing closed after four turns (observed on PR #4077, Actions runs 34692519987, 34700918528 and 34702442346).

What this means for operators: no manual migration is required. Existing workflows keep their triggers and interfaces, while malformed workflow identity, broker policy violations, and uncertain retry-state reads stop or defer privileged automation instead of falling back to mutable or unbounded execution.
