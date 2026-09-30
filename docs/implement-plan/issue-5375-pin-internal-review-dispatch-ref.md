# Implement-Plan Log — Pin the pre-approved internal-review.yml dispatch to the default branch

- Plan: docs/plans/issue-5375-pin-internal-review-dispatch-ref-plan.md
- Source issue: shubhodeep1/coding-workflows#5375
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-4985-skip-marker-review-stall
- Project branch: claude/implement-plan-issue-5375-pin-internal-review-dispatch-ref   Final PR: #5386 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #5477: twin sync
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 implemented twin-first and verified; phase PR held for the `[claude-twin-sync]` copy of five `.claude/` files (twin-sync blocker on #5375).

## Phases
1. [ ] Phase 1 — pin the internal-review.yml dispatch to the default branch   — PR #5477 open (held: twin sync); review rounds: 0; interventions: 0
   - protected paths: .claude/settings.json, .claude/scripts/dispatch_workflow.py, .claude/hooks/gh_api_write_guard.py, .claude/commands/fix-claude-pr.md, .claude/commands/implement-plan-claude.md
   - [x] twin allow list drops `Bash(gh workflow run internal-review.yml *)` (workflow-templates/.claude/settings.json; tests/test_dispatch_workflow.py::test_allowlist_matches_the_gh_workflow_run_allow_rules)
   - [x] twin helper pins internal-review.yml to the REST default branch, refuses another `--ref`, accepts only a numeric `pr_number` (workflow-templates/.claude/scripts/dispatch_workflow.py:88, :174, :198; tests/test_dispatch_workflow.py:291-363)
   - [x] twin API guard asks on a raw internal-review.yml dispatch (workflow-templates/.claude/hooks/gh_api_write_guard.py:175; tests/test_gh_api_write_guard.py::test_internal_review_dispatch_asks)
   - [x] docs: CLAUDE.md §23.H/§23.I (workflow-templates/CLAUDE.md is a symlink to it), agents.md (stall re-dispatch, helper entry, guard paragraph), /implement-plan-claude and /fix-claude-pr twins
   - [x] tests: 51 test files touching changed paths — 2232 passed; 13 root-vs-twin parity checks fail until the twin sync; after a simulated sync they pass. tests/test_implement_post_codex_recovery.py::test_review_pipeline_integration_chain_module_runs_clean fails locally on the base too (no gawk)
   - [x] changelog.d/5375-pin-internal-review-dispatch-ref.md (security)

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How should the `Bash(gh workflow run internal-review.yml *)` allow rule be restricted? — Picked: A — remove it; `dispatch_workflow.py`, which pins the ref, is the only pre-approved path. Alternatives: B — narrow it and add deny rules for `--ref` / `-r`; C — keep it. Why: a glob cannot pin `--ref` (`gh` keeps the last one), so B is bypassable and C leaves the finding open. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] How should the §23.H API guard treat `gh api … internal-review.yml/dispatches`? — Picked: A — not routine: it asks, in every mode. Alternatives: B — routine only when `ref` equals a default branch derived from local git state. Why: the guard makes no API calls and local git state is not a verified default branch; the helper is the verified path. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Which inputs may the helper pass to `internal-review.yml`? — Picked: A — only `pr_number`, as a positive decimal integer (at most 10 digits). Alternatives: B — also `allow_workflow_edits` (`true` / `false`). Why: the only caller passes `pr_number` alone; the smallest accepted surface. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Should the other six pre-approved `gh workflow run <file> *` rules be pinned in this project too? — Picked: A — no; fix `internal-review.yml`, which the finding names, and record the others in `## Notes`. Alternatives: B — pin all seven. Why: §5; those rules predate the finding, and changing them touches §23.C/§23.H contracts and several commands. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] Which ref string does the helper send for `internal-review.yml`? — Picked: A — the bare default branch name from the REST API. Alternatives: B — `refs/heads/<default>`. Why: A is what `review_autofix_sweep.yml` sends since #4618 and what `check_in_status.py` matches on `head_branch`; B's run metadata is unverified. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:security] A `Bash(<cmd> *)` allow rule cannot pin a flag value such as `gh workflow run --ref`; pre-approve the dispatch only through a helper that reads and enforces the ref itself. (files: .claude/settings.json, .claude/scripts/dispatch_workflow.py)

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
- Security pass skipped per `security_pass_skip.py`: ai:security: created and labelled by the issue automation.
- AD-4 follow-up: the six older `Bash(gh workflow run <file> *)` rules on `main` accept an unpinned `--ref` the same way; a separate issue should decide whether to pin them.
