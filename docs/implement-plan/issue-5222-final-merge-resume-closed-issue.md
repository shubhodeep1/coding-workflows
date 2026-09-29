# Implement-Plan Log — Resume an issue-mode project with /reclarify after the final merge closed its issue

- Plan: docs/plans/issue-5222-final-merge-resume-closed-issue-plan.md
- Source issue: shubhodeep1/coding-workflows#5222 (https://github.com/shubhodeep1/coding-workflows/issues/5222)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5222-final-merge-resume-closed-issue   Final PR: #5225 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #5271: twin sync
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (twin-first hold: the wait is armed by the stage `/reclarify` resumes)
- Last updated: 2026-09-29
- Last note: phase 1 PR #5271 opened twin-first; held (hold claim) until the supervising session copies the two command twins into .claude/commands as a [claude-twin-sync] commit and comments /reclarify on #5222

## Phases
1. [ ] Phase 1 — final-merge resume on a closed issue   — PR #5271 open (held for twin sync); review rounds: 0; interventions: 0; protected paths: .claude/commands/implement-issue-claude.md, .claude/commands/implement-plan-claude.md (twin-first)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which fix direction? — Picked: A — intake route `final_merge_resume` in clarify, intake, and `/implement-issue-claude`. Alternatives: B — swap the final PR's `Fixes` for `Refs` at a final-merge block; C — the project checker resumes on the merge. Why: the issue lists A first and asks for closed-issue route tests; it keeps the `Fixes` contract and also covers an issue that already closed. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] New trigger value or new route reason? — Picked: A — keep `trigger: reclarify` and add route reason `final_merge_resume`. Alternatives: B — a new `final_merge_resume` trigger in `VALID_TRIGGERS` and the payload. Why: §5, no payload or fire-text schema change, and the intake re-derives eligibility from live data anyway. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Who may author the blocked comment that unlocks the route? — Picked: A — a trusted `User` only (`OWNER` / `MEMBER` / `COLLABORATOR`). Alternatives: B — also `github-actions[bot]`. Why: §1; the sessions post blocked comments through MCP as a user, never as the Actions bot. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] How is the blocked stage read? — Picked: A — the first `Stage:` / `**Stage:**` line of the latest trusted blocked comment, whose value starts with `final-merge`. Alternatives: B — a new marker attribute `<!-- ai:claude-blocked:v1 stage=… -->`. Why: A matches the comments already on blocked issues (#5119), and B would strand them. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Where is `ai:claude-blocked` removed? — Picked: A — the resumed `/implement-issue-claude` session's step 2 Claim (existing behaviour, now reached for the closed issue). Alternatives: B — the clarify handoff script. Why: the label stays the visible "blocked" signal until a session really resumes, and the label gate keeps a repeated `/reclarify` from re-routing after that. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Should the handoff's routing comment differ for the resume? — Picked: A — yes, a resume-specific body under the same `ai:claude-issue-routed:v1` marker. Alternatives: B — reuse the "a Claude session will implement this issue" text. Why: the generic text says the issue closes when the completion PR merges, which is wrong for a closed issue. Applied in: phase 1. Status: pending review

## Lessons
- [source:plan-deviation] A full local `pytest tests` run does not finish within 40 minutes on a cloud session runner; verify a phase by running each test file that references the changed paths separately with a per-file timeout, and leave the full suite to CI. (files: tests/test_orchestrate_poll_process.py)

## Notes
- Issue progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5222#issuecomment-5898432937
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
