# Implement-Plan Log — Fail closed on direct `.claude/` guard-path changes in the twin sync-state check

- Plan: docs/plans/issue-5246-guard-sync-provenance-plan.md
- Source issue: shubhodeep1/coding-workflows#5246
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4785-twin-first-claude-sync
- Project branch: claude/implement-plan-issue-5246-guard-sync-provenance   Final PR: #5267 draft
- Status: BLOCKED
- Stage: validation 1/3
- Activation: not started
- Waiting on: answer to Q1 on #5246 (`ai:claude-blocked`), then `/reclarify`
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01V6cH71S7p1H6ovhesNZWfA (idle, no pending check-in)   safety net: none   hand-back: none
- Last updated: 2026-09-30
- Last note: conformance 2/3 CONFORMANT with no fix PR (Correctness: CONCERNS, one HYPOTHESIS outside the plan's scope). Security is skipped by the plan header. Validation was not dispatched: `validate.yml` authorizes `target_ref` only for an open PR into the default branch, and final PR #5267 targets the #4785 project branch, so the run would fail before validating. Stopped at `Status: BLOCKED` and asked Q1 on #5246 (CLAUDE.md §28.C; standing decision Q17 applies). After `Q1: A` the next stage is `completion`.

## Phases
1. [x] Phase 1 — guard-path rule in `claude_twin_sync.py check`, CI wiring, tests, docs   — PR #5273 merged 2026-09-29; review rounds: 0; interventions: 0

## Conformance
- Run 1 — 2026-09-29: CONFORMANT (Implemented: COMPLETE, Correctness: CONCERNS) — conformance fix PR #5322 merged 2026-09-30 (8644919) (pre-security): agents.md and the changelog fragment claimed a required `lint` check lets GitHub enforce the guard rule for every PR, but a `pull_request` run uses the PR's own `ci.yml` and `scripts/claude_twin_sync.py`.
- Run 2 — 2026-09-30: CONFORMANT (Implemented: COMPLETE, Correctness: CONCERNS) — no fix PR (pre-security). Audited the project branch at 8644919 (issue base 970fa03): every goal G1–G6 traces to code, #5322's doc correction is accurate, `workflow-templates/CLAUDE.md` is a symlink so the §28.C text has no stale copy, and the parent #4785 branch changes no guard path, so the new rule does not fail its final PR #4804. One HYPOTHESIS CONCERN, outside the plan's scope (G1 and G2 name `main` only): the CI step skips PRs into `stable`, so a direct PR into `stable` that edits a hook and its twin together is not checked; `stable` is otherwise only fast-forwarded to `main` by `promote-main-to-stable.yml`, and whether a direct `stable` PR can merge unreviewed was not verified. Not fixed.

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header (`.claude/scripts/security_pass_skip.py` verified it).

## Validation
- Cycle 1 — 2026-09-30: not dispatched. `validate.yml` ("Authorize explicit validation target") accepts `target_ref` only when exactly one open PR has that head and `base=<default branch>`; final PR #5267 targets `claude/implement-plan-issue-4785-twin-first-claude-sync`, so the listing returns 0 PRs and the run would fail with `target PR binding is missing or ambiguous` before validating. #4734 (validate stacked targets) is not on `main` yet. Validating the default branch or the base branch in its place is not allowed, so this is a stop (CLAUDE.md §28.C), asked as Q1 on #5246.

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where is the guard policy enforced? — Picked: A — in `claude_twin_sync.py check` (the CI "Claude twin sync state" step in the `lint` job), fail closed for PRs into `main` and pushes to `main`. Alternatives: B — also add a `review_autofix.yml` gate skip for every PR touching a `.claude/` guard path; C — PRs only, pushes unchanged. Why: A fixes the check the finding names and blocks Claude-fixer auto-merge deterministically; B edits a 455 KB reusable workflow that consumers run, with several merge points (§5, §12.C blast radius), and the GPT-path residual is documented with the operator's required-check step. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What counts as "verified bot provenance" when every PR and commit is authored by the owner account? — Picked: A — a same-repository `claude/claude-twin-sync-*` head plus guard content bound to the twin blob and mode on the base commit. Alternatives: B — also require the `claude-twin-sync` committer identity on every head commit; C — refuse every guard change, sync PRs included. Why: B's identity is forgeable with `git config` and adds no security; C breaks the owner-merged sync path #4785 ships. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How does a push to `main` treat guard changes, where no PR context exists? — Picked: A — the new content must equal the twin at `before`, and a deletion fails. Alternatives: B — keep the twin-at-head rule for pushes; C — skip guard paths on pushes. Why: A passes every merged sync PR and reports a combined twin-and-`.claude/` edit that reached `main`; B and C leave the bypass invisible after merge (§1). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Is deleting a guard file together with its twin still allowed as a "synced delete"? — Picked: A — no: a guard deletion is always a violation, so removing a hook or setting needs the owner's hand-merge. Alternatives: B — keep allowing a two-sided guard delete. Why: the sync never copies a deletion, and removing a guard weakens what sessions are limited by (§1); non-guard deletes are unchanged (§5). Applied in: phase 1. Status: pending review

## Lessons
- [source:conformance] A check that runs on `pull_request` executes the PR's own workflow file and scripts, so docs must not claim that requiring it enforces a rule against a PR that can also edit that check. (files: .github/workflows/ci.yml, scripts/claude_twin_sync.py, agents.md)

## Notes
- Phase 1 adds `guard_violation_reason` next to the planned `is_sync_pr_head` in `scripts/claude_twin_sync.py`, to keep `check_not_ahead` readable; the behaviour is the plan's.
- Invoked by the Claude issue dispatcher (`/implement-issue-claude`), session session_01CkwU2FZQfHVM3gi2PP6RdZ, in Auto mode.
- Base branch `claude/implement-plan-issue-4785-twin-first-claude-sync` is unmerged (its final PR #4804 into `main` is a draft), so this project ends after its final merge: `Activation: n/a` and the final-merge stage closes #5246 with `ai:merged`.
- No phase touches `.claude/**`.
- Conformance 2/3 HYPOTHESIS (not fixed, outside the plan's scope): `ci.yml`'s "Claude twin sync state" step skips PRs into `stable` (its `else` branch prints "enforced on PRs into main and pushes to main; skipping"), so the guard rule does not cover a direct PR into `stable`. The repository's only ruleset (`Copilot review for default branch`) targets the default branch.
