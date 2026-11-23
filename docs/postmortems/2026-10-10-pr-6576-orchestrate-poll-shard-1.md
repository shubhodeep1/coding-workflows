# Reproduction report: PR #6576 `orchestrate-poll` shard 1 failure

> **Date:** 2026-10-10
> **Issue:** re-issue of #6886 by the unblock judge
> **PR under investigation:** #6576 (head branch `ai/issue-6573`, squash-merged into `main` on 2026-10-09)
> **Outcome:** **No failure reproduced** in the runnable part of the partition.
> Most of the partition could not run in the sandbox (see "Coverage gap"), so
> the original failure is **not identified**. No code was changed.

This report is the terminal deliverable the approved plan calls for when bounded
reruns pass (plan decision D3). It is not a `BLOCKED` verdict and makes no
speculative code change. The aggregate `lint` gate, the `orchestrate-poll`
matrix and the partition logic in `.github/workflows/ci.yml` are unchanged.

## What "shard 1" was taken to mean

Two readings are possible, and both were run (clarification Q1: A+B):

- **A.** Matrix job `orchestrate-poll (1)`, which is group index 1 of 4. All four of its
  local shards were run.
- **B.** Local shard 1, the source of the
  `::error::orchestrate-poll shard 1 failed (exit N)` line in
  `.github/workflows/ci.yml`. Shards are numbered from 0. Local shard 1 was run
  in every group.

## Tree used

- Checkout: `main` snapshot commit `16b6bb004d592faa4e83ad47c182fbff6719b6f2`.
  The checkout is a single "isolated snapshot" commit with no history, and it
  includes the content #6576 squash-merged into `main`.
- **UNVERIFIED:** #6576's pre-squash recorded head is not present, and no
  `ai/issue-6573` ref exists in this checkout. A squash carries the PR head's
  tree into `main`, so a defect at the head would reproduce here unless a later
  commit fixed it. That cannot be told apart from this checkout.
- **UNVERIFIED:** The original failing job log was not fetched. The implement
  sandbox has no network and no GitHub credential. The partition below
  matches the failed job only if the module's test list is unchanged since
  #6576's head, so the names are listed at the end for comparison with the real
  log.

## Environment

| Item | Sandbox | CI (`orchestrate-poll` job) |
|---|---|---|
| Python | 3.11.2 | 3.12 (`actions/setup-python`) |
| `jq` | **absent** | present on `ubuntu-latest` |
| `gh` | absent | present |
| PyYAML | **absent** | installed by "Install Python CI dependencies" |
| Network | none | yes |

Nothing could be installed, because the sandbox is offline.

## Partition commands (copied from `ci.yml`, not modified)

1. "Derive orchestrate poll test subsets": `ast`-parse
   `tests/test_orchestrate_poll_process.py`, keep the top-level `test_*`
   functions, `sort()` them, and split off `test_implementation_failed_*`
   (the fast-fail subset). Result: 550 tests, 10 fast-fail, 540 remaining.
   The lists were written to `/tmp`, outside the worktree.
2. Group: `awk -v n=<g> -v total=4 'NR % total == n'` over the remaining list.
   Each group has 135 tests.
3. Local shard: `awk -v n=<s> -v total=4 'NR % total == n'` over the group
   list. Shard sizes are 33 / 34 / 34 / 34 in every group.
4. Each shard ran in parallel as
   `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_orchestrate_poll_process.py <names...>`,
   with one log and one exit code per shard, the same as CI.

`tests/test_ci_poll_test_sharding.py`, which CI runs before the shards, could
not run here: `ModuleNotFoundError: No module named 'yaml'`.

## Results

Every run exited 0. The pass and skip counts were identical on every repeat.

