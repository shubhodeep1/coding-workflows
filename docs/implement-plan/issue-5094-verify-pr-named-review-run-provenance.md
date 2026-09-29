# Implement-Plan Log — Verify workflow identity and default-branch provenance before the poller trusts a PR-named review run

- Plan: docs/plans/issue-5094-verify-pr-named-review-run-provenance-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Source issue: shubhodeep1/coding-workflows#5094   Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5094-verify-pr-named-review-run-provenance   Final PR: #5106 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (head claude/implement-plan-issue-5094-verify-pr-named-review-run-provenance-phase-1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented and verified locally; phase PR opened against the project branch.

## Phases
1. [ ] Phase 1 — verify identity and provenance for every PR-named match in the poller   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped: ai:security: automation-produced issue (plan header; `security_pass_skip.py` verified).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which PR-named matchers does this issue fix? — Picked: A — every PR-named match in `scripts/orchestrate_poll_process.sh` (the helper and the three cached-blob copies). Alternatives: B — only `_pr_named_review_dispatch_runs`; C — also `gh_helpers.sh`, `review_merge_train.sh`, `review_autofix_sweep.yml`, `.claude/scripts/check_in_status.py`. Why: the finding is the poller's trust decision; §1 security first; C reaches other flows and a protected path. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How does the helper get the workflow identity? — Picked: A — one REST `actions/runs?event=workflow_dispatch&branch=<default>&per_page=100` call exposing `path` and `head_branch`. Alternatives: B — `gh run list` + `workflowName`; C — `gh run list` + an extra `actions/workflows` call. Why: `path` is the identity at no extra call (§15). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] What happens when the default branch cannot be resolved? — Picked: A — PR-named matching yields nothing (pre-#4701 behaviour), with a warning log line. Alternatives: B — assume `main`; C — skip the provenance check. Why: B and C re-open the spoofing path. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] A shell variable that the script fills with a guessed fallback (`DEFAULT_BRANCH=... || echo "main"`) must not feed a security check; resolve the value separately and fail closed when it is unknown. (files: scripts/orchestrate_poll_process.sh)

## Notes
- Issue mode (CLAUDE.md §28.A): the permission mode is `auto`; start-up checks were auto-decided.
- This session had no `mcp__github__*` tools, so GitHub writes go through `gh api` (REST) routine writes (§23.B/§23.H).
- Stale Routine sweep: one ended Routine (`trig_01LNLrMkYu8BdNKWa5PZNEYS`, `implement-plan issue-4835: check-in`) could not be deleted (Auto-mode classifier denial); it is left for the owner.
- Plan deviation (phase 1): the plan first said `_pr_named_review_default_branch` would reuse `DEFAULT_BRANCH` when set. The poller sets that variable with a `main` fallback, so the helper resolves the branch itself; the plan text was updated in the phase PR.
