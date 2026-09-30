# Implement-Plan Log — Give the conflict-resolver model a private Git index

- Plan: docs/plans/issue-5627-resolver-private-model-index-plan.md
- Source issue: shubhodeep1/coding-workflows#5627
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5627-resolver-private-model-index   Final PR: #5637 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (review round or merge)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 implemented and verified (782 related tests passed, 1 skipped); phase PR opened against the project branch

## Phases
1. [ ] Phase 1 — private resolver model index (`GIT_INDEX_FILE` copy per attempt plus OpenCode snapshot opt-out, source repo only; regression tests; agents.md; changelog fragment)   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (ai:workflow-heal: automation-produced issue; plan header `Security pass: skip`)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which branch should this project build on, given that the issue's `Target branch:` is PR #5596's head `auto/forward-merge-stable-36694528358-1` but `review_autofix.yml` loads `review_conflict_resolve.sh` from protected `main`? — Picked: A — the default branch `main`. Alternatives: B — the named target branch. Why: a fix on #5596's head reaches `main` only when #5596 merges, which this failure blocks; the resolver code is identical on both branches. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-30] Should the private model index apply to every repo or only the workflow source repo? — Picked: A — only when `IS_WORKFLOW_SOURCE_REPO=true`. Alternatives: B — every repo. Why: smallest blast radius; only the source repo runs the index-sensitive guards. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How to keep OpenCode's own snapshot git calls, which inherit the environment, from sharing the model's private index? — Picked: A — `snapshot: false` in the resolver's own OpenCode config (source repo only). Alternatives: B — leave snapshots on; C — add an option to the shared `write_opencode_config.sh`. Why: smallest change that keeps the model's index view correct. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Where should the regression tests live? — Picked: A — `tests/test_review_conflict_resolve_retry_prelude_render.py`, registered in its `main()`. Alternatives: B — a new test file wired into `ci.yml`. Why: the issue names it and all three CI workflows already run it. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] A heal issue for a source-repo review/autofix failure must target `main`, not the failing PR's head: `review_autofix.yml` loads its support scripts from protected `main`, so a fix on the PR head cannot unblock that PR. (files: scripts/workflow_failure_heal_intake.sh, .github/workflows/review_autofix.yml)

## Notes
- Base branch: the issue named `auto/forward-merge-stable-36694528358-1` (PR #5596's head); the project uses `main` per AD-1.
- `security_pass_skip.py`: skip=true (label `ai:workflow-heal`).
- Out of scope: the stale `source_pr_head` comment in `scripts/workflow_failure_heal_intake.sh` that sends source-repo review/autofix heal issues to the PR head branch.
