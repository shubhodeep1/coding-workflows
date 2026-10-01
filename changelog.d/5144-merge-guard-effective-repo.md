<!-- changelog: fixed -->
- **The §21 merged-PR guard now judges the repository each `git commit` / `git push` actually runs in, and the branch a push writes to.** A push from a scratch worktree is no longer blocked, or let through, because of the branch the session's main checkout sits on.

On 2026-09-29 a twin-sync push from a detached worktree onto an open PR's branch was blocked with "Branch `claude/write-plan-retire-master-session` already had PR #5123 merged", because `.claude/hooks/pr_merge_status_guard.py` only looked at the main checkout. Detaching the main checkout was the only workaround. The same logic meant a worktree pushing merged history could pass unguarded. The guard now replays the command line: `cd <path>`, `git -C <path>`, and `GIT_DIR=` / `--git-dir` choose the repository. `git push <remote> <src>:<dst>` (and `HEAD:<dst>`) is checked on `<dst>`, with `<src>` as the commit that would stack on it. When the directory cannot be resolved (a variable, a subshell, `pushd`, a `cd` joined by `||`, `&` or `|`, a `cd` after `&&` that may be skipped once its chain ends, a `cd` inside an `if`, loop or `case` body, a path that does not exist yet or cannot be entered), or a refspec cannot be turned into one branch (`HEAD:$B`, a glob such as `refs/heads/*`, a `heads/<branch>` shorthand, a source starting with `-`), the call is judged on the session checkout as before and a warning names the reason.

| The numbers that matter | Value |
| --- | --- |
| Directory sources followed | earlier `cd <path>` in the same command, `git -C <path>`, `GIT_DIR=<path>`, `--git-dir` |
| Path resolution | `cd` lexical (physical with `-P`), `git -C` and the git directory physical; a quoted or escaped `~` (`"~"`, `'~'`, `\~`, `--git-dir=~`) is a literal directory, not `$HOME`, read per word so a quoted copy elsewhere does not change an unquoted `~/x` |
| Git calls after a reserved word | `then git push`, `do git commit`, `else …`, `! git …`, `time git …` are guarded like a bare `git` call |
| Push refspecs judged on the target | `<src>:<dst>`, `HEAD:<dst>`, `+<src>:refs/heads/<dst>`, `<branch>` |
| Not judged | `--delete`, `:<dst>`, `refs/tags/…` (patterns such as `refs/tags/*` included), `--tags` (or `--tag`) with no refspec, also in a directory the guard cannot resolve |
| Judged on the current branch, then a confirmation prompt | `--all`, `--branches`, `--mirror` (and prefixes such as `--al`), the `:` matching refspec, `*` pattern refspecs |
| Judged on the session checkout, with a warning | refspecs containing `$`, a backtick, `*`, `?`, `[`, `{` or `~`; `heads/` / `tags/` / `remotes/` shorthands; a `-`-prefixed source |
| GitHub API calls | at most 1 per `(slug, branch)` pair judged, 0 when the 300-second cache holds it |

What this means for operators and supervising sessions: twin syncs, held-merge conflict resolutions, and fixer pushes can run from scratch worktrees (`git push origin HEAD:<phase branch>`) without detaching the main checkout first. A push that would strand work on a merged branch is blocked wherever it runs from. A bulk push such as `git push --all origin` now asks for confirmation, because the guard cannot check every branch it writes, and `git push --tags origin` is no longer judged against the checked-out branch.

### For contributors

The new logic lives in `guard_targets` and `_judge_guard_target`. `_unreachable_outcome` and `_request_confirmation` keep their behaviour and now wrap the non-printing `_unreachable_decision` and `_confirmation_payload`, so the MCP push path is unchanged. Several guarded calls in one command are merged into one hook result: any block blocks, otherwise one JSON object carries every warning and at most one confirmation prompt. CLAUDE.md §21.B and §21.D describe the rules. `tests/test_pr_merge_status_guard.py` covers the parser and the worktree cases against real git repositories.
