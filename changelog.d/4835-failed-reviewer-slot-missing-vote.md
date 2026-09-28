<!-- changelog: fixed -->
- **Claude-fixer review no longer hands off a clean review because one reviewer slot failed.** A slot that hit its retry limit, was killed, or timed out is now a missing vote, not a finding. The review stays clean when enough other reviewers completed with nothing to report.

In Claude-fixer mode, `scripts/review_autofix_step_claude_fixer_handoff.sh` used to require every reviewer block in the consensus ledger to read `(No findings reported.)`. A failed slot's block holds its failure line instead, so the step posted a `kind=findings` hand-off with 0 entries that no Claude session could resolve without a verdict bot. Final PR #4695 stopped this way after five of six reviewers came back clean and `minimax/minimax-m3` was killed on all three attempts (run 36416865588), and #4747 stalled the same day. A block now counts as a failed slot only when its single line is the reviewer runner's retry-exhaustion line for that block's own model and the runner's `status_review_<slug>.txt` reads `failed`. With failed slots, a ledger is clean when both consensus blocks are empty, no completed reviewer reported anything, and at least `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` blocks read `(No findings reported.)` from slots whose status reads `success`. It then takes the existing clean path, which still needs a fresh, ready check snapshot on the same head before auto-merge.

| The numbers that matter | Value |
| --- | --- |
| New repo variable | `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS`, default `5` |
| Failure lines recognised | slot retryable-failure limit, retryable failure recovery exhausted, failed after N attempts |
| New log key | `CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS pr=… head=… round=… failed_slots=… clean_reviewers=… min=…` |
| New GitHub API calls | 0 |

What this means for operators and consumer repos: a `claude/*` PR with a clean review and one stalled reviewer now auto-merges once its checks are green, instead of waiting on a human or a forced re-review. With fewer clean reviewers than the minimum, the round is handed to the Claude session as before, and the hand-off names the failed slots. Reviews with no failed slot are unchanged, and a value of `CLAUDE_FIXER_MIN_CLEAN_REVIEWERS` above the panel size restores the old behaviour. Consumers get this with the next `@stable` release, with no wrapper change.

### For contributors

The ledger text is model output over reviewer output, so it never proves a failure by itself: the status files come from `scripts/review_run_reviewers.sh` only. A failed slot is never a clean vote. Any other text in a block, a failure line naming another model, a repeated block, a slug outside `[A-Za-z0-9_-]`, or a status file that does not match keeps the round as a hand-off. Non-retryable errors and skipped slots are still findings.