| Run | Selection | Exit | Passed | Failed | Skipped | Wall |
|---|---|---|---|---|---|---|
| 1 | group 1, shard 0 | 0 | 8 | 0 | 25 | 8 s (all 4 shards in parallel) |
| 1 | group 1, shard 1 | 0 | 5 | 0 | 29 | |
| 1 | group 1, shard 2 | 0 | 6 | 0 | 28 | |
| 1 | group 1, shard 3 | 0 | 5 | 0 | 29 | |
| 2 | group 1, shards 0-3 | 0 ×4 | 8 / 5 / 6 / 5 | 0 | 25 / 29 / 28 / 29 | 8 s |
| 3 | group 1, shards 0-3 | 0 ×4 | 8 / 5 / 6 / 5 | 0 | 25 / 29 / 28 / 29 | 8 s |
| 1 | group 0, shard 1 | 0 | 7 | 0 | 27 | 6 s (3 shards in parallel) |
| 1 | group 2, shard 1 | 0 | 8 | 0 | 26 | |
| 1 | group 3, shard 1 | 0 | 7 | 0 | 27 | |
| 1 | fast-fail subset (group 0 only in CI) | 0 | 0 | 0 | 10 | — |

No test failed in any run, so no test needed an isolated rerun.

Context runs, outside the `orchestrate-poll` job:
`tests/test_ai_engine.py` gave 51 passed under pytest.
`tests/test_orchestrator_judges_claude_engine.py` could not be collected
(no PyYAML).

## Coverage gap: most of the partition is UNVERIFIED

Every skip, 191 in the runs above, has the same reason:
`jq binary not available in test environment`. These are the tests that spawn
the real poller (`scripts/orchestrate_poll_process.sh`), which is where #6576's
behaviour change lives, so their results here say nothing about the CI
failure. In group 1, only 24 of 135 tests ran, and those are static or
contract-style checks that do not need `jq`.

The original failure is therefore **neither reproduced nor ruled out**. The
skips are a sandbox gap, not a product defect, so no production code or test
was changed for them (plan, failure modes).

## What would settle it

- Pull the log of the failed `orchestrate-poll` job on #6576's recorded head
  from the Actions UI or API, take the `FAIL` / `TEST_CASE_EVENT ...
  "status":"fail"` lines, and rerun just those tests on `main`, where `jq` is
  present.
- Or rely on the required CI of the PR that carries this report. It runs all four
  `orchestrate-poll` groups on a real runner against the current `main` tree.
  If they pass, the #6576 failure does not reproduce on `main`.

## Group 1 test names (for comparison with the real log)

#### Local shard 0 (33 tests)

```text
test_capture_intent_fingerprints_helper_is_defined_and_idempotent
test_clean_wave_skip_does_not_run_when_wave_has_failures
test_complete_verdict_enters_validation_and_finishes_after_integration_drift
test_contract_helper_guard_in_poller_tests
test_external_finalize_detect_skips_terminal_fallthrough
test_final_merge_success_sends_critical_telegram_alert
test_integration_backpressure_falls_back_to_raw_ahead_by_when_compare_commits_truncated
test_integration_judge_non_redispatch_verdict_keeps_terminal_path
test_judge_non_array_new_issues_is_treated_as_empty_without_aborting_cycle
test_judge_resume_reset_stall_only
test_manual_re_security_pass_takes_precedence_over_engine_auto_reset
test_parameterized_search_issues_calls_pin_get_only_on_targeted_poller_paths
test_resolver_tooling_refresh_function_has_3way_merge_fallback
test_retrigger_review_redispatches_pr_named_failure_tied_with_head_branch_success
test_revalidate_from_unauthorized_author_is_ignored
test_review_blocked_fix_scope_rejects_extensionless_citation
test_review_blocked_fix_scope_rejects_unrelated_action_and_claude_hook
test_review_blocked_merged_fix_followup_refuses_when_integration_branch_invalid
test_security_pass_blocker_free_post_codex_failure_escalates_at_existing_cap
test_security_pass_cycle_exhaustion_terminalizes_project
test_security_pass_exhaustion_judge_rejects_mixed_fail_verdict
test_security_pass_flag_off_preserves_legacy_completion_route
test_security_pass_merge_conflict_route_blocks_then_clean_pass_completes
test_security_pass_validation_complete_deleted_branch_findings_repair_converges
test_staged_support_latch_sweep_only_mode_releases_without_tracking_work
test_standalone_conflict_sweep_active_run_guard_skips_dispatch
test_standalone_retrigger_review_sees_pr_named_pending_run
test_state_extraction_ignores_newer_forged_state
test_tracking_body_reconcile_runs_during_normal_poll_cycle
test_validated_removes_stale_validating_and_validation_fixing_labels
test_validation_harness_error_raw_status_preserves_budget_and_sets_additive_label
test_verify_integration_fingerprints_list_mode_includes_must_not_exist_violations
test_verify_integration_fingerprints_ref_mode_rejects_resurrected_file
```

