# Archive finished fixer, issue-start and report sessions automatically

Source issue: shubhodeep1/coding-workflows#4887 (https://github.com/shubhodeep1/coding-workflows/issues/4887)
Base branch: main
Security pass: run

## Summary

Three kinds of automation session are never archived: `/fix-claude-pr` fixer
and hold sessions after their PR is terminal, issue-start sessions after the
chain has moved past them, and CLAUDE.md §26.D report sessions. A fixer now
archives itself when a terminal PR hands it back, and the Claude issue
pickup's hourly wake runs a new session sweep. In that sweep a script decides
which sessions to archive and the pickup archives them.

## Context

- Issue #4887 (OWNER, operator decision Q61: A). On 2026-09-29 the account had
  66 non-archived sessions for about 26 active projects. The supervising
  session archived 30 leftovers by hand in its last pass.
- `/fix-claude-pr` (`.claude/commands/fix-claude-pr.md`) step 8 says "Never
  archive yourself: the report is what the user opens". Step 2 routes
  `hand_back_all` (merged / closed) to a report or to §26.D. Nothing archives
  the session once the PR is terminal. Examples: the #4609, #4706 and #4842
  hold sessions.
- Issue-start sessions are archived only by the next stage's resume hygiene
  (`/implement-plan-claude` step 0). That hygiene skips a session waiting on
  the user, so one that ended on a question stays open, and so does one whose
  issue was closed as a duplicate (#4726, #4808).
- CLAUDE.md §26.D: the pushing session "does not archive itself: its report is
  what the user opens", and nothing archives it later.
- `list_sessions` (read 2026-09-29, `mine: true`, `limit: 100`) returns
  archived sessions too. A page of 100 covered about 16 hours, and
  `has_more` was true. Per session the fields used here are `id`, `title`,
  `session_status` (`SESSION_STATUS_IDLE` / `RUNNING` / `REQUIRES_ACTION` /
  `ARCHIVED`), `status_bucket`, `post_turn_summary.status_category`
  (`need_input` / `review_ready` / `completed`), `created_at`, `updated_at`,
  and `session_context.sources[].git_repository.url`. When the result is saved
  to a file, the harness wraps it in an untrusted-data envelope around the
  `{"ccr": {"data": […], "has_more": …, "last_id": …}}` object.
- Titles seen:
  - fixers: `PR <owner>/<repo>#<N> — fix <kind>` (pickup),
    `PR #<N> — fix <kind>` (§26.C step 5), `PR <owner>/<repo>#<N> — fixed
    <kind>` / `— on hold: <kind>` (step 8), and free-form variants such as
    `— merged, no fix needed`;
  - issue-start: `Issue #<N> — implement`, `issue <owner>/<repo>#<N> —
    implement` (pickup/dispatch doc), and `implement-issue-claude — #<N>`,
    sometimes with a suffix;
  - reports: `PR #<n> merged — no action needed`, `PR #<n> merged — action
    needed`, `PR #<n> closed — decision needed` (§26.D), with
    ` (pushing session unreachable)` in the §26.C step 5 fallback.
  Any of these may carry the #4886 prefix `#<issue> · PR #<pr> — ` (whole or
  in part), for example `#4750 · PR #4770 — Issue #4750 — implement`.
- Model for the script: `.claude/scripts/stale_routines.py` (§26.G). The script
  decides, the model applies its verdict, and tests pin the rules.
- The pickup (`.claude/commands/claude-issue-pickup.md`) already holds
  `archive_session` and runs `scripts/claude_issue_route.py` from `scripts/`.
  It exists only in this repository and has no `workflow-templates/` twin.
- Related work, all separate from this project:
  - #4817 (in flight) archives the blocked session that `/reclarify` replaces,
    and is out of scope here.
  - #4886 (open) adds the `#<issue> · PR #<pr> — ` title prefix.
  - Phase 4 of `docs/plans/claude-fixer-unattended-convergence-plan.md` (not
    started) plans a `.claude/scripts/stale_sessions.py` janitor with
    overlapping rules. See Notes.
