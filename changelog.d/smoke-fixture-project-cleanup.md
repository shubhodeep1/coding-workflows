<!-- changelog: fixed -->
- **Release-gate smoke projects no longer outlive a gate that times out.** Gates 43445515854, 43590943288 and 43708007145 had left orchestrator projects #9490, #9523 and #9550 open, and the poller kept running waves, CI triage, implement runs and PRs for them.

The cause was a race, not a skipped cleanup. On a long Actions queue, the orchestrate bootstrap the orchestrate-decompose-test job dispatched was still queued when the 50-minute watch ended. "Cleanup orchestrate test issues" then ran with no project to close, and the run created the project hours later. The cleanup now cancels that run first, using a new `dispatched_run_id` output of `scripts/dispatch_and_watch_workflow_run.sh`, and picks up a tracking issue the run already created. As a backstop, the orchestrator poller closes a smoke fixture project once the gate run named in its title or body has completed: its PRs, its child issues, then the tracking issue, with one comment and no deletes.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls per smoke project per poll tick | 1 run read (2 when the title has no gate run id) |
| Cancel wait in the gate cleanup | 120 s, then one force-cancel and 60 s |
| Projects left open by the three gates | #9490, #9523, #9550 (closed by hand on 2026-11-17) |

What this means for operators: a timed-out or killed release gate no longer leaves a project the pipeline keeps working on. Look for `SMOKE_FIXTURE_REAP` in the poller log and `ORCHESTRATE_SMOKE_DISPATCH_CANCEL` in the gate's cleanup step.

### For contributors

The backstop lives in `scripts/orchestrate_poll_smoke_fixture_reap.sh`, which `scripts/orchestrate_poll_process.sh` sources and `.github/workflows/orchestrate_poll.yml` stages. The gate-side helper is `scripts/stable_orchestrate_cancel_dispatched_run.sh`. The existing `run_id` output of the watcher keeps its meaning. A consumer repository used as the gate's `test_repo` is never reaped. Tests: `tests/test_smoke_fixture_project_cleanup.py`.