#### Local shard 1 (34 tests)

```text
test_actions_runs_cached_loader_uses_if_none_match_when_stale
test_capture_intent_fingerprints_records_must_not_exist_on_file_deletion
test_close_merged_issues_sweep_accepts_closing_body_reference_pr
test_complete_verdict_keeps_open_when_validation_disabled
test_custom_runner_emits_timing_heartbeat_and_preserves_exit_semantics
test_failed_comments_fetch_skips_state_reconstruction
test_force_merge_bypass_promotes_eager_pr_once_per_sha_and_records_audit
test_integration_backpressure_still_trips_on_real_work_drift_in_3928_shape
test_integration_stale_alert_fires_once_after_threshold
test_judge_prompt_over_character_cap_skips_codex_attempts
test_label_batch_graphql_partial_falls_back_to_rest
test_missing_integration_branch_marks_failed
test_project_state_and_reset_commands_require_authenticated_commenters
test_retrigger_review_does_not_increment_when_redispatch_skipped
test_retrigger_review_skips_empty_commit_for_review_run_past_stall_threshold_but_within_budget
test_revalidate_not_triggered_for_non_validation_failure
test_review_blocked_fix_scope_rejects_merged_followup
test_review_blocked_fix_scope_reports_all_workflow_edit_opt_out_paths
test_review_blocked_merged_followup_refuses_default_base_when_active_integration_branch_unavailable
test_security_pass_cap_mixed_blocking_and_low_keep_fixing_waives_nothing
test_security_pass_deleted_branch_rejects_inconsistent_final_pr_metadata
test_security_pass_external_finalize_route_blocks_then_clean_pass_completes
test_security_pass_head_advance_after_clean_pass_restores_fix_cycle_budget
test_security_pass_reaudit_after_merged_fix_is_a_delta_with_prior_findings
test_security_pass_waive_command_in_failed_state_persists_waivers_and_reaudits
test_stall_judge_escalate_human_adds_needs_human_label_and_increments_counter
test_standalone_conflict_sweep_missing_head_ref_logs_warning_and_continues
test_standalone_staged_support_guard_reuses_conclusive_comment_cache
test_state_identity_failure_skips_reconstruction_and_state_writes
test_unblock_scan_sweep_only_mode_runs_without_tracking_work
test_validation_dispatch_failure_marks_failed
test_validation_run_fallback_does_not_trigger_on_failure
test_verify_integration_fingerprints_list_mode_returns_violated_files
test_verify_integration_fingerprints_skips_empty_object
```

#### Local shard 2 (34 tests)

