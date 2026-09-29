# Protected-path phases: apply the twin-first rule automatically (interim until #4785)

Source issue: shubhodeep1/coding-workflows#4948 (https://github.com/shubhodeep1/coding-workflows/issues/4948)
Base branch: main
Security pass: run

## Summary

A `/implement-plan-claude` (or `/implement-issue-claude`) phase that touches `.claude/**` stops twice for a human today: once before it starts, to ask how it should run, and once after it ships, for the `[claude-twin-sync]` copy. The first stop always gets the operator's standing answer Q40: A (twin-first). This plan makes the chain record that answer itself and run the phase twin-first, so only the sync stop remains until #4785's Actions sync lands.

## Context

- `.claude/commands/implement-plan-claude.md` step 4 ("Protected paths stop the phase before it starts") sets `Status: BLOCKED` and asks Q with options A (watched session), B (drop the `.claude/` part) and C (try unattended). Step 3 marks such phases `protected paths: <paths>`; the phase starts only under a `Protected-path approval: phase <n> — <answer> (<date>)` line in the log's `## Notes`.
- The operator's standing decision Q40: A (`docs/operations/master-session.md`) is the interim twin-first rule: edit only the `workflow-templates/.claude/**` twins, post a `hold` claim, stop `BLOCKED`; the master copies the twins into `.claude/` as `[claude-twin-sync]`, runs the tests, pushes, and comments `/reclarify`. The master also answers every stop-1 question with a `Protected-path approval: phase N — twin-first per Q40 (<date>)` line. Examples on 2026-09-29: #4858 (blocked 00:42, answered 01:00, sync stop 01:51), #4891, #4817, #4755, #4750.
- #4785 (final PR #4804, phase PR #4807, both open) is the lasting fix: twin-first becomes the documented default and `claude-twin-sync.yml` (`scripts/claude_twin_sync.py`) lands the root copies through a sync PR. It rewrites the step 3 bullet, the first sentence of the step 4 stop, and the CLAUDE.md §28.C "Protected-path edits" bullet.
- CLAUDE.md §28.C lists protected-path edits as never auto-decided; the change here names an interim automatic default there. `workflow-templates/CLAUDE.md` is a symlink to `CLAUDE.md`.
- This change edits `.claude/commands/implement-plan-claude.md`, a protected path, so it lands twin-first itself (operator context of the dispatch: `Protected-path approval: phase 1 — twin-first per Q40 (2026-09-29)`).

## Goals

