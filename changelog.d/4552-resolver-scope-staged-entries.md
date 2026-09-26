<!-- changelog: fixed -->
- **The review conflict resolver's scope check no longer fails a run because `git status` touched the index, and the resolver prompt now forbids staging.** Real staging still fails the run closed, and the log now says which check failed.

`_resolver_scope_state` in `scripts/review_conflict_resolve.sh` compared the raw bytes of `.git/index` before and after each resolver attempt. A read-only `git status` or `git diff` rewrites index stat metadata without changing any staged entry, so harmless attempts ended the `Run Codex resolver, validate, stage, commit` step with `Resolver scope check failed closed (ValueError)`. The check now compares `git ls-files -s -v -z` (mode, object id, stage, path, and assume-unchanged/skip-worktree flags) and keeps `MERGE_HEAD` byte-exact. The two failed runs behind issue #4552 also showed the resolver model running `git add` on the conflicted files, so `prompts/conflict-resolver.txt` now tells it never to change the Git index or merge state. A failed-closed scope action prints a second line, `Resolver scope <action> failure reason: <reason>`, for the function's own fixed `ValueError` messages only.

| The numbers that matter | Value |
| --- | --- |
| Failed review runs traced | `36240577301`, `36241767821` (PR #4549) |
| Existing `::error::Resolver scope … failed closed (…)` line | unchanged |
| Staging, index flag, or `MERGE_HEAD` changes | still exit 2, no retry, no commit |

What this means for operators: conflict-resolver runs on the workflow source repo no longer fail when the model only inspects the tree. A run that does fail on scope now names the check in its log, so a staging violation can be told apart from other unsafe states without the full model transcript.