```text
test_backward_scan_label_batch_error_falls_back_to_rest
test_clean_wave_skip_advances_when_pending_issue_defs_exist
test_close_merged_issues_sweep_default_branch_unavailable_closes_nothing
test_complete_verdict_still_defers_validation_for_failed_wave_phase
test_deferred_creation_scopes_lookup_to_issues_and_adopts_existing_issue
test_final_merge_legacy_validated_gate_blocks_later_error_outcome
test_fresh_push_suppress_window_pinned_to_50_minutes
test_integration_conflict_branch_rebuild_refuses_audit_warnings
test_integration_sync_conflict_max_retries_env_var_is_documented_and_defaulted
test_judge_repeat_fingerprint_normalization_resets_on_material_change
test_malformed_latest_state_falls_back_to_older_valid_and_posts_healed_state
test_no_labels_open_issue_uses_bounded_recovery_policy
test_recovery_budget_legacy_flag_charges_every_failed_verdict
test_retrigger_review_inflight_guard_treats_zombie_run_as_eligible
test_retrigger_review_skips_empty_commit_when_review_run_inflight
test_review_blocked_fix_scope_accepts_cited_pr_file_without_citation_authority
test_review_blocked_fix_scope_rejects_protected_judge_citation
test_review_blocked_fix_target_rejects_head_move_during_judge
test_review_blocked_rejects_fork_before_any_judge_action
test_security_pass_closed_fix_evidence_lookup_failure_retains_fixing_state
test_security_pass_exhaustion_judge_accepts_all_findings_and_passes
test_security_pass_failed_project_auto_reset_fires_once_per_engine
test_security_pass_implementation_failed_noop_fix_is_reissued_with_noop_guidance
test_security_pass_reissue_state_persist_failure_adopts_successor_next_poll
test_security_pass_waiver_does_not_suppress_a_nearby_new_exploit
test_stall_judge_resolve_merge_conflict_dispatches_review_and_increments_once
test_standalone_conflict_sweep_skips_integration_base_prs
test_standalone_stall_recovery_reconciles_merged_pr_with_stale_merge_train_label
test_sync_conflict_dedupe_skips_identical_warnings
test_untrusted_security_pass_reset_cannot_clear_findings
test_validation_fixing_completion_preempts_sync_branch_missing_failure_when_final_pr_already_merged
test_verify_integration_fingerprints_blank_cli_ref_falls_back_to_working_tree
test_verify_integration_fingerprints_partial_removal_regressions
test_verify_integration_fingerprints_trailing_unknown_flag_returns_exit_2
```

#### Local shard 3 (34 tests)

```text
test_backward_scan_updates_prior_wave_merged_issue
test_clean_wave_skip_blocked_when_stuck_wave_forces_judge
test_close_merged_issues_sweep_rejects_closing_pr_merged_into_non_target_base
test_comprehensive_pending_complete_dispatches_release_without_optional_metadata
test_external_finalize_detect_leaves_merge_conflict_validation_path_intact
test_final_merge_legacy_validated_gate_fails_open_on_shell_wrapper_history_read_error
test_integration_ahead_creates_and_reuses_eager_draft_pr_before_validation_complete
test_integration_conflict_mergeable_payload_reuse_preserves_false_values
test_invalid_max_validate_cycles_defaults_to_three
test_judge_resume_ignored_for_validation_failed_project
test_managed_skip_retries_timeline_discovery_failure_without_closing_issue
test_no_labels_with_rest_fallback_closing_linked_pr_skips_retrigger_pipeline
test_resolver_tooling_refresh_3way_merge_falls_back_to_skip_on_conflict
test_retrigger_review_pr_named_lookback_reads_leading_zero_env_as_decimal
test_revalidate_allows_same_actor_after_integration_sha_changes
test_review_blocked_fix_scope_ignores_invalid_citations
test_review_blocked_fix_scope_rejects_template_outside_pr
test_review_blocked_judge_caps_minified_pr_diff_by_bytes
test_security_pass_advisory_followup_kill_switch_files_at_judge_time
test_security_pass_closed_fix_with_mention_only_merged_pr_still_fails
test_security_pass_exhaustion_judge_keep_fixing_cap_converts_to_advisories
test_security_pass_final_merge_reanswers_advisory_followups_parked_in_ai_blocked
test_security_pass_invalid_waiver_line_window_warns_and_keeps_running
test_security_pass_terminal_failure_records_engine_sha
test_staged_support_latch_predicate_error_fails_closed
test_standalone_attempt_merge_counts_conclusive_missing_implementation_pr_accurately
test_standalone_retrigger_review_counts_unresolvable_wrong_pr_as_attempt
test_standalone_stall_recovery_skips_when_phase_attempts_exhausted
test_task_state_mirror_disabled_writes_no_task_files
test_v2_extract_helper_matches_production_for_interleaved_older_complete_and_newer_prefix_same_total
test_validation_fixing_lookup_failure_is_fail_safe_and_no_backfill
test_verify_integration_fingerprints_fails_open_on_missing_file
test_verify_integration_fingerprints_ref_mode_fails_open_on_unknown_ref
test_wave_judge_isolation_failure_defers_without_terminal_judge_failure
```
