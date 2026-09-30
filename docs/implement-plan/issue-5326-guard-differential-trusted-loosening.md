# Implement-Plan Log — Guard differential check: accept intended loosening only from a base-branch policy, never from the PR body

- Plan: docs/plans/issue-5326-guard-differential-trusted-loosening-plan.md
- Source issue: shubhodeep1/coding-workflows#5326   Progress comment: 5902380346
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-5174-guard-differential-check
- Project branch: claude/implement-plan-issue-5326-guard-differential-trusted-loosening   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened from claude/implement-plan-issue-5174-guard-differential-check; phase 1 in progress.

## Phases
1. [ ] Phase 1 — base-branch loosening policy (script, policy file, tests, ci.yml step, docs)

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue), per the plan header.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Where may an intended-loosening exception come from? — Picked: A — a policy file read only from the base ref, each entry scoped to hook, verbatim shape, and PR head commit, with required `approved_by` / `reason` audit fields. Alternatives: B — no exceptions at all; an operator merges past the red check (§23.C); C — keep the PR body but require an OWNER/MEMBER PR author. Why: A is machine-verifiable with no API call and sits in the check's existing base-ref trust boundary; C gates nothing here because every PR is the owner's. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What does the PR body's `Intended loosening:` section do now? — Picked: A — keep parsing it (§6) but as data only: a listed shape without a policy entry still fails, and its regression line carries `pr_body_listed=true`. Alternatives: B — ignore the body, leaving `--pr-body-file` a no-op; C — require both the body listing and the policy entry. Why: keeps the flag and parser meaningful for reviewers without letting the author authorize anything. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Where does the policy live, and in what format? — Picked: A — `.github/guard_differential/intended_loosening.json`, `{"version": 1, "exceptions": [...]}`. Alternatives: B — `tests/guard_corpus/intended_loosening.json`; C — a markdown list. Why: separate from the corpus that PRs routinely edit, never matched by the corpus loader's `*.txt` glob, and strictly parseable. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] How does the check learn the head revision? — Picked: A — a new `--head-sha` flag fed from `github.event.pull_request.head.sha` in CI; otherwise the `--head-ref` commit; otherwise none, and no entry matches. Alternatives: B — the checked-out merge commit, which changes on every base move; C — the head hook tree's object id. Why: the event payload is GitHub's, not the author's, and #5326 asks for head-revision scope. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] New changelog fragment or correct #5174's unreleased one? — Picked: A — correct the one sentence in `changelog.d/5174-guard-differential-check.md`, no new fragment. Alternatives: B — add `changelog.d/5326-….md` as a `security` entry as well. Why: the PR-body exemption never reached a release, so a separate security entry would describe behaviour no release carried and contradict the #5174 entry. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Update #5174's plan goal G5, which specifies the PR-body exemption? — Picked: A — amend G5 to the policy rule with a pointer to #5326, so #5174's later conformance and activation audits grade the fixed behaviour. Alternatives: B — leave it, and let #5174's audits flag the fix as a regression. Why: the goal is superseded by a security finding filed against that project. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): single-phase plan written by /implement-issue-claude from issue #5326 (security-audit finding on #5174's project branch).
- Base branch is not the default branch: the issue closes by an explicit close + `ai:merged` at final merge, and steps 12–13 do not run (`Activation: n/a`).
- `ci.yml` runs only on PRs into `main` / `stable`, so neither this project's PRs nor its final PR (into #5174's branch) run the guard differential step; verification is local, and the step gates #5185 into `main`.
