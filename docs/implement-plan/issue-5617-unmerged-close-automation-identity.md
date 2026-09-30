# Implement-Plan Log — Change issue state on an unmerged PR close only for the issue's own automation PR

- Plan: docs/plans/issue-5617-unmerged-close-automation-identity-plan.md
- Source issue: shubhodeep1/coding-workflows#5617
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
- Project branch: claude/implement-plan-issue-5617-unmerged-close-automation-identity   Final PR: #5629 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started (n/a while the issue base is claude/implement-plan-issue-4813-close-sweep-target-branch-merges; settled at final-merge)
- Waiting on: phase 1 PR (branch claude/implement-plan-issue-5617-unmerged-close-automation-identity-phase-1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 implemented and verified (gate test file 31/31 as a script; 98 related tests under pytest; the 4 new gating tests fail on the old workflow; yamllint -s and actionlint 1.7.12 clean); phase PR opened against the project branch.

## Phases
1. [ ] Phase 1 — automation identity for unmerged closes in `issue_pr_status.yml` (workflow + tests + helper comment + README row + changelog fragment)   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] What counts as verified automation provenance for an unmerged close? — Picked: A — a same-repository head that `pr_head_ref_is_issue_automation_branch` accepts for that issue (the #5226 rule). Alternatives: B — also require an ai-memory lineage record naming the PR; C — accept any same-repository head. Why: A reuses a tested, API-free identity that only write-access accounts can satisfy; B adds a network read with its own failure modes; C lets any collaborator branch close an issue. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Does the gate also cover tracking-bucket lineage on an unmerged close? — Picked: A — yes, orchestrator-tracking and unclassified issues are finalized on an unmerged close only for an automation PR of that issue. Alternatives: B — keep their lineage finalization unchanged. Why: the finding covers lineage state and §1 puts security first; the orchestrator owns the tracker lifecycle. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Should `claude/implement-plan-issue-<n>-…` heads count as automation heads? — Picked: A — no. Alternatives: B — extend `pr_head_ref_is_issue_automation_branch` to accept them. Why: §5; the helper is shared with `close_merged_issues_sweep`, and an issue left open after its abandoned PR is the safe failure. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-30] Should the merged-PR paths change? — Picked: A — no. Alternatives: B — also require the automation identity on default-branch merges. Why: GitHub closes the issue on a default-branch merge anyway, and non-default merges already require it (#5226). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] How are the three existing unmerged-close tests kept? — Picked: A — keep their names (§6) and give them an `ai/issue-10` head; add new tests for rejected heads. Alternatives: B — rename them. Why: §6. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Does this need a changelog fragment? — Picked: A — yes, under `security`. Alternatives: B — none. Why: §20.A lists security fixes. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Started by the Claude issue dispatcher routine (`implement-issue #5617`, trig_01Vd44aaVLf3gaYwpV63kD9m) in session session_01999dzSLqMucahCkUynZdzz.
- Issue progress comment id: 5909962763.
- The issue base's final PR #4826 (draft, into main) was open at the start, so the base has not moved.
