<!-- changelog: fixed -->
- **Review autofix edits are no longer lost on the way out of the isolated editor workspace.** The sandbox now snapshots from, and copies results back into, the work tree the review job's git commands read, so editor fixes are committed and pushed again instead of ending in `editor_changes_lost`.

The review job works in a per-run copy of the checkout (`WORKSPACE_PATH`, under `/home/runner/work/_temp/workspaces/`), with `GIT_WORK_TREE` pointing at it and `GIT_DIR` at `${GITHUB_WORKSPACE}/.git`. `scripts/review_untrusted_sandbox.sh` took `GITHUB_WORKSPACE` as its host directory instead, so every edit the editor made in the sandbox was copied into a directory git no longer read. The editor's diff check then saw no change on every attempt, the commit step found a clean tree, and the run failed with `editor_changes_lost` (issue #6055: PRs #6041, #4877, #6126, #6130 and #6133). The sandbox's `prepare` step now takes `WORKSPACE_PATH` as the host, rejects it unless it sits directly under `${RUNNER_TEMP}/workspaces` next to a real `${GITHUB_WORKSPACE}/.git`, and records it for the `run` step, which transfers only into that recorded path. `scripts/review_untrusted_workspace.py snapshot` takes an optional host Git dir and lists the work tree with `--git-dir`/`--work-tree`. The same fix was made twice before (#4478, #4585), but both merged into project branches that never reached `main`.

| The numbers that matter | Value |
| --- | --- |
| Host directory before | `${GITHUB_WORKSPACE}` (the original checkout) |
| Host directory now | `WORKSPACE_PATH` when set (validated under `${RUNNER_TEMP}/workspaces`), else `${GITHUB_WORKSPACE}` as before |
| Failed runs inspected | 36966343894 (PR #6041), 37166027253 (PR #6133) |

What this means for operators: PRs stuck on repeated `editor_changes_lost` failures can be re-reviewed once this reaches `@stable`, and their autofix rounds should push commits again. The changes-lost guard, the transfer safety checks, and the auto-merge gates are unchanged.

### For contributors

The workspace path is chosen once, in `prepare`, and written to `${REVIEW_SANDBOX_ROOT}/workspace`; `run` refuses to start without it, so a model-controlled environment cannot redirect the transfer. The synthetic repository inside the sandbox still runs with the scrubbed `git_env`; only the host `git ls-files` calls in `snapshot` get the explicit Git dir. Regression test: `test_review_isolation_transfers_into_active_work_tree` in `tests/test_review_autofix_review_pipeline_contract.py` drives the real `prepare` and `run` actions with Docker stubbed, checks that a workspace outside `${RUNNER_TEMP}/workspaces` is rejected, and fails on the old code exactly as production did (exit 0, no transfer-failure marker, edit missing from the work tree).
