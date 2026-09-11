<!-- changelog: fixed -->
- **AI workflow model execution now fails closed against mutable code, unauthorized model spend, and evicted resolver state.** Custom Codex installers are pinned to a reviewed commit, reusable-workflow support runs from the defining immutable SHA, the provider broker enforces phase-specific model/token/price policy, and resolver retry discovery verifies every paginated comment before selecting the highest generation.

What this means for operators: no manual migration is required. Existing workflows keep their triggers and interfaces, while malformed workflow identity, broker policy violations, and uncertain retry-state reads stop or defer privileged automation instead of falling back to mutable or unbounded execution.
