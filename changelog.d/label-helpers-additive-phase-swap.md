<!-- changelog: fixed -->
- **Phase-label swaps no longer drop labels added while they run.** `set_issue_phase_label_resilient` in `scripts/label_helpers.sh` now adds the new phase label and then removes each old phase label one at a time. It no longer replaces the issue's whole label set.

Until now the helper read the issue's labels, worked out the new set and wrote it back with one `PUT`. A label added between the read and the write, such as `ai:orchestrator-managed`, `force-review`, `ai:needs-human` or `ai:merge-queued`, was removed by that stale write (finding BUG-001). Now the helper POSTs the target label first, so the issue always has a phase label, and then DELETEs each phase label that the read found. Labels outside the phase set are never touched. A removal that returns 404 counts as already done. Any other failed removal logs a warning and leaves the outcome as `applied`.

| The numbers that matter | Value |
| --- | --- |
| GitHub API calls per swap | 1 GET + 1 POST + one DELETE per old phase label (usually 1); was 1 GET + 1 PUT |
| `SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME` values | unchanged: `applied`, `failed`, `skipped` |

A phase label that another writer adds between the read and the removals can stay next to the new one. The existing label repair (`scripts/ai_labels.py repair-labels` / `reconcile_managed_issue_labels`) reconciles it.

### For contributors

When the target POST fails, the outcome is `failed` and no label is removed. When the read or the set computation fails, the helper still falls back to a single POST, as before. `tests/test_label_helpers_phase_transition_contract.py` runs the helper against a fake `gh` that adds `force-review` after the read, and checks that no call removes it.
