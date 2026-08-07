<!-- changelog: added -->
- **Commits and pushes onto a branch whose pull request already merged are now blocked before they happen, instead of silently stranding the work.** A new `PreToolUse` hook checks PR merge status on every `git commit` and `git push` in an interactive Claude Code session.

Sessions that run for days or weeks outlive the PRs they open. When the PR for the working branch merges mid-session, further commits land on history that is already in the default branch and that no open PR carries anywhere, so the work never ships and disappears when the branch is deleted. The rule against this existed only as prose in `CLAUDE.md`, which is furthest from the model's live context exactly when a session has run long enough for the merge to happen. `.claude/hooks/pr_merge_status_guard.py` now enforces it deterministically: the harness runs it on every Bash tool call regardless of what the model remembers, and a block is reported back with the `git checkout -B <branch> origin/<default>` remediation already filled in.

The guard blocks only when all three conditions hold: a merged PR exists for the current branch, no open PR exists for it, and that merged PR's head commit is an ancestor of `HEAD`. The third condition is what makes it self-clearing. Branch names are reused after the reset, so the merged PR keeps matching the branch forever, and ancestry is what separates stacking on merged history from fresh work that reuses the name. The guard goes quiet the moment the branch is reset, before the replacement PR exists, so no override flag is needed to commit the fix.

| The numbers that matter | Value |
| --- | --- |
| Consumer repos reached | 12 (`.github/ai/consumer_repos.json`) |
| Conditions required before a command is blocked | 3 |
| Guarded git subcommands | `commit`, `push` |
| GitHub API calls per guarded command | 1, cached 300s per `<slug>/<branch>` |
| New env var | `CLAUDE_PR_MERGE_GUARD` (unset = enabled; `off` disables) |
| New `CLAUDE.md` section | §21 |

What this means for operators: nothing to install. The hook, its `PreToolUse` wiring in `.claude/settings.json`, and the §21 rule that documents it all arrive in consumer repos on the next `@stable` sync, through the existing `workflow-templates/.claude/` mirror in `update_workflows.yml`. Every failure mode allows the command and prints a warning naming the branch, so a lapsed token degrades the guard to a no-op rather than blocking all committing.

### For contributors

PR state is read over REST (`gh api repos/<slug>/pulls?state=all`), not `gh pr list`. Claude Code Web's agent proxy serves only a pinned set of GraphQL operations and rejects the rest with HTTP 403, and `gh pr list` is GraphQL-backed, so using it as the primary transport would have made the guard fail open on every commit in exactly the long-running web sessions it exists to protect. `gh pr list` stays wired as a transport fallback for environments where REST is gated instead; it retries the same question rather than issuing a second query, so the §15 budget of one call per guarded command holds. Cached data can satisfy an allow, but a block is always re-verified against a live call first, so opening a new PR clears the guard immediately rather than after the TTL. The hook is invoked as `python3 .claude/hooks/pr_merge_status_guard.py` rather than relying on its executable bit, because the consumer sync copies with plain `cp`, which leaves an existing destination's mode untouched. `tests/test_pr_merge_status_guard.py` covers the detection rule, the fail-open contract, both transports, and the block-then-reset-then-allow sequence end to end against a real git repository.
