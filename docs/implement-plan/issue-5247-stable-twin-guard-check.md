# Implement-Plan Log — Run the Claude twin sync-state check on PRs into stable, with guard-path provenance

- Plan: docs/plans/issue-5247-stable-twin-guard-check-plan.md
- Source issue: shubhodeep1/coding-workflows#5247
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4785-twin-first-claude-sync
- Project branch: claude/implement-plan-issue-5247-stable-twin-guard-check   Final PR: #5262 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5270
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01DntaymvRrhdQCxacVD9Xe2   safety net / hand-back: re-armed by the review-round 1 stage (session_01LxpQSNhcioY5Sj2cgFEDzF), ids in its report
- Last updated: 2026-09-30
- Last note: review round 1 on PR #5270: fixed the fail-open skip on a push that creates `stable` (AD-5) and rewrapped the agents.md bullet; rejected the compare-status, comment-style, token, and task-gap findings (compare semantics verified against the live API).

## Phases
1. [ ] Phase 1 — `stable` twin check with guard provenance (`scripts/claude_twin_sync.py`, `.github/workflows/ci.yml`, tests, `agents.md`, changelog fragment; protected paths: none)   — PR #5270 open (waiting); review rounds: 1; interventions: 0

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How does CI tell a promotion push to `stable`, which it should skip, from a push it must check? — Picked: A — ask the compare API whether the pushed commit is already on `main` (`ahead` or `identical` means promotion), and run the full check when the call fails. Alternatives: B — treat a push of more than one commit as a promotion; C — never skip `stable` pushes. Why: B misclassifies merge-commit PR merges and one-commit promotions; C reintroduces the #4785 promotion false positive, which blocks the release gate. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] What counts as "verified sync provenance" for a guard change into `stable`? — Picked: A — the guard `.claude/` file at the head must equal the same path on `main`'s tip (blob and mode, or absent on both). Alternatives: B — refuse every guard change into `stable`; C — accept any version the path ever had on `main` (full-history fetch in CI). Why: blocks guard content that never passed `main`'s owner-reviewed sync, allows a backport, needs one depth-1 fetch (§1, §5). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Does the provenance rule also apply to PRs into `main`? — Picked: A — no, only to `stable` events. Alternatives: B — apply it to `main` too. Why: on `main` the owner-reviewed sync PR is the provenance; the finding is about `stable` (§5). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Should a PR into `stable` whose head is already on `main` skip like a promotion push? — Picked: A — no, every PR into `stable` is checked. Alternatives: B — skip it after the same compare call. Why: promotions are pushes by `promote-main-to-stable.yml`, and the finding asks that PRs into `stable` fail closed (§1). Applied in: phase 1. Status: pending review
- AD-5 [phase 1/1 — review round 1, 2026-09-30] How is a push that creates `stable` (no previous tip, `before` all zeros) and is not a promotion checked? — Picked: A — check it against `main`'s tip (`--base <main tip> --guard-provenance-ref <main tip>`). Alternatives: B — keep skipping it ("No base commit"); C — fail the step outright. Why: B is the fail-open path the reviewers flagged; C blocks a legitimate re-creation from a clean commit; A holds every `.claude/` difference from `main` to the twin and guard rules (§1, §5). Applied in: PR #5270. Status: pending review

## Lessons
- [source:intervention] A branch-gated CI check that skips when `github.event.before` is all zeros fails open on a push that creates the branch; give the creating push a fixed base (for `stable`, `main`'s tip) instead of skipping. (files: .github/workflows/ci.yml)
- [source:intervention] `GET /repos/{repo}/compare/{sha}...main` returns `ahead` (not `behind`) when `sha` is an ancestor of `main`; verify compare semantics against the live API before acting on a reviewer's reading. (files: .github/workflows/ci.yml)
- [source:plan-deviation] A CI step that reads a token should scope it to the events that need it (`${{ github.event_name == 'push' && github.token || '' }}`): on `pull_request` the step runs PR-controlled scripts. (files: .github/workflows/ci.yml)

## Notes
- Security pass: skip (`security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- Permission mode auto; session `session_014VJr6NU3mPymKcha53aw6n` started by the Claude issue dispatcher (trigger `dispatch shubhodeep1/coding-workflows#5247: start`).
- Issue progress comment id 5899549666. `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` = `shubhodeep1`; no verdict bot configured.
- Base is not the default branch: the project ends after the final merge (`Activation: n/a`), and the final-merge stage closes #5247 with `ai:merged`.
- Base-move check (2026-09-29): #4804 (head = the base branch) is open, not merged.
- Plan deviation (phase 1): `GH_TOKEN` in the CI step is set on push events only (plan text updated in the phase PR).
