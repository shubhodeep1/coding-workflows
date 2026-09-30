# Implement-Plan Log — Guard differential check: fail closed on settings-only guard disablement

- Plan: docs/plans/issue-5328-settings-guard-wiring-check-plan.md
- Source issue: shubhodeep1/coding-workflows#5328   Progress comment: 5902373778
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-5174-guard-differential-check
- Project branch: claude/implement-plan-issue-5328-settings-guard-wiring-check   Final PR: #5356 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5369
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01DZdvBhT9Qf3x3SXQfb4Ugi (project checker, reused); safety net and hand-back ids in the stage report
- Last updated: 2026-09-30
- Last note: review round 2 (session session_01W4tTj8UKhKRzuJFJCcxA44): the one finding (3 ledger entries, NIT/low: a redundant `reason=env` line when a settings file is deleted) rejected; the env line is the only regression for a deleted env-only file, now pinned by `test_cli_a_deleted_settings_file_with_only_env_fails`. No verdict bot, so no verdict posted; the round's push is the project-branch sync (cba1f90, 4 doc-only commits from the issue base) merged into the phase branch, the pin test, and this log (AD-8). tests/test_guard_differential.py 111 passed.

## Phases
1. [ ] Phase 1 — settings guard-wiring check (`scripts/guard_differential.py`, tests, `ci.yml` step comment, `agents.md`, changelog)   — PR #5369 open (waiting); review rounds: 2; interventions: 0

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
- AD-6 [phase 1/1, 2026-09-30] A settings `env` change reaches every hook process (`CLAUDE_PR_MERGE_GUARD=off`, a `PATH` that shadows `python3`) while the guard command stays canonical. Cover it? — Picked: A — fail closed on any change to the top-level `env` object (identity `settings:<file>:env`). Alternatives: B — a denylist of sensitive keys; C — leave it out of scope. Why: §1; it is the same settings-only guard disablement class, neither settings file has ever had an `env` key, so the rule costs nothing, and a denylist is porous. Applied in: phase 1 PR. Status: pending review
- AD-7 [phase 1/1 — review round 1, 2026-09-30] The reviewer panel's consensus finding says the skip line `GUARD_DIFFERENTIAL status=skipped reason=no-hook-change` is narrower than the gate, which also covers the settings files. How to fix it? — Picked: A — keep `reason=no-hook-change` and add `checked=hooks,settings` to the line. Alternatives: B — rename the value to `reason=no-hook-or-settings-change`; C — reject the finding. Why: §6 keeps the existing log value for anyone who greps it, while the new field states the gate's full scope; C would leave a valid finding unfixed with no verdict bot configured. Applied in: PR #5369. Status: pending review
- AD-8 [phase 1/1 — review round 2, 2026-09-30] Round 2's only finding (a `reason=env` line beside the `removed` lines when a settings file is deleted, called redundant) is invalid, and no `CLAUDE_FIXER_VERDICT_BOT_LOGIN` is configured to post the verdict. How does the round close? — Picked: A — reject it in a PR reply, pin the env-only deletion case with a test, and push a new head that carries the due project-branch sync merge, which starts round 3. Alternatives: B — post a hold claim and stop BLOCKED on the issue now; C — drop the env line when the head file is deleted. Why: C is fail-open (a deleted env-only file would pass with no regression); A follows the issue-4886 AD-12 and issue-4707 precedent, the merge is due after the step 2 sync, and a round 3 that repeats the rejected finding stops BLOCKED (B) then. Applied in: PR #5369. Status: pending review

## Lessons
- [source:security] A coverage gate that triggers on changed code files must also trigger on the config that wires that code: a settings-only change can disable a hook while every file-based check stays skipped, and a substring wiring test accepts a command that merely names the hook. (files: scripts/guard_differential.py, .claude/settings.json)
- [source:intervention] A fail-closed line that shares an input with other checks (a settings `env` change on a deleted file) reads as a duplicate to reviewers whenever it co-occurs with them; pin the case where it is the only line with a test so the next round cannot argue it away. (files: scripts/guard_differential.py, tests/test_guard_differential.py)

## Notes
- 2026-09-30: started by the Claude issue dispatcher (`/implement-issue-claude`), session session_01JGsmyk8gEMSXFh6ocygH4q. The issue base is the #5174 project branch, so the final PR targets it and activation is n/a.
- Protected paths: none. Phase 1 edits no `.claude/**` file.
