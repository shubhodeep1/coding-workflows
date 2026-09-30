# Implement-Plan Log — Require the automation account as author before an automation-named head closes an issue

- Plan: docs/plans/issue-5620-bind-automation-pr-author-plan.md
- Source issue: shubhodeep1/coding-workflows#5620
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5620-bind-automation-pr-author   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened from claude/implement-plan-issue-4813-close-sweep-target-branch-merges; implementing phase 1.

## Phases
1. [ ] Phase 1 — require the automation account as PR author before an automation-named head counts (gh_helpers helper + sweep + issue_pr_status.yml + tests + README + changelog)

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] What proves that an automation-named PR was made by the automation? — Picked: A — the PR's author is the account `GH_PAT` authenticates as, checked in addition to the head name and head repository. Alternatives: B — bind each head SHA to trusted automation runs through markers; C — rely on a repository ruleset only. Why: A closes the reported writer path with one cached call and no change to the PR-producing workflows. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Where does the automation login come from? — Picked: A — `gh api user` with the caller's `GH_PAT`, lazy and cached on success. Alternatives: B — a new repository variable; C — a hardcoded login. Why: no new configuration, cannot drift from the credential that opens the PRs. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What happens when the login cannot be resolved? — Picked: A — fail closed (sweep skips the issue for the cycle without an alert; workflow leaves the issue unchanged with a warning). Alternatives: B — fail open. Why: §1 security first. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Does the author check also cover `issue_pr_status.yml`'s `ai/issue-<n>` head-name link? — Picked: A — yes, when the issue is not already linked otherwise; same-repository head and automation author required. Alternatives: B — limit the change to the non-default merge gate. Why: same name-as-identity trust; genuine automation PRs carry `Closes <issue URL>`, so no cost to them. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Does this project protect automation branch refs? — Picked: A — no; a ruleset for `ai/issue-*` and `fix/*-followup-*` is an operator recommendation. Alternatives: B — change rulesets from the chain. Why: §23.C ask-first, never unattended (§28.C). Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] Do default-branch merges need the author check? — Picked: A — no; keep the closing-keyword identity there. Alternatives: B — require the automation author on every base. Why: GitHub closes the issue on that merge anyway; #5226 kept this rule. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5620#issuecomment-5910000438
- Security pass: skip (`security_pass_skip.py`: `ai:security: created and labelled by the issue automation`).
- Base-branch check 2026-09-30: PR #4826 (head = base branch, into main) open, draft, unmerged.
