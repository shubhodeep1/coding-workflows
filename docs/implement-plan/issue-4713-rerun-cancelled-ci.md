# Implement-Plan Log — Automatically re-run a PR's cancelled CI once per head from the review sweep

- Plan: docs/plans/issue-4713-rerun-cancelled-ci-plan.md
- Source issue: shubhodeep1/coding-workflows#4713
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4713-rerun-cancelled-ci   Final PR: #4721 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4722
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01CFHuujSGs9A5J62SiGwiCD   safety net (re-armed 2026-09-29)   hand-back trig_01TNRpUcu2Nc88bvGtPkBecL
- Last updated: 2026-09-29
- Last note: safety net fired after 24h; the claude-branch-review finding (LOG_PREFIX.name= registry) had no hand-off marker, so the checker never saw it. Fixed in review round 1.

## Phases
1. [ ] Phase 1 — cancelled-CI re-run in the review sweep (`scripts/ci_cancelled_rerun.py`, `review_autofix_sweep.yml`, tests, docs, changelog)   — PR #4722 open (waiting); review rounds: 1; interventions: 0

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [planning, 2026-09-28] Which sweep job hosts the re-run? — Picked: A — the 30-minute `sweep` job. Alternatives: B — the hourly `claude-pr-catch-all` job. Why: it already lists every open non-draft PR, has `actions: write`, and re-runs within 30 minutes. Applied in: phase 1. Status: pending review
- AD-2 [planning, 2026-09-28] Shape of the runs read? — Picked: A — one unfiltered `ci.yml/runs?per_page=100` listing. Alternatives: B — `status=completed` plus three active-status listings; C — `status=completed` only, without the active-run rule. Why: one call answers both rules and sees a queued re-run. Applied in: phase 1. Status: pending review
- AD-3 [planning, 2026-09-28] How does the re-run logic get the PR heads? — Picked: A — PR snapshot file from the enumerate step, read by `scripts/ci_cancelled_rerun.py`. Alternatives: B — inline bash; C — list PRs again. Why: no second PR listing (§15), unit-testable, keeps the workflow small (§27). Applied in: phase 1. Status: pending review
- AD-4 [planning, 2026-09-28] Fork PRs? — Picked: A — same-repository heads only. Alternatives: B — every PR. Why: §1. Applied in: phase 1. Status: pending review
- AD-5 [planning, 2026-09-28] Honour `[skip ai]`? — Picked: A — yes. Alternatives: B — ignore it. Why: the sweep's documented only opt-out. Applied in: phase 1. Status: pending review
- AD-6 [planning, 2026-09-28] `startup_failure` refused by `rerun-failed-jobs`? — Picked: A — fall back once to `POST …/rerun`. Alternatives: B — log and skip. Why: no jobs to preserve; still one re-run per head. Applied in: phase 1. Status: pending review
- AD-7 [planning, 2026-09-28] Consumer repos? — Picked: A — this repo only. Alternatives: B — registered consumers too. Why: the workflow is internal and consumer CI names are not known. Applied in: phase 1. Status: pending review
- AD-8 [planning, 2026-09-28] `dry_run` / `head_ref_filter` inputs? — Picked: A — honour both. Alternatives: B — ignore them. Why: operator scope. Applied in: phase 1. Status: pending review
- AD-9 [planning, 2026-09-28] Kill-switch parsing? — Picked: A — `1`/`true`/`yes`/`on` enable, anything else disables, unset defaults to `true`. Alternatives: B — only exact `false` disables. Why: matches `ALLOW_WORKFLOW_EDITS`; fails toward no re-run. Applied in: phase 1. Status: pending review

## Lessons
- [source:intervention] A new stable log prefix goes into both agents.md forms: the `- `PREFIX`` prose list and the `LOG_PREFIX.name=PREFIX` registry; contract tests assert both. (files: agents.md)

## Notes
- 2026-09-29: PR #4722 stalled 24h. Its only review was the push-triggered `claude-branch-review` comment (run 36379062798), which carries no `ai:claude-fixer-handoff` marker; the pull_request run skipped the `review` job. `check_in_status.py` therefore reported `open` every hour. The safety-net session handled the finding directly as review round 1.
- Live dry run (2026-09-28, read-only): 15 open non-draft PRs, 1 runs listing; would re-run the cancelled attempt-1 CI of #4704, #4693, #4611.
- The inventory gate (`tests/inventory_parity.py`) needs every new `scripts/` file in `docs/INVENTORY.md`.
- Security pass: `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`, so `Security pass: run`.
