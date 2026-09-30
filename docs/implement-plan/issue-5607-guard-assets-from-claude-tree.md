# Implement-Plan Log — Consumer sync takes hook and settings files from the owner-reviewed `.claude/` tree, not the twin

- Plan: docs/plans/issue-5607-guard-assets-from-claude-tree-plan.md
- Source issue: shubhodeep1/coding-workflows#5607
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5607-guard-assets-from-claude-tree   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project started from issue #5607 (issue mode, base claude/implement-plan-issue-4785-twin-first-claude-sync).

## Phases
1. [ ] Phase 1 — guard assets from the `.claude/` tree at consumer sync and seed (edits the twin `workflow-templates/.claude/commands/seed-repo.md` only; protected paths: none)

## Conformance

## Security pass
- Security: skipped (ai:security: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which sync surfaces does the fix cover? — Picked: A — the consumer updater and the `/seed-repo` twin. Alternatives: B — the updater only; C — also add a release gate. Why: both copy the stable twin tree into consumers; seed is edited twin-first at no extra cost (§1). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What changes at release? — Picked: A — nothing; release sources no `.claude/` asset, and the consumer side reads the tagged commit's `.claude/` tree. Alternatives: B — fail the release while any guard twin differs from `.claude/`; C — rewrite the twin at release. Why: B stalls unrelated releases behind an owner review, C writes to protected history (§5). Applied in: no code change. Status: pending review
- AD-3 [plan, 2026-09-30] A guard twin differs from `.claude/` and the consumer lacks the file: what is installed? — Picked: A — the `.claude/` copy from the same commit (owner-reviewed). Alternatives: B — nothing. Why: follows the recommendation and never leaves a new consumer without a reviewed guard. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] The `.claude/` copy is missing at the stable commit: what happens? — Picked: A — skip with a warning; never install the twin. Alternatives: B — install the twin. Why: B is the finding's exploit path (§1). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Where does the rule live? — Picked: A — in the existing shell step, with a parity test against `claude_twin_sync.py`'s constants. Alternatives: B — a new `claude_twin_sync.py` subcommand. Why: smallest change, no new CLI surface (§5). Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Security pass: skip (`security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- Base PR check (2026-09-30): the base branch's PR #4804 is open (draft) into main.
