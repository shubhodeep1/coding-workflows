# Implement-Plan Log — Review sandbox: snapshot and transfer the per-PR workspace, not the checkout

- Plan: docs/plans/issue-4580-review-sandbox-workspace-transfer-plan.md
- Source issue: shubhodeep1/coding-workflows#4580
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: ai/issue-4568
- Project branch: claude/implement-plan-issue-4580-review-sandbox-workspace-transfer   Final PR: #4585 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-27
- Last note: phase 1 implemented and verified; phase PR opened against the project branch.

## Phases
1. [ ] Phase 1 — transfer review-editor edits into the per-PR workspace (WORKSPACE_PATH) instead of the checkout
   - scripts/review_untrusted_sandbox.sh: prepare validates and records the host workspace; run uses the record
   - scripts/review_untrusted_workspace.py: snapshot takes an optional host Git dir
   - tests: regression with a separate WORKSPACE_PATH and fail-closed cases
   - README.md / agents.md wording; changelog.d fragment
   - done: new regression test passes (fails without the fix); existing sandbox / broker / pipeline-contract tests pass

## Conformance

## Security pass
- Skipped (ai:workflow-heal: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Which branch should the fix be built on? — Picked: A — the issue's `Target branch: ai/issue-4568`. Alternatives: B — the default branch `main`. Why: `/implement-issue-claude` builds on the branch the issue names, which already carries PR #4572's edits to the same script. Applied in: plan header. Status: pending review
- AD-2 [plan, 2026-09-27] Is the failure a code defect or transient? — Picked: A — code defect in the sandbox transfer target; the final run's OpenRouter credit error is reported as environmental. Alternatives: B — report as transient, no change. Why: reproduced locally and in all five runs. Applied in: plan. Status: pending review
- AD-3 [plan, 2026-09-27] Also stage the consolidator/floor/parser/ledger scripts that every run skips? — Picked: A — no, separate latent gap. Alternatives: B — add them to REQUIRED_BOOTSTRAP_SCRIPTS here. Why: §5; turning on stages dark since June changes review output beyond this failure. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-27] How does the sandbox pick its host directory? — Picked: A — prepare validates WORKSPACE_PATH under ${RUNNER_TEMP}/workspaces, records it in the sandbox root, and lists files through the checkout's Git database. Alternatives: B — mirror transferred files afterwards; C — trust GIT_WORK_TREE from the environment. Why: one tree for snapshot, transfer, and commit; only the trusted prepare step chooses the write target. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-27] Relax the editor's `Review file issue audit:` wording check? — Picked: A — no, out of scope. Alternatives: B — relax it here. Why: §5; model output drift the retry absorbs. Applied in: no code change. Status: pending review

## Lessons
- [source:plan-deviation] A helper that copies files back from an isolated copy must target the directory the commit step's Git work tree points at (WORKSPACE_PATH in review_autofix.yml), not GITHUB_WORKSPACE. (files: scripts/review_untrusted_sandbox.sh, scripts/review_untrusted_workspace.py)

## Notes
- Permission mode auto (issue mode records it, never asks).
- Review support for this repo's PRs is staged from protected main, so the fix affects live reviews only once it reaches main.