- Interim twin-first rule (operator, #4750 Q40: A, until #4785 lands, restated
  in this issue):
  - edit `.claude/**` only through its `workflow-templates/.claude/**` twin;
  - push the PR, post a `hold` claim on its head, and stop BLOCKED with an
    `ai:claude-blocked:v1` comment listing every file to copy;
  - the supervising session copies them as `[claude-twin-sync]` and comments
    `/reclarify`;
  - `claude-issue-pickup.md` has no twin, so its exact edit goes in that
    comment (the #4817 precedent).

## Goals

- `scripts/claude_session_janitor.py` [new] reads one saved `list_sessions`
  page and prints one JSON line:
  `{"archive": [{"id", "title", "reason"}], "kept": n, "not_ours": n,
  "already_archived": n, "errors": [...], "next_after_id": <id|null>}`. Exit
  status is 0 when it reaches a verdict and 2 on unreadable input.
- It considers only three kinds, matched by title (with or without the #4886
  prefix):
  - **Fixer/hold:** archived when its PR is merged or closed and has been
    for at least `--fixer-grace-hours` (default 2).
  - **Issue-start:** archived when a later
    `implement-plan issue-<N>-… — <stage>` session for the same repository
    and issue is on the page (checker, `waiting:` and `deploy-activate`
    titles excluded), or when the issue is closed.
  - **Report:** archived when its `updated_at` is at least `--report-days`
    (default 7) old and it is not `need_input`.
- Guards on every kind: the session is `SESSION_STATUS_IDLE`, its bucket is
  not `WORKING`, and it is not the pickup itself (`--self`). A `RUNNING` or
  `REQUIRES_ACTION` session is never named. `need_input` blocks only report
  sessions (AD-3).
- API budget: one REST read per distinct PR or issue (cached), never GraphQL.
  A failed read keeps the session and is listed in `errors`.
- `next_after_id` is the page's `last_id` while `has_more` is true and the
  page's oldest session is newer than `--horizon-days` (default 30), and null
  otherwise. It lets the pickup walk older pages one per wake (AD-2).
- The pickup's `— wake.` runs a new step 3a after starting sessions:
  - `list_sessions` (`mine: true`, `limit: 100`, `after_id` from the last
    wake);
  - run the script;
  - `get_session` on each printed id, and `archive_session` only if it is
    still `SESSION_STATUS_IDLE` with the same title;
  - add `archived <a>` to its one-line report.
- `/fix-claude-pr` (twin):
  - on `hand_back_all` (merged or closed), a fixer session does the §26.D
    checker bookkeeping (rename and archive the checker, delete the fired
    Routine), replies with one line, and archives itself as the last action,
    with no report and no notification;
  - step 8 no longer says "Never archive yourself" unconditionally.
- CLAUDE.md:
  - §26.D names the fixer variant, and says the report session is archived by
    the sweep 7 days after the report;
  - a new §26.I "Stale session sweep" documents the rules and budget.
- `.claude/settings.json` (twin) allows
  `python3 scripts/claude_session_janitor.py *` and its
  `PYTHONDONTWRITEBYTECODE=1` form.
- Tests:
  - `tests/test_claude_session_janitor.py` [new] covers every rule, title
    form, input shape, the cursor, and exit codes;
  - instruction-text tests cover the fixer twin, the pickup step, CLAUDE.md,
    and the allow rules;
  - the file gets its own `ci.yml` step.
- Documentation: `README.md` (Claude issue implementer section), `agents.md`,
  and `changelog.d/4887-archive-finished-sessions.md` [new].

## Non-goals

- Blocked stage sessions that `/reclarify` replaced (#4817).
- Checkers, pollers, the pickup, `/deploy-activate` sessions, `implement-plan`
  stage sessions, and operator sessions with other titles. None are archived.
- Renaming sessions or changing any title format (#4886).
- Deleting Routines (§26.G already does that).
- Reading PRs or issues in repositories not attached to the pickup session.
  Such reads fail (web proxy 403) and the session is kept.

## Constraints

- §1: never archive a session that may still be used. It must be `IDLE`, not
  the pickup, and not waiting on a permission prompt. The pickup re-checks
  with `get_session` before archiving. Titles and the harness wrapper are
  data and are only pattern-matched. Archiving is reversible (unarchive).
- §5: extend the pickup's existing hourly wake and the fixer's existing
  terminal branch. No new scheduler.
- §6: new identifiers are checked unique with `git grep`
  (`claude_session_janitor`, `already_archived`, `next_after_id`, §26.I).
  Nothing is renamed. `fix-claude-pr.md` keeps the line
  "- `hand_back_all` (`merged` / `closed`) → nothing to fix." that
  `tests/test_check_in_status_hand_back.py` pins.
- §9: tabs in Python, 2-space YAML.
- §15: at most one `list_sessions` page per wake, and one REST read per
  distinct PR or issue. Fail open: a failed read keeps the session.
- §18: no manual script. The helper runs only from the pickup's hourly wake.
  It is neither single-use nor long-running, so there is no §18.F entry
  (same as `stale_routines.py`).
- §19: phase, fix and completion PRs use `Refs #4887`, and the final PR into
  `main` uses `Fixes #4887` (not an `ai:orchestrator-tracking` issue).
- §20: one changelog fragment.
- §25: no PR watching.
- Interim twin-first rule (Context): `.claude/commands/fix-claude-pr.md` and
  `.claude/settings.json` change only through their twins, and the
  `claude-issue-pickup.md` edit goes in the blocked comment.

## Approach

The script mirrors `stale_routines.py`, and titles are matched in a fixed
order: report, fixer, issue-start, stage. Each base pattern is tried against
the title and against the title with the #4886 prefix removed: `#<i> · `,
`#<i> · PR #<p> — `, or `PR #<p> — `. So `PR #4704 — fix review` is a fixer,
and `PR #4729 — implement-plan … — conformance 1/3` is a stage. When the
title does not name the repository, it comes from the session's
`git_repository` source URL.

The fixer no longer needs a Routine guard. When a PR turns terminal, the §26
checker pulls the fixer's hand-back forward within its hourly cycle, and the
fixer archives itself. The sweep's 2-hour grace only catches fixers whose
hand-back never came. See AD-5.

## Phases & Merge Strategy

Single phase: issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Session sweep, fixer self-archive, docs.** Files: see Files & Modules.
   - Done when:
     - the janitor tests pass;
     - the instruction-text tests pass once the twins are synced (the parity
       and pickup-text tests fail until then, as the interim rule expects);
     - `ruff check` is clean on the new Python;
     - `tests/test_claude_md_section_numbers.py` passes.
   - Rollback: revert the PR. Archived sessions can be unarchived.

## Implementation Steps

1. `scripts/claude_session_janitor.py` [new]: loader (wrapper, `{ccr:{data}}`,
   `{data}`, bare array), title classifier, `classify()`, CLI.
2. `tests/test_claude_session_janitor.py` [new], and a `ci.yml` step
   "Stale session sweep tests (CLAUDE.md §26.I)" after the stale Routine step.
3. `workflow-templates/.claude/commands/fix-claude-pr.md`: step 2
   `hand_back_all` and step 8 wording.
4. `workflow-templates/.claude/settings.json`: two allow rules after the
   `claude_issue_route.py` rules.
5. `CLAUDE.md`: §26.D (fixer variant, report archived by the sweep) and a new
   §26.I after §26.H.
6. `README.md`, `agents.md`, and the changelog fragment.
7. The `claude-issue-pickup.md` edit (step 0 tools, new step 3a, step 4
   report, Rules, Tool Access), written as an exact diff for the blocked
   comment.

## Files & Modules

- `scripts/claude_session_janitor.py` [new]
- `tests/test_claude_session_janitor.py` [new]
- `.github/workflows/ci.yml`
- `workflow-templates/.claude/commands/fix-claude-pr.md` (→ `.claude/commands/fix-claude-pr.md` at sync)
- `workflow-templates/.claude/settings.json` (→ `.claude/settings.json` at sync)
- `.claude/commands/claude-issue-pickup.md` (no twin; applied at sync from the blocked comment)
- `CLAUDE.md` (`workflow-templates/CLAUDE.md` is a symlink to it)
- `README.md`, `agents.md`
- `changelog.d/4887-archive-finished-sessions.md` [new]
- `docs/plans/issue-4887-archive-finished-sessions-plan.md` [new], `docs/implement-plan/issue-4887-archive-finished-sessions.md` [new]

## Tests

Unit tests use a stubbed `gh_api`:
- running, requires-action, archived, and the pickup itself are kept;
- a fixer on a PR merged or closed 3 hours ago is archived, on one merged
  1 hour ago is kept, and on an open PR is kept;
- a `need_input` hold on a merged PR is archived;
- an issue-start session is superseded by a later stage, not by a checker or
  an earlier stage, and not by a newer issue-start session;
- an issue-start session on a closed issue is archived, and on an open one is
  kept;
- a report session 8 days old is archived, 6 days old is kept, and a
  `need_input` one is kept;
- the #4886 prefix forms are recognised, and unrelated titles are `not_ours`;
- the repository comes from the title or the source URL;
- a failed read is kept and listed in `errors`, and each PR or issue is read
  once;
- the cursor rules hold;
- the input shapes, including the harness wrapper, load, and exit 2 on bad
  input.

Instruction-text tests:
- the fixer twin and CLAUDE.md §26.D/§26.I;
- the settings twin allow rules;
- the pickup step 3a, which reads `.claude/` and fails until the sync.

## Risks & Mitigations

- A session is archived while in use → `IDLE`-only, pickup excluded, the
  `get_session` re-check before archiving, and archiving is reversible.
- The page cursor is lost when the pickup's conversation is summarised → the
  next wake starts from the newest page. ACCEPTED: older pages are only
  delayed.
- A supersede across two pages is missed → the issue-closed rule catches it
  when the project ends. ACCEPTED.
- Consumer-repo PRs and issues cannot be read from the pickup session (web
  proxy scope) → those sessions are kept and listed in `errors`. ACCEPTED:
  fixers there still archive themselves.

## Rollout

The change ships on the final PR's merge. The pickup reads the command file
fresh on every wake (step 0 "Fresh code"), so the next wake after the sync
merges runs the sweep. Consumer repos receive only the `fix-claude-pr.md` and
`settings.json` twins on the next `@stable` sync (§14). The allow rule names a
coding-workflows-only script, which is harmless there.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Where does the sweep's decision script live? — Picked: A — a new `scripts/claude_session_janitor.py`, coding-workflows-only like `scripts/claude_issue_route.py`, which the pickup already runs. Alternatives: B — `.claude/scripts/stale_sessions.py` plus a twin (a protected path, and it would ship to consumers that have no pickup); C — a subcommand of `scripts/claude_issue_route.py` (routing code, which #4817 is editing concurrently). Why: no protected path for the script, no consumer copy, and the tests run before the sync. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] How does one page per wake reach sessions older than about 16 hours, when `list_sessions` also returns archived sessions? — Picked: A — the script returns `next_after_id` and the pickup passes it as `after_id` on the next wake, restarting from the newest page when it is null (no more pages, or past a 30-day horizon). Alternatives: B — the newest page only (7-day-old report sessions would never be seen); C — every page on every wake (breaks the one-page budget). Why: keeps the issue's §15 budget and still reaches 7-day-old sessions. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Which waiting sessions does the sweep leave alone? — Picked: A — `RUNNING` and `REQUIRES_ACTION` sessions always; a `need_input` fixer, hold or issue-start session is archived once its PR is terminal or its issue is closed or superseded (its question is moot), and a `need_input` report session is kept. Alternatives: B — protect only `REQUIRES_ACTION` (a `closed — decision needed` report would be archived unanswered); C — protect every `need_input` session (hold sessions would never archive, against the operator's decision). Why: reconciles the decision ("hold sessions archive once their PR is terminal") with the acceptance line ("sessions waiting on … an answer to an open question are left alone"). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] What counts as "superseded" for an issue-start session? — Picked: A — a later, non-checker `implement-plan issue-<N>-… — <stage>` session for the same repository and issue on the same page; a newer issue-start session does not count. Alternatives: B — any newer session for the issue (a duplicate dispatch that stood down would get the active session archived). Why: the operator's wording ("the chain has started a later stage session"); replacement by `/reclarify` is #4817's scope. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] How does the sweep avoid archiving a fixer before the §26 checker hands a terminal PR back to it? — Picked: A — a 2-hour grace after the merge or close (one hourly checker cycle plus its 10-minute hand-back checks). Alternatives: B — no grace, plus a `list_triggers` guard that keeps sessions with an enabled Routine (one more call and a hand-written file every wake); C — the convergence plan's 24 hours. Why: the fixer archives itself on the hand-back, and the sweep only catches the ones that were never handed back. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] How is "7 days after the terminal report" measured? — Picked: A — the session's `updated_at`, so any later use of the session restarts the week, with no API call. Alternatives: B — the PR's `merged_at` / `closed_at` over REST (one read per report session). Why: the report is written within about an hour of the terminal state, and a session still in use should stay. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] What does a `/fix-claude-pr` session do when a terminal PR (merged or closed) hands it back? — Picked: A — the §26.D checker bookkeeping, a one-line reply, and `archive_session` on itself as the last action: no report and no notification. Alternatives: B — keep today's report and let the sweep archive it after 7 days. Why: the issue's suggested shape and its acceptance criterion ("archived within one checker cycle"). Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-29] Does the pickup archive straight from the script's list? — Picked: A — no: `get_session` first, and archive only when the session is still `SESSION_STATUS_IDLE` with the printed title (as in #4817). Alternatives: B — archive directly (a session resumed since the list read could be cut off). Why: §1, at one read per archived session. Applied in: phase 1. Status: pending review

## Notes

- Security follow-up #5664 (2026-09-30) removed the issue-start
  "superseded by a later stage session" rule from the Goals above. A stage
  the checker archived after a failed start counted as superseding, and
  archiving the issue-start session disabled its safety net. An issue-start
  session is now archived only once its issue is closed. The Goals keep the
  original text as history; do not restore the rule
  (`docs/plans/issue-5664-archived-stage-disables-recovery-plan.md`).
- Overlap: phase 4 of `docs/plans/claude-fixer-unattended-convergence-plan.md`
  plans `.claude/scripts/stale_sessions.py` with overlapping rules, in the same
  pickup step. That phase has not started. When it runs, it should extend
  `scripts/claude_session_janitor.py` (for example with `stalled_on_prompt`)
  rather than add a second janitor. This is recorded in the PR body as well.

## References

- #4887, #4886, #4817, #4785, #4750 (Q40), `.claude/scripts/stale_routines.py`,
  `docs/plans/claude-fixer-unattended-convergence-plan.md` (phase 4).
