<!-- changelog: fixed -->
- **A successful AI implementation run is no longer reported as a failure when the shared `ai-memory` branch loses a write race.** `memory_finalize_task` now fails open like the other post-PR bookkeeping helpers, and a memory-branch rebase conflict now names the files it collided on.

`implement.yml` calls `memory_finalize_task` only after the branch is pushed and the pull request is open, under `set -euo pipefail`. The `ai-memory` branch is a single ref that `implement`, `issue_pr_status` and `orchestrate_poll` all push to concurrently, so a losing writer can hit an add/add rebase conflict on the same `tasks/issue-<n>/lineage/task_lineage.v1.json` file. That conflict exited 2 and marked the whole run `failure`, which posted an "AI implementation workflow failed" comment on the issue, fired a Telegram alert, and recorded a `phase_failed` event in the memory ledger for a run that had actually succeeded. Every sibling post-PR helper — `memory_record_run_event`, `memory_record_candidate`, `memory_processed_command_complete` — already fails open; `memory_finalize_task` was the outlier. It now warns, emits `"fail_open":true` telemetry, and returns 0.

| The numbers that matter | Value |
| --- | --- |
| Observed incident | `tele-funtoken-msg-scoring` run 33231997918, issue #3833 (PR #3837 created, then merged) |
| Gap between the two writers | 38s (finalize commit 04:04:18, competing push 04:04:56) |
| Post-PR bookkeeping helpers that fail open | 3 of 4 → 4 of 4 |
| New regression tests | 5 |

What this means for operators: a run that pushed its branch and opened its PR now finishes green even when its lineage bookkeeping loses the race, so the failure comment, the Telegram alert, and the false `phase_failed` ledger entry stop firing on successful work. Lineage state is unaffected in practice — the writer that wins the race is the one holding the newer state.

### For contributors

`memory_processed_command_claim` is deliberately left strict: it is a mutual-exclusion gate whose failure must stop the caller, unlike the bookkeeping helpers around it. `persist_memory_operation` still treats a rebase conflict as terminal rather than re-running the operation onto the fresh head; for this call site that is the correct outcome, since re-running would rewrite a `merged` lineage state back to a stale `in_progress`. The conflict message now appends `rebase stdout`, where git writes `CONFLICT (add/add): Merge conflict in <path>` — stderr carries only `error: could not apply <sha>` plus generic hints, which is what made the original incident undiagnosable from the run log alone.
