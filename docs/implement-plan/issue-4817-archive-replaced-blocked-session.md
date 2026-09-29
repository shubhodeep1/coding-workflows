# Implement-Plan Log — Archive the blocked session when /reclarify starts its replacement

- Plan: docs/plans/issue-4817-archive-replaced-blocked-session-plan.md
- Source issue: shubhodeep1/coding-workflows#4817 (https://github.com/shubhodeep1/coding-workflows/issues/4817)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4817-archive-replaced-blocked-session   Final PR: #4827 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4846
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: project checker armed right after the review-round push (ids in the stage report and the issue progress comment)
- Last updated: 2026-09-29
- Last note: resumed after the twin sync (`f523f30`, #4817 Q1: A); synced the project branch with main (`e4d6391`); review round 1 on `f523f30`: fixed the `parse_sessions_listing` candidate cap and the preamble-array acceptance and the quoted-marker match, rejected 2 findings with reasons.

## Phases
1. [ ] Phase 1 — blocked-session marker, `replaced-sessions` selection, pickup archive step   — PR #4846 open (twin sync `f523f30` 2026-09-29); review rounds: 1; interventions: 0 — protected paths: `.claude/settings.json`, `.claude/commands/implement-plan-claude.md`, `.claude/commands/implement-issue-claude.md` (edited through their `workflow-templates/.claude/` twins), `.claude/commands/claude-issue-pickup.md` (no twin; exact diff listed for the supervising session)
   - [x] `scripts/claude_issue_route.py`: `blocked_comment_session`, `parse_sessions_listing`, `select_replaced_sessions`, `fetch_issue_comments`, CLI `replaced-sessions`
   - [x] `workflow-templates/.claude/settings.json`: two `replaced-sessions` allow rules
   - [x] `workflow-templates/.claude/commands/implement-plan-claude.md` + `implement-issue-claude.md`: blocked-session marker line
   - [x] CLAUDE.md §28.C, README.md, agents.md: marker + archive sentence
   - [x] `claude-issue-pickup.md` exact diff (for the supervising session)
   - [x] tests: `tests/test_claude_issue_route.py`, `tests/test_implement_issue_claude_command.py`; changelog fragment

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
- [source:intervention] When a script locates a JSON result inside a harness-saved tool-result file, scan every `[` / `{` lazily and accept only a candidate whose shape is the expected result; a fixed candidate cap, or accepting any JSON array, misses the result behind a bracket-heavy preamble. (files: scripts/claude_issue_route.py)
- [source:intervention] A hidden HTML-comment marker that a script parses from an issue comment must be matched on a line of its own: the same comment often quotes the marker in prose or a diff, and an unanchored match counts the quote. (files: scripts/claude_issue_route.py)
- [source:plan-deviation] A new test helper in a large test module must be checked against the module's existing private helpers too (`_comment` already existed in tests/test_claude_issue_route.py); a shadowing helper silently breaks earlier tests. (files: tests/test_claude_issue_route.py)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher (trigger `trig_016YvL4hv95AGN5WB5Xz79Rq`) in session `session_012VrDSaYG3hUdX8hAvmRAUf`; permission mode auto.
- Security pass: run (`security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`).
- Protected-path approval: phase 1 — interim twin-first rule (operator #4750 Q40: A, restated in the #4817 body: "Edits to `.claude/commands/**` follow the interim twin-first rule until #4785 lands") (2026-09-28). The phase edits only the `workflow-templates/.claude/` twins, pushes, posts a `hold` claim, and stops BLOCKED listing the files to copy and the exact `claude-issue-pickup.md` diff.
- Phase 1 verification (2026-09-28): real tree `tests/test_claude_issue_route.py` 228 passed; scratch copy with twins synced and the pickup diff applied: CI step files 309 passed, the 60 test files referencing the changed files 2766 passed, 8 environmental failures (7 need a `.git` directory and pass in the real checkout, 1 needs `gawk`, not installed here). Pickup diff sha256 of the result: 5df0a2100a8146e8911d196e65996bab0676ac0eef7d2485429cbb21ea6d0bb2.
- Resumed 2026-09-29 in session `session_0176578RCQhTAxSESw6Zjbjq` (started by the master session, operator Q14: A, after `/reclarify`). The blocked session `session_012VrDSaYG3hUdX8hAvmRAUf` was already archived. Project branch synced with `origin/main` (clean merge, `e4d6391`).
- Review round 1 (head `f523f30`, ledger `4ced9e51…`): fixed — `parse_sessions_listing` 50-candidate cap (consensus, plus the task gap) and its latency note (lazy scan); fixed proactively — a JSON array of non-sessions in the preamble was taken as an empty listing, and `BLOCKED_SESSION_MARKER_RE` counted a marker quoted inside a line, so #4817's own blocked comment (which quotes it) read as `several_markers` and named nothing; the marker now counts only on a line of its own. Rejected — `str.removesuffix` (runtime is Python 3.11 in sessions and 3.12 in CI); exit 2 not retried (deliberate fail-closed: a missed archive leaves the session as it was before this change).
