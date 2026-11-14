<!-- agents: section="Workflow architecture" -->
### Wedged queued runs (issue #9473)

GitHub can leave a workflow run in `status=queued` with zero jobs and refuses to cancel, force-cancel or delete it (run `43095142301`: 409, 409, 403). Every active-run guard ignores such a run once it is older than `ZOMBIE_RUN_MAX_QUEUED_HOURS` (default `24`; `0` or a value that is not a whole number disables the rule).

- Rule: `status == "queued"` and `created_at <= now - ZOMBIE_RUN_MAX_QUEUED_HOURS` hours. Other statuses never age out; a missing or unparseable `created_at` keeps the run active.
- Bash helpers in `scripts/gh_helpers.sh`: `zombie_queued_cutoff_epoch`, `zombie_queued_jq_def` (embed it in front of a jq program and pass the cutoff with `--argjson`; it defines `is_wedged_queued($cutoff)`), and `zombie_queued_log_ignored <guard> <cutoff>` (feed it with `<<<`, not a pipe, so the per-process dedupe survives). Scripts that source `gh_helpers.sh` with `|| true` define no-op fallbacks.
- Python helpers in `scripts/orchestrate_lib.py`: `zombie_run_max_queued_hours`, `is_wedged_queued_run` and `log_zombie_run_ignored`. `unblock_scan.py` and `ci_cancelled_rerun.py` load them from their own directory only and fall back to the old behaviour when the sibling is missing.
- Filter only the listing a guard already fetched; never add an API call for it.
- Guard labels: `poll_validate_active`, `stall_inflight_direct_check`, `build_active_issue_set`, `retrigger_review_inflight`, `std_retrigger_review_inflight`, `conflict_dispatch_active_autofix`, `autofix_inflight_peer`, `merge_train_release`, `review_autofix_sweep`, `auto_release_gate`, `auto_release_promote`, `auto_release_legacy`, `promote_cycle_self`, `promote_cycle_gate_idle`, `apply_analysis_orchestrate_in_flight`, `worktree_registry_active_runs`, `unblock_scan_busy`, `unblock_scan_active_judge`, `ci_cancelled_rerun`, `stable_gate_plan_runs`. `tests/test_zombie_queued_guard_contract.py` locks them.
- Not filtered: `scripts/workflow_failure_heal.py` (run-provenance checks, never a dispatch guard) and `.github/workflows/cancel_on_pr_close.yml` (a 409 on a wedged run is harmless).

<!-- agents: section="Stable log prefixes (contractual)" -->
- `ZOMBIE_RUN_IGNORED` — `run=<id> workflow=<path> age_h=<n> guard=<guard>`, on stderr, once per run per guard per process, when an active-run guard ignores a wedged queued run (issue #9473).
LOG_PREFIX.name=ZOMBIE_RUN_IGNORED
