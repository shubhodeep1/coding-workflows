# Reproduction report: PR #6650 `orchestrate-poll (3)` shard 1 failure

> **Date:** 2026-10-10
> **Issue:** #7083 (re-issue of #6933 by the unblock judge)
> **PR under investigation:** #6650, merged into `main`
> **Failing run:** 37935105340, job `orchestrate-poll (3)`. `lint` failed
> because the aggregate requires every job to succeed.
> **Outcome:** **Pending: verified by this PR's own CI.** No failure
> reproduced in the runnable part of the selection. Most of the selection
> could not run in the sandbox (see "Coverage gap"), and code reading found
> no defect. No code was changed.

This is the evidence-only deliverable from the approved plan for #7083
(decision D1: change nothing unless a defect is demonstrated). It follows the
layout of `2026-10-10-pr-6576-orchestrate-poll-shard-1.md`. The shard and group
partition, the "no log or no exit code counts as failed" handling, and the
`lint` aggregate's `needs` / `if: always()` in `.github/workflows/ci.yml` are
all unchanged (decision D3).

## What "shard 1" means

Matrix job `orchestrate-poll (3)` runs group index 3 of 4
(`strategy.job-index`, exported as `CI_POLL_TEST_GROUP_INDEX`). Inside that job,
local shard 1 of `CI_POLL_TEST_SHARDS` = 4 produces the
`::error::orchestrate-poll shard 1 failed (exit N)` line. Shards and groups
are numbered from 0.

## Tree used

- Checkout: `main` snapshot commit `f9a85b1bb77b68688683dfdb02f20543f9f78fbf`.
  It is a single "isolated snapshot" commit with no history. The plan was made
  at `46f176d169ba001daca94434d3dff7ba98b88ae0` and clarification at
  `72903ed807455c080786ce768d93dd9eacc6f235`, all three of them `main`. At
  this commit the rebuilt shard-1 list is identical to the planning list.
- **UNVERIFIED:** PR #6650's pre-squash head is not in this checkout.
- **UNVERIFIED:** Run 37935105340's job log was not fetched, because the
  implement sandbox has no network and no GitHub credential. So the failing
  test name and traceback are **not known**.

## Environment

| Item | Sandbox | CI (`orchestrate-poll` job) |
|---|---|---|
| Python | 3.11.2 | 3.12 (`actions/setup-python`) |
| `jq` | **absent** | present on `ubuntu-latest` |
| `gh` | absent | present |
| pytest | present (sandbox venv) | installed by "Install Python CI dependencies" |
| PyYAML | **absent** | installed by "Install Python CI dependencies" |
| Network | none | yes |

## Partition commands (copied from `ci.yml`, not modified)

1. "Derive orchestrate poll test subsets": `ast`-parse
   `tests/test_orchestrate_poll_process.py`, keep the top-level `test_*`
   functions, `sort()` them, and split off `test_implementation_failed_*`
   (the fast-fail subset, which only group 0 runs). Result: 550 tests,
   10 fast-fail, 540 remaining. The lists were written to `/tmp`, outside the
   worktree.
2. Group split: `awk -v n=3 -v total=4 'NR % total == n'` over the remaining
   list. Result: 135 tests.
3. Local shard split: `awk -v n=<s> -v total=4 'NR % total == n'` over the
   group list. Shard sizes: 33 / 34 / 34 / 34.
4. Each shard ran in parallel as
   `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_orchestrate_poll_process.py <names...>`,
   with one log and one exit code per shard, as CI does.

**Caveat: the selection depends on the head.** Adding or removing any test in
the module moves tests between `NR % total` buckets. PR #6650's failing head
may therefore have run a different set of tests as "group 3 / shard 1". The
list below is exactly what this PR's CI runs, which is not necessarily what
run 37935105340 ran.

## Results (sandbox, one run)

| Selection | Exit | Passed | Failed | Skipped |
|---|---|---|---|---|
| group 3, shard 0 | 0 | 6 | 0 | 27 |
| group 3, shard 1 | 0 | 7 | 0 | 27 |
| group 3, shard 2 | 0 | 6 | 0 | 28 |
| group 3, shard 3 | 0 | 8 | 0 | 26 |

All four shards ran in parallel and finished in about 10 s. No test failed.

The shard-1 tests that actually ran and passed:

```text
test_capture_intent_fingerprints_substring_dedup_preserves_unrelated_pairs
test_linked_pr_graphql_queries_request_full_label_page
test_missing_pipeline_login_alerts_once_per_tick
test_review_blocked_open_pr_head_identity_contract
test_surface_reissue_closed_without_pr_emits_stable_signals
test_verify_integration_fingerprints_list_mode_unparseable_json_keeps_stdout_clean
test_verify_integration_fingerprints_substring_dedup_still_fails_on_unrelated_must_not_contain
```

