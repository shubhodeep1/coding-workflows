# Implement-Plan Log — Claude-fixer review: a clean vote must come from the reviewer's own output

- Plan: docs/plans/issue-5114-verify-clean-votes-from-runner-output-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5114-verify-clean-votes-from-runner-output   Final PR: #5134 draft
- Source issue: shubhodeep1/coding-workflows#5114   Base branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote   Security pass: skip (ai:security: automation-produced issue)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (head claude/implement-plan-issue-5114-verify-clean-votes-from-runner-output-phase-1; number in the stage report and the checker instructions)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented and verified (82 Claude-fixer tests incl. 12 new, 342 review_autofix tests, changelog / plan / workflow-size contract tests, shellcheck, mawk and gawk); 10 new tests fail on the unfixed script; phase PR opened against the project branch; waiting on its review round or merge

## Phases
1. [ ] Phase 1 — Clean votes verified against the runner output (runner-output classifier in the Claude-fixer failed-slot clean check, tests, docs, changelog)   — PR open (waiting); review rounds: 0; interventions: 0
   - scripts/review_autofix_step_claude_fixer_handoff.sh: `clean:success` counts only when review_<slug>.txt is an unambiguous no-findings result (NONE present, no finding / task-gap field label, all nine lens verdicts NONE when the checklist is used); otherwise warn and hand off
   - tests/test_review_autofix_claude_fixer_mode.py: exploit case (ledger clean, runner finding), task gap, markdown labels, missing / empty / unreadable output, no NONE, lens with prose, missing lens, prose around verdicts, bare NONE, heading list pinned to the checklist prompt, mawk / gawk parity, no-failed-slot path unchanged
   - README.md, agents.md, changelog.d/5114-verify-clean-votes-from-runner-output.md
   - Done: new and existing Claude-fixer tests pass (default awk, mawk, gawk), workflow size and review_autofix contract tests pass, shellcheck clean, docs describe the requirement

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Should the runner-output check also apply to ledgers with no failed slot? — Picked: A — no, only the failed-slot path. Alternatives: B — every ledger. Why: the issue and the audited code are the failed-slot path; the every-block-clean rule is main's pre-existing behaviour shared with the GPT path, and changing it alters every Claude-fixer PR's merge contract (§5, §12.D). Flagged for human review. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What counts as an unambiguous no-findings result in review_<slug>.txt? — Picked: A — at least one NONE, no finding or task-gap field label (markdown-tolerant), and when checklist headings appear, all nine present, each followed by NONE. Alternatives: B — only headings and NONE lines; C — the runner's reviewer_output_has_findings / _has_explicit_none pair. Why: on real outputs A accepts 5 of 6 reviewers on a clean run and rejects every output with a finding; B accepts 1 of 6 and brings back the #4835 stall; C accepts a lens left without a verdict and misses markdown-decorated labels. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Where do the lens headings come from? — Picked: A — a fixed list in the script, pinned by a test to prompts/review-reviewer-checklist.txt. Alternatives: B — parse the prompt file at run time. Why: no run-time dependency on the prompts dir, and CI catches drift; B would fail open if the prompt file were missing. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine; session session_01DMNTkZSE9rs2PJLh4orpGN in Auto mode.
- Issue progress comment id 5889573899.
- security_pass_skip.py: {"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}.
- The session had no GitHub MCP tools and no `gh` at start (repo attached mid-session, so the SessionStart hook had not run); ran `.claude/hooks/session-start.sh` to install `gh`, and used `gh api` REST plus the Helpers for GitHub writes.
- Issue base PR #4847 (the #4835 project's final PR) is open and unmerged at start.
