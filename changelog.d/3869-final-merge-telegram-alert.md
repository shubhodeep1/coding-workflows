<!-- changelog: added -->
- **The orchestrator poller now sends a CRITICAL Telegram alert when a project's final integration PR is squash-merged into the default branch.**

Operators previously had no reliable Telegram signal at the moment a validated project actually landed in `main`: the only send at that point was the DEBUG-level "completed after validation pass" summary in `mark_validation_complete`, which any `ALERT_MSG_LEVEL` above `DEBUG` suppresses. The merge-success arm of `finalize_integration_merge_if_needed` in `scripts/orchestrate_poll_process.sh` now fires `tg_send_msg` at `CRITICAL` level, so the alert is delivered regardless of the configured threshold. The message carries the merged PR's title, the PR url, the tracking-issue url, and a readable gate description (`validation passed`, `operator ai:ready-to-merge`, or `validation disabled`). The title comes from the PR JSON snapshot the tick already fetched, so no additional GitHub API call is issued. The existing DEBUG summary is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Alert level | `CRITICAL` |
| New GitHub API calls per merge | 0 |
| Test pinning the alert | `test_final_merge_success_sends_critical_telegram_alert` |

What this means for operators: every final integration merge (for example a `feat:` squash PR that passed runtime validation) now produces one Telegram message with direct links to the merged PR and its tracking issue, without needing `ALERT_MSG_LEVEL=DEBUG`.

### For contributors

The alert lives between the "Final merge complete" tracking comment and the arm's `return 0`; a source-anchor test in `tests/test_orchestrate_poll_process.py` pins its placement, the gate mapping, both urls, and the `CRITICAL` level.
