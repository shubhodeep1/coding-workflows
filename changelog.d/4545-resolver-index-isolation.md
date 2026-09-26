<!-- changelog: fixed -->
- **The source-repo conflict resolver no longer fails closed on its own in-scope resolution.** A model attempt that staged its resolved, conflict-marker-free file before the trusted commit step used to trip the resolver's "merge index changed" scope guard and abort the run, even though the content change never left the conflicted set.

`scripts/review_conflict_resolve.sh`'s `IS_WORKFLOW_SOURCE_REPO` scope check snapshots the real Git index before each model attempt and fails closed if that index changes during the attempt. Four `Internal: Review-Blocked Judge Dispatch` runs (most recently #4516, run 36227396863) hit exactly that path: the model ran `git add` on the file it had just resolved, which changed the real index's bytes even though the file itself stayed in scope, and the run aborted with `Resolver scope check failed closed (ValueError)` instead of committing. The resolver now gives each attempt a disposable copy of the index via `GIT_INDEX_FILE` (`_resolver_prepare_scratch_index`), seeded right after the pre-attempt snapshot and torn down right after the attempt, so a model-issued `git add` / `git commit` lands on the scratch copy instead of the real index. The real-index equality check itself is unchanged, and an edit to a file outside the conflicted set is still rejected. `prompts/conflict-resolver.txt` also now explicitly tells the model not to stage or commit at all.

| The numbers that matter | Value |
| --- | --- |
| Consecutive failed review runs on PR #4516 before this fix | 4 |
| New env var / CLI flag | none — `GIT_INDEX_FILE` is scoped to the model subprocess only |

What this means for operators: a conflict-resolver attempt that edits only in-scope files no longer aborts just because the model happened to stage its own edit; the trusted `stage_resolver_touched_path_or_fail` step still does the actual staging and commit, on the real index, after validation.

### For contributors

`tests/test_review_conflict_resolve_retry_prelude_render.py` adds
`test_scope_state_git_index_isolation_prevents_false_positive_and_still_guards_worktree`,
which reproduces the false positive without isolation, confirms the real
index stays byte-identical with isolation active, and confirms an
out-of-scope worktree edit is still rejected either way. The pre-existing
`test_scope_snapshot_restore_and_index_fail_closed` regression (which
does not use the new isolation helper) is unchanged and still passes.
