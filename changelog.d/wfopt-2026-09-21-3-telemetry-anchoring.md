<!-- changelog: fixed -->
- **Context-budget and break-glass telemetry now counts only lines that start with the marker.** Before, `CONTEXT_BUDGET_WARN:` and `BREAK_GLASS:` counted anywhere a line contained them after a space, so a report, summary or stderr tail that quoted a marker added a fake event. Audit run 35571055909 counted 13 context warnings, of which 6 were real.

`CONTEXT_BUDGET_WARN_RE` and `BREAK_GLASS_RE` in `scripts/cost_audit.py` now require the marker as the first token of the line. Only an optional UTF-8 BOM and an optional GitHub Actions log timestamp may come before it, because every emitter prints the bare marker at column 0. `scripts/collect_workflow_logs.py` imports both patterns, so its counts and its structured-line dedupe keys change the same way. Names, named groups and field grammar are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Context warnings counted in audit run 35571055909 | 13 before, 6 real |
| Patterns changed | 2 |

What this means for workflow-log analysis: `context_budget_warn_count` and `break_glass_count` drop to real emissions, so comparisons with older reports show a step down. A quoted line that itself starts with the bare marker (optionally after a timestamp) still counts. The MCP and OpenRouter matchers keep their old rule and can still count quoted copies.
