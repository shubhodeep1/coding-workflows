<!-- changelog: security -->
- **A failed reviewer slot now counts as a missing vote only when the reviewer runner's own output file shows the retry-exhaustion failure.** A slot that failed on a non-retryable error can no longer be passed off as a stalled slot to reach auto-merge with the other votes.

The Claude-fixer failed-slot rule (#4835) accepted a slot as failed when its consensus-ledger line was a retry-exhaustion line and `status_review_<slug>.txt` read `failed`. `scripts/review_run_reviewers.sh` writes `failed` to that status file for non-retryable errors too, and the ledger line is written by the summariser over reviewer output. Author-controlled review text could therefore lead the summariser to relabel a non-retryable failure as retry exhaustion, and five clean votes with green checks would enable auto-merge (issue #4885, security audit tracker #3576). `scripts/review_autofix_step_claude_fixer_handoff.sh` now also requires the runner-written `review_<slug>.txt` to be exactly the ledger block's line. A missing, empty, or unreadable file, a different line (such as `… failed after non-retryable error on attempt 1.`), or any extra output keeps the round as a hand-off and logs a warning naming the slot and which of those it found.

| The numbers that matter | Value |
| --- | --- |
| Files the check reads per failed slot | `status_review_<slug>.txt` and `review_<slug>.txt` under `PREVIOUS_REVIEWS_DIR` |
| New GitHub API calls | 0 |
| New variables or workflow inputs | 0 |

What this means for operators and consumer repos: a `claude/*` PR with one stalled reviewer still auto-merges once its checks are green, as #4835 intended. A slot that failed for any other reason no longer counts toward that. Consumers get it with the #4835 change on the next `@stable` release, with no wrapper change.

### For contributors

The warning reads `Claude-fixer ledger block '<slug>' reads failed but the reviewer runner's review_<slug>.txt is <missing | empty | unreadable | different>, not that failure line; the ledger is not clean.` Only verified slots are listed in the hand-off's "Reviewer slots that failed" line. The awk classifier now emits `failed <slug> <line>` records, and the repeated-block check compares slugs only.
