<!-- changelog: changed -->
- **`ensure_label_exists` now creates each label at most once per repository in a shell process.** A step that labels several issues no longer sends one `gh label create` call per issue for the same label.

Steps that label many issues with one label now make one label-create call instead of one per issue, which saves GitHub API quota. `scripts/label_helpers.sh` keeps a process-local cache keyed by repository and label (`_AI_LABEL_ENSURED_CACHE`) and records a label only after GitHub confirmed it, by creating it or by answering that it already exists; a later call for the same pair returns 0 without an API call and logs a `::debug::` line. A failed create is never cached, so the next call tries again and still logs the same `::warning::` and returns 1. Sourcing the file again keeps the cache, but subshells do not share it, so a caller inside `$(...)` or a pipeline may still repeat one create. `set_issue_phase_label_resilient` uses the cache through its existing `ensure_label_exists` call; its outcome values are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Label-create calls for one label across N issues | N+1 before, 1 now |
| Outcomes cached | 2 (created, already exists) |
| Failures cached | 0 |

What this means for operators: nothing to set or change. Runs that label many issues spend less GitHub API quota on label creation, and a label that fails to create is still retried and still logs its `::warning::` on every call.
