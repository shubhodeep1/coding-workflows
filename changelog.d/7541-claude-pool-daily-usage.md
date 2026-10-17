<!-- changelog: added -->
- **A daily Telegram message now reports the usage of every Claude pool account.** One `📊` message a day lists each account's 5-hour and 7-day utilization, so pool headroom is visible before an account reaches the gate.

The orchestrator poller (`orchestrate_poll.yml`) in `shubhodeep1/coding-workflows` sends it on the first poll tick at or after 04:00 UTC (12:00 Singapore time). A new `claude_pool_daily_usage` job probes the pool through `.github/actions/claude-pool-token`, and `scripts/claude_pool_daily_usage.sh` sends one message with a line per account: both utilizations, the reset time of any window at or above `gate_utilization`, rejected tokens and failed probes. An `actions/cache` marker per UTC day stops a second send, and a failed send is retried on the next tick. The message is delivered at the default `ALERT_MSG_LEVEL=WARNING` threshold; only `SILENT` suppresses it. The hourly near-cap alert is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Send time | first poll tick at or after 04:00 UTC (12:00 SGT); ticks arrive 25-60 minutes apart |
| Messages per day | 1 |
| Repo variables | `CLAUDE_POOL_DAILY_USAGE_ENABLED` (default `true`), `CLAUDE_POOL_DAILY_USAGE_HOUR_UTC` (default `4`) |
| Repos that send it | `shubhodeep1/coding-workflows` only |

What this means for operators: the report arrives in the `TG_ADMIN_CHAT_ID` chat with no setup. Set `CLAUDE_POOL_DAILY_USAGE_HOUR_UTC` to move it or `CLAUDE_POOL_DAILY_USAGE_ENABLED=false` to stop it. Consumer repos are not affected.

### For contributors

`claude_engine.py pool-usage` builds the text. `tg_send_report_msg` in `scripts/tg_helpers.sh` sends without a level prefix or threshold, honouring `SILENT` only. The report runs in its own job so probing the pool never changes the poll job's Claude credential or capacity-wait state. The broker already allows `orchestrate_poll.yml`, so no Worker redeploy is needed. Tests: `tests/test_claude_pool_daily_usage.py`.
