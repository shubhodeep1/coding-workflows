# Implement-Plan Log — Give the conflict-resolver model a private Git index

- Plan: docs/completed/issue-5627-resolver-private-model-index-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5627
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5627-resolver-private-model-index   Final PR: #5637 ready
- Status: COMPLETE
- Stage: final-merge — review round
- Activation: pending verify-activation
- Waiting on: PR #5637 (final PR, project branch into main; review round 4 after the round-3 log fix)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01LX3ut8ixr4nKefuuzQVDA4 (reused; safety net and hand-back recorded in the stage report)
- Last updated: 2026-10-01
- Last note: final PR #5637 review round 3 (head 8cb2739): fixed the one valid finding, this log's stale completion and final-PR state; rounds 1 and 2 had no valid finding (reviewer infrastructure failures only)

## Phases
1. [x] Phase 1 — private resolver model index (`GIT_INDEX_FILE` copy per attempt plus OpenCode snapshot opt-out, source repo only; regression tests; agents.md; changelog fragment)   — PR #5648 merged 2026-09-30 (merge commit 442b5cb); review rounds: 2; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT — no fixes (pre-security; 0 findings, 0 auto-decisions)

## Security pass
- Skipped (ai:workflow-heal: automation-produced issue; plan header `Security pass: skip`)

## Validation
- Cycle 1 — run 36750018684 2026-09-30 (target_ref: claude/implement-plan-issue-5627-resolver-private-model-index, checked out 442b5cb): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 295s); no fix PR

## Completion
- PR #5751 merged 2026-09-30 (merge commit cb3c13b) — doc moved to docs/completed/issue-5627-resolver-private-model-index-plan.md
- Final PR #5637 ready (marked ready at the final-merge stage) — review rounds: 3

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which branch should this project build on, given that the issue's `Target branch:` is PR #5596's head `auto/forward-merge-stable-36694528358-1` but `review_autofix.yml` loads `review_conflict_resolve.sh` from protected `main`? — Picked: A — the default branch `main`. Alternatives: B — the named target branch. Why: a fix on #5596's head reaches `main` only when #5596 merges, which this failure blocks; the resolver code is identical on both branches. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-30] Should the private model index apply to every repo or only the workflow source repo? — Picked: A — only when `IS_WORKFLOW_SOURCE_REPO=true`. Alternatives: B — every repo. Why: smallest blast radius; only the source repo runs the index-sensitive guards. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How to keep OpenCode's own snapshot git calls, which inherit the environment, from sharing the model's private index? — Picked: A — `snapshot: false` in the resolver's own OpenCode config (source repo only). Alternatives: B — leave snapshots on; C — add an option to the shared `write_opencode_config.sh`. Why: smallest change that keeps the model's index view correct. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Where should the regression tests live? — Picked: A — `tests/test_review_conflict_resolve_retry_prelude_render.py`, registered in its `main()`. Alternatives: B — a new test file wired into `ci.yml`. Why: the issue names it and all three CI workflows already run it. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] A heal issue for a source-repo review/autofix failure must target `main`, not the failing PR's head: `review_autofix.yml` loads its support scripts from protected `main`, so a fix on the PR head cannot unblock that PR. (files: scripts/workflow_failure_heal_intake.sh, .github/workflows/review_autofix.yml)
- [source:intervention] A shell helper that resolves the index with `git rev-parse --git-path index` and then deletes or overwrites a file must refuse when that path is the file it deletes: an inherited `GIT_INDEX_FILE` redirects `--git-path index`. (files: scripts/review_conflict_resolve.sh)

## Notes
- Review round 1 (2026-09-30, head 887d5ef): fixed the inherited-`GIT_INDEX_FILE` same-path deletion risk, the duplicate `import os` (pre-existing), and the staging-contract task gap; rejected the raw f-string regex finding (compiles and is deterministic) and the `TypeError` finding (`sys.argv[1]` is always a string).
- Review round 2 (2026-09-30, head 96769bb): fixed the consensus task gap (minimax, qwen, glm): `stage_resolver_touched_path_or_fail` now documents that it stages into the real index and must never run with `GIT_INDEX_FILE` on the model's copy; `test_private_model_index_wiring` pins it. No findings rejected.
- Final PR #5637 review round 1 (2026-10-01, head cb3c13b): no valid finding; OpenRouter credit exhaustion caused the 3 reviewer failures; synced main → 7004d36.
- Final PR #5637 review round 2 (2026-10-01, head 7004d36): 0 findings; qwen pass-2 empty output made the ledger non-clean; synced main → 8cb2739.
- Final PR #5637 review round 3 (2026-10-01, head 8cb2739): one valid consensus task gap (all 6 reviewers): this log still showed the completion PR as open and the final PR as a draft. Fixed the header and Completion fields. The failed `review / gate` and `review-claude-branch-push / gate` checks on that head were both a GitHub API rate-limit 403 in the gate's support-ref step (runs at 02:01 and 02:06 UTC, before review run 36804792120); infrastructure, no code defect.
- Base branch: the issue named `auto/forward-merge-stable-36694528358-1` (PR #5596's head); the project uses `main` per AD-1.
- `security_pass_skip.py`: skip=true (label `ai:workflow-heal`).
- Out of scope: the stale `source_pr_head` comment in `scripts/workflow_failure_heal_intake.sh` that sends source-repo review/autofix heal issues to the PR head branch.
