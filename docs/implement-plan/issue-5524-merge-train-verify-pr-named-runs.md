# Implement-Plan Log — Merge train: count a PR-named review run as active only when it came from the default branch and its wrapper

- Plan: docs/plans/issue-5524-merge-train-verify-pr-named-runs-plan.md
- Source issue: shubhodeep1/coding-workflows#5524
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5524-merge-train-verify-pr-named-runs   Final PR: #5540 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (review round or merge)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 implemented and verified (87 merge-train and dispatch tests pass; 3 new exploit tests fail on the old script); phase PR opened.

## Phases
1. [ ] Phase 1 — verify PR-named run provenance in the merge-train release (`scripts/review_merge_train.sh`, tests, docs, changelog fragment)   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified 2026-09-30)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Where does the release get the default branch it checks PR-named runs against? — Picked: A — `base.repo.default_branch` from the open-PR listing `_mt_release` already makes. Alternatives: B — one extra `GET repos/<repo>` per release like the poller's `_pr_named_review_default_branch`; C — a caller-supplied env var. Why: no new API call (§15), and GitHub reports the field on every PR; C can carry a guessed `main`. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What happens when the default branch cannot be resolved? — Picked: A — ignore PR-named runs for that release, log `MERGE_TRAIN_PR_NAMED_PROVENANCE … outcome=default_branch_unresolved`, keep head-branch keys, continue. Alternatives: B — leave every queued PR queued for that tick. Why: matches the finding's recommendation and the script's existing fail-open for an unreadable runs listing; B would recreate the denial of service on an API hiccup. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Fix the other PR-named matchers (`scripts/gh_helpers.sh`, `review_autofix_sweep.yml`, `check_in_status.py`) too? — Picked: A — no, only the merge train named by #5524. Alternatives: B — fix every matcher in this project. Why: one issue, one phase; the others are separate call sites with their own tests. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-30] Which (path, title) pairs are approved? — Picked: A — exactly (`.github/workflows/internal-review.yml`, `Internal: AI Review & Autofix [pr:<N>]`) and (`.github/workflows/ai-review.yml`, `AI Review [pr:<N>]`), as in the poller (#5094). Alternatives: B — also accept `review_autofix.yml` with either name. Why: `review_autofix.yml` sets no PR run name. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Local verification: `tests/test_implement_post_codex_recovery.py::test_review_pipeline_integration_chain_module_runs_clean` fails in this container on the base branch too (`gawk: command not found`); unrelated to this change.
- Invoked by the Claude issue dispatcher (routine `implement-issue #5524`) in session session_01Wcp5vvyxgd3YizNmY7bQqD, Auto mode.
