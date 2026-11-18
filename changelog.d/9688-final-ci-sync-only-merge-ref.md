<!-- changelog: fixed -->
- **An approved PR into a busy integration branch can run its final CI again.** Final CI no longer stops on a merge ref that is behind its base only because the integration branch synced `main` while the jobs waited for runners.

Final-CI jobs can wait about an hour for a runner. By then an integration branch (`orchestrator/project-*`) has often synced `main` again, so `refs/pull/<n>/merge` is one sync behind its base, and the freshness check in `scripts/ci_final_merge_ref_fresh.sh` stopped every job as `stale` before any test ran. PR #9602 hit this twice on 2026-11-17 and 2026-11-18: 24, then 36 jobs ended with `outcome=stale reason=not_refreshed` while its review gate stayed green. Now, once the refresh attempts have run out, the check accepts the checked-out merge when only integration-sync merges and resolver commits separate it from the tip and none of them changes a file the PR changes. Every other stale ref still fails as before.

| The numbers that matter | Value |
| --- | --- |
| Commits between the tested base and the tip | at most 250 |
| Files changed in that range | fewer than 300 |
| Files in the PR | at most 3000 |
| Extra GitHub reads | up to 3 (the file listing at 100 per page), only on the stale path |
| Switch | `CI_FINAL_MERGE_REF_SYNC_ONLY_ACCEPT_ENABLED` (default `true`) |

What this means for operators: a final CI job that logs `CI_FINAL_MERGE_REF ... outcome=stale_accepted` tested a merge one or more integration syncs behind the tip, and those syncs touch none of the PR's files. A job that still fails `outcome=stale` now says why in `detail=`: `not_ahead`, `truncated`, `foreign_commit`, `file_overlap` or `api_error`. A PR into the default branch never takes the new path.

### For contributors

The first `pulls/<n>` read also returns the default branch and `changed_files`, with no extra call. New helpers: `ci_final_merge_ref_api_get` and `ci_final_merge_ref_sync_only`. A sync merge counts only when its second parent is a default-branch commit. Tests: `tests/test_ci_final_merge_ref_fresh.py`.