`tests/test_ci_poll_test_sharding.py`, which CI runs before the shards, could
not run here: `ModuleNotFoundError: No module named 'yaml'`.

## Coverage gap: most of the selection is UNVERIFIED

All 108 skips in group 3 have the same reason:
`jq binary not available in test environment`. These tests spawn the real
poller (`scripts/orchestrate_poll_process.sh`), so passing results here say
nothing about the CI failure. The original failure is **neither reproduced nor
ruled out** in the sandbox. The skips come from the sandbox, not from a product
defect, so no code or test was changed because of them.

## Code reading performed

- **Partition.** The two-level `NR % total == n` split in `ci.yml` is
  unchanged, and `tests/test_ci_poll_test_sharding.py` pins it against the real
  module (`POLL_MODULE`, `ast.parse`). Result: no defect.
- **pytest dependency.** The module imports `pytest` at module level. Every
  job that runs it installs pytest (`ci.yml` "Install Python CI dependencies",
  and the release gates' `validate-scripts` jobs). Result: no defect.
- **Failure accounting.** The shard runner fails the step for a shard that has
  tests but no log, a missing or non-numeric `.rc`, or a non-zero exit. It
  prints failing test names outside `::group::` (#6889, #6946). Result: no
  defect.
- **Overall.** No test or poller defect can be shown from the code without the
  failing test's name.

## What settles it

This PR's own `orchestrate-poll (3)` and `lint` checks. They run the selection
below against current `main` on a real runner with `jq`.

- **If they pass:** the run 37935105340 failure is superseded on current
  `main`. Record that run ID here and set the Outcome to "Superseded on
  current `main`: no failure reproduced in the same shard".
- **If they fail:** the failing test names appear in the
  `::error::orchestrate-poll shard <n> failing tests: ...` annotation. Fix only
  the named test or poller defect, add
  `changelog.d/7083-orchestrate-poll-group3-fix.md`, and rerun. Do not weaken
  the shard or aggregate checks. Do not merge while a required check is red.

## Group 3, local shard 1 test names (34 tests, at `f9a85b1`)

```text
test_all_invalid_state_comments_trigger_reconstruction_path_without_heal
test_capture_intent_fingerprints_substring_dedup_preserves_unrelated_pairs
test_close_merged_issues_sweep_closes_merge_into_declared_integration_branch
test_complete_verdict_redispatches_validation_when_previous_dispatch_cycle_exists
test_deferred_creation_adopts_existing_github_issue_instead_of_duplicating
test_final_merge_keeps_legacy_open_non_draft_pr_behind_readiness_gate
test_forced_terminal_merged_phase_repair_removes_single_existing_phase_label
test_integration_backpressure_uses_wave_issue_count_when_total_issues_missing
test_integration_stale_alert_window_clears_when_branch_catches_up
test_judge_repeat_fingerprint_breaker_escalates_after_limit
test_linked_pr_graphql_queries_request_full_label_page
test_missing_pipeline_login_alerts_once_per_tick
test_reconciliation_uses_implementation_pr_masked_by_later_mention
test_retrigger_review_ignores_pr_named_dispatch_run_of_another_pr
test_retrigger_review_skips_empty_commit_when_pr_named_dispatch_run_is_in_flight
test_revalidate_with_extra_text_after_command
test_review_blocked_fix_scope_rejects_mixed_workflow_edit_opt_out
test_review_blocked_fix_target_rejects_failed_fetch_without_local_fallback
test_review_blocked_open_pr_head_identity_contract
test_security_pass_clean_result_is_sha_bound_and_allows_completion
test_security_pass_deleted_branch_repair_accepts_only_exact_sha_create_race
test_security_pass_fail_closed_without_integration_branch_rerenders_tracking_body
test_security_pass_implementation_failed_fix_with_open_blockers_defers_reissue
test_security_pass_reissue_adopts_existing_successor_without_duplicate
test_security_pass_waive_command_rejects_bots_and_malformed_ids
test_stall_judge_escalate_human_is_downgraded_when_human_terminalization_disabled
test_standalone_conflict_sweep_sees_named_pending_dispatch
test_standalone_stall_recovery_honors_ai_done_phase_attempt_override
test_surface_reissue_closed_without_pr_emits_stable_signals
test_untrusted_judge_resume_cannot_reset_project_counters
test_validation_fixing_backfills_ai_merged_from_linked_merged_pr_evidence
test_validation_run_fallback_ignores_unmarked_success
test_verify_integration_fingerprints_list_mode_unparseable_json_keeps_stdout_clean
test_verify_integration_fingerprints_substring_dedup_still_fails_on_unrelated_must_not_contain
```
