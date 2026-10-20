<!-- changelog: fixed -->
- **Codex token usage now counts only the CLI's own `tokens used` lines, once per run.** Before, `CODEX_TOKENS_RE` in `scripts/cost_audit.py` matched `tokens used` anywhere in a line, so prompt and diff text echoed into logs added fake usage, and a parent job log that repeated its child step's usage counted it twice. The collector reported 7.945M Codex tokens in `analysis/workflow-optimization-2026-09-22.md`, far above the usage the sampled runs really had.

`CODEX_TOKENS_RE` now counts only a line that is exactly `tokens used` followed by a line that is only the number, or a single line `tokens used <number>`. Each line may start with an optional UTF-8 BOM, the `<job>\t<step>\t` fields of `gh run view --log`, ANSI escapes and a GitHub Actions timestamp. The new `CODEX_TOKENS_HEADER_RE` matches the header line alone; `scripts/collect_workflow_logs.py` adds it to `STRUCTURED_COST_TELEMETRY_PATTERNS`, so the existing parent/child dedupe drops a parent's copy of a child's header and the orphaned number line no longer counts. The name and capture group of `CODEX_TOKENS_RE` are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Codex tokens in the 2026-09-22 collector report | 7.945M, inflated |
| Patterns changed or added | 2 |

What this means for workflow-log analysis: `codex_tokens_used` and `codex_calls` drop to the real CLI output, so comparisons with older reports show a step down. A quoted line that is itself exactly `tokens used <number>` still counts.
