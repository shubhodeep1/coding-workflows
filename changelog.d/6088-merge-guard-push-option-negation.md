<!-- changelog: security -->
- **The §21 merged-PR guard now reads `git push` options in order, so `--no-delete` can no longer hide a branch push.** A push option word the guard cannot read the way git does now blocks the push instead of slipping through.

The security audit (finding `negated-delete-skips-branch-push`, issue #6088) showed that `git push --delete --no-delete origin HEAD:claude/stale` creates the branch, while `.claude/hooks/pr_merge_status_guard.py` stopped reading at `--delete` and skipped the merged-PR check. The guard now reads every option with the rules git 2.43 uses for `git push`: exact names, unambiguous prefixes, `--no-<option>` and its prefixes, short-option clusters, and options placed after the remote or refspecs. Delete, bulk, and tags-only are decided by the state after the last option. `git push --tags --no-tags origin`, which pushes the current branch, is judged too. So is `git push --recurse-submodules check origin`, which used to be checked against a branch named `origin`. An option git rejects (unknown, ambiguous such as `--no-d`, `--force=x`, `-o` with no value, `-dele`) blocks the push with a reason naming the word, and asks GitHub nothing.

| The numbers that matter | Value |
| --- | --- |
| `git push` options in the guard's table | 27 (git 2.43, `--branches` included) |
| Option words checked against real git 2.43 | 930 (rejected / usage / accepted all agree) |
| Newly read as bulk pushes (confirmation prompt) | `--b` (`--branches`), `--m` (`--mirror`) |
| Not judged (no push happens) | a final `--delete`, `-h`, `--help`, `--help-all` |
| GitHub API calls for a blocked unreadable option | 0 |

What this means for operators and supervising sessions: pushes written with full option names behave as before. A push that combines an option with its `--no-` form is judged as the push git runs. A push with a misspelled or ambiguous option is blocked with a message telling you to spell the option out, and git would have refused that word anyway. `CLAUDE_PR_MERGE_GUARD=off` still disables the whole guard.

### For contributors

`_read_push_option_word` mirrors `parse_long_opt` / `parse_short_opt` from git's `parse-options.c` over `_PUSH_OPTION_TABLE`. `_push_refspec_targets` applies the effects in order. `GuardTarget` gains `unreadable_reason`, which `_evaluate_bash` turns into a block before any API call. `_is_push_delete_option`, `_push_option_takes_next_word`, `_is_push_bulk_flag` and `_is_push_tags_only_flag` keep their names and now wrap the resolver.
