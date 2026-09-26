<!-- changelog: fixed -->
- **The pre-review conflict resolver no longer fails closed when the model stages an allowed conflicted path itself.** `_resolver_scope_state` in `scripts/review_conflict_resolve.sh` previously refused to retry or commit any attempt where the model ran `git add` on one of its own allowed conflicted paths before the script's own validation/staging step.

The scope guard used to hash the raw `.git/index` file to detect tampering during a resolver attempt, and that file's bytes change on any `git add`, including a legitimate stage of a path the resolver was explicitly allowed to touch. `merge_state()` now compares a structural fingerprint of the index (mode, blob id, and stage per path) instead, and a new `_assert_index_transition_allowed` check accepts an index change only when it is confined to allowed conflicted paths and the staged blob matches the resolved worktree content. `MERGE_HEAD`, any index change to a path outside the allowed set, and an allowed path whose staged content does not match its worktree content still fail closed exactly as before.

| The numbers that matter | Value |
| --- | --- |
| Source incident | PR shubhodeep1/coding-workflows#4531, run 36224679302 |
| Affected file | `scripts/review_conflict_resolve.sh` (`_resolver_scope_state`) |
| New regression tests | 3 (allowed staged resolution, out-of-scope staged path, staged/worktree mismatch) |

What this means for operators: a resolver attempt that resolves and stages its allowed conflicted files is no longer rejected with `Resolver attempt scope cannot be verified; refusing to retry or commit.` The scope and final commit gates are unchanged for every other case.
