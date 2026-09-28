# Implement-Plan Log — Claude issue intake authorizes the target issue and its dispatcher

- Plan: docs/completed/issue-4620-intake-authorize-target-issue-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4620
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main
- Project branch: claude/implement-plan-issue-4620-intake-authorize-target-issue   Final PR: #4633 ready
- Status: COMPLETE
- Stage: final-merge — review round
- Activation: pending verify-activation
- Waiting on: PR #4633
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_016k76j8mrNr5YTgGxjRTT7q (project checker)
- Last updated: 2026-09-28
- Last note: final PR #4633 review round 2 (head 0dbf8cf) — 4 findings: 2 fixed in a [claude-autofix] commit (invalid_payload now refuses through reject() and writes nothing to the payload-named issue, AD-6; fail() omits the Run line when RUN_URL is empty), 2 rejected (`maintain` already reads as `write` in the REST `permission` field; error_tail is capped at 300 bytes); project branch synced with main at 2a769a0.

## Phases
1. [x] Phase 1 — intake authorizes the target issue and dispatcher   — PR #4637 merged 2026-09-27 (squash 1dd20d0, merged by a human per Q1: A on #4620); review rounds: 2; interventions: 0

## Conformance
- Run 1 — 2026-09-27: CONFORMANT — no fixes (pre-security)

## Security pass
- Skipped: plan header `Security pass: skip (ai:security: automation-produced issue)`

## Validation
- Cycle 1 — run 36323096060 2026-09-27 (target_ref: project branch, head 193e287): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 277s); no fixes

## Completion
- Completion PR #4660 merged into the project branch 2026-09-27 (squash 63dc0c2) by a human per Q2: A on #4620 — doc moved to docs/completed/issue-4620-intake-authorize-target-issue-plan.md; review rounds: 1 (0 findings; auto-merge withheld because the check snapshot timed out on a concurrent push-event review); 3 push-review findings rejected
- Merged PRs: phase 1 #4637, completion #4660
- Final PR #4633 ready 2026-09-27 — review rounds: 2 (round 1, head 20abf81: 1 finding fixed — reject() no longer sends a bare `Run: ` line when RUN_URL is empty; round 2, head 0dbf8cf: 2 of 4 findings fixed — invalid_payload writes nothing to the target (AD-6), fail() omits a bare `Run: ` line; 2 rejected)

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] How should each dispatch be bound to an authorized party? — Picked: A — check that every dispatcher login GitHub reports for the run (`github.actor`, and `github.triggering_actor` when different) has `admin` or `write` permission on the target repo. Alternatives: B — verify the payload's `reporter_run_url` is a live clarify run in the target repo; C — both. Why: the actor is set by GitHub and cannot be forged in the payload, while a run URL only proves some run exists and cannot be tied to one issue. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] Which issue authors may be implemented? — Picked: A — clarify's rule: a `User` with `OWNER`/`MEMBER`/`COLLABORATOR` association or `github-actions[bot]`, or any author when a trusted `User` commented `/reclarify`. Alternatives: B — trusted authors only, ignoring `/reclarify`; C — no author check. Why: matches the gate the dispatch bypasses, so a collaborator can still vouch for an outside issue as clarify allows today. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] What does a refused or unverifiable dispatch write? — Picked: A — nothing on the target issue; `CLAUDE_ISSUE_INTAKE rejected` log, `::error::`, Telegram ERROR, exit 1, also for read failures. Alternatives: B — the existing `fail` path (label + comment on the target); C — B for read failures only. Why: until authorization succeeds the target is unverified, so an unauthorized dispatcher must not be able to make `GH_PAT` write to arbitrary issues. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] Should the intake queue a closed issue? — Picked: A — refuse as `issue_closed`. Alternatives: B — queue it and let `/implement-issue-claude` stop. Why: the recommendation asks to verify the live target, clarify skips closed issues, and a refused closed issue costs no session. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] Add a second author gate inside `/implement-issue-claude`? — Picked: A — no; the intake is the only producer of queue items the pickup trusts (`github-actions[bot]`), so one gate there closes the path. Alternatives: B — also gate in the command file. Why: §5 minimal change; a human running the command by hand chooses the issue themselves, and the command file ships to every consumer repo. Applied in: no code change. Status: pending review
- AD-6 [final-merge — review round 2, 2026-09-28] Should an `invalid_payload` refusal still label and comment on the issue the payload names, which the plan listed as a non-goal to change? — Picked: A — no; it refuses through reject() like an authorization refusal (log, `::error::`, Telegram ERROR, exit 1, no write), log reason `invalid_payload` unchanged. Alternatives: B — keep fail() and document the gap; C — authorize the dispatcher against the payload repo before writing. Why: §1 security first — the payload is unverified before step 1b, so fail() let anyone who can dispatch make GH_PAT label and comment on any issue it can reach (an unregistered repo included), the same confused-deputy AD-3 closes; C adds reads for a malformed payload. Applied in: PR #4633 (review round 2). Status: pending review

## Lessons
- [source:intervention] When a shell step reports a `gh_retry_to_file` failure, quote the tail of the captured stderr, not the head: the helper writes its retry warnings first and the final attempt's error last, so a head cut can hide the error that decided. (files: scripts/gh_helpers.sh, scripts/claude_issue_intake.sh)
- [source:intervention] Push-event branch reviews start when a claude/* branch has no PR yet, so pushing a branch just before opening its PR puts a second review run on the same head; the Claude-fixer ready-snapshot check waits only 300s and counts that run as incomplete, which withholds auto-merge from a clean review. (files: .github/workflows/internal-review.yml, scripts/review_autofix_step_claude_fixer_handoff.sh)
- [source:intervention] A "writes nothing to an unverified target" rule has to cover every failure path that runs before authorization, not only the new refusals: an earlier validation failure that reuses the old label-and-comment reporter reopens the same confused deputy. (files: scripts/claude_issue_intake.sh)

## Notes
- Security pass skipped per plan header (ai:security follow-up).
- The Claude Code Web proxy refuses `repos/<repo>/collaborators/<login>/permission` (HTTP 403), so the permission read was verified only against stubs and GitHub's documented shape; the intake runs it in Actions with GH_PAT, outside the proxy.
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4620#issuecomment-5854273112
- Project branch synced with main 2026-09-27 (193e287, clean merge).
- Project branch synced with main at 20abf81 on 2026-09-27 (conflict in the intake queue step's env block, both sides kept).
- Project branch synced with main 2026-09-27 (79ed7ed, clean merge) in the final-merge review round 1 stage (session_01HzsrwaGiEFjBvwpoM8opjA).
- Validation 1/3 — read result (session_01HpEvuimseWjo6JnShFEURj): run 36323096060 success, status=pass, validated the project branch at 193e287 (the authorized draft final PR head); project branch already contained main; opened the completion PR.
- Project branch synced with main 2026-09-28 (2a769a0, clean merge) in the final-merge review round 2 stage (session_012NCDcpbsSLphhSkuPbAMff).
- Review round 2 finding `invalid_payload` writes to the target departs from the plan's non-goal "Changing the existing `invalid_payload` / `queue_failed` failure paths"; taken as AD-6 on §1 grounds. `queue_failed` / `queue_not_configured` still use fail(), because they run after authorization.
