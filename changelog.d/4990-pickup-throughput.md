<!-- changelog: changed -->
- **The Claude issue pickup drains its queue faster: 20 items per wake, one catch-up wake, and resumes first.** A backlog no longer waits hours, and a `/reclarify` resume no longer waits behind newly filed issues.

On 2026-09-29 the `ai:claude-issue-queue` held 24 items while the pickup started at most 10 per hourly wake, so the last item could wait about 3 hours. `scripts/claude_issue_route.py queue-pending` now starts up to 20 targets per wake (`QUEUE_PICKUP_LIMIT`), and the pickup session's `CLAUDE_ISSUE_PICKUP_LIMIT` overrides that, clamped to 1..30. A bad value falls back to 20. Issue entries whose trigger is `reclarify` are listed before every other entry, and each group keeps queue order. A start or hourly wake that leaves items queued schedules one `send_later` into the pickup session itself, 30 minutes out, named `Claude issue pickup: catch-up`. A catch-up wake never schedules another (`catch_up_due`, `--wake hourly | catch-up`), and the pickup still never creates a session for its own wake. The one-line pickup report adds `oldest_waiting=<minutes>` and `catch_up=<scheduled | pending | none | failed>`.

| The numbers that matter | Value |
| --- | --- |
| Targets started per wake | 10 → 20 (`CLAUDE_ISSUE_PICKUP_LIMIT`, 1..30) |
| Wakes per hour with a backlog | 1 → 2 (hourly plus one catch-up, 30 minutes later) |
| 24 queued items fully started | about 3 hours → about 30 minutes |
| Binding-read window | first 30 → first 60 targets (3 × limit) |
| Watchdog threshold `CLAUDE_ISSUE_QUEUE_STALE_HOURS` | unchanged (3) |

What this means for operators: queued issues and resumes start within about half an hour even during a burst, and the pickup's report shows how long the oldest item has waited. Nothing needs restarting, because the pickup reads the command file and script fresh on every wake.

### For contributors

`queue-pending` output gains `limit`, `oldest_waiting_minutes` (bound or deferred items only; ignored items stay the watchdog's), and `catch_up_due`, and it takes `--wake` and `--now`. Existing fields and an explicit `--limit` are unchanged. The catch-up costs one `send_later`. More sessions per wake add only the per-item `create_session`, `create_trigger` and `issue_write` calls. The binding reads grow only when more than 30 bound targets are queued.
