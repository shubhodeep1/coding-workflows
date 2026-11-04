<!-- changelog: fixed -->
- **Review runs that already retried lost editor changes no longer trigger a "heal rejected run provenance" WARNING.** The heal reporter now skips them, because the retry is already recovering the PR.

When the editor's changes are lost, `review_autofix.yml` dispatches a fresh review and the run ends green without a failure comment. The heal reporter, `scripts/workflow_failure_heal_autofix_report.sh`, still sent an `editor_changes_lost` report for that run. The intake only accepts a reporting run while it is still running or after it failed, so it rejected the report as `not_failed` and paged "Workflow failure heal rejected run provenance … reason no_verified_runs" (PR #8718, run 41564230342, 2026-11-03). The reporter now logs `WORKFLOW_HEAL_AUTOFIX_REPORT skip reason=changes_lost_retrying` when `CHANGES_LOST_REDISPATCHED` is `true` or `skipped_peer_inflight`. When no retry was possible, the run still reports as before.

What this means for operators: one fewer false WARNING. Consumer repos pick the change up on their next `@stable` sync, because the reporter is a staged support script and no wrapper change is needed.
