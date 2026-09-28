<!-- changelog: security -->
- **A `REJECTED_FINDING` line quoted from a pull request can no longer demote a genuine review finding on a `claude/*` PR.** Rejection votes now count only when they name a finding ID issued for that review run.

Security audit finding #4688 showed a gap in the Claude-fixer hand-off gate from #4586, `scripts/review_claude_fixer_nonblocking.py`. It counted every line in a reviewer's raw output that looked like a rejection: indented or fenced code, a line with no reason, anything. A PR could therefore plant a correctly shaped `REJECTED_FINDING:` line in a doc or a code comment. When two reviewers quoted it, a real single-reviewer finding moved to the non-blocking block and the PR could auto-merge. Now, after pass 1, `review_claude_fixer_nonblocking.py --issue-ids` gives every single-reviewer consensus finding a random `RF-<16 hex>` ID and writes it to `rejection_ids_pass1.json`. The pass-2 cross-pollination header in `scripts/review_run_reviewers.sh` lists those IDs. The gate counts a line only when it names one of them, carries a non-empty reason, and sits at the start of a line outside any code block.

| The numbers that matter | Value |
| --- | --- |
| Vote shape | `REJECTED_FINDING: <ID> \| <file>:<line> \| flagged_by: <slug> \| reason: <one sentence>` |
| Finding ID | `RF-` + 16 hex characters from `secrets.token_hex`, new every time the pass-2 header is built |
| Manifest | `${PREVIOUS_REVIEWS_DIR}/rejection_ids_pass1.json` (schema `rejection_ids.v1`), saved with the partial-finalize artifacts |
| Demotion thresholds | unchanged: at least 2 rejecters, a strict majority of the other successful reviewers, lines within 3 |
| New log lines | `CLAUDE_FIXER_NONBLOCKING_VOTES manifest=<present\|missing> ids=<n> votes=<n>`, `CLAUDE_FIXER_NONBLOCKING_IDS issued=<n>` |

What this means for operators and Claude sessions: a single-reviewer finding is demoted only when a majority of the other reviewers rejected it through this run's own IDs. Nothing in the reviewed code can supply such a vote. A reviewer that writes the older shape without an ID, or omits the reason, casts no vote, so the finding stays blocking as it did before #4586. When the IDs cannot be issued, the header shows no rejection instructions and nothing is demoted.

### For contributors

The gate uses the manifest entry an ID names (file, line range, sole flagger), never the location a reviewer echoes, which stays in the line only for the summariser's informational `rejected_by:` line. A rebuilt pass-2 header always issues fresh IDs, so an ID an earlier run wrote (and may have published in an uploaded log artifact) never counts again. Votes in cached pass-2 outputs from that earlier run stop counting too, which fails toward blocking. Only a same-head resume that skips the whole reviewer phase keeps the persisted manifest, because it writes no new reviewer output. A manifest that exists but is invalid makes the gate exit 1, and the hand-off step then keeps the original ledger. `REJECTED_FINDING_RE` and `reviewer_rejections()` stay in the script for compatibility, but the gate no longer uses them. Residual risk: a PR that prompt-injects a majority of reviewers into writing fresh, correct votes. That is the same class of attack as injecting reviewers to leave a finding out altogether.
