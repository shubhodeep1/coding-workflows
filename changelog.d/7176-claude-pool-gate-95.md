<!-- changelog: changed -->
- **The Claude account pool now keeps an account until it reaches 95% usage, up from 90%.** `gate_utilization` in `.github/ai/claude_engine.json` is now `0.95`.

The pool step probes each account's 5-hour and 7-day utilization at the start of every Claude job and leaves out any account whose higher window is at or above `gate_utilization`. Raising the gate gives every account five more points of usable capacity before it is skipped, so jobs fall back to codex less often. The hourly near-cap alert from `orchestrate_poll.yml` (`scripts/claude_pool_health_alert.sh`) reads the same key, so it now names accounts at 95% or above instead of 90%.

| The numbers that matter | Value |
| --- | --- |
| Usage gate | `gate_utilization` 0.9 → 0.95 |
| Windows checked | 5-hour and 7-day, the higher of the two |
| Near-cap alert threshold | follows the gate (now 95%) |

What this means for operators: the pool drains closer to each account's real limit before Claude roles fall back to codex, and the near-cap Telegram warning arrives later. To keep the old threshold in a fork, set `gate_utilization` back to `0.9` in `.github/ai/claude_engine.json`.
