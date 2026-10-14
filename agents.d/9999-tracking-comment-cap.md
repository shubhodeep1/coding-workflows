<!-- agents: section="Stable log prefixes (contractual)" -->
- `STATE_PRUNE` (`scripts/orchestrate_poll_process.sh` `prune_state_comment_chains`: `tracking_issue= deleted= failed= remaining= outcome=done|budget_exhausted|skip [reason=comments_unavailable|login_unavailable|plan_failed]`; `trigger=comment_cap` when a state write met GitHub's 2500-comment cap)
- `TRACKING_COMMENT_REPEAT` (`post_tracking_comment`: `tracking_issue= comment= count= outcome=updated|update_failed` when a repeat of a recent pipeline comment updates its count instead of posting)
LOG_PREFIX.name=STATE_PRUNE
LOG_PREFIX.name=TRACKING_COMMENT_REPEAT
