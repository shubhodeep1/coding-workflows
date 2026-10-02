# Implement-Plan Log — Claude worker pool in GitHub Actions: replace the long-running relay sessions

- Plan: docs/plans/claude-actions-worker-pool-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-claude-actions-worker-pool   Final PR: (opening) draft
- Status: IN_PROGRESS
- Stage: phase 1/5
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-02
- Last note: project started; phase 1 in progress (session_01Gpi41BKskELLT7oHKT8xaN)

## Phases
1. [ ] Phase 1 — pool core and worker
   - `scripts/claude_pool.py` [new]: `accounts`, `normalize`, `probe-parse`, `choose`, `prompt`, `classify`, `run-name`
   - `.github/workflows/claude-pool-worker.yml` [new]: `workflow_call`, jobs `select` / `work` / `report`
   - `.github/ai/claude_pool.json` [new]: `dispatch_types: []` (pool off)
   - `shubhodeep1/claude-workers` `.github/workflows/claude-pool-worker.yml` [new]: wrapper PR with the `push`-to-`claude/**` smoke job
   - `tests/test_claude_pool.py` [new] + its own `ci.yml` step
   - README "Claude worker pool" section; agents.md contract section
   - `docs/plans/claude-multi-account-pool-plan.md` → `docs/completed/` with the superseded line
   - `changelog.d/<pr>-claude-pool-core.md`
   - Done: unit tests pass; the runner-repo smoke run succeeds on the wrapper PR's branch; nothing dispatches
2. [ ] Phase 2 — dispatcher
   - `scripts/claude_pool_dispatch.py` [new], `.github/workflows/claude-pool-dispatch.yml` [new]
   - `claude_issue_route.py queue-pending` pool filter (`--pool-config`, `pool` key)
   - `tests/test_claude_pool_dispatch.py` [new], route test updates; watchdog message
   - README / agents.md dispatcher section; changelog fragment
   - Done: unit tests (state machine, budget, fail-open); a live tick logs `pool=off`
3. [ ] Phase 3 — pool-mode waits — protected paths: `.claude/commands/implement-plan-claude.md`, `.claude/commands/implement-issue-claude.md`, `.claude/commands/fix-claude-pr.md` (twin-first)
   - Pool-mode sections in the three command twins
   - `QUEUE_PRODUCERS["stage"]`, the `"workflows"` tuple, `claude_stage.v1` build/parse
   - `scripts/claude_pool_sweep.py` [new] wait handling; `.github/workflows/claude-pool-sweep.yml` [new]
   - Tests (sweep, route, command text); changelog fragment
   - Done: tests; a live sweep with no waits logs `waits=0`
4. [ ] Phase 4 — §26 rewrite, fast catch-all, terminal notices — protected paths: `.claude/hooks/pr_check_in_reminder.py` (twin-first)
   - CLAUDE.md §26 A–D rewrite (headings kept), §25.B/C references
   - `pr_check_in_reminder.py` twin text; sweep catch-all + terminal notices
   - agents.md "Interactive post-push PR status check-in"; tests; changelog fragment
   - Done: tests; a merged `claude/*` PR gets exactly one Telegram message and one terminal marker across two ticks
5. [ ] Phase 5 — retire the relays — protected paths: `.claude/commands/claude-issue-pickup.md` (no twin; twin-sync hold), command twins
   - Pickup self-retirement on `retire_pickup: true`
   - Remove checker / hand-back / safety-net / zombie-cleanup / session-janitor text; janitor and stale-routine callers
   - README / agents.md / `docs/operations/master-session.md`; changelog fragment
   - Done: tests; nothing set live

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions

## Lessons

## Notes
- Protected-path phases (3, 4, 5) run twin-first under the interim automatic default (CLAUDE.md §28.C) unless a different `Protected-path approval:` answer is recorded here first.
