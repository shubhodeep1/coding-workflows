# Implement-Plan Log — Run the claude/* merge hold gate before ready labels and at the poller's ready-to-merge merges

- Plan: docs/plans/issue-5564-hold-gate-ready-labels-and-poller-merge-plan.md
- Source issue: shubhodeep1/coding-workflows#5564
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims
- Project branch: claude/implement-plan-issue-5564-hold-gate-ready-labels-and-poller-merge   Final PR: #5572 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5589
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01AAqS3J7LAS52a77brRnGm3   safety net (re-armed at end of review round 1)   hand-back (re-armed at end of review round 1)
- Last updated: 2026-09-30
- Last note: review round 1 on PR #5589: fixed the dead `.codex-workflow-src-main` gate fallback (consensus finding) and its docs/test; rejected 3 minor findings with reasons; waiting on round 2

## Phases
1. [ ] Phase 1 — hold gate before ready labels and at the poller's ready-to-merge merges (scripts/review_enable_auto_merge.sh, review_autofix.yml deterministic-skip-merge, scripts/orchestrate_poll_process.sh, orchestrate_poll.yml, tests, docs) — PR #5589 open (waiting); review rounds: 1; interventions: 0

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
- AD-8 [phase 1/1 — review round 1, 2026-09-30] The poller's `.codex-workflow-src-main` gate fallback (AD-4) is unreachable: `orchestrate_poll.yml` deletes that snapshot before the poller runs. How to fix? — Picked: A — drop the dead probe; the gate comes from `CLAUDE_MERGE_HOLD_GATE_SCRIPT`, else `.codex-workflow-src/scripts/` (as in `review_autofix.yml`), missing refuses. Alternatives: B — copy the main snapshot's gate and `check_in_status.py` to a new directory before the `rm -rf` and probe it; C — stop deleting `.codex-workflow-src-main`. Why: the poller script is staged from `.codex-workflow-src` first, so it always ships with the gate; B/C add workflow surface for a state that cannot arise (§5). Applied in: PR #5589 (review round 1). Status: pending review

## Lessons
- [source:plan-deviation] A gate on merge enablement must also guard every path that sets merge-authorization labels without merging (auto-merge disabled, e2e opt-outs), because the orchestrator poller merges the PR of any ai:ready-to-merge issue. (files: scripts/review_enable_auto_merge.sh, .github/workflows/review_autofix.yml, scripts/orchestrate_poll_process.sh)
- [source:intervention] A support-checkout fallback in a poller-side script must be checked against the workflow's staging step: `orchestrate_poll.yml` deletes `.codex-workflow-src-main` before the poller runs, so only `.codex-workflow-src` (or a staged copy) is reachable at runtime. (files: scripts/orchestrate_poll_process.sh, .github/workflows/orchestrate_poll.yml)

## Notes
- Security pass: skip (`security_pass_skip.py`: `ai:security: created and labelled by the issue automation`).
- Phase 1 has no protected paths (no `.claude/**` edit).
- Base branch `claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims` is the head of open draft PR #5323 (checked 2026-09-30).
- Phase 1 found a third label bypass the finding did not name: the helper's `e2e-smoke-test` exit also authorized labels without the gate. It is covered by the same change (the gate runs inside `reviewed_head_is_current_for_labels`, which both early exits use), within AD-1.
- Local verification env (2026-09-30): Python 3.11 container; pytest, pytest-xdist, yamllint, shellcheck-py installed locally; actionlint 1.7.12 (CI's pinned sha256) run on the two changed workflows.
