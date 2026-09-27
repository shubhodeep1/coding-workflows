# Implement-Plan Log — Rejected single-reviewer findings stop blocking Claude-fixer PRs, and hold comments name their real reason

- Plan: docs/plans/issue-4586-rejected-singleton-findings-hold-reason-plan.md
- Source issue: shubhodeep1/coding-workflows#4586
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason   Final PR: #4593 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (the PR that carries this log commit)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-27
- Last note: phase 1 implemented and verified locally (filter, hand-off step, claim reasons, docs, tests); phase PR opened

## Phases
1. [ ] Phase 1 — non-blocking rejected singletons, and truthful hold reasons   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Where does the "explicitly rejected" signal come from? — Picked: A — a structured `REJECTED_FINDING:` line each pass-2 reviewer emits, read deterministically from its raw output. Alternatives: B — a `rejected_by:` field the summariser model writes; C — infer it from WHY prose. Why: a summariser error cannot unblock a real defect (§1). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-27] Where does the demotion apply? — Picked: A — only in the Claude-fixer hand-off step, on a filtered copy of the ledger. Alternatives: B — rewrite the shared ledger for every consumer. Why: the GPT editor judges findings itself and the memory step keeps its input (§5). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-27] How many rejecters are needed? — Picked: A — a strict majority of the other successful reviewers and at least two rejecters. Alternatives: B — a strict majority only. Why: with a two-reviewer panel, one dissent alone must not clear a finding (§1). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-27] Do task gaps get the same rule? — Picked: A — no, only CONSENSUS FINDINGS entries. Alternatives: B — task gaps too. Why: the issue scopes the change to findings (§5). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-27] How does a demoted finding stay visible when nothing else blocks? — Picked: A — post the filtered ledger (with its NON-BLOCKING block) before auto-merge, best-effort. Alternatives: B — only a workflow log line. Why: the issue requires it to stay visible on the PR. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-27] How does the hold comment learn its reason? — Picked: A — a `--reason` enum on `claude_fix_claim.py` (`cap`, `review-no-verdict-bot`, `conflict-decision`, `ci-outside-pr`, `workflow-failure`, `needs-human`), with neutral text when omitted. Alternatives: B — free-text `--reason`; C — `post` reads the claims to compute `cap_reached` itself. Why: fixed sentences are testable and safe in a comment, and C adds API reads (§15). Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-27] How close must a rejection's line be to the finding? — Picked: A — same file, range overlapping or within 3 lines, and naming the same flagger. Alternatives: B — the summariser's 5-line dedupe window without the flagger match; C — exact line only. Why: #4575's rejections cited line 1262 for a 1261 finding; the flagger match keeps a nearby different finding from being swept up. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Final PR #4593 (draft) opened 2026-09-27 into `main` with `Fixes #4586`.
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher in session session_018qDgVu4VtaQEnTp4n3MekE (permission mode auto). Base branch `main` (the issue names no integration or target branch), so the final PR carries `Fixes #4586`.
