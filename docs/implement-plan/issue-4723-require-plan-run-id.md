# Implement-Plan Log — Require a captured Plan run ID before the release smoke test reports Plan success

- Plan: docs/plans/issue-4723-require-plan-run-id-plan.md
- Source issue: shubhodeep1/coding-workflows#4723   Base branch: stable
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4723-require-plan-run-id   Final PR: #4729 draft
- Status: IN_PROGRESS
- Stage: conformance 2/3
- Activation: not started
- Waiting on: conformance fix PR from `claude/implement-plan-issue-4723-require-plan-run-id-conformance-fix-2` (the PR carrying this log update)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01SN9XdEWdK9hLtuWFjcG31M (hand-back and safety net re-armed each stage)
- Last updated: 2026-09-29
- Last note: conformance 2/3, review round 2 on #5035 (head 41fc6fb): 1 of 2 findings fixed (changelog contributor note now lists the empty-title test), 1 rejected (a whitespace-only `ISSUE_TITLE` cannot occur: `create-issue` builds the title as a fixed non-blank literal, and the guard already rejects an empty output)

## Phases
1. [x] Phase 1 — paginate the scoped Plan run lookup and require the ID before success (`.github/workflows/test-and-mark-stable.yml` `wait-plan` step, `tests/test_test_and_mark_stable_plan_polling_guard.py`, `changelog.d/4723-require-plan-run-id.md`)
   - PR #4730 merged 2026-09-28 (83cb4b3); review rounds: 3; interventions: 0
   - Done when: new behavioural tests (missing ID fails at capture with `status=run_id_missing`; page-2 match succeeds; unrelated title rejected) and existing guard tests pass; workflow YAML parses.

## Conformance
- Run 1 — 2026-09-28: CONFORMANT (Implemented COMPLETE, Correctness CONCERNS) — fix PR #4875 (pre-security): a failed or malformed runs page made `latest_scoped_run_field` end the walk as a short page (exit 0) instead of returning 1; no behaviour change for its callers, which retry on both. Review round 1 (2026-09-29): 2 NITs rejected with evidence, 0 fixed; the round could not converge without a verdict bot (`CLAUDE_FIXER_VERDICT_BOT_LOGIN` unset), so the project stopped at `Status: BLOCKED` (issue comment 5882150855). The maintainer answered Q1: A and merged #4875 into the project branch as `6d682f6` on 2026-09-29.
- Run 2 — 2026-09-29: CONFORMANT (Implemented COMPLETE, Correctness CONCERNS) — fix PR from `claude/implement-plan-issue-4723-require-plan-run-id-conformance-fix-2` (pre-security): `wait-clarify` and `wait-implement` captured run IDs from one 100-run page with no issue-title filter and wrote `status=success` without an ID; run 36504041362's Implement run sat at index 137 of the window (`Implement: run ID not found`), and the alt-model job's newer runs were in the same window. Fixed per AD-8. Review round 1 (2026-09-29, head e286b24): 1 of 5 findings fixed (empty-title guard, AD-9), 4 rejected (AD-10 for the refactor). Review round 2 (2026-09-29, head 41fc6fb): 1 of 2 findings fixed (changelog test list), 1 rejected (whitespace-only title is unreachable).

