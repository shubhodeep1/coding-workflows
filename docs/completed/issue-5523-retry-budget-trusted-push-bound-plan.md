# Bound the changes-lost retry budget by GitHub-recorded run times, not the head commit's committer time

Source issue: shubhodeep1/coding-workflows#5523 (https://github.com/shubhodeep1/coding-workflows/issues/5523)
Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
Security pass: skip (ai:security: automation-produced issue)

## Summary

`autofix_changes_lost_head_retry_consumed` in `scripts/gh_helpers.sh` decides whether a PR head has already used its one automated editor-changes-lost retry. When its branch lookup counts nothing, it counts PR-named review runs created at or after a push-time bound. That bound is the earlier of the head commit's committer time and the first branch run on the head. A committer time is set by whoever made the commit, so a PR author can backdate it, pull earlier heads' review runs into the count, and suppress the recovery retry (security finding `retry-budget-uses-author-controlled-commit-time`). This plan drops the commit time from the bound and keeps only timestamps GitHub records for runs on that exact head SHA.

## Context

- The finding was filed by `.github/workflows/security-audit.yml` against #4898's project branch (`Refs #3576`, the audit tracker). It names `scripts/gh_helpers.sh:1575`, the jq program that computes `push_bound`.
- #4898 (plan `docs/plans/issue-4898-retrigger-dispatch-default-branch-plan.md`, its AD-4) moved the changes-lost retry to a default-branch `workflow_dispatch`, so the retry's `head_sha` is the default branch's and the branch lookup can no longer count it. The helper therefore also counts completed, non-cancelled `workflow_dispatch` runs named for the PR (`_autofix_pr_named_review_runs`) created at or after the push-time bound.
- On the base branch the bound is (`scripts/gh_helpers.sh` lines 1567–1581):
  `min(created_at of every branch run whose head_sha == head, head_commit_epoch)`.
  `head_commit_epoch` is the fifth argument, which `scripts/review_autofix_step_changes_lost_redispatch.sh` line 48 fills with `git log -1 --format=%ct HEAD`: the committer date, which `git commit --date` / `GIT_COMMITTER_DATE` set freely.
- Exploit: the PR author pushes head B with a committer date days in the past. The bound becomes that date, every completed PR-named review of earlier heads (created after it) counts as a prior retry of B, and B's changes-lost run skips its one recovery dispatch (`AUTOFIX_DISPATCH_SKIPPED reason=changes_lost_budget_unavailable_or_exhausted`). The review then ends in the terminal blocked comment instead of retrying.
- A branch run's `created_at` is set by GitHub when the push event creates the run, and its `head_sha` is the commit GitHub ran it for. Neither is author-controlled. A push creates runs of every workflow triggered on the branch (the review wrapper's `pull_request` run, cancelled twins included, plus CI), so the helper's existing `branch=<head>&per_page=30` page normally holds at least one run on the head. The bound uses any workflow's run on the head, not only review runs.

## Goals

- `push_bound` is computed only from `created_at` of runs in the existing branch page whose `head_sha` equals the reviewed head. The head commit's committer time no longer affects the result.
- With no such run, the helper fails closed exactly as it already does for a missing bound: `AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED … reason=missing_head_time` on stderr, return 0 (budget consumed, no dispatch).
- A backdated head commit can no longer make completed PR-named runs from before the head's first run count toward its budget. A regression test shows this: it fails on the base branch and passes after the change.
- No new GitHub API call (§15): the bound still comes from the branch page the helper already fetches.
- `agents.md`, the helper's header comment, and the step script's comment describe the new bound (§7).
- §20: one `changelog.d/` fragment in the `security` section.

## Non-goals

- Binding PR-named runs to the head SHA they reviewed through a new dispatch input or run-name field (AD-1 alternative C): that changes the pinned run names of `internal-review.yml` / `ai-review.yml` and the poller's `_pr_named_review_dispatch_runs` matcher, and needs a consumer `@stable` sync.
- The peer check `autofix_retrigger_has_inflight_peer`: it uses no time bound.
- Rewriting #4898's plan, log, or changelog fragment. They are that project's record; its fragment still reads correctly (the fifth argument is still accepted).
- Any change to `.claude/**`.

