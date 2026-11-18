<!-- changelog: fixed -->
- **Final CI no longer fails when GitHub is slow to recompute a PR's merge ref.** The job now builds the merge of the PR head into the base itself, and every job of a run tests the same base.

On 2026-11-18 the final CI runs of PR #9690 (run 44277478295) and PR #9681 (run 44285991274) failed in every job with `CI_FINAL_MERGE_REF ... outcome=stale reason=not_refreshed`, and the PR code was never tested: `refs/pull/<n>/merge` still merged into a `main` that was more than an hour old, after all 6 refresh attempts. `scripts/ci_final_merge_ref_fresh.sh` now merges the dispatched head into the base with `git merge-tree` and checks that commit out (`outcome=merged_locally`). A real conflict still fails the job (`outcome=conflict reason=merge_conflict`), and a merge base too deep to fetch still fails it as stale. The run's base is pinned to the tip of the base branch when the run was created, so jobs that start an hour apart, and re-run attempts, test the same merge even when `main` moves during the run.

| The numbers that matter | Value |
| --- | --- |
| Jobs that failed on a stale merge ref, run 44285991274 | 38 of 38 |
| Fetch window for the local merge (`CI_FINAL_MERGE_REF_MERGE_DEPTH`) | 200 commits (1-1000) |
| Extra API reads per job | 0 when the event payload carries `repository.pushed_at`, else 1 (`GET actions/runs/<id>`) |
| Local merge time on this repository | about 12 s |

What this means for operators: a reviewed PR whose merge ref GitHub has not refreshed gets a real final CI verdict on the first run instead of a red `lint` that sends it back through `final-ci-failed` and the sweep's base sync. Grep `CI_FINAL_MERGE_REF` with `outcome=merged_locally` to see which jobs merged locally; every job of a run logs the same `tree=` value.

### For contributors

- The local merge commit has fixed author, committer, dates and message, so it is the same commit in every job that builds it. Its parents are the pinned base and the head, so `HEAD^1` and `HEAD^2` keep their meaning for the later steps.
- origin does not have that commit. The script appends `CI_FINAL_MERGE_REF_LOCAL_MERGE=<sha>` to `GITHUB_ENV`, and the three "Fetch base commits for the shallow checkout" steps and the agents.md direct-edit check in `ci.yml` then fetch the merge's parents instead of HEAD.
- The pin is the newest first-parent commit of the base whose committer date is not after the event payload's `repository.pushed_at` (or, without it, the run's `created_at`). If the pin cannot be computed, the job falls back to the live tip as before and logs `pin=none pin_reason=<token>`.
- The local merge reads attributes from the empty tree, so a `.gitattributes` merge driver cannot hide a conflict that GitHub's merge would report.
