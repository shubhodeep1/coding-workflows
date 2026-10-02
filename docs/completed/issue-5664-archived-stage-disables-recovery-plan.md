# Keep issue-start sessions until their issue closes (archived-stage-disables-recovery)

Source issue: shubhodeep1/coding-workflows#5664 (https://github.com/shubhodeep1/coding-workflows/issues/5664)
Base branch: claude/implement-plan-issue-4887-archive-finished-sessions
Security pass: skip (ai:security: automation-produced issue)

## Summary

The stale session sweep (`scripts/claude_session_janitor.py`, CLAUDE.md §26.I)
archives an idle issue-start session as soon as any later
`implement-plan issue-<n>-… — <stage>` session for the same issue is on the
page, even one the checker archived because its start trigger failed.
Archiving the issue-start session disables its 24-hour safety net, the only
thing that restarts a stalled chain, so the project can stall before its
security pass. This plan removes the "superseded by a later stage" rule: an
issue-start session is archived only once its issue is closed.

## Context

- Security finding #5664 (`ai:security`, medium, confidence 9/10, STRIDE
  Denial of Service), filed by the audit of project #4887 against
  `scripts/claude_session_janitor.py:288`. Recommendation: "Treat a stage as
  superseding only after verifying it started successfully and the
  predecessor's recovery trigger was disarmed; keep the predecessor when
  either check is uncertain."
- `scripts/claude_session_janitor.py` lines 278–293 collect every stage
  session whose stage is not `checker`, `waiting:`, or `deploy-activate`
  into `later_stages`, whatever its `session_status`. Lines 330–333 then name
  an idle issue-start session created before any of them. The existing test
  `test_issue_start_superseded_by_a_later_stage_is_archived_without_a_read`
  pins exactly the exploit scenario: the superseding stage is
  `SESSION_STATUS_ARCHIVED`.
- How the chain uses the issue-start session (`.claude/commands/implement-plan-claude.md`):
  - it arms the project's first wait, including a `send_later` safety net
    into itself (Check-in Loop, "Arming the wait" step 4) and, for a PR wait,
    a hand-back Routine bound to itself (step 0);
  - the checker starts the next stage with `create_session` then
    `create_trigger`; when `create_trigger` fails it archives the new session
    (Two-step start; checker prompt step 5), so an archived stage session can
    be one that never ran;
  - a stage that does run archives the previous stage session in its step 0
    (resume hygiene) unless that session is `blocked` or `need_input`, and
    deletes its safety net and hand-back triggers.
  So a later stage on the page does not prove that the issue-start session's
  recovery triggers were disarmed.
- Archiving a session auto-disables every trigger bound to it
  (`ended_reason: auto_disabled_session_gone`), safety net included.
- Verifying "the recovery trigger was disarmed" needs `list_triggers` data.
  On 2026-09-30 a `list_triggers` call with `enabled: true` and `limit: 100`
  returned 100 Routines with `has_more: true`, so one page never shows them
  all. Passing a complete listing would need several pages per wake and an
  edit to `.claude/commands/claude-issue-pickup.md`, a protected path with no
  `workflow-templates/` twin (CLAUDE.md §28.C interim twin-first rule).
- The sweep keeps its second issue-start rule: a session whose issue is
  closed is archived (one REST read per distinct issue).

## Goals

- `classify()` no longer names an issue-start session because a stage
  session exists. An idle issue-start session is archived only when
  `repos/<owner>/<repo>/issues/<n>` reads `state: closed`, with the reason
  `issue-start: <repo>#<n> closed`.
- A stage session, archived or not, never causes an archive. Stage titles are
  still recognised and counted as `not_ours`.
- Tests pin the finding's scenario: an issue-start session with an open issue
  is kept when a later stage is archived (failed start) and when a later
  stage is live.
- The script docstring, CLAUDE.md §26.I, `README.md`, `agents.md`, and the
  unreleased `changelog.d/4887-archive-finished-sessions.md` describe the new
  rule.

## Non-goals

- Reading `list_triggers` in the sweep, or any change to
  `.claude/commands/claude-issue-pickup.md` (AD-1).
- The fixer / hold and report rules, the cursor, and the guards.
- The closed-issue rule itself.
- Resume hygiene in `/implement-plan-claude` step 0, which already keeps
  sessions waiting on the user.

## Constraints

- §1: the finding is an availability defect; the fix only makes the sweep
  archive less (fail safe).
- §5: script, tests, and the docs that describe the rule. No new option,
  input, or scheduler.
- §6: no identifier is renamed or removed. `STAGE_TITLE_PATTERN`,
  `NON_SUPERSEDING_STAGE_PATTERN`, `classify_title()`'s `stage` kind, and
  every output key stay (AD-2). No new identifiers except test names.
- §9: tabs in Python.
- §15: the closed-issue rule already costs one cached REST read per distinct
  issue. Issue-start sessions that used to be named without a read now cost
  that read.
- §19: phase and completion PRs use `Refs #5664`; the final PR into the base
  branch, which is not the default branch, carries `Refs #5664`, and the
  final-merge stage closes the issue.
- §20: the behaviour ships unreleased on project #4887's branch, so its
  fragment is corrected instead of adding a second one (AD-3).
- §25: no PR watching.

## Approach

Drop the supersede branch of `classify()`: without trigger data the sweep
cannot verify either condition the finding asks for, and the finding says to
keep the predecessor when a check is uncertain. The closed-issue rule already
ends every leftover issue-start session when its project finishes, and
resume hygiene still archives the issue-start session on the first stage in
the normal path. The stage-title parsing stays so stage sessions remain
`not_ours`.

