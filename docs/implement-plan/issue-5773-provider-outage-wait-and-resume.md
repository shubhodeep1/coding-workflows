# Implement-Plan Log — Model-provider outages: wait instead of blocking PRs, then resume on recovery

- Plan: docs/plans/issue-5773-provider-outage-wait-and-resume-plan.md
- Source issue: shubhodeep1/coding-workflows#5773 (progress comment 5921914621)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5773-provider-outage-wait-and-resume   Final PR: #5775 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #5871: twin sync
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-01
- Last note: phase 1 PR #5871 opened twin-first; hold claim posted and twin-sync blocker on #5773; the wait is armed by the /reclarify stage after the [claude-twin-sync] copy

## Phases
1. [ ] Phase 1 — classify provider outages, wait, mark once, probe and resume   — PR #5871 open (twin sync pending); review rounds: 0; interventions: 0 — protected paths: .claude/scripts/check_in_status.py, .claude/commands/fix-claude-pr.md, .claude/commands/implement-plan-claude.md (via workflow-templates/.claude/** twins)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] Where does the repo-wide outage marker live? — Picked: A — one `ai:provider-outage` issue in coding-workflows, opened by the heal intake from the reporter dispatch. Alternatives: B — a marker issue per repo; C — a git-ref lock. Why: one marker and one alert across all repos; extends the heal path (§18.A). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] Which errors classify as `provider_unavailable`? — Picked: A — 402 / credits and 401 anywhere in the failing stage's logs; 429 / 5xx only when no reviewer succeeded. Alternatives: B — any provider error line; C — 402 only. Why: an incidental 429 next to a real defect must not bypass the cap. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] How do the counters treat outage markers? — Picked: A — skipped, neither counted nor ending a run. Alternatives: B — reset the count. Why: the issue says they do not count. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] What does `check_in_status.py` report for an outage? — Picked: A — not-done `provider-unavailable` (`wait`) when every failed check is an outage run; other failed checks still hand back. Alternatives: B — any outage marker suppresses all. Why: precise. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-01] How does the probe test the provider? — Picked: A — one 1-token completion; only HTTP 200 recovers. Alternatives: B — `GET /api/v1/key`; C — `GET /api/v1/credits`. Why: exercises the failing path. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-01] How does the resume find the paused PRs? — Picked: A — scan open PRs updated since the outage opened. Alternatives: B — per-PR records on the marker. Why: the serialized heal intake drops runs under load. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-10-01] Which `ai:review-blocked` labels does the resume remove? — Picked: A — on resumed PRs or issues they reference, latest `labeled` event inside the window by the workflow account. Alternatives: B — every label added during the window. Why: marker plus window, never a guess. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-10-01] How is an outage-caused hold released? — Picked: A — post a newer trusted `kind=review` claim when the hold is in the window, every failed check is an outage run, and the PR is not conflicted. Alternatives: B — delete the hold; C — leave it. Why: the documented lift; not counted toward the cap. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-10-01] Does the probe wake sessions or `/reclarify` issues? — Picked: A — no; sessions wait through their checkers. Alternatives: B — `/reclarify` blocked issues in the window; C — port #5660's wake path. Why: Actions cannot reach claude.ai; #5660 unmerged; B guesses. Applied in: no code change. Status: pending review
- AD-10 [plan, 2026-10-01] How are release runs handled? — Picked: A — signature opens or extends the marker; any failed release run is deferred while a marker is open; newest re-run only when `PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED=true`. Alternatives: B — only runs whose own logs show a signature. Why: #5758's run lacked the 402 text in its own logs. Applied in: phase 1 PR. Status: pending review
- AD-11 [plan, 2026-10-01] Are consumer repos covered? — Picked: A — yes. Alternatives: B — this repo only. Why: the outage is account-wide. Applied in: phase 1 PR. Status: pending review
- AD-12 [plan, 2026-10-01] How is the marker issue kept out of the pipelines? — Picked: A — clarify job `if:`s and `CODEX_ONLY_LABELS`. Alternatives: B — a title prefix check. Why: same as the security tracker. Applied in: phase 1 PR. Status: pending review
- AD-13 [plan, 2026-10-01] Alert channel and levels? — Picked: A — Telegram, `ERROR` on open, `WARNING` on recovery; per-PR alert suppressed. Alternatives: B — keep per-PR alerts. Why: one alert. Applied in: phase 1 PR. Status: pending review
- AD-14 [plan, 2026-10-01] Which switches? — Picked: A — `PROVIDER_OUTAGE_PROBE_ENABLED` (true), marker rides `WORKFLOW_HEAL_ENABLED`, `PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED` (false). Alternatives: B — separate marker switch. Why: fewest new variables. Applied in: phase 1 PR. Status: pending review
- AD-15 [plan, 2026-10-01] How does this phase change `.claude/**`? — Picked: A — twin-first per Q40. Alternatives: B — edit `.claude/**` unattended; C — drop it. Why: §28.C. Applied in: phase 1 PR. Status: pending review
- AD-16 [plan, 2026-10-01] What does the intake trust for an autofix report? — Picked: A — `failure_reason` plus the reporter's `provider_outage` evidence line. Alternatives: B — a signature in the job log. Why: the job log is not readable while the job runs. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] A whole Actions job log carries reviewer stderr thousands of lines before its end, so a log classifier must read far more than a 64 KB tail and must drop the echoed step script lines first. (files: scripts/workflow_failure_heal.py)

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-01)
- security_pass_skip.py: {"skip": false, "label": null, "reason": "no skip label"} → Security pass: run.
- Verification: 3860 tests in the 131 test files that reference a changed file pass; 11 fail: 8 twin-parity tests (expected until the twin sync), 2 clarify predicate pins (fixed in 60b26e2), and 1 environmental (`gawk` missing; also fails on main). The full suite exceeds the container's run time; tests/test_workflow_retro.py needs Python 3.12 (CI uses 3.12).
