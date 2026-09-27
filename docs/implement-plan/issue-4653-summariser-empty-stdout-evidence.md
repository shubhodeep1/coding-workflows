# Implement-Plan Log — Name the summariser's empty-stdout failure in reviewer heal evidence

- Plan: docs/plans/issue-4653-summariser-empty-stdout-evidence-plan.md
- Source issue: shubhodeep1/coding-workflows#4653
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: ai/issue-4605
- Project branch: claude/implement-plan-issue-4653-summariser-empty-stdout-evidence   Final PR: #4657 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (opened with this commit)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-27
- Last note: phase 1 implemented and verified; phase PR opened against the project branch.

## Phases
1. [ ] Phase 1 — summariser empty-stdout evidence and log upload   — PR open (waiting); review rounds: 0; interventions: 0

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
- 2026-09-27: root cause of the inconclusive heal located in run 36317817104's job log: 10/10 `summariser (pass1): attempt N produced empty stdout` lines, unrecognised by reviewer_failure_evidence; summariser_pass1.log was never in the failure-log artifact.
- 2026-09-27: two tests in tests/test_workflow_failure_heal.py fail on the base branch ai/issue-4605 without this change and pass on main (test_fingerprint_cap_block_labels_comments_and_reports: PR 4259 vs 4255; test_reviewers_failed_names_the_failure_before_the_editor_flags: review_autofix_step_iteration_summary.sh now requires RUNNER_TEMP). They come from PR #4607's own changes and are out of scope here.
- 2026-09-27: review_autofix.yml is 468,516 bytes on ai/issue-4605 (§27 guard 480,000); this change adds about 200 bytes.
