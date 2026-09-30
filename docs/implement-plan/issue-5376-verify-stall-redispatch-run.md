# Implement-Plan Log — Decide a stalled review's re-dispatch from a verified workflow run, not from a claim

- Plan: docs/completed/issue-5376-verify-stall-redispatch-run-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5376 (https://github.com/shubhodeep1/coding-workflows/issues/5376)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4985-skip-marker-review-stall
- Project branch: claude/implement-plan-issue-5376-verify-stall-redispatch-run   Final PR: #5387 draft (into claude/implement-plan-issue-4985-skip-marker-review-stall)
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4985-skip-marker-review-stall)
- Waiting on: the completion PR from claude/implement-plan-issue-5376-verify-stall-redispatch-run-complete
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01Nr1eDxdAgRNU9mhy74VqNH   safety net and hand-back: see the completion stage report
- Last updated: 2026-09-30
- Last note: completion stage (session_01XHXrJt1PB3KymEh8jqaWu2): validation cycle 1 passed (run 36688108776, 10/10 tests, on project head `7d1f95a`); plan moved to docs/completed/. Conformance run 1 CONFORMANT; security skipped (plan header). Next: final-merge 1/1 marks #5387 ready and closes #5376 with `ai:merged` once it merges.

## Phases
1. [x] Phase 1 — verified-run re-dispatch rule — PR #5408 merged 2026-09-30 into the project branch as `7d1f95a` (human merge per issue #5376 blocker Q1: A, bound to head `b4945b0`); twin sync `e18fa4a`; review rounds: 2 (round 2 on `b4945b0`: 1 consensus finding rejected again, blocked for lack of a verdict bot, resumed by /reclarify 2026-09-30); interventions: 0; protected paths: `.claude/scripts/check_in_status.py`, `.claude/commands/fix-claude-pr.md` (edited through their `workflow-templates/.claude/` twins)
   - checker twin: `_dispatched_review_runs`, `_head_arrival_time`, `_verified_review_redispatch`; `stall_redispatched` from a verified run; `_active_run_count` shares the listing
   - consumer wrapper: `workflow-templates/ai-review.yml` dispatch-only `run-name`
   - fixer twin: `fix-claude-pr.md` step 3 wording
   - tests: `tests/test_check_in_status_hand_back.py` (loads the twin)
   - docs: `agents.md`, `README.md`, `changelog.d/5376-verify-stall-redispatch-run.md`
   - Done: new and changed tests pass against the twin; ruff and yamllint clean; only twin-parity tests red until `[claude-twin-sync]`

## Conformance
- Run 1 — 2026-09-30: CONFORMANT — no fixes (pre-security; session_01QyrbnXXgomug7hUsRnrLSZ)

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py`: "ai:security: created and labelled by the issue automation")

## Validation
- Cycle 1 — run 36688108776 2026-09-30 (target_ref: claude/implement-plan-issue-5376-verify-stall-redispatch-run, pinned head `7d1f95a`): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 288s); no fix issues

## Completion
- Completion PR from claude/implement-plan-issue-5376-verify-stall-redispatch-run-complete (open) — doc moved to docs/completed/issue-5376-verify-stall-redispatch-run-plan.md
- Final PR #5387 draft

## Activation
- n/a: the base is claude/implement-plan-issue-4985-skip-marker-review-stall (#5031), not the default branch; this change goes live with that project.

## Auto-decisions
- AD-1 [plan, 2026-09-30] What proves a stalled head's review was already re-dispatched? — Picked: A — a completed, not-cancelled `workflow_dispatch` review run on the default branch, titled for this PR, created at or after the head arrived. Alternatives: B — keep the claim and also require the run; C — any claim by the workflow account only. Why: the issue's recommendation; default-branch dispatched runs cannot be forged by a PR author. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] When did the head arrive? — Picked: A — the later of the committer date and the earliest check-run `started_at`. Alternatives: B — committer date only; C — earliest check-run start only. Why: backdating cannot pull an older run in. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Which run conclusions count? — Picked: A — every completed conclusion except `cancelled`. Alternatives: B — `success` only; C — all. Why: a cancelled run reviewed nothing; others would recur. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] How do consumer repos bind a dispatched run to the PR? — Picked: A — dispatch-only `run-name` `AI Review [pr:<N>]` in `workflow-templates/ai-review.yml`, with a 404 fallback to its listing. Alternatives: B — no binding, always retry; C — keep the claim rule there. Why: B loops per lease, C keeps the vulnerability. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Should the active-run count see consumer dispatched runs? — Picked: A — yes, through the same listing helper. Alternatives: B — `internal-review.yml` only. Why: a running consumer re-dispatch would otherwise be re-dispatched again. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-30] Which copy do the hand-back tests load? — Picked: A — the twin. Alternatives: B — `.claude/`, red until sync. Why: the twin-first rule. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-30] Change the `CLAUDE.md` §26.H wording? — Picked: A — no, it stays true. Alternatives: B — spell out the run rule there. Why: §5. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Issue progress comment: 5903022362.
- Invoking session: session_01GokLJ6bAqCLubeWsfg7Zch (permission mode auto).
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
- 2026-09-30, phase 1 (session_01GokLJ6bAqCLubeWsfg7Zch): the new hand-back tests (loading the twin) pass: 131 passed, with only the 2 twin-parity checks failing. Related suites (71 files): 2502 passed, 1 skipped. The 5 failures are 4 twin-parity checks and 1 missing `gawk`; the validation-template renderer suites need `jsonschema` and `jinja2`, which are not installed here. With the twins copied into `.claude/` (simulated sync), the 8 affected suites pass (427). ruff and yamllint are clean.
- 2026-09-30, `/reclarify` resume (session_01DYKF4onPHPSJafUNdeYYH5): the operator answered A on #5376. `[claude-twin-sync]` `e18fa4a` copies both twins; `.claude/` and twin sha256 match the blocker (`66e9b051…3aaf`, `736d160b…dc81`). The project branch took 4 base commits in a clean merge (`162ccb8`), and #5408 still merges cleanly on it. On that merge: 972 passed, 1 skipped across the 19 suites that reference the changed files, and 857 passed, 1 skipped in the 13 twin-parity suites. ruff (`--select E,F --ignore E501`) and yamllint are clean.
- 2026-09-30 phase 1/1 review round 1 (PR #5408, head `83be7a0`, run 36667727788), session session_01W8L9mx6L8Xiunj2hwLmY9n: ledger of 7 entries, 3 consensus findings from `google_gemini-3_1-flash-lite` (one shared with `deepseek_deepseek-v4-pro`); the other 4 reviewers reported nothing. Rejected all three: (1) "`_dispatched_review_runs()` falls through to the consumer workflow on any non-404 error" misreads `check_in_status.py:365-371`, which `continue`s only on `HTTP 404` and re-raises every other `ReadError` (checked: an `HTTP 502` read raises after 1 call); (2) "`_head_arrival_time()` passes a `None` `started_at` to `_parse_time`" is handled: `_parse_time` (`:301-304`, unchanged by this PR) raises `ValueError` for a non-string, which the loop catches (checked: runs with `started_at: None`, no `started_at`, and a non-dict entry are skipped); (3) `ignore_claim_by` stays on `_review_stall_verdict` because removing a parameter is a §6 breaking change, as its docstring and both reviewers say. No failing check on the head. No verdict bot is configured, so no verdict was posted; the round's push merges the project branch (5 commits: the issue-4985 base sync `162ccb8`, clean) into the PR head as `[claude-merge-resolve]`, plus this log update.
- Use `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1` (never empty) for this project's checker calls.
