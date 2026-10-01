<!-- changelog: security -->
- **A failed review of a Claude PR now blocks the pending-checks auto-merge, even when later runs succeed.** Before, the `claude-pr-catch-all` sweep checked only the latest newer review run, so a later gate-skipped dispatch could hide a failed forced re-review.

The sweep's pending-checks pass (`scripts/claude_fixer_pending_checks.py`) enables auto-merge from a clean review's `ai:claude-fixer-pending-checks` marker once the head's checks finish green. Before this change it looked only at the latest review run of the PR that was newer than the marker's run. A review dispatch that the review gate skips also concludes `success`, so a forced re-review that failed without posting a hand-off was hidden by the next routine dispatch, and the older clean marker authorized the merge (issue #5904). Now every newer review run bound to the PR must have succeeded, on every attempt: a re-run keeps its run's id and the runs listings show only the newest attempt, so the earlier attempts of a re-run review are read too. The same holds for the marker's own review run: a forced re-run of it that failed after the marker was posted, followed by a gate-skipped re-run that succeeds, blocks the merge, while an attempt that failed before the re-run that posted the marker does not. A failure clears only when a newer successful full review posts its own marker, a hand-off, or enables auto-merge itself.

| The numbers that matter | Value |
| --- | --- |
| Newer review runs that must have concluded `success` | all of them, on every attempt (was: the latest one) |
| New GitHub API calls per evaluated PR | 0 in the usual case; 1 per earlier attempt of a re-run newer review, at most 5 per run; 1 per attempt of the marker's run re-run after the marker was posted, at most 5 |
| Sweep result for a PR with a failed newer review | `review_superseded`, logged every hour |

What this means for operators: a PR whose newer review failed is no longer merged from the earlier clean result. Routine sweep dispatches stay skipped on that head, so the PR waits until a push, a base change, or the `force-review` label sends it through a new full review. The sweep's `pending_checks … state=review_superseded` log line names the failed run.
