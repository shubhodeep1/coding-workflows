# Implement-Plan Log — Archive the blocked session when /reclarify starts its replacement

- Plan: docs/plans/issue-4817-archive-replaced-blocked-session-plan.md
- Source issue: shubhodeep1/coding-workflows#4817 (https://github.com/shubhodeep1/coding-workflows/issues/4817)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4817-archive-replaced-blocked-session   Final PR: draft (opened right after this commit; number in the issue progress comment)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: project branch opened; implementing phase 1 under the interim twin-first rule.

## Phases
1. [ ] Phase 1 — blocked-session marker, `replaced-sessions` selection, pickup archive step   — protected paths: `.claude/settings.json`, `.claude/commands/implement-plan-claude.md`, `.claude/commands/implement-issue-claude.md` (edited through their `workflow-templates/.claude/` twins), `.claude/commands/claude-issue-pickup.md` (no twin; exact diff listed for the supervising session)
   - [ ] `scripts/claude_issue_route.py`: `blocked_comment_session`, `parse_sessions_listing`, `select_replaced_sessions`, `fetch_issue_comments`, CLI `replaced-sessions`
   - [ ] `workflow-templates/.claude/settings.json`: two `replaced-sessions` allow rules
   - [ ] `workflow-templates/.claude/commands/implement-plan-claude.md` + `implement-issue-claude.md`: blocked-session marker line
   - [ ] CLAUDE.md §28.C, README.md, agents.md: marker + archive sentence
   - [ ] `claude-issue-pickup.md` exact diff (for the supervising session)
   - [ ] tests: `tests/test_claude_issue_route.py`, `tests/test_implement_issue_claude_command.py`; changelog fragment

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Where is it decided which session the pickup archives? — Picked: A — a new `claude_issue_route.py replaced-sessions` subcommand; the pickup runs it, confirms each id with `get_session`, and archives. Alternatives: B — selection written as prose in the pickup; C — archive from `/implement-issue-claude` in the new session. Why: the pickup's rule is that the script decides, the rule can be unit-tested, and the pickup diff applied by hand stays small. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How does a blocked comment name its session? — Picked: A — a second line `<!-- ai:claude-blocked-session:v1 id=session_… -->`. Alternatives: B — a visible `Blocked session:` line; C — a full `— resume.` block. Why: parsing does not depend on Markdown formatting, and there is one representation. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Whose blocked comments can name a session? — Picked: A — only the latest `ai:claude-blocked:v1` comment, and only from a trusted author (`is_trusted_issue_author`). Alternatives: B — any author. Why: §1, an outsider's forged comment must not get a session archived. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Which sessions may be archived at all? — Picked: A — a whitelist on both routes: same source repo; the title belongs to this issue and is not protected (`— checker`, `— waiting:`, `— deploy-activate`, `status check-in`, `Claude issue pickup`); not the new or pickup session; `SESSION_STATUS_IDLE`. The title route also needs blocked evidence. Alternatives: B — exclude only protected titles. Why: §1 fails closed, and item 3 forbids archiving checkers, pollers, and `/deploy-activate` sessions. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] Archive only the named session, or also the other stale sessions of the issue? — Picked: A — the named session plus every title-route match, at most 5 per issue per wake. Alternatives: B — the named session only. Why: the operator's cleanup found duplicates, and the `/reclarify` answer makes all of them stale. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-28] Which queue triggers archive? — Picked: A — `reclarify` only. Alternatives: B — also `manual`. Why: the issue names `reclarify`, and `manual` intake runs are operator-driven. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-28] What happens to a named session missing from the pickup's `list_sessions` page? — Picked: A — kept and reported as `named_not_listed`. Alternatives: B — the pickup calls `get_session` and decides itself. Why: the script cannot check its title or repo, so it fails closed. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-28] What if the comment read fails? — Picked: A — fail open to the title route and report `comments_read: failed: …`. Alternatives: B — archive nothing. Why: §15 fail-open, and the title route keeps the AD-4 checks. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-28] Which blocked stops write the marker? — Picked: A — the issue-mode stops (`/implement-plan-claude` Issue Mode, `/implement-issue-claude` step 0) and CLAUDE.md §28.C. Alternatives: B — also the legacy dispatcher's routine-run stop. Why: §5; a routine run is never a `/reclarify` predecessor. Applied in: phase 1 PR. Status: pending review
- AD-10 [plan, 2026-09-28] How does the pickup edit land without a twin? — Picked: A — the exact diff goes in the stage's `ai:claude-blocked` comment and the phase PR body, and the supervising session applies it with the twin sync. Alternatives: B — leave the pickup unchanged. Why: the issue says so (interim twin-first rule, #4750 Q40: A). Applied in: phase 1 PR. Status: pending review
- AD-11 [plan, 2026-09-28] How is the new subcommand allowlisted? — Picked: A — two new `permissions.allow` rules for `replaced-sessions` in the settings twin. Alternatives: B — a flag on the allowlisted `queue-pending`. Why: a narrow rule for each subcommand, reviewed at the twin sync. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher (trigger `trig_016YvL4hv95AGN5WB5Xz79Rq`) in session `session_012VrDSaYG3hUdX8hAvmRAUf`; permission mode auto.
- Security pass: run (`security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`).
- Protected-path approval: phase 1 — interim twin-first rule (operator #4750 Q40: A, restated in the #4817 body: "Edits to `.claude/commands/**` follow the interim twin-first rule until #4785 lands") (2026-09-28). The phase edits only the `workflow-templates/.claude/` twins, pushes, posts a `hold` claim, and stops BLOCKED listing the files to copy and the exact `claude-issue-pickup.md` diff.
