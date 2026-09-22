<!-- changelog: fixed -->
- **A transient GitHub artifact-service error no longer fails the AI Orchestrate Poller.** The `Upload state snapshot artifact` step is now `continue-on-error`, so a blip in GitHub's artifact backend stops turning a healthy poll cycle red.

Operators stop getting ERROR alerts for poll runs that did all their work. On 2026-09-22 around 00:00 UTC, GitHub's artifact backend rejected `FinalizeArtifact` with a non-retryable 403 for about 40 seconds, and the four repos running the poller each happened to reach that step inside the window. The poll cycle, the snapshot build, and the `Publish state snapshot branch` step had all already succeeded in every run, so no orchestration state was lost, but the job still failed and paged. The artifact is the secondary copy of the snapshot; the `state-snapshot` branch is the durable one consumers read, and it stays fatal-by-default.

| The numbers that matter | Value |
| --- | --- |
| Affected runs | 35669827207, 35669899082, 35669889742, 35669957264 |
| Outage window | roughly 40 seconds, 2026-09-22 00:00 UTC |
| Snapshot channels still fatal on failure | 1 (`Publish state snapshot branch`) |
| Spurious Telegram ERROR alerts this prevents | 4 per artifact-service blip |

What this means for operators: an artifact-service blip is now silent in the poller, and a poll failure alert once again means the poll cycle itself failed. The snapshot artifact can be missing from a run without the job going red, so read the `state-snapshot` branch rather than the run artifact when reconstructing a tick.

### For contributors

`if-no-files-found: error` is kept on the step on purpose: a genuinely missing `state.json` is a real defect and still surfaces as a failed-step annotation, though it no longer fails the job. `tests/test_state_snapshot.py` pins both settings, and pins that `Publish state snapshot branch` carries no `continue-on-error`. Consumer repos pick this up on the next `@stable` promotion through the existing `ai-orchestrate-poll.yml` wrapper, whose interface is unchanged.
