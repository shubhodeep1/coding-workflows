# Implement-Plan Log — Require blocked evidence and Claude-session provenance before `replaced-sessions` archives a named session

- Plan: docs/plans/issue-5063-require-blocked-named-session-plan.md
- Source issue: shubhodeep1/coding-workflows#5063
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4817-archive-replaced-blocked-session
- Project branch: claude/implement-plan-issue-5063-require-blocked-named-session   Final PR: pending (draft)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened from the issue base; phase 1 starting.

## Phases
1. [ ] Phase 1 — named-session blocked check and Claude-app provenance in `replaced-sessions`

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; plan header `Security pass: skip`)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Must the session named by a blocked comment show blocked evidence before it is archived? — Picked: A — yes, the same evidence as the title route (bucket `SESSION_STATUS_BUCKET_BLOCKED` or `BLOCKED` in the title); a name only sets priority. Alternatives: B — skip the check only for a Claude-app comment; C — keep as is and rely on provenance only. Why: this is the issue's recommendation and §1 puts security first; keeping a session costs nothing, while B and C still let a collaborator's own Claude session forge a name. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What provenance must a blocked comment have to name a session? — Picked: A — a trusted author (`is_trusted_issue_author`) and `performed_via_github_app.slug == "claude"`. Alternatives: B — `author_association` OWNER only; C — keep the trusted-author rule alone. Why: every observed session comment carries the `claude` app; B would break consumer orgs whose sessions post as MEMBER; C leaves the forged path open. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Should the pickup command (`.claude/commands/claude-issue-pickup.md`) be updated to describe the new rules? — Picked: A — no; describe them in README.md, agents.md, and the script docstrings. Alternatives: B — edit the pickup command and its `workflow-templates/` twin. Why: the pickup already defers to the script, and a `.claude/**` edit would stop an unattended phase (§28.C). Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-29] Should CLAUDE.md §28.C's summary line change? — Picked: A — no. Alternatives: B — add "if it is still blocked". Why: §5 minimal change; the summary stays true, and the detailed rules live in README.md and agents.md. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] Should `issue_session_title` also match dispatcher titles like `Issue #<N> — implement` (no repo)? — Picked: A — no, out of scope; record it under Notes. Alternatives: B — widen the patterns in this PR. Why: §5; the gap only means fewer sessions are archived, and the security fix does not depend on it. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A), started by the Claude issue dispatcher routine in session session_016c1EzZWwVAwndmHzJ6SRof. Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5063#issuecomment-5885057190
- The base branch is not the default branch: the final-merge stage closes #5063 with `ai:merged`, and steps 12–13 are skipped (`Activation: n/a`).
- Observed gap (AD-5): sessions titled `Issue #<N> — implement` are not matched by `issue_session_title`.
