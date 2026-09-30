# Implement-Plan Log — Run the Claude twin sync-state check on PRs into stable, with guard-path provenance

- Plan: docs/completed/issue-5247-stable-twin-guard-check-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5247
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4785-twin-first-claude-sync
- Project branch: claude/implement-plan-issue-5247-stable-twin-guard-check   Final PR: #5262 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4785-twin-first-claude-sync) — the project ends after the final merge; #4785 carries the change to `main`
- Waiting on: completion PR
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01DntaymvRrhdQCxacVD9Xe2   safety net / hand-back: armed by the validation 1/3 read-result stage (session_01EWb21CwpBzkQGUZ4y7iDSo), ids in its report
- Last updated: 2026-09-30
- Last note: validation cycle 1 (run 36665347481, target_ref = the project branch) passed, 10/10 tests; completion PR moves the plan to docs/completed/.

## Phases
1. [x] Phase 1 — `stable` twin check with guard provenance (`scripts/claude_twin_sync.py`, `.github/workflows/ci.yml`, tests, `agents.md`, changelog fragment; protected paths: none)   — PR #5270 merged 2026-09-30 (459043f); review rounds: 2; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Implemented: COMPLETE; Correctness: PASS) — no fixes (pre-security)

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header.

## Validation
- Cycle 1 — run 36665347481 2026-09-30 (target_ref: claude/implement-plan-issue-5247-stable-twin-guard-check): dispatched after #4734 landed on main (owner correction on #5247, Q1 skip withdrawn)
- Cycle 1 result — 2026-09-30: status=pass raw_status=pass — "Runtime validation passed (10/10 tests, 290s)."; no fix issues

## Completion
- Completion PR (this PR) — doc moved to docs/completed/issue-5247-stable-twin-guard-check-plan.md
- Final PR #5262 draft — into claude/implement-plan-issue-4785-twin-first-claude-sync

## Activation
- n/a: the base is #4785's project branch, so this project ends after the final merge (issue mode); the final-merge stage closes #5247 with `ai:merged`.

## Auto-decisions
- AD-1 [plan, 2026-09-29] How does CI tell a promotion push to `stable`, which it should skip, from a push it must check? — Picked: A — ask the compare API whether the pushed commit is already on `main` (`ahead` or `identical` means promotion), and run the full check when the call fails. Alternatives: B — treat a push of more than one commit as a promotion; C — never skip `stable` pushes. Why: B misclassifies merge-commit PR merges and one-commit promotions; C reintroduces the #4785 promotion false positive, which blocks the release gate. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What counts as "verified sync provenance" for a guard change into `stable`? — Picked: A — the guard `.claude/` file at the head must equal the same path on `main`'s tip (blob and mode, or absent on both). Alternatives: B — refuse every guard change into `stable`; C — accept any version the path ever had on `main` (full-history fetch in CI). Why: blocks guard content that never passed `main`'s owner-reviewed sync, allows a backport, needs one depth-1 fetch (§1, §5). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Does the provenance rule also apply to PRs into `main`? — Picked: A — no, only to `stable` events. Alternatives: B — apply it to `main` too. Why: on `main` the owner-reviewed sync PR is the provenance; the finding is about `stable` (§5). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Should a PR into `stable` whose head is already on `main` skip like a promotion push? — Picked: A — no, every PR into `stable` is checked. Alternatives: B — skip it after the same compare call. Why: promotions are pushes by `promote-main-to-stable.yml`, and the finding asks that PRs into `stable` fail closed (§1). Applied in: phase 1. Status: pending review
- AD-5 [phase 1/1 — review round 1, 2026-09-30] How is a push that creates `stable` (no previous tip, `before` all zeros) and is not a promotion checked? — Picked: A — check it against `main`'s tip (`--base <main tip> --guard-provenance-ref <main tip>`). Alternatives: B — keep skipping it ("No base commit"); C — fail the step outright. Why: B is the fail-open path the reviewers flagged; C blocks a legitimate re-creation from a clean commit; A holds every `.claude/` difference from `main` to the twin and guard rules (§1, §5). Applied in: PR #5270. Status: pending review

## Lessons
- [source:intervention] A branch-gated CI check that skips when `github.event.before` is all zeros fails open on a push that creates the branch; give the creating push a fixed base (for `stable`, `main`'s tip) instead of skipping. (files: .github/workflows/ci.yml)
- [source:intervention] `GET /repos/{repo}/compare/{sha}...main` returns `ahead` (not `behind`) when `sha` is an ancestor of `main`; verify compare semantics against the live API before acting on a reviewer's reading. (files: .github/workflows/ci.yml)
- [source:intervention] Reviewer models repeatedly cite a "CLAUDE.md §9 no comments" rule; §9 covers indentation and braces only, so judge comment findings against the actual CLAUDE.md text, not the reviewer's quote. (files: CLAUDE.md)
- [source:plan-deviation] A CI step that reads a token should scope it to the events that need it (`${{ github.event_name == 'push' && github.token || '' }}`): on `pull_request` the step runs PR-controlled scripts. (files: .github/workflows/ci.yml)

## Notes
- Security pass: skip (`security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- Permission mode auto; session `session_014VJr6NU3mPymKcha53aw6n` started by the Claude issue dispatcher (trigger `dispatch shubhodeep1/coding-workflows#5247: start`).
- Issue progress comment id 5899549666. `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` = `shubhodeep1`; no verdict bot configured.
- Base is not the default branch: the project ends after the final merge (`Activation: n/a`), and the final-merge stage closes #5247 with `ai:merged`.
- Base-move check (2026-09-29): #4804 (head = the base branch) is open, not merged.
- Base-move check (2026-09-30, review round 2): no closed PR has the base branch as its head; base unchanged.
- Plan deviation (phase 1): `GH_TOKEN` in the CI step is set on push events only (plan text updated in the phase PR).
- Base-move check (2026-09-30, validation read): #4804 (head = the base branch) is still open; base unchanged, and the project branch already contains the base tip (no sync push).
- Validation: the 02:42 blocker on #5247 (validate.yml accepted only final PRs into the default branch) was cleared by #4734 (PR #4746, on `main` 2026-09-30); the owner withdrew the Q1 skip and cycle 1 ran against the project branch.
