<!-- agents: section="Workflow architecture" -->
- **Wrapper prefixes and cd resolution (#6038).** `pr_merge_status_guard.py` (both copies, byte-identical) finds `git commit` / `git push` behind:
  - the `time`, `command`, `nice`, `ionice`, `exec`, `nohup`, `setsid` and `timeout` prefixes, with their options, in any order with `env`;
  - a `( ... )` subshell whose paren is glued to the command word or the last argument.

  It then runs the normal merged-PR check, so a stranded checkout blocks. It asks for confirmation instead of passing:
  - for another command with a `git` word followed by `push` or `commit` among its words (`sudo git push`, `xargs git push`), or a known runner (`sudo`, `ssh`, `xargs`, `find`, ...) with a Git write anywhere in its words (`sudo sh -c "git push"`);
  - for a prefix option it does not know;
  - for a `cd` behind a `CDPATH=` prefix, or after `export CDPATH=...` in the same command (also inside a later `bash -c`);
  - for any `cd` after `cd` is redefined by a function, alias or `enable -n cd`.

  `X=1 cd dir`, `builtin cd dir` and `command cd dir` are still resolved as a cd. `command -v git`, `time git status` and `echo git push` stay silent. Tests: the `#6038` block at the end of `tests/test_pr_merge_status_guard.py`.
