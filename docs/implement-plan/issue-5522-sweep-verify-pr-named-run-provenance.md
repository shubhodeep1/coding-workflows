# Implement-Plan Log — Verify default-branch provenance before the review sweep and the retrigger helpers trust a PR-named review run

- Plan: docs/plans/issue-5522-sweep-verify-pr-named-run-provenance-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Source issue: shubhodeep1/coding-workflows#5522   Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5522-sweep-verify-pr-named-run-provenance   Final PR: #5546 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (its number is on the #5522 progress comment and in the phase 1/1 stage report)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 implemented and verified (sweep + `_autofix_pr_named_review_runs` provenance checks; 133 targeted tests pass, yamllint/actionlint/shellcheck clean); phase PR opened against the project branch.

## Phases
1. [ ] Phase 1 — verify default-branch provenance and wrapper identity for PR-named runs in the sweep and in `_autofix_pr_named_review_runs`   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header (`security_pass_skip.py` verified).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which PR-named run consumers does this issue fix? — Picked: A — the sweep and `_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh`. Alternatives: B — the sweep only; C — also `scripts/review_merge_train.sh` and `.claude/scripts/check_in_status.py`. Why: the recommendation covers the other run-name consumers; the merge train (#5524) and the check-in script (#5521) have their own projects, and the check-in script is a protected path. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What happens to a PR-named run whose `head_branch` is null, missing, or empty (the #4928 case)? — Picked: A — it no longer counts under `pr:<N>` and is dropped. Alternatives: B — keep counting it by name; C — keep it only when its `head_sha` is in recent default-branch history (one extra read per tick). Why: the finding names the null-head path; a missing head branch cannot prove provenance, and a missed one costs a duplicate dispatch that the concurrency group absorbs. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Where does the sweep get the default branch? — Picked: A — `.base.repo.default_branch` from the open-PR listing it already fetches; no single value disables PR-named keys with a warning. Alternatives: B — `github.event.repository.default_branch`; C — one extra `GET repos/<repo>` per tick. Why: A costs no API call (§15); B is absent on `schedule` events. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] How does `_autofix_pr_named_review_runs` get the default branch, and what if it cannot? — Picked: A — one cached `GET repos/<repo>` per process; a failure returns 1 with no listing call, so each caller's existing failure rule applies. Alternatives: B — `GITHUB_EVENT_PATH`; C — assume `main`. Why: authoritative in every caller context and consistent with #5094; C lets a guess vouch for a run. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode: plan written by `/implement-issue-claude` for #5522; the base is #4898's project branch (final PR #4923, open), so the project ends at the final merge (`Activation: n/a`) and the final-merge stage closes #5522.
- Residual same-pattern matchers owned elsewhere: `scripts/review_merge_train.sh` → #5524; `.claude/scripts/check_in_status.py` `_active_run_count` → #5521.
