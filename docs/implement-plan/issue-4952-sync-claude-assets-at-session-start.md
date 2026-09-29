# Implement-Plan Log — Sync the default branch's `.claude/` guards into long-running working branches

- Plan: docs/plans/issue-4952-sync-claude-assets-at-session-start-plan.md
- Source issue: shubhodeep1/coding-workflows#4952
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4952-sync-claude-assets-at-session-start   Final PR: draft (number recorded in the issue progress comment and the first phase PR's log commit)
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: `[claude-twin-sync]` on the phase 1 PR (master session), then `/reclarify` or a direct wake
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented twin-first (Q40): only the `workflow-templates/.claude/**` twins changed; the phase PR carries a `hold` claim and waits for the master's `[claude-twin-sync]` of the four twins into `.claude/` (the hook needs the Q62/Q64 window).

## Phases
1. [ ] Phase 1 — asset sync in the commands, drift log in the hook, tests, docs — phase PR open, twin sync pending; review rounds: 0; interventions: 0 — protected paths: `.claude/commands/implement-plan-claude.md`, `.claude/commands/implement-issue-claude.md`, `.claude/commands/fix-claude-pr.md`, `.claude/hooks/session-start.sh`
   - Edit the `workflow-templates/.claude/**` twins first, then copy them into `.claude/` byte-identical.
   - `implement-plan-claude.md`: `### Claude-asset sync` under `## Helpers`, cited from step 2, step 7 Blocked, and step 7a.
   - `implement-issue-claude.md` step 4: the resume runs the sync through `/implement-plan-claude` step 2.
   - `fix-claude-pr.md` step 5: run the sync after the checkout, with a hold-claim blocker on a `.claude/**` conflict.
   - `session-start.sh`: `report_claude_assets_drift` logs `[session-start] claude_assets=stale behind=<n> files=<list>`, never fails the hook.
   - Tests: the command text and twin parity in `tests/test_implement_plan_claude_command.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_claude_asset_sync_command.py` [new]; drift-log cases in `tests/test_session_start_extract_repo_slug.py`; `ci.yml` step for the new file.
   - `agents.md` stable log prefix and the helper paragraph; `changelog.d/4952-claude-asset-sync.md`.
   - Done: all of the above present, and the listed suites plus `tests/test_update_workflows_guardrails.py` and `tests/test_check_in_status_hand_back.py` pass.

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where does the sync live? — Picked: A — command text using git commands `.claude/settings.json` already allows. Alternatives: B — a new `.claude/scripts/sync_claude_assets.py` helper. Why: the issue asks for command text; A adds no permission surface. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Which working branches get the default branch merged in? — Picked: A — only branches whose integration line ends in the default branch; others skip and record `claude_assets=stale`. Alternatives: B — always merge; C — overlay files uncommitted. Why: merging the default branch into a branch bound for `stable` or another PR's head carries unrelated commits into that base. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] A PR head whose base is a lagging project branch? — Picked: A — sync the project branch first, then merge it into the PR head. Alternatives: B — merge the default branch straight into the PR head; C — skip. Why: the PR diff stays limited to its own change. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Where does `/fix-claude-pr` report a `.claude/**` conflict? — Picked: A — hold claim plus an `ai:claude-blocked:v1` PR comment and one `PushNotification`. Alternatives: B — comment on the source issue. Why: the hold claim is the command's existing stop and blocks a second fixer. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] A sync merge that conflicts only outside `.claude/`? — Picked: A — the caller's existing conflict rule applies. Alternatives: B — stop on any conflict. Why: the issue makes only `.claude/**` conflicts a stop. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] "Must not fetch when offline" in the hook? — Picked: A — at most one bounded fetch, every failure ignored. Alternatives: B — never fetch. Why: A never blocks or fails the hook, and still refreshes a `source_revision` clone. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] Does the drift log run in local sessions? — Picked: A — only inside the `CLAUDE_CODE_REMOTE=true` gate. Alternatives: B — every session. Why: the hook documents that local sessions are unaffected. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-29] Which diff decides "stale"? — Picked: A — three-dot `HEAD...origin/<default>`. Alternatives: B — two-dot. Why: a branch's own guard edits are not staleness. Applied in: phase 1. Status: pending review

## Lessons
- [source:plan-deviation] Put new hook behaviour tests in their own pytest file next to a plain-script contract test, and register that file in a `ci.yml` step, rather than extending the plain-script runner. (files: tests/test_session_start_claude_assets_drift.py, .github/workflows/ci.yml)

## Notes
- Started by the Claude issue dispatcher routine (`dispatch shubhodeep1/coding-workflows#4952: deliver`) into session `session_012jKBsy2vcNfjmnK3STfpbp` (permission mode `auto`).
- Security pass: run (`security_pass_skip.py`: `no skip label`).
- Phase 1 is protected-path (CLAUDE.md §28.C) and has no `Protected-path approval: phase 1` line yet, so the chain stops before phase 1. The standing operator answer for this case is Q40 twin-first (`docs/operations/master-session.md`); the `session-start.sh` copy additionally needs the Q62/Q64 approval window.
- Protected-path approval: phase 1 — twin-first per Q40 (2026-09-29) (answered by the repo owner on #4952, comment 5883394764; relayed by master session `session_01LF9aeTnk15B7e9mKy7vDNM`).
- Plan deviation: the drift-log tests live in the new `tests/test_session_start_claude_assets_drift.py` (pytest, own `ci.yml` step) instead of extending the plain-script `tests/test_session_start_extract_repo_slug.py`; the command-text tests are all in `tests/test_claude_asset_sync_command.py`.
- Twin-first verification: with the four twins copied over `.claude/` in a scratch worktree, `tests/test_claude_asset_sync_command.py`, `tests/test_session_start_claude_assets_drift.py`, `tests/test_implement_plan_claude_command.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_check_in_status_hand_back.py`, and `tests/test_update_workflows_guardrails.py` pass (196 passed), and `tests/test_session_start_extract_repo_slug.py` prints PASS. On the branch as pushed, only the twin-parity assertions fail, as expected until the sync.
