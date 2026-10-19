<!-- changelog: fixed -->
- **Items parked for a person now stay in the needs-human digest.** The poller's label repair no longer strips `ai:needs-human` from an item that also carries `ai:blocked`, `ai:review-blocked` or `ai:implementation-failed`.

When the unblock judge spends every round on an item, it parks the item with `ai:needs-human` on top of its stop label and lists it in the needs-human digest. On the next tick, `reconcile_managed_issue_labels` in `scripts/orchestrate_poll_process.sh` ran `scripts/ai_labels.py repair-labels`, which kept only the last phase label in contract order and removed `ai:needs-human`. The digest then pruned the item as cleared, and the judge parked it again about 6 hours later with another CRITICAL alert. In fun-token-multi-chain#639 this repeated 9 times in 3 days while the digest read "Nothing is waiting". Repair now keeps `ai:needs-human` beside the kept stop label unless that label is `ai:merged` or `ai:closed`.

| The numbers that matter | Value |
| --- | --- |
| Re-park rounds on fun-token-multi-chain#639 | 9 (Oct 16–18) |
| Labels it now survives beside | `ai:blocked`, `ai:review-blocked`, `ai:implementation-failed` |

What this means for operators: an item in the digest stays there until a person removes `ai:needs-human` or the item merges or closes, and the judge stops re-judging and re-alerting on it.
