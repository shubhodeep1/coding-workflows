# Implement-Plan Log — Claude-fixer pending-checks merge: re-check the reviewed base at merge time

- Plan: docs/plans/issue-5905-pending-merge-base-retarget-race-plan.md
- Source issue: shubhodeep1/coding-workflows#5905
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge
- Project branch: claude/implement-plan-issue-5905-pending-merge-base-retarget-race   Final PR: #5915 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5917
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-01
- Last note: review round 1 on PR #5917: fixed the re-read/disable race (confirming read; `merged_unreviewed_base` / new `merge_revoke_unconfirmed`), rejected the merged-PR base-sha and uppercase-sha findings with reasons.

## Phases
1. [ ] Phase 1 — re-check the reviewed base at merge time and revoke a stale authorization   — PR #5917 open (waiting); review rounds: 1; interventions: 0
   - `scripts/review_enable_auto_merge.sh` refuses a merge when optional `REVIEWED_BASE_REF` / `REVIEWED_BASE_SHA` do not match its pre-merge PR read (unchanged when both are empty)
   - `scripts/claude_fixer_pending_checks.py` passes the reviewed base, reports a helper refusal as `base_changed`, re-reads the PR after enabling auto-merge, and revokes it (`merge_revoked` / `merge_revoke_failed`) or reports `merged_unreviewed_base`
   - `scripts/claude_pr_sweep.py` warns on `merge_revoke_failed` / `merged_unreviewed_base`
   - Tests reproduce the #5905 retarget-inside-the-window exploit; README / agents.md; `changelog.d/` fragment
   - Done: the plan's phase 1 "done" condition. Protected paths: none.

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] Where is the reviewed base re-checked at merge time? — Picked: A — in `review_enable_auto_merge.sh`, from the PR read it already makes right before the merge call, through optional `REVIEWED_BASE_REF` / `REVIEWED_BASE_SHA` inputs only the sweep sets. Alternatives: B — a second PR read in `evaluate` just before the helper; C — bind the base in the merge mutation (not possible). Why: narrowest window with zero new API calls. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-10-01] How is an authorization cancelled when the PR is retargeted after the base check? — Picked: A — re-read the PR once right after enabling auto-merge; revoke with `gh pr merge --disable-auto` on a moved head or base or an unreadable re-read; report an already-made merge into another base as `merged_unreviewed_base`. Alternatives: B — hourly revoke of stale authorizations; C — a new `pull_request: edited` workflow. Why: A closes the window the sweep opens; B can loop against the in-run auto-merge of a fresh review; C adds a workflow for a case GitHub already covers (non-write retargets disable auto-merge; write users can merge directly). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-10-01] What triggers the re-review after a retarget? — Picked: A — the existing #5147 gate path (30-minute review sweep). Alternatives: B — the catch-all sweep dispatches `internal-review.yml` after a revoke. Why: no new dispatch or write path. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-10-01] Should the in-run zero-findings auto-merge step also pass the reviewed base? — Picked: A — no, out of scope (as #5147 AD-7). Alternatives: B — pass `PR_PAYLOAD_FILE`'s base to it. Why: §5; the finding is the sweep's delayed authorization. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-10-01] What does the helper do with only one base input, or a malformed sha? — Picked: A — refuse (`reason=base_changed`). Alternatives: B — skip the check unless both are valid. Why: fail closed. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-10-01] Which states does `evaluate` report for the refusal and the post-enable outcomes? — Picked: A — `base_changed` for the refusal; new `merge_revoked`, `merge_revoke_failed`, `merged_unreviewed_base`. Alternatives: B — fold into `merge_failed`. Why: operators can tell a security refusal and a revoke apart; no existing state changes meaning. Applied in: phase 1. Status: pending review
- AD-7 [phase 1/1 — review round 1, 2026-10-01] What does `evaluate` report when auto-merge was disabled after a moved pair but the confirming PR read fails? — Picked: A — a new alarm state `merge_revoke_unconfirmed` (in `PENDING_CHECKS_ALARM_STATES`). Alternatives: B — reuse `merge_revoke_failed`; C — report `merge_revoked` without an alarm. Why: fail loud (§1) without giving `merge_revoke_failed` a second meaning; the name was checked for collisions (§6). Applied in: PR #5917. Status: pending review

## Lessons
- [source:security] A read-compare guard placed in a caller is only as fresh as its read: re-check the guarded value in the helper that makes the final read before the write, and verify again after a write that the API cannot bind atomically. (files: scripts/claude_fixer_pending_checks.py, scripts/review_enable_auto_merge.sh)
- [source:intervention] A merged PR's `base.sha` from the REST API is a stale snapshot, not the tip it merged onto (#5637: `base.sha` 1084dfd, merge parent 5aa792d), so it cannot prove which base commit a merge used; and a revoke that follows a separate read needs one more read to rule out a merge in between. (files: scripts/claude_fixer_pending_checks.py)

## Notes
- Security pass skipped: `security_pass_skip.py` verified `ai:security` created and labelled by the issue automation (`Refs #3576`).
