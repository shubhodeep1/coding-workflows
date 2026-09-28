# Implement-Plan Log — validate.yml authorizes explicit targets whose final PR goes into a project branch or stable

- Plan: docs/plans/issue-4734-validate-stacked-stable-targets-plan.md
- Source issue: shubhodeep1/coding-workflows#4734
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4734-validate-stacked-stable-targets   Final PR: #TBD draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: project branch opened; implementing phase 1.

## Phases
1. [ ] Phase 1 — authorize stacked project-branch and stable-heal targets in validate.yml

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Where does the new authorization logic live? — Picked: A — inline in the `validate.yml` step. Alternatives: B — move to `scripts/validate_authorize_target.sh`. Why: the step runs before checkout in the caller's repository, so a script file in this repo is not on disk there, and the workflow is 65,523 bytes, far under the §27 guard. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How is the project-branch base's own PR bound? — Picked: A — list open PRs for the base head without a base filter and require exactly one, into the default branch. Alternatives: B — filter the listing by `base=<default>` and require exactly one. Why: stricter; a base branch with a second open PR anywhere is ambiguous, and one level of stacking is enforced by the same check. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] What must issue `<n>` satisfy for a `stable` base? — Picked: A — an issue (no `pull_request` key) carrying `ai:workflow-heal`, any state. Alternatives: B — also require it to be open; C — label only. Why: the issue says "issue `<n>` carries `ai:workflow-heal`"; excluding pull requests keeps the number bound to an issue, and requiring open state would reject a re-run after an explicit close. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Update the "Validation target trust boundary" line in `.claude/commands/implement-plan-claude.md`? — Picked: A — leave `.claude/**` unchanged and document the new cases in `README.md` and `agents.md`. Alternatives: B — edit both `.claude/commands/` and `workflow-templates/.claude/commands/` copies. Why: a `.claude/**` edit stops an unattended phase (CLAUDE.md §28.C), and the existing line stays true for default-base projects. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] Post `/reclarify` on #4665 after merge? — Picked: A — leave it to the owner and list it as the next step in the progress comment and final report. Alternatives: B — the chain posts it. Why: `/reclarify` starts an unattended pipeline, which CLAUDE.md §23.C keeps ask-first. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Issue mode; base branch `main`; security pass: run (`security_pass_skip.py`: no skip label).
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4734#issuecomment-5865547320
