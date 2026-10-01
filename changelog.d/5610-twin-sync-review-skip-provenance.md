<!-- changelog: security -->
- **The AI review gate no longer skips a PR just because its head branch is named `claude/claude-twin-sync-*`.** Only a genuine Claude twin sync PR skips review now. Every other PR with that branch name, including fork and consumer-repository PRs, is reviewed normally.

The security audit found that the `Evaluate review gate` step of `.github/workflows/review_autofix.yml` exempted any `claude/claude-twin-sync-*` head from review (issue #5610). A PR author could pick that name, in a fork or in any consumer repository that calls the reusable workflow, and bypass the review. The exemption now requires three things: the PR is in `shubhodeep1/coding-workflows`, its head branch is in that same repository, and its author is the account `GH_PAT` authenticates as, which is the account `claude-twin-sync.yml` opens sync PRs with. Each denied exemption logs one line, `AUTOFIX_GATE_TWIN_SYNC_NOT_EXEMPT reason=<reason> pr=<n> head_ref=<ref>`. If the author, head repository, or `GH_PAT` login cannot be read, the exemption is denied, and a real sync PR still skips through the `[skip ai]` marker in its body. That holds on `workflow_dispatch` runs too: their callers pass no PR body to the gate, so for a `claude/claude-twin-sync-*` head the gate reads the marker from the PR it fetched itself.

| The numbers that matter | Value |
| --- | --- |
| Repositories where the exemption can apply | 1 (`shubhodeep1/coding-workflows`) |
| Deny reasons logged | `not_library_repository`, `head_repository_mismatch`, `pr_author_unknown`, `sync_identity_unavailable`, `pr_author_not_sync_identity` |
| Extra GitHub API calls per gate run | at most 1 `gh api user`, only for a `claude/claude-twin-sync-*` PR in coding-workflows from a same-repository head (shared with the marker-comment check when that runs); 0 for every other PR |
| New env vars or repo variables | none |

What this means for consumer repositories: after the next `@stable` sync, a `claude/claude-twin-sync-*` PR in your repository gets the same AI review as any other `claude/*` PR. The twin sync workflow never runs in consumers, so no real sync PR is affected.

### For contributors

A no-PR claude-branch push to a `claude/claude-twin-sync-*` branch (no PR number yet) keeps the exemption only in coding-workflows. That run skips review entirely (no reviewer panel, no commit comments) and cannot merge. `scripts/claude_pr_sweep.py` and CI's `claude_twin_sync.py check` still match sync PRs by name and same-repository head. `tests/test_review_autofix_claude_fixer_mode.py` covers every deny reason, the `[skip ai]` fallback on `pull_request` and `workflow_dispatch` runs, the no-PR path, and the single `gh api user` call.
