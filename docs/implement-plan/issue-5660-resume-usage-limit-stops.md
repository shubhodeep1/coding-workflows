# Implement-Plan Log — Claude issue pickup: resume sessions stopped by the usage limit

- Plan: docs/plans/issue-5660-resume-usage-limit-stops-plan.md
- Source issue: shubhodeep1/coding-workflows#5660 (progress comment 5911814092)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5660-resume-usage-limit-stops   Final PR: #5678 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #5718: twin sync
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 PR #5718 opened twin-first; hold claim posted; waiting on the Q40 twin sync (twins + claude-issue-pickup.md diff) and /reclarify on #5660

## Phases
1. [ ] Phase 1 — usage-limit resumes (selector script, pickup step 1a, sweep name, docs, tests)   — protected paths: .claude/scripts/usage_limit_resumes.py [new], .claude/scripts/stale_routines.py, .claude/settings.json (via workflow-templates/.claude/** twins); .claude/commands/claude-issue-pickup.md (no twin — diff in the sync blocker)   — PR #5718 open (hold: twin sync); review rounds: 0; interventions: 0

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which `rate_limit_info` decides the hold-off, and what counts as allowed? — Picked: A — the pickup's own entry account-wide and each candidate's own entry; `allowed` and `allowed_warning` are allowed; a limited status holds only while its `resetsAt` is in the future. Alternatives: B — pickup's own only; C — any status but `allowed` holds. Why: `allowed_warning` is the normal state (95 of 200 sessions), snapshots are frozen at the last turn. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Does `rejected` on overage count as limited? — Picked: A — yes, holds while `resetsAt` is in the future. Alternatives: B — overage counts as allowed. Why: the issue says hold when not allowed; resuming on paid overage is the operator's cost decision. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Scope of the snapshot-only rule from the owner's comment? — Picked: A — checker sessions only, plus `status_category` not `need_input`. Alternatives: B — every session. Why: 13 finished sessions showed `rejected` overage snapshots with healthy summaries on 2026-09-30. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] How is a duplicate stage prevented when a resumed checker had already started it? — Picked: A — the checker resume prompt checks `list_sessions` for the exact title first. Alternatives: B — a child-session heuristic in the script; C — no guard. Why: B misreads quick stages; C risks a duplicate stage. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] How does the pickup pace `create_trigger`? — Picked: A — 10 per minute across the wake (steps 1a and 3); background `sleep 60` after each 10th call and on a refusal; up to 3 retries. Alternatives: B — stop at the first refusal; C — foreground `sleep`. Why: the issue says wait it out; the harness blocks foreground `sleep`. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Which bound triggers block a resume? — Picked: A — a checker: any enabled trigger (its triggers are its own check-ins, so one means its chain is alive; refined in phase 1 from "snapshot path only"); any other session: an enabled trigger due within 30 minutes, overdue, or unreadable. Alternatives: B — any enabled trigger on both. Why: the issue names 30 minutes; stage sessions keep a 7-day hand-back Routine. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Trigger name without an issue number? — Picked: A — `(#<N>)`, else `(PR #<n>)`, else the session id's last 8 characters. Alternatives: B — always the session id. Why: keeps the issue's format under the 60-character cap. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-30] Who writes the resume prompts? — Picked: A — the script, from fixed text and a required non-empty `--handoff-author-login`. Alternatives: B — the pickup model. Why: "the script decides", testable, no untrusted text in prompts. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-30] How far back are sessions listed? — Picked: A — 3 days by `created_at`, at most 10 pages of 100. Alternatives: B — 8 days. Why: the issue specifies it; every hourly wake pays for paging. Applied in: phase 1 PR. Status: pending review
- AD-10 [plan, 2026-09-30] Which pickup modes run the step? — Picked: A — `start`, `— wake.`, `— wake. — catch-up`, before the queue. Alternatives: B — hourly wakes only. Why: the issue says every wake. Applied in: phase 1 PR. Status: pending review
- AD-11 [plan, 2026-09-30] How does this phase change `.claude/**`? — Picked: A — twin-first per Q40; pickup diff in the sync blocker; hold claim. Alternatives: B — edit `.claude/**` unattended; C — drop the `.claude/` part. Why: §28.C forbids B; C ships nothing. Applied in: phase 1 PR. Status: pending review
- AD-12 [plan, 2026-09-30] Where does the selector live? — Picked: A — `.claude/scripts/usage_limit_resumes.py` with its twin. Alternatives: B — `scripts/usage_limit_resumes.py`. Why: the issue names the path. Applied in: phase 1 PR. Status: pending review
- AD-13 [plan, 2026-09-30] The handbook's manual procedure and the "Retiring the master" table? — Picked: A — replace the procedure with a pointer plus a fallback for sessions beyond the 3-day window; add the row. Alternatives: B — delete the note, add no row. Why: the issue asks for both. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] A selector that resumes stopped sessions must treat any trigger bound to a checker as a live chain (its triggers are its own check-ins), or a resume starts a second send_later chain. (files: workflow-templates/.claude/scripts/usage_limit_resumes.py)
- [source:plan-deviation] A `rejected` rate_limit_info snapshot does not mean a session stopped: turns completed on overage record it too, so it may only select checkers with no bound trigger. (files: workflow-templates/.claude/scripts/usage_limit_resumes.py)

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
- security_pass_skip.py: {"skip": false, "label": null, "reason": "no skip label"} → Security pass: run.
- Phase 1 verification (2026-09-30): full suite in this session's checkout 156 failed / 4916 passed; 114 of the failures also fail on an origin/main copy (missing local tools), 31 orchestrate_* tests time out only in the proxied checkout and pass on a local clone of the phase head, and the rest are the expected pre-sync parity and pickup assertions. A clone with the twins copied and the pickup diff applied passes all 774 tests of the affected suites.
- Twin-sync blocker posted on #5660 (ai:claude-blocked); no project checker armed yet (twin-first: the stage that /reclarify resumes arms the wait on PR #5718).
