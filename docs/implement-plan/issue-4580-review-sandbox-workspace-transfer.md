# Implement-Plan Log — Review sandbox: snapshot and transfer the per-PR workspace, not the checkout

- Plan: docs/completed/issue-4580-review-sandbox-workspace-transfer-plan.md
- Source issue: shubhodeep1/coding-workflows#4580
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-orchestrator-sync-contract-list-union-security-fix-1 (was ai/issue-4568)
- Project branch: claude/implement-plan-issue-4580-review-sandbox-workspace-transfer   Final PR: #4585 ready — review rounds: 1
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-orchestrator-sync-contract-list-union-security-fix-1)
- Waiting on: PR #4585 (final PR, review round 2)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_011n7LVKDJEuhGwymWCYTy23
- Last updated: 2026-09-27
- Last note: final-merge review round 1: fixed WORKSPACE_PATH leaking into the pytest session (tests/conftest.py); rejected 2 findings.

## Phases
1. [x] Phase 1 — transfer review-editor edits into the per-PR workspace (WORKSPACE_PATH) instead of the checkout   — PR #4589 merged 2026-09-27; review rounds: 0; interventions: 0
   - scripts/review_untrusted_sandbox.sh: prepare validates and records the host workspace; run uses the record
   - scripts/review_untrusted_workspace.py: snapshot takes an optional host Git dir
   - tests: regression with a separate WORKSPACE_PATH and fail-closed cases
   - README.md / agents.md wording; changelog.d fragment
   - done: new regression test passes (fails without the fix); existing sandbox / broker / pipeline-contract tests pass

## Conformance
- Run 1 — 2026-09-27: CONFORMANT — no fixes (pre-security). 36 broker + 138 pipeline-contract + 43 related tests pass; the 3 new regression tests fail with the pre-fix scripts; bash -n, shellcheck --severity=error, ruff clean.

## Security pass
- Skipped (ai:workflow-heal: automation-produced issue)

## Validation
- Skipped — Q1: A (human, 2026-09-27): validate.yml binds target_ref only to a final PR into the default branch, and this issue-mode project's final PR #4585 targets a non-default base. The change reaches main with project 3965, whose chain runs its own validation.

## Completion
- Completion PR #4612 merged 2026-09-27 — doc moved to docs/completed/issue-4580-review-sandbox-workspace-transfer-plan.md
- Final PR #4585 ready — review rounds: 1 (base retargeted to claude/implement-plan-orchestrator-sync-contract-list-union-security-fix-1)
  - Round 1 (2026-09-27, head f0a654ae9642): fixed — WORKSPACE_PATH inherited by the legacy sandbox test (tests/conftest.py strips it; nested-pytest regression in tests/test_pytest_git_env_isolation.py); rejected — unquoted workspace read (already quoted, newline-checked in prepare), host Git dir validation (only the trusted prepare step passes it, realpath-resolved).

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Which branch should the fix be built on? — Picked: A — the issue's `Target branch: ai/issue-4568`. Alternatives: B — the default branch `main`. Why: `/implement-issue-claude` builds on the branch the issue names, which already carries PR #4572's edits to the same script. Applied in: plan header. Status: pending review
- AD-2 [plan, 2026-09-27] Is the failure a code defect or transient? — Picked: A — code defect in the sandbox transfer target; the final run's OpenRouter credit error is reported as environmental. Alternatives: B — report as transient, no change. Why: reproduced locally and in all five runs. Applied in: plan. Status: pending review
- AD-3 [plan, 2026-09-27] Also stage the consolidator/floor/parser/ledger scripts that every run skips? — Picked: A — no, separate latent gap. Alternatives: B — add them to REQUIRED_BOOTSTRAP_SCRIPTS here. Why: §5; turning on stages dark since June changes review output beyond this failure. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-27] How does the sandbox pick its host directory? — Picked: A — prepare validates WORKSPACE_PATH under ${RUNNER_TEMP}/workspaces, records it in the sandbox root, and lists files through the checkout's Git database. Alternatives: B — mirror transferred files afterwards; C — trust GIT_WORK_TREE from the environment. Why: one tree for snapshot, transfer, and commit; only the trusted prepare step chooses the write target. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-27] Relax the editor's `Review file issue audit:` wording check? — Picked: A — no, out of scope. Alternatives: B — relax it here. Why: §5; model output drift the retry absorbs. Applied in: no code change. Status: pending review

## Lessons
- [source:plan-deviation] An issue-mode project whose issue base is not the default branch cannot be validated with target_ref, because validate.yml binds it only to a final PR into the default branch; settle validation when the plan is written. (files: .github/workflows/validate.yml)
- [source:plan-deviation] An issue base that is another PR's head can be closed unmerged and superseded; check the base PR's state at every stage, not only whether it merged. (files: .claude/commands/implement-plan-claude.md)
- [source:plan-deviation] A helper that copies files back from an isolated copy must target the directory the commit step's Git work tree points at (WORKSPACE_PATH in review_autofix.yml), not GITHUB_WORKSPACE. (files: scripts/review_untrusted_sandbox.sh, scripts/review_untrusted_workspace.py)
- [source:intervention] When a script under test starts reading a workflow-exported variable (WORKSPACE_PATH, GIT_DIR, staged-support ledgers), add it to tests/conftest.py's session strip list in the same change, or tests that copy os.environ inherit the editor's live value. (files: tests/conftest.py, scripts/workspace_init.sh)

## Notes
- 2026-09-27 validation blocker: asked Q1 on #4580 (validate.yml `-f "base=${VALIDATE_DEFAULT_BRANCH}"` binding); answered Q1: A (skip validation) by the human in the conformance stage session.
- 2026-09-27 base move: PR #4572 (head ai/issue-4568) closed unmerged at 03:35Z; project 3965 moved to /implement-plan-claude and continued in #4602 from the same head (110eb165). Human answer Q2: A: move this project onto #4602's head `claude/implement-plan-orchestrator-sync-contract-list-union-security-fix-1`. Merged it into the project branch cleanly (no conflicts; #4602 only adds `python3 -I -B` hardening to review_untrusted_sandbox.sh and does not fix #4580); 217 sandbox/broker/pipeline/host-isolation tests pass on the merged tree. Final PR #4585 retargeted. If #4602 merges, the base follows it to claude/implement-plan-orchestrator-sync-contract-list-union.
- Checker: the resume block for conformance 1/3 named session_019GkQhkHGKa6Zxe9SxK8CVd (the phase-1 stage) as the checker; the actual project checker is session_011n7LVKDJEuhGwymWCYTy23.
- Permission mode auto (issue mode records it, never asks).
- Review support for this repo's PRs is staged from protected main, so the fix affects live reviews only once it reaches main.
