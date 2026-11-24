<!-- agents: section="Workflow architecture" -->
### Release-gate runner wait (issue #10037)

- `release_gate_oldest_queued_wait <repo> <limit_minutes> <zq_cutoff>` in `scripts/gh_helpers.sh` prints one line, `outcome=<long|short|read_failed> oldest_queued_min=<n|-> queued_in_window=<n|-> calls=<0|1|2>`, from one `actions/runs?status=queued&created=<cutoff+1s>..<now-limit>` read (plus the last page, capped at page 10, when more than 100 match). Callers declare a fail-open fallback with `declare -F`.
- `scripts/auto_release_stable.sh` (after the `runner_queue_deep` back-off) and `scripts/promote_main_cycle.sh` (step 7, after the gate idle wait and before `pin_freeze`) skip with reason `runner_wait_long` while the wait is `RELEASE_GATE_MAX_RUNNER_WAIT_MINUTES` or longer. The bound reuses `AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS`: the newest stable gate run (auto-release, any status) or the newest `Test & Mark Stable Release [cycle:` run (promote), read from listings the tick already holds; none listed counts as reached.
- Poller retry: the `cycle` job's step "Record runner-wait skip for the poller retry" runs on `outcome=skipped:runner_wait_long`. The poll job's step "Retry a promote cycle skipped for a long runner wait" (source repo only) runs `promote_main_cycle.sh` with `PROMOTE_CYCLE_RETRY_CHECK_ONLY=true`, which dispatches `-f cycle_retry=true` when the newest cycle run (schedule, or `workflow_dispatch` titled `Promote main to stable [cycle-retry]`) succeeded with that step 60 minutes to 24 hours ago. `cycle_retry` routes to the `cycle` job only; guard 5 counts failed retry runs as attempts.
- Gate stop: on a completion timeout `dispatch_and_watch_workflow_run.sh` reads the watched run's jobs once and classifies `runner_wait` (every job queued without a runner or first step started in the last third of the budget; empty jobs only when queued the whole time) or `timeout` (fail closed). With `DISPATCH_WATCH_STOP_RELEASE_GATE=true` (workflow-level env in `test-and-mark-stable.yml`) it reads the gate's jobs and, when none already failed, cancels its own gate run so it ends `cancelled` (no attempt in either release script). Phase 4 (`scripts/stable_e2e_step_wait_review.sh`) uses `--stop-release-gate` for its watched review run and writes `runner_wait=true` next to `status=timeout`.
- Heal intake: `skip reason=release_gate_runner_wait run=<id> jobs=<n>` when every failed job of a `Test & Mark Stable Release` run is a Phase 4 or `Dispatch & watch — ` step whose log has a line-start `RELEASE_GATE_RUNNER_WAIT outcome=cancel_requested|stop_failed` marker.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `AUTO_RELEASE_RUNNER_WAIT_CHECK` — `outcome=disabled|bound_reached|clear|read_failed ...`, the auto-release runner-wait check when it does not skip (issue #10037); the skip itself is `AUTO_RELEASE_SKIPPED reason=runner_wait_long oldest_queued_min=<n> max=<m> sha=<tip> last_run=<id> last_age_hours=<n> window_hours=<n>`.
LOG_PREFIX.name=AUTO_RELEASE_RUNNER_WAIT_CHECK
- `PROMOTE_CYCLE_RUNNER_WAIT_CHECK` — `outcome=disabled|bound_reached|clear|read_failed ...`, the promote cycle's runner-wait check when it does not skip (issue #10037); the skip is `PROMOTE_CYCLE_SKIPPED reason=runner_wait_long oldest_queued_min=<n> max=<m> ...`.
LOG_PREFIX.name=PROMOTE_CYCLE_RUNNER_WAIT_CHECK
- `PROMOTE_CYCLE_RETRY` — `outcome=disabled|read_failed|cycle_active|no_cycle_run|not_runner_wait|not_due|stale|dispatched|dispatch_failed ...`, the poller's check-only promote retry (issue #10037).
LOG_PREFIX.name=PROMOTE_CYCLE_RETRY
- `PROMOTE_CYCLE_RETRY_MARKER` — `run=<id>`, printed by the cycle job's marker step after a `runner_wait_long` skip (issue #10037).
LOG_PREFIX.name=PROMOTE_CYCLE_RETRY_MARKER
- `DISPATCH_WATCH_RESULT` — `outcome=runner_wait|timeout run=<id> reason=never_assigned|late_assigned|ran_early|no_jobs|jobs_unreadable jobs=<n|-> queued_without_runner=<n|->`, the watch helper's completion-timeout classification (issue #10037).
LOG_PREFIX.name=DISPATCH_WATCH_RESULT
- `RELEASE_GATE_RUNNER_WAIT` — `outcome=cancel_requested|real_failure_present|stop_failed ...`, the release gate stopping itself after a child runner wait (issue #10037); the heal intake matches `cancel_requested` and `stop_failed` at line start.
LOG_PREFIX.name=RELEASE_GATE_RUNNER_WAIT
