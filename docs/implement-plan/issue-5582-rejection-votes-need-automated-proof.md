# Implement-Plan Log — Rejection votes never demote a finding without an automated disproof

- Plan: docs/completed/issue-5582-rejection-votes-need-automated-proof-plan.md
- Source issue: shubhodeep1/coding-workflows#5582
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5582-rejection-votes-need-automated-proof   Final PR: #5605 ready
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation
- Waiting on: PR #5605 (final PR into the issue base; this log rides it)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01EbGhNnCvS36xDxb7uEq7sz   safety net (re-armed by the final-merge — review round 2 stage)   hand-back (re-armed by the final-merge — review round 2 stage)
- Last updated: 2026-10-01
- Last note: final PR #5605 review round 3 (head 087e32a, run 36846564089): 1 consensus finding (no test observed that the demoted record holds its own `entry` / `rejecters` copies), valid; fixed with a regression test in one [claude-autofix] commit (stage session session_01QATHZE54gUavEKgs5YFD82)

## Phases
1. [x] Phase 1 — votes alone never demote a single-reviewer finding   — PR #5611 merged 2026-09-30 (cb8a7aa); review rounds: 2; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Implemented: COMPLETE; Correctness: CONCERNS, 3 EVIDENCE-BASED concerns) — fix PR from branch `claude/implement-plan-issue-5582-rejection-votes-need-automated-proof-conformance-fix-1` (pre-security; security pass skipped for this project)
- Fix PR #5741 — review runs 1–3 failed on 2026-09-30 (OpenRouter account out of credits; fingerprint cap labelled it `ai:review-blocked`, hold claim posted); resumed 2026-10-01 on the owner's `/reclarify` (Q1: A). Review round 1 (head 4f3649a, run 36794089014): 5 findings, 1 fixed (AD-6), 4 rejected; review rounds: 1; interventions: 0. Merged 2026-10-01 as dc0af3e (the owner reviewed and merged it, Q1: A on #5582)
- Run 2 — 2026-10-01: CONFORMANT (Implemented: COMPLETE; Correctness: PASS) — no fixes (pre-security; security pass skipped)

## Security pass
- Skipped (ai:security: automation-produced issue; `.claude/scripts/security_pass_skip.py` verified it)

## Validation
- Cycle 1 — run 36813024951 2026-10-01 (target_ref: claude/implement-plan-issue-5582-rejection-votes-need-automated-proof): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 289s)

## Completion
- PR #5900 merged 2026-10-01 (ae9eb47) — doc moved to docs/completed/issue-5582-rejection-votes-need-automated-proof-plan.md
- Final PR #5605 ready 2026-10-01 — review rounds: 3 (into `claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason`)

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How should the gate stop quoted-source votes from demoting a finding? — Picked: A — demote only when a caller-supplied `disproof_check` independently proves the finding false. None exists in production, so votes never demote on their own; they stay as diagnostics. Alternatives: B — an opt-in repo variable that re-enables vote-only demotion (default off); C — build an automated disproof mechanism now. Why: model-written votes are untrusted however they are verified; B keeps a switch that reopens the hole (§1), and C has no generic design and is far beyond this issue (§5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] Where does the new keep reason sit among the existing checks? — Picked: A — last, after `too_few_rejecters`, as `no_automated_proof`. Alternatives: B — first, for every single-reviewer entry. Why: the log keeps naming the first failing condition, which the #4586-#4976 diagnostics depend on (§8). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] What happens to the pass-2 rejection instructions? — Picked: A — keep issuing IDs and asking for `REJECTED_FINDING` lines, but reword the sentence that promises a rejected singleton is not handed to the fixer. Alternatives: B — stop issuing IDs and asking for votes; C — leave the text as it is. Why: B removes a mechanism and its log lines (§6) that a future disproof check would build on; C leaves a false statement in the reviewer prompt. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] Should other projects' changelog fragments and the `.claude/` command docs be edited? — Picked: A — no; the new `security` fragment states the change, and the `.claude/` text stays true of the (now never produced) `NON-BLOCKING FINDINGS` block. Alternatives: B — edit the #4586/#4976 fragments and the `.claude/` twins. Why: smallest change (§5), and no protected-path edit in an unattended session (§28.C). Applied in: phase 1. Status: pending review
- AD-5 [phase 1/1 — review round 2, 2026-09-30] How should the misnamed test `test_handoff_rejected_singleton_auto_merges_and_stays_visible` (it now asserts no auto-merge) be fixed? — Picked: A — define it under the accurate name `test_handoff_rejected_singleton_is_handed_off_not_auto_merged` and keep the old name as an alias bound to it. Alternatives: B — rename in place; C — keep the name and reject the finding. Why: fixes the misleading name while honouring §6 (no in-place rename); the only cost is pytest running the same test twice. Applied in: PR #5611. Status: pending review
- AD-6 [conformance 1/3 — review round 1, 2026-10-01] Is PR #5741's finding that the end-to-end tests skip the verifier branch for a quoted file missing from the source valid? — Picked: A — valid at the hand-off layer: add one case to `test_handoff_rejections_without_verified_evidence_hand_the_round_off` where the reviewed commit exists but lacks the quoted file (`source_unavailable`). Alternatives: B — reject it as covered by the module test `tests/test_review_claude_fixer_nonblocking.py:1390`. Why: the end-to-end list enumerates evidence-failure shapes and lacked this one; one parametrize row, no behaviour change (§5); B would need a dedicated-bot verdict, which this repo has not configured. Applied in: PR #5741. Status: pending review
- AD-7 [final-merge — review round 2, 2026-10-01] Is final PR #5605's round-2 consensus finding, that the demoted record keeps live references to `entry` and `rejecters` instead of copies, valid? — Picked: A — apply it as hardening: the demoted record gets its own `list(entry)` / `list(rejecters)` copies, matching the copy already given to `disproof_check`. Alternatives: B — reject it, since neither list is mutated today and production never reaches the path (`disproof_check` is `None`). Why: one-line change with no behaviour change (§5); B would need a dedicated-bot verdict, which this repo has not configured, so the PR would block. Applied in: PR #5605. Status: pending review

