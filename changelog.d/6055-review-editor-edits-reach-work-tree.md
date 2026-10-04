<!-- changelog: fixed -->
- **Review autofix edits are no longer lost on the way out of the isolated editor workspace.** The sandbox now snapshots from, and copies results back into, the work tree the review job's git commands read, so editor fixes are committed and pushed again instead of ending in `editor_changes_lost`.

The review job works in a per-run copy of the checkout (`WORKSPACE_PATH`, under `/home/runner/work/_temp/workspaces/`), with `GIT_WORK_TREE` pointing at it and `GIT_DIR` at `${GITHUB_WORKSPACE}/.git`. `scripts/review_untrusted_sandbox.sh` took `GITHUB_WORKSPACE` as its host directory instead, so every edit the editor made in the sandbox was copied into a directory git no longer read. The editor's diff check then saw no change on every attempt, the commit step found a clean tree, and the run failed with `editor_changes_lost` (issue #6055: PRs #6041, #4877, #6126, #6130 and #6133). The sandbox now uses `git rev-parse --show-toplevel`, which follows `GIT_WORK_TREE`, and `scripts/review_untrusted_workspace.py` lists a work tree that has no `.git` of its own through `GIT_DIR`.

| The numbers that matter | Value |
| --- | --- |
| Host directory before | `${GITHUB_WORKSPACE}` (the original checkout) |
| Host directory now | the active git work tree (`WORKSPACE_PATH`), falling back to `${GITHUB_WORKSPACE}` when git cannot resolve one |
| Failed runs inspected | 36966343894 (PR #6041), 37166027253 (PR #6133) |

What this means for operators: PRs stuck on repeated `editor_changes_lost` failures can be re-reviewed once this reaches `@stable`, and their autofix rounds should push commits again. The changes-lost guard, the transfer safety checks, and the auto-merge gates are unchanged.

### For contributors

Steps run after "Activate workspace shell context" in `.github/workflows/review_autofix.yml` start in `WORKSPACE_PATH` (through `BASH_ENV`), so the git work tree and the current directory agree. The synthetic repository inside the sandbox still never sees `GIT_DIR`; only the host `git ls-files` calls in `snapshot` use it, and only when the host directory has no `.git`. Regression test: `test_review_isolation_transfers_into_active_work_tree` in `tests/test_review_autofix_review_pipeline_contract.py` drives the real `prepare` and `run` actions with Docker stubbed, and fails on the old code exactly as production did (exit 0, no transfer-failure marker, edit missing from the work tree).
