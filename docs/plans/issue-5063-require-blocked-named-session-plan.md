# Require blocked evidence and Claude-session provenance before `replaced-sessions` archives a named session

Source issue: shubhodeep1/coding-workflows#5063 (https://github.com/shubhodeep1/coding-workflows/issues/5063)
Base branch: claude/implement-plan-issue-4817-archive-replaced-blocked-session
Security pass: skip (ai:security: automation-produced issue)

## Summary

`scripts/claude_issue_route.py replaced-sessions` archives the session named by the
latest trusted `ai:claude-blocked:v1` comment even when that session is not blocked, so
a collaborator can forge a blocked comment that gets an idle, working session of the
issue archived on the next `/reclarify`. This plan makes the named session pass the same
blocked-evidence check as every other candidate, and counts a blocked comment only when
a Claude session posted it (GitHub App `claude`).

## Context

- Issue #5063 (security-audit finding
  `forged-blocked-comment-archives-nonblocked-session`, A01 Broken Access Control,
  medium, confidence 9/10) points at `scripts/claude_issue_route.py:1215`, in
  `select_replaced_sessions`. The condition there is
  `elif not is_named and session.get("status_bucket") != SESSION_BUCKET_BLOCKED and not re.search(r"\bBLOCKED\b", title)`,
  so the named session skips the blocked check.
- The code comes from issue #4817 (project branch
  `claude/implement-plan-issue-4817-archive-replaced-blocked-session`, final PR #4827,
  still a draft), which is why this issue names that branch as its integration branch.
- `blocked_comment_session` trusts any comment whose author passes
  `is_trusted_issue_author` (OWNER, MEMBER, COLLABORATOR, or `github-actions[bot]`). A
  collaborator passes that check.
- Evidence from this account (2026-09-29): every recent `ai:claude-blocked:v1` and
  `ai:claude-issue-progress:v1` comment in this repo has
  `performed_via_github_app.slug == "claude"`: the Claude sessions post through the
  Claude GitHub App (MCP or the web agent proxy). Of the idle sessions named by such
  comments, two had `status_bucket` `SESSION_STATUS_BUCKET_BLOCKED`
  (`session_018qE7…`, `session_01D8zy…`), and one had `…_REVIEW_READY`
  (`session_016HSP…`).
- CLAUDE.md §1 (security first), §5 (minimal change), §6 (no renames), §7 (docs on
  behaviour change), §15 (no new API calls), §20 (changelog fragment).

## Goals

1. `select_replaced_sessions` archives a named session only when it passes the
   blocked-evidence check the title route already uses: `status_bucket` is
   `SESSION_STATUS_BUCKET_BLOCKED`, or its title has the word `BLOCKED`. Otherwise it is
   kept with reason `not_blocked`. The existing idle, same-repository,
   title-of-this-issue, not-protected, and not-excluded checks still apply.
2. `blocked_comment_session` counts a comment only when it passes
   `is_trusted_issue_author` **and** was posted through the Claude GitHub App
   (`performed_via_github_app.slug == "claude"`). A comment that fails this check can
   neither name a session nor hide an earlier genuine one.
3. Unit tests cover both rules, including the forged comment from issue #5063's
   exploit scenario.
4. README.md, agents.md, the script's module docstring, and the function docstrings
   describe the new rules. A `changelog.d/` fragment (`security`) records the fix.

## Non-goals

- No change to `.claude/**` (the pickup command and its twin). The pickup already says
  "the script decides". Its bullet about the named session gets less exact, but that
  changes nothing the pickup does (AD-3).
- No change to CLAUDE.md. Its one-line summary ("it archives the named session") still
  holds for a session that is still blocked (AD-4).
- No widening of `issue_session_title`. Titles like `Issue #4985 — implement`, which
  carry no repo, are still not matched. That is a separate gap, noted under
  `## Notes` (AD-5).
- No new CLI flags, env vars, API calls, or output fields.

## Constraints

- §6: no identifier is renamed or removed. New: the constant `BLOCKED_COMMENT_APP_SLUG`
  and the function `is_claude_session_comment`, both checked for clashes (no hits in
  the repo). The output keys and `reason` values stay the same.
- §15: no new GitHub API call. `performed_via_github_app` is already in the paginated
  comments read.
- §1: when unsure, keep the session. Keeping one wrongly costs nothing, and the user can
  archive it by hand. Archiving one wrongly can kill a live hand-back Routine.

## Approach

- **Blocked evidence for the named session.** Drop `not is_named and` from the
  `not_blocked` condition, so the named session needs the same evidence as a title
  match. A name then only puts that session first (at most 5 are archived) and shows up
  in the report (`named`, `named_not_listed`). Being named no longer widens what can be
  archived, so a forged name gives an attacker nothing.
- **Provenance for the blocked comment.** A new pure helper,
  `is_claude_session_comment(comment)`, returns true when `is_trusted_issue_author`
  holds and `performed_via_github_app.slug` equals `BLOCKED_COMMENT_APP_SLUG`
  (`"claude"`). `blocked_comment_session` filters with it before it picks the latest.
  A comment posted without the app (the owner by hand, a local CLI session with a PAT,
  an older comment) names nothing. The pickup then falls back to the title route,
  which now has the same blocked requirement, so nothing is lost beyond ordering.
- Alternatives are recorded as AD-1 and AD-2.

## Phases & Merge Strategy

This plan is **one phase**. Issue mode (CLAUDE.md §28.A, `/implement-issue-claude`)
authorises a single-phase plan: the issue fixes the scope, and the change is one
function pair plus its tests and docs.

