<!-- changelog: fixed -->
- **The daily promote cycle and the 6-hourly auto-release no longer cancel each other's release gate, and a cancelled gate is retried instead of blocking the next release.**

On the first night both schedules fired at 00:00 UTC, the cycle's `gate_only` smoke run on `main` cancelled the stable release's E2E job through the gate's per-repository `cancel-in-progress` group, so no v1.29.7 was cut, and every later `auto-release-stable.yml` tick skipped with `AUTO_RELEASE_SKIPPED reason=last_gate_failed conclusion=cancelled`. `scripts/promote_main_cycle.sh` now waits for `test-and-mark-stable.yml` to be idle on every branch before dispatching (skipping with `reason=gate_busy` if it never frees within the idle-wait budget), `scripts/auto_release_stable.sh` counts active gate runs on any branch and any active `promote-main-to-stable.yml` run as in flight, and neither script treats a `cancelled` gate as a failed tip any more (`PROMOTE_CYCLE_SKIPPED reason=smoke_gate_cancelled` on the cycle side). `auto-release-stable.yml` runs at 30 past the hour so the two never start together.

| The numbers that matter | Value |
| --- | --- |
| auto-release cron | `30 */6 * * *` (was `0 */6 * * *`) |
| cycle wait for an idle gate | up to `PROMOTE_CYCLE_GATE_IDLE_WAIT_SECS` (default 1800s), polling every `PROMOTE_CYCLE_GATE_POLL_SECS` |
| cycle wait for its dispatched gate | `PROMOTE_CYCLE_GATE_WAIT_SECS` (default 18000s), starting after dispatch |
| E2E smoke job cap asserted by `tests/test_ci_poll_test_sharding.py` | 300 minutes (was 180, stale since #4168) |

What this means for operators: a stable patch and the daily cycle can coexist; the release goes out on the next 6-hour tick after the gate is free, and a gate cancelled by concurrency or a runner loss is simply retried rather than waiting for a human.
