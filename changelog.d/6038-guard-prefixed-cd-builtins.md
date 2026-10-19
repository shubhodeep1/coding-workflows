<!-- changelog: security -->
- **The merged-PR guard now sees a `cd`, `pushd`, `eval` or `source` written behind `time`, `builtin`, `command` or `VAR=value`.** Before, `time -p cd <worktree> && git push origin HEAD:<merged-branch>` and similar forms passed `.claude/hooks/pr_merge_status_guard.py` without any check, because the hook missed the directory change and judged the session checkout instead (#6038).

Bash runs `time cd dir`, `command -p cd dir`, `builtin pushd dir` and `command eval 'cd dir'` in the current shell, so the directory changes just as it does for the bare builtin. The hook now follows a prefixed `cd` to its target and blocks a push stacked on a merged PR's history there. A prefixed `pushd`, `popd`, `eval`, `source` or `.` now asks for confirmation, the same as the bare form already did. The same prefixes in front of `enable -n cd` or `export CDPATH=...` now count as redefining `cd`. `command -v cd` still runs nothing, and pushes to a branch with an open PR still pass without a prompt.

| The numbers that matter | Value |
| --- | --- |
| Command shapes that passed silently and now block or ask | 18 tested |
| New tests in `tests/test_pr_merge_status_guard.py` | 32 (26 fail on the previous hook) |

What this means for interactive sessions: a push onto merged history is caught whether the `cd` before it is bare or prefixed. Consumer repos get the change with the next `.claude/` sync.
