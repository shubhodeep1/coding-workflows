<!-- changelog: security -->
- **Only the PR's author or the workflow account can claim or hold a `claude/*` pull request.** Before this change any collaborator could post an `ai:claude-fix-claim` hold marker and keep the §26 checker and the catch-all sweep away from a PR indefinitely.

`read_fix_claims` in `.claude/scripts/check_in_status.py` used to count every claim comment with an `OWNER`, `MEMBER`, or `COLLABORATOR` author association. A collaborator could therefore comment a well-formed `kind=hold` marker for a PR's current head, which parks it until someone pushes. The same collaborator could also post `conflict` / `ci` / `blocked` claims on made-up heads to push `hand_backs` to the cap, which forces the real fixer into a hold. Now a claim counts only when its comment is posted as the PR's own author (the account whose Claude sessions push and fix it) or as `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` (the `GH_PAT` account the `claude-pr-catch-all` sweep posts its reservations with). Logins are compared case-insensitively. When neither is known, no claim counts. The check reuses the PR read the verdict already makes, so it adds no GitHub API call (issue #4622).

| The numbers that matter | Value |
| --- | --- |
| Accounts whose claims count | 2: the PR's author and `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` |
| Extra GitHub API calls | 0 |
| Claim marker format, lease, cap | unchanged (`CLAUDE_FIX_CLAIM_LEASE_HOURS` 3, `CLAUDE_FIX_HAND_BACK_CAP` 3) |

What this means for operators: a hold or claim posted from any other account is now ignored, including one from a teammate's Claude session fixing a PR someone else opened, so the checker or the sweep may start a second fixer on that head. If the `GH_PAT` account differs from the users whose Claude sessions open `claude/*` PRs, set the `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` repository variable so the sweep's reservations keep counting. Consumer repos receive the updated `.claude/scripts/check_in_status.py` on the next `@stable` sync.

### For contributors

`_fix_claim_trusted_logins(pr)` builds the trusted set from `pr["user"]["login"]` and the environment variable, and `check_pr_hand_back` passes it to `read_fix_claims` through the new keyword parameter `trusted_logins` (default `()`, which counts nothing). New cases in `tests/test_check_in_status_hand_back.py` cover a forged hold, forged counted claims, the workflow account's reservation, a PR with no readable author, and case-insensitive matching.
