<!-- agents: section="Workflow architecture" -->
- **Prefixed directory builtins (#6038).** `pr_merge_status_guard.py` (both copies, byte-identical) strips the prefixes after which Bash still runs a builtin in the current shell before it looks for a directory change: `VAR=value` assignments, `builtin`, `command [-p]` and the `time [-p] [--]` reserved word (`_shell_builtin_view`). `command -v` / `-V` and an unknown option mean no builtin runs.
  - A prefixed `cd` is resolved like a bare one.
  - A prefixed `pushd`, `popd`, `eval`, `source` or `.` makes the directory unknown from the next segment on, so a later Git write asks. Git inside that same segment (`time eval 'git push'`) is still judged where the segment starts.
  - `builtin enable -n cd` and `command export CDPATH=...` count as redefining `cd` / setting `CDPATH`.

  Tests: `test_worktree_push_is_judged_behind_prefixes`, `test_unknown_directory_or_prefix_asks_instead_of_passing`, `test_prefixed_builtins_keep_open_branch_pushes_allowed` and `test_shell_builtin_view` in `tests/test_pr_merge_status_guard.py`.