- A new project whose phase is marked `protected paths:` and whose log has no `Protected-path approval: phase <n>` line records `Protected-path approval: phase <n> — twin-first (automatic, interim until #4785) (<date>)` and implements the phase twin-first, without the stop-1 question, in a repo that has `workflow-templates/.claude/`.
- Twin-first means: only `workflow-templates/.claude/**` twins are edited; a `.claude/` path with no twin is not edited, and its exact diff plus the sha256 of the resulting file go into the sync blocker; after the phase PR opens, the stage posts a `hold` claim on its head and the twin-sync blocker (stop 2, unchanged) instead of arming the wait.
- The A/B/C question still appears for an edit that is denied even in the twin tree and for a phase whose plan says it needs a watched session; a recorded answer other than twin-first is never overwritten by the automatic default.
- CLAUDE.md §28.C names the automatic twin-first default and its sunset (#4785).
- The `Protected-path approval:` line format and its readers are unchanged (§6); the new value is an addition.
- The removal trigger is recorded and enforced: a test fails once #4785's `scripts/claude_twin_sync.py` exists while the interim text remains.

## Non-goals

- The Actions sync workflow itself (#4785).
- Removing stop 2 (the `[claude-twin-sync]` wait); it stays until #4785.
- Changing how the master performs twin syncs (Q40 runbook), beyond noting that stages now record the approval themselves.
- Consumer repos: they have no `workflow-templates/.claude/` twin tree, so the question stays as it is there.

## Constraints

- §6: `Protected-path approval: phase <n> — <answer> (<date>)` is unchanged; `twin-first (automatic, interim until #4785)` is a new `<answer>` value.
- §28.C: protected-path edits stay out of the general auto-decision scope; only this named interim default is added.
- §5: add text next to the existing stop instead of rewriting the lines #4785 rewrites, so the two changes merge in either order without a textual conflict.
- §20: one `changelog.d/` fragment.
- Protected paths: the command change is made in `workflow-templates/.claude/commands/implement-plan-claude.md` only (twin-first); the root copy lands through `[claude-twin-sync]`.

## Approach

Add one paragraph to step 4, right after the existing A/B/C question, headed "Interim automatic default: twin-first (until #4785)". It applies in a repo with `workflow-templates/.claude/` when the log has no `Protected-path approval: phase <n>` line and the plan does not say the phase needs a watched session: the stage records the automatic approval line and proceeds twin-first, then after step 6 posts the `hold` claim and the twin-sync blocker and ends `BLOCKED`; on `/reclarify` the resumed stage arms the wait on that PR. The paragraph lists the three cases that still reach the question. Add a matching bullet at the end of the CLAUDE.md §28.C list, separate from the "Protected-path edits" bullet #4785 rewrites. Tests read the twin so they pass before the sync. A sunset test fails once `scripts/claude_twin_sync.py` exists while the interim text is still there, which makes #4785 (or whichever PR lands the sync) remove it.

Alternative considered: rewriting the step 4 stop sentence and the §28.C bullet in place. Rejected (AD-3): #4785 rewrites the same lines, so every merge order would conflict.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue is one change to one rule.

1. **Phase 1 — interim twin-first default** — protected paths: `.claude/commands/implement-plan-claude.md` (edited only in its `workflow-templates/.claude/` twin, operator Q40). Files: the twin command, `CLAUDE.md` (§28.C), `agents.md`, `docs/operations/master-session.md`, `tests/test_implement_plan_claude_command.py`, `changelog.d/4948-protected-path-twin-first-default.md`. Done: the new tests pass against the twin and CLAUDE.md, every other existing test passes except `test_template_parity` (red until the `[claude-twin-sync]` copy, then green). Rollback: revert the phase PR; the question returns.

## Implementation Steps

1. `workflow-templates/.claude/commands/implement-plan-claude.md` step 4: after `Record the answer as … Never retry a blocked protected-path edit in a loop.`, add the interim-default paragraph (automatic approval line, twin-first rules, no-twin diff + sha256, hold claim + twin-sync blocker after step 6, resume behaviour, later stages of the project, the three question cases, sunset).
2. `CLAUDE.md` §28.C: add a bullet after "Whether to run the chain at all" naming the interim automatic twin-first default, the cases that still ask, and its sunset with #4785.
3. `agents.md` (§23.I section, protected-path sentence): add the interim default.
4. `docs/operations/master-session.md` Q40 row: note that stages now record the approval themselves (AD-6).
5. `tests/test_implement_plan_claude_command.py`: tests for the command twin text (automatic approval line, the three question cases, hold + blocker, §6 format), the CLAUDE.md wording, and the sunset guard.
6. `changelog.d/4948-protected-path-twin-first-default.md`.

## Files & Modules

- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `.claude/commands/implement-plan-claude.md` (via `[claude-twin-sync]` only, not by this stage)
- `CLAUDE.md` (and `workflow-templates/CLAUDE.md`, a symlink)
- `agents.md`
- `docs/operations/master-session.md`
- `tests/test_implement_plan_claude_command.py`
- `changelog.d/4948-protected-path-twin-first-default.md` [new]

## Tests

- Unit (text contract), in `tests/test_implement_plan_claude_command.py`, reading the twin: the automatic approval line and its value; twin-only edits; no-twin diff + sha256; hold claim and twin-sync blocker; the three cases that still ask; `workflow-templates/.claude/` scope; the approval line format unchanged.
- CLAUDE.md §28.C wording test.
- Sunset guard: fails when `scripts/claude_twin_sync.py` exists and the interim default text is still in the twin or CLAUDE.md.
- Full `tests/` run; `test_template_parity` is expected red until the sync.

## Risks & Mitigations

- The chain edits twins that the master must review before they reach `.claude/` — ACCEPTED: stop 2 is kept for exactly that review.
- #4785 merges first and its sync makes twin-first the default — mitigated by the sunset test, which fails until the interim text is removed.
- A textual conflict with #4785 — mitigated by adding new text next to, not inside, the lines #4785 rewrites (AD-3).
- A session misreads a recorded non-twin-first answer as "no approval" — mitigated by stating that any recorded answer stands (AD-2).

## Rollout

Lands on `main` through the project's final PR, after the `[claude-twin-sync]` copy of the command. Consumers receive the twin on the next `@stable` sync, where the default does not apply (no `workflow-templates/.claude/`). **Removal trigger:** the PR that makes #4785's sync live (it adds `scripts/claude_twin_sync.py`) removes the interim paragraph, the §28.C bullet, the agents.md sentence, and the sunset test's subject text; the sunset test fails that PR until it does.

## References

- #4948 (this issue), #4785 (lasting fix; PRs #4804, #4807), #4858, #4891, #4817, #4755, #4750
- `docs/operations/master-session.md` (Q40: A)

## Auto-decisions

- AD-1 [plan, 2026-09-29] Where does the automatic default apply? — Picked: A — only in a repo that has `workflow-templates/.claude/` (coding-workflows). Alternatives: B — every repo that receives the command. Why: consumers have no twin tree, so twin-first cannot run there and the question must stay. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What happens when the log already records a different `Protected-path approval:` answer? — Picked: A — the recorded answer stands and the automatic default never overwrites it; the phase runs under that answer as before, and a `.claude/**` edit that answer cannot carry out still reaches the question. Alternatives: B — ask the A/B/C question whenever a non-twin-first answer is recorded. Why: B re-asks after every human answer, so a `/reclarify` would loop on the same question. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Rewrite the existing stop and §28.C bullet, or add text beside them? — Picked: A — add a new paragraph after the step 4 question and a new §28.C bullet after "Whether to run the chain at all". Alternatives: B — rewrite the lines in place. Why: #4785 rewrites those lines; A merges with it in either order (§5). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] How is the removal trigger enforced? — Picked: A — a test that fails once `scripts/claude_twin_sync.py` (added by #4785) exists while the interim text remains, plus this plan's Rollout note and one comment on #4785. Alternatives: B — the plan note only. Why: a failing check is what makes "#4785 removes it" happen without anyone remembering (§18). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] Do later stages of the project (review round, fix PR, intervention) that must change `.claude/**` follow the default? — Picked: A — yes, under the phase's recorded approval; a push that changes a twin posts the hold claim and the twin-sync blocker the same way. Alternatives: B — leave later stages unspecified. Why: without it a review round on such a phase would stop again for the same question. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Update the Q40 row in `docs/operations/master-session.md`? — Picked: A — one sentence saying stages now record the approval themselves and the master answers stop 1 only for the remaining cases. Alternatives: B — leave it. Why: the row would otherwise tell the master to keep answering a question that no longer appears. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] How is "a phase whose plan explicitly needs a watched session" recognised? — Picked: A — the plan's phase text says the phase must run in a watched session (for example `watched session: required`). Alternatives: B — infer it from the files (for example hooks or `settings.json`). Why: hook and `settings.json` twins can be edited unattended; only their sync needs the operator (Q62/Q64), which stop 2 already covers. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py` printed `{"skip": false, "label": null, "reason": "no skip label"}`, so `Security pass: run`.
