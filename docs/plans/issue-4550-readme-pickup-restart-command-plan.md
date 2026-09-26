# Name the Claude issue pickup restart command under README Failure modes

Source issue: shubhodeep1/coding-workflows#4550 (https://github.com/shubhodeep1/coding-workflows/issues/4550)
Base branch: main
Security pass: run

## Summary

The README's "Claude issue implementer" → "Failure modes" paragraph says the
queue watchdog's stale-item alert (`ai:claude-issue-queue-stale`) sends "the
restart command" but never names it. Name `/claude-issue-pickup start — restart`
there so an operator can restart a stopped pickup from the README alone.

## Context

- `README.md:1253-1269` ("Failure modes") ends the watchdog sentence with
  "sends a Telegram ERROR with the restart command." The same sentence also
  repeats a word across the line break (`means the pickup` / `pickup stopped`).
- The command the Telegram alert actually sends is in
  `scripts/claude_issue_queue_watchdog.sh:86`: "Restart it from a new cloud
  session opened in the app, in Auto mode: /claude-issue-pickup start — restart".
- `.claude/commands/claude-issue-pickup.md:10,30,60` defines `start — restart`
  as "take over from a pickup that exists but has stopped".
- The issue's acceptance criterion: README.md only; no code, workflow, or
  changelog changes. §20.A agrees: a docs-only edit needs no fragment.

## Goals

- The Failure modes paragraph names `/claude-issue-pickup start — restart` next
  to the `ai:claude-issue-queue-stale` alert, with the same "new app session,
  Auto mode" instruction the alert itself carries.
- The duplicated word in that sentence is removed.

## Non-goals

- No change to the watchdog script, workflow, command files, `agents.md`,
  `CHANGELOG.md`, or `changelog.d/`.
- No other README section is reworded.

## Constraints

- §5 minimal change set: edit only the watchdog sentence.
- §6: no identifier changes; the command, label, and variable names are quoted
  as they exist.
- §20: docs-only, no changelog fragment (and the issue forbids one).
- §14: README.md is not a synced template asset; no consumer impact.

## Approach

Rewrite the watchdog sentence in `README.md` "Failure modes" to:
"… means the pickup stopped: `claude-issue-queue-watchdog.yml` labels it
`ai:claude-issue-queue-stale` and sends a Telegram ERROR with the restart
command, `/claude-issue-pickup start — restart`, to run from a new cloud
session opened in the app, in Auto mode." Rewrap the lines to the paragraph's
existing width.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
change is one README sentence and cannot be split.

1. **Phase 1 — name the restart command in README Failure modes.**
   - Files: `README.md` (the "Failure modes" paragraph under
     "Claude issue implementer", around lines 1253-1269).
   - Done when: the paragraph contains `/claude-issue-pickup start — restart`
     in the watchdog sentence, the duplicated "pickup" is gone, and no other
     file changes.
   - Rollback: revert the phase PR.

## Implementation Steps

1. Phase 1 — `README.md` ~1255-1258: replace "means the pickup / pickup
   stopped" with "means the pickup stopped", and "with the restart / command."
   with the named command and its "new app session, Auto mode" instruction.

## Files & Modules

- `README.md`

## Tests

- `git diff --stat` shows only `README.md` (plus the plan and log the chain
  commits).
- `grep -n 'claude-issue-pickup start — restart' README.md` shows the new
  mention in the Failure modes paragraph.
- Run the repo's docs checks that cover README.md, if any (`python3 -m pytest
  tests/ -q -k readme`), and confirm they still pass.

## Risks & Mitigations

- The command text drifts from the watchdog message → the wording is copied
  from `scripts/claude_issue_queue_watchdog.sh:86`.

## Rollout

Docs only; takes effect on merge. Nothing to activate.

## Auto-decisions

- AD-1 [plan, 2026-09-26] Q1: How much context should accompany the named command? — Picked: A — the command plus "run from a new cloud session opened in the app, in Auto mode", as the Telegram alert says. Alternatives: B — the bare command only. Why: the pickup refuses to start more than 3 links deep, so the new-app-session instruction is what makes the command work; it mirrors `scripts/claude_issue_queue_watchdog.sh:86`. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-26] Q2: Fix the duplicated "pickup pickup" in the same sentence? — Picked: A — yes, since the sentence is being rewritten anyway. Alternatives: B — leave it (strict §5). Why: it is a typo inside the exact sentence the issue asks to change; README-only scope holds. Applied in: phase 1 PR. Status: pending review

## References

- Issue #4550
- `scripts/claude_issue_queue_watchdog.sh`
- `.claude/commands/claude-issue-pickup.md`
