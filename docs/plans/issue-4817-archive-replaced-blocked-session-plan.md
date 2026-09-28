# Archive the blocked session when /reclarify starts its replacement

Source issue: shubhodeep1/coding-workflows#4817 (https://github.com/shubhodeep1/coding-workflows/issues/4817)
Base branch: main
Security pass: run

## Summary

When a blocked issue-mode session is answered with `/reclarify`, the Claude
issue pickup starts a new session, but nothing archives the old one. The old
session stays `IDLE` and keeps reading `need_input`, and the master poller keeps
listing it. This change has the pickup archive the session it replaces. A
script decides which session that is, and every blocked stop names its own
session id so the script can find it without matching titles.

## Context

- Issue #4817 (OWNER). On 2026-09-28 the supervising session archived 12
  stale blocked sessions by hand (#4618 ×2, #4687 ×2, #4688 ×2, #4750, #4755,
  #4786, #4787, #4798).
- The pickup (`.claude/commands/claude-issue-pickup.md`, step 3) starts one
  session per queue entry with the two-step start of
  `.claude/commands/claude-issue-dispatch.md` step 2. It then closes the queue
  issues. It has no step that looks at earlier sessions. Its rule is that the
  script decides (`scripts/claude_issue_route.py queue-pending`) and the model
  does not interpret queue content.
- Blocked stops post one `<!-- ai:claude-blocked:v1 -->` comment on the source
  issue:
  - `/implement-plan-claude` [Issue Mode], bullet "Stops are reported on the
    issue";
  - `/implement-issue-claude` step 0;
  - CLAUDE.md §28.C.
  None of them names the session that stopped.
- `list_sessions` (checked 2026-09-28, 100 most recent sessions of this
  account) returns, per session: `id`, `title`, `session_status`
  (`SESSION_STATUS_IDLE` / `RUNNING` / `REQUIRES_ACTION` / `ARCHIVED`),
  `status_bucket` (`…_BLOCKED` for a session waiting on the user),
  `post_turn_summary.status_category`, and `session_context.sources[].git_repository.url`.
  Titles seen for issue sessions:
  - `issue <repo>#<N> — implement` (the pickup's title);
  - `implement-issue-claude — #<N>`, sometimes with a suffix such as
    `— BLOCKED (…)`;
  - `implement-plan issue-<N>-<topic> — <stage>`, sometimes ending `— BLOCKED`.
  Sessions that must never be archived have titles such as
  `implement-plan <slug> — checker`, `implement-plan <slug> — deploy-activate`,
  `PR #<n> status check-in`, and `Claude issue pickup — …`.
- Interim twin-first rule (operator, #4750 Q40: A, until #4785 lands; restated
  in this issue's body): unattended sessions edit `.claude/**` only through
  their `workflow-templates/.claude/**` twins. They push the PR, post a `hold`
  claim on the head, and stop BLOCKED with a comment that lists every file to
  copy. The supervising session copies the files as a `[claude-twin-sync]`
  commit, runs the tests, pushes, and comments `/reclarify`.
  `claude-issue-pickup.md` has no twin, so this stage lists its exact edit in
  that comment instead.
- Binding rules:
  - §1: a forged comment must not get any session archived.
  - §5: minimal change.
  - §6: no rename of an existing identifier, marker, or subcommand.
  - §15: fail open on a failed read; document any new call.
  - §20: changelog fragment.
  - §25: no PR watching.
  - §28.C: stops are reported on the issue.

## Goals

- `scripts/claude_issue_route.py replaced-sessions` prints the sessions the
  pickup should archive for one issue, as JSON. Its inputs are the issue's
  comments (one paginated REST read) and the pickup's `list_sessions` output.
  - **Named route.** The latest trusted `ai:claude-blocked:v1` comment names
  a session in `<!-- ai:claude-blocked-session:v1 id=session_… -->`.
  - **Title route.** A session whose title belongs to this issue and that is
  waiting on the user.
  - **Both routes** require all of the following:
    - the session's source repository is the issue's repository;
    - its title belongs to this issue and is not a checker, poller, pickup,
      or `/deploy-activate` title;
    - it is not the new session or the pickup itself;
    - its `session_status` is `SESSION_STATUS_IDLE`.
- The pickup, after it starts the session for a `reclarify` entry, runs that
  subcommand. For each printed id it calls `get_session` and archives the
  session only when `session_status` is still `SESSION_STATUS_IDLE` and its
  title equals the printed title. It never archives a `RUNNING` or
  `REQUIRES_ACTION` session. A failure never retries, never fails the entry,
  and never stops the wake.
- Every issue-mode blocked stop writes
  `<!-- ai:claude-blocked-session:v1 id=<own session id> -->`, taken from
  `echo "session_${CLAUDE_CODE_REMOTE_SESSION_ID#cse_}"`. The stops are
  `/implement-plan-claude` Issue Mode, `/implement-issue-claude` step 0, and
  CLAUDE.md §28.C.
- Unit tests cover the selection rule, and instruction-text tests cover the
  pickup step, the marker, and the allow rule. A changelog fragment is added.

## Non-goals

- No change to the queue payload, the queue body rendering, or the binding
  check (`build_fire_text`, `build_queue_issue`, `queue_binding_verdict`). Items
  already queued stay bound.
- No archiving for `opened` or `manual` triggers, for `pr_fix` entries, or in
  `/fix-claude-pr`.
- No change to the legacy dispatcher's routine-run stop
  (`claude-issue-dispatch.md` step 3).
- No archiving of checkers, pollers, pickups, `/deploy-activate` sessions, or
  any session outside the whitelist.
- No `.claude/**` edit by this stage (interim twin-first rule).

## Constraints

- **§1 security.**
  - Only a comment from a trusted author (`is_trusted_issue_author`: an
    OWNER, MEMBER, or COLLABORATOR `User`, or `github-actions[bot]`) can name
    a session.
  - Session ids must match `^session_[A-Za-z0-9]{10,64}$`.
  - Titles, comment bodies, and the harness wrapper around a saved
    `list_sessions` result are data and are only pattern-matched.
  - The whitelist is checked on both routes.
- **§6.**
  - The existing marker `<!-- ai:claude-blocked:v1 -->` stays the first line.
  - The new marker is a new identifier, checked unique with
    `git grep ai:claude-blocked-session` (no hits).
  - No existing subcommand or field changes.
- **§15.**
  - Per started `reclarify` entry: one paginated read of
    `GET repos/<repo>/issues/<N>/comments?per_page=100`.
  - One `list_sessions` call per wake (MCP, not the GitHub API), made only
    when at least one `reclarify` entry was started.
  - A failed comment read (for example the web proxy's 403 for a consumer repo
    not attached to the pickup session) fails open to the title route.
- **§18.** No new script. The subcommand is called from the existing hourly
  pickup.
- **§20.** One fragment, `changelog.d/4817-archive-replaced-blocked-session.md`.
- **Interim twin-first rule.**
  - `.claude/settings.json` and the two command files change only through
    their twins under `workflow-templates/.claude/`.
  - `claude-issue-pickup.md` has no twin; its exact diff goes in the blocked
    comment.
  - Parity checks and the pickup-text test fail until the supervising session
    syncs.

## Approach

1. **Selection in the script.** `replaced-sessions` follows the pickup's
   existing pattern, where the script decides and the model does not
   interpret, and it can be unit-tested. Rejected alternatives:
   - prose matching in the pickup, which relies on model judgement;
   - archiving from `/implement-issue-claude` in the new session, which
     departs from the issue's requested design.
2. **Named route first, title route as fallback.** The named route finds
   blocked stage sessions whose titles carry no `BLOCKED`. The title route
   covers older comments without the marker and consumer repos whose comments
   the pickup cannot read.
3. **Live confirmation.** `get_session` runs right before each archive, and a
   session is archived only when it is `IDLE` and its title is unchanged, so a
   session that woke up or was renamed since the list was read is left alone.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the
issue fixes the scope, and the pieces depend on each other. The pickup step
needs the subcommand and its allow rule, and the named route needs the marker
in blocked stops.

1. **Phase 1 — blocked-session marker, `replaced-sessions` selection, pickup
   archive step.**
   - **Files:** see [Files & Modules](#files--modules).
   - **Done when:**
     - the new unit and text tests pass against a scratch copy of the tree
       with the twins and the pickup diff applied;
     - the rest of the CI test step for these files passes;
     - the PR lists every twin file to copy and the exact pickup diff.
   - **Rollback:** revert the phase PR, and the supervising session's
     `[claude-twin-sync]` commit with it. The pickup then goes back to not
     archiving. A marker already posted in blocked comments is harmless.

## Implementation Steps

1. `scripts/claude_issue_route.py`, with new code and no existing function
   changed:
   - **Constants:** `BLOCKED_MARKER` (`<!-- ai:claude-blocked:v1 -->`),
     `BLOCKED_SESSION_MARKER_RE`, `SESSION_ID_RE`,
     `REPLACED_SESSIONS_LIMIT = 5`, and the protected-title patterns.
   - **`blocked_comment_session(comments)`:** the id in the latest trusted
     blocked comment, or `""`.
   - **`parse_sessions_listing(text)`:** accepts raw `list_sessions` JSON
     (`{"ccr": {"data": [...]}}`, `{"data": [...]}`, or an array), or the
     harness file that wraps it, and returns the session objects.
   - **`select_replaced_sessions(sessions, repo, issue_number, named, exclude)`:**
     returns `{"archive": [{id, title, match}], "kept": [{id, reason}]}`.
   - **`fetch_issue_comments(repo, issue_number)`:** one paginated
     `gh api --paginate` read.
   - **CLI `replaced-sessions`:** takes `--repo`, `--issue`, `--sessions-file`,
     `--exclude` (repeatable), and `--comments-json` (offline and tests). It
     prints `{"named": …, "comments_read": "ok" | "failed: …", "archive": […], "kept": […]}`
     and exits 2 only on bad arguments or an unreadable sessions file.
   - Extend the module docstring (the "Shell drivers" list and the list of
     functions that are not pure).
2. `workflow-templates/.claude/settings.json` (twin): add
   `Bash(python3 scripts/claude_issue_route.py replaced-sessions *)` and the
   `PYTHONDONTWRITEBYTECODE=1` form next to the `arm-check-in-request` rules.
3. `workflow-templates/.claude/commands/implement-plan-claude.md` (twin),
   Issue Mode bullet "Stops are reported on the issue": the comment's second
   line is `<!-- ai:claude-blocked-session:v1 id=<own session id> -->`, taken
   from `echo "session_${CLAUDE_CODE_REMOTE_SESSION_ID#cse_}"`, and the pickup
   archives that session once `/reclarify` starts its replacement.
4. `workflow-templates/.claude/commands/implement-issue-claude.md` (twin),
   step 0: the blocked comment carries the same second line.
5. CLAUDE.md §28.C (failure escalations, issue mode): the comment also names
   the stopping session with the marker, and the pickup archives that session
   when `/reclarify` starts the next one.
6. `.claude/commands/claude-issue-pickup.md`, **not edited by this stage.**
   The exact diff goes to the supervising session:
   - Preflight tools list: add `list_sessions`.
   - New step 3.4 for each started `issue` entry whose `trigger` is
     `reclarify`:
     - call `list_sessions` once per wake;
     - run `replaced-sessions`;
     - call `get_session` on each id and `archive_session` when the session is
       still IDLE and its title is unchanged.
   - Report line: add `archived <a>`.
   - Rules: "Only start sessions" gains the archive exception and the
     never-archive list.
   - Tool Access: add `list_sessions`, `archive_session` use, and the new
     subcommand.
7. `README.md` (Claude issue implementer, the `/reclarify` paragraph) and
   `agents.md` (the `ai:claude-blocked` lines): one sentence each on the marker
   and the archive.
8. `changelog.d/4817-archive-replaced-blocked-session.md` (`changed`).
9. Tests:
   - `tests/test_claude_issue_route.py`: selection unit tests.
   - `tests/test_implement_issue_claude_command.py`: text tests for the marker
     in both commands and CLAUDE.md, the pickup step, and the allow rules.

## Files & Modules

- `scripts/claude_issue_route.py`
- `workflow-templates/.claude/settings.json` (twin; copied to `.claude/settings.json` by the supervising session)
- `workflow-templates/.claude/commands/implement-plan-claude.md` (twin)
- `workflow-templates/.claude/commands/implement-issue-claude.md` (twin)
- `.claude/commands/claude-issue-pickup.md` (exact diff listed; applied by the supervising session)
- `CLAUDE.md`
- `README.md`, `agents.md`
- `changelog.d/4817-archive-replaced-blocked-session.md` [new]
- `tests/test_claude_issue_route.py`, `tests/test_implement_issue_claude_command.py`
- `docs/plans/issue-4817-archive-replaced-blocked-session-plan.md` [new], `docs/implement-plan/issue-4817-archive-replaced-blocked-session.md` [new]

## Tests

- **Unit (`tests/test_claude_issue_route.py`):**
  - `blocked_comment_session` returns the latest trusted comment's id. It
    returns nothing for an untrusted author, for a latest blocked comment
    without the marker, or for a malformed id.
  - `parse_sessions_listing` handles all three raw shapes and the harness
    wrapper.
  - `select_replaced_sessions`:
    - the named session is archived when it is IDLE;
    - these are kept: `RUNNING`, `REQUIRES_ACTION`, `ARCHIVED`, another repo,
      a protected title (checker, deploy-activate, status check-in, pickup)
      even when named, an excluded id, a named id outside the whitelist, an
      unlisted named id (`named_not_listed`), a title-route session without
      blocked evidence, and `#48170` against `#4817`;
    - the title route archives `issue <repo>#<N> — implement`,
      `implement-issue-claude — #<N> — BLOCKED (…)`, and
      `implement-plan issue-<N>-x — phase 1/1 — BLOCKED` sessions that are
      blocked;
    - the cap is 5.
  - The CLI runs offline with `--comments-json`.
- **Text (`tests/test_implement_issue_claude_command.py`):**
  - the marker sentence is in both command files and CLAUDE.md §28.C;
  - the pickup step 3.4 wording and the `archived <a>` report;
  - the two allow rules in `.claude/settings.json`.
- **Verification:**
  - Run all of `tests/test_claude_issue_route.py` and
    `tests/test_implement_issue_claude_command.py` in a scratch copy of the
    tree with the twins copied over `.claude/` and the pickup diff applied. It
    passes there.
  - In the real tree, only the parity, allow-rule, pickup, and new-marker
    tests fail, as the interim rule expects.

## Risks & Mitigations

- **The wrong session is archived.** Checks run on both routes: the
  whitelist, the repo match, the protected titles, the excluded ids, IDLE only,
  a live `get_session`, and an unchanged title. Archiving is also reversible
  (`unarchive_session`).
- **A consumer repo's comments cannot be read from the pickup.** The script
  fails open to the title route and reports it in `comments_read`.
- **The named session is older than the 100 listed sessions.** It is kept,
  reported as `named_not_listed`, and left for a human.
  ACCEPTED: failing closed is the safe side.
- **The parity and pickup tests fail until the supervising session syncs.**
  ACCEPTED: the operator's interim twin-first rule expects this, and a hold
  claim keeps fixers off the head.

## Rollout

- The phase merges into the project branch, and the final PR merges into
  `main` (`Fixes #4817`).
- The supervising session copies the twins and applies the pickup diff on the
  phase PR branch before it merges.
- The pickup reads the new command text on its next wake. It refreshes to
  `origin/main` at every wake.
- Consumers receive the twins at `@stable`. The pickup runs only in
  coding-workflows.

## Auto-decisions

- AD-1 [plan, 2026-09-28] Where is it decided which session the pickup archives? — Picked: A — a new `claude_issue_route.py replaced-sessions` subcommand; the pickup runs it, confirms each id with `get_session`, and archives. Alternatives: B — selection written as prose in the pickup; C — archive from `/implement-issue-claude` in the new session. Why: the pickup's rule is that the script decides, the rule can be unit-tested (item 4), and the pickup diff applied by hand stays small. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How does a blocked comment name its session? — Picked: A — a second line `<!-- ai:claude-blocked-session:v1 id=session_… -->`. Alternatives: B — a visible `Blocked session:` line; C — a full `— resume.` block. Why: parsing does not depend on Markdown formatting, and there is one representation. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Whose blocked comments can name a session? — Picked: A — only the latest `ai:claude-blocked:v1` comment, and only from a trusted author (`is_trusted_issue_author`). Alternatives: B — any author. Why: §1, an outsider's forged comment must not get a session archived. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Which sessions may be archived at all? — Picked: A — a whitelist on both routes: source repo equals the issue's repo; the title belongs to this issue (`issue <repo>#<N> — implement`, `implement-issue-claude — #<N>…`, `implement-plan issue-<N>-…`) and is not protected (`— checker`, `— waiting:`, `— deploy-activate`, `status check-in`, `Claude issue pickup`); not the new or pickup session; `SESSION_STATUS_IDLE`. The title route also needs blocked evidence (`status_bucket` BLOCKED or `BLOCKED` in the title). Alternatives: B — exclude only protected titles. Why: §1 fails closed, and item 3 forbids archiving checkers, pollers, and `/deploy-activate` sessions. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] Archive only the named session, or also the other stale sessions of the issue? — Picked: A — the named session plus every title-route match, at most 5 per issue per wake. Alternatives: B — the named session only. Why: the operator's cleanup found duplicates (#4618 ×2, #4687 ×2, #4688 ×2), and the `/reclarify` answer makes all of them stale. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-28] Which queue triggers archive? — Picked: A — `reclarify` only. Alternatives: B — also `manual`. Why: the issue names `reclarify`, and `manual` intake runs are operator-driven. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-28] What happens to a named session missing from the pickup's `list_sessions` page? — Picked: A — kept and reported as `named_not_listed`. Alternatives: B — the pickup calls `get_session` and decides itself. Why: the script cannot check its title or repo, so it fails closed. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-28] What if the comment read fails (consumer repo not attached to the pickup session)? — Picked: A — fail open to the title route and report `comments_read: failed: …`. Alternatives: B — archive nothing. Why: §15 fail-open, and the title route keeps the AD-4 checks. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-28] Which blocked stops write the marker? — Picked: A — the issue-mode stops (`/implement-plan-claude` Issue Mode, `/implement-issue-claude` step 0) and the CLAUDE.md §28.C text. Alternatives: B — also the legacy dispatcher's routine-run stop. Why: §5; a routine run matches no whitelisted title and is never a `/reclarify` predecessor. Applied in: phase 1 PR. Status: pending review
- AD-10 [plan, 2026-09-28] How does the pickup edit land without a twin? — Picked: A — this stage lists the exact diff in its `ai:claude-blocked` comment and the phase PR body. The supervising session applies it with the twin sync, and the pickup-text test fails until then. Alternatives: B — leave the pickup unchanged. Why: the issue says so (interim twin-first rule, #4750 Q40: A). Applied in: phase 1 PR. Status: pending review
- AD-11 [plan, 2026-09-28] How is the new subcommand allowlisted? — Picked: A — two new `permissions.allow` rules for `replaced-sessions` in the settings twin. Alternatives: B — hide the logic behind a flag of the allowlisted `queue-pending`. Why: a narrow rule for each subcommand, and the guard file change is reviewed at the twin sync. Applied in: phase 1 PR. Status: pending review

## References

- Issue #4817; interim twin-first rule: #4750 (operator comment, Q40: A); twin-first sync: #4785.
- `.claude/commands/claude-issue-pickup.md`, `.claude/commands/implement-plan-claude.md` (Issue Mode), `.claude/commands/implement-issue-claude.md`, CLAUDE.md §15, §25, §28.C.
