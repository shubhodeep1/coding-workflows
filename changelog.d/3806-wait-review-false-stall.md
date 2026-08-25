<!-- changelog: fixed -->
- **Release smoke tests no longer fail on healthy long reviewer steps.** The v1.27.0 release run failed even though its review pipeline was working correctly; the wait-review gate now waits long enough and watches the right job.

On the v1.27.0 release (run 32824674139), `e2e-smoke-test` declared "Review phase stalled — no activity for 40 minutes" 58 seconds before the review run's `Run reviewer models` step completed successfully. Three defects lined up: `promote-main-to-stable.yml` still forwarded `review_timeout=40` after the downstream default had been raised to 60, the per-step stall cap of 50 minutes sat below the 51-minute healthy reviewer-step duration actually observed, and the wait loop's live-log probes read the wrapper run's 4-second `review / gate` job instead of `review / codex-agent`, so its log-based activity signals and early-exit shortcuts never worked. The promote dispatcher default now tracks the downstream 60, the per-step cap default is 75 minutes, and the log probes select the codex-agent job by name with an in-progress-job and `jobs[0]` fallback.

| The numbers that matter | Value |
| --- | --- |
| `promote-main-to-stable.yml` `review_timeout` default | 40 → 60 minutes |
| `test-and-mark-stable.yml` `review_step_timeout` default | 50 → 75 minutes |
| Observed healthy `Run reviewer models` durations | 41m (v1.20.1), 51m (v1.27.0) |
| Margin by which the v1.27.0 gate gave up too early | 58 seconds |

What this means for operators: a slow-but-healthy review run no longer kills a release; re-dispatching `promote-main-to-stable.yml` without overriding `review_timeout` now uses the intended 60-minute inactivity window. When `test-and-mark-stable.yml`'s `review_timeout` default changes again, `promote-main-to-stable.yml`'s default must change with it — its description now says so explicitly.

### For contributors

The wait loop's editor-noop and reviewer-majority shortcuts were inert in every wrapper (`internal-review.yml`) run because `.jobs[0]` is the gate job there; with the codex-agent job selected they can fire again, letting Phase 4 exit success shortly after the reviewer step ends instead of waiting out the editor.
