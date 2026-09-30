# Implement-Plan Log — Bound the changes-lost retry budget by GitHub-recorded run times, not the head commit's committer time

- Plan: docs/plans/issue-5523-retry-budget-trusted-push-bound-plan.md
- Source issue: shubhodeep1/coding-workflows#5523
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5523-retry-budget-trusted-push-bound   Final PR: #5541 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (this log's PR; number in the stage report and resume block)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 implemented and verified; phase PR opened, waiting on the review workflow

## Phases
1. [ ] Phase 1 — trusted push-time bound for the changes-lost retry budget (`scripts/gh_helpers.sh`, `scripts/review_autofix_step_changes_lost_redispatch.sh`, `tests/test_retrigger_default_branch_dispatch.py`, `agents.md`, `changelog.d/5523-retry-budget-trusted-push-bound.md`)
   - [x] `push_bound` drops the committer-time term; only `created_at` of branch runs on the head SHA (`scripts/gh_helpers.sh` push_bound jq)
   - [x] no run on the head → `reason=missing_head_time`, fail closed (`test_budget_fails_closed_when_only_the_commit_time_is_known`)
   - [x] `$5` / `REVIEWED_HEAD_COMMIT_EPOCH` kept, accepted and ignored (AD-2)
   - [x] regression test: backdated commit no longer suppresses the retry (`test_budget_ignores_a_backdated_commit`: failed on base, passes after)
   - [x] replaced commit-time fallback test (AD-3); PR-named-failure test reaches the call
   - [x] `agents.md`, helper header, step comment updated; `changelog.d/5523-retry-budget-trusted-push-bound.md` (security)
   - Done: listed test suites and `bash -n scripts/gh_helpers.sh` pass

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` per the plan header (`security_pass_skip.py`, 2026-09-30)

## Validation

## Completion

## Activation
- Base is not the default branch: `Activation: n/a (base claude/implement-plan-issue-4898-retrigger-dispatch-default-branch)` once the final PR merges.

## Auto-decisions
- AD-1 [plan, 2026-09-30] What replaces the author-controlled commit time in the changes-lost push-time bound? — Picked: A — only GitHub-recorded `created_at` of runs on the head SHA from the existing branch page; with none, fail closed (`missing_head_time`). Alternatives: B — a trusted push time from the PR timeline / repo events API; C — bind PR-named runs to the reviewed head through a new dispatch input or run-name field. Why: A removes the untrusted input with no new API call and fails closed. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What happens to the helper's fifth argument and the step's `REVIEWED_HEAD_COMMIT_EPOCH`? — Picked: A — keep both; `$5` is accepted and ignored. Alternatives: B — remove both; C — keep the argument but stop computing it. Why: §6 immutability; every caller keeps working. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What happens to `test_budget_uses_the_commit_time_when_no_branch_run_is_on_the_head`, which pins the removed behaviour? — Picked: A — replace it with `test_budget_fails_closed_when_only_the_commit_time_is_known`. Alternatives: B — keep the name, change its assertions. Why: a name describing removed behaviour misleads. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] When a shell helper's bound or filter reads a REST field (such as `created_at`), the test fixtures must carry that field as the real API returns it; fixtures that omit it can pass only through a fallback input and hide the dependency. (files: tests/test_editor_changes_lost_redispatch_budget.py, tests/test_gh_helpers_list_runs_method.py)

## Notes
- Phase 1 plan deviation: also edited the fixtures of `tests/test_editor_changes_lost_redispatch_budget.py` and `tests/test_gh_helpers_list_runs_method.py` (runs gain `created_at` and, where a test had no run on the head, the head's cancelled twin); assertions unchanged. Recorded in the plan's Notes.
- Local verification runs Python 3.11; `tests/test_workflow_retro.py` cannot be collected there (3.12 f-string syntax in the untouched `scripts/workflow_retro.py`), CI runs 3.12.
- Issue mode; progress comment https://github.com/shubhodeep1/coding-workflows/issues/5523#issuecomment-5906421374
- Base PR state at start: #4923 (head = base branch) open draft into `claude/implement-plan-issue-4701-review-dispatch-default-branch`.
