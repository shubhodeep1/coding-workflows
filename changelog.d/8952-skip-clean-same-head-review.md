<!-- changelog: changed -->
- **The review gate no longer re-reviews a head that already had a clean review.** A full review whose editor changed nothing now ends its "AI autofix editor summary" comment with a hidden `review-autofix-clean:v1` marker for the head, base and review-support SHA. Later runs on the same head (sweep cron, poller dispatches, manual dispatches) skip at the gate with `skip_reason=clean_same_head` and make no model call.

A Claude pool audit for 2026-11-05 to 2026-11-07 found seven e2e smoke PRs re-reviewed 68-113 times each on unchanged heads, about $3,891 (83.5% of pool spend). Each round ended with `Change status: not-edited`.

| What re-arms a review | |
| --- | --- |
| A new head, a moved base, or a new review-support SHA | the marker no longer matches |
| A newer comment or submitted review from anyone other than the pipeline account or a bot | `reason=newer_human_comment` / `newer_review` |
| A newer pipeline failure, partial-finalize or no-op comment, or a freshness CI-failure marker for the head | `reason=newer_failure_marker` / `freshness_ci_failed` |
| A conflicted or not-yet-computed PR, a `force_rb_judge` dispatch, a retargeted base, a reopened PR | checked before the marker |

The `force-review` label and `[force-review]` title marker still force one full review per head, but no longer re-review a head that already has a clean marker. Any lookup failure runs the review. Set the repository variable `AUTOFIX_SKIP_CLEAN_SAME_HEAD=false` to turn the skip off.

Refs #8952.
