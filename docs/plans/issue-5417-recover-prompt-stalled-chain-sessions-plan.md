# Recover chain sessions stuck on a permission prompt

Source issue: shubhodeep1/coding-workflows#5417 (https://github.com/shubhodeep1/coding-workflows/issues/5417)
Base branch: claude/implement-plan-claude-fixer-unattended-convergence
Security pass: run

## Summary

The hourly Claude issue pickup already lists sessions stuck on a permission prompt (#4648 phase 4, plan D12) but only reports them. This plan makes it recover the chain sessions among them: interrupt the stuck turn, rename the session `… — replaced (stuck on prompt)`, and start a fresh Opus 5.5 session that continues the same stage, issue, or fix, at most twice per stage (or per PR head for a fixer).

## Context

- Observed on #4786 (2026-09-29/30): the review-round stage session `session_01W3xS8M9M5SCd3EzFYUANxs` sat on a Bash prompt from 18:45Z. Its conformance-fix PR #5049 merged at 23:10Z, the hand-back went to the stuck session, and no stage ran for over 4 hours until the master session restarted it by hand.
- `.claude/scripts/stale_sessions.py` (`stalled_on_prompt`) and step 3a of `.claude/commands/claude-issue-pickup.md` are the janitor this extends (issue constraint: "extend the same janitor, don't add a second one"; CLAUDE.md §5).
- A hand-back Routine that `SUCCEEDED` in a session counts as delivered (CLAUDE.md §26.C step 5, `/implement-plan-claude` checker step 4b), but a session on a prompt only queues the wake behind the prompt.
- The operator's Q61: a session that sat on a prompt stays visible (it is never archived by this flow). Q40 / CLAUDE.md §28.C: `.claude/**` changes are delivered twin-first until #4785.

## Goals

1. `stale_sessions.py` reports a `recover` list on its final run: every `stalled_on_prompt` session whose wait exceeds `CLAUDE_PROMPT_STALL_RECOVER_MINUTES` (new env var, default `45`; flag `--prompt-stall-recover-minutes`) and whose title (after an optional `#<N> · ` and an optional `PR #<P> — ` prefix) is `implement-plan <slug> — <stage>`, `issue [<owner>/<repo>]#<N> — implement`, or `PR [<owner>/<repo>]#<N> — fix …`.
2. Each `recover` entry carries a deterministic `action`: `recover` (with the exact rename title, the fresh session's title, start prompt and trigger name, the triggers bound to the stuck session, and for a fixer the reservation claim to post), `cap_reached` (the third stall of the same stage, or a fixer head with 2 recoveries; `notify` is true once), or `skip` (with a reason).
3. Idempotent: a title ending `— replaced (stuck on prompt)` is never a candidate; a chain that moved on (a newer session of the same chain exists, or, for a fixer, the PR is terminal, held, claimed by another session, or has no fix due) is skipped.
4. Pickup step 3a acts on `recover` entries: the two-step Opus 5.5 high-effort start, then `interrupt_session`, the rename, and deleting the stuck session's bound triggers; it never archives the stuck session.
5. A fresh stage, issue, or fixer session continues without duplicating the stuck session's pushed work and without being stopped by its claim.
6. CLAUDE.md §26.C step 5 and `/implement-plan-claude`'s hand-back check say that a hand-back delivered to a session that then sits on a prompt is not a live fixer; README, `agents.md`, and one `changelog.d/` fragment (`added`) document the behaviour.
7. Tests cover title matching, the time threshold, the recovery cap, and idempotency.

## Non-goals

- Recovering checkers (`implement-plan <slug> — checker`, `— waiting: …`), `/deploy-activate` sessions, the pickup itself, operator sessions, or §26 pushing sessions with free-form titles (the §26.H sweep covers those once the claim lease ends).
- Detecting a prompt-blocked subscriber inside the §26 or project checker (the issue asks only for the doc statement there).
- Changing how the janitor archives sessions (`classify`), including the gap that it does not strip a `PR #<P> — ` title prefix (recorded under Notes).
- Answering or denying the prompt itself; the `ai:permission-prompt` issue for the command shape stays the long-term fix.

## Constraints

- §5: extend `stale_sessions.py` and pickup step 3a; no second janitor and no new script.
- §6: no rename or removal of existing identifiers, flags, output keys, or title formats; every new key is additive.
- §4: the new env var has a default (`45`).
- §15: titles and saved trigger pages decide stage and issue recoveries with no API call; a fixer candidate costs one cached PR read plus the comment pages and the hand-back reads `check_pr_hand_back` already makes, with the PR and comments passed in so neither is fetched twice. REST only.
- §18: runs from the existing hourly pickup trigger; nothing manual.
- §25: no PR subscription or polling; the pickup acts once per wake.
- §28.C / Q40: `.claude/**` edits go to the `workflow-templates/.claude/**` twins; `.claude/commands/claude-issue-pickup.md` has no twin, so its diff ships in the twin-sync blocker.
- Q61: the stuck session is interrupted and renamed, never archived; the resumed stage's resume hygiene must skip it too.

## Approach

**Detection (`stale_sessions.py`).** On the final run, after `stalled_on_prompt`, a new `prompt_stall_recoveries()` parses each stalled session's title with the recovery parser (strips `#<N> · ` and `PR #<P> — `, then matches one of the three chain forms; `fix` only, so a `fixed` or `on hold` fixer that already reported is left alone). Candidates are stalls with `minutes` above the recovery threshold. For each:

- `implement-plan` (key: repo, slug, stage) and `issue` (key: repo, issue number): skip when a newer session of the same chain exists (any later `created_at` with the same key, an issue's `issue-<N>-…` stage sessions included; checkers excluded). Count sessions whose title ends `— replaced (stuck on prompt)` with the same key; at 2 the action is `cap_reached`, otherwise `recover` with attempt = count + 1.
- `fixer` (key: repo, PR): skip when a newer fixer exists. Otherwise read the PR (cached) and its comments, skip a held head, then call `check_in_status.check_pr_hand_back` with the stuck session and every `sweep-run-*` / `prompt-stall-recovery-*` reservation on the head ignored; anything but `hand_back_fixer` is `skip`. `stall_recoveries` on the head at 2 is `cap_reached`.

A `recover` entry lists the bound enabled triggers, the rename title, and the start (title = the stuck title, prompt, trigger name `implement-plan <slug>: stage start` or `dispatch <repo>#<N>: start`, both swept by `stale_routines.py`). Stage prompt: a `— resume.` block with `Stage:` = the stalled stage, `Waiting on was: prompt stall in stage <stage> (session <id>)`, the recovery attempt, the checker found in the session list, and the safety-net and hand-back triggers bound to the stuck session. Issue prompt: `/implement-issue-claude <url> — recovered from <id> (prompt stall <k>/2)`. Fixer prompt: `/fix-claude-pr <url> — kind <kind> — head <sha> — claim prompt-stall-recovery-<suffix>`. `cap_reached` sets `notify` once per session and `updated_at` (`reported-recovery-caps.json` in the stall log dir).

**Claims (`check_in_status.py`).** A trusted claim whose `by` starts `prompt-stall-recovery-` ends every earlier claim on the same head (it replaces a stuck session), and `read_fix_claims` reports `stall_recoveries`, the number of distinct such claimants on the current head. `check_pr_hand_back` gains optional `pr` / `comments` parameters so the janitor's prefetched reads are reused. Nothing else changes for existing claimants.

**Pickup step 3a.** For `recover`: start the replacement first (two-step start; for a fixer, post the reservation claim with `claude_fix_claim.py` first), then `interrupt_session`, `set_session_title` with `replaced_title`, and `delete_trigger` on each `bound_triggers` id. A failed start leaves the stuck session untouched for the next wake. `cap_reached` with `notify`: one `PushNotification`. Report adds `recovered <r>`.

**Resumed sessions.** `/implement-plan-claude` gains a "Prompt-stall recovery" section: never archive the replaced session, record the recovery in `## Notes`, ignore its claim, and look for its pushed branch, open PR, or dispatched run before redoing anything. `/implement-issue-claude` passes `— recovered from …` on and edits an existing progress comment instead of posting a second. `/fix-claude-pr` documents the reservation hint.

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the detector, the pickup wiring, the claim rule, and the resumed sessions' preflight only work together, and the whole change lands on the integration branch with #4648.

1. **Phase 1 — prompt-stall recovery.** Files: see Files & Modules. Done when the new tests pass against the twins, the existing suites stay green apart from the parity and pickup-wiring tests that wait for the `[claude-twin-sync]` copy, and the docs describe the behaviour. Rollback: revert the phase PR; the janitor then only reports stalls again. Protected paths: `.claude/scripts/stale_sessions.py`, `.claude/scripts/check_in_status.py`, `.claude/commands/{implement-plan-claude,implement-issue-claude,fix-claude-pr,claude-issue-pickup}.md`, `.claude/settings.json` (twin-first).

## Implementation Steps

1. `workflow-templates/.claude/scripts/check_in_status.py`: recovery-claim prefix constant, supersede rule and `stall_recoveries` in `read_fix_claims`, optional `pr` / `comments` in `check_pr_hand_back`; docstrings.
2. `workflow-templates/.claude/scripts/stale_sessions.py`: recovery parser, threshold (env + flag), `prompt_stall_recoveries()`, cap-notify state, output key `recover`, docstring.
3. `workflow-templates/.claude/settings.json`: allow `interrupt_session` for both claude-code-remote server names.
4. `workflow-templates/.claude/commands/implement-plan-claude.md`: Prompt-stall recovery section, resume hygiene exception, Claims ignore, hand-back check note.
5. `workflow-templates/.claude/commands/implement-issue-claude.md` and `fix-claude-pr.md`: recovery arguments.
6. `.claude/commands/claude-issue-pickup.md` (no twin): step 3a recovery sub-step, report, rules, tool access; shipped as a diff in the twin-sync blocker.
7. `CLAUDE.md` §26.C step 5 (and the §26.B instructions restating it), `README.md`, `agents.md`, `changelog.d/5417-recover-prompt-stalled-chain-sessions.md`.
8. Tests in `tests/test_stale_sessions.py`.

## Files & Modules

- `workflow-templates/.claude/scripts/stale_sessions.py`
- `workflow-templates/.claude/scripts/check_in_status.py`
- `workflow-templates/.claude/settings.json`
- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `workflow-templates/.claude/commands/implement-issue-claude.md`
- `workflow-templates/.claude/commands/fix-claude-pr.md`
- `.claude/commands/claude-issue-pickup.md` (diff in the twin-sync blocker)
- `CLAUDE.md`, `README.md`, `agents.md`
- `changelog.d/5417-recover-prompt-stalled-chain-sessions.md` [new]
- `tests/test_stale_sessions.py`
- `docs/plans/issue-5417-recover-prompt-stalled-chain-sessions-plan.md` [new], `docs/implement-plan/issue-5417-recover-prompt-stalled-chain-sessions.md` [new]

## Tests

Unit tests (pytest, `tests/test_stale_sessions.py`, already its own `ci.yml` step), loading the twins:
- title matching: the three chain forms with and without `#<N> · ` / `PR #<P> — ` prefixes; checkers, `waiting:`, `deploy-activate`, `fixed` / `on hold`, and unrelated titles are never candidates;
- threshold: 45-minute default, the env var and the flag, no candidate at or below it, none on an intermediate page run;
- cap: 0 and 1 replaced sessions recover with attempt 1 and 2, 2 give `cap_reached` with `notify` once; fixer cap from `stall_recoveries`;
- idempotency: a replaced title is skipped, a newer stage / issue / fixer session skips the stuck one, a fixer whose PR is merged, held, claimed by another session, or has no fix due is skipped;
- the resume block, prompts, trigger names, and bound triggers are exact;
- `read_fix_claims`: a recovery reservation ends earlier claims on the head, is counted per head, and leaves other heads and existing claimants unchanged;
- wiring: the pickup runs recovery in step 3a, settings allow `interrupt_session`, the commands document the recovery arguments.

## Risks & Mitigations

- Interrupting a session a human is about to answer → only the three chain title forms, after 45 minutes; the session stays visible and renamed. ACCEPTED — the operator wants these chains unattended.
- `interrupt_session` fails and the old turn later runs alongside the replacement → the pickup reports it and sends one `PushNotification`; the fixer reservation and the stage's own claim checks keep the old session off a claimed head.
- The replaced session's unpushed work is lost → it ran in another container; the fresh session redoes the stage from what was pushed.
- Uncommitted auto-decisions carried only in the stuck session's `— resume.` block are lost → ACCEPTED; rare, and the resumed stage records its own.
- Cap counted by stage title means review rounds of one phase share the budget → ACCEPTED (AD-8): the same command shape keeps prompting.
- #4887's project (`claude/implement-plan-issue-4887-archive-finished-sessions`, PR #4924) also edits `claude-issue-pickup.md` step 3a → this plan adds one sub-step and keeps every existing line, so whichever lands second merges both; the conflict resolution keeps both sides (CLAUDE.md §12).

## Rollout

Lands on `claude/implement-plan-claude-fixer-unattended-convergence` and reaches `main` with #4648. The pickup reads `main` on every wake, so recovery starts on the first wake after that merge. `CLAUDE_PROMPT_STALL_RECOVER_MINUTES` needs no setting (default 45). Consumer repos receive the `.claude/` changes on the next `@stable` sync; the pickup itself runs only in coding-workflows. Rollback: revert the PR.

## Auto-decisions

- AD-1 [plan, 2026-10-01] How does the janitor tell that a chain already moved on? — Picked: A — a newer session of the same chain in the session list (and, for a fixer, the live PR state). Alternatives: B — compare the project branch log's `Stage:` with the stalled stage; C — both. Why: the log on the project branch lags (each PR carries its own copy) and stage names have no total order, while every later stage is a newer session titled with the slug; no API call. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-10-01] Which stage does the replacement run? — Picked: A — the stalled stage itself, with a recovery preflight that reuses the stuck session's pushed branch, open PR, or dispatched run. Alternatives: B — the pickup derives the next stage from the log. Why: each stage already re-verifies against GitHub, and arming the wait on an open PR lets the checker route to the real next stage. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-10-01] How is an issue implement session requeued? — Picked: A — the pickup starts a fresh `/implement-issue-claude` session itself with the step 3 start. Alternatives: B — comment `/reclarify` on the issue. Why: same end state without an hour's wait or a comment that starts `issue_comment` workflows. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-10-01] In which order does the pickup act? — Picked: A — start the replacement, then interrupt, rename, and delete bound triggers. Alternatives: B — interrupt and rename first. Why: a failed start then leaves the stuck session for the next wake instead of stranding the chain; the replacement's first prompt arrives two minutes later, after the interrupt. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-10-01] How are fixer recoveries recorded and counted per head? — Picked: A — a reservation claim `prompt-stall-recovery-<suffix>` posted with `claude_fix_claim.py`, which ends earlier claims on the head and is counted as `stall_recoveries`. Alternatives: B — a separate marker comment plus a growing ignore list for the fresh fixer; C — count per PR from session titles. Why: one comment is the record, the count, and the hand-off, and the sweep and checker leave the head alone meanwhile. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-10-01] Which `implement-plan` and fixer titles are excluded? — Picked: A — checkers, `waiting:` sessions, `deploy-activate`, and fixers titled `fixed` / `on hold`. Alternatives: B — every `implement-plan <slug> — …` title. Why: a checker has no stage to resume, `/deploy-activate` waits on a human, and a `fixed` / `on hold` fixer already reported. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-10-01] Should the archive classifier also learn the `PR #<P> — ` prefix? — Picked: A — no; only the recovery parser strips it, and the gap is recorded under Notes. Alternatives: B — fix `classify_title` too. Why: §5, archiving behaviour is outside this issue. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-10-01] What is "the same stage" for the cap? — Picked: A — the stage text of the title, so every review round of a phase shares one budget. Alternatives: B — scope the count to sessions newer than the last non-replaced stage. Why: deterministic from titles, and a stage that keeps prompting needs a human either way. Applied in: phase 1. Status: pending review
- AD-9 [plan, 2026-10-01] What happens to the stuck session's Routines? — Picked: A — the pickup deletes every enabled trigger bound to it after the interrupt (the resume block also names them). Alternatives: B — leave them. Why: a later safety-net or hand-back fire would wake the interrupted session and race the replacement. Applied in: phase 1. Status: pending review

## Notes

- Out of scope, found while reading: `stale_sessions.classify_title` strips only a `#<N> · ` prefix, so on a branch that has `/implement-plan-claude`'s numbers-first session titles (`#<N> · PR #<P> — implement-plan …`) those sessions count as `not_ours` and are never archived.

## References

- #5417, #4786, #4648 (plan `docs/plans/claude-fixer-unattended-convergence-plan.md`, D7, D12), #4887, #4785, PR #5215.
