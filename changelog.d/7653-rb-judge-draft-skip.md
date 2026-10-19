<!-- changelog: fixed -->
- **The review-blocked judge no longer judges or tries to merge an open draft pull request.** It now skips while the PR is a draft and runs again once the PR is marked ready for review.

Dispatched review runs cannot see draft state, because every wrapper sends `pr_is_draft: false` on `workflow_dispatch`. Since #7031 the orchestrator's integration conflict heal dispatches review runs on its draft integration PRs during the security pass. When those runs reached the iteration cap, the judge decided to merge, GitHub refused because the PR was a draft, and a CRITICAL `merge_failed` alert was sent (tele-funtoken-msg-scoring PR #3933, run 39131897079).

`scripts/review_rb_judge.sh` now reads `draft` from the PR it already fetches for its closed-PR check, so there is no extra API call. For an open draft it logs `RB_JUDGE_DRAFT_SKIP pr=<n> outcome=skip reason=draft_pr`, writes `judge_skip_reason=draft_pr` and stops before the linked-issue lookup and the model call. Only a JSON `true` counts as a draft; an unreadable PR read proceeds as before. The "Telegram review-blocked judge decision" step sends nothing for this skip.

Review and conflict resolution on draft PRs are unchanged. Marking the PR ready for review starts a fresh review, which brings the judge back if that review reaches the cap again.
