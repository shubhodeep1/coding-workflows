# Implement-Plan Log — Name the Claude issue pickup restart command under README Failure modes

- Plan: docs/plans/issue-4550-readme-pickup-restart-command-plan.md
- Source issue: shubhodeep1/coding-workflows#4550
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main
- Project branch: claude/implement-plan-issue-4550-readme-pickup-restart-command   Final PR: (pending)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-26
- Last note: project branch opened; implementing phase 1.

## Phases
1. [ ] Phase 1 — name `/claude-issue-pickup start — restart` in README "Failure modes"

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-26] How much context should accompany the named command? — Picked: A — the command plus "run from a new cloud session opened in the app, in Auto mode", as the Telegram alert says. Alternatives: B — the bare command only. Why: the pickup refuses to start more than 3 links deep, so the new-app-session instruction is what makes the command work; it mirrors `scripts/claude_issue_queue_watchdog.sh:86`. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-26] Fix the duplicated "pickup pickup" in the same sentence? — Picked: A — yes, since the sentence is being rewritten anyway. Alternatives: B — leave it (strict §5). Why: a typo inside the exact sentence the issue asks to change; README-only scope holds. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Invoking session: session_01Kud6sT9PzbpLh6S6TBEChD (started by the Claude issue pickup, permission mode auto).
- Stale Routine sweep: 17 ended `PR #45xx status check-in…` Routines deleted; none for this slug.
