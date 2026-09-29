# Implement-Plan Log — Finalize issue lineage only for merges the target-branch gate accepted

- Plan: docs/plans/issue-5227-lineage-only-target-branch-merges-plan.md
- Source issue: shubhodeep1/coding-workflows#5227
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
- Project branch: claude/implement-plan-issue-5227-lineage-only-target-branch-merges   Final PR: #5257 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (branch claude/implement-plan-issue-5227-lineage-only-target-branch-merges-phase-1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented and verified (18/18 gate tests, 103 related tests, yamllint -s, ruff, actionlint 1.7.12); phase PR opened against the project branch.

## Phases
1. [ ] Phase 1 — lineage allow-list in `issue_pr_status.yml` (workflow + tests + README row + changelog fragment)   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which issues does the lineage step skip? — Picked: A — only issues the target-branch gate rejected on a merged PR; tracking issues and unmerged closes keep today's finalization. Alternatives: B — also stop finalizing orchestrator-tracking issues. Why: §5, the finding names the gate only; tracking-issue lineage is a separate behaviour. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How is the per-issue list represented? — Picked: A — a positive allow-list `LINEAGE_FINALIZE_ISSUE_NUMBERS` exported next to `LINKED_ISSUE_NUMBERS`. Alternatives: B — export a rejected list and subtract it in the lineage step. Why: fails closed and matches the finding's recommendation. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Does this need a changelog fragment? — Picked: A — yes, `changelog.d/5227-lineage-only-on-target-branch-merges.md` under `security`. Alternatives: B — none. Why: §20.A lists security fixes. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Should the Telegram alert and cleanup steps also use the accepted list? — Picked: A — no, they keep `LINKED_ISSUE_NUMBERS`. Alternatives: B — switch them too. Why: §5; they are not lineage writes and the finding does not cover them. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5227#issuecomment-5899512080
- Security pass: skip (`security_pass_skip.py`: ai:security created and labelled by the issue automation).
