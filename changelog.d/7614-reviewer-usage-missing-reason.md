<!-- changelog: changed -->
- **Reviewer OpenRouter usage lines now say why usage is missing.** When a reviewer's `INFO: openrouter usage` line reports `usage_available=false`, it ends with `usage_missing_reason=<reason>`: `log_missing` (no log file), `no_json_events` (no JSON in the log), `no_usage_event` (no `step_finish` usage and no `usage` object), `incomplete_step_finish` (`step_finish` events with a missing or invalid token field) or `incomplete_fields` (a `usage` object without every token field).

About a quarter of model calls in a recent cost review had no usage data and nothing said why. The new field comes last on the line, so lines with usage are unchanged and `scripts/cost_audit.py`, the iteration summary and the ledger parser read the line as before.
