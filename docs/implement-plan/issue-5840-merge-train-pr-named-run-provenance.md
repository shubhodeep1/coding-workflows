# Implement-Plan Log — Merge train: trust a PR-named review run only from the default branch and its wrapper

- Plan: docs/plans/issue-5840-merge-train-pr-named-run-provenance-plan.md
- Source issue: shubhodeep1/coding-workflows#5840 (progress comment 5923126918)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-5689-smoke-empty-job-log-retryable
- Project branch: claude/implement-plan-issue-5840-merge-train-pr-named-run-provenance   Final PR: #5850 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (the PR carrying this log commit)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-01
- Last note: phase 1 implemented and verified (tests/test_review_merge_train.py 29 passed, tests/test_review_dispatch_default_branch.py passed, shellcheck clean); phase PR opened.

## Phases
1. [ ] Phase 1 — default-branch and wrapper provenance for the merge train's PR-named key (`scripts/review_merge_train.sh`, `tests/test_review_merge_train.py`, `README.md`, `changelog.d/5840-merge-train-pr-named-run-provenance.md`)   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header (`security_pass_skip.py`).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] Where does the release path get the default branch? — Picked: A — from `base.repo.default_branch` in the open-PR listing it already makes. Alternatives: B — one extra `GET repos/<repo>`; C — a new env var set by the calling workflows. Why: no new API call (§15) and no workflow or env change (§5); every PR in that listing carries it. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What happens when the default branch cannot be determined? — Picked: A — emit no `pr:<N>` keys, log `MERGE_TRAIN_PR_NAMED_PROVENANCE … outcome=default_branch_unresolved`, keep head-branch keys. Alternatives: B — hold every queued PR back; C — assume `main`. Why: A never trusts an unverified name (§1) and keeps the train's fail-open contract; B stalls the queue, C trusts a guess. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] Fix the other PR-named matchers (`gh_helpers.sh`, `review_autofix_sweep.yml`, `check_in_status.py`) too? — Picked: A — no, only the merge train the finding names. Alternatives: B — fix all four in this PR. Why: the issue names one location; the others have their own callers and tests and are a separate change (§5). Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-10-01] How strict is the workflow path check? — Picked: A — exact `.github/workflows/internal-review.yml` / `.github/workflows/ai-review.yml`, each paired with its own run name, as in #5094. Alternatives: B — any path ending in the wrapper file name. Why: a suffix match would accept a lookalike path; the exact pairing matches the poller's rule. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue base `claude/implement-plan-issue-5689-smoke-empty-job-log-retryable` (final PR #5702, draft, into `claude/implement-plan-issue-4898-retrigger-dispatch-default-branch`), not merged as of 2026-10-01 02:00Z.
- `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1` in every checker instruction (operator instruction recorded on #5689's project).
- Phase 1 also updated the static name assertion in `tests/test_review_dispatch_default_branch.py` (`ReviewWrapperRunNames.test_lookups_use_the_names_the_wrappers_set`), which pinned the old title-only regex; it now also checks the name/path pairing.
- Steps 12–13 (activation) do not run: the base is not the default branch (`Activation: n/a` at the end).
