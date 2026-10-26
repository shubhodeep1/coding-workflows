<!-- changelog: added -->
- **Orchestrator integration branches now get their tests checked, and repaired once, after a clean sync of the default branch or a sibling merge.** Until now a clean sync or a sub-issue squash-merge into `orchestrator/project-*` could leave a test failing that the default branch does not have, and nothing repaired it short of a new implement loop, because the conflict resolver only starts on conflict markers.

After a clean `main` sync, a cleared integration conflict, or a sibling squash-merge, the orchestrator poller dispatches the project's final PR review once for each new integration head, with the new `post_sync_test_repair` input. In that run:

- the reviewers and the editor are skipped;
- the pre-push affected-test gate runs on the integration head against its merge-base with the default branch;
- failures the head caused get one repair turn from the conflict resolver's sandbox (`review_conflict_resolve.sh --test-repair`) with the new `prompts/integration-sync-test-repair.txt`;
- the repair is kept only if it passes `check_resolver_diff.sh` and the integration fingerprint check. It is committed as `[ai-merge-resolve] repair post-sync test failures` and pushed.

| The numbers that matter | Value |
| --- | --- |
| Review dispatches per new integration head | at most 1 |
| Repair turns per dispatch | at most 1 |
| New GitHub API reads | 0 |
| Growth of `review_autofix.yml` | 3,036 bytes |

Each dispatch counts toward `INTEGRATION_CONFLICT_LIFETIME_MAX`, so repairs that keep producing new heads stop at the same cap as conflict resolutions.

What this means for operators: nothing to set up. `PREPUSH_TEST_GATE_MODE=off` stops both the dispatch and the repair, and `report` runs the gate without the repair turn. Consumers get the input through their next `ai-review.yml` sync; until then the poller skips them. In this repository the poller dispatches `review_autofix.yml` directly, because `internal-review.yml` cannot forward the input before it is on `main`.
