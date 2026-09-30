<!-- changelog: security -->
- **Claude-fixer review no longer auto-merges on a clean-looking ledger that leaves a failed reviewer out.** A zero-finding ledger with no failed reviewer block is now checked against the reviewer runner's own files before the round can auto-merge, the same way a ledger with a failed block already was.

In Claude-fixer mode, `scripts/review_autofix_step_claude_fixer_handoff.sh` marked a ledger clean from its text alone when no block was a failed slot. The ledger is written by a summariser model, so it could drop a failed reviewer's block, or write `(No findings reported.)` for a slot the runner recorded as `failed`, and the round then enabled auto-merge on green checks (security audit finding #5579). Every zero-finding ledger now needs a block for every slot the runner wrote a `status_review_<slug>.txt` or `review_<slug>.txt` for, a `success` status file behind every block, and no repeated block. Any gap, including an unreadable or empty `PREVIOUS_REVIEWS_DIR`, keeps the round as a hand-off with a `::warning::` naming the slot. Ledgers with a failed slot are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Checks now applied to a ledger with no failed slot | roster coverage, `success` status per block, no repeated block |
| Checks still applied only with a failed slot | strict runner-output verdict format (#5298), `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` (default 5) |
| New variables, log keys, GitHub API calls | none |

What this means for operators and consumer repos: a `claude/*` PR auto-merges on a clean review only when every reviewer the runner ran completed and appears in the ledger. A review where a reviewer was skipped or failed, and the ledger shows it as clean or leaves it out, is handed to the Claude session instead. Consumers get this with the next `@stable` release, with no wrapper change.
