<!-- changelog: fixed -->
- **The standalone stall-recovery close comment and CRITICAL alert now report the real number of recovery attempts.** They used to say "0 attempts" when an issue was closed after five.

The standalone loop in `scripts/orchestrate_poll_process.sh` caps recovery with two counters. `stall_recovery_count` resets every time the issue's phase label changes. `phase_attempts` keeps a running total for each phase. An issue that goes round in a loop (`/approved`, then the implement run fails and puts it back in `ai:awaiting-approval`) hits the `phase_attempts` cap while `stall_recovery_count` still reads 0. The close comment, the linked-PR close note and the Telegram alert all printed `stall_recovery_count`, so tele-funtoken-msg-scoring#4786 was closed with "(0 attempts)" after five auto-approvals. They now print whichever counter hit the cap, plus the limit and the phase. The comment also stops saying "over N minutes": that number is how long the issue was stuck this time, not how long all the attempts took.

| The numbers that matter | Value |
| --- | --- |
| Default cap (`MAX_STALL_RECOVERIES_PER_ISSUE`) | 5 |
| New close comment | `Closing: standalone stall recovery exhausted (5 attempts in 'ai:awaiting-approval' phase, limit 5; stuck 65 minutes this time).` |

What this means for operators: when the alert says an issue's recovery is exhausted, the attempt count now matches the `/approved` or `/answer` comments on the issue. When to close an issue is unchanged.
