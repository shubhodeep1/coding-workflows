# Implement-Plan Log — smoke_review_checked_out_sha: an empty job-log body is retryable, not "no line"

- Plan: docs/plans/issue-5689-smoke-empty-job-log-retryable-plan.md
- Source issue: shubhodeep1/coding-workflows#5689
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5689-smoke-empty-job-log-retryable   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened from the issue base; implementing phase 1.

## Phases
1. [ ] Phase 1 — empty job-log body is retryable (`scripts/smoke_review_dispatch.sh`, `tests/test_smoke_review_dispatch.py`, `changelog.d/5689-smoke-empty-job-log-retryable.md`)
   - [ ] helper returns 1 for an empty and a whitespace-only log body, 2 for a non-empty body with no line, 0 with the SHA for a genuine line
   - [ ] helper doc comment names the new rc=1 case
   - [ ] tests cover the four cases; `tests/test_smoke_review_dispatch.py` passes in full
   - [ ] `changelog.d/` fragment (fixed)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How should an empty job-log body be handled? — Picked: A — return 1 so the caller retries on its own schedule. Alternatives: B — retry the log read a bounded number of times inside the helper; C — both. Why: both callers already retry rc=1 within a deadline, and A keeps the helper's one-log-read budget (§15, §5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What counts as whitespace-only? — Picked: A — no byte outside `[:space:]` (so `\r`, `\n`, spaces, and tabs only). Alternatives: B — only a zero-byte body; C — also treat an ANSI-escape-only body as empty. Why: the issue names whitespace-only bodies; an escape-only body is not whitespace and is not a known GitHub response, so it stays rc=2 (§5). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Update `agents.md` or the workflow comments? — Picked: A — no; update only the helper's doc comment and add a changelog fragment. Alternatives: B — also add the rc semantics to the `agents.md` smoke-job entry. Why: `agents.md` and the workflow comments do not document the helper's return codes, and Phase 4's comment already says an unreadable log is retried (§5). Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A); started by the Claude issue dispatcher routine in session session_012MbKqZLi3pyF4eS7sTqUbR.
- Security pass: run (`security_pass_skip.py`: no skip label).
