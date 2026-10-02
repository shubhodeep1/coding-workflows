# Implement-Plan Log — review_autofix retrigger probes: trust a PR-named review run only from the default branch and its own wrapper

- Plan: docs/plans/issue-5838-autofix-pr-named-review-run-provenance-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Source issue: shubhodeep1/coding-workflows#5838   Base branch: claude/implement-plan-issue-5689-smoke-empty-job-log-retryable
- Project branch: claude/implement-plan-issue-5838-autofix-pr-named-review-run-provenance   Final PR: #5852 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (number in the stage report and the checker's resume block)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-01
- Last note: phase 1 implemented and verified (58 tests in the three probe test files, incl. 12 new provenance tests; every other gh_helpers.sh test file passes); phase PR opened against the project branch.

## Phases
1. [ ] Phase 1 — provenance check in `_autofix_pr_named_review_runs` (default-branch head + exact wrapper path/title pair)
   - [x] `_autofix_pr_named_review_runs` drops runs not dispatched on the default branch or not from the exact wrapper for their name (`scripts/gh_helpers.sh`)
   - [x] `_autofix_pr_named_review_default_branch`: event payload (same repository only), else one `GET repos/<repo>`; no `main` fallback
   - [x] unresolved default branch: no listing call, `AUTOFIX_PR_NAMED_REVIEW_PROVENANCE` stderr line; peer probe fails open, budget probe fails closed
   - [x] tests: 12 new in `tests/test_retrigger_default_branch_dispatch.py` (11 fail on the unfixed helper); harnesses of the two other probe test files updated
   - [x] `README.md`, `agents.md`, `changelog.d/5838-autofix-pr-named-review-run-provenance.md` (security)

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified, 2026-10-01)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] Where does the helper get the default branch? — Picked: A — the run's event payload (`.repository.default_branch`, only when `.repository.full_name` is `GITHUB_REPOSITORY`), else one `GET repos/<repo>` read; no `main` fallback. Alternatives: B — always one `GET repos/<repo>` read; C — pass `github.event.repository.default_branch` through both steps' `env:`. Why: GitHub writes both sources; A adds no API call in Actions (§15) and touches no workflow file. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] Fix only this helper, or also the other PR-named matchers #5094 left out? — Picked: A — only `_autofix_pr_named_review_runs`. Alternatives: B — also the merge train, the sweep, and `.claude/scripts/check_in_status.py`. Why: the issue names this helper; the others need their own findings (§5). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] How strict is the path match? — Picked: A — exact `.github/workflows/internal-review.yml` / `.github/workflows/ai-review.yml`, each paired with its title. Alternatives: B — keep the `(^|/)…\.ya?ml$` regex and only pair titles. Why: the issue asks for the exact pair; the poller already uses these paths. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] What happens when the default branch cannot be resolved? — Picked: A — the helper returns 1 before any listing call and logs one `AUTOFIX_PR_NAMED_REVIEW_PROVENANCE` line; callers keep their reasons and fail-open / fail-closed contracts. Alternatives: B — return an empty list; C — a new caller reason value. Why: B would let the budget probe dispatch again on an unverifiable answer; C changes pinned log output (§6). Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode: the session was started by the Claude issue pickup (routine "PR dispatch: #5838") in Auto mode.
- Base branch's own final PR #5702 is an open draft into `claude/implement-plan-issue-4898-retrigger-dispatch-default-branch` (checked 2026-10-01).
- `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1` for every checker instruction (operator instruction recorded on sibling projects #5689 / #5016).
