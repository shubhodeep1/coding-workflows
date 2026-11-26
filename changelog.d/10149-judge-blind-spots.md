<!-- changelog: fixed -->
- **The stall judge and the unblock judge now see a PR's queued review runs and an issue's open PR before they escalate.**

The stall judge only saw review runs that had already started. When a pull request's review was still waiting for a runner, the judge escalated the issue to a person, or closed and re-issued it, even though the review simply had not started yet. The unblock judge, judging a blocked issue, did not know that the issue's work already sat on an open `ai/issue-<n>` pull request, so `retry_budget` and `reissue` implemented the same work again from the default branch.

The stall judge now lists the linked pull request's branch runs once. It adds the review runs, queued and completed, with their head SHA, to its diagnostics (at most 5) and sets `review_waiting_for_runner` when a review of the current head is waiting for a runner. While that holds, `escalate_human` and `close_and_reissue` are held: the judge comment still posts, nothing runs and no attempt is counted, and the next tick checks again. A queued run older than `ZOMBIE_RUN_MAX_QUEUED_HOURS` does not hold anything.

For an issue, the unblock judge now looks up its open pull request on this repository's own `ai/issue-<n>` branch, shows the model that pull request's diff and number, and adds the number to the block's fingerprint. `retry_budget` and `reissue` are refused while that pull request is open; the model is asked once more and steered to `wait` on that pull request or a fix-up verdict.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls per stall-judge run | at most 1 (`gh run list --branch`, only with an open linked PR) |
| Extra API calls per unblock-judge issue item | 1 open-PR lookup, plus 1 diff read when a PR is open |
| New repo variables | `STALL_JUDGE_LIVE_REVIEW_RUNS_ENABLED`, `UNBLOCK_JUDGE_LINKED_PR_ENABLED` (both default `true`) |
| New jobs or workflows | none |

What this means for operators: issues whose review is only queued no longer reach `ai:needs-human`, and blocked issues with an open pull request are no longer implemented twice. Either lookup failing falls back to the previous behaviour. Set either variable to `false` to restore the previous judging exactly.
