<!-- agents: section="Workflow architecture" -->
Telegram alert levels (human-only paging): `scripts/tg_helpers.sh` documents the
level convention. `DEBUG` means the pipeline is already handling the event
(dispatch, retry, re-issue, stall-recovery step, auto-answer, success ping).
`WARNING` and above mean a person has to act. The default `ALERT_MSG_LEVEL` is
`WARNING` in `tg_helpers.sh`, in every workflow fallback
(`env.ALERT_MSG_LEVEL || vars.ALERT_MSG_LEVEL || 'WARNING'`) and in
`workflow-templates/ai-review.yml`. A new alert must pick its level by who acts
next. An auto-handled path that can repeat forever needs a once-per-condition
stuck alert, not a page per attempt. The existing once-only helpers in
`scripts/orchestrate_poll_process.sh` are:
- `_standalone_conflict_count_attempt`: one `WARNING` after
  `STANDALONE_CONFLICT_STUCK_ALERT_ATTEMPTS` (default 3) resolution attempts on
  one PR head, counted in an `<!-- ai:standalone-conflict-attempts:v1 -->`
  PR comment read from the sweep's GraphQL batch;
- `_noop_gate_failure_alert`: one `ERROR` per PR head and force-merge gate,
  deduplicated by an `<!-- ai:noop-force-merge-gate-alert:v1 -->` PR comment;
- `_merge_deferral_cap_alert`: one notice when `MAX_MERGE_DEFERRALS` is
  reached;
- `_judge_output_failure_alert`: `CRITICAL` only at `JUDGE_OUTPUT_FAILURE_MAX`.

Final integration merge and project-complete messages stay `CRITICAL` on every
completion path so they remain visible. `tests/test_alert_levels_human_only.py`
pins the default, the helpers, and the ban on invalid level strings
(`WARN`, `INFO`, …).

<!-- agents: section="Stable log prefixes (contractual)" -->
- `STANDALONE_CONFLICT_STUCK` (`scripts/orchestrate_poll_process.sh`: `pr= head= attempts= threshold= dispatch_rc= outcome=counted|alerted|restart|skip|write_failed reason=`; a failed marker write does not page, so the next tick can retry)
- `NOOP_FORCE_MERGE_GATE_ALERT` (`scripts/orchestrate_poll_process.sh`: `pr= head= gate= outcome=alerted|skip reason=`)
- `COMPREHENSIVE_RELEASE_DISPATCH_FAILED` (`scripts/orchestrate_poll_process.sh`: `tracking_issue= alerted=`)
LOG_PREFIX.name=STANDALONE_CONFLICT_STUCK
LOG_PREFIX.name=NOOP_FORCE_MERGE_GATE_ALERT
LOG_PREFIX.name=COMPREHENSIVE_RELEASE_DISPATCH_FAILED
