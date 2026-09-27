# Implement-Plan Log — Name the summariser's empty-stdout failure in reviewer heal evidence

- Plan: docs/plans/issue-4653-summariser-empty-stdout-evidence-plan.md
- Source issue: shubhodeep1/coding-workflows#4653
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: ai/issue-4605
- Project branch: claude/implement-plan-issue-4653-summariser-empty-stdout-evidence   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-27
- Last note: project branch opened from ai/issue-4605; implementing phase 1.

## Phases
1. [ ] Phase 1 — summariser empty-stdout evidence and log upload

## Conformance

## Security pass
- Skipped (ai:workflow-heal: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Which branch should the fix build on? — Picked: A — `ai/issue-4605`, the issue's `Target branch:` (the failing PR #4607's head). Alternatives: B — the default branch. Why: `/implement-issue-claude` step 3 builds on the branch the issue names; the affected files are identical on both branches. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-27] How far should the fix go? — Picked: A — recognise the empty-stdout line in the evidence and upload the summariser logs with the failure-log artifact. Alternatives: B — evidence parser only; C — also change the summariser's model or retry behaviour. Why: the issue asks to inspect the uploaded summariser log, which the artifact never carried, and forbids a retry fix; C guesses at an unidentified cause (§8). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] What should the evidence say for an empty-stdout attempt? — Picked: A — `summariser_exit rc=0` plus one `summariser_empty_stdout prefix=<prefix>` line, no attempt counts. Alternatives: B — copy the raw diagnostic line with its attempt number; C — only `summariser_exit rc=0`. Why: A keeps the fingerprint stable and names the failure mode; B changes the fingerprint per attempt count; C loses the distinction from a clean exit. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode: start-up checks auto-decided (CLAUDE.md §28.A). Session mode: auto.