## Phases & Merge Strategy

Single phase: issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Issue-start sessions archive only on a closed issue.** Files: see Files
   & Modules.
   - Done when:
     - `tests/test_claude_session_janitor.py` passes, including the new
       failed-start and live-stage cases;
     - `tests/test_changelog_fragment_contract.py` and
       `tests/test_claude_md_section_numbers.py` pass;
     - `ruff check scripts/claude_session_janitor.py tests/test_claude_session_janitor.py` is clean.
   - Rollback: revert the PR. The sweep then archives superseded issue-start
     sessions again.

## Implementation Steps

1. `scripts/claude_session_janitor.py`: remove the `later_stages` collection
   and the supersede branch in `classify()`; the issue-start branch calls
   `_issue_closed()` only. Update the module docstring (rule 2, the stage
   paragraph, the API budget) and mark `NON_SUPERSEDING_STAGE_PATTERN` as
   kept for §6 only.
2. `tests/test_claude_session_janitor.py`: replace the two "superseded" tests
   with keep tests for an archived (failed-start) and a live later stage,
   update `test_pickup_named_sessions_are_swept` for the extra issue read,
   and keep the non-superseding parametrised test.
3. CLAUDE.md §26.I, `README.md` (Claude issue implementer, item 5),
   `agents.md` (Check-in Loop paragraph and "Stale session sweep"),
   `changelog.d/4887-archive-finished-sessions.md`: the issue-start rule
   reads "its issue is closed".
4. `docs/plans/issue-4887-archive-finished-sessions-plan.md`: one `## Notes`
   line recording that #5664 narrowed the issue-start rule, so that
   project's later conformance runs do not restore it (AD-4).

## Files & Modules

- `scripts/claude_session_janitor.py`
- `tests/test_claude_session_janitor.py`
- `CLAUDE.md` (`workflow-templates/CLAUDE.md` is a symlink to it)
- `README.md`, `agents.md`
- `changelog.d/4887-archive-finished-sessions.md`
- `docs/plans/issue-4887-archive-finished-sessions-plan.md` (Notes line only)
- `docs/plans/issue-5664-archived-stage-disables-recovery-plan.md` [new], `docs/implement-plan/issue-5664-archived-stage-disables-recovery.md` [new]

## Tests

Unit (stubbed `gh_api`):
- an idle issue-start session on an open issue is kept, and its issue is
  read, when a later stage session is `SESSION_STATUS_ARCHIVED` (the failed
  start of #5664) and when it is idle and live;
- the same session is archived when the issue is closed, with the
  `issue-start: o/r#7 closed` reason;
- checkers, earlier stages, other issues, other repositories, and newer
  issue-start sessions still never cause an archive;
- the pickup-named titles test still archives the closed-issue session and
  the fixer, and now keeps the `need_input` issue-start session whose issue
  is open.

Instruction text: the existing CLAUDE.md / agents.md needles still pass.

## Risks & Mitigations

- Issue-start sessions waiting on the user stay open until their issue
  closes, which can be days on a long project. ACCEPTED: clutter only; the
  closed-issue rule ends them, and an operator can archive one by hand.
- Each open issue-start session now costs one issue read per wake. ACCEPTED:
  cached per issue, a handful per page.

## Rollout

Lands on project #4887's branch and reaches `main` with that project's final
PR (#4924). The pickup reads the script fresh on every wake. Nothing ships to
consumer repos (`scripts/claude_session_janitor.py` has no template copy).

## Auto-decisions

- AD-1 [plan, 2026-09-30] How does the sweep make sure a later stage really replaced the issue-start session before archiving it? — Picked: A — it no longer tries: the supersede rule is removed, and an issue-start session is archived only when its issue is closed. Alternatives: B — the pickup passes a complete `list_triggers` listing (`--triggers`) and a stage supersedes only when it is not archived and no enabled trigger is bound to the issue-start session (needs several `list_triggers` pages per wake, since 100+ Routines are enabled, and an edit to the no-twin `.claude/commands/claude-issue-pickup.md` that blocks the project on a manual twin sync); C — count only non-archived stages whose `updated_at` shows they ran (a heuristic that cannot prove the safety net was deleted). Why: the finding says to keep the predecessor when a check is uncertain, and the session page alone cannot show the trigger check; A is the smallest fail-safe change (§1, §5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What happens to the stage-title parsing and `NON_SUPERSEDING_STAGE_PATTERN` once nothing supersedes? — Picked: A — keep both: stage titles still classify as `stage` and count as `not_ours`, and the constant stays with a comment. Alternatives: B — delete the constant (an identifier removal under §6). Why: §6 and §28.B forbid removing identifiers in place. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Where does the changelog record this? — Picked: A — correct the unreleased `changelog.d/4887-archive-finished-sessions.md` row and text. Alternatives: B — a new `changelog.d/5664-…` security fragment (it would describe a flaw that never reached `main` next to an entry that still claims the old rule). Why: both land in `main` together through #4924, and the released entry must describe the shipped rule. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] Does project #4887's plan learn about the narrowed rule? — Picked: A — one `## Notes` line in `docs/plans/issue-4887-archive-finished-sessions-plan.md`. Alternatives: B — leave it; a later conformance or activation run of #4887 would read the rule as missing and could restore it. Why: prevents a regression from the parent project's own judge. Applied in: phase 1. Status: pending review

## References

- #5664 (this finding), #4887 (the sweep), #3576 (audit tracker), #4924
  (project #4887 final PR), #4525, #4785.
