# Unattended stages skip a denied cleanup call instead of retrying it

Source issue: shubhodeep1/coding-workflows#5068 (https://github.com/shubhodeep1/coding-workflows/issues/5068)
Base branch: main
Security pass: run

## Summary

Unattended stage, checker, fixer, and pickup sessions retry housekeeping calls (`delete_trigger`, `archive_session`, `set_session_title`) that the Auto-mode classifier refused. After three consecutive refusals Claude Code stops asking the classifier and waits for a human who is not there. This plan makes every such flow skip a denied cleanup call, record `cleanup skipped: <tool> denied (<reason>)`, and carry on with the stage's real work. It also scopes each targeted `delete_trigger` to the flow's own Routine first, so the classifier can see it is not another workload.

## Context

- 2026-09-29: the #4755 resume stage (`session_019PaJAyxWrJjeWb93aYofLY`, conformance 1/3) stopped for more than an hour at `Allow Claude to use delete trigger? — 3 consecutive actions were blocked … [Interfere With Workloads] — Trigger ID trig_012QFMb1nQkFFs1JyPsnZVsE`. It was doing `/implement-plan-claude` step 0 resume hygiene ("Delete the `Safety-net trigger` and the `Hand-back trigger` it names"), then the zombie-checker cleanup. The trigger no longer existed, so the call would have done nothing.
- No command text says what to do with a refused cleanup call, so the session retried until the third refusal.
- Every leftover the cleanup removes is also handled elsewhere: an archived session's triggers auto-disable (`auto_disabled_session_gone`), the stale Routine sweep (`.claude/scripts/stale_routines.py`, CLAUDE.md §26.G) deletes ended ones, and a reused checker ignores stale waits (checker prompt step 0).
- Cleanup call sites today:
  - `.claude/commands/implement-plan-claude.md`: step 0 resume hygiene (archive the previous stage, delete the safety-net and hand-back triggers), Zombie-checker cleanup (archive stray checkers), Arming the wait step 1 (archive a replaced checker) and step 2 (delete stale check-ins), the two-step start's archive of a session whose start trigger failed, Hand-back steps 1–2 (delete the safety net, rename), steps 12–13 (archive the checker, archive self), and the checker prompt's step 4b (delete the hand-back trigger).
  - `.claude/commands/fix-claude-pr.md` step 8 (rename).
  - `.claude/commands/claude-issue-pickup.md` step 1 (delete other pickups' triggers, archive their sessions), step 4 (rename), step 5.3 (archive a checker nobody instructs). This file has no `workflow-templates/` twin.
  - CLAUDE.md §26.C step 5 (rename; terminal fallback deletes the fixer's Routine), §26.D (the fixer renames and archives the checker, then deletes the fired Routine; the pushing session renames itself), §26.G (the sweep's deletes).
- Protected paths: `.claude/commands/*` are protected. The issue (owner-authored spec) says to land them twin-first per Q40 / #4948: edit only `workflow-templates/.claude/**`; the root copies land through a `[claude-twin-sync]` commit by the supervising session. `CLAUDE.md` is not protected; `workflow-templates/CLAUDE.md` is a symlink to it.
- `get_trigger` is already allow-listed in `.claude/settings.json` (both server names), so the new pre-delete read needs no settings change.

## Goals

- A cleanup call denied by the Auto-mode classifier (or a permission rule) is never retried, in every flow listed above. After the first denial in a step, the step's remaining cleanup calls are skipped, `cleanup skipped: <tool> denied (<reason>)` goes into the stage report (and the progress log where the flow has one), and the stage continues its real work.
- Only essential calls (starting the next stage or a checker, arming or renewing a wait) may be retried, at most once.
- Before a `delete_trigger` on a trigger named by id, the flow reads it with `get_trigger` and deletes it only when its `name` is one this flow creates for this project or PR (`implement-plan <slug>: …`, `PR #<n> …`) and its `persistent_session_id` is a session of that project or PR; not found counts as already done. The text before the call says so.
- The rule is defined once, in a new CLAUDE.md §26.I, and each command states it where its cleanup happens.
- Tests assert the rule is present in each flow; a `changelog.d/` fragment describes it.

## Non-goals

- Changing what gets cleaned up, or when.
- The stale Routine sweep script's selection rules (`stale_routines.py`).
- `settings.json` / hooks (no permission changes).
- `/claude-issue-dispatch` (not named in the issue; its one archive call is not retried today).
- #4755 (unreported blocking prompts), #5018 (classifier refusals of master-answered resumes), #4910 (checker liveness).

## Constraints

- §5: add text next to the existing cleanup sentences; do not rewrite unrelated text or text #4948 (PR #5008) and #4785 rewrite (step 4 protected paths, §28.C).
- §6: no identifier renames. The existing `Zombie checkers archived: <n>` report key, trigger names, and session titles are unchanged; `cleanup skipped: …` is a new report phrase.
- §6 section numbers: §26.I is a new subsection after §26.H; nothing is renumbered.
- §15: the ownership check uses the fields a `list_triggers` result already carries when the trigger came from one; `get_trigger` is one extra read only for a trigger named by id.
- §20: one `changelog.d/` fragment.
- §25 / §26: no watching; the check-in flows keep their order (the fixer archives the checker before deleting the fired Routine; a skipped archive skips the delete too).
- Protected paths (Q40, issue spec): `.claude/**` is edited only through `workflow-templates/.claude/**` twins; `.claude/commands/claude-issue-pickup.md` (no twin) goes into the twin-sync blocker as an exact diff with the resulting file's sha256.

## Approach

Add CLAUDE.md §26.I, "Denied cleanup calls are skipped, never retried", which lists the cleanup calls, the skip-and-record rule, the essential-call exception, and the ownership check before a targeted delete, with the #4755/#5068 incident. Each flow then gets one short sentence at its cleanup site that applies §26.I there (so a session following only the command still sees it), and a Rules bullet in each command. The checker prompt, which a Sonnet checker follows with no other context, gets the rule inline. The fix-claude-pr and implement-plan-claude changes go into their twins; the pickup change is delivered as an exact diff for the sync. Tests read the twins and CLAUDE.md, so they pass before the sync, except the pickup test and the existing template-parity tests, which turn green with the sync.

Alternative considered: stating the whole rule in each command only. Rejected (AD-1): the §26 flows live in CLAUDE.md, and one definition keeps the six call sites from drifting.

## Phases & Merge Strategy

One phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the issue is one rule applied to the flows that clean up.

1. **Phase 1 — skip denied cleanup calls** — protected paths: `.claude/commands/implement-plan-claude.md`, `.claude/commands/fix-claude-pr.md`, `.claude/commands/claude-issue-pickup.md` (edited only through the `workflow-templates/.claude/` twins, or, for the pickup, an exact diff in the sync blocker; Q40 per the issue). Files: `CLAUDE.md` (§26.I, cross-references in §26.C/§26.D/§26.G), the two command twins, `agents.md`, three test files, `changelog.d/5068-skip-denied-cleanup-calls.md`. Done: the new tests pass against the twins and CLAUDE.md, and every other test passes except `test_template_parity` for the two commands and the pickup test, which are red until the `[claude-twin-sync]` copy and green after it. Rollback: revert the phase PR; the flows lose the rule and behave as today.

## Implementation Steps

1. `CLAUDE.md`: add `### I) Denied cleanup calls are skipped, never retried` after §26.H; add a pointer to §26.I in §26.C step 5 (terminal fallback delete, rename), §26.D (the fixer's rename/archive/delete, the pushing session's rename), and §26.G (the sweep's deletes).
2. `workflow-templates/.claude/commands/implement-plan-claude.md`: apply §26.I in step 0 resume hygiene (with the `get_trigger` ownership check), the Zombie-checker cleanup, Arming the wait steps 1–2, the two-step start's archive, Hand-back steps 1–2, steps 12–13, and the checker prompt step 4b; add a Rules bullet; add `cleanup skipped` to the Output Format and the log's `Last note`.
3. `workflow-templates/.claude/commands/fix-claude-pr.md`: step 8 rename and a Rules bullet.
4. `.claude/commands/claude-issue-pickup.md` (no twin): step 1 deletes/archives, step 4 rename, step 5.3 archive, and a Rules bullet — written as an exact diff plus the resulting file's sha256 for the sync blocker, not edited by the stage.
5. `agents.md`: a bullet after the stale Routine sweep bullet documenting §26.I.
6. Tests: `tests/test_implement_plan_claude_command.py` (twin + CLAUDE.md), `tests/test_check_in_status_hand_back.py` (fix-claude-pr twin), `tests/test_implement_issue_claude_command.py` (pickup, root file).
7. `changelog.d/5068-skip-denied-cleanup-calls.md`.

## Files & Modules

- `CLAUDE.md` (and `workflow-templates/CLAUDE.md`, a symlink)
- `workflow-templates/.claude/commands/implement-plan-claude.md`
- `workflow-templates/.claude/commands/fix-claude-pr.md`
- `.claude/commands/implement-plan-claude.md`, `.claude/commands/fix-claude-pr.md`, `.claude/commands/claude-issue-pickup.md` (via `[claude-twin-sync]` only, not by this stage)
- `agents.md`
- `tests/test_implement_plan_claude_command.py`
- `tests/test_check_in_status_hand_back.py`
- `tests/test_implement_issue_claude_command.py`
- `changelog.d/5068-skip-denied-cleanup-calls.md` [new]

## Tests

- Unit (text contract), reading the twins: each implement-plan-claude cleanup site (resume hygiene, zombie cleanup, arming the wait, two-step start, hand-back, steps 12–13, checker prompt) states the skip rule; resume hygiene states the `get_trigger` ownership check and not-found-is-done; the Rules bullet and the essential-call exception are present; fix-claude-pr step 8 and Rules state it.
- CLAUDE.md §26.I exists after §26.H, carries the rule, the ownership check, the essential-call exception, and the incident; §26.C/§26.D/§26.G point to it; section numbering test still passes.
- Pickup: the root file states the rule at step 1, step 4, step 5.3, and in Rules (red until the sync).
- Full `tests/` run; the two `test_template_parity` checks and the pickup test are expected red until the sync.

## Risks & Mitigations

- A skipped cleanup leaves a leftover Routine or session — ACCEPTED: the stale Routine sweep, `auto_disabled_session_gone`, the zombie-checker cleanup of later stages, and checker step 0 each catch it.
- A skipped archive in §26.D followed by the Routine delete would make the checker read a failed hand-back — mitigated: after a denial the step's remaining cleanup (the delete) is skipped too, which keeps the existing order.
- The ownership check refuses a delete that was safe (a name that does not match) — ACCEPTED: the leftover is swept later; the call is recorded as `cleanup skipped`.
- A textual conflict with #5008 / #4785 in the same command twin — mitigated: this change touches step 0, the Check-in Loop, Hand-back, and Rules, not step 4 or §28.C.

## Rollout

Lands on `main` through the project's final PR, after the `[claude-twin-sync]` copy of the two command twins and the pickup diff. Consumers receive CLAUDE.md §26.I and the two command twins on the next `@stable` sync; the pickup runs only in coding-workflows. No flag, no data change.

## References

- #5068 (this issue), #4755, #5018, #4910, #4948 (PR #5008), #4785
- `docs/operations/master-session.md` (Q40: A)

## Auto-decisions

- AD-1 [plan, 2026-09-29] Where does the rule live? — Picked: A — one definition in a new CLAUDE.md §26.I, plus a short sentence at each command's cleanup site and a Rules bullet. Alternatives: B — the full rule repeated in each command only. Why: the §26 flows are in CLAUDE.md, and one definition keeps six call sites from drifting; CLAUDE.md is not a protected path. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Which calls count as cleanup? — Picked: A — every `delete_trigger`, `archive_session`, and `set_session_title` in the listed flows, including the §26.G sweep's deletes, the checker prompt's step 4b delete, the two-step start's archive after a failed start trigger, and the end-of-project archives. Alternatives: B — only the three sites the issue names. Why: the acceptance criterion is that no flow retries a denied cleanup call. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] What does a failed cleanup call other than a denial do? — Picked: A — not found counts as done; any other failure is recorded as `cleanup skipped: <tool> failed (<error>)` and not retried, and only a denial skips the rest of the step's cleanup. Alternatives: B — leave other failures unspecified. Why: a transient failure on housekeeping is not worth a retry either, and the leftover is swept later; skipping the rest only after a denial is what the issue asks. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] When is `get_trigger` needed before a delete? — Picked: A — for a trigger named by id (the `— resume.` block, the log, a report, the checker's own hand-back trigger); a trigger picked from a `list_triggers` result in the same step is checked on the listing's `name` and `persistent_session_id`, and the §26.G sweep keeps its script-chosen list. Alternatives: B — `get_trigger` before every delete. Why: the listing already carries both fields (§15), and the sweep's script already restricts names. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] How does the ownership check apply to the pickup, whose triggers are bound to other pickup sessions by design? — Picked: A — the pickup deletes only triggers named exactly `Claude issue pickup: hourly` (its existing filter), and the skip-on-denial rule applies unchanged. Alternatives: B — require the trigger to be bound to this session. Why: B would stop `— restart` and `stop` from working at all. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] Where is `cleanup skipped` recorded in flows without a progress log? — Picked: A — in the flow's report (the fix-claude-pr report, the pickup's one-line report, the checker's reply, the §26.D report). Alternatives: B — as a PR or issue comment. Why: a comment per skipped cleanup is noise, and the report is where each flow already records its outcome. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] How is `.claude/commands/claude-issue-pickup.md`, which has no twin, changed? — Picked: A — not edited by this stage; its exact diff and the resulting file's sha256 go into the twin-sync blocker, and its test reads the root file (red until the sync). Alternatives: B — add a `workflow-templates/` twin for the pickup. Why: the pickup is coding-workflows-only, and B would ship it to consumers (§5); A is the Q40 no-twin rule. Applied in: phase 1. Status: pending review

## Notes

- `security_pass_skip.py` printed `{"skip": false, "label": null, "reason": "no skip label"}`, so `Security pass: run`.
