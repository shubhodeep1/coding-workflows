# Implement-Plan Log — Sync the default branch's `.claude/` guards into long-running working branches

- Plan: docs/plans/issue-4952-sync-claude-assets-at-session-start-plan.md
- Source issue: shubhodeep1/coding-workflows#4952
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4952-sync-claude-assets-at-session-start   Final PR: #4995 draft
- Status: BLOCKED
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5010: twin sync (`[claude-twin-sync]` of `workflow-templates/.claude/commands/implement-plan-claude.md` from review round 2), then `/reclarify` on #4952
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: project checker session_012niGV5xiV8hoE895nP1LjQ (idle, no pending check-in; reused by the next wait); stage session session_01JoURMcPWZoFtjYMZJLUWzR
- Last updated: 2026-09-29
- Last note: review round 2 on PR #5010 (head 9a2290f, workflow round 1 after the twin sync): 1 consensus finding (step 2's project-branch merge vs the `[claude-asset-sync]` subject) fixed twin-first by clarifying which command step 2 uses (AD-12); project branch synced with `main` and the resulting `agents.md` conflict resolved on the phase branch; waiting for the master's `[claude-twin-sync]` of `commands/implement-plan-claude.md`.

## Phases
1. [ ] Phase 1 — asset sync in the commands, drift log in the hook, tests, docs — PR #5010 open; review rounds: 2 (round 1 fixed and synced; round 2 fixed twin-first, twin sync pending); interventions: 0 — protected paths: `.claude/commands/implement-plan-claude.md`, `.claude/commands/implement-issue-claude.md`, `.claude/commands/fix-claude-pr.md`, `.claude/hooks/session-start.sh`
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
- AD-9 [phase 1/1 — review round, 2026-09-29] What does the drift hook log when shallow history has no merge base? — Picked: A — `claude_assets=diverged behind=unknown files=<list>`, never `stale`. Alternatives: B — stay silent; C — deepen the fetch until a merge base appears. Why: a two-dot diff cannot tell the branch's own edits from upstream ones; A keeps the signal without the false claim, and C adds unbounded network. Applied in: PR #5010. Status: pending review
- AD-10 [phase 1/1 — review round, 2026-09-29] How does the hook find the default branch when `origin/HEAD` is unset (as in Claude Code Web clones)? — Picked: A — one bounded `git ls-remote --symref origin HEAD`, else `main`. Alternatives: B — probe local `origin/main` / `origin/master` refs; C — keep the `main` fallback. Why: A is correct for any default-branch name and costs one ref advertisement only when `origin/HEAD` is unset. Applied in: PR #5010. Status: pending review
- AD-11 [phase 1/1 — review round, 2026-09-29] What does the hook do when GNU `timeout` is missing? — Picked: A — skip the network calls and use the refs on disk. Alternatives: B — a background-kill timer in bash; C — run unbounded. Why: AD-6 promises a bounded fetch; A keeps that promise with no new code paths. Applied in: PR #5010. Status: pending review
- AD-12 [phase 1/1 — review round, 2026-09-29] Step 2's project-branch merge is called the Claude-asset sync but uses `git merge --no-edit`, not the sync's `[claude-asset-sync]` subject. Which way is it made consistent? — Picked: A — keep step 2's command and subject and state that it takes the place of the sync's step 4, with `[claude-asset-sync]` marking only the sync merge into a PR head. Alternatives: B — change step 2 to `git merge --no-ff -m "[claude-asset-sync] …"`. Why: step 2's merge carries every default-branch change, not only `.claude/` ones, and predates #4952; no tooling reads the subject (§5). Applied in: PR #5010. Status: pending review

## Lessons
- [source:intervention] Test a documented git command by running it in a scratch repository, not by string-matching the prose: `git merge --no-edit` never sets a custom merge subject, and `HEAD@{1}` needs a reflog; use `git merge --no-ff -m "<subject>"` and `HEAD^1`. (files: .claude/commands/implement-plan-claude.md, tests/test_claude_asset_sync_command.py)
- [source:plan-deviation] Put new hook behaviour tests in their own pytest file next to a plain-script contract test, and register that file in a `ci.yml` step, rather than extending the plain-script runner. (files: tests/test_session_start_claude_assets_drift.py, .github/workflows/ci.yml)
- [source:intervention] When a command step says its action "is also" a procedure defined elsewhere, name which of that procedure's steps it replaces and whose command and commit subject win; otherwise reviewers read two commands for one action. (files: .claude/commands/implement-plan-claude.md)

