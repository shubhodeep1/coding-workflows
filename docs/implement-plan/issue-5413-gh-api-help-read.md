# Implement-Plan Log — Treat `gh api --help` as a read in the `gh api` permission guard

- Plan: docs/plans/issue-5413-gh-api-help-read-plan.md
- Source issue: shubhodeep1/coding-workflows#5413
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5413-gh-api-help-read   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened; phase 1 starting

## Phases
1. [ ] Phase 1 — help-only `gh api` calls are reads — protected paths: .claude/hooks/gh_api_write_guard.py
   - [ ] twin guard: `-h` / `--help` parsed; help-only call is a read, help with a request is unreadable
   - [ ] tests in tests/test_gh_api_write_guard.py against the twin
   - [ ] CLAUDE.md §23.H `read` row (both copies)
   - [ ] changelog.d/5413-gh-api-help-read.md

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Where should the fix for the `gh api --help` prompt live? — Picked: A — extend the `gh api` guard so a help-only call is a read. Alternatives: B — add a `permissions.allow` rule for `gh api --help*`; C — close as not planned. Why: the guard's ask is the cause, and the issue's fix order names extending the guard for reads. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Should `--help` make a call a read when it also carries an endpoint, method, field, header, or `--input`? — Picked: A — no; only a help-only call is a read. Alternatives: B — yes, any call with `--help` is a read. Why: §1 security first. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Should `grep` become a safe filter so the issue's full command is allowed by the guard? — Picked: A — no. Alternatives: B — add `grep` to the safe filters. Why: §5 minimal change; `grep -r` reads files. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
- security_pass_skip.py: skip=false (no skip label); Security pass: run.
