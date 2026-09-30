<!-- changelog: security -->
- **The release gate's E2E smoke job now starts its review runs from the default branch, never from the smoke PR's branch.** This closes security finding `e2e-dispatch-executes-unreviewed-workflow` (#5520).

`test-and-mark-stable.yml` dispatches the review wrapper (`internal-review.yml` by default) in two places: the Phase 3c "Bug B" fallback right after the bait commit, and the Phase 4b retry. Both passed `--ref "${BRANCH}"`, so the run executed the smoke branch's own copy of the wrapper with the test repo's secrets, and any writer could change that copy before the dispatch. Both now dispatch without `--ref`. Such a run shows the default branch as its head. The gate therefore finds it by a workflow-scoped listing of `workflow_dispatch` runs on the default branch with the wrapper's exact PR run name (`Internal: AI Review & Autofix [pr:<N>]` or `AI Review [pr:<N>]`), and a name alone never counts. It accepts the run only when the PR head the run reviewed matches: that head is the `Captured INITIAL_HEAD_SHA=<sha>` line in its `codex-agent` job log, written by the default branch's workflow code.

| The numbers that matter | Value |
| --- | --- |
| E2E review dispatches that run the smoke branch's workflow file | 0 (was 2) |
| Extra GitHub API calls in Phase 4 | 1 listing per poll while a bait is pinned, 1 default-branch read per step, 2 cached reads per completed candidate run |
| New log line | `E2E_REVIEW_DISPATCH_CORRELATION pr=<N> run=<id> reviewed_head=<sha> … result=<…>` |

What this means for operators: in a release gate run, the Bug B and retry review runs appear in the Actions list under the default branch with the PR in their run name. A retry run that never checked out the PR now fails Phase 4b as `retry_workflow_failed` instead of blaming the editor with `spec_mismatch`. A `review_workflow_file` override other than `internal-review.yml` or `ai-review.yml` has no PR run name, so with it the Phase 4b retry refuses to dispatch (`retry_dispatch_failed`).

### For contributors

The helpers (`e2e_review_dispatch_title`, `e2e_review_dispatch_resolve_default_branch`, `e2e_review_dispatch_runs`, `e2e_review_dispatch_resolve_head`) are inline in both the `wait-review` and `verify-bait-removed` steps, because the job sources `scripts/` from a `ref: main` checkout. `tests/test_test_and_mark_stable_e2e_dispatch_default_branch.py` runs them in bash against a stubbed API, keeps the two copies identical, and pins the `review_autofix.yml` log line they read.
