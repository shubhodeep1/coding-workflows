<!-- changelog: security -->
- **A stalled `claude/*` review is held only after a real re-dispatch, not after a claim.** Before this change, a PR's author could post a `review` claim, dispatch nothing, and make the next fixer hold an unreviewed head instead of retrying.

`.claude/scripts/check_in_status.py --hand-back` still reports a head nobody reviewed as `review-stalled`. It now sets `stall_redispatched`, which tells `/fix-claude-pr` to hold and ask instead of dispatching the review again, only from a workflow run. The run must be a completed, not-cancelled `workflow_dispatch` of `internal-review.yml` from the default branch, titled for this PR, and created after the head arrived. The head's arrival time is the later of its committer date and its earliest check-run start, so a backdated commit cannot pull an older run in. Claims go back to being leases: they keep a second fixer away while one works, and nothing more.

| The numbers that matter | Value |
| --- | --- |
| Security finding | #5376 (`STRIDE: Spoofing`, medium) |
| Extra API calls in coding-workflows | 0 (the stall check reuses the dispatched-run listing) |
| Extra API calls in a consumer repo | 1 listing read (`internal-review.yml` answers 404, then `ai-review.yml`) |
| Consumer run title for dispatched reviews | `AI Review [pr:<n>]` (new `run-name` in `workflow-templates/ai-review.yml`) |

What this means for consumer repos: the next `@stable` sync delivers the new `run-name` in the `ai-review.yml` wrapper together with the updated script. From then on, a review that `/fix-claude-pr` or the sweep re-dispatches is recognised, and a running one counts as active. Other runs keep GitHub's default name.

### For contributors

`_dispatched_review_runs`, `_head_arrival_time` and `_verified_review_redispatch` hold the rule. `_active_run_count` takes an optional `dispatched_listing` dict to share its listing. `_review_stall_verdict` keeps its `ignore_claim_by` parameter, which no longer affects the result.
