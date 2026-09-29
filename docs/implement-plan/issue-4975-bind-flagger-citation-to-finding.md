# Implement-Plan Log — Bind the flagger's consensus_id citation to its own structured finding

- Plan: docs/plans/issue-4975-bind-flagger-citation-to-finding-plan.md
- Source issue: shubhodeep1/coding-workflows#4975
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Base branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason
- Project branch: claude/implement-plan-issue-4975-bind-flagger-citation-to-finding   Final PR: #5026 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (head claude/implement-plan-issue-4975-bind-flagger-citation-to-finding-phase-1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented and verified (186 tests in the two review suites, 693 in the 20 related suites, ruff); phase PR opened.

## Phases
1. [ ] Phase 1 — bind the flagger citation to a structured finding record (scripts/review_claude_fixer_nonblocking.py, scripts/review_run_reviewers.sh header sentence, tests, README.md, agents.md, changelog fragment)   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `.claude/scripts/security_pass_skip.py` printed `"skip": true`).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] What counts as the flagger's "specific structured finding"? — Picked: A — a `File:` record in the flagger's raw pass-2 output, outside fences, ending at the first blank line, next `File:` / `Requirement:` / `REJECTED_FINDING` line, heading, or fence; only a whole `consensus_id: <id>` line inside it counts. Alternatives: B — any standalone `consensus_id:` line outside fences; C — a model-graded same-defect judgement. Why: the smallest deterministic binding to one finding. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How to verify the citing record describes the same defect? — Picked: A — exactly one citing record, same file as the pass-1 and pass-2 entries, a line reference overlapping both ranges; an unreadable file or line binds nothing. Alternatives: B — A plus text similarity of the problem lines; C — file match only. Why: location is the only field both passes carry verbatim. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] When is a same-line flagger finding ambiguous? — Picked: A — any other flagger record in the same file within 3 lines of either range, or in the same file (or an unreadable file) with no readable line, keeps the entry blocking. Alternatives: B — only an overlapping record; C — no flagger-side ambiguity check. Why: matches `LINE_TOLERANCE` and fails toward blocking (§1). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Reason keys for the new cases? — Picked: A — keep `flagger_did_not_cite`, add `flagger_citation_mismatch` and `ambiguous_flagger_nearby`. Alternatives: B — fold the new cases into existing keys. Why: §6 keeps existing log keys' meaning. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Tell reviewers where the `consensus_id:` line goes? — Picked: A — add one sentence to the cross-pollination header. Alternatives: B — no prompt change. Why: well-behaved reviewers keep demotion; minimal change. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] Tightening how a reviewer's raw output is parsed breaks test fixtures that model that output loosely; search every test that writes review_<slug>.txt (tests/test_review_autofix_claude_fixer_mode.py as well as the script's own suite) before changing the parser. (files: tests/test_review_autofix_claude_fixer_mode.py, scripts/review_claude_fixer_nonblocking.py)

## Notes
- Issue mode: plan written by /implement-issue-claude from #4975; security pass skipped per plan header.
- The session had neither `gh` nor the `mcp__github__*` tools at start (repo cloned after SessionStart); the repo's SessionStart hook was run by hand to install `gh`, and GitHub writes go through `gh api` REST.
