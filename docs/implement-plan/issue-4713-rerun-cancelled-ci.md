# Implement-Plan Log — Automatically re-run a PR's cancelled CI once per head from the review sweep

- Plan: docs/plans/issue-4713-rerun-cancelled-ci-plan.md
- Source issue: shubhodeep1/coding-workflows#4713
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4713-rerun-cancelled-ci   Final PR: (pending) draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: project branch opened; implementing phase 1.

## Phases
1. [ ] Phase 1 — cancelled-CI re-run in the review sweep (`scripts/ci_cancelled_rerun.py`, `review_autofix_sweep.yml`, tests, docs, changelog)

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

## Notes
- Security pass: `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`, so `Security pass: run`.
