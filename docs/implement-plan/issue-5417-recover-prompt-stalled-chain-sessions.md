# Implement-Plan Log — Recover chain sessions stuck on a permission prompt

- Plan: docs/plans/issue-5417-recover-prompt-stalled-chain-sessions-plan.md
- Source issue: shubhodeep1/coding-workflows#5417
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5417-recover-prompt-stalled-chain-sessions   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-01
- Last note: project branch opened from claude/implement-plan-claude-fixer-unattended-convergence (8d5fc03)

## Phases
1. [ ] Phase 1 — prompt-stall recovery in the janitor, the pickup, the claim reader, and the resumed sessions — protected paths: .claude/scripts/stale_sessions.py, .claude/scripts/check_in_status.py, .claude/settings.json, .claude/commands/implement-plan-claude.md, .claude/commands/implement-issue-claude.md, .claude/commands/fix-claude-pr.md, .claude/commands/claude-issue-pickup.md
   - [ ] check_in_status.py twin: recovery-claim supersede rule, `stall_recoveries`, optional prefetched `pr` / `comments`
   - [ ] stale_sessions.py twin: recovery parser, threshold (CLAUDE_PROMPT_STALL_RECOVER_MINUTES, default 45), `recover` list, cap, idempotency
   - [ ] settings.json twin: allow `interrupt_session`
   - [ ] implement-plan-claude.md / implement-issue-claude.md / fix-claude-pr.md twins: recovery arguments and preflight
   - [ ] claude-issue-pickup.md (no twin): step 3a recovery sub-step (diff in the twin-sync blocker)
   - [ ] CLAUDE.md §26.C step 5, README, agents.md, changelog.d fragment
   - [ ] tests in tests/test_stale_sessions.py

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] How does the janitor tell that a chain already moved on? — Picked: A — a newer session of the same chain in the session list (and, for a fixer, the live PR state). Alternatives: B — compare the project branch log's `Stage:` with the stalled stage; C — both. Why: the log on the project branch lags and stage names have no total order, while every later stage is a newer session titled with the slug. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-10-01] Which stage does the replacement run? — Picked: A — the stalled stage itself, with a recovery preflight that reuses the stuck session's pushed branch, open PR, or dispatched run. Alternatives: B — the pickup derives the next stage from the log. Why: each stage already re-verifies against GitHub, and arming the wait on an open PR lets the checker route to the real next stage. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-10-01] How is an issue implement session requeued? — Picked: A — the pickup starts a fresh `/implement-issue-claude` session itself with the step 3 start. Alternatives: B — comment `/reclarify` on the issue. Why: same end state without an hour's wait or a comment that starts `issue_comment` workflows. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-10-01] In which order does the pickup act? — Picked: A — start the replacement, then interrupt, rename, and delete bound triggers. Alternatives: B — interrupt and rename first. Why: a failed start then leaves the stuck session for the next wake instead of stranding the chain. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-10-01] How are fixer recoveries recorded and counted per head? — Picked: A — a reservation claim `prompt-stall-recovery-<suffix>` that ends earlier claims on the head and is counted as `stall_recoveries`. Alternatives: B — a separate marker comment plus a growing ignore list; C — count per PR from session titles. Why: one comment is the record, the count, and the hand-off. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-10-01] Which titles are excluded? — Picked: A — checkers, `waiting:` sessions, `deploy-activate`, and fixers titled `fixed` / `on hold`. Alternatives: B — every `implement-plan <slug> — …` title. Why: none of them has a stage or fix to resume. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-10-01] Should the archive classifier also learn the `PR #<P> — ` prefix? — Picked: A — no; only the recovery parser strips it, and the gap is noted. Alternatives: B — fix `classify_title` too. Why: §5. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-10-01] What is "the same stage" for the cap? — Picked: A — the stage text of the title. Alternatives: B — scope the count to sessions newer than the last non-replaced stage. Why: deterministic from titles. Applied in: phase 1. Status: pending review
- AD-9 [plan, 2026-10-01] What happens to the stuck session's Routines? — Picked: A — the pickup deletes every enabled trigger bound to it after the interrupt. Alternatives: B — leave them. Why: a later fire would wake the interrupted session and race the replacement. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Permission mode: auto.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-01)
- Out of scope: `stale_sessions.classify_title` strips only a `#<N> · ` prefix (see the plan's Notes).
