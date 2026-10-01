# Implement-Plan Log — Limit the Claude twin sync review skip to genuine sync PRs

- Plan: docs/plans/issue-5610-limit-twin-sync-review-skip-plan.md
- Source issue: shubhodeep1/coding-workflows#5610
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4785-twin-first-claude-sync
- Project branch: claude/implement-plan-issue-5610-limit-twin-sync-review-skip   Final PR: #5652 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3
- Activation: not started
- Waiting on: conformance fix PR (branch claude/implement-plan-issue-5610-limit-twin-sync-review-skip-conformance-fix-1; number in the PR list and the issue progress comment)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01UqCpJKZQmhoAuGnkmAc3LK   safety net and hand-back in the next stage's resume block
- Last updated: 2026-10-01
- Last note: phase 1 PR #5723 merged; conformance run 1 CONFORMANT with two stale-doc concerns, fixed in the conformance fix PR

## Phases
1. [x] Phase 1 — verify twin sync provenance before the review skip   — PR #5723 merged 2026-10-01; review rounds: 0; interventions: 0

## Conformance
- Run 1 — 2026-10-01: CONFORMANT (Implemented: COMPLETE; Correctness: CONCERNS, two stale-doc findings) — conformance fix PR (branch claude/implement-plan-issue-5610-limit-twin-sync-review-skip-conformance-fix-1) (pre-security; security pass skipped)

## Security pass
- Skipped (ai:security: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which repository counts as "this repository" for the exemption? — Picked: A — the hardcoded `shubhodeep1/coding-workflows`. Alternatives: B — a new repository variable; C — the resolved review-support repository output. Why: the sync workflow only runs there, and a variable could re-open the hole in a consumer. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] How is the sync workflow's bot identity verified? — Picked: A — PR author equals the GH_PAT login (`gh api user`). Alternatives: B — a new `CLAUDE_TWIN_SYNC_BOT_LOGIN` variable; C — also check the head commit's author/committer. Why: no new configuration and no extra API call; PR authorship cannot be spoofed. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] What happens when an identity cannot be read? — Picked: A — deny the exemption and review normally. Alternatives: B — keep the skip. Why: §1; genuine sync PRs still skip via `[skip ai]`. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] Does the no-PR claude-branch push path keep the exemption? — Picked: A — in coding-workflows only. Alternatives: B — never. Why: that run cannot merge and the push can only come from this repository. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Align the §26.H sweep and CI's `is_sync_pr_head`? — Picked: A — no, out of scope. Alternatives: B — align both. Why: §5; neither is a review-gate exemption. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] How is a sync-named PR that fails the check handled? — Picked: A — reviewed normally. Alternatives: B — keep skipping; C — fail the gate. Why: the issue's recommendation. Applied in: phase 1. Status: pending review

## Lessons
- [source:conformance] When a change narrows a skip or exemption, grep CLAUDE.md and agents.md for every sentence that still describes the old trigger (for example "the gate skips `<branch prefix>`"): CLAUDE.md is synced to consumers and Claude sessions act on it. (files: CLAUDE.md, agents.md)
- [source:conformance] A fail-closed fallback that relies on a PR-body marker such as `[skip ai]` only works on `pull_request` runs: `workflow_dispatch` callers pass no PR title or body to the review gate, so docs must not promise the fallback for every run. (files: .github/workflows/review_autofix.yml, .github/workflows/internal-review.yml)

## Notes
- Security pass skipped: `security_pass_skip.py` returned skip=true (label ai:security).
- Phase 1 local verification: `tests/test_review_autofix_claude_fixer_mode.py` 49 passed (the 4 new tests fail on the old gate); full `tests/` suite under Python 3.12: 5103 passed, 9 skipped, 0 failed. The container needed `gawk` installed (`scripts/review_issue_ledger.sh`), as GitHub runners have it; under Python 3.11 `tests/test_workflow_retro.py` cannot be collected (3.12 f-string syntax), which is pre-existing and unrelated.
- 2026-10-01 conformance 1/3: merged origin/claude/implement-plan-issue-4785-twin-first-claude-sync into the project branch (clean, b37ddb1); base PR not merged yet. Local checks: 976 passed across tests/test_review_autofix_*, test_claude_twin_sync, test_workflow_file_size_limit, and the suites the base merge touched; the full suite in this container only fails tests that source scripts/orchestrate_poll_process.sh (60 s hang here, untouched by this project, green in base-branch CI).
