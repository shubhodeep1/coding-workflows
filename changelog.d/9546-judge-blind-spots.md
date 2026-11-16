<!-- changelog: fixed -->
- **The stall and unblock judges no longer treat a finished PR that is waiting for review as a failure.** The stall judge now sees queued and completed review runs and holds escalation while a review waits for a runner. The unblock judge now sees a blocked issue's open `ai/issue-<n>` pull request and must build on it instead of re-implementing the work.

On 2026-11-15 (project shubhodeep1/tele-funtoken-msg-scoring#5241), the stall judge escalated shubhodeep1/tele-funtoken-msg-scoring#5258, shubhodeep1/tele-funtoken-msg-scoring#5259 and shubhodeep1/tele-funtoken-msg-scoring#5260 to `ai:needs-human`. Each had a finished open PR whose review job sat in a long runner queue, but the judge's evidence listed only in-progress runs, so it read "no review ever ran". Later that day the unblock judge ordered a full re-implementation of #5258 because it never saw the open PR.

Now:
- The stall judge context lists the linked open PR's review runs in every status (at most 5, each with `head_sha`) and adds `review_waiting_for_runner`. While a fresh review run on the PR head is queued, waiting or pending, `escalate_human` and `close_and_reissue` are held for that tick (`STALL_SKIP ... reason=review_waiting_for_runner`) and no recovery attempt is counted. Switch: `STALL_JUDGE_LIVE_REVIEW_RUNS_ENABLED` (default `true`).
- For a blocked issue, the unblock judge context gets `linked_open_pr` and the PR's diff. `retry_budget`, `reissue` and `descope` verdicts must name that PR as `#<n>`; otherwise they are refused and asked once more. Switch: `UNBLOCK_JUDGE_LINKED_PR_ENABLED` (default `true`).

| The numbers that matter | Value |
| --- | --- |
| Extra REST calls per stall judge call (open linked PR) | 1 (`gh run list --branch`) |
| Extra REST calls per unblock judge run on an issue | 1, or 3 when an open PR is found (lookup, PR read, diff) |
| Review runs shown to the stall judge | up to 5 (was 3, in-progress only) |

What this means for consumer repos: after the next `@stable` sync, both behaviours are on. Set the repository variable `STALL_JUDGE_LIVE_REVIEW_RUNS_ENABLED=false` or `UNBLOCK_JUDGE_LINKED_PR_ENABLED=false` to turn either off.
