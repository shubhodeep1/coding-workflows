<!-- changelog: fixed -->
- **Implement runs no longer stack new work on an earlier PR's leftover `ai/issue-<N>` branch.** A follow-up implementation, such as the next phase of a multi-phase issue, now gets a clean branch from the base. It no longer carries already-merged history.

`.github/workflows/implement.yml` builds each run's commit on the fresh base branch. When the issue's earlier branch was still on origin, the "Push branch" step used to rebase the new commit onto it. Git dropped the squash commit as already applied, so the new PR re-showed the earlier phase's diff and could conflict with the base. After a rejected push, the step now asks `scripts/implement_stale_branch_check.sh` whether any open PR uses the branch. When none does, it replaces the branch with `--force-with-lease` pinned to the fetched SHA. An open PR, a failed lookup or a moved branch keeps the old rebase path. Each decision is logged as `IMPLEMENT_STALE_BRANCH branch= outcome=replace|keep reason=`.

| The numbers that matter | Value |
| --- | --- |
| PRs that reused `ai/issue-4726` in shubhodeep1/tele-funtoken-msg-scoring | 6 (#4727, #4729, #4735, #4744, #4752, #4755) |
| `ai/issue-*` branches on origin there, against open PRs on them | 737 vs 2 |
| Extra GitHub API calls | at most 1 per run, only after a non-fast-forward push |

What this means for operators: follow-up PRs for an issue show only their own changes, and the "still in merge conflict" alerts this caused stop. No variables or settings change.
