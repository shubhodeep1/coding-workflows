# Implement-Plan Log — Rejection votes never demote a finding without an automated disproof

- Plan: docs/plans/issue-5582-rejection-votes-need-automated-proof-plan.md
- Source issue: shubhodeep1/coding-workflows#5582
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5582-rejection-votes-need-automated-proof   Final PR: #5605 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5611 (phase 1/1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01EbGhNnCvS36xDxb7uEq7sz   safety net trig_01WEmgJaepRai7GtkhkUmabH   hand-back trig_01JU3LjnRVwb57ZAZ1mAW2QW
- Last updated: 2026-09-30
- Last note: review round 1 on PR #5611: merged the synced project branch (sibling #4975 landed in the issue base; README keep-reason list and pass-2 header conflicts resolved keeping both sides) and fixed the one task gap (README `CLAUDE_FIXER_ENABLED` row still promised vote-only demotion)

## Phases
1. [ ] Phase 1 — votes alone never demote a single-reviewer finding   — PR #5611 open (waiting); review rounds: 1; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `.claude/scripts/security_pass_skip.py` verified it)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How should the gate stop quoted-source votes from demoting a finding? — Picked: A — demote only when a caller-supplied `disproof_check` independently proves the finding false. None exists in production, so votes never demote on their own; they stay as diagnostics. Alternatives: B — an opt-in repo variable that re-enables vote-only demotion (default off); C — build an automated disproof mechanism now. Why: model-written votes are untrusted however they are verified; B keeps a switch that reopens the hole (§1), and C has no generic design and is far beyond this issue (§5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] Where does the new keep reason sit among the existing checks? — Picked: A — last, after `too_few_rejecters`, as `no_automated_proof`. Alternatives: B — first, for every single-reviewer entry. Why: the log keeps naming the first failing condition, which the #4586-#4976 diagnostics depend on (§8). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] What happens to the pass-2 rejection instructions? — Picked: A — keep issuing IDs and asking for `REJECTED_FINDING` lines, but reword the sentence that promises a rejected singleton is not handed to the fixer. Alternatives: B — stop issuing IDs and asking for votes; C — leave the text as it is. Why: B removes a mechanism and its log lines (§6) that a future disproof check would build on; C leaves a false statement in the reviewer prompt. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] Should other projects' changelog fragments and the `.claude/` command docs be edited? — Picked: A — no; the new `security` fragment states the change, and the `.claude/` text stays true of the (now never produced) `NON-BLOCKING FINDINGS` block. Alternatives: B — edit the #4586/#4976 fragments and the `.claude/` twins. Why: smallest change (§5), and no protected-path edit in an unattended session (§28.C). Applied in: phase 1. Status: pending review

## Lessons
- [source:security] A reviewer vote is model output from PR-influenced input: verifying its fields (IDs, quotes, ranges) proves the reviewer copied text, never that a finding is false, so an unattended gate must not let votes alone clear a finding. (files: scripts/review_claude_fixer_nonblocking.py)
- [source:intervention] When a change alters behaviour a doc describes, grep every summary of it too (the Quickstart variables table row, not only the detailed section): a stale one-line summary contradicts the new rule. (files: README.md)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher in session session_01Btdcvy38twXdNjpDgPWDBg (permission mode auto). Base branch `claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason` (the issue's `Integration branch:` line; its final PR #4593 is an open draft into `main`). The issue therefore closes by an explicit close plus `ai:merged` at the final-merge stage, not by a `Fixes` keyword.
- Sibling security follow-up #4975 is in flight on `claude/implement-plan-issue-4975-bind-flagger-citation-to-finding` and edits the same function region, README.md, and agents.md paragraphs; expect a merge conflict on whichever project merges second.
- Review round 1 (2026-09-30): the sibling #4975 project merged into the issue base first, as expected; the phase branch took it through a `[claude-merge-resolve]` merge. `no_automated_proof` still runs last, after #4975's `flagger_citation_mismatch` and `ambiguous_flagger_nearby` checks.
