# Implement-Plan Log — Claude-fixer review: a failed-slot ledger must cover every reviewer the runner ran

- Plan: docs/plans/issue-5297-ledger-must-cover-reviewer-roster-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5297-ledger-must-cover-reviewer-roster   Final PR: #5303 draft
- Source issue: shubhodeep1/coding-workflows#5297   Base branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote   Security pass: skip (ai:security: automation-produced issue)
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5306 (phase 1/1 into the project branch)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01U5bovtxjovuW9C4AdVFSvq   safety net and hand-back: see the latest stage report and resume block
- Last updated: 2026-09-30
- Last note: review round 1 on PR #5306: fixed the mawk/gawk coverage gap for the roster check, the ambiguous agents.md sentence, and the empty-roster warning for an unset or missing PREVIOUS_REVIEWS_DIR; rejected the `task_gap` prefix finding (the parsed block list holds only `clean` and `failed` records)

## Phases
1. [ ] Phase 1 — Ledger must cover the runner's reviewer roster (failed-slot path of the Claude-fixer clean-ledger check, tests, docs, changelog)   — PR #5306 open (waiting); review rounds: 1; interventions: 0
   - scripts/review_autofix_step_claude_fixer_handoff.sh: roster from status_review_<slug>.txt and review_<slug>.txt; every roster slot needs a ledger block; empty roster or bad slug fails closed
   - tests/test_review_autofix_claude_fixer_mode.py: exploit (6 reviewers, min 4, 1 failed, 1 omitted finding) hands off; omitted clean slot, output-only slot, bad slug hand off; existing tests pass
   - README.md, agents.md, changelog.d/5297-ledger-must-cover-reviewer-roster.md
   - Done: new and existing Claude-fixer tests pass, workflow size test and review_autofix contract tests pass, shellcheck clean

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Should the roster check also apply to ledgers with no failed slot? — Picked: A — no, only the failed-slot path. Alternatives: B — every ledger. Why: the issue and the audited line are the failed-slot quorum; the every-block-clean rule is main's pre-existing behaviour, kept by #4835 AD-1 and #5114 AD-1 (§5, §12.D). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What is the runner-generated roster? — Picked: A — every slug with a status_review_<slug>.txt or review_<slug>.txt in PREVIOUS_REVIEWS_DIR. Alternatives: B — a new roster file written by the runner; C — the status files only. Why: A covers the summariser's inputs and every slot the runner recorded with no runner change. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Add the roster size to the CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS log line? — Picked: A — no; the omission gets its own ::warning:: line. Alternatives: B — append roster=<n>. Why: §5 and §6. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Security pass for this project? — Picked: A — skip, as security_pass_skip.py verified. Alternatives: B — run. Why: the issue is itself an audit follow-up; the parent project's security pass re-audits the branch. Applied in: no code change. Status: pending review

## Lessons
- [source:intervention] Every new awk program in a review_autofix step script needs its own mawk and gawk test run; a parametrised test that covers another awk program in the same script does not cover it. (files: scripts/review_autofix_step_claude_fixer_handoff.sh, tests/test_review_autofix_claude_fixer_mode.py)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher; session session_01PNBt8chdKoRBRJDwPFFsHj in Auto mode.
- Issue progress comment id 5901135136.
- security_pass_skip.py: {"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}.
- Issue base check (2026-09-29): PR #4847 (head = issue base) is open and draft, so the base has not moved.
