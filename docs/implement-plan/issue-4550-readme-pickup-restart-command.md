# Implement-Plan Log — Name the Claude issue pickup restart command under README Failure modes

- Plan: docs/plans/issue-4550-readme-pickup-restart-command-plan.md
- Source issue: shubhodeep1/coding-workflows#4550
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main
- Project branch: claude/implement-plan-issue-4550-readme-pickup-restart-command   Final PR: #4554 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3
- Activation: not started
- Waiting on: conformance fix PR (the PR carrying this log update)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_017uNnABktATbDtY45HwphsK (project checker)
- Last updated: 2026-09-26
- Last note: conformance run 1 CONFORMANT (Correctness: CONCERNS — README.md:1260 not rewrapped as the plan's Approach requires); conformance fix PR opened.

## Phases
1. [x] Phase 1 — name `/claude-issue-pickup start — restart` in README "Failure modes"   — PR #4562 merged 2026-09-26; review rounds: 0; interventions: 0

## Conformance
- Run 1 — 2026-09-26: CONFORMANT (Correctness: CONCERNS) — conformance fix PR (rewrap README.md:1260 to the paragraph width) (pre-security)

## Security pass

## Validation

## Completion
- Final PR #4554 draft

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-26] How much context should accompany the named command? — Picked: A — the command plus "run from a new cloud session opened in the app, in Auto mode", as the Telegram alert says. Alternatives: B — the bare command only. Why: the pickup refuses to start more than 3 links deep, so the new-app-session instruction is what makes the command work; it mirrors `scripts/claude_issue_queue_watchdog.sh:86`. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-26] Fix the duplicated "pickup pickup" in the same sentence? — Picked: A — yes, since the sentence is being rewritten anyway. Alternatives: B — leave it (strict §5). Why: a typo inside the exact sentence the issue asks to change; README-only scope holds. Applied in: phase 1 PR. Status: pending review
- AD-3 [phase 1/1, 2026-09-26] Which fixer logins should the checker use? — Picked: A — leave CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN and CLAUDE_FIXER_VERDICT_BOT_LOGIN empty (fail closed). Alternatives: B — infer the hand-off author from workflow comments. Why: no trusted configuration names either login and the command forbids inferring them from comments; merges, conflicts, and blocks are still detected. Applied in: no code change. Status: pending review

## Lessons
- [source:conformance] When a plan's Approach says to rewrap edited Markdown lines, check every touched line's width against the paragraph before shipping; a sentence rewrite that appends to an existing line leaves an overlong line the plan did not ask for. (files: README.md)

## Notes
- Invoking session: session_01Kud6sT9PzbpLh6S6TBEChD (started by the Claude issue pickup, permission mode auto).
- Stale Routine sweep: 17 ended `PR #45xx status check-in…` Routines deleted; none for this slug.
- Conformance 1/3 (session_0187w1hujjVKtvbg66uJ8mpn): synced the project branch with main (merge a0a4af4). tests/test_workflow_retro.py fails collection on Python 3.11 (f-string backslash) on main too; unrelated to this project.
