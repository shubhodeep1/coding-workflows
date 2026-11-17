<!-- changelog: changed -->
- **Pull requests now run the full CI suite once, when AI review finishes, instead of on every push.** Each push runs only the static checks.

`.github/workflows/ci.yml` no longer runs on `pull_request`. Every PR push runs `CI quick` (`.github/workflows/ci_pr_quick.yml`), which calls `ci.yml`'s `static-checks` job; its checks are named `quick / ...`, so it never reports `lint`. When review finishes with a head, `internal-review.yml` (now passing `final_ci_workflow: ci.yml`) starts one `CI [pr:<n>]` run that tests the PR's merge ref and reports the required `lint` on that head. An `orchestrator/project-*` head is covered by its push run and gets no second run. A failed final run sends the PR back to review once per head. The review sweep starts a final run for any reviewed head that has none.

| The numbers that matter | Value |
| --- | --- |
| Share of CI runner time spent in the test jobs (24 shards plus 7 other jobs) | about 90% |
| PR CI runs per PR, 2026-11-10 to 2026-11-15 | median 3, 90th percentile 9, max 15 |
| Full CI runs per reviewed head after this change | at most 1 |
| Per-push check names | `CI quick / quick / static-checks` and the skipped `quick / ...` jobs |

What this means for operators: a PR's `lint` appears only after review has passed, so merges wait for that one run instead of for every push's run. A PR whose branch was created before `ci.yml` gained its `workflow_dispatch` trigger cannot get its final run (`REVIEW_FINAL_CI ... reason=dispatch_refused`) until the branch is updated from its base. To roll back, revert this change: the `pull_request` trigger comes back and the hooks go inert.
