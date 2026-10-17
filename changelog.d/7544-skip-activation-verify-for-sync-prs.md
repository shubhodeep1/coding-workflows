<!-- changelog: fixed -->
- **Activation verification no longer runs on the consumer workflow updater's own pull requests, and the implement prompt stops tests that pin an upstream commit SHA.**

Merged `auto/update-workflows-<sha12>` and `ai-sync/stable` pull requests only refresh upstream-managed wrappers, but the `activation-verify` job of `issue_pr_status.yml` graded them anyway. Of the 34 `ai:activation-fix:v1` issues open or closed across the consumer repositories on 2026-10-17, 7 came from such PRs (binance-blessings #370 and #373, bitsafe.io #534, #538 and #544, multi-user-ai-agent #285 and #290), and tele-funtoken-msg-scoring PR #4605 received a LIVE verdict comment. The job now skips a PR whose head branch is in the same repository and starts with `auto/update-workflows-` or equals `ai-sync/stable`, and `scripts/activation_verify.sh pr` repeats the check (new optional inputs `PR_HEAD_REF` and `PR_HEAD_REPOSITORY`) and logs `ACTIVATION_VERIFY mode=pr item=<n> outcome=skip reason=workflow_sync_pr sync_branch=<prefix>` before making any GitHub call.

The implement prompt also gains a "Pinned-SHA test discipline" paragraph, in the inline `mode-implement-inline.txt` heredoc of `implement.yml` (the prompt the implement job sends), `prompts/mode-implement.txt` and `prompts/_templates/mode-implement.txt`. An implement run in bitsafe.io had written `PINNED_SUPPORT_SHA = "420545bd50f77b44c59d1a7e8cc3b502fd8d1d6e"` into `test/ai-review-security-hold.test.mjs`; the next sync moved the wrapper pin and the test failed on the sync PR. The implementer is now told to assert the `^[0-9a-f]{40}$` shape, or that two pins agree, instead of a specific SHA.

| The numbers that matter | Value |
| --- | --- |
| Activation-fix issues traced to sync PRs | 7 of 34 |
| Skipped head branches | `auto/update-workflows-*`, `ai-sync/stable` (same repository only) |
| New GitHub API calls | 0 |

What this means for operators: nothing to set. Sync PRs stop producing activation-gap issues and verdict comments once a consumer's wrappers pick up the next `@stable` release; a fork PR with the same branch name is still verified. `ACTIVATION_VERIFY_ENABLED=false` still disables the whole job.
