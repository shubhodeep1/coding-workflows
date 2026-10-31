<!-- changelog: fixed -->
- **The review-blocked judge now retries a merge that failed on a GitHub server error.** Before, an HTTP 5xx or GraphQL "Something went wrong" on both `gh pr merge` attempts left an approved PR open with `merge_failed`, and nothing retried it.

On shubhodeep1/tele-funtoken-msg-scoring#5025 the judge chose merge at 2026-10-30 14:23 UTC, both attempts hit GitHub's GraphQL error, and the PR stayed open until it was merged by hand. `scripts/review_rb_judge.sh` now retries those failures on the `merge`, final-round `fix` and `merge_with_followup` paths, still bound to the judged head with `--match-head-commit`. Before each retry it reads the PR once over REST: a merge or auto-merge that landed anyway counts as success, and a closed PR or moved head stops the retry. Draft, permission, conflict and other 4xx errors fail at once, as before. Each retry logs `RB_JUDGE_MERGE_RETRY`.

| The numbers that matter | Value |
| --- | --- |
| Retries | at most 3 |
| Waits | 10s, 20s, 40s |
| Extra API calls | 1 REST read per retry, none when the error is not a server error |

What this means for operators: a "Review-blocked judge approved PR as-is, but the merge did not go through" alert now means GitHub kept failing for about 70 seconds, or the error was not a server error. Check the `RB_JUDGE_MERGE_ERROR` line for the reason.