1. **Phase 1 — named-session blocked check and Claude-app provenance.**
   - Files: `scripts/claude_issue_route.py`, `tests/test_claude_issue_route.py`,
     `README.md`, `agents.md`,
     `changelog.d/5063-require-blocked-named-session.md` [new], plus the plan doc and
     the progress log.
   - Done when: the goals above hold, `pytest tests/test_claude_issue_route.py` and
     the repo's standard checks pass, and the exploit scenario's test keeps the forged
     session.
   - Rollback: revert the phase PR. The earlier behaviour comes back in full, and no
     data or state is involved.

## Implementation Steps

Phase 1:
1. `scripts/claude_issue_route.py`, constants block (around the #4817 constants):
   add `BLOCKED_COMMENT_APP_SLUG = "claude"` with a comment citing #5063.
2. Same file, after `is_trusted_issue_author`: add `is_claude_session_comment(item)`.
   It is pure and returns `is_trusted_issue_author(item)` and
   `performed_via_github_app` is a dict whose `slug` equals `BLOCKED_COMMENT_APP_SLUG`.
3. `blocked_comment_session`: filter with `is_claude_session_comment` instead of
   `is_trusted_issue_author`, and update its docstring.
4. `select_replaced_sessions`: remove `not is_named and` from the `not_blocked`
   condition, and update its docstring (a named session needs blocked evidence too).
5. Update the module docstring's `replaced-sessions` bullet and the #4817 constants
   comment.
6. `tests/test_claude_issue_route.py`: give `_blocked_issue_comment` a
   `performed_via_github_app` slug parameter (default `"claude"`). Change
   `test_select_archives_the_named_idle_session_first_then_blocked_title_matches` so
   the named session carries blocked evidence. Add tests for:
   - a named, idle, not-blocked session kept as `not_blocked` (the #5063 exploit);
   - a collaborator's comment without the app, and one from another app, naming
     nothing;
   - a later forged comment that cannot hide an earlier genuine one;
   - the CLI end to end with a forged comment.
7. README.md (the #4817 paragraph near line 1327) and agents.md (near line 297): state
   both rules.
8. `changelog.d/5063-require-blocked-named-session.md` with `<!-- changelog: security -->`.

## Files & Modules

- `scripts/claude_issue_route.py`
- `tests/test_claude_issue_route.py`
- `README.md`
- `agents.md`
- `changelog.d/5063-require-blocked-named-session.md` [new]
- `docs/plans/issue-5063-require-blocked-named-session-plan.md` [new]
- `docs/implement-plan/issue-5063-require-blocked-named-session.md` [new]

## Tests

- Unit: `tests/test_claude_issue_route.py` (steps above). The whole file must pass.
- Repo checks: the `ci.yml` Python test steps that touch this module, and a full
  `pytest tests/` run where feasible.
- End to end: the chain's conformance audit and runtime validation. The pickup's
  behaviour is covered by the CLI test, which runs `replaced-sessions` offline.

## Risks & Mitigations

- A genuinely blocked session that the platform reports as `REVIEW_READY` (seen once,
  `session_016HSP…`) is no longer archived automatically. ACCEPTED: it stays open and
  costs nothing, the user can archive it, and the issue's recommendation requires the
  blocked check (AD-1).
- The Claude GitHub App slug could differ in some environment. Then the named route
  names nothing, and the title route still archives sessions with blocked evidence.
  Fail-safe (AD-2).
- Merge conflicts with the #4817 project branch while its conformance stage is in
  flight. Mitigation: the chain syncs from the base branch at every stage.

## Rollout

Lands on the #4817 project branch through this project's final PR. It reaches `main`
with #4817's final PR #4827, and consumer repos with the next `@stable` sync. No flag,
no migration.

## References

- Issue #5063, issue #4817, PR #4846 (4817 phase 1), draft final PR #4827.
- Tracker: `Refs #3576` (security-audit tracker).

## Auto-decisions

- AD-1 [plan, 2026-09-29] Must the session named by a blocked comment show blocked evidence before it is archived? — Picked: A — yes, the same evidence as the title route (bucket `SESSION_STATUS_BUCKET_BLOCKED` or `BLOCKED` in the title); a name only sets priority. Alternatives: B — skip the check only for a Claude-app comment; C — keep as is and rely on provenance only. Why: this is the issue's recommendation and §1 puts security first; keeping a session costs nothing, while B and C still let a collaborator's own Claude session forge a name. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What provenance must a blocked comment have to name a session? — Picked: A — a trusted author (`is_trusted_issue_author`) and `performed_via_github_app.slug == "claude"`. Alternatives: B — `author_association` OWNER only; C — keep the trusted-author rule alone. Why: every observed session comment carries the `claude` app; B would break consumer orgs whose sessions post as MEMBER; C leaves the forged path open. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Should the pickup command (`.claude/commands/claude-issue-pickup.md`) be updated to describe the new rules? — Picked: A — no; describe them in README.md, agents.md, and the script docstrings. Alternatives: B — edit the pickup command and its `workflow-templates/` twin. Why: the pickup already defers to the script, and a `.claude/**` edit would stop an unattended phase (§28.C). Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-29] Should CLAUDE.md §28.C's summary line change? — Picked: A — no. Alternatives: B — add "if it is still blocked". Why: §5 minimal change; the summary stays true, and the detailed rules live in README.md and agents.md. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] Should `issue_session_title` also match dispatcher titles like `Issue #<N> — implement` (no repo)? — Picked: A — no, out of scope; record it under Notes. Alternatives: B — widen the patterns in this PR. Why: §5; the gap only means fewer sessions are archived, and the security fix does not depend on it. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` result: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Observed gap (AD-5): sessions titled `Issue #<N> — implement`, the title the pickup gives many live sessions, are not matched by `issue_session_title`, so `replaced-sessions` never archives them.
