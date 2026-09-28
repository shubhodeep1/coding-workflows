<!-- changelog: changed -->
- **`gh api` reads and routine writes no longer stop Claude Code sessions at a permission prompt.** A new hook, `.claude/hooks/gh_api_write_guard.py`, replaces the seven `gh api` `permissions.ask` rules in `.claude/settings.json` and prompts only for writes that are not routine.

The old ask rules matched any `gh api` call carrying `-X`, `--method`, `-f`, `-F`, `--field`, `--raw-field` or `--input`, so a read such as `gh api -X GET search/issues -f q=...` prompted exactly like `gh api -X DELETE`. An ask rule prompts even in Auto mode, which stopped unattended `/implement-plan-claude` stage sessions on searches, PR-description updates, progress-comment edits and security-audit dispatches. The hook works out each call's real HTTP method and lets reads and CLAUDE.md §23.B routine writes to the session's own repository through: creating a PR, editing a PR's or issue's title or body, adding or editing comments, replying to review threads, adding or removing a label, requesting reviewers, and dispatching the six workflows already allowed as `gh workflow run <file> *`. It approves the whole command when the rest is only `cd`, `sleep`, `echo`, `2>&1` or a pipe into `head`, `tail`, `wc -l` or `sort`. Every other write, including closing a PR or issue, merging, deleting a branch, changing settings, dispatching any other workflow, or writing to another repository, still prompts in every permission mode. It also prompts for `gh api` that could run out of the hook's sight, such as inside `$(...)`, `bash -c`, `sudo`, `xargs` or a heredoc fed to `python3`.

| The numbers that matter | Value |
| --- | --- |
| `gh api` ask rules removed from `.claude/settings.json` | 7 |
| Workflows whose `gh api` dispatch is routine | 6 (`security-audit.yml`, `ai-security-audit.yml`, `internal-validate.yml`, `ai-validate.yml`, `review_autofix.yml`, `ai-review.yml`) |
| GitHub API calls the hook makes | 0 |
| Repos affected | this repo and the 13 consumers in `.github/ai/consumer_repos.json`, on the next `@stable` sync |

What this means for operators: unattended stage sessions stop waiting on prompts for reads and routine writes. Destructive or administrative `gh api` calls still ask a human. Commands that combine `gh api` with loops, `python3` scripts, `$VAR` paths or file redirects are left to the allow list or the Auto-mode classifier, so they can still prompt outside Auto mode. The hook fails closed: if it cannot read its input or hits an internal error, it prompts. Do not add `gh api` ask rules back, because an ask rule overrides the hook.

### For contributors

CLAUDE.md §23.H documents the classification and §23.D how to shape `gh api` calls so the hook can approve them. `tests/test_gh_api_write_guard.py` runs in its own `ci.yml` step and covers the commands from the observed prompts, each class, the helper allowlist, the fail-closed contract, and the wiring. It also checks that the dispatchable workflows match the `gh workflow run` allow rules. `workflow-templates/.claude/hooks/gh_api_write_guard.py` and `workflow-templates/.claude/settings.json` must stay byte-identical to the root copies.
