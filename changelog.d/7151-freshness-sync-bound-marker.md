<!-- changelog: fixed -->
- **A freshness update now skips review only for the exact commit GitHub produced, and remaining base overlap never merges without CI.** Two security-pass findings in the #7084 freshness-sync path are closed with automated controls; no human step is added.

After an accepted `update-branch`, `_pr_base_fresh_for_merge` (`scripts/pr_checks_lib.sh`) now waits for the new head, checks that it is GitHub's signed two-parent merge of the approved head with the base tip the freshness check saw, and only then posts the marker, which gains `base=<base tip> merge=<update commit>`. The review gate (`scripts/review_head_gate.sh freshness-sync`) skips the reviewer panel only when the head is that recorded `merge` commit with parents `[from, base]`. A branch reset to the approved head followed by a GitHub conflict-resolver merge produces a different commit and is reviewed normally (`freshness-sync-signed-merge-review-bypass`). Legacy markers without the new fields never skip (`reason=unbound_marker`).

`MERGE_BASE_FRESHNESS_MAX_SYNCS` no longer lets a PR merge over remaining overlap (`merge-base-sync-cap-proceeds-on-overlap`). Past the threshold each further update still happens and logs `::warning::MERGE_BASE_FRESHNESS ... action=update reason=max_syncs_exceeded`, so the merge waits for CI on a commit bound to the current head and base.

| The numbers that matter | Value |
| --- | --- |
| Extra reads per accepted update | at most 5 PR reads and 1 commit read |
| Updates after which a warning is logged | `MERGE_BASE_FRESHNESS_MAX_SYNCS` (default 2) |
| Merges allowed over remaining overlap | 0 |

What this means for operators: a PR whose base keeps changing the same files now keeps updating, one CI run each, until the base is quiet for one CI cycle. When the update commit cannot be observed or verified, or the base gained more than 250 commits, no marker is posted (`MERGE_BASE_SYNC_MARKER ... outcome=skipped reason=<token>`) and the next run reviews in full. `MERGE_BASE_SYNC_HEAD_POLL_ATTEMPTS` (default 5, 1-20) and `MERGE_BASE_SYNC_HEAD_POLL_SECONDS` (default 3, 1-30) tune the wait for the new head.

### For contributors

The fallback `_pr_base_fresh_for_merge` stubs in `scripts/review_enable_auto_merge.sh`, `scripts/review_rb_judge.sh`, `scripts/orchestrate_poll_process.sh` and the `deterministic-skip-merge` job now defer (`outcome=unknown reason=library_unavailable`) instead of letting a merge proceed unchecked. Refs #6664. Tests: `tests/test_pr_base_freshness_gate.py`, `tests/test_review_head_gate.py`.
