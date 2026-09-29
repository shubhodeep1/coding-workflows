# Implement-Plan Log — Require an automation-owned PR before closing an issue on a non-default branch merge

- Plan: docs/plans/issue-5226-close-sweep-automation-identity-plan.md
- Source issue: shubhodeep1/coding-workflows#5226
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5226-close-sweep-automation-identity   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened from claude/implement-plan-issue-4813-close-sweep-target-branch-merges; phase 1 starting.

## Phases
1. [ ] Phase 1 — require an automation-owned PR for non-default target merges (sweep + issue_pr_status.yml + gh_helpers.sh helper + tests + README + changelog)

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which paths get the identity rule? — Picked: A — both the sweep and `issue_pr_status.yml`, through one helper in `scripts/gh_helpers.sh`. Alternatives: B — the sweep only; C — change `_pr_json_is_issue_implementation_pr` for every caller. Why: B leaves the workflow closing labelled managed children on the same PR text; C changes callers that close nothing. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What proves a PR is the issue's assigned fix on a non-default base? — Picked: A — an automation head branch for that issue in this repository. Alternatives: B — trust the closing keyword from collaborator-authored issues; C — a new issue-to-PR marker comment. Why: A needs no API call and no new state; a same-repo branch requires write access. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which head names count as automation branches? — Picked: A — `ai/issue-<n>`, `ai/issue-<n>-…` / `ai/issue-<n>/…`, `fix/<n>-followup-<digits>`. Alternatives: B — exact `ai/issue-<n>` only. Why: the workflow already maps the suffixed shape to the issue, and the orchestrator judge's follow-up PR closes its issue on the integration branch. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Does a default-branch merge also need the identity? — Picked: A — no. Alternatives: B — require it everywhere. Why: GitHub closes the issue on that merge by itself. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Should the editable `Integration branch:` line itself be authorized? — Picked: A — no separate check. Alternatives: B — trust the line only on automation-authored issues. Why: with AD-2 a changed line can only point at a branch where the issue's own automation PR merged. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5226#issuecomment-5899545241
- Security pass: skip (`security_pass_skip.py`: ai:security, created and labelled by the issue automation).
- Base branch is another project's branch (#4813, final PR #4826, open draft); per Issue Mode this project ends after its final merge (`Activation: n/a`) and the final-merge stage closes #5226 with `ai:merged`.
