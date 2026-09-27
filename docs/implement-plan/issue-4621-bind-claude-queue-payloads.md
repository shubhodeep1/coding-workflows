# Implement-Plan Log — Bind Claude issue queue items to the run that queued them

- Plan: docs/plans/issue-4621-bind-claude-queue-payloads-plan.md
- Source issue: shubhodeep1/coding-workflows#4621
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4621-bind-claude-queue-payloads   Final PR: #4636 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4639
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01HuPy8SJxGNHdJhSC3PfGv4   safety net trig_01PD1JSiiSzka6r9kD6bnqBZ   hand-back trig_014MvEcueRPDob8em912x3vy
- Last updated: 2026-09-27
- Last note: review round 1 on PR #4639 handled by session_01JtjK3nAm8UkHUSJzhniBF5 (2 findings fixed, 1 rejected); waiting on the next review round or merge

## Phases
1. [ ] Phase 1 — bind queue items to their producing run and verify the binding at pickup   — PR #4639 open (waiting); review rounds: 1; interventions: 0
   - [x] `scripts/claude_issue_route.py`: binding constants, `append_queue_binding`, `load_queue_binding`, `evaluate_producer_run`, `fetch_queue_bindings`, binding check in `queue_pending`, CLI (`add-queue-binding`, `--bindings-json`, `--default-branch`)
   - [x] `scripts/claude_issue_intake.sh` + `.github/workflows/claude-issue-intake.yml`: write the binding (new and reused items) and upload it
   - [x] `scripts/claude_pr_sweep.py` + `.github/workflows/review_autofix_sweep.yml`: write the binding and upload it
   - [x] `.claude/commands/claude-issue-pickup.md`, `README.md`, `agents.md`: document the binding
   - [x] Tests in `tests/test_claude_issue_route.py`, `tests/test_claude_pr_sweep.py`; changelog fragment
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
- AD-6 [phase 1/1 — review round 1, 2026-09-27] Reviewers asked for the default-branch head check on `repository_dispatch` runs too, which GitHub always runs on the default branch. Apply it? — Picked: A — check the head commit of every producer run (`repository_dispatch`, `schedule`, `workflow_dispatch`) with one compare read against `refs/heads/<default>`. Alternatives: B — keep it for `workflow_dispatch` only, as AD-4 planned, and reject the finding; C — extend it to every event but keep comparing against the bare branch name. Why: §1 security first, at one REST read per completed producer run; the fully qualified ref also keeps a same-named tag out of the comparison, which the bare name did not. Widens AD-4 and the plan's API budget (plan line 71). Applied in: PR #4639. Status: pending review

## Lessons
- [source:plan-deviation] When a trust check binds a GitHub issue body to automation, compare the whole body against the producer's canonical rendering, not only the payload block, or text added around the payload still reaches the model that reads the body. (files: scripts/claude_issue_route.py)

- [source:intervention] A trust check on a GitHub Actions run must not rely on `head_branch`, which is only a name a tag can share: verify `head_sha` with `compare/<sha>...refs/heads/<default>` for every event, using the fully qualified ref so the comparison cannot resolve to a tag. (files: scripts/claude_issue_route.py)
- [source:intervention] In a `set -e` bash script, `$(jq ...)` used as a command argument is exempt from `set -e`, so a missing field is written as `null` or empty; read such values once into a checked variable before writing them anywhere. (files: scripts/claude_issue_intake.sh)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue pickup's dispatch trigger; permission mode auto.
- Phase 1 adds a whole-body canonical check beyond the plan's title+payload comparison (same `binding_mismatch` reason), so text added to a queue issue never reaches the pickup model.
- Review round 1 (PR #4639, head 2b634550c889, ledger 9e3c32a8…): fixed the producer head check (AD-6) and the intake's unchecked queue body; rejected the sweep-dedupe finding (counting unbound items is what stops the sweep queueing twice, AD-5).
- Local verification: 412 tests across every file referencing the changed files passed; the full suite was not completed locally (network-bound legacy tests exceed the 25-minute local timeout, and `tests/test_workflow_retro.py` needs Python 3.12 f-strings while the container has 3.11). CI runs the full suite.
