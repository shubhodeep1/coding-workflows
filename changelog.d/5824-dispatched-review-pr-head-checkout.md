<!-- changelog: fixed -->
- **Dispatched review runs now review the pull request's head, not the default branch.** Sweep re-runs and the Claude-fixer `claude_fixer_converged_head` verification run stop handing off false "missing at HEAD" findings (issue #5824).

A `review_autofix.yml` run started by `workflow_dispatch` has no `pull_request` payload, so `codex-agent` → "Checkout repo" fell back to `github.sha` and put the default branch into `GITHUB_WORKSPACE`. The reviewer panel reads files from there while its git commands show the PR's diff, so on PR #5097 (run 36794195824) all 30 ledger entries reported the PR's own changes as absent. The checkout now uses `github.event.pull_request.head.sha || needs.gate.outputs.review_checkout_sha || github.sha`. The new gate output `review_checkout_sha` is the PR head SHA the gate already reads from `/pulls/<n>`, set only when the head is in the same repository. The gate logs `AUTOFIX_GATE_REVIEW_CHECKOUT … checkout=pr_head`, or `checkout=event_sha reason=cross_repo_head` with a warning for a fork head, which keeps `github.sha` so fork code never sits next to the run's secrets.

| The numbers that matter | Value |
| --- | --- |
| False findings on run 36794195824 (PR #5097) | 30 of 30 |
| New GitHub API calls | 0 (one more jq field on the gate's existing `/pulls/<n>` fetch) |
| Runs whose checkout changes | runs with no `pull_request` payload on a same-repository PR |

What this means for operators and consumer repos: `pull_request` runs and the no-PR `claude/**` push review are unchanged. Sweep re-runs, convergence verification runs, and manual `workflow_dispatch` reviews now read the head's files, so a Claude stage session no longer spends a round rejecting findings about the default branch. Consumers get the fix with the next `@stable` sync; no wrapper change is needed.
