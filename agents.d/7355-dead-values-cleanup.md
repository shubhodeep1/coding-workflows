<!-- agents: section="Stable log prefixes (contractual)" -->
- `BRANCH_REBUILD_THRESHOLD_DECISION` (`scripts/orchestrate_poll_process.sh` `_attempt_branch_rebuild_after_escalation`: `tracking_issue= skip_reason=<reason|none> last_rebuild_at=<ts|none>`; one line per threshold check on an escalated integration-conflict head; `skip_reason=none` means the rebuild proceeds, `last_rebuild_at=none` means no earlier rebuild was recorded)
LOG_PREFIX.name=BRANCH_REBUILD_THRESHOLD_DECISION
