<!-- changelog: fixed -->
- **Workflow cost reports no longer count malformed MCP log text as runtime traffic.** Structured Semble, Serena, and generic MCP events continue to populate the same telemetry fields.

`scripts/cost_audit.py` now requires each MCP event to carry the fields emitted by its canonical helper before including it in per-run or aggregate totals. `scripts/collect_workflow_logs.py` applies the same validation before retaining structured telemetry lines from full logs, so echoed markers are not reintroduced during wrapper/child deduplication. Counter echoes such as `SERENA_QUERY 0`, prose that merely mentions an event prefix, and partial query, fallback, or probe lines are ignored instead of inflating usage. Valid Semble fallback events retain their existing contract-test versus runtime classification, and malformed telemetry remains fail-open for the workflow.

What this means for operators: workflow analysis reports may show lower, more accurate MCP query, fallback, and probe totals when captured logs contain echoed counters or malformed event text; no emitter format or telemetry key changes.

### For contributors

Treat the existing Semble, Serena, and generic MCP emitter fields as the parser contract; add focused tests before relaxing required telemetry fields.
