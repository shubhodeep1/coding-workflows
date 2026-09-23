<!-- changelog: added -->
- **Interactive Claude Code sessions now check back on every pull request they push, every 3 hours, until it merges or closes.** A new CLAUDE.md §26 arms the check-in automatically after a push, keeps the status read on a Sonnet subagent, and reports the next steps once the PR is terminal.

Until now a session pushed a branch, opened its pull request, reported the link, and went quiet; whoever merged or closed the PR had to come back and ask what was left. §26 makes the session arm a `send_later` self check-in 180 minutes out as soon as the PR exists, for PRs it opens and for existing PRs it pushes new commits to, one chain per PR. Each check-in delegates a single PR status read to a Sonnet subagent and, while the PR is open, re-arms silently with no message, no PR comment, and no CI, review, or conflict work. Once the PR is merged or closed, the session's own model writes the summary: which terminal state was reached, the concrete next steps that still exist, or a plain statement that the session can be closed, plus one push notification. A per-task "no check-in" from the user opts out. A new `PostToolUse` hook, `.claude/hooks/pr_check_in_reminder.py`, feeds the §26 reminder back into the model's context after every `git push`, `gh pr create`, and MCP PR-creation or remote-write call so the step is not forgotten late in a long session; it ships to consumer repos through the same `.claude/` sync and `/seed-repo` asset set as the §21 and §25 guards. §25 is unchanged: the check-in reads only the PR's open/merged/closed state and never subscribes to PR activity.

| The numbers that matter | Value |
| --- | --- |
| Check-in interval | 180 minutes |
| GitHub API calls per check-in | 1 |
| Model doing the status read | Sonnet subagent (session model only spawns it, re-arms, and writes the terminal summary) |
| New `CLAUDE.md` section | §26 |
| Hook | `.claude/hooks/pr_check_in_reminder.py` (`PostToolUse`) |
| Test file now run by `ci.yml` | `tests/test_pr_check_in_reminder.py` |
| Pull request | #4304 |

What this means for operators: after a session pushes a pull request you can leave it; hours later, once the PR merges or closes, the session tells you what is left to do or that it is safe to close. It still never touches CI or review comments on its own, and asking it in chat remains the only way to have it fix a PR. Nothing to configure; the hook and the §26 text arrive in consumer repos on the next `@stable` sync.

### For contributors

The session model cannot switch itself, so the Sonnet requirement is met by delegation: the fired turn spawns the status read as a Sonnet subagent before doing anything else. `send_later` binds the check-in to the session and survives container restarts; local sessions without the Claude Code Remote MCP server fall back to the in-session `CronCreate` scheduler and say so once. The hook's settings matcher is anchored (`^(?:Bash|mcp__.*__create_pull_request|mcp__.*__push_files|mcp__.*__create_or_update_file)$`) so an unanchored regex interpretation cannot select `mcp__github__create_pull_request_with_copilot`; the test file asserts the matcher, template parity, the seed-repo asset list, and the CLAUDE.md prose.