## Notes
- Started by the Claude issue dispatcher routine (`dispatch shubhodeep1/coding-workflows#4952: deliver`) into session `session_012jKBsy2vcNfjmnK3STfpbp` (permission mode `auto`).
- Security pass: run (`security_pass_skip.py`: `no skip label`).
- Phase 1 is protected-path (CLAUDE.md §28.C) and has no `Protected-path approval: phase 1` line yet, so the chain stops before phase 1. The standing operator answer for this case is Q40 twin-first (`docs/operations/master-session.md`); the `session-start.sh` copy additionally needs the Q62/Q64 approval window.
- Protected-path approval: phase 1 — twin-first per Q40 (2026-09-29) (answered by the repo owner on #4952, comment 5883394764; relayed by master session `session_01LF9aeTnk15B7e9mKy7vDNM`).
- Plan deviation: the drift-log tests live in the new `tests/test_session_start_claude_assets_drift.py` (pytest, own `ci.yml` step) instead of extending the plain-script `tests/test_session_start_extract_repo_slug.py`; the command-text tests are all in `tests/test_claude_asset_sync_command.py`.
- Twin-first verification: with the four twins copied over `.claude/` in a scratch worktree, `tests/test_claude_asset_sync_command.py`, `tests/test_session_start_claude_assets_drift.py`, `tests/test_implement_plan_claude_command.py`, `tests/test_implement_issue_claude_command.py`, `tests/test_check_in_status_hand_back.py`, and `tests/test_update_workflows_guardrails.py` pass (196 passed), and `tests/test_session_start_extract_repo_slug.py` prints PASS. On the branch as pushed, only the twin-parity assertions fail, as expected until the sync.
- Review round 1 (PR #5010, head 0fcc87d, ledger 6149d782…): fixed — merge subject (`git merge --no-ff -m`), `HEAD^1` instead of `HEAD@{1}`, shallow fallback labelled `diverged`, default branch from `ls-remote` when `origin/HEAD` is unset, no unbounded network without `timeout`, `behind` meaning documented; task gaps — shallow, settings-only, unset `origin/HEAD`, and executed-command tests added. Rejected — fetch on every session start (AD-6 by design), no kill switch (§5: bounded, fail-open, web-only), and the duplicate low `behind` entries are covered by the documentation fix.
- Plan deviation: step 4 of the sync uses `git merge --no-ff -m "[claude-asset-sync] …"` instead of the plan's `git merge --no-edit`, and step 6 uses `HEAD^1` instead of `HEAD@{1}` (review round 1; the plan's commands could not produce the documented subject or needed a reflog).
- Twin-first (Q40) for review round 1: only `workflow-templates/.claude/commands/implement-plan-claude.md` and `workflow-templates/.claude/hooks/session-start.sh` changed. With both copied over `.claude/` in a scratch copy, the six suites pass (208 passed) and `tests/test_session_start_extract_repo_slug.py` prints PASS; on the branch as pushed only the `.claude/` parametrizations and twin-parity checks fail, as expected until the sync.
- Review round 2 (PR #5010, head 9a2290f, ledger 28b84bb7…; the workflow numbers it round 1 because the `[claude-twin-sync]` commit ended the `[claude-autofix]` run): 1 consensus finding (deepseek NIT, qwen MAJOR) — step 2's merge vs the `[claude-asset-sync]` subject — valid as a documentation inconsistency and fixed per AD-12 in `workflow-templates/.claude/commands/implement-plan-claude.md` only (step 2 and sync step 4), with assertions in `tests/test_claude_asset_sync_command.py`. The round-2 review on head 0d19fa7 (six task gaps claiming the files were absent) described a different checkout and was superseded by the 9a2290f review.
- Stage sync (2026-09-29): merged `main` (9 commits, including #4948's twin-first default) into the project branch cleanly (40dd01e); merging it into the phase branch conflicted only in `agents.md` (#4948's interim paragraph vs this PR's Claude-asset sync bullet), resolved keeping both in `[claude-merge-resolve]` 93c40cc.
- Twin-first (Q40) for review round 2: only `workflow-templates/.claude/commands/implement-plan-claude.md` changed. With it copied over `.claude/` in a scratch copy, the six suites pass (212 passed; one failure was the scratch copy's flattened `workflow-templates/CLAUDE.md` symlink) and `tests/test_session_start_extract_repo_slug.py` prints PASS; on the branch as pushed only the twin-parity checks and the `.claude/commands` parametrization fail (4), as expected until the sync.
