<!-- changelog: added -->
- **Orchestrator integration branches now get their tests checked, and repaired once, after a clean sync of the default branch or a sibling merge.** Until now a clean sync or a sub-issue squash-merge into `orchestrator/project-*` could leave a test failing that the default branch does not have, and nothing repaired it short of a new implement loop, because the conflict resolver only starts on conflict markers.

After a clean `main` sync, a cleared integration conflict, or a sibling squash-merge, the orchestrator poller dispatches the project's final PR review once for each new integration head, with the new `post_sync_test_repair` input. In that run:

- the reviewers and the editor are skipped, and the run neither runs the single-issue security pass or the review-blocked judge, nor enables auto-merge, nor marks linked issues ready to merge, nor labels the PR review-blocked or sends review notifications;
- the pre-push affected-test gate runs on the integration head against its merge-base with the default branch;
- failures the head caused get one repair turn from the conflict resolver's sandbox (`review_conflict_resolve.sh --test-repair`) with the new `prompts/integration-sync-test-repair.txt`;
- the repair is kept only if it passes `check_resolver_diff.sh` and the integration fingerprint check, read from the project's current state. It is committed as `[ai-merge-resolve] repair post-sync test failures`, the gate runs again, and the commit is pushed unless failures the head caused remain.

| The numbers that matter | Value |
| --- | --- |
| Review dispatches per new integration head | at most 1 |
| Repair turns per dispatch | at most 1 |
| New GitHub API reads in the poller | 0 |
| GitHub API reads per repair turn | 3: tracking issue, `/user`, tracking comments (one call per 100 comments) |
| Growth of `review_autofix.yml` | 3,468 bytes |

Repair dispatches are counted in their own state field, `integration_test_repair_dispatches`, and stop at `INTEGRATION_CONFLICT_LIFETIME_MAX`. They do not use up the conflict resolver's budget, so routine syncs cannot leave the next real conflict without a resolver attempt.

What this means for operators: nothing to set up. `PREPUSH_TEST_GATE_MODE=off` stops both the dispatch and the repair, and `report` runs the gate without the repair turn. Consumers get the input through their next `ai-review.yml` sync; until then the poller skips them. In this repository the poller dispatches `review_autofix.yml` directly, because `internal-review.yml` cannot forward the input before it is on `main`.
