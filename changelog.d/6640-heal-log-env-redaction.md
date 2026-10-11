<!-- changelog: security -->
- **Workflow-failure heal intake now redacts step `env:` values and credential headers from the job logs it hands to the diagnosis agent and files in heal issues.** Security finding `basic-auth-survives-evidence-redaction` (high) is closed.

`scripts/workflow_failure_heal.py filter-log` is what the intake (`scripts/workflow_failure_heal_intake.sh`) uses to cut each failed job's log down to the text in `FILTERED_LOG`. That text goes into the diagnosis prompt, the dedup signature and the heal issue evidence. Until now it kept every step header's `env:` block word for word and never ran `redact_secrets`. A value the runner does not mask, such as a token minted in an earlier step or an `Authorization: Basic …` header printed by `curl -v`, therefore reached the model and could be copied into an issue. The finding cited `scripts/workflow_failure_heal_evidence.py`, which is not on `main`; `filter_log` is the live path with the same leak.

`filter_log` now does three things:

- It keeps each env variable's name and replaces its value with `[redacted]`. Any other line inside an `env:` block, such as a multi-line value, is replaced whole. The block ends only at the header close, so a value line that looks like `with:` or `shell:` cannot switch redaction off, and an inline `env: <value>` line keeps only `env:`. In a timestamped log, an untimestamped line inside the block is a value continuation and is redacted even when it reads `##[endgroup]` or `##[group]Run`. A marker line inside the block that is followed by an untimestamped line is also treated as part of a value and redacted.
- When a step header that opened an `env:` block is still open where the log ends (a truncated log), it drops everything from that header onward and adds one `[env block omitted: unterminated step header]` line. The other jobs and the earlier part of the log are kept. An open header with no `env:` block holds no env values, so its lines are kept and the step's output still reaches the diagnosis.
- It runs `redact_secrets` over the whole text before the tail and byte cut, so the cut can never leave part of a token behind without its prefix.

The `Authorization` pattern also now matches in any letter case.

| The numbers that matter | Value |
| --- | --- |
| Env values left verbatim in filtered heal logs | 0 (was: every one) |
| New GitHub API calls | 0 |
| CLI, env var or payload changes | 0 |

What this means for operators: heal diagnoses no longer see step env values, only their names. `with:` inputs of `uses:` steps are still shown as before.
