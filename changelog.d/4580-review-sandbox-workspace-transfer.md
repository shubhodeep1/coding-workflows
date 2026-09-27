<!-- changelog: fixed -->
- **Review editor fixes are committed again.** The review sandbox now copies validated editor edits into the per-PR workspace that the commit step reads, instead of into the runner checkout, where nothing picked them up.

Since the review sandbox landed (#4435), `scripts/review_untrusted_sandbox.sh` snapshotted and wrote back to `GITHUB_WORKSPACE`. The editor step, its `EDITOR_CHANGES_LOST` check, and the commit step all work in `WORKSPACE_PATH`, so every edit the GPT editor made was dropped. Runs then ended `editor_changes_lost` and re-dispatched, or passed as `clean_review_no_commit` with the findings unfixed. PR #4572 failed five review runs in a row this way (issue #4580). `prepare` now validates `WORKSPACE_PATH` as a direct child of `${RUNNER_TEMP}/workspaces`, records it in the sandbox root, and lists files through the checkout's Git database; `run` transfers only into that recorded directory. Without an active workspace the checkout stays the target, as before.

| The numbers that matter | Value |
| --- | --- |
| Consecutive failed review runs on PR #4572 | 5 |

What this means for operators: after this reaches the review support commit (`main` here, the next `@stable` sync for consumers), review rounds push `[ai-autofix]` commits again. A `WORKSPACE_PATH` outside `${RUNNER_TEMP}/workspaces` now fails the `Install project dependencies (best-effort)` step with `Review workspace path rejected`.

### For contributors

`scripts/review_untrusted_workspace.py snapshot` takes an optional fifth argument, the host Git dir; `refresh` and `transfer` keep four. Regression tests: `test_review_sandbox_transfers_into_active_workspace` and its neighbours in `tests/test_model_provider_broker.py`.
