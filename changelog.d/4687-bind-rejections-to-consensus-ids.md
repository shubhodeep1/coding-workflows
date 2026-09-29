<!-- changelog: security -->
- **A reviewer's rejection of one review finding can no longer demote a different finding on a nearby line. Rejections are now bound to the finding they name by a pass-1 `consensus_id`, and an ambiguous match stays blocking.**

Security audit follow-up #4687 (A08, high) found a flaw in the #4586 rule that stops a rejected single-reviewer finding from blocking a `claude/*` PR. `scripts/review_claude_fixer_nonblocking.py` matched each `REJECTED_FINDING` line to a ledger entry by file, flagger, and a 3-line window. If one reviewer reported a false positive and a real flaw within three lines of each other, the rejections of the false positive also matched the flaw and moved it into `NON-BLOCKING FINDINGS`, and with green checks the PR could auto-merge. Now every pass-1 consensus finding shown to pass-2 reviewers carries a `consensus_id: p1-<12 hex>` line, a hash of its text. A rejection counts only when the run ID it cites (#4688) was issued for the entry with that id, and a pass-2 finding is tied to the rejected entry only through the same id, which the flagging reviewer must have written itself. A finding stays blocking when its id is missing, duplicated, or mismatched, or when another consensus entry lies within 3 lines of it in either pass.

| The numbers that matter | Value |
| --- | --- |
| Rejection line | `REJECTED_FINDING: <ID> \| <file>:<line> \| flagged_by: <slug> \| reason: …`, where the run ID (#4688) is bound to the entry's `consensus_id`; lines without a run ID are ignored |
| Id | `p1-` plus the first 12 hex digits of the SHA-256 of the pass-1 entry |
| Ambiguity window (keeps the finding blocking) | another consensus entry in the same file within 3 lines, in either pass |
| Rejections needed | unchanged: a strict majority of the other successful pass-2 reviewers, and at least 2 |
| New log lines | `CLAUDE_FIXER_NONBLOCKING_KEPT file=… flagged_by=… reason=…`, `CLAUDE_FIXER_NONBLOCKING_LEGACY_REJECTIONS count=<n>`, `CLAUDE_FIXER_CONSENSUS_IDS annotated=<n>` |

What this means for operators: a distinct finding next to a rejected false positive now reaches the Claude fixer instead of being demoted silently. Some rejected false positives also stay blocking, when a reviewer or the summariser drops the id or two findings sit close together, and cost one fixer round. That is the intended direction. Each `CLAUDE_FIXER_NONBLOCKING_KEPT` line in the review run log names the reason a single-reviewer finding stayed blocking.

### For contributors

`build_cross_pollination_summary` in `scripts/review_run_reviewers.sh` shows an annotated copy of the pass-1 ledger (`review_claude_fixer_nonblocking.py --annotate`) and leaves `consensus_pass1.txt` itself unchanged, so the hand-off step recomputes the same ids from it. If annotation fails, the plain ledger is shown with a warning; the run-ID list still names each rejectable entry's `consensus_id`. `scripts/summarize_reviewer_consensus.sh` copies a reviewer's `consensus_id:` line into the matching consensus entry and per-reviewer bullet, and never takes one from a `REJECTED_FINDING` line. The demoter also requires the flagger's raw `review_<slug>.txt` to contain the id, so an id the summariser copies onto the wrong finding binds nothing. Per-reviewer bullets move with a demoted entry only when they overlap it and carry its id.
