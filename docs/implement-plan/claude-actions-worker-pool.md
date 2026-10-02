# Implement-Plan Log — Claude worker pool in GitHub Actions: replace the long-running relay sessions

- Plan: docs/plans/claude-actions-worker-pool-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-claude-actions-worker-pool   Final PR: #6097 draft
- Status: IN_PROGRESS
- Stage: phase 1/5
- Activation: not started
- Waiting on: PR #6100
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-02
- Last note: phase 1 PR #6100 opened (session_01Gpi41BKskELLT7oHKT8xaN); runner-repo wrapper PR shubhodeep1/claude-workers#1 opened (merge = activation gate 2); live smoke run 37015227644 green

## Phases
1. [ ] Phase 1 — pool core and worker   — PR #6100 open (waiting); review rounds: 0; interventions: 0; runner-repo wrapper PR shubhodeep1/claude-workers#1 (open; operator merges it after the project lands, §23.C)
   - `scripts/claude_pool.py` [new]: `accounts`, `normalize`, `probe-parse`, `choose`, `prompt`, `classify`, `run-name`
   - `.github/workflows/claude-pool-worker.yml` [new]: `workflow_call`, jobs `select` / `work` / `report`
   - `.github/ai/claude_pool.json` [new]: `dispatch_types: []` (pool off)
   - `shubhodeep1/claude-workers` `.github/workflows/claude-pool-worker.yml` [new]: wrapper PR with the `push`-to-`claude/**` smoke job
   - `tests/test_claude_pool.py` [new] + its own `ci.yml` step
   - README "Claude worker pool" section; agents.md contract section
   - `docs/plans/claude-multi-account-pool-plan.md` → `docs/completed/` with the superseded line
   - `changelog.d/6100-claude-pool-core.md`
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
- AD-1 [phase 1/5, 2026-10-02] How should phase 1's `prompt` handle `stage` items, whose payload parser (`parse_stage_text`) and resume-block read arrive in phase 3? — Picked: A — refuse `stage` with `PoolError` unless `claude_issue_route.parse_stage_text` exists, and take the resume block from a `--resume-block` file. Alternatives: B — define the `claude_stage.v1` format in phase 1; C — leave `stage` out of `prompt`. Why: the plan says code needing a later piece checks for it and does nothing; B would pre-empt phase 3's design. Applied in: PR #6100. Status: pending review
- AD-2 [phase 1/5, 2026-10-02] What does `choose` do with an account whose probe succeeded but reported no utilization? — Picked: A — use it only when no account has a known reading under the gate. Alternatives: B — skip it; C — rank it as 0%. Why: the account works, but its usage is unknown, so known-safe accounts go first (plan G3 test case "unknown"). Applied in: PR #6100. Status: pending review
- AD-3 [phase 1/5, 2026-10-02] Which outcome ends a run when no account is usable and none is gated? — Picked: A — `auth_failed` when only token failures, `crashed` when only other probe failures (`no_accounts` with no accounts, `all_gated` when any is gated). Alternatives: B — always `no_accounts`. Why: the dispatcher alerts on `auth_failed` and re-dispatches `crashed`, which matches what each case needs. Applied in: PR #6100. Status: pending review
- AD-4 [phase 1/5, 2026-10-02] Log masking does not cover artifacts, and a worker's shell commands inherit the token environment: protect the 14-day transcript artifact? — Picked: A — add `claude_pool.py redact`, which replaces the pool token (raw and normalised), `GH_PAT`, and the base64 `x-access-token:` form of each with `***` in every uploaded file. Alternatives: B — upload transcripts unredacted as the plan says. Why: §1 security first; the plan's G8 requires that no token leaks. Applied in: PR #6100. Status: pending review
- AD-5 [phase 1/5, 2026-10-02] Which coding-workflows ref do the worker's script checkouts use? — Picked: A — a `pool_ref` input on the reusable workflow, default `main`; the wrapper does not pass it. Alternatives: B — hard-code `main`. Why: keeps Q15 (`@main`) while allowing the phase 1 smoke run against the phase branch before merge. Applied in: PR #6100. Status: pending review
- AD-6 [phase 1/5, 2026-10-02] The wrapper calls `coding-workflows@main`, which has no worker workflow until this project merges; how is the smoke run verified now? — Picked: A — one wrapper commit pointed `uses:`/`pool_ref` at the phase 1 branch (smoke run 37015227644 green), the next pins `@main` with `[skip ci]`; the wrapper PR is merged by the operator after the project lands. Alternatives: B — leave the wrapper PR red until then; C — keep the phase-branch ref. Why: proves the done condition live without leaving a red run or a temporary ref on the PR's head. Applied in: shubhodeep1/claude-workers#1. Status: pending review
- AD-7 [phase 1/5, 2026-10-02] How is a worker timeout told apart from a crash? — Picked: A — the CLI runs under `timeout <timeout_minutes>m` inside the job (exit 124/137 → `timeout`), the job limit is that plus 10 minutes capped at 360, and a cancelled work job is also `timeout`. Alternatives: B — job-level `timeout-minutes` only. Why: the job is then still alive to upload the transcript and exit info. Applied in: PR #6100. Status: pending review
- AD-8 [phase 1/5, 2026-10-02] Which git identity do workers commit with? — Picked: A — `Claude <noreply@anthropic.com>`, the identity claude.ai cloud sessions use today. Alternatives: B — `github-actions[bot]`. Why: workers replace those sessions; the bot identity would misattribute commits pushed with `GH_PAT`. Applied in: PR #6100. Status: pending review
- AD-9 [phase 1/5, 2026-10-02] Is an error result without a usage reading, or no result with a `rejected` reading, a usage limit? — Picked: A — `usage_limit` when the last `rate_limit_info` is `rejected` or at ≥ 1.0, or the result text names a usage limit, whether or not a result event exists. Alternatives: B — only `is_error` results, as the plan's interim rule words it. Why: excluding a rejected account on re-dispatch is the safer failover (Q7: no real rejection seen yet). Applied in: PR #6100. Status: pending review

## Lessons
- [source:plan-deviation] A new `.github/workflows/*.yml` file changes the auto-generated repo tree in agents.md; run `make generate` in the same PR, or the CI `make generate-check` drift step fails. (files: agents.md, tools/repo_tree/update_repo_tree.py)

## Notes
- 2026-10-02: the operator's runner repo now holds pool secrets `CLAUDE_POOL_TOKEN_FUNTOKEN1` and `_FUNTOKEN2` (TEST1/TEST2 gone), and `GH_PAT` there is already the classic token: spike round 9 (run 37012676689) and the phase 1 smoke run read check runs on private consumers. Activation gate 1 looks done; the activation stage re-checks it.
- 2026-10-02: running the test suite locally with `GH_TOKEN` set makes some unrelated tests clone `ai-memory` over the network; phase checks ran with `GH_TOKEN`/`GITHUB_TOKEN` unset, on Python 3.12 (CI's version).
- Protected-path phases (3, 4, 5) run twin-first under the interim automatic default (CLAUDE.md §28.C) unless a different `Protected-path approval:` answer is recorded here first.