## Constraints

- §6: `autofix_changes_lost_head_retry_consumed`, its positional arguments, `REVIEWED_HEAD_COMMIT_EPOCH`, and the `AUTOFIX_CHANGES_LOST_BUDGET` / `AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED` log lines keep their names and field sets. The fifth argument stays accepted and is ignored (AD-2). `reason=missing_head_time` keeps its name.
- §5: only the bound changes; the branch count, the PR-named count, `reason=unnamed_dispatch_run`, and the fail-closed paths are untouched.
- §9: tabs in `gh_helpers.sh` and the Python tests; the step script keeps its existing 2-space indentation.
- §15: no new call. The helper's API-call doc block stays accurate.
- §27: `.github/workflows/review_autofix.yml` is not edited.
- §1 (security first): the change only removes an author-controlled input. Every new failure mode fails closed (no dispatch), never open (no loop).

## Approach

In the `push_bound` jq program, drop the `($commit_epoch | …)` term and the `--arg commit_epoch` binding, so the bound is the minimum `created_at` of the runs on the head in the branch page. With none, it is empty, and the existing `missing_head_time` branch fails closed. The helper still reads `$5` into `head_commit_epoch` so callers that pass it keep working; the header documents that it is accepted and not used (AD-2).

In normal use a commit is made before it is pushed, so the committer time was the smaller term. Dropping it moves the bound later, to the head's first run. A PR-named run created between the commit and the push can only review an earlier head, so excluding it is more correct, not less. The loop bound still holds: the run that dispatched the retry reviewed this head, so it was created after the push, and the retry counts it.

Alternatives: see AD-1.

## Phases & Merge Strategy

This is a single-phase plan: issue mode (CLAUDE.md §28.A) authorises it, and the fix is one helper, its caller's comment, its tests, and docs.

1. **Phase 1 — trusted push-time bound.** Files: `scripts/gh_helpers.sh`, `scripts/review_autofix_step_changes_lost_redispatch.sh` (comment only), `tests/test_retrigger_default_branch_dispatch.py`, `agents.md`, `changelog.d/5523-retry-budget-trusted-push-bound.md` [new].
   - Done when: the new backdated-commit regression test fails on the base branch and passes with the change; `tests/test_retrigger_default_branch_dispatch.py`, `tests/test_editor_changes_lost_redispatch_budget.py`, `tests/test_gh_helpers_list_runs_method.py`, and the log-prefix and step-script contract tests pass; `bash -n scripts/gh_helpers.sh` is clean.
   - Rollback: revert the phase PR. The helper then uses the commit time again, which is fail-closed as well.

## Implementation Steps

1. `scripts/gh_helpers.sh`, `autofix_changes_lost_head_retry_consumed` (lines 1413–1614): remove the commit-time term and `--arg commit_epoch` from the `push_bound` jq program. Update the header's `$5` entry, the "PR-named runs" paragraph, and the in-body comment: the bound is the earliest GitHub-recorded run on the head SHA, a committer time is author-controlled and never used (issue #5523), and no such run means `missing_head_time`.
2. `scripts/review_autofix_step_changes_lost_redispatch.sh` lines 40–46: correct the comment that says the commit time is an input to the bound. Code unchanged (AD-2).
3. `tests/test_retrigger_default_branch_dispatch.py`:
   - add `test_budget_ignores_a_backdated_commit` (a head with a committer date a day before its first run, and a completed PR-named run of an earlier head in between: budget available);
   - replace `test_budget_uses_the_commit_time_when_no_branch_run_is_on_the_head` with `test_budget_fails_closed_when_only_the_commit_time_is_known` (AD-3);
   - give `test_budget_fails_closed_when_the_pr_named_call_fails` a branch run on the head, so it still reaches the PR-named call.
4. `agents.md` line 2084: describe the bound as the first run on the head SHA, and note that the commit time is not used.
5. `changelog.d/5523-retry-budget-trusted-push-bound.md` [new]: a `security` entry per §20.

