<!-- changelog: security -->
- **The release gate's E2E smoke job now starts its review runs from the test repo's default branch, never from the smoke PR's branch.** This closes security finding `smoke-review-dispatches-unmerged-workflow` (#5093).

`test-and-mark-stable.yml` dispatched `${REVIEW_WORKFLOW_FILE}` at `ai/issue-<N>` in two places. One is the Phase 3c "Bug B" fallback after the bait commit; the other is the Phase 4b editor retry. So a writer who changed that branch's review wrapper got it run with the test repo's secrets and write permissions. Both now dispatch at the test repo's default branch, which "Validate prerequisites" reads from the `repos/<test repo>` call it already makes. A default-branch run no longer carries the bait commit as its head SHA. The job therefore finds it by the wrapper's run name (`… [pr:<N>]`) and accepts it only when the run's own `codex-agent` log shows that it checked out the bait commit. For the retry, a descendant of the bait also counts. A retry run that fails that check ends Phase 4b with the new status `retry_run_unverified`, which fails the gate like the other retry failures.

| The numbers that matter | Value |
| --- | --- |
| Smoke-job review dispatches that run an unmerged branch's workflow file | 0 (was 2) |
| New GitHub API reads for the default branch | 0 (same `repos/<test repo>` response) |
| Phase 3c registration window for its dispatch run | 90 seconds, fail-soft |
| Extra reads per Phase 4 poll while the registered run is live | 1 run read, plus 1 jobs and 1 log read to verify it once |

What this means for operators: in the Actions list, the smoke job's Bug B and retry review runs now appear on the default branch, named `Internal: AI Review & Autofix [pr:<N>]` here or `AI Review [pr:<N>]` in consumer repos. A consumer wrapper whose dispatch runs are not named for the PR cannot be matched. There, Phase 3c relies on `pull_request: synchronize` alone, and Phase 4b reports `retry_dispatch_failed` until the next workflow sync adds the run name.

### For contributors

The helpers live in `scripts/smoke_review_dispatch.sh`: `smoke_review_pr_named_runs`, `smoke_review_checked_out_sha` (first line-anchored `Captured INITIAL_HEAD_SHA=` line, return codes 0–4), and `smoke_review_sha_descends_from`. `smoke_review_pr_named_runs` keeps a PR-named run only when its `head_branch` is the default branch: a run name alone proves nothing (#5094), so a run on another branch, or with a null or empty `head_branch`, is dropped. Phase 3c outputs `bug_b_run_id`, and Phase 4 reads it as leg (c) through `jq --argjson extra`. `tests/test_smoke_review_dispatch.py` runs the helper against a stub `gh`, and runs the inline Phase 3c, Phase 4, and Phase 4b blocks verbatim. The Phase 6 poller-wrapper dispatch at `ai/issue-<N>` is unchanged.
