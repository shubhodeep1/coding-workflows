# Implement-Plan Log — Run the claude/* merge hold gate before ready labels and at the poller's ready-to-merge merges

- Plan: docs/plans/issue-5564-hold-gate-ready-labels-and-poller-merge-plan.md
- Source issue: shubhodeep1/coding-workflows#5564
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims
- Project branch: claude/implement-plan-issue-5564-hold-gate-ready-labels-and-poller-merge   Final PR: (pending)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened from the issue base; phase 1 starting

## Phases
1. [ ] Phase 1 — hold gate before ready labels and at the poller's ready-to-merge merges (scripts/review_enable_auto_merge.sh, review_autofix.yml deterministic-skip-merge, scripts/orchestrate_poll_process.sh, orchestrate_poll.yml, tests, docs)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which label paths get the gate when auto-merge is disabled? — Picked: A — both: `review_enable_auto_merge.sh` and the `deterministic-skip-merge` job. Alternatives: B — only the job line the finding cites. Why: the helper has the identical bypass. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Which poller merges get the gate? — Picked: A — the two ready-to-merge merges (current wave and prior-wave backward scan). Alternatives: B — every poller merge; C — only the current-wave merge. Why: both consume the ready label; the others act on judge/stall verdicts (#5316 AD-5), §5. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How is the poller merge bound to the gated head? — Picked: A — `--match-head-commit <gated head>` for `claude/*` heads only. Alternatives: B — every ready-to-merge merge; C — no binding. Why: head-bound gate without changing non-`claude/*` merges (§5). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Where does the poller find the gate? — Picked: A — `CLAUDE_MERGE_HOLD_GATE_SCRIPT`, else `.codex-workflow-src/scripts/`, else `.codex-workflow-src-main/scripts/`; missing refuses. Alternatives: B — stage it into `scripts/`. Why: the gate imports `.claude/scripts/check_in_status.py` relative to its checkout root. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Which log key does the poller use? — Picked: A — new `ORCH_MERGE_HOLD_GATE`, registered in `agents.md`. Alternatives: B — reuse `AUTOFIX_AUTO_MERGE_SKIPPED`. Why: `AUTOFIX_*` keys belong to the review workflow. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Which log keys does the disabled label path use? — Picked: A — the existing `AUTOFIX_AUTO_MERGE_SKIPPED` / `AUTOFIX_MERGE_HOLD_GATE` lines. Alternatives: B — a new key. Why: same gate, same verdict. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Which login besides the PR author counts in the poller? — Picked: A — `vars.CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` passed to the poll step. Alternatives: B — PR author only. Why: matches the review workflow (#5316 AD-7). Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Security pass: skip (`security_pass_skip.py`: `ai:security: created and labelled by the issue automation`).
- Phase 1 has no protected paths (no `.claude/**` edit).
- Base branch `claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims` is the head of open draft PR #5323 (checked 2026-09-30).
