# Reproduction report: PR #6894 `orchestrate-poll (3)` shard 1 failure

> **Date:** 2026-10-11
> **Issue:** #7189 (re-issue of #7078 by the unblock judge)
> **PR under investigation:** #6894, merged into `main` on 2026-10-11
> **Failing run:** 37913663098, job `orchestrate-poll (3)`. `lint` failed
> because the aggregate requires every job to succeed.
> **Outcome:** **Pending this PR's CI.** No failure reproduced in the
> sandbox, but most of the selection was skipped there (see "Coverage gap").
> This PR's own `orchestrate-poll (3)` and `lint` checks settle it; see
> "What settles it".

This is the evidence-only deliverable from the approved plan for #7189
(decision D1: change nothing unless a defect is demonstrated). It follows the
layout of `2026-10-10-pr-6650-orchestrate-poll-group-3-shard-1.md` (#7083).
The work targets current `main`; #7078's instruction to work on
`ai/issue-6882` was dropped because PR #6894 had already merged. The shard
and group partition, the "no log or no exit code counts as failed" handling,
and the `lint` aggregate's `needs` / `if: always()` in
`.github/workflows/ci.yml` are all unchanged.

## What "shard 1" means

Matrix job `orchestrate-poll (3)` runs group index 3 of 4
(`poll-group: [0, 1, 2, 3]`, `strategy.job-index` exported as
`CI_POLL_TEST_GROUP_INDEX`). Inside that job, local shard 1 of
`CI_POLL_TEST_SHARDS` = 4 produces the
`::error::orchestrate-poll shard 1 ...` lines. Shards and groups are
numbered from 0.

## Tree used

- Checkout: `main` snapshot commit `0577a7e4585edde6737cd2e9218d24da4b5e24d5`,
  a single "isolated snapshot" commit with no history. The plan was made at
  `main` `fec7beed0eab1c1fe08faacf3f166173029a4f1e`. At this commit the
  module has the same 564 tests (10 fast-fail, 554 remaining) and the same
  138-test group 3 that the plan recorded.
- **UNVERIFIED:** PR #6894's failing head is not in this checkout.
- **UNVERIFIED:** run 37913663098's job log, and the later passing checks on
  PR #6894, were not fetched: the implement sandbox has no network and no
  GitHub credential. The failing test name and traceback are **not known**,
  and no test name is inferred from the current shard list (decision D3).

## Environment

| Item | Sandbox | CI (`orchestrate-poll` job) |
|---|---|---|
| Python | 3.11.2 | 3.12 (`actions/setup-python`) |
| `jq` | **absent** | present on `ubuntu-latest` |
| `gh` | absent | present |
| pytest | 9.1.1 (sandbox venv) | installed by "Install Python CI dependencies" |
| PyYAML | **absent** | installed by "Install Python CI dependencies" |
| Network | none | yes |

## Partition commands (copied from `ci.yml`, not modified)

1. "Derive orchestrate poll test subsets": `ast`-parse
   `tests/test_orchestrate_poll_process.py`, keep the top-level `test_*`
   functions, `sort()` them, and split off `test_implementation_failed_*`
   (the fast-fail subset, which only group 0 runs). Result: 564 tests,
   10 fast-fail, 554 remaining. The lists were written to `/tmp`, outside the
   worktree.
2. Group split: `awk -v n=3 -v total=4 'NR % total == n'` over the remaining
   list. Result: 138 tests.
3. Local shard split: `awk -v n=<s> -v total=4 'NR % total == n'` over the
   group list. Shard sizes: 34 / 35 / 35 / 34.
4. Each shard ran in parallel as
   `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_orchestrate_poll_process.py <names...>`,
   with one log and one exit code per shard, as CI does.

**Caveat: the selection depends on the head.** Adding or removing any test in
the module moves tests between `NR % total` buckets. The #7083 report counted
550 tests (135 in group 3, 34 in shard 1) at `f9a85b1`; the count has changed
since. PR #6894's failing head may therefore have run a different set of
tests as "group 3 / shard 1". The list below is exactly what current `main`
runs, which is not necessarily what run 37913663098 ran.

## Results (sandbox, one run)

| Selection | Exit | Passed | Failed | Skipped |
|---|---|---|---|---|
| group 3, shard 0 | 0 | 4 | 0 | 30 |
| group 3, shard 1 | 0 | 8 | 0 | 27 |
| group 3, shard 2 | 0 | 7 | 0 | 28 |
| group 3, shard 3 | 0 | 5 | 0 | 29 |

No test failed and every shard left a log and an exit code.

The shard-1 tests that actually ran and passed:

```text
test_capture_intent_fingerprints_substring_dedup_preserves_unrelated_pairs
test_custom_runner_tests_need_no_pytest_arguments
test_integration_backpressure_threshold_cache_avoids_command_substitution_subshells
test_resolver_tooling_refresh_allowlist_includes_both_retry_preludes
test_stall_recovery_retrigger_implement_arms_swap_label_before_posting_approved
test_verify_integration_fingerprints_blank_cli_ref_falls_back_to_working_tree
test_verify_integration_fingerprints_partial_removal_regressions
test_verify_integration_fingerprints_trailing_unknown_flag_returns_exit_2
```

`tests/test_ci_poll_test_sharding.py`, which CI runs before the shards, could
not run here: `ModuleNotFoundError: No module named 'yaml'` (**UNVERIFIED**).

## Coverage gap: most of the selection is UNVERIFIED

All 114 skips in group 3 have the same reason:
`jq binary not available in test environment`. These tests spawn the real
poller (`scripts/orchestrate_poll_process.sh`), so passing results here say
nothing about the CI failure. The original failure is **neither reproduced nor
ruled out** in the sandbox. The skips come from the sandbox, not from a
product defect, so no code or test was changed because of them.

## Code reading performed

- **Partition.** The two-level `NR % total == n` split in `ci.yml` is
  unchanged, and `tests/test_ci_poll_test_sharding.py` pins it against the
  real module (`POLL_MODULE`, `ast.parse`). Result: no defect.
- **Failure accounting.** The shard runner in `ci.yml` fails the step for a
  shard that has tests but no log (about line 1858), a missing or
  non-numeric `.rc` (about line 1872), or a non-zero exit, and prints the
  failing test names outside `::group::` before the
  `orchestrate-poll shard(s) failed.` tally (about line 1973; #6888, #6889,
  #6946). Result: no defect.
- **Aggregate.** `lint` (about line 2022) runs with `if: always()` and its
  `needs` covers the `orchestrate-poll` matrix, so a red group 3 fails the
  required check. Result: no defect.
- **Overall.** No test or poller defect can be shown from the code without
  the failing test's name.

## What settles it

This PR's own `orchestrate-poll (3)` and `lint` checks (decision D2). They run
the selection below against current `main` on a real runner with `jq`.

- **If they pass:** the run 37913663098 failure is superseded on current
  `main`. Record this PR's CI run ID here and set the Outcome to "Superseded
  on current `main`: no failure reproduced in the same shard".
- **If they fail:** the failing test names appear in the
  `::error::orchestrate-poll shard <n> failed tests: ...` and
  `::error::orchestrate-poll failing test: ...` annotations. Fix only the
  named test or poller defect, add
  `changelog.d/7189-orchestrate-poll-group3-shard1-fix.md`, record the root
  cause under a "Resolution" section here, and rerun. Do not weaken the shard
  or aggregate checks, and do not merge while a required check is red.

**Reopen condition:** a red `orchestrate-poll (3)` run on this PR or on later
`main` reopens the question under the "If they fail" branch above.

## Group 3, local shard 1 test names (35 tests, at `0577a7e`)

```text
test_all_invalid_state_comments_trigger_reconstruction_path_without_heal
test_capture_intent_fingerprints_substring_dedup_preserves_unrelated_pairs
test_close_merged_issues_sweep_closes_merge_into_declared_integration_branch
test_complete_verdict_redispatches_validation_when_previous_dispatch_cycle_exists
test_custom_runner_tests_need_no_pytest_arguments
test_final_merge_conflict_sets_merge_conflict_status
test_force_merge_bypass_promotion_failure_posts_retry_audit_once_per_sha
test_integration_backpressure_threshold_cache_avoids_command_substitution_subshells
test_integration_stale_alert_disabled_when_hours_zero
test_judge_prompt_keeps_small_pr_diffs_intact_under_default_byte_caps
test_label_batch_graphql_full_skips_rest_fallback
test_managed_stall_recovery_skips_destructive_blocked_latch
test_no_labels_with_rest_fallback_keyword_substring_still_retriggers_pipeline
test_resolver_tooling_refresh_allowlist_includes_both_retry_preludes
test_retrigger_review_pushes_past_spoofed_pr_named_run
test_revalidate_dedupes_same_actor_and_sha_within_five_minutes
test_review_blocked_fix_scope_rejects_capped_pr_listing
test_review_blocked_fix_scope_rejects_uncited_unrelated_script
test_review_blocked_judge_skips_codex_when_prompt_exceeds_character_cap
test_security_pass_auto_reset_unreadable_state_keeps_project_parked
test_security_pass_closed_fix_without_merged_pr_terminalizes_recoverably
test_security_pass_exhaustion_judge_keep_fixing_creates_consolidated_fix_issue
test_security_pass_first_audit_records_delta_pointer_and_findings_memory
test_security_pass_malformed_managed_issue_pages_defer_creation
test_security_pass_unrated_audit_output_fails_closed_before_judge
test_staged_support_latch_release_is_source_only_and_compensates_comment_failure
test_stall_recovery_retrigger_implement_arms_swap_label_before_posting_approved
test_standalone_conflict_sweep_skips_merge_train_queued_prs
test_standalone_stall_recovery_skips_destructive_blocked_latch
test_sync_conflict_escalates_to_judge_immediately_after_retry_budget_exhausted
test_untrusted_state_comments_cannot_replace_pipeline_state
test_validation_fixing_completion_preempts_sync_branch_missing_failure_when_final_pr_already_merged
test_verify_integration_fingerprints_blank_cli_ref_falls_back_to_working_tree
test_verify_integration_fingerprints_partial_removal_regressions
test_verify_integration_fingerprints_trailing_unknown_flag_returns_exit_2
```
