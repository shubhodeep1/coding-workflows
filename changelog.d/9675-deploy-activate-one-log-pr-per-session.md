<!-- changelog: changed -->
- **`/deploy-activate` keeps a session's activation-log updates in one pull request.** The command used to end up with one PR per log entry, each paying for a full review and check round.

The `## Activation Log` section of `.claude/commands/deploy-activate.md` and its template copy `workflow-templates/.claude/commands/deploy-activate.md` now tell the session to open one draft PR titled `chore: activation log updates` with its first log commit, push every later update to the same branch, and mark the PR ready for review once when the runbook work ends. A draft gets no AI review round per push. If the PR merges or closes mid-session, the session restarts its branch from the default branch (CLAUDE.md §21.A) and opens one new draft PR for the remaining updates, so nothing is committed onto a merged PR's branch; a PR closed without merging has its log commits rebased onto the new branch rather than dropped. A later session that finds an earlier session's open log PR merges that branch into its own before its first log commit.

| The numbers that matter | Value |
| --- | --- |
| Activation-log PRs opened 2026-11-10 to 2026-11-17 | 38 in binance-blessings, 10 in fun-token-multi-chain, 8 in tele-funtoken-msg-scoring |
| Log PRs per `/deploy-activate` session after this change | 1 (a second only if the first merges or closes mid-session) |

What this means for operators: a `/deploy-activate` session shows up as one log PR that is reviewed once at the end, not one PR per completed step. Consumer repos receive the new command text on their next `@stable` sync.
