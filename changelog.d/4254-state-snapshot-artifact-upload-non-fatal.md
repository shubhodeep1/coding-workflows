<!-- changelog: fixed -->
- **A transient GitHub artifact-service error no longer fails the AI Orchestrate Poller.** The `Upload state snapshot artifact` step is now `continue-on-error`, so a blip in GitHub's artifact backend stops turning a healthy poll cycle red.

Operators stop getting ERROR alerts for poll runs that did all their work. On 2026-09-22 around 00:00 UTC, GitHub's artifact backend rejected `FinalizeArtifact` with a non-retryable 403 for about 40 seconds, and the four repos running the poller each happened to reach that step inside the window. The poll cycle, the snapshot build, and the `Publish state snapshot branch` step had all already succeeded in every run, so no orchestration state was lost, but the job still failed and paged. The artifact is a secondary, best-effort copy of the snapshot; consumers read the durable `state-snapshot` branch when its independently retried, best-effort publication succeeds.

| The numbers that matter | Value |
| --- | --- |
| Affected runs | 35669827207, 35669899082, 35669889742, 35669957264 |
| Outage window | roughly 40 seconds, 2026-09-22 00:00 UTC |
| Snapshot persistence channels that are best-effort | 2 (run artifact and `state-snapshot` branch push) |
| Spurious Telegram ERROR alerts this prevents | 4 per artifact-service blip |

What this means for operators: an artifact-service blip no longer fails the poller, and a poll failure alert once again points to the poll cycle, snapshot build, or another fatal workflow step. The snapshot artifact can be missing from a run without the job going red, so prefer the `state-snapshot` branch when reconstructing a tick and check workflow warnings if that tick is absent.

### For contributors

`if-no-files-found: error` is kept on the step on purpose: a genuinely missing `state.json` is a real defect and still surfaces as a failed-step annotation, though it no longer fails the job. `tests/test_state_snapshot.py` pins both settings, and pins that `Publish state snapshot branch` carries no blanket `continue-on-error`; expected branch-push failures remain handled by its existing retry-and-warning path. Consumer repos pick this up on the next `@stable` promotion through the existing `ai-orchestrate-poll.yml` wrapper, whose interface is unchanged.
