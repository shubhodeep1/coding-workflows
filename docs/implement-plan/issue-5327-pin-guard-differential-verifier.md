# Implement-Plan Log — Pin the guard differential verifier to the base branch

- Plan: docs/plans/issue-5327-pin-guard-differential-verifier-plan.md
- Source issue: shubhodeep1/coding-workflows#5327   Progress comment: 5902400503
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-5174-guard-differential-check
- Project branch: claude/implement-plan-issue-5327-pin-guard-differential-verifier   Final PR: #5364 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3 — review round
- Activation: not started
- Waiting on: PR #5539
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01QvzxwDiVmtxwS9giV5Ro4Q   safety net and hand-back: see the latest stage report
- Last updated: 2026-09-30
- Last note: conformance 1/3 review round 1 on fix PR #5539: one valid finding (the post-merge wording did not qualify the bootstrap case, where a base without the verifier runs the PR's copy) fixed in agents.md and the changelog fragment.

## Phases
1. [x] Phase 1 — pin the verifier to the base branch and report verifier changes (ci.yml step, scripts/guard_differential.py, tests, agents.md, changelog) — PR #5368 merged 2026-09-30 (b197b1f); review rounds: 3; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Correctness: CONCERNS) — fix PR #5539 (pre-security); review rounds: 1. Checks: tests/test_guard_differential.py 83 passed; yamllint, actionlint, ruff clean; CI contract, changelog, and workflow-size suites passed.

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
- [source:intervention] A field added to a script's `status=` summary line belongs on every status line the script prints, early-return paths included, and its docs must not claim it on lines printed before the value exists (for example `status=error`). (files: scripts/guard_differential.py, agents.md)
- [source:intervention] A line-based cutter for YAML steps must not end a step at a comment line indented at or left of the step (valid YAML, but not a boundary), and must drop such trailing comments, or edits to the step's later keys slip past the comparison. (files: scripts/guard_differential.py)
- [source:conformance] Docs for a CI step that runs a script fetched from the base branch at run time must say a change applies to every run after it merges, including new runs on already-open PRs, not only to PRs opened afterwards. (files: agents.md, .github/workflows/ci.yml)
- [source:intervention] When a doc states when a change takes effect ("only once it has merged"), qualify it for every fallback path the step has, such as a bootstrap branch that runs the PR's own copy, in each place the rule is stated, not only where the fallback is first described. (files: agents.md, changelog.d/5327-pinned-guard-verifier.md)

## Notes
- Issue mode: single-phase plan; security pass skipped per the plan header.
- 2026-09-30 conformance 1/3: merged claude/implement-plan-issue-5174-guard-differential-check (now carrying #5325's project, #5363) into the project branch cleanly (0adb754); tests/test_guard_differential.py 83 passed after the merge.
