# Implement-Plan Log — Change issue state on an unmerged PR close only for the issue's own automation PR

- Plan: docs/completed/issue-5617-unmerged-close-automation-identity-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5617
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
- Project branch: claude/implement-plan-issue-5617-unmerged-close-automation-identity   Final PR: #5629 draft (into claude/implement-plan-issue-4813-close-sweep-target-branch-merges)
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4813-close-sweep-target-branch-merges)
- Waiting on: the completion PR from claude/implement-plan-issue-5617-unmerged-close-automation-identity-complete
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01UpJzt3ZxdJ6Ytek7PpMhgM   safety net and hand-back: see the completion stage report
- Last updated: 2026-09-30
- Last note: completion stage: validation cycle 1 passed (run 36725868367, 10/10 tests); plan moved to docs/completed/; conformance run 1 CONFORMANT; security skipped (plan header). Next: final-merge 1/1 marks #5629 ready and closes #5617 with `ai:merged` once it merges.

## Phases
1. [x] Phase 1 — automation identity for unmerged closes in `issue_pr_status.yml` (workflow + tests + helper comment + README row + changelog fragment)   — PR #5634 merged 2026-09-30 (267660c, merged by the operator under Q1: B, evidence https://github.com/shubhodeep1/coding-workflows/pull/5634#issuecomment-5911885526); review rounds: 1 (all 7 task-gap entries rejected as already implemented); interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Implemented COMPLETE, Correctness PASS, no findings) — no fixes (pre-security). Evidence: goals traced to .github/workflows/issue_pr_status.yml:268-271 (`pr_is_issue_automation_pr`), :579-580 (tracking/unclassified lineage gate), :596-599 (unmerged-close gate + `continue`); the stub at :255-259 fails closed; merged paths :601-638 unchanged; README `issue_pr_status.yml` row; changelog.d/5617-unmerged-close-automation-identity.md (`security`). Checks: tests/test_issue_pr_status_target_branch_gate.py as a script PASS; pytest over it plus the payload-fallback, gh_helpers automation-branch, label-precreation, checkout-integration-ref audit, changelog-fragment contract, and assemble-changelog suites: 94 passed; workflow YAML parses; `bash -n` on the step OK; 49,143 bytes (§27 fine); validate.yml authorizes this stacked target (final PR #5629 into the #4813 project branch, whose PR #4826 is open into main).

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified)

## Validation
- Cycle 1 — run 36725868367 2026-09-30 (target_ref: claude/implement-plan-issue-5617-unmerged-close-automation-identity): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 293s).

## Completion
- Completion PR from claude/implement-plan-issue-5617-unmerged-close-automation-identity-complete (open) — doc moved to docs/completed/issue-5617-unmerged-close-automation-identity-plan.md
- Final PR #5629 draft

## Activation
- n/a: the base is the #4813 project branch, so this change goes live with project #4813.

## Auto-decisions
- AD-1 [plan, 2026-09-30] What counts as verified automation provenance for an unmerged close? — Picked: A — a same-repository head that `pr_head_ref_is_issue_automation_branch` accepts for that issue (the #5226 rule). Alternatives: B — also require an ai-memory lineage record naming the PR; C — accept any same-repository head. Why: A reuses a tested, API-free identity that only write-access accounts can satisfy; B adds a network read with its own failure modes; C lets any collaborator branch close an issue. Applied in: PR #5634. Status: pending review
- AD-2 [plan, 2026-09-30] Does the gate also cover tracking-bucket lineage on an unmerged close? — Picked: A — yes, orchestrator-tracking and unclassified issues are finalized on an unmerged close only for an automation PR of that issue. Alternatives: B — keep their lineage finalization unchanged. Why: the finding covers lineage state and §1 puts security first; the orchestrator owns the tracker lifecycle. Applied in: PR #5634. Status: pending review
- AD-3 [plan, 2026-09-30] Should `claude/implement-plan-issue-<n>-…` heads count as automation heads? — Picked: A — no. Alternatives: B — extend `pr_head_ref_is_issue_automation_branch` to accept them. Why: §5; the helper is shared with `close_merged_issues_sweep`, and an issue left open after its abandoned PR is the safe failure. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-30] Should the merged-PR paths change? — Picked: A — no. Alternatives: B — also require the automation identity on default-branch merges. Why: GitHub closes the issue on a default-branch merge anyway, and non-default merges already require it (#5226). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] How are the three existing unmerged-close tests kept? — Picked: A — keep their names (§6) and give them an `ai/issue-10` head; add new tests for rejected heads. Alternatives: B — rename them. Why: §6. Applied in: PR #5634. Status: pending review
- AD-6 [plan, 2026-09-30] Does this need a changelog fragment? — Picked: A — yes, under `security`. Alternatives: B — none. Why: §20.A lists security fixes. Applied in: PR #5634. Status: pending review

## Lessons
- [source:intervention] In a repo without `CLAUDE_FIXER_VERDICT_BOT_LOGIN`, a Claude-fixer review round whose only entries are rejected (here, task gaps whose own evidence showed the requirement implemented) cannot converge, because the rejection verdict must come from the dedicated bot; the chain blocks and the operator merges. Configure the verdict bot before relying on unattended convergence. (files: .github/workflows/review_autofix.yml, .claude/commands/implement-plan-claude.md)

## Notes
- Started by the Claude issue dispatcher routine (`implement-issue #5617`, trig_01Vd44aaVLf3gaYwpV63kD9m) in session session_01999dzSLqMucahCkUynZdzz.
- Issue progress comment id: 5909962763.
- The issue base's final PR #4826 (draft, into main) was open at the start, so the base has not moved.
- 2026-09-30: phase 1 review round 1 had no valid finding and no verdict bot is configured; blocked as Q1 on the issue (comment 5911560026). Q1: B (operator, comments 5911894628 and 5912456169): PR #5634 merged into the project branch as 267660c under standing merge approval Q46.
- 2026-09-30: project branch synced with the base at every stage since; already up to date at 267660c at the conformance and validation-read stages (the base's PR #4826 was still open, so the base did not move).
- Protected paths: none (phase 1 touches no `.claude/**` path).
