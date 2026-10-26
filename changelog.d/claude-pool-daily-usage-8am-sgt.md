<!-- changelog: changed -->
- **The daily Claude pool usage report now goes out at 08:00 Singapore time instead of 12:00.** The default of `CLAUDE_POOL_DAILY_USAGE_HOUR_UTC` moved from `4` to `0`.

`orchestrate_poll.yml` sends the `📊 Claude pool daily usage` Telegram message on the first poll tick at or after `CLAUDE_POOL_DAILY_USAGE_HOUR_UTC`, once per UTC day. With the new default of `0` the slot opens at 00:00 UTC, which is 08:00 SGT, so the report arrives at the start of the Singapore working day. An invalid value now falls back to `0` as well. A repository variable `CLAUDE_POOL_DAILY_USAGE_HOUR_UTC` still overrides the default.

| The numbers that matter | Value |
| --- | --- |
| Old default | `4` (12:00 SGT) |
| New default | `0` (08:00 SGT) |
| Sends per UTC day | 1 |

What this means for operators: expect the report shortly after 08:00 SGT every day. If the repository variable is set, it wins over the new default; delete it or set it to `0` to get the 08:00 slot.
