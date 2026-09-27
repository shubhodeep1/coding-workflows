<!-- changelog: fixed -->
- **The §26 status check-in and the hourly `claude/*` catch-all sweep now pick up a review hand-off whose review run was triggered by an earlier push than the head it reviewed.**

When two pushes land close together on a `claude/*` pull request, GitHub records the first push as the review run's `head_sha`, but the review workflow reviews the pull request's head when it runs and posts its hand-off for the second push. `.claude/scripts/check_in_status.py` required the run's `head_sha` to equal the pull request head. As a result, `--hand-back` and `scripts/claude_pr_sweep.py` reported `waiting for verified completed review run` indefinitely and no Claude fixer was started (PR #4594, run 36290049170: run head `a59fc87`, reviewed head `d1c6f92`). The check now accepts a run whose triggering commit is the reviewed head or an ancestor of it, confirmed with one compare read. Every other check on the run and on the hand-off comment is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Extra REST reads per PR check | at most 1 (`compare/<run head>...<reviewed head>`), only when the two commits differ |
| Compare results accepted | `ahead` only; `diverged`, `behind` and malformed SHAs keep waiting |

What this means for operators and consumer repos: review findings on a `claude/*` pull request reach the pushing session, or a fresh `/fix-claude-pr` session from the sweep, even after back-to-back pushes. Consumer repos get the updated `workflow-templates/.claude/scripts/check_in_status.py` on the next `@stable` sync.
