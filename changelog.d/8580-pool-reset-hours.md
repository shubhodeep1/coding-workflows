<!-- changelog: changed -->
- **Claude pool messages now show hours until each window resets, for every account.** The hourly near-cap alert also lists every probed account, not only the gated ones.

The `📊 Claude pool daily usage` report (`claude_engine.py pool-usage`) and the hourly near-cap alert (`scripts/claude_pool_health_alert.sh`, `claude_engine.py pool-health`) now print `resets in Xh` next to each window with a known reset time, for example `TEAM1: 5h 93% (resets in 1.7h), 7d 71% (resets in 5.8h)`. Windows at or above `gate_utilization` keep the UTC reset time as well, for example `(resets in 0.8h, 2026-11-01 01:00 UTC)`. A reset time that has already passed reads `resets now`. The capacity-wait line now reads `earliest reset <UTC time> (in Xh)`.

What this means for operators: one message tells you when each account frees up, with no time-zone math. The hourly alert gets longer by one line per usable account. When it is sent and its level are unchanged.

### For contributors

`pool-health` JSON gains `next_usable_in` (`in 0.8h`, or empty) next to the unchanged `next_usable_at`. `pool_health` and `pool_usage_report` take an optional `now` (epoch seconds) so tests can pin the reference time.
