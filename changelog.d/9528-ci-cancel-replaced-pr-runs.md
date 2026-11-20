<!-- changelog: changed -->
- **A new push to a pull request now cancels that PR's older `CI` run.** Push CI on `main`, `stable` and `orchestrator/project-*` is never cancelled.

`.github/workflows/ci.yml` has a workflow-level `concurrency` block. A `pull_request` run joins the group `ci-pr-<PR number>` with `cancel-in-progress`, so when a PR gets a new head, its queued or running CI for the old head stops and only the newest head is tested. Every other run gets a group of its own (`ci-<run id>`) and is never cancelled, because check-failure triage compares a failing PR against the base branch's newest push run. The cancelled-CI rerun sweep already skips a cancelled run whose head is no longer the PR's head, so it does not bring the cancelled run back.

| The numbers that matter | Value |
| --- | --- |
| CI runs replaced by a newer push before their first job started (2026-11-15, 12 h window) | 16 runs, 424 runner-minutes |
| Group for PR runs | `ci-pr-<PR number>`, cancel in progress |
| Group for push runs | `ci-<run id>`, never cancelled |

What this means for operators: a PR that is pushed several times in a row ties up runners for one CI run, not one per push. The required `lint` check is reported by the run on the PR's newest head, as before.

### For contributors

`tests/test_ci_concurrency.py` pins both expressions, evaluates them for `pull_request`, `push`, `workflow_dispatch`, `workflow_call` and `schedule` events, checks that no job overrides the block, and runs in `static-checks`.
