<!-- changelog: fixed -->
- **Review/autofix retries a conflict resolution that edits outside its captured conflict set only after restoring the pre-attempt worktree.** An unsafe or unverifiable restore still stops the run without committing.

In the workflow source repository, `scripts/review_conflict_resolve.sh` now checks the entire resolver attempt before accepting its result. An out-of-scope edit discards that attempt, verifies the worktree and merge index match the pre-attempt snapshot, and gives the resolver scope-specific feedback on the next bounded attempt. The existing final `check_resolver_diff.sh` commit gate remains in place. Consumer-repository resolver runs are unchanged because the per-attempt snapshot is source-repository-only.

| The numbers that matter | Value |
| --- | --- |
| Resolver attempts | Up to 3 (`INTEGRATION_SYNC_RESOLVER_MAX_ATTEMPTS`) |
| Snapshot scope | Source-repository resolver attempts only |

What this means for operators: an out-of-scope resolver edit no longer immediately ends an otherwise recoverable source-repository review run. If the snapshot, worktree, or merge index cannot be verified or restored, the run fails closed and leaves the final commit gate intact.
