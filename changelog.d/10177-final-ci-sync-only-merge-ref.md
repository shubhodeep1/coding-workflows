<!-- changelog: fixed -->
- **Final CI on an integration branch no longer fails just because `main` was synced into the base while the run waited for runners.** Refs #9650.

`scripts/ci_final_merge_ref_fresh.sh` failed every final-CI job with `CI_FINAL_MERGE_REF ... outcome=stale` when `refs/pull/<n>/merge` merged into an old base and the job could not build the merge itself (for example `merge_base_too_deep` after many syncs). Integration branches sync `main` more often than final-CI jobs get a runner, so integration-branch PRs (incident: PR #9602) looped until a human stepped in. Before failing, the job now checks whether the base moved only by syncs of the default branch and `[ai-merge-resolve]` commits that touch none of the PR's files. If so it tests the checked-out merge and logs `outcome=stale_accepted reason=sync_only_no_overlap`.

| The numbers that matter | Value |
| --- | --- |
| Extra API reads on the fresh, refreshed and merged-locally paths | 0 |
| Extra API reads on the stale path, per job | at most 32 (two compares, up to 30 PR-files pages); at most 64 requests if every authenticated read is refused and retried without credentials |
| Largest range accepted | 250 commits, under 300 files |
| Largest PR accepted | 3000 changed files |

What this means for operators: grep `CI_FINAL_MERGE_REF` for `outcome=stale_accepted` to see accepted jobs, and the `detail=` field of a stale line for the reason a job was not accepted. Set the repository variable `CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED=false` to roll back.

### For contributors

- A PR based on the default branch is never accepted (`detail=default_base`); a crafted merge with the sync subject is accepted only when its second parent is a default-branch commit.
- Any unreadable response fails closed as `detail=api_error`, which is the old outcome.
