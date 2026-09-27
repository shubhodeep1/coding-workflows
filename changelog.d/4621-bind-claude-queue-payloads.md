<!-- changelog: security -->
- **The Claude issue pickup now starts only queue items that match what the run that queued them recorded.** An `ai:claude-issue-queue` issue edited after it was opened is refused instead of starting a session for the edited target.

Before this fix, the pickup trusted a queue issue because `github-actions[bot]` had opened it. That says nothing about the title and payload, which anyone who can edit the issue can change afterwards (security finding #4621). Now `claude-issue-intake.yml` and the `claude-pr-catch-all` job of `review_autofix_sweep.yml` record every queue item they open, with its exact title and payload, in a `claude-issue-queue-binding` artifact of their own run. `claude_issue_route.py queue-pending --fetch-repo` follows the item's `Intake run:` / `Sweep run:` line to that run. It checks that the run is a completed default-branch run of the right workflow and event, and that the run's head commit is on the default branch itself (one compare read against `refs/heads/<default branch>`, so a tag carrying the branch's name does not pass). It also requires the artifact to list the item unchanged, and the body to be exactly what the producer wrote, so no added text reaches the pickup. Anything else is listed under `ignored` (`unbound`, `binding_mismatch`, `binding_untrusted`, `binding_pending`, `binding_unavailable`) and left open for the watchdog.

| The numbers that matter | Value |
| --- | --- |
| Producer workflows and events trusted | `claude-issue-intake.yml` (`repository_dispatch`, `workflow_dispatch`), `review_autofix_sweep.yml` (`schedule`, `workflow_dispatch`), default branch only |
| Binding artifact | `claude-issue-queue-binding`, uploaded with `if: always()`, kept 30 days |
| Pickup API reads per wake with items open | 1 queue read + 4 shared reads + 1 compare read and 1 artifact download per completed producer run of the first 30 targets (per-run fallbacks when a listing misses) |
| New env vars (with defaults) | `CLAUDE_ISSUE_QUEUE_BINDING_FILE`, `CLAUDE_PR_SWEEP_QUEUE_BINDING_FILE` |

What this means for operators: nothing to configure. Queue items opened before this change carry no binding, so the pickup refuses them and the watchdog flags them after `CLAUDE_ISSUE_QUEUE_STALE_HOURS` (default 3). For an issue item, comment `/reclarify` on the target issue: the intake rewrites and re-binds the queue item. For a PR-fix item, close the stale queue issue, and the next hourly sweep queues a fresh, bound one. Consumer repos need no change.

### For contributors

`scripts/claude_issue_route.py` gains `append_queue_binding` / `add-queue-binding`, `load_queue_binding`, `evaluate_producer_run`, `fetch_queue_bindings`, `queue_binding_run_ids` and `queue_binding_verdict`. `queue_pending` takes an optional `bindings` argument and reports `deferred`. `queue-pending` gains `--bindings-json` and `--default-branch`. `queue_pending(..., bindings=None)` skips the check and is kept only for the sweep's "already queued" dedupe, which must count every open trusted item. Tests are in `tests/test_claude_issue_route.py` and `tests/test_claude_pr_sweep.py`.
