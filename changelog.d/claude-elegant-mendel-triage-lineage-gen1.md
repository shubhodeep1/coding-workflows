<!-- changelog: fixed -->
- **Check-failure triage no longer crashes on ordinary pipeline PRs.** A failing CI run on an `ai/issue-<N>` PR whose source issue is not a triage issue now starts a new lineage at generation 1 instead of aborting with `parent_generation_missing_or_malformed`.

Since #6273 routed every failed `CI` run on a pull request through `scripts/check_failure_triage.sh`, the lineage step assumed the PR's source issue was itself a triage issue carrying `<!-- check-failure-triage:gen=N -->`. Every implement, activation-gap, heal and orchestrator-wave PR (all on `ai/issue-<N>` branches) hit that assumption: the triage run exited 1 before collecting any context, the `Internal: AI Check Failure Triage` workflow showed a failed run on `main`, a Telegram CRITICAL went out, and no triage issue was ever filed for the PR. A triage-born source issue still increments the generation and inherits the root; a marker that is present but not numeric still fails the run.

| The numbers that matter | Value |
| --- | --- |
| Lineage generation for a non-triage source issue | 1 (new root) |
| Lineage generation for a triage source issue at `gen=N` | N + 1 |
| Tests added | 3 (`CheckFailureTriageLineageTests` in `tests/test_check_failure_triage_workflow_security.py`) |

What this means for operators: CI failures on pipeline PRs reach the diagnosis and posting steps again, so the check-failure auto-fix loop works for every PR, not only for fix PRs of earlier triage issues. The posting step still needs the `CHECK_TRIAGE_ISSUES_TOKEN` repository secret (README secrets table); without it the run fails at `gh issue create` after diagnosis.

### For contributors

The new log line is `CHECK_TRIAGE lineage parent_issue=<N> parent_gen=none gen=1 root=<fp> reason=source_issue_not_triage`; the error key `parent_generation_missing_or_malformed` is unchanged and now means a marker was found but is not numeric. The tests drive the script's `collect` stage end to end with a fake `gh` (PR payload, source issue, empty open-triage list) and `CHECK_RUNS_AUTOFIX_ENABLED=false` so no check-run context is collected.
