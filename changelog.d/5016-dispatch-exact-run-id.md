<!-- changelog: fixed -->
- **`.claude/scripts/dispatch_workflow.py` now records the run its own dispatch started, even when another session dispatches the same workflow seconds apart.** A `/implement-plan-claude` stage can no longer read another project's security or validation verdict.

On 2026-09-29 two stage sessions dispatched `security-audit.yml` 3 seconds apart, and the issue-4787 project recorded run 36512928347, which audited issue-4813's project branch; the helper took the newest new run, and both sessions saw both runs. The helper now sends `return_run_details: true` and uses the `workflow_run_id` GitHub returns for the dispatch. When GitHub returns no id, it falls back to polling but never guesses: a single new run is flagged `matched_by: new_run`, and several are reported as `ambiguous: true` with `candidate_run_ids` and exit 2. A failed read of the recent-runs list before the dispatch no longer cancels it: that list only serves the polling fallback, so without a returned id the helper exits 2 with `dispatched: true` instead of guessing. `/implement-plan-claude` steps 9 and 10 now confirm that the run audited or validated the project branch (`AUDIT_TARGET_REF_INPUT`, `target_ref`) before reading its verdict, and re-dispatch once on a mismatch.

| The numbers that matter | Value |
| --- | --- |
| New output fields | `matched_by` (`dispatch_response` or `new_run`), `ambiguous`, `candidate_run_ids` |
| Existing output fields | unchanged (`dispatched`, `workflow`, `ref`, `run_id`, `html_url`, `status`, `created_at`, `error`) |
| GitHub API calls on the normal path | 1 list read, 1 POST, 1 run read (previously 1 list read, 1 POST, 1 or more polls) |
| Workflow or consumer-wrapper changes | none |

What this means for operators: parallel `/implement-plan-claude` projects in the same repo can run their security audits and validations at the same time without cross-reading verdicts. Consumer repos pick the fix up with the next `.claude/` sync.
