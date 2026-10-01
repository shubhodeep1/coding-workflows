# Implement-Plan Log — Run the Claude twin sync-state guard from the protected base commit, not the PR checkout

- Plan: docs/completed/issue-5608-run-twin-guard-from-base-plan.md (moved from docs/plans/ by the completion PR)
- Source issue: shubhodeep1/coding-workflows#5608
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-4785-twin-first-claude-sync
- Project branch: claude/implement-plan-issue-5608-run-twin-guard-from-base   Final PR: #5653 ready
- Status: COMPLETE
- Stage: final-merge — review round 1
- Activation: pending verify-activation (n/a if the base is still claude/implement-plan-issue-4785-twin-first-claude-sync when the final PR merges; Issue Mode)
- Waiting on: PR #5653 (final PR, review round 2 after the round-1 push)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_011j2At372XRi9gKjH5qhqW4 (project checker, reused)   safety net and hand-back re-armed at the end of the completion stage
- Last updated: 2026-10-01
- Last note: final PR #5653 review round 1 (head b2d943a): 1 doc gap fixed (bootstrap window spelled out in agents.md and the changelog), 4 findings rejected (test is exercised, AD-2 bootstrap by design, empty provenance unreachable, temp dir on an ephemeral runner, ci.yml gap is AD-3); issue base merged in

## Phases
1. [x] Phase 1 — run the twin sync-state guard from the base commit (protected paths: none)   — PR #5656 merged 2026-09-30 (d023432); review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Implemented: COMPLETE; Correctness: PASS, 4 files audited, test_claude_twin_sync.py 150 passed, yamllint/actionlint/size/job-split/changelog/inventory checks pass, no findings) — no fixes (pre-security)

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` printed `"skip": true`).

## Validation
- Cycle 1 — run 36748287842 2026-09-30 (target_ref: claude/implement-plan-issue-5608-run-twin-guard-from-base, head d023432): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 290s)

## Completion
- PR #5750 merged — doc moved to docs/completed/issue-5608-run-twin-guard-from-base-plan.md
- Final PR #5653 ready — review rounds: 1 (base claude/implement-plan-issue-4785-twin-first-claude-sync)

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which commit supplies the guard script the step runs? — Picked: A — the event's `base` commit (the merge commit's first parent, or `github.event.before`), with `main`'s tip as the fallback on `stable`. Alternatives: B — always `main`'s tip; C — a `pull_request_target` workflow. Why: the smallest change that runs only protected-branch content, using commits the step already fetches (§1, §5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What does the step do when the trusted commit resolves but has no `scripts/claude_twin_sync.py`? — Picked: A — log a `::warning::` and run the checkout's copy, as today. Alternatives: B — skip the check; C — fail closed. Why: a base without the script has no guard to bypass; A keeps today's coverage while an unreadable commit still fails closed (§1, §3). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Should this project also stop a PR from editing `.github/workflows/ci.yml` to drop the step? — Picked: A — no; record it as the remaining gap in `agents.md`. Alternatives: B — a `pull_request_target` re-check workflow. Why: the finding names the script; B is a new privileged workflow needing its own design and an operator ruleset change (§5, §23.C). Applied in: phase 1 (docs only). Status: pending review

## Lessons
- [source:intervention] In a fail-closed workflow step, give every git read its own `if ! …; then echo "::error::…"; exit 1; fi` even under `set -e`, so the job log names the read that failed instead of ending on a bare git exit code. (files: .github/workflows/ci.yml)
- [source:intervention] When a security fix keeps a bootstrap fallback to the untrusted copy, write down who can reach that fallback and which event ends it, in the docs and the changelog, so reviewers do not read the fallback as an open hole. (files: agents.md, .github/workflows/ci.yml)

## Notes
- Issue progress comment: 5911158781.
- Issue mode: base is not the default branch, so the final-merge stage closes #5608 with `state_reason: completed` and `ai:merged`; Activation n/a.
- 2026-10-01 final-merge review round 1: merged the issue base (claude/implement-plan-issue-4785-twin-first-claude-sync, still unmerged into main via #4804) into the project branch. The earlier `review / codex-agent` failures on b2d943a were reviewer-provider HTTP 402 errors, not this project's code.
