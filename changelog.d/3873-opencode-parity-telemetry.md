<!-- changelog: fixed -->
- **Review-autofix now records measured OpenCode token and cache usage without confusing missing telemetry with zero.** Reviewer text output and recovery behavior remain unchanged.

Production reviewer calls now retain OpenCode JSON events long enough to reconstruct reviewer text and aggregate `step_finish` token evidence. Existing usage markers gain an availability signal, run summaries distinguish measured zero cache reads from unavailable evidence, and cost reports show unavailable cache telemetry as `N/A` rather than calculating a misleading hit rate.

The cutover plan now records the auditable three-run Codex latency baseline and keeps the pre-P2 cache-read baseline explicitly unavailable. `@stable` remains held until three consecutive post-cutover production runs satisfy the amended parity criterion.
