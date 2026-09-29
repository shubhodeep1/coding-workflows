# Implement-Plan Log — Verify workflow identity and default-branch provenance in the PR-named review-run matchers outside the poller

- Plan: docs/plans/issue-5152-review-run-provenance-outside-poller-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Source issue: shubhodeep1/coding-workflows#5152   Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5152-review-run-provenance-outside-poller   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened from the #4898 project branch; phase 1 starting.

## Phases
1. [ ] Phase 1 — apply the identity and provenance check to the four PR-named matchers outside the poller   — protected paths: .claude/scripts/check_in_status.py (twin-first); not started

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which branch does this project build on? The issue names no `Integration branch:` or `Target branch:` line, and the rule's default (`main`) lacks `_autofix_pr_named_review_runs`, which exists only on the unmerged #4898 → #4701 stack. — Picked: A — `claude/implement-plan-issue-4898-retrigger-dispatch-default-branch`. Alternatives: B — #5094's project branch; C — `main`. Why: the shallowest branch carrying all four matchers, independent of #5094's blockers. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How is a PR-named run with a null or empty `head_branch` treated? — Picked: A — accept it (default branch or none); reject any other branch. Alternatives: B — strict `head_branch ==` default; C — carve-out in the sweep only. Why: a forged run always reports the branch it ran from; B re-opens #4928 in all four guards. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Where does each matcher get the default branch, and what happens when it is unknown? — Picked: A — event payload or PR object, then at most one `GET repos/<repo>` per process; unresolved disables PR-named matching with a `REVIEW_RUN_PROVENANCE` warning. Alternatives: B — fall back to `main`; C — skip the check. Why: a guess must never vouch for a run; no per-item call (§15). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Should `check_in_status.py` and the sweep also match the consumer name `AI Review [pr:<N>]`? — Picked: A — no. Alternatives: B — add `ai-review.yml` to both. Why: §5; harden, do not widen. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): the permission mode is `auto`; start-up checks were auto-decided.
- This session has no `mcp__github__*` tools, so GitHub writes go through `gh api` REST routine writes (§23.B/§23.H). `gh` was installed by running `.claude/hooks/session-start.sh`, because the repo was attached after the session started.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5152#issuecomment-5891548135
