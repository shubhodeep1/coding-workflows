# Implement-Plan Log — Count Claude fix claims and holds only from the PR's author or the workflow account

- Plan: docs/completed/issue-4622-trusted-fix-claim-authors-plan.md (moved from docs/plans/ by the completion PR)
- Source issue: shubhodeep1/coding-workflows#4622
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main
- Project branch: claude/implement-plan-issue-4622-trusted-fix-claim-authors   Final PR: #4632 ready
- Status: IN_PROGRESS
- Stage: final-merge — review round
- Activation: pending verify-activation
- Waiting on: PR #4632 (final PR, review round 4)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01GWqGBroosei1DkAcn377y3 (project checker, reused)
- Last updated: 2026-09-28
- Last note: final-merge review round 3 (head 207086e): the panel reported zero findings and every check went green, but CI (44 min) outlasted the review's 300 s check-snapshot wait, so the workflow handed off instead of auto-merging. The step 2 sync merge of main starts round 4 on a new head.

## Phases
1. [x] Phase 1 — restrict claim authors to the PR author and the workflow account   — PR #4644 merged 2026-09-27; review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-27: CONFORMANT — no fixes (pre-security)

## Security pass
- Skipped (ai:security: automation-produced issue) — plan header `Security pass: skip`.

## Validation
- Cycle 1 — run 36322375907 2026-09-27 (target_ref: project branch, head 1290f3c): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 289s).

## Completion
- PR #4659 merged 2026-09-27 — doc moved to docs/completed/issue-4622-trusted-fix-claim-authors-plan.md
- Final PR #4632 ready — review rounds: 3 (round 1 on fafc37a: 3 findings, all rejected as false positives; round 2 on 9efe92c: 2 line-length nits fixed, 1 task gap rejected per AD-2; round 3 on 207086e: 0 findings, auto-merge withheld because CI was still running at the check snapshot; round 4 after the main sync merge)

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Which accounts may post a claim or hold that counts? — Picked: A — an owner/member/collaborator comment whose author is the PR's author or the configured workflow account `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`. Alternatives: B — only a dedicated GitHub App bot, as for fixer verdicts; C — keep association-only and give holds a lease. Why: A closes the spoofing path for every collaborator who does not own the PR while keeping the two identities that actually post claims; B would ignore every session claim because web sessions cannot post as a bot; C leaves the forgery in place for days. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] Should a hold also need verified cap state (`cap_reached`) to count? — Picked: A — no; bind holds by author identity only. Alternatives: B — honour a hold only when `cap_reached` is true. Why: `/fix-claude-pr` legitimately holds dead ends below the cap, so B would break documented holds; the author binding already stops forged holds and forged counted claims. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] Add a new env var listing extra trusted claim logins? — Picked: A — no. Alternatives: B — add `CLAUDE_FIX_CLAIM_AUTHOR_LOGINS`. Why: §5 minimal change; the PR author and the workflow account cover every writer today. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] Compare logins exactly or case-insensitively? — Picked: A — case-insensitively. Alternatives: B — exact. Why: GitHub logins are case-insensitive and unique; a mis-cased repo variable no longer drops sweep reservations. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] Should claims from untrusted authors still count toward `hand_backs`? — Picked: A — no. Alternatives: B — count them. Why: forged counted claims would otherwise push a PR to the cap and force a real fixer into a hold. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:intervention] When a test relies on an env var its shared fixture sets, also set it in the test itself if the assertion depends on it; the reviewer panel reads tests in isolation and flags the fixture-set value as missing. (files: tests/test_check_in_status_hand_back.py)
- [source:intervention] In Claude-fixer mode a clean review auto-merges only when every check on the head is already complete; a CI run longer than `CHECK_RUNS_WAIT_TIMEOUT_SECS` (300 s) turns a zero-finding review into a findings hand-off that only the dedicated verdict bot or a new head can close. (files: scripts/review_autofix_step_claude_fixer_handoff.sh)

## Notes
- 2026-09-27 validation 1/3 stage: project branch synced with main at 9dbedb2; README `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` row conflicted with #4601 and was resolved by keeping both sentences.
- Started by the Claude issue dispatcher (`/implement-issue-claude`, issue mode) in session session_01T3kpGgTVrJhjefULKLUUqL, permission mode auto.
- Issue progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4622#issuecomment-5854270496
- 2026-09-27 final-merge review round 1 (head fafc37a): all 3 findings rejected as false positives (https://github.com/shubhodeep1/coding-workflows/pull/4632#issuecomment-5857879664); convergence needed a dedicated-bot verdict this session cannot post, so the head was held and Q1 asked on the issue (https://github.com/shubhodeep1/coding-workflows/issues/4622#issuecomment-5857881698).
- 2026-09-27 Q1 answered by the owner: A (https://github.com/shubhodeep1/coding-workflows/issues/4622#issuecomment-5860414886). Resumed in session session_01JqR7ctkXcMwSibYZ3vRHoq: claimed head fafc37a (kind review, lifts the hold), merged main (clean), pushed one [claude-autofix] commit.
- 2026-09-27 final-merge review round 2 (head 9efe92c), session session_018yEAaM7ydUTg85UAyLGb7m: claimed the head (kind review); fixed the CLAUDE.md:2003 and agents.md:915 line-length nits by rewrapping; rejected the task gap asking holds to require verified cap state (AD-2: `/fix-claude-pr` holds dead ends below the cap).
- 2026-09-28 final-merge review round 3 (head 207086e, run 36360387041), session session_01RcY77osrKWSNbkX5LTiSjS: ledger clean (0 findings from 6 reviewers), `CLAUDE_FIXER_HANDOFF ... findings=0 failed_checks=none`; the hand-off step logged `CHECK_RUNS_WAIT_TIMEOUT reached after 300s` with CI run 36360386467 still in progress (it passed at 00:41). Claimed the head (kind review), merged main (clean, docs-only), and pushed the merge, so round 4 reviews a new head. No bot verdict was posted.
