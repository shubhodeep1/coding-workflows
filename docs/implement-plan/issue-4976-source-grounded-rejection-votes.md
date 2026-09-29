# Implement-Plan Log — Rejection votes must carry source-grounded evidence before they demote a finding

- Plan: docs/plans/issue-4976-source-grounded-rejection-votes-plan.md
- Source issue: shubhodeep1/coding-workflows#4976
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4976-source-grounded-rejection-votes   Final PR: #5027 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR from claude/implement-plan-issue-4976-source-grounded-rejection-votes-phase-1 (number in the stage report and the `— resume.` block)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: project checker armed after this commit (ids in the stage report and the `— resume.` block)
- Last updated: 2026-09-29
- Last note: phase 1 implemented and verified (218 tests in the ci.yml Claude-fixer step pass, 475 across the related review-autofix contract tests; ruff, shellcheck, inventory parity clean); phase PR opened

## Phases
1. [ ] Phase 1 — source-grounded rejection votes   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified it).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] What must a rejection vote carry to count? — Picked: A — an `evidence: <file>:<range>` citation plus a verbatim `quote:` that the gate checks against the reviewed commit. Alternatives: B — never let a demotion authorize auto-merge (hand the round to the fixer instead); C — drop demotion entirely. Why: A is the issue's recommendation and keeps #4586's purpose; B and C revert it (§5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Where may the evidence point? — Picked: A — the voted entry's own file, within 10 lines of its range, at most 20 lines long. Alternatives: B — any file in the repository; C — only the flagged lines themselves. Why: binds the evidence to the finding (§1) while still allowing a nearby guard to be cited. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How is the quote matched? — Picked: A — at least 10 non-whitespace characters, compared with whitespace runs collapsed, also accepting one pair of wrapping backticks removed. Alternatives: B — an exact byte match; C — any length. Why: tolerates reviewer formatting without letting a trivial quote such as `}` through. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Where does the gate read the source? — Picked: A — `git cat-file blob <HEAD_SHA>:<path>` in `GITHUB_WORKSPACE`, the reviewed commit. Alternatives: B — the working tree files; C — the GitHub contents API. Why: binds the check to the reviewed commit, with no API calls (§15). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Does the summariser prompt change? — Picked: A — no. Alternatives: B — document the new fields there too. Why: it already drops every `REJECTED_FINDING … | reason: …` line, and the new fields follow `reason:` (§5). Applied in: no code change. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher in session session_01KViqS5xRtntZ347wDryDoM (permission mode auto). Base branch `claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason` (the issue's `Integration branch:` line), so the final PR targets it and the final-merge stage closes #4976 explicitly with `ai:merged`; activation (steps 12–13) is n/a for this base.
- The session had no `gh` and no GitHub MCP tools at start (the repo was attached after SessionStart); `.claude/hooks/session-start.sh` was run by hand to install `gh`. GitHub writes in this stage go through `gh api` REST.