## Files & Modules

- `scripts/gh_helpers.sh`
- `scripts/review_autofix_step_changes_lost_redispatch.sh`
- `tests/test_retrigger_default_branch_dispatch.py`
- `tests/test_editor_changes_lost_redispatch_budget.py` (fixture runs gain `created_at`; see Notes)
- `tests/test_gh_helpers_list_runs_method.py` (fixture gains the head's cancelled twin; see Notes)
- `agents.md`
- `changelog.d/5523-retry-budget-trusted-push-bound.md` [new]

## Tests

- Unit (bash-under-pytest, mocked `gh`): the new backdated-commit test, the replaced fail-closed test, and every existing budget test in `tests/test_retrigger_default_branch_dispatch.py` and `tests/test_editor_changes_lost_redispatch_budget.py`.
- Contract: `tests/test_gh_helpers_list_runs_method.py` (`-X GET` on every list call), `tests/test_log_prefix_regressions.sh` (log line field sets), and the review_autofix step-script registry tests.
- End-to-end: runtime validation of the project branch (`/implement-plan-claude` step 10).

## Risks & Mitigations

- The head's first run falls off the 30-run branch page, so the earliest visible run on the head is later than the push — ACCEPTED: every run a push creates is on the head, so the push's runs would need 30 newer runs on the same branch before the retry check. The result is at most one extra retry, never an unbounded loop, because each retry run counts the one before it.
- No run on the head at all (a push that triggers no workflow on the branch) — the budget now fails closed where the commit time used to supply a bound. This means no automated retry and the existing terminal blocked comment. ACCEPTED: failing closed is the helper's documented contract.

## Rollout

Ships to consumer repos with the next `@stable` sync of `scripts/gh_helpers.sh`, through #4898's project branch and then #4701's. No flag and no env var. Rollback: revert the phase PR.

## References

- #5523 (this finding), #3576 (security audit tracker), #4898 (the PR-named budget extension, its AD-4), #4701 / #4618 (default-branch review dispatches).

## Auto-decisions

- AD-1 [plan, 2026-09-30] What replaces the author-controlled commit time in the changes-lost push-time bound? — Picked: A — only GitHub-recorded `created_at` of runs on the head SHA from the existing branch page; with none, fail closed (`missing_head_time`). Alternatives: B — a trusted push time from the PR timeline / repo events API (one more REST call per probe, and timeline `committed` events carry the same author-set dates); C — bind PR-named runs to the reviewed head through a new dispatch input or run-name field (changes pinned run names, the poller matcher, and consumer wrappers). Why: A removes the untrusted input with no new API call and fails closed; B and C are larger and add calls or cross-repo contract changes. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What happens to the helper's fifth argument and the step's `REVIEWED_HEAD_COMMIT_EPOCH`? — Picked: A — keep both; the helper accepts `$5` and ignores it, and the header says so. Alternatives: B — remove the argument and the variable; C — keep the argument but stop computing it in the step. Why: §6 forbids removing identifiers without a reviewed ask, and an ignored argument keeps every existing caller working. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] `test_budget_uses_the_commit_time_when_no_branch_run_is_on_the_head` pins the removed behaviour; what happens to it? — Picked: A — replace it with `test_budget_fails_closed_when_only_the_commit_time_is_known`. Alternatives: B — keep the name and change its assertions. Why: a test name that describes removed behaviour would mislead readers (§12.B stale docs); test names are not referenced anywhere else. Applied in: phase 1 PR. Status: pending review

## Notes

- Plan deviation (phase 1): `tests/test_editor_changes_lost_redispatch_budget.py` and `tests/test_gh_helpers_list_runs_method.py` built runs without `created_at` (the REST API always returns it) and, in two tests, with no run on the head. They passed only because the commit time supplied the bound. Their fixtures now carry `created_at` and the head's cancelled `pull_request` twin; every assertion is unchanged.
- `security_pass_skip.py` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}` (2026-09-30).
