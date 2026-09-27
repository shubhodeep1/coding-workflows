# Implement-Plan Log — Bind Claude issue queue items to the run that queued them

- Plan: docs/plans/issue-4621-bind-claude-queue-payloads-plan.md
- Source issue: shubhodeep1/coding-workflows#4621
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4621-bind-claude-queue-payloads   Final PR: pending (draft)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-27
- Last note: project branch opened by /implement-issue-claude session session_01RMDgoU4wupVJb3BX4Zrekg

## Phases
1. [ ] Phase 1 — bind queue items to their producing run and verify the binding at pickup
   - [ ] `scripts/claude_issue_route.py`: binding constants, `append_queue_binding`, `load_queue_binding`, `evaluate_producer_run`, `fetch_queue_bindings`, binding check in `queue_pending`, CLI (`add-queue-binding`, `--bindings-json`, `--default-branch`)
   - [ ] `scripts/claude_issue_intake.sh` + `.github/workflows/claude-issue-intake.yml`: write the binding (new and reused items) and upload it
   - [ ] `scripts/claude_pr_sweep.py` + `.github/workflows/review_autofix_sweep.yml`: write the binding and upload it
   - [ ] `.claude/commands/claude-issue-pickup.md`, `README.md`, `agents.md`: document the binding
   - [ ] Tests in `tests/test_claude_issue_route.py`, `tests/test_claude_pr_sweep.py`; changelog fragment
   - Done when: the listed tests pass and both workflows parse and upload `claude-issue-queue-binding` with `if: always()`

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] How should a queue item be bound so the pickup can verify it? — Picked: A — an artifact uploaded by the producing default-branch workflow run, verified through REST at pickup. Alternatives: B — an HMAC signature keyed by a secret shared by Actions and the pickup's cloud environment; C — re-authorize at pickup from the target issue's labels. Why: A needs no new secret or operator step and cannot be forged from outside the producing run. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] What happens when the intake reuses an open queue item for the same target? — Picked: A — rewrite the item's body with the fresh payload and this run's URL, then bind it in this run's artifact. Alternatives: B — stop reusing and always open a new item; C — keep reuse as is. Why: keeps one item per target and lets `/reclarify` heal an unbound or edited item. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] How should items with no valid binding (legacy, failed upload, edited) be handled? — Picked: A — fail closed: list under `ignored`, never start, leave open for the watchdog. Alternatives: B — accept items opened before the deploy; C — close them automatically. Why: §1 security first. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] How should `workflow_dispatch` producer runs be trusted? — Picked: A — only when the run's `head_sha` is on the default branch (one compare call per such run). Alternatives: B — refuse `workflow_dispatch` runs; C — trust `head_branch` alone. Why: `head_branch` can name a tag that shadows the default branch; B breaks manual re-fires. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] Should the sweep's "already queued" dedupe check bindings? — Picked: A — no; keep counting every trusted, well-formed open item. Alternatives: B — count only bound items. Why: dedupe must err toward not queueing twice. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue pickup's dispatch trigger; permission mode auto.
