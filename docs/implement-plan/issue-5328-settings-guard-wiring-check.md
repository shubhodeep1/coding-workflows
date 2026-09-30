# Implement-Plan Log — Guard differential check: fail closed on settings-only guard disablement

- Plan: docs/plans/issue-5328-settings-guard-wiring-check-plan.md
- Source issue: shubhodeep1/coding-workflows#5328   Progress comment: 5902373778
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-5174-guard-differential-check
- Project branch: claude/implement-plan-issue-5328-settings-guard-wiring-check   Final PR: (opening)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened from the issue base; phase 1 starting.

## Phases
1. [ ] Phase 1 — settings guard-wiring check (`scripts/guard_differential.py`, tests, `ci.yml` step comment, `agents.md`, changelog)

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which settings entries are "mandatory" guard wiring? — Picked: A — every hook entry, under any event, whose command names `.claude/hooks/<name>_guard.py`, plus the `disableAllHooks` kill switch. Alternatives: B — `PreToolUse` entries only; C — every hook entry, guards or not. Why: covers the finding and the one-key kill switch of the same class without failing every harmless non-guard hook edit (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What verifies that a changed guard command still executes the guard? — Picked: A — only the exact canonical form `python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/<hook>.py` with that hook file present at the head, a covering matcher, no lower timeout, and all other keys equal; everything else fails closed. Alternatives: B — run the head's wired command through a shell against the corpus and accept matching outcomes; C — fail every change with no verification. Why: §1; B passes a command that runs the guard only under CI, while A is verifiable by construction. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How does an intended wiring change pass? — Picked: A — list its printed identity (`settings:<file>:<event>:<matcher>:<hook>`) under the existing `Intended loosening:` section, where it counts as loosening. Alternatives: B — no escape hatch. Why: reuses the #5174 mechanism, and the operator still sees it. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Added non-guard hooks that could auto-allow and `permissions.*` rule changes? — Picked: A — out of scope, recorded under Notes. Alternatives: B — fail every added hook entry in this PR. Why: §5; the finding names changed guard matchers and commands, and retire-master phase 3 classifies rule diffs. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] An unparseable head settings file? — Picked: A — a wiring regression (exit 1); an unparseable base contributes no wiring. Alternatives: B — a setup error (exit 2). Why: fail closed on the loosening side while keeping exit 2 for bad refs. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- 2026-09-30: started by the Claude issue dispatcher (`/implement-issue-claude`), session session_01JGsmyk8gEMSXFh6ocygH4q. The issue base is the #5174 project branch, so the final PR targets it and activation is n/a.
- Protected paths: none. Phase 1 edits no `.claude/**` file.
