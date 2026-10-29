<!-- changelog: fixed -->
- **Big orchestrator projects no longer fail while posting their first state.** The "Build and post initial state" step in `.github/workflows/orchestrate.yml` now writes the state as an `ORCHESTRATOR_STATE_V2` chain of comments, the format the poller already writes.

The step used to post one `ORCHESTRATOR_STATE_V1` comment and passed its whole body to `jq` as a command-line argument. Linux limits one argument to 128 KiB, so a big project's state failed with `jq: Argument list too long`, `gh api` then sent an empty body, and GitHub answered HTTP 422. A V1 body that size is also over GitHub's 65,536-byte comment limit. Tracking issue #8291 (31 issues in 20 waves, about 200 KB of state) failed this way in run 40789524145, after its Wave 1 issues were created. Wave 1 was not dispatched and the run was reported as failed. The poller later rebuilt the state on its own. The step now packs the state with `scripts/orchestrate_state_v2.py pack`, posts each chunk from a file, and logs `parts=<n> raw_bytes=<n>`. If the packer's file count does not match its declared total, the step fails rather than post an incomplete chain.

| The numbers that matter | Value |
| --- | --- |
| Linux per-argument limit that `jq --arg` hit | 128 KiB |
| GitHub comment body limit | 65,536 bytes |
| State size of tracking issue #8291 | about 200 KB, 31 issues in 20 waves |

What this means for operators: decomposing a large plan no longer leaves a tracking issue without state and a failed orchestrate run. New tracking issues carry only V2 state comments. The poller already writes only V2, and its state reader tries V2 first.

### For contributors

`orchestrate_state_v2.py` is now in the required staging loop of the "Stage workflow support files" step in `orchestrate.yml`. `tests/test_orchestrate_initial_state_v2.py` runs the step's post block against a 200 KB state with a stubbed `gh_retry` and checks that `orchestrate_state_v2.py extract` returns the same state.
