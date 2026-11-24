<!-- agents: section="Workflow architecture" -->
### Wedged queued runs (issue #9473)

GitHub can leave a workflow run in `status=queued` with zero jobs and refuses to cancel, force-cancel or delete it (run `43095142301`: 409, 409, 403). Every active-run guard ignores such a run once it is older than `ZOMBIE_RUN_MAX_QUEUED_HOURS` (default `24`; `0` or a value that is not a whole number disables the rule).

- Rule: `status == "queued"` and `created_at <= now - ZOMBIE_RUN_MAX_QUEUED_HOURS` hours. Other statuses never age out; a missing or unparseable `created_at` keeps the run active.
- Bash helpers in `scripts/gh_helpers.sh`: `zombie_queued_cutoff_epoch`, `zombie_queued_jq_def` (embed it in front of a jq program and pass the cutoff with `--argjson`; it defines `is_wedged_queued($cutoff)`), and `zombie_queued_log_ignored <guard> <cutoff>` (feed it with `<<<`, not a pipe, so the per-process dedupe survives). Scripts that source `gh_helpers.sh` with `|| true` define no-op fallbacks.
- Python helpers in `scripts/orchestrate_lib.py`: `zombie_run_max_queued_hours`, `is_wedged_queued_run` and `log_zombie_run_ignored`. `unblock_scan.py` and `ci_cancelled_rerun.py` load them from their own directory only and fall back to the old behaviour when the sibling is missing.
- Filter only the listing a guard already fetched; never add an API call for it.
- Guard labels: `poll_validate_active`, `stall_inflight_direct_check`, `build_active_issue_set`, `retrigger_review_inflight`, `std_retrigger_review_inflight`, `conflict_dispatch_active_autofix`, `autofix_inflight_peer`, `merge_train_release`, `review_autofix_sweep`, `auto_release_gate`, `auto_release_promote`, `auto_release_legacy`, `promote_cycle_self`, `promote_cycle_gate_idle`, `apply_analysis_orchestrate_in_flight`, `worktree_registry_active_runs`, `unblock_scan_busy`, `unblock_scan_active_judge`, `ci_cancelled_rerun`, `stable_gate_plan_runs`, `concurrency_snapshot` (`orchestrate_lib.build_concurrency_snapshot`), `rollback_stable_drain` (`scripts/rollback_stable.sh`, #9342). `tests/test_zombie_queued_guard_contract.py` locks them.
- Not filtered: `scripts/workflow_failure_heal.py` (run-provenance checks, never a dispatch guard) and the cancel step of `.github/workflows/cancel_on_pr_close.yml` (a 409 on a wedged run is harmless). That workflow's merge-train release step does run the filter through `review_merge_train.sh`, so its `env:` forwards `ZOMBIE_RUN_MAX_QUEUED_HOURS` like the other wired workflows.
- `SWEEP_STALE_QUEUED_MINUTES=0` turns off only the sweep's minutes cutoff; the `ZOMBIE_RUN_MAX_QUEUED_HOURS` floor still applies until that variable is `0` too.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `ZOMBIE_RUN_IGNORED` — `run=<id> workflow=<path> age_h=<n> guard=<guard>`, on stderr, once per run per guard per process, when an active-run guard ignores a wedged queued run (issue #9473).
LOG_PREFIX.name=ZOMBIE_RUN_IGNORED

<!-- agents: section="Workflow file size limit" -->
- Phase 2 moved too (#9473): the `e2e-smoke-test` step "Phase 2: Wait for plan to complete" (`id: wait-plan`) sources `scripts/stable_e2e_step_wait_plan.sh` through the same `github.sha`-pinned wrapper as Phase 4 and Phase 4b. The step's `env:` sets `PLAN_CREATED_AFTER` from `steps.create-issue.outputs.created_after`, because a script file cannot hold a `${{ }}` expression. With the wedged-queued filter added to that step, `test-and-mark-stable.yml` would have been 431,166 bytes, over the 430,000-byte limit in `tests/test_test_and_mark_stable_step_scripts_contract.py`; it is now about 415,000 bytes.