## Security pass
- Skipped (ai:workflow-heal: automation-produced issue, verified by `.claude/scripts/security_pass_skip.py`)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Scope: fix only `wait-plan`, or also Clarify/Implement run-ID capture? — Picked: A — Plan only, as the issue specifies. Alternatives: B — also harden `wait-clarify` and `wait-implement`. Why: §5 minimal change; the evidence implicates only Plan. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How to narrow or paginate the lookup? — Picked: A — bounded page walk (≤10 pages) of the existing scoped query, stopping at the first page with a match or a short page. Alternatives: B — per-workflow run list; C — keep one page and only fail earlier. Why: B needs a repo-specific workflow file and still overflows on skipped `issue_comment` runs; C turns one false block into another. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] What does `wait-plan` report when no ID is found? — Picked: A — new status value `run_id_missing`, `::error::`, exit 1. Alternatives: B — reuse `status=plan_failed`. Why: the gate prints the status verbatim, so a distinct value names the cause. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Make the page cap configurable? — Picked: A — step-local constant `PLAN_RUN_LOOKUP_MAX_PAGES=10`. Alternatives: B — a new env var defaulting to 10. Why: §5; 10 pages is GitHub's 1,000-result ceiling for this list. Applied in: phase 1 PR. Status: pending review
- AD-5 [phase 1/1, 2026-09-28] Should the 10-second status poll page through runs too? — Picked: A — no, the poll keeps reading one page (`latest_scoped_run_field "Plan" "status" 1`), and only the run-ID capture at the success exits pages. Alternatives: B — the poll pages like the capture (up to 10 calls per poll while no run matches, about 3,600 calls an hour); C — the poll reads 2 pages. Why: §15, since the poll is an activity signal and its per-poll cost stays one call as before. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1 — review round 2, 2026-09-28] Should the "other active Plan runs" check in `wait-plan` page past the first 100 runs, although the plan listed it as a Non-goal? — Picked: A — yes: when page 1 finds no active run, walk later pages of the same query with the same shape guard and `PLAN_RUN_LOOKUP_MAX_PAGES` cap, stopping at the first page with an active run or a short page. Alternatives: B — reject the review finding as out of scope. Why: an older, still-active real Plan run behind 100 newer runs is the #4723 false release block in the same step; the branch is rare (Plan completed without labels), so the extra reads stay bounded. Applied in: phase 1 PR (review round 2). Status: pending review
- AD-7 [phase 1/1 — review round 3, 2026-09-28] How should the "unable to confirm concurrent Plan runs" retries in `wait-plan` be bounded, since they `continue` before the loop's inactivity check? — Picked: A — apply the existing inactivity limit inside both retries (page 1 and later pages): once no activity was seen for `PLAN_PHASE_TIMEOUT` minutes, fail with `status=timeout`. Alternatives: B — a new consecutive-retry counter; C — add exponential backoff around the retry. Why: §5, it reuses the step's own stall rule, and `gh_api_safe` already backs off on rate limits; the page-1 retry had the same gap on the base branch. Applied in: phase 1 PR (review round 3). Status: pending review
- AD-8 [conformance 2/3, 2026-09-29] The 2026-09-29 occurrence on #4723 (run 36504041362) failed at `wait-implement`'s run-ID capture (one page, no issue-title filter), and `wait-clarify` has the same code. Should the conformance fix extend the #4723 fix to both sibling captures, although AD-1 scoped the project to Plan? — Picked: A — yes: both captures are issue-title scoped, walk at most 10 pages, and require a numeric ID before `status=success` (`run_id_missing`). Alternatives: B — `wait-implement` only; C — keep AD-1 and only record the finding. Why: the recorded occurrence falsifies AD-1's premise, and both siblings share the same failure mode and the same deep-verification symptom; without the title filter a paged lookup would take the parallel alt-model job's newer run. Applied in: conformance fix PR 2. Status: pending review
- AD-9 [conformance 2/3 — review round 1, 2026-09-29] Which status should `wait-clarify` and `wait-implement` write when `ISSUE_TITLE` is empty (the new up-front guard, mirroring `wait-plan`'s)? — Picked: A — the existing `run_id_missing`, with `::error::Missing issue title for <phase> run scoping`. Alternatives: B — a per-phase failure value (`clarify_failed`, new; `implement_failed`, which already means the Implement workflow failed). Why: §5 and §6, it adds no new status identifier and does not overload `implement_failed`; without a title the run ID cannot be captured, and the error line names the cause. Applied in: conformance fix PR 2 (#5035, review round 1). Status: pending review
- AD-10 [conformance 2/3 — review round 1, 2026-09-29] The reviewer panel asked to consolidate the run-ID capture copied across `wait-clarify`, `wait-plan`, and `wait-implement`. Refactor now? — Picked: A — no: keep the per-step copies and reject the finding as an out-of-scope refactor. Alternatives: B — move one paged, title-scoped lookup into the already-sourced `scripts/comprehensive_test_and_release_gh_api.sh` and call it from all three steps. Why: §5 and §12.C, the copies behave the same and are each covered by behavioural tests, while B rewrites `wait-plan`'s capture (return codes 1/2, the one-page poll cap, the other-active walk) that already passed three review rounds, in a release gate. Applied in: no code change. Status: pending review

## Lessons
- [source:plan-deviation] A poll loop that shares a lookup helper with a one-shot capture must not inherit the capture's pagination: pass a page cap so the per-poll API cost stays one call (files: .github/workflows/test-and-mark-stable.yml)
- [source:intervention] A retry loop around a paginated lookup must not retry a walk that already read every page up to the cap: the result cannot change and each retry repeats the full page cost (files: .github/workflows/test-and-mark-stable.yml)
- [source:intervention] When a lookup is fixed to page past the first 100 runs, every other check in the same step that reads the same run window needs the same paging, or the fix leaves the same false failure one branch away (files: .github/workflows/test-and-mark-stable.yml)
- [source:intervention] A retry branch that `continue`s a poll loop before the loop's inactivity-timeout check must apply that timeout itself, or a persistent read failure polls until the job's timeout-minutes (files: .github/workflows/test-and-mark-stable.yml)
- [source:conformance] Capturing `gh_api_safe_print` with `$(...)` also captures its `::error::gh api call failed` line, so a failed read is non-empty text, not an empty string: shape-check the JSON before trusting it, never just test for emptiness (files: .github/workflows/test-and-mark-stable.yml, scripts/comprehensive_test_and_release_gh_api.sh)
- [source:conformance] When a fix hardens one copy of a pattern that several workflow steps duplicate (per-step `capture_run_id`), check every sibling copy, and re-read a heal issue's occurrence comments before closing the scope: a later occurrence can fail on a sibling the plan left out (files: .github/workflows/test-and-mark-stable.yml)
- [source:conformance] A run-ID lookup over the repo-wide `actions/runs` list must scope by `display_title` as well as by workflow name, or a parallel job's newer run of the same workflow is taken as ours (files: .github/workflows/test-and-mark-stable.yml)
- [source:intervention] When a lookup starts filtering on an input (`display_title == $ISSUE_TITLE`), guard that input the way sibling steps do: an empty filter value can match a run instead of failing (files: .github/workflows/test-and-mark-stable.yml)

## Notes
- 2026-09-29: the project stopped at `Status: BLOCKED` in conformance 1/3, review round 1 on #4875 (no verdict bot configured). Resumed by session_01KWw6TgxasoCBTJ7xDtAJcy after the maintainer's Q1: A answer and `/reclarify`; project branch already current with `stable`.
- Until `CLAUDE_FIXER_VERDICT_BOT_LOGIN` names a dedicated verdict bot, a review round that finds only invalid findings cannot converge and stops the project the same way.
- Issue progress comment: 5864145856.
- Invoking session: session_01JsFVWAjMCmc5aojW1k4Am2 (started by the Claude issue dispatcher).
