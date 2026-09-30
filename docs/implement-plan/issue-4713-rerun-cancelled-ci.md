# Implement-Plan Log — Automatically re-run a PR's cancelled CI once per head from the review sweep

- Plan: docs/plans/issue-4713-rerun-cancelled-ci-plan.md
- Source issue: shubhodeep1/coding-workflows#4713
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4713-rerun-cancelled-ci   Final PR: #4721 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3
- Activation: not started
- Waiting on: PR #5576
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01CFHuujSGs9A5J62SiGwiCD   safety net (re-armed 2026-09-29)   hand-back trig_01TNRpUcu2Nc88bvGtPkBecL
- Last updated: 2026-09-30
- Last note: conformance 1/3 opened fix PR #5576 (pin `head_repo` in the sweep's PR snapshot test, correct the changelog note); phase PR #4722 merged by hand after the round-2 blocker (Q1: B).

## Phases
1. [x] Phase 1 — cancelled-CI re-run in the review sweep (`scripts/ci_cancelled_rerun.py`, `review_autofix_sweep.yml`, tests, docs, changelog)   — PR #4722 merged 2026-09-30 (3617db3, held merge by hand after Q1: B); review rounds: 2; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Implemented COMPLETE, Correctness CONCERNS) — fix PR #5576 (pre-security): `tests/test_ci_cancelled_rerun.py:257` pinned `head_sha` in the sweep's PR snapshot but not `head_repo`, which `scripts/ci_cancelled_rerun.py:162` needs for its same-repository filter (AD-4); the `changelog.d/4713-rerun-cancelled-ci.md` contributor note said the projection only "adds `head_sha`". #5576 review rounds: 1

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
- [source:intervention] Never put the literal skip-ai marker in a PR's title or body, not even quoted: `review_autofix.yml` greps both and skips the review (`skip_reason=skip_ai_marker`), so the PR sits unreviewed with no hand-off. (files: .github/workflows/review_autofix.yml)
- [source:intervention] A Claude-fixer PR whose review findings are all rejected cannot converge while `vars.CLAUDE_FIXER_VERDICT_BOT_LOGIN` is unset: the verdict must come from that bot, never from a collaborator or `GH_PAT`, so the PR needs a held merge by hand. (files: .github/workflows/review_autofix.yml)
- [source:plan-deviation] A wiring test for a snapshot projection must pin every field a downstream reader uses (`head_repo` as well as `head_sha`), or a merge from `main` that drops the field leaves the feature silently dead with a green suite. (files: tests/test_ci_cancelled_rerun.py, .github/workflows/review_autofix_sweep.yml)

## Notes
- 2026-09-29: PR #4722 stalled 24h. Its only review was the push-triggered `claude-branch-review` comment (run 36379062798), which carries no `ai:claude-fixer-handoff` marker; the pull_request run skipped the `review` job. `check_in_status.py` therefore reported `open` every hour. The safety-net session handled the finding directly as review round 1. Corrected 2026-09-30: the missing reviews came from the literal skip-ai marker in PR #4722's own body (`review_autofix.yml` `skip_reason=skip_ai_marker`), not from the `claude-branch-review` path; the marker was removed on 2026-09-30.
- 2026-09-30: BLOCKED after review round 2 of PR #4722 (head `c767c0b`). Both findings were rejected (response-body check already covered by `gh api`'s non-zero exit; the 60 s timeout is a NIT), but convergence needs the dedicated verdict bot and `CLAUDE_FIXER_VERDICT_BOT_LOGIN` is unset. Resolved the same day: Q1: B, the master session merged #4722 by hand into the project branch as `3617db3`, bound to head `c767c0b`, and the chain resumed at `conformance 1/3`.
- Live dry run (2026-09-28, read-only): 15 open non-draft PRs, 1 runs listing; would re-run the cancelled attempt-1 CI of #4704, #4693, #4611.
- The inventory gate (`tests/inventory_parity.py`) needs every new `scripts/` file in `docs/INVENTORY.md`.
- Security pass: `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`, so `Security pass: run`.
