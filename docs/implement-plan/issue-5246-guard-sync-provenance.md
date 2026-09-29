# Implement-Plan Log — Fail closed on direct `.claude/` guard-path changes in the twin sync-state check

- Plan: docs/plans/issue-5246-guard-sync-provenance-plan.md
- Source issue: shubhodeep1/coding-workflows#5246
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4785-twin-first-claude-sync
- Project branch: claude/implement-plan-issue-5246-guard-sync-provenance   Final PR: #5267 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #5273
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01V6cH71S7p1H6ovhesNZWfA   safety net trig_014gA4xY3L2q8cWZTSo8r9df   hand-back trig_01JPhGbbeDDdkLmDbhQeSuCa
- Last updated: 2026-09-29
- Last note: phase 1 PR #5273 opened against the project branch; waiting on its review round or merge.

## Phases
1. [ ] Phase 1 — guard-path rule in `claude_twin_sync.py check`, CI wiring, tests, docs   — PR #5273 open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header (`.claude/scripts/security_pass_skip.py` verified it).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where is the guard policy enforced? — Picked: A — in `claude_twin_sync.py check` (the CI "Claude twin sync state" step in the `lint` job), fail closed for PRs into `main` and pushes to `main`. Alternatives: B — also add a `review_autofix.yml` gate skip for every PR touching a `.claude/` guard path; C — PRs only, pushes unchanged. Why: A fixes the check the finding names and blocks Claude-fixer auto-merge deterministically; B edits a 455 KB reusable workflow that consumers run, with several merge points (§5, §12.C blast radius), and the GPT-path residual is documented with the operator's required-check step. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What counts as "verified bot provenance" when every PR and commit is authored by the owner account? — Picked: A — a same-repository `claude/claude-twin-sync-*` head plus guard content bound to the twin blob and mode on the base commit. Alternatives: B — also require the `claude-twin-sync` committer identity on every head commit; C — refuse every guard change, sync PRs included. Why: B's identity is forgeable with `git config` and adds no security; C breaks the owner-merged sync path #4785 ships. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How does a push to `main` treat guard changes, where no PR context exists? — Picked: A — the new content must equal the twin at `before`, and a deletion fails. Alternatives: B — keep the twin-at-head rule for pushes; C — skip guard paths on pushes. Why: A passes every merged sync PR and reports a combined twin-and-`.claude/` edit that reached `main`; B and C leave the bypass invisible after merge (§1). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Is deleting a guard file together with its twin still allowed as a "synced delete"? — Picked: A — no: a guard deletion is always a violation, so removing a hook or setting needs the owner's hand-merge. Alternatives: B — keep allowing a two-sided guard delete. Why: the sync never copies a deletion, and removing a guard weakens what sessions are limited by (§1); non-guard deletes are unchanged (§5). Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Phase 1 adds `guard_violation_reason` next to the planned `is_sync_pr_head` in `scripts/claude_twin_sync.py`, to keep `check_not_ahead` readable; the behaviour is the plan's.
- Invoked by the Claude issue dispatcher (`/implement-issue-claude`), session session_01CkwU2FZQfHVM3gi2PP6RdZ, in Auto mode.
- Base branch `claude/implement-plan-issue-4785-twin-first-claude-sync` is unmerged (its final PR #4804 into `main` is a draft), so this project ends after its final merge: `Activation: n/a` and the final-merge stage closes #5246 with `ai:merged`.
- No phase touches `.claude/**`.
