<!-- changelog: fixed -->
- **Workflow failure heal no longer files heal issues for the conflict resolver's host-only stop.** A merge conflict on a path the resolver sandbox keeps on the host (for example `.claude/hooks/pr_merge_status_guard.py`) still stops the review run with the manual-merge error, but it no longer turns into a chain of heal issues.

The conflict resolver fails closed with `::error::Conflict resolver: host-only conflicted path(s) need a manual merge: <paths>` when a conflicted path cannot enter its sandbox. Each such run, the identical-failure cap that follows it, and the `ai:needs-human` label it can lead to were all reported to the heal intake, which kept opening issues for a stop that only a human merge can clear (#6738). The review/autofix reporter, the label reporter and the intake now skip these reports with `skip reason=host_only_conflict_manual_merge`. The other non-retryable resolver failures (`conflict_resolver_sandbox_path_unsupported`, `conflict_resolver_sandbox_support_missing`) are still healed.

| The numbers that matter | Value |
| --- | --- |
| Failure reason skipped | `conflict_resolver_sandbox_path_host_only` |
| Report paths covered | 3 (per-run report, identical-failure cap, `ai:needs-human` label) |
| New GitHub API calls | 0 |

What this means for operators: a host-only conflict shows up once, as the resolver's error and the cap comment on the PR, and waits for a manual merge. Heal issues still open for every other review/autofix failure.

### For contributors

- `skip_reason()` in `scripts/workflow_failure_heal.py` decides all three paths. Cap reports carry an optional `repeated_failure_reason` (validated, `identical_failure_cap` reports only), taken from the gate's `FINGERPRINT_CAP_REASON` or the trusted head markers, never from the evidence text. Older reports without it route as before.
- The label path counts only markers posted by the `GH_PAT` account (`host-only-conflict-marker` subcommand, newest marker wins, a newer editor summary clears it). If that identity cannot be read, the report is dispatched as before. An `ai:needs-human` label on a linked issue whose own comments carry no such marker is still reported.
