<!-- changelog: added -->
- **Memory retrieve telemetry now says how many records were eligible.**

`retrieve` in `scripts/ai_memory.py` adds `records_considered` to its JSON output and its `AI_MEMORY_TELEMETRY` line. It counts the eligible records after the existing status, prune, schema, category, scope and dedupe filters, before the token-budget and `--max` selection, so comparing it with `records_selected` shows whether the budget is cutting relevant records. The disabled, branch-unavailable and git-error paths report `0`. The field is additive: no key is renamed or removed, the injected context text is unchanged, and only a count is emitted. Refs #8845.
