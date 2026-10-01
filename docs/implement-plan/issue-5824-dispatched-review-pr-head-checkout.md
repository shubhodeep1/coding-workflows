# Implement-Plan Log — Dispatched review runs check out the PR head

- Plan: docs/plans/issue-5824-dispatched-review-pr-head-checkout-plan.md
- Source issue: shubhodeep1/coding-workflows#5824
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5824-dispatched-review-pr-head-checkout   Final PR: #5837 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5857
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_016vfvJu7J6F4Y3sRhBoQPFf   safety net and hand-back: see the latest stage report (round 5 stage: session_0145RCULW96ibF4Nm8vVDesf)
- Last updated: 2026-10-01
- Last note: review round 5 on PR #5857 (head 6da94e12c753): the one consensus finding (a dispatched run on a fork head checks out `github.sha` per AD-2, skips the metadata comparison, and its fork exit sets only `CAN_PUSH=false`, which no reviewer step checks, so reviewers read default-branch files against the fork diff) was valid; "Checkout PR head branch" now logs `AUTOFIX_REVIEW_CROSS_REPO_DISPATCH_SKIP … action=soft_exit` and sets `AUTOFIX_STALE_BASE_SKIP` for that case before any exit (AD-4). Nothing rejected.

## Phases
1. [ ] Phase 1 — check out the gate-verified PR head on dispatched review runs   — PR #5857 open (waiting); review rounds: 5; interventions: 0
   - gate exports `review_checkout_sha` from the existing `/pulls/<n>` fetch (same-repo heads only)
   - `codex-agent` → "Checkout repo" uses `pull_request.head.sha || review_checkout_sha || github.sha`
   - regression test `tests/test_review_autofix_dispatch_pr_head_checkout.py`; updated `tests/test_review_autofix_merge_precheck.py`
   - `agents.md` paragraph; changelog fragment `changelog.d/5824-dispatched-review-pr-head-checkout.md`
   - done: tests pass, workflow under 480,000 bytes

## Conformance

## Security pass

## Validation

## Completion
- Final PR #5837 draft

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] How should dispatched runs give reviewers the PR head's files? — Picked: A — check out the gate-verified PR head SHA into `GITHUB_WORKSPACE` for non-`pull_request` runs through a new gate output. Alternatives: B — repoint every reviewer-side read to `WORKSPACE_PATH` (~10 sites, 4 scripts); C — both. Why: one change covers every `GITHUB_WORKSPACE` reader and matches what `pull_request` runs already do (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What should a dispatched run do for a PR whose head is in another repository? — Picked: A — keep the `github.sha` checkout and log a warning. Alternatives: B — skip the run; C — check out the fork head. Why: C would put untrusted fork code next to secrets that a fork `pull_request` run never gets (§1); B changes gate routing for a case the workflow does not support anyway (§5). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] Should the `HEAD_SHA` env of the interim judge, behavioural smoke, and claude-branch consensus steps also switch to the gate head? — Picked: A — no, out of scope. Alternatives: B — switch them too. Why: they are not reviewer file reads, and the issue's acceptance criteria do not cover them (§5). Applied in: no code change. Status: pending review
- AD-4 [phase 1/1 — review round 5, 2026-10-01] What should a dispatched run on a fork head do once AD-2 keeps the `github.sha` checkout? — Picked: A — soft-exit in "Checkout PR head branch" (log `AUTOFIX_REVIEW_CROSS_REPO_DISPATCH_SKIP`, set the existing `AUTOFIX_STALE_BASE_SKIP`), so the reviewers, the Claude-fixer hand-off, and auto-merge are skipped. Alternatives: B — keep reviewing the default branch's files against the fork diff (the round-5 finding); C — route the gate to skip the job (AD-2 option B). Why: A stops false findings with the soft-exit the step already uses and changes no gate routing (§5); fork PRs stay unsupported (AD-2). Applied in: PR #5857. Status: pending review

## Lessons
- [source:intervention] A workflow change that emits a new stable log prefix must add it to both `agents.md` inventories (the "Stable log prefixes (contractual)" bullet list and the `LOG_PREFIX.name=` block) in the same PR, and a test should pin both entries. (files: agents.md, .github/workflows/review_autofix.yml)
- [source:intervention] When `agents.md` prose says which files or directories a set of scripts reads, a test should pin each named reader, not just one, or reviewers flag the unverified ones. (files: agents.md, tests/test_review_autofix_dispatch_pr_head_checkout.py)
- [source:intervention] A step that checks out one commit for file reads and later resets the branch to its live tip must compare the two SHAs and soft-exit (or re-align) when they differ, or reviewers read one tree while merge gates bind to another. (files: .github/workflows/review_autofix.yml)
- [source:intervention] A moved-head guard must run before every early exit of the step that holds it (fork heads, rejected branch names), comparing against the head the diff was collected for, or the early-exit paths pair old files with a newer diff. (files: .github/workflows/review_autofix.yml, scripts/review_collect_pr_metadata.sh)
- [source:intervention] An exit that only sets `CAN_PUSH=false` does not stop `review_autofix.yml` from reviewing or merging; a run whose workspace cannot hold the PR head must set `AUTOFIX_STALE_BASE_SKIP` (the flag the reviewer, hand-off, and auto-merge steps check). (files: .github/workflows/review_autofix.yml)

## Notes
- Security pass: run (`security_pass_skip.py`: no skip label).
