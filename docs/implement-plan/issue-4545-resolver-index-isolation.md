# Implement-Plan Log — Isolate the conflict resolver's model attempt from the real Git index

- Plan: docs/plans/issue-4545-resolver-index-isolation-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4545-resolver-index-isolation   Final PR: #4546 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3
- Activation: not started (base is main; steps 12–13 run after the final merge)
- Waiting on: conformance fix PR from branch `claude/implement-plan-issue-4545-resolver-index-isolation-conformance-fix-1` (next stage conformance 2/3 on merge)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: project checker `implement-plan issue-4545-resolver-index-isolation — checker`, armed by the conformance 1/3 stage session (session_01GqaeCivxQZjjgQYJ97pv8o) after this PR opened; ids are in that session's report and the next `— resume.` block
- Last updated: 2026-09-27
- Last note: Conformance run 1 (2026-09-27): CONFORMANT (Implemented COMPLETE, Correctness CONCERNS); three EVIDENCE-BASED CONCERN findings fixed in the conformance fix PR; AD-2 recorded.

## Phases
1. [x] Phase 1 — Git index isolation for the resolver's model attempt   — PR #4547 merged 2026-09-26

## Conformance
- Run 1 — 2026-09-27: CONFORMANT (Implemented: COMPLETE; Correctness: CONCERNS, 3 EVIDENCE-BASED CONCERN findings, no BLOCKER) — fix PR from `claude/implement-plan-issue-4545-resolver-index-isolation-conformance-fix-1` (pre-security; security pass is skipped per the plan header)

## Auto-decisions
- AD-1 [plan, 2026-09-26] The issue's suggested fix names a specific mechanism (`GIT_INDEX_FILE`, keep the real-index check, forbid staging in the prompt). Should the plan adopt it as-is, or design an alternative isolation mechanism?
  - A — Adopt the issue's suggested `GIT_INDEX_FILE` isolation mechanism as specified (RECOMMENDED)
  - B — Design an alternative (e.g. a full disposable worktree clone for the model attempt)
  - Picked: A. Why: smaller blast radius, no change to `RESOLVER_OPENCODE_WORKSPACE`'s existing design, directly addresses the confirmed root cause. Applied in: this phase's PR. Status: pending review

- AD-2 [conformance 1/3, 2026-09-27] `GIT_INDEX_FILE` isolates only the index: a model-issued `git commit` during the resolver's `git merge --no-commit` still moves `HEAD` and removes `MERGE_HEAD` (the unchanged `merge_state()` check then fails the attempt closed), while the plan's Approach, the script comment, `agents.md`, and the changelog fragment said a model `git commit` is contained by the scratch copy. How should the conformance fix handle it? — Picked: A — correct the comments/docs to say only the index is isolated and a model commit still fails closed, and pin that fail-closed behaviour with a regression test. Alternatives: B — also block model commits (e.g. a rejecting `core.hooksPath` pre-commit hook via `GIT_CONFIG_*` env; bypassable with `--no-verify`, new mechanism); C — snapshot and restore `HEAD`/`MERGE_HEAD` around the attempt (changes the fail-closed contract). Why: §5 smallest change; the outcome is already safe (fail closed, as before #4545), the prompt forbids committing, and only the documentation was wrong. Applied in: conformance fix PR (branch `claude/implement-plan-issue-4545-resolver-index-isolation-conformance-fix-1`). Status: pending review

## Notes
- Permission mode: this session's mode was not explicitly queryable via `get_session` (no claude-code-remote MCP tools present); proceeding under CLAUDE.md §28.A's issue-mode rule that start-up-check questions are recorded, not asked, and the session's actual harness permission mode governs what actually executes (no ask-first operation beyond routine writes was attempted).
- Security pass: skipped per the plan header (`ai:workflow-heal` label on the source issue).
- No claude-code-remote MCP tools (`create_session`, `send_later`, `get_session`, `create_trigger`, `archive_session`, `list_triggers`, `delete_trigger`) were available in this session (confirmed via ToolSearch). Per implement-plan-claude's Check-in Loop → Fallbacks: no scheduler substitute persists past this ephemeral session, so this stage reports its state in this log/PR and ends rather than polling or fabricating a wait. A future session (this repo's normal claude-code-remote-equipped sessions, or a human) can resume by re-running `/implement-plan-claude docs/plans/issue-4545-resolver-index-isolation-plan.md` once the phase PR (opened this session) has a review-round outcome.
- 2026-09-26 (session_01CefhktnVbiiRuVHUvLkZmN, user decision Q26: A): the issue base `ai/issue-4512` merged into `main` as #4516, so the project base is now `main`. main was merged into the project branch (`[claude-merge-resolve]`), the plan header was updated, and the final PR #4546 was retargeted to `main`. Issue Mode's default-base rules now apply: the final PR body carries `Fixes #4545`, and verify-activation runs after the final merge. PR #4539 (a second fix for the same resolver bug, from heal issue #4538) was closed as superseded by this project.
- 2026-09-27 (restart, user decision Q29: A, operator note from session_01CefhktnVbiiRuVHUvLkZmN): the chain stopped after phase 1 because the session that ran it was a claude.ai routine run with no claude-code-remote tools. It was restarted by hand at stage conformance 1/3 in session_01GqaeCivxQZjjgQYJ97pv8o, which has the tools and creates the project checker at its first wait. The project branch was synced with `main` (clean merge, `[claude-merge-resolve]` 5853a09).
- 2026-09-27 (user decision Q28: A): PR #4555 (issue #4552, project `claude/implement-plan-issue-4552-resolver-scope-staged-entries`) fixes the same resolver path by comparing staged entries instead of raw index bytes, and should merge after #4546. This project does not modify #4555, its branch, or issue #4552. If `main` already carries #4555 at a later sync, merge `main` in and keep both fixes. At the 2026-09-27 sync `main` did not carry it yet.

## Lessons
- [source:conformance] When isolating a model's Git state with `GIT_INDEX_FILE`, state in comments and docs that only the index is isolated: `git commit` still moves `HEAD` and removes `MERGE_HEAD`, and cleanup must be keyed to what the block itself exported, not to whether the variable happens to be set. (files: scripts/review_conflict_resolve.sh, agents.md)
