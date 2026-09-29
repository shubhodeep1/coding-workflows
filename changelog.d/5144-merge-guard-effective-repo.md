<!-- changelog: fixed -->
- **The §21 merged-PR guard now judges the repository each `git commit` / `git push` actually runs in, and the branch a push writes to.** A push from a scratch worktree is no longer blocked, or let through, because of the branch the session's main checkout sits on.

On 2026-09-29 a twin-sync push from a detached worktree onto an open PR's branch was blocked with "Branch `claude/write-plan-retire-master-session` already had PR #5123 merged", because `.claude/hooks/pr_merge_status_guard.py` only looked at the main checkout. Detaching the main checkout was the only workaround. The same logic meant a worktree pushing merged history could pass unguarded. The guard now replays the command line: `cd <path>`, `git -C <path>`, and `GIT_DIR=` / `--git-dir` choose the repository. `git push <remote> <src>:<dst>` (and `HEAD:<dst>`) is checked on `<dst>`, with `<src>` as the commit that would stack on it. When the directory cannot be resolved (a variable, a subshell, `pushd`, a path that does not exist yet), the call is judged on the session checkout as before and a warning names the reason.

| The numbers that matter | Value |
| --- | --- |
| Directory sources followed | earlier `cd <path>` in the same command, `git -C <path>`, `GIT_DIR=<path>`, `--git-dir` |
| Push refspecs judged on the target | `<src>:<dst>`, `HEAD:<dst>`, `+<src>:refs/heads/<dst>`, `<branch>` |
| Not judged | `--delete`, `:<dst>`, `refs/tags/…` |
| GitHub API calls | at most 1 per `(slug, branch)` pair judged, 0 when the 300-second cache holds it |

What this means for operators and supervising sessions: twin syncs, held-merge conflict resolutions, and fixer pushes can run from scratch worktrees (`git push origin HEAD:<phase branch>`) without detaching the main checkout first. A push that would strand work on a merged branch is blocked wherever it runs from.

### For contributors

The new logic lives in `guard_targets` and `_judge_guard_target`. `_unreachable_outcome` and `_request_confirmation` keep their behaviour and now wrap the non-printing `_unreachable_decision` and `_confirmation_payload`, so the MCP push path is unchanged. Several guarded calls in one command are merged into one hook result: any block blocks, otherwise one JSON object carries every warning and at most one confirmation prompt. CLAUDE.md §21.B and §21.D describe the rules. `tests/test_pr_merge_status_guard.py` covers the parser and the worktree cases against real git repositories.
