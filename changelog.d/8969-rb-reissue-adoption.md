<!-- changelog: fixed -->
- **The orchestrator now tracks a wave issue's review-blocked reissue instead of opening a duplicate.** When the review pipeline's review-blocked judge closed a wave PR and reissued its issue, the poller never mapped the new issue into the project, so the wave judge saw "closed without merge" and opened a second redo of the same work.

On project #8969, wave issue #8970 was reissued as #8994 by `scripts/review_rb_judge.sh`, and eight minutes later the wave judge opened #8995 for the same Phase 1. Both PRs built it; #9003 merged and #9005 was left conflicting, and the project raised the CRITICAL "wave PR(s) closed without merge" alert. The poller (`scripts/orchestrate_poll_process.sh`) now finds such a reissue from the closed issue's timeline and re-points the wave entry at it before wave status or the judge run. The reissue must carry `ai:orchestrator-managed` and the `Tracking issue`, `Replaces` and `review-blocked-reissue` lines. `scripts/review_rb_judge.sh` also stops dropping a wave parent's `Local ID`, `Priority` and `Managed by` lines, which it discarded because wave issues write the integration branch without backticks.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls | 1 REST timeline read per closed-without-merge wave issue per tick, none once adopted |
| New log prefix | `RB_REISSUE_ADOPTION` |
| Incident | project #8969: #8970 → #8994, duplicate #8995 |

What this means for operators: a review-blocked reissue now counts toward the original wave item, so the project stops reporting it as closed without merge and dependent waves unblock when the reissue merges.
