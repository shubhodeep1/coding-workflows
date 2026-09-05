<!-- changelog: fixed -->
- **The merged-PR commit guard no longer goes quiet when GitHub is unreachable, and it now covers the GitHub MCP push tools.** In Claude Code Web sessions where the API proxy answers HTTP 403, the guard used to allow every commit and push with a warning the model never saw; it now decides from git history, and asks a human before any push it cannot prove safe.

Consumer-repo sessions were pushing follow-up commits onto branches whose pull request had already merged. The `.claude/hooks/pr_merge_status_guard.py` hook that enforces `CLAUDE.md` §21 was installed, but in a Claude Code Web session without the GitHub App connected both of its transports (`gh api` and `gh pr list`) fail with HTTP 403, and its fail-open contract turned that into a silent allow. The hook now falls back to the git remote, which works in exactly those sessions: it fetches `origin/<default>` and inspects where the branch forks off it. A branch sitting on merge-commit side history whose remote tip is fully contained in the default branch is blocked with the usual `git checkout -B <branch> origin/<default>` remediation. Anything git cannot settle, including an absent remote ref that could be deleted or never pushed, routes a `git push` through the harness permission prompt so a human confirms the PR is still open; a bare `git commit` is still allowed with a warning, since work only strands once pushed.

Two further gaps closed in the same change. Pushes made through `mcp__github__push_files` and `mcp__github__create_or_update_file` never touched the Bash hook at all; a second `PreToolUse` matcher in `.claude/settings.json` now runs the same guard on them, using the fetched remote branch tip in place of `HEAD`. And in repositories that merge with merge commits, the merged head is an ancestor of the default branch, so the guard's ancestry test blocked the very reset §21.A prescribes; the fork point on the default branch's first-parent chain now tells a rebuilt branch from a stranded one.

| The numbers that matter | Value |
| --- | --- |
| GitHub API calls added by the fallback | 0 (one `git ls-remote`, one `git fetch`) |
| Hook timeout in `.claude/settings.json` | 60s → 90s |
| MCP tools newly guarded | `mcp__github__push_files`, `mcp__github__create_or_update_file` |
| Test file now run by `ci.yml` | `tests/test_pr_merge_status_guard.py` |
| Consumer repos reached on next `@stable` sync | 12 (`.github/ai/consumer_repos.json`) |

What this means for operators: in a session where GitHub is unreachable you will now see a permission prompt on `git push` naming the branch and the reason; allow it only if the PR for that branch is still open. Nothing to install: the hook, its settings wiring, and the updated §21 text reach consumer repos through the existing `workflow-templates/.claude/` mirror in `update_workflows.yml`.

### For contributors

The refinement and the fallback share one primitive, `on_first_parent_chain`, bounded by excluding the candidate's parents from the `git rev-list --first-parent` walk. A stacked branch (forked off another branch that has since merged by merge commit) is reported inconclusive when it has unmerged commits on origin or has never been pushed. For an MCP push whose target repository is not the local checkout, or whose remote tip cannot be fetched for ancestry verification, the guard asks instead of blocking because a block could not self-clear safely. `tests/test_pr_merge_status_guard.py` gained an offline end-to-end fixture with a bare origin reached through `url.<path>.insteadOf`, so fetch and ls-remote run without network, and `ci.yml` now runs the file.
