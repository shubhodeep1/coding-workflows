# Implement-Plan Log — Check `.claude/` guard drift against a PR's own base in the Claude-asset sync

- Plan: docs/plans/issue-5260-asset-sync-pr-base-drift-plan.md
- Source issue: shubhodeep1/coding-workflows#5260
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5260-asset-sync-pr-base-drift   Final PR: #5280 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #5282: twin sync (`[claude-twin-sync]` of `workflow-templates/.claude/commands/implement-plan-claude.md` and `workflow-templates/.claude/commands/fix-claude-pr.md` into `.claude/commands/`), then `/reclarify` on #5260
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (twin-first stop: the stage `/reclarify` resumes arms the wait on PR #5282)
- Last updated: 2026-09-29
- Last note: phase 1 implemented twin-first and opened as PR #5282 (hold claim posted); waiting for the `[claude-twin-sync]` copy and `/reclarify` on #5260.

## Phases
1. [ ] Phase 1 — base-aware drift check in the Claude-asset sync — PR #5282 open (twin sync pending); review rounds: 0; interventions: 0 — protected paths: `.claude/commands/implement-plan-claude.md`, `.claude/commands/fix-claude-pr.md`
   - Edit the `workflow-templates/.claude/**` twins only (interim twin-first default); the `.claude/` copies follow via `[claude-twin-sync]`.
   - `implement-plan-claude.md` `### Claude-asset sync` steps 1–4: fetch the PR base, default and base drift checks, project-base verification, base merge for other bases.
   - `fix-claude-pr.md` step 5: the stop also covers a failed project-base verification.
   - `tests/test_claude_asset_sync_command.py`: text assertions plus executed scratch-repo scenarios (issue #5260 case; verification).
   - `agents.md` Claude-asset sync bullet; `changelog.d/5260-asset-sync-pr-base-drift.md` [new].
   - Done: `tests/test_claude_asset_sync_command.py` passes for the `workflow-templates` parametrization (both after the twin copy), and `tests/test_implement_plan_claude_command.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_session_start_claude_assets_drift.py`, `tests/test_update_workflows_guardrails.py` pass apart from the expected twin-parity checks.

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which branches define staleness for a PR head? — Picked: A — both the default branch and the PR's base (two three-dot diffs). Alternatives: B — the PR's base only; C — the default branch only (today). Why: the issue asks for both; B would miss a default-branch fix the base has not merged yet. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] A PR head on a base that does not land in the default branch lacks its base's guard change: merge the base in? — Picked: A — merge `origin/<base>` into the head. Alternatives: B — skip and record `claude_assets=stale (base <base>)` as for default-only drift. Why: a PR lands in its base, so merging it carries nothing unrelated, and B leaves the old guard running. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How is the synced project base verified? — Picked: A — `git fetch origin <project branch>` then `git diff --quiet origin/<project branch>...origin/<default> -- .claude/hooks .claude/settings.json`, stop with the caller's blocker on a non-zero exit. Alternatives: B — `git merge-base --is-ancestor origin/<default> origin/<project branch>`; C — no verification. Why: A uses an allowlisted command (B's `git merge-base` would prompt in an unattended session) and checks exactly the guarded paths; the issue asks for verification. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Should the SessionStart drift log also check the PR's base? — Picked: A — no, keep it default-only. Alternatives: B — compare against the branch's upstream tracking ref. Why: the hook cannot learn a PR's base without a GitHub API call (§15), the log is diagnostic only, and the command sync is where the merge happens (§5). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] How is the changelog updated? — Picked: A — a new `changelog.d/5260-asset-sync-pr-base-drift.md`, #4952's fragment unchanged. Alternatives: B — edit `changelog.d/4952-claude-asset-sync.md`. Why: §20.B one fragment per PR; both fragments release together with #4995. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Started by the Claude issue dispatcher routine (`dispatch shubhodeep1/coding-workflows#5260: start`) into session `session_01HMaxnWJosFZFkojR4Z83GA` (permission mode `auto`).
- Security pass: skip (`security_pass_skip.py`: `ai:security: created and labelled by the issue automation`).
- Issue base `claude/implement-plan-issue-4952-sync-claude-assets-at-session-start` is the head of the open draft final PR #4995 (base `main`); no merged PR has that head, so the base has not moved.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
- Twin-first verification: with the two twins copied over `.claude/commands/` in a scratch copy, `tests/test_claude_asset_sync_command.py`, `tests/test_session_start_claude_assets_drift.py`, `tests/test_implement_plan_claude_command.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_check_in_status_hand_back.py`, and `tests/test_update_workflows_guardrails.py` pass (223 passed, 1 skipped), as do `tests/test_permission_prompts.py`, `tests/test_check_in_session_targeting.py`, `tests/test_check_in_status.py`, and `tests/test_ingest_implement_plan_lessons.py` (316 passed); `tests/test_session_start_extract_repo_slug.py` prints PASS. On the branch as pushed only the `.claude/commands` parametrizations and the twin-parity checks fail, as expected until the sync.
- Twin sync needed (sha256 of the twins at 10929bc): `workflow-templates/.claude/commands/implement-plan-claude.md` → `.claude/commands/implement-plan-claude.md` `c0a46399aed90a3e4a757053642ff7635084912fb52a8204691d4e159e689b1d`; `workflow-templates/.claude/commands/fix-claude-pr.md` → `.claude/commands/fix-claude-pr.md` `0b5400ff0c82f3836a3987f05d538b76632704157f04cba6b11f55f237bfff8a`. No `.claude/` path without a twin changed.
