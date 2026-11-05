<!-- changelog: changed -->
- **The Claude account pool now uses the account whose spare weekly quota expires soonest first.** Accounts that just started a new week are saved for later, so the pool stops draining fresh accounts to the gate days before they reset.

Before each job, `.github/actions/claude-pool-token` probes every account and `choose_account` in `scripts/claude_engine.py` orders them. It used to pick the lowest weekly %, which ignores that every account's 7-day window resets at a different time. An account that had just reset took nearly every job: on 2026-11-05 PERSONAL1 had used 58% of its week in 17 hours, while TEAM3 sat at 60% with 35% of its quota due to be lost at its reset 19.8 hours later. Accounts are now ranked by their 7-day headroom under `gate_utilization` divided by the hours until their 7-day reset, highest first. The 5-hour window still only gates an account, and the 95% gate is unchanged. Accounts whose probe has no 7-day reset time follow, lowest `max(5h, 7d)` first, and accounts with no 7-day reading still go last.

| The numbers that matter | Value |
| --- | --- |
| Gate (`gate_utilization` in `.github/ai/claude_engine.json`) | `0.95`, unchanged |
| Rank key | `(0.95 - 7d use) / hours until the 7-day reset`, floored at 0.1 hours |
| First pick in the 2026-11-05 report | TEAM3 (1.77% spare per hour); the lowest-% rule picked PERSONAL1 |
| Unserved work in a 6-week, 10-account simulation above pool capacity | 609 → 159 (% of one account's weekly quota) |

What this means for operators: weekly use should track each account's reset schedule, so less quota is thrown away at resets and the pool is fully gated less often. The daily usage report and the hourly near-cap alert are unchanged; only the order jobs try accounts in changes.

### For contributors

`choose_account` takes an optional `now` (epoch seconds, default the current time) so tests can pin the reference time. The verdict's `utilization` is now `max(5h, 7d)` of the first account in `order`, which is no longer always the least used one.
