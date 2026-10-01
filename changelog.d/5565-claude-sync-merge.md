<!-- changelog: security -->
- **`claude/*` PRs are no longer enrolled in auto-merge. The review workflow merges them synchronously, right after the merge hold gate passes.** It also cancels any auto-merge enrollment it finds on an open `claude/*` PR, so a hold or a new push can no longer be outrun by an armed merge.

The #5316 merge hold gate checked claims and twin parity once, then ran `gh pr merge --auto`. On a base that requires checks, that enrollment waited for the checks and then merged without checking the gate again. A `hold` posted in the meantime, or a new head pushed with write access, did not stop it. `scripts/review_enable_auto_merge.sh` and the `deterministic-skip-merge` job in `.github/workflows/review_autofix.yml` now merge `claude/*` heads with the REST `PUT pulls/{n}/merge` call (`merge_method=squash`, `sha=<reviewed head>`). `gh pr merge` is not used for them, because on a base with a merge queue it enrolls or queues the PR even without `--auto`. If GitHub cannot merge yet, they enroll nothing and add no merge-authorization label, after one `pulls/{n}` read that counts a merge whose response was lost. The gate job of every review run cancels a leftover enrollment with `gh pr merge --disable-auto`, and every push starts a review run.

| The numbers that matter | Value |
| --- | --- |
| Merge call for an allowed `claude/*` head | REST `PUT pulls/{n}/merge` with `merge_method=squash` and `sha=<head>` (1 call; 1 `pulls/{n}` read after a refusal) |
| New audit value | `AUTOFIX_AUTO_MERGE_HEAD_BOUND` / `AUTOFIX_DET_SKIP_MERGE_BOUND … action=squash_sync` |
| New refusal reason | `AUTOFIX_AUTO_MERGE_SKIPPED … reason=merge_not_ready` |
| New log key | `AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED pr=<n> head_sha=<sha> result=disabled\|failed` |
| Extra API calls | 1 `gh pr merge --disable-auto` (GraphQL lookup + mutation), only when an open `claude/*` PR is enrolled, up to 3 attempts with 1 REST re-check after each failure; 0 for other PRs |

What this means for operators: nothing changes in coding-workflows. `main` and the project branches require no checks, so these PRs already merged at once. In a consumer repo whose base requires checks or reviews that are still pending when the review finishes, a `claude/*` PR now stays open with a `merge_not_ready` warning instead of being armed. The next review run, from a push or a dispatch, merges it. On a base that requires a merge queue the workflow no longer merges `claude/*` PRs at all: they stay open with the same warning. The pending-checks merge path planned in #4900 will merge PRs with pending checks once they pass, and it must merge synchronously too. Other PRs keep their existing auto-merge calls.

### For contributors

The cancel reuses the gate job's `pulls/{n}` read (`auto_merge: (.auto_merge != null)` in its projection). `tests/test_claude_merge_hold_gate.py` covers the synchronous merge and the refusal in both paths, the unchanged `--auto` calls for other heads, and the cancel block run against a fake `gh`.
