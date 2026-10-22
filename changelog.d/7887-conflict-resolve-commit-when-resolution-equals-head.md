<!-- changelog: fixed -->
- **The merge-conflict resolver now commits the merge when the right resolution is the PR side unchanged.** Before this fix, such PRs stayed conflicted no matter how many times the resolver ran.

`scripts/review_conflict_resolve.sh` skipped the `[ai-merge-resolve]` commit whenever the staged tree matched the PR head. It did this even with a merge in progress, so the base branch was never recorded as the second parent. The resolver reported success, nothing was pushed, and the poller kept dispatching it until it raised the stuck-conflict alert. This happens when every conflict resolves to the PR side, for example on a branch reused after its earlier PR was squash-merged. Now the skip applies only when no merge is in progress. Otherwise the run logs `Merge resolution keeps the PR side unchanged (index matches HEAD); committing the two-parent merge so the base branch is recorded as merged.` and commits as usual.

| The numbers that matter | Value |
| --- | --- |
| Failed resolver attempts on shubhodeep1/tele-funtoken-msg-scoring#4752 before the alert | 3 on head `5056ecf` |
| Stuck-conflict alert threshold (`STANDALONE_CONFLICT_STUCK_ALERT_ATTEMPTS`) | 3 |

What this means for operators: a "still in merge conflict after N automatic resolution attempt(s)" alert caused by this case goes away. No variables or settings change.
