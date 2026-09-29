<!-- changelog: security -->
- **Claude-fixer review no longer auto-merges past a failed reviewer slot when the consensus ledger leaves a reviewer out.** With a failed slot, the ledger now needs a block for every reviewer the runner ran, so an omitted reviewer's finding cannot slip past the clean-reviewer minimum.

In Claude-fixer mode, a review with a failed reviewer slot is clean when at least `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` other reviewers came back clean (issue #4835). `scripts/review_autofix_step_claude_fixer_handoff.sh` checked only the reviewer blocks the ledger contained. The ledger is written by a summariser model, so it could drop a reviewer's block entirely. With six reviewers and the minimum at 4, four clean blocks and one verified failed block passed, and the sixth reviewer's finding was never checked (security audit finding #5297). The check now reads the runner's own `status_review_<slug>.txt` and `review_<slug>.txt` files in `PREVIOUS_REVIEWS_DIR` as the reviewer roster, and every slot in it must have its own ledger block. An omitted slot, an empty roster, or an unexpected slot name keeps the round as a hand-off, with a warning naming the slot.

| The numbers that matter | Value |
| --- | --- |
| Roster source | `status_review_<slug>.txt` and `review_<slug>.txt` written by `scripts/review_run_reviewers.sh` |
| New variables, log keys, GitHub API calls | none |

What this means for operators and consumer repos: a `claude/*` PR can only auto-merge past a failed reviewer slot when every reviewer that ran is accounted for in the ledger. Reviews with no failed slot are unchanged. Consumers get this with the next `@stable` release, with no wrapper change.