## Lessons
- [source:security] A reviewer vote is model output from PR-influenced input: verifying its fields (IDs, quotes, ranges) proves the reviewer copied text, never that a finding is false, so an unattended gate must not let votes alone clear a finding. (files: scripts/review_claude_fixer_nonblocking.py)
- [source:intervention] When a change alters behaviour a doc describes, grep every summary of it too (the Quickstart variables table row, not only the detailed section): a stale one-line summary contradicts the new rule. (files: README.md)
- [source:conformance] When a security fix has an end-to-end regression goal, the end-to-end test must use the exploit's own input shape (here, a vote quoting the defective line), not a benign variant the gate happens to treat the same. (files: tests/test_review_autofix_claude_fixer_mode.py)
- [source:intervention] Docs that describe where reviewer votes or verdicts surface must name the real surface: the Actions run log carries only the gate's aggregate lines, per-reviewer output is in the `reviewer-logs-*` artifact, and verdict convergence happens only when `CLAUDE_FIXER_VERDICT_BOT_LOGIN` is set (empty by default). (files: README.md, scripts/review_run_reviewers.sh)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher in session session_01Btdcvy38twXdNjpDgPWDBg (permission mode auto). Base branch `claude/implement-plan-issue-4586-rejected-singleton-findings-hold-reason` (the issue's `Integration branch:` line; its final PR #4593 is an open draft into `main`). The issue therefore closes by an explicit close plus `ai:merged` at the final-merge stage, not by a `Fixes` keyword.
- Sibling security follow-up #4975 is in flight on `claude/implement-plan-issue-4975-bind-flagger-citation-to-finding` and edits the same function region, README.md, and agents.md paragraphs; expect a merge conflict on whichever project merges second.
- Review round 1 (2026-09-30): the sibling #4975 project merged into the issue base first, as expected; the phase branch took it through a `[claude-merge-resolve]` merge. `no_automated_proof` still runs last, after #4975's `flagger_citation_mismatch` and `ambiguous_flagger_nearby` checks.
- Review round 2 (2026-09-30): both consensus findings fixed (README vote-conditions paragraph; misnamed hand-off test, AD-5). The task gap was round 1's README `CLAUDE_FIXER_ENABLED` row, which its own evidence says is already updated. A dispatched review run for PR #5611 (36722924281) was still queued when this stage started; the round-2 push supersedes it.
- 2026-09-30/10-01: PR #5741's review runs failed three times because the OpenRouter account had no credits (`AI_APICallError: Insufficient credits`, run 36766395306); the stage stopped `BLOCKED` on #5582 instead of pushing. The owner topped up the credits, removed `ai:review-blocked`, and the 00:00Z sweep re-ran review (run 36794089014), which handed round 1 to Claude. The resumed session (session_01H7iPhquKg4c9Jd1NBj3WDd) replaced the hold with a review claim on 4f3649a.
- 2026-10-01 (conformance 2/3 stage): follow-up issue #5880 filed for the check-run collector's empty-snapshot ready status, found while waiting on validation; it is out of this project's scope.
- 2026-10-01 (validation 1/3 — read result stage): the step 2 `git merge --no-edit origin/<issue base>` was denied by the Auto-mode classifier (`[Modify Shared Resources]`); no merge was needed, because `git rev-list --count HEAD..origin/<issue base>` was 0.
- 2026-10-01 (final-merge — review round stage): final PR #5605 review round 1 handed 5 consensus findings on ae9eb47 (two themes: the run log holds only aggregate vote diagnostics, and verdict-bot convergence needs `CLAUDE_FIXER_VERDICT_BOT_LOGIN`, empty by default). All valid; fixed in the changelog fragment, README, the pass-2 header in `scripts/review_run_reviewers.sh`, and the archived plan. No new auto-decision.
- 2026-10-01 (final-merge — review round 2 stage): two review runs on bf878b7 (36832092673, 36832619840) each handed round 2 to Claude with the same theme; the later hand-off (ledger b89d6aa6…) was answered. Fix in `scripts/review_claude_fixer_nonblocking.py` (the `demoted.append` line); no test can observe the aliasing from outside the function, so the existing 383 tests in `tests/test_review_claude_fixer_nonblocking.py` and `tests/test_review_autofix_claude_fixer_mode.py` were the check (all pass).
- 2026-10-01 (final-merge — review round 3 stage): round 3 on 087e32a (run 36846564089, ledger d8c9b40d…) raised one consensus finding: no test checked AD-7's copies. Valid, and it corrects the round-2 note above: the aliasing is observable by wrapping `parse_ledger` to capture the parsed consensus entries. Added `test_issue_5582_the_demoted_record_holds_its_own_copies`, which fails with the copies reverted and passes with them; 384 tests pass across the two suites. No new auto-decision.
