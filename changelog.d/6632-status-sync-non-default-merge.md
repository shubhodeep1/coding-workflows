<!-- changelog: fixed -->
- **A PR merged into a branch other than the default branch no longer marks its linked issue `ai:merged`.** The issue status sync now changes an issue's label, state and AI-memory lineage only when the PR actually completes it.

`AI Issue PR Status Sync` (`.github/workflows/issue_pr_status.yml`) applied `ai:merged` and finalized lineage as `merged` for every linked issue of every merged PR, and only the close itself waited for a merge into `main`. On 2026-09-30, PR #5649 was merged into a `claude/implement-plan-*` project branch. Its body said only `Refs #4867`, but it linked to a comment on #4867, so #4867 was labelled `ai:merged` while still open. The poller's close sweep then sent a Telegram WARNING on every poll until someone removed the label by hand (#5776).

The workflow now changes a linked issue only for a real completion:

- the PR was closed without merging (`ai:closed`, unchanged);
- the PR was merged into the repository's default branch, read from the event payload instead of the literal `main`;
- the issue is an orchestrator-managed child and the PR was merged into its integration branch (the `Integration branch:` its body declares, or an `orchestrator/project-*` branch).

For any other merge, it logs that the issue was left unchanged. The body/title fallback also stops treating `…/issues/N#issuecomment-…` (or any `#fragment`, including one after a `?query`) as a link to issue N. A URL with a query string is no longer counted. Closing keywords and bare issue URLs still count.

| The numbers that matter | Value |
| --- | --- |
| Issues relabelled by a merge into a non-default, non-integration branch | 0 (was: every linked issue) |
| New GitHub API calls | 0 |
| New lineage telemetry reason | `non_completion_merge` |

What this means for operators: in a consumer repo whose default branch is not `main`, a merged PR now closes and labels its linked issues, which it previously skipped. Review/autofix uses the same link helper, so a comment URL in a PR body no longer makes that issue one of the PR's linked issues there either.
