<!-- changelog: fixed -->
- **Review autofix now sees the failing step of a CI job even when a passing test printed `::error::` lines earlier in the log.** `scripts/collect_pr_check_runs_context.py` ends the `failed[i].log_tail` window at the runner's last `##[error]Process completed with exit code N.` line (an earlier one can come from a tolerated continue-on-error step), and falls back to the first `##[error]` line only when the log has no such line.

On PR #9180 (issue #9179), the `orchestrate-poll (1)` job failed with an `AssertionError` from `test_plain_runner_can_call_every_test`. A passing integration-fingerprint test had printed two `##[error]` lines about `implement.yml` and #1059 hundreds of lines earlier, and the window stopped there. Five review autofix runs reported that the log tail "shows only passing tests", made no edits, and the PR reached the iteration cap. Logs without a runner exit-code line get the same window as before.

What this means for operators: a deterministic test failure in a PR's CI now reaches the editor prompt, so autofix can fix it instead of stalling to `ai:needs-human`.
