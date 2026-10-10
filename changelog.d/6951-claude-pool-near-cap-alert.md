<!-- changelog: added -->
- **Hourly Telegram warning when a Claude pool account reaches the usage gate.** `orchestrate_poll.yml` now names every account at or above `gate_utilization` (0.9) instead of staying silent until the whole pool is gated.

The pool step already probed each account's 5-hour and 7-day usage on every poll cycle, but the only operator signal was the per-job engine fallback alert, which fires once every account is gated. The new step `Alert on Claude pool accounts at the usage gate` runs `scripts/claude_pool_health_alert.sh` right after the pool step and sends one Telegram `WARNING` to the `TG_ADMIN_CHAT_ID` chat listing each gated account with both utilizations and the reset time of each window at the gate, each account whose token was rejected, and how many accounts remain usable. Nothing is sent while every account is below the gate. The message goes out at most once an hour: only the poll cycle whose UTC minute is below `CLAUDE_POOL_HEALTH_WINDOW_MINUTES` sends it.

| The numbers that matter | Value |
| --- | --- |
| Usage gate | `gate_utilization` 0.9 in `.github/ai/claude_engine.json` |
| Alert cadence | at most once an hour, from the poll cycle in the first `CLAUDE_POOL_HEALTH_WINDOW_MINUTES` (default 5) minutes |
| Off switch | repo variable `CLAUDE_POOL_HEALTH_ALERT_ENABLED=false` |
| Log line | `CLAUDE_POOL_HEALTH accounts= gated= auth_failed= probe_failed= alert=` |

What this means for operators: a `WARNING` naming `ALPHA: 5h 95% (resets 2026-10-09 15:00 UTC), 7d 10%` arrives while the other accounts still carry the work, so a token can be rotated or usage shifted before the engine falls back to codex.

### For contributors

`.github/actions/claude-pool-token` gained a `probes` output (the probe records, never a token), written even on the `all_gated` path where the pool directory is removed. `scripts/claude_engine.py pool-health` builds the message from those records; `tests/test_claude_pool_health_alert.py` runs the shipped script against a fake `curl` and pins the workflow wiring.
