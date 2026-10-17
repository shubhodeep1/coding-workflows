<!-- changelog: security -->
- **The merged-PR guard now checks `git push` and `git commit` behind command prefixes and subshells, and asks when it cannot tell where they run.** Before, `time -p git push`, `command git push`, `nice git push`, `exec git push`, `(git push ...)` and a `cd` resolved through an in-command `CDPATH` or a redefined `cd` all passed `.claude/hooks/pr_merge_status_guard.py` without any check (#6038).

The hook now skips the `time`, `command`, `nice`, `ionice`, `exec`, `nohup`, `setsid` and `timeout` prefixes and a glued `(` subshell, then runs the normal check, so a push or commit stacked on a merged PR's history is blocked. Other commands that run a Git write it cannot follow (`sudo git push`, `xargs git push`, `ssh host 'git commit ...'`) ask for confirmation. So does a `cd` after `CDPATH=` or `export CDPATH=...` in the same command, and any `cd` after a `cd` function, alias or `enable -n cd`. `X=1 cd dir`, `builtin cd dir` and `command cd dir` are resolved as a cd. `command -v git`, `time git status` and `echo git push` stay silent.

| The numbers that matter | Value |
| --- | --- |
| Prefixes now looked through | 8 |
| New tests in `tests/test_pr_merge_status_guard.py` | 71 (47 fail on the previous hook) |

What this means for interactive sessions: a wrapped push onto merged history is blocked like a bare one, and a few rare forms now prompt instead of passing silently. Consumer repos get the change with the next `.claude/` sync.
