# Implement-Plan Log — Pin the guard differential verifier to the base branch

- Plan: docs/plans/issue-5327-pin-guard-differential-verifier-plan.md
- Source issue: shubhodeep1/coding-workflows#5327   Progress comment: 5902400503
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-5174-guard-differential-check
- Project branch: claude/implement-plan-issue-5327-pin-guard-differential-verifier   Final PR: (opened after this commit)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened from the issue base; phase 1 starting.

## Phases
1. [ ] Phase 1 — pin the verifier to the base branch and report verifier changes (ci.yml step, scripts/guard_differential.py, tests, agents.md, changelog)

## Conformance

## Security pass
- Skipped: ai:security: automation-produced issue (`.claude/scripts/security_pass_skip.py`).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Where should the pinned verifier run? — Picked: A — keep the `pull_request` step and run the base commit's `scripts/guard_differential.py` blob, copied into `$RUNNER_TEMP`, against the checked-out merge commit. Alternatives: B — a `pull_request_target` / `workflow_run` workflow defined on the default branch; C — keep the PR's verifier and only add checks. Why: B executes PR hook code in a privileged context and needs a §23.C ruleset change to be enforced; C leaves the exploit open; A closes it with the smallest change (§1, §5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What runs when the base branch has no verifier yet? — Picked: A — run the PR's copy and print `::warning::GUARD_DIFFERENTIAL verifier=head reason=base-has-no-verifier`. Alternatives: B — fail the step; C — skip the check. Why: the PR already controls the whole check on such a base; B would block #5185; C drops a check that runs today. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What should a PR's change to the verifier or its CI steps do? — Picked: A — report it as a `::warning::GUARD_DIFFERENTIAL verifier_change path=…` line and a summary count, without failing. Alternatives: B — fail when a hook and a verifier path change together; C — fail on any verifier-path change. Why: the pinned verifier already ignores a changed script and a PR editing the step bypasses any in-step failure, so B and C add no security while blocking #5325 / #5326-style fixes. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Which `ci.yml` changes count as verifier changes? — Picked: A — only steps whose name starts with `Guard differential`, compared as text. Alternatives: B — any change to `ci.yml`. Why: unrelated `ci.yml` edits are frequent and would drown the warning. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode: single-phase plan; security pass skipped per the plan header.
