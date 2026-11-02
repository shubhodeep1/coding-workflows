<!-- changelog: fixed -->
- **The Claude account pool now ranks an account with no 5-hour reading by its 7-day use.** Before, such an account was tried after every other usable account, so the pool used up some accounts and left others idle.

Before each job, `.github/actions/claude-pool-token` probes every account, and `choose_account` in `scripts/claude_engine.py` orders them, lowest `max(5h, 7d)` use first. Many accounts no longer report a 5-hour window, and a probe without one (`5h ?` in the `📊 Claude pool daily usage` report) used to put the account in the "unknown" group at the end of the order. On 2026-11-02 that left PERSONAL1 and PERSONAL2 at 74% and 76% weekly use while the other six accounts were at or above the 95% gate. A missing 5-hour reading now means the account has no 5-hour limit, so it ranks by its 7-day use. It is gated only when that 7-day reading reaches `gate_utilization` or the probe was `rejected`. An account with no 7-day reading still goes last.

| The numbers that matter | Value |
| --- | --- |
| Gate (`gate_utilization` in `.github/ai/claude_engine.json`) | `0.95`, unchanged |
| Accounts in the pool on 2026-11-02 | 8 |
| Accounts reporting `5h ?` that day | 3 (FUNTOKEN1, PERSONAL1, PERSONAL2) |

What this means for operators: weekly use should even out across the pool, so fewer accounts sit at the gate while others still have room. The daily usage report and the hourly near-cap alert look the same; only the order jobs try accounts in changes.
