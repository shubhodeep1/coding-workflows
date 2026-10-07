<!-- changelog: fixed -->
- **The merged-PR guard no longer prompts on pipes and heredoc text.** Since #6133 and #6135, interactive sessions in Auto mode were stopped by `merged-PR guard (CLAUDE.md §21)` prompts on everyday commands. Two cases are fixed in `.claude/hooks/pr_merge_status_guard.py` and its `workflow-templates/` copy.

A pipe anywhere before a `git push` (`git log ... | head -3; git push origin <branch>`) made the push directory "unknown" and asked for confirmation. Pipeline elements run in subshells and cannot change the directory, so a pipe now leaves it known and a `cd` inside a pipeline is ignored. A `||` branch makes the directory unknown only after a `cd` in the same `&&`/`||` list. A heredoc body with an odd quote, such as `it's` in a commit message or `'''` in a `python3 - <<'EOF'` script, made the whole command unparseable and asked "Cannot parse the Bash command" even with no git in it. Bodies that Bash passes on as data are now removed before parsing.

| Case | Before | After |
| --- | --- | --- |
| `git log \| head -3; git push origin <branch>` | asks | checked normally |
| `cat <<'EOF'` with `it's` in the body | asks | allowed |
| `git commit -F - <<'EOF'` with `main's` in the body, on a merged branch | asks | blocked, as for any commit there |
| `bash <<'EOF'` body, or an unquoted body with `$(...)` | parsed as shell | parsed as shell (unchanged) |
| `cd "$VAR" && git commit`, `sleep 1 & git push` | asks | asks (unchanged) |

CLAUDE.md §23.D now leads with the rule to type IDs literally into `gh api` endpoint paths, since an unquoted `$VAR` or `$(...)` there prompts in every permission mode.

What this means for interactive sessions: Auto mode stops asking for routine push and heredoc commands, while a push that really could land on a merged branch is still blocked or confirmed.
