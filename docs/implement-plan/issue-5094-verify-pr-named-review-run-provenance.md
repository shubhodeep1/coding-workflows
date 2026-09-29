# Implement-Plan Log — Verify workflow identity and default-branch provenance before the poller trusts a PR-named review run

- Plan: docs/completed/issue-5094-verify-pr-named-review-run-provenance-plan.md (moved from docs/plans/ in the completion PR)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Source issue: shubhodeep1/coding-workflows#5094   Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5094-verify-pr-named-review-run-provenance   Final PR: #5106 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4898-retrigger-dispatch-default-branch)
- Waiting on: completion PR (the number is in the validation 1/3 resume stage report and on the #5094 progress comment)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01EbmsjwwSRH1HDRVfTqiAxq (reused)   safety net and hand-back in the validation 1/3 resume stage report
- Last updated: 2026-09-29
- Last note: validation skipped on operator answer Q2: A (covered by the base chain's validation); plan moved to docs/completed/ in the completion PR; next: final-merge 1/1.

## Phases
1. [x] Phase 1 — verify identity and provenance for every PR-named match in the poller   — PR #5109 merged 2026-09-29 (a7e0c0b); review rounds: 1 (all findings rejected); interventions: 0 (merged by the operator after Q1: A)

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-validation; security skipped). Correctness: PASS. All four PR-named match sites in `scripts/orchestrate_poll_process.sh` require `event == workflow_dispatch`, `head_branch ==` the resolved default branch, and the exact title paired with its wrapper `path`, and fail closed when the default branch is unresolved.

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header (`security_pass_skip.py` verified).

## Validation
- Cycle 1 — run 36568025757 2026-09-29 (target_ref: project branch): conclusion `failure` at *Authorize explicit validation target* (`target PR binding is missing or ambiguous`), no verdict.
- Skipped (target_ref not authorizable: final PR #5106 is stacked two levels below main; covered by the base chain's validation) — operator answer Q2: A on #5094, 2026-09-29. `validate.yml` authorizes `target_ref` only for an open final PR into `main`; #5106 targets #4898's project branch, whose final PR #4923 targets #4701's project branch, whose final PR #4709 targets `main` (#4734 would cover one level only). The base projects re-run their security audit and runtime validation on branches that contain this fix before anything reaches `main`.

## Completion
- Completion PR (claude/implement-plan-issue-5094-verify-pr-named-review-run-provenance-complete) open 2026-09-29 — doc moved to docs/completed/issue-5094-verify-pr-named-review-run-provenance-plan.md
- Final PR #5106 draft (into claude/implement-plan-issue-4898-retrigger-dispatch-default-branch)

## Activation
- n/a: the base is #4898's project branch, so the change goes live with that chain's final PRs (#4923, then #4709). The final-merge stage closes #5094 and labels it `ai:merged` once #5106 merges.

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which PR-named matchers does this issue fix? — Picked: A — every PR-named match in `scripts/orchestrate_poll_process.sh` (the helper and the three cached-blob copies). Alternatives: B — only `_pr_named_review_dispatch_runs`; C — also `gh_helpers.sh`, `review_merge_train.sh`, `review_autofix_sweep.yml`, `.claude/scripts/check_in_status.py`. Why: the finding is the poller's trust decision; §1 security first; C reaches other flows and a protected path. Applied in: PR #5109. Status: pending review
- AD-2 [plan, 2026-09-29] How does the helper get the workflow identity? — Picked: A — one REST `actions/runs?event=workflow_dispatch&branch=<default>&per_page=100` call exposing `path` and `head_branch`. Alternatives: B — `gh run list` + `workflowName`; C — `gh run list` + an extra `actions/workflows` call. Why: `path` is the identity at no extra call (§15). Applied in: PR #5109. Status: pending review
- AD-3 [plan, 2026-09-29] What happens when the default branch cannot be resolved? — Picked: A — PR-named matching yields nothing (pre-#4701 behaviour), with a warning log line. Alternatives: B — assume `main`; C — skip the provenance check. Why: B and C re-open the spoofing path. Applied in: PR #5109. Status: pending review
- AD-4 [validation 1/3 — resume, 2026-09-29] The log on the project branch still read `Status: IN_PROGRESS` / `Stage: phase 1/1` / `Check-in: none` (the conformance and validation stages opened no PR, so their log updates were never committed), while the #5094 blocked comment, the idle project checker, and the operator's `/reclarify` showed the project stopped at validation 1/3. Resume from the state the blocked comment carries, or restart from the committed log? — Picked: A — resume at validation 1/3 with the operator's Q2 answer, taking the uncommitted state from the blocked comment (5891275022). Alternatives: B — restart from `phase 1/1` as the committed log says. Why: PR #5109 is merged and conformance run 1 is recorded on the issue, so B would redo finished work and spend a second run of the shared conformance cap. Applied in: no code change. Status: pending review

## Lessons
- [source:plan-deviation] A shell variable that the script fills with a guessed fallback (`DEFAULT_BRANCH=... || echo "main"`) must not feed a security check; resolve the value separately and fail closed when it is unknown. (files: scripts/orchestrate_poll_process.sh)
- [source:plan-deviation] An issue-mode project stacked more than one level below the default branch cannot run runtime validation, even after #4734; ask once and record the base chain's validation as the cover instead of dispatching a run that is bound to fail. (files: .github/workflows/validate.yml, .claude/commands/implement-plan-claude.md)

## Notes
- Issue mode (CLAUDE.md §28.A): the permission mode is `auto`; start-up checks were auto-decided.
- This session had no `mcp__github__*` tools, so GitHub writes go through `gh api` (REST) routine writes (§23.B/§23.H).
- Stale Routine sweep: one ended Routine (`trig_01LNLrMkYu8BdNKWa5PZNEYS`, `implement-plan issue-4835: check-in`) could not be deleted (Auto-mode classifier denial); it is left for the owner.
- Plan deviation (phase 1): the plan first said `_pr_named_review_default_branch` would reuse `DEFAULT_BRANCH` when set. The poller sets that variable with a `main` fallback, so the helper resolves the branch itself; the plan text was updated in the phase PR.
- 2026-09-29 (phase 1/1 — review round 1): every reviewer-panel finding on head `98c62c6` was rejected (reasons on PR #5109). An all-rejected round converges only through the dedicated verdict bot, and `CLAUDE_FIXER_VERDICT_BOT_LOGIN` is not configured, so the stage stopped and asked Q1 on #5094 (comment 5889363578). Operator answer Q1: A (comment 5889835374): the operator merged PR #5109 into the project branch by hand. Findings 4 and 5 (same-class matchers outside the poller) are filed as #5152, out of scope under AD-1.
- 2026-09-29 (conformance 1/3, session_01VN1G7GVYrrw4wybuR9nsGf): CONFORMANT with no fix PR. Checks: `bash -n` passed; `tests/test_review_dispatch_default_branch.py` 34/34; `tests/test_conflict_dispatch_active_run_visibility.py` 17/17; `tests/test_orchestrate_poll_process.py` 452/452. `shellcheck --severity=error` on the whole file could not run (OOM-killed at about 13 GB); the new functions checked alone are clean at warning level.
- 2026-09-29 (validation 1/3, same session): run 36568025757 refused at target authorization (final PR #5106 not into `main`). Asked Q2 on #5094 (comment 5891275022, `ai:claude-blocked`).
- 2026-09-29: operator answer Q2: A (comment 5893343789; skip validation here, covered by the base chain's validation; same answer as #4687 and #4688), then `/reclarify`.
- 2026-09-29 (validation 1/3 — resume, session_01Gq5MCUxfXBowp2fcMZANcv): started by the Claude issue dispatcher routine; the repo was attached with add_repo, so the session-start hook ran by hand to install `gh`. The base branch has not merged (#4923 open), so no base move; the project branch already contained it (head a7e0c0b). Removed `ai:claude-blocked`. Re-ran the checks on that head (results in the completion PR body).
