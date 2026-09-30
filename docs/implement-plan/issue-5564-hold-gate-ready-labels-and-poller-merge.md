# Implement-Plan Log — Run the claude/* merge hold gate before ready labels and at the poller's ready-to-merge merges

- Plan: docs/completed/issue-5564-hold-gate-ready-labels-and-poller-merge-plan.md (moved from docs/plans/ by the completion PR)
- Source issue: shubhodeep1/coding-workflows#5564
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims
- Project branch: claude/implement-plan-issue-5564-hold-gate-ready-labels-and-poller-merge   Final PR: #5572 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation (n/a if the base is still claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims when the final PR merges; Issue Mode)
- Waiting on: completion PR (claude/implement-plan-issue-5564-hold-gate-ready-labels-and-poller-merge-complete)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01AAqS3J7LAS52a77brRnGm3   safety net and hand-back re-armed at the end of the completion stage
- Last updated: 2026-09-30
- Last note: validation cycle 1 (run 36746066439) passed 10/10 against the project branch; conformance run 1 CONFORMANT; security skipped; completion PR moves the plan to docs/completed/

## Phases
1. [x] Phase 1 — hold gate before ready labels and at the poller's ready-to-merge merges (scripts/review_enable_auto_merge.sh, review_autofix.yml deterministic-skip-merge, scripts/orchestrate_poll_process.sh, orchestrate_poll.yml, tests, docs) — PR #5589 merged 2026-09-30 (c50c928, by hand per Q1: A); review rounds: 2; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT — no fixes (pre-validation)

## Security pass
- Skipped (ai:security: automation-produced issue; plan header `Security pass: skip`)

## Validation
- Cycle 1 — run 36746066439 2026-09-30 (target_ref: claude/implement-plan-issue-5564-hold-gate-ready-labels-and-poller-merge): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 295s)

## Completion
- Completion PR (branch claude/implement-plan-issue-5564-hold-gate-ready-labels-and-poller-merge-complete) open — doc moved to docs/completed/issue-5564-hold-gate-ready-labels-and-poller-merge-plan.md
- Final PR #5572 draft (base claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims)

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
- AD-9 [conformance 1/3, 2026-09-30] The log said Status: IN_PROGRESS with a live checker, but the project was blocked on Q1 (answered A, /reclarify) and nothing was pending. Stop as "already in progress" or resume? — Picked: A — resume at conformance 1/3. Alternatives: B — stop as already in progress. Why: the log lagged (the blocked stage had no PR in flight to carry it); no check-in or trigger was pending, so nothing would have continued the project. Applied in: no code change. Status: pending review

## Lessons
- [source:plan-deviation] A gate on merge enablement must also guard every path that sets merge-authorization labels without merging (auto-merge disabled, e2e opt-outs), because the orchestrator poller merges the PR of any ai:ready-to-merge issue. (files: scripts/review_enable_auto_merge.sh, .github/workflows/review_autofix.yml, scripts/orchestrate_poll_process.sh)
- [source:intervention] A support-checkout fallback in a poller-side script must be checked against the workflow's staging step: `orchestrate_poll.yml` deletes `.codex-workflow-src-main` before the poller runs, so only `.codex-workflow-src` (or a staged copy) is reachable at runtime. (files: scripts/orchestrate_poll_process.sh, .github/workflows/orchestrate_poll.yml)

## Notes
- Security pass: skip (`security_pass_skip.py`: `ai:security: created and labelled by the issue automation`).
- Phase 1 has no protected paths (no `.claude/**` edit).
- Base branch `claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims` is the head of open draft PR #5323 (checked 2026-09-30).
- Phase 1 found a third label bypass the finding did not name: the helper's `e2e-smoke-test` exit also authorized labels without the gate. It is covered by the same change (the gate runs inside `reviewed_head_is_current_for_labels`, which both early exits use), within AD-1.
- Q1 (blocker after phase 1's review rounds): answered A on the source issue; PR #5589 merged by hand (c50c928) and the project resumed via `/reclarify` at conformance 1/3 (AD-9).
- Local verification env (2026-09-30): Python 3.11 container; pytest, pytest-xdist, yamllint, shellcheck-py installed locally; actionlint 1.7.12 (CI's pinned sha256) run on the two changed workflows.
