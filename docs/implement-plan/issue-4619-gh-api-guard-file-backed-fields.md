# Implement-Plan Log — gh api guard: prompt for file-backed field values

- Plan: docs/plans/issue-4619-gh-api-guard-file-backed-fields-plan.md
- Source issue: shubhodeep1/coding-workflows#4619
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4619-gh-api-guard-file-backed-fields   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-27
- Last note: project branch opened; implementing phase 1.

## Phases
1. [ ] Phase 1 — file-backed `-F` values and `--input` always prompt in the gh api guard

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Which calls does a file-backed `-F` value make a write? — Picked: A — every call, any method, endpoint, or repository, GraphQL variables included. Alternatives: B — only routine write endpoints. Why: a GET query field or GraphQL variable sends the file off-box too, and the `Bash(gh api repos/*)` allow rule would still approve those. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] Should file-backed values be allowed from a private generated-content directory (the recommendation's optional half)? — Picked: A — no allowance: every file-backed value prompts, and bodies go through the GitHub MCP tools or an inline `-f body=...`. Alternatives: B — allow `@<path>` that resolves under the session scratchpad. Why: the hook cannot verify a path is private or its contents are safe, and no interactive flow depends on it. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] Should `--input` on GET/HEAD, classified read today, also prompt? — Picked: A — yes, `--input` is a write on every method. Alternatives: B — leave it, as outside the finding. Why: same file-read exfiltration path in the same function (CLAUDE.md §12.B). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] Should `-f`/`--raw-field` values starting with `@` prompt too? — Picked: A — no, they stay as today. Alternatives: B — prompt for them as well. Why: `gh` sends raw fields literally and reads no file. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] What happens to the existing test fixtures that recorded file-backed calls as allowed or undecided? — Picked: A — keep the observed commands and move them to a new ask expectation. Alternatives: B — delete them. Why: real stage-session commands make the best regression cases. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher; permission mode auto.
