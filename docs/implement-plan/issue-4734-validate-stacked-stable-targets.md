# Implement-Plan Log — validate.yml authorizes explicit targets whose final PR goes into a project branch or stable

- Plan: docs/completed/issue-4734-validate-stacked-stable-targets-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4734
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4734-validate-stacked-stable-targets   Final PR: #4746 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation
- Waiting on: the completion PR from claude/implement-plan-issue-4734-validate-stacked-stable-targets-complete
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_012e6ydasL9pmy6MozTwudaD   safety net and hand-back: see the completion stage report (session_013T65hijxW77S4k7Aj1gzy9)
- Last updated: 2026-09-30
- Last note: completion stage: validation cycle 1 passed (run 36544520177, 10/10 tests, `target_ref` = the project branch at 5c3af70); `main` merged into the project branch at stage start (759c9b4, clean, no overlap with this project's lines; targeted tests 73 passed); plan moved to docs/completed/ with a superseded note for the #4791 `stable` rule (AD-6). Next: final-merge 1/1 marks #4746 ready; its merge into `main` closes #4734 (`Fixes #4734`), then verify-activation 1/3.

## Phases
1. [x] Phase 1 — authorize stacked project-branch and stable-heal targets in validate.yml   — PR #4758 merged 2026-09-28 (f1dbe12); review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-28: CONFORMANT (Implemented COMPLETE, Correctness PASS, no findings) — no fixes (pre-security)

## Security pass
- Cycle 1 — run 36409316423 2026-09-28 (ref: project branch): 1 finding — follow-up #4791 (medium: the `stable` case authorized from a mutable `ai:workflow-heal` label and the branch name alone). Fixed by its own issue-mode project on this project branch: final PR #4793 merged 2026-09-29 (88f6695); #4791 closed with `ai:merged`.
- Cycle 2 — run 36537138882 2026-09-29 (ref: project branch, after #4793 and a `main` sync): clean (`findings=0 followups_created=0`)

## Validation
- Cycle 1 — run 36544520177 2026-09-29 (target_ref: project branch, head 5c3af70, includes #4793): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 293s). No validation-fix PR, so no conformance re-run.

## Completion
- Completion PR from claude/implement-plan-issue-4734-validate-stacked-stable-targets-complete (open) — doc moved to docs/completed/issue-4734-validate-stacked-stable-targets-plan.md
- Final PR #4746 draft

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Where does the new authorization logic live? — Picked: A — inline in the `validate.yml` step. Alternatives: B — move to `scripts/validate_authorize_target.sh`. Why: the step runs before checkout in the caller's repository, so a script file in this repo is not on disk there, and the workflow is 65,523 bytes, far under the §27 guard. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How is the project-branch base's own PR bound? — Picked: A — list open PRs for the base head without a base filter and require exactly one, into the default branch. Alternatives: B — filter the listing by `base=<default>` and require exactly one. Why: stricter; a base branch with a second open PR anywhere is ambiguous, and one level of stacking is enforced by the same check. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] What must issue `<n>` satisfy for a `stable` base? — Picked: A — an issue (no `pull_request` key) carrying `ai:workflow-heal`, any state. Alternatives: B — also require it to be open; C — label only. Why: the issue says "issue `<n>` carries `ai:workflow-heal`"; excluding pull requests keeps the number bound to an issue, and requiring open state would reject a re-run after an explicit close. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Update the "Validation target trust boundary" line in `.claude/commands/implement-plan-claude.md`? — Picked: A — leave `.claude/**` unchanged and document the new cases in `README.md` and `agents.md`. Alternatives: B — edit both `.claude/commands/` and `workflow-templates/.claude/commands/` copies. Why: a `.claude/**` edit stops an unattended phase (CLAUDE.md §28.C), and the existing line stays true for default-base projects. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-28] Post `/reclarify` on #4665 after merge? — Picked: A — leave it to the owner and list it as the next step in the progress comment and final report. Alternatives: B — the chain posts it. Why: `/reclarify` starts an unattended pipeline, which CLAUDE.md §23.C keeps ask-first. Applied in: no code change. Status: pending review
- AD-6 [completion, 2026-09-29] The security follow-up #4791 replaced goal 3's label-only `stable` rule and the "ACCEPTED" label risk, so this plan no longer matches the shipped code. How to archive it? — Picked: A — add a short superseded note under the goals and on the risk, pointing at the #4791 plan. Alternatives: B — rewrite goals 3 and 5, the approach, and the tests to the final rule; C — archive it unchanged. Why: the final PR's reviewer panel reads the plan as the spec; a note fixes the misleading text without rewriting the history of what this project planned (§5). Applied in: completion PR. Status: pending review

## Lessons
- [source:security] A workflow gate that releases a secret-bearing run must not authorize from a label or a branch name alone: both are writable by any collaborator. Bind it to provenance the automation leaves (who applied the label and when, a marker line) and to the requesting PR's author. (files: .github/workflows/validate.yml)

## Notes
- Issue mode; base branch `main`; security pass: run (`security_pass_skip.py`: no skip label).
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4734#issuecomment-5865547320
- 2026-09-29: the project checker's title carries a `#4734 · PR #4746 — ` prefix in front of `implement-plan issue-4734-validate-stacked-stable-targets — checker` (renamed outside the chain). The stages kept reusing it as the recorded checker; the checker archive check (exact title) will skip archiving it at the end of the project, so archive it by hand then.
