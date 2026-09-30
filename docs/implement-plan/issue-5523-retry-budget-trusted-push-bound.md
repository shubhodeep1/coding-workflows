# Implement-Plan Log — Bound the changes-lost retry budget by GitHub-recorded run times, not the head commit's committer time

- Plan: docs/completed/issue-5523-retry-budget-trusted-push-bound-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5523
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5523-retry-budget-trusted-push-bound   Final PR: #5541 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4898-retrigger-dispatch-default-branch)
- Waiting on: completion PR (this log's PR; number in the stage report and resume block)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01S4vC2eBBTmdMrfvawqbKGh (reused)   safety net and hand-back in the conformance 1/3 stage report
- Last updated: 2026-09-30
- Last note: conformance 1/3 CONFORMANT with no fix PR; security skipped (plan header); validation skipped (operator standing rule, stacked two levels below main); completion PR opened

## Phases
1. [x] Phase 1 — trusted push-time bound for the changes-lost retry budget (`scripts/gh_helpers.sh`, `scripts/review_autofix_step_changes_lost_redispatch.sh`, `tests/test_retrigger_default_branch_dispatch.py`, `agents.md`, `changelog.d/5523-retry-budget-trusted-push-bound.md`)
   - [x] `push_bound` drops the committer-time term; only `created_at` of branch runs on the head SHA (`scripts/gh_helpers.sh` push_bound jq)
   - [x] no run on the head → `reason=missing_head_time`, fail closed (`test_budget_fails_closed_when_only_the_commit_time_is_known`)
   - [x] `$5` / `REVIEWED_HEAD_COMMIT_EPOCH` kept, accepted and ignored (AD-2)
   - [x] regression test: backdated commit no longer suppresses the retry (`test_budget_ignores_a_backdated_commit`: failed on base, passes after)
   - [x] replaced commit-time fallback test (AD-3); PR-named-failure test reaches the call
   - [x] `agents.md`, helper header, step comment updated; `changelog.d/5523-retry-budget-trusted-push-bound.md` (security)
   - Done: listed test suites and `bash -n scripts/gh_helpers.sh` pass
   - PR #5573 merged 2026-09-30; review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT — no fixes (pre-security). Implemented: COMPLETE (every plan goal maps to #5573: `scripts/gh_helpers.sh:1570-1585` bound from head runs only, `:1583` `missing_head_time`, `:1507` `$5` kept; `scripts/review_autofix_step_changes_lost_redispatch.sh:41-52`; `agents.md:2084`; `changelog.d/5523-retry-budget-trusted-push-bound.md`). Correctness: CONCERNS, one HYPOTHESIS concern, not fixed (see Notes). Checks: `test_budget_ignores_a_backdated_commit` and `test_budget_fails_closed_when_only_the_commit_time_is_known` fail against the pre-#5573 helper and pass now; pytest 78/78 (`test_retrigger_default_branch_dispatch.py`, `test_editor_changes_lost_redispatch_budget.py`, `test_gh_helpers_list_runs_method.py`, `test_smoke_review_dispatch.py`, `test_test_and_mark_stable_phantom_review_run_filter.py`, `test_workflow_file_size_limit.py`) and 46/46 (every test referencing the helper or step, including `tests/review_autofix_step_scripts.py`); `tests/test_log_prefix_regressions.sh` and `bash -n scripts/gh_helpers.sh` pass.

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` per the plan header (`security_pass_skip.py`, 2026-09-30)

## Validation
- Skipped (target_ref not authorizable: final PR #5541 is stacked two levels below main; covered by the base chain's validation) — operator standing rule for projects stacked two or more levels below `main`, stated with the answer Q1: A on #5093 (comment 5904738992, OWNER, 2026-09-30). `validate.yml` ("Authorize explicit validation target") authorizes `target_ref` only for an open final PR into `main`, or into a project branch whose own open PR goes into `main`. #5541 targets #4898's project branch, whose PR #4923 targets #4701's project branch, whose PR #4709 targets `main`, so a dispatch would stop before validating. Not dispatched.

## Completion
- Completion PR (claude/implement-plan-issue-5523-retry-budget-trusted-push-bound-complete) open 2026-09-30 — doc moved to docs/completed/issue-5523-retry-budget-trusted-push-bound-plan.md
- Final PR #5541 draft (into claude/implement-plan-issue-4898-retrigger-dispatch-default-branch); marked ready at final-merge 1/1

## Activation
- n/a: the base is #4898's project branch, so the change goes live with that chain's final PRs (#4923, then #4709). The final-merge stage closes issue 5523 and labels it `ai:merged` once #5541 merges.

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
- 2026-09-30 (conformance 1/3, session_01H9By8UdDCktfy2QecbqnEu): base not moved (#4923 still open into #4701's project branch). Project branch synced with the base as `5e36025`: the `agents.md` conflict kept the base's rewritten retrigger bullet (E2E dispatches no longer keep `--ref`, #5093) and this project's push-time bound sentence (#5523).
- Conformance run 1, HYPOTHESIS concern, not fixed: the bound is the earliest run on the head SHA anywhere in the branch's newest 30 runs, so a head that was pushed before, replaced, and later force-pushed back gets the bound of its first push, and completed PR-named reviews of the heads in between count toward its budget (fail closed: the retry is skipped). The plan chose this bound (AD-1); only the PR's own pusher can cause it, and only while those earlier runs are still on the 30-run page. Changing it (for example to the latest run on the head) is a design tradeoff, not a demonstrated defect; listed for the final PR's review.
