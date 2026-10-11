# Claude pool capacity tiers and the remaining OpenRouter callers

## Summary

Add per-role capacity tiers to the Claude account pool (core 0.9, standard 0.8,
utility 0.7), so low-priority roles stop drawing on accounts that are nearly
full. Move the three OpenRouter callers that project #6664 does not cover onto
the pool: the memory learnings extractor, the release-gate soft-error analyzer
and the review interim judge.

## Context

Project #6664 (integration PR #6667, branch `orchestrator/project-6664`, head
`043f46ea5` when this plan was written) moves every codex-default pipeline role
to the Claude pool. On that branch:

- `.github/ai/claude_engine.json` sets `engine: "claude"` for all 30 roles in
  `scripts/claude_engine.py` `ROLES`.
- `scripts/ai_engine.sh` adds `claude_run_selected` and
  `AI_ENGINE_FALLBACK_POLICY=capacity`. In `ai_engine_fallback_class()`, only
  the reasons `all_gated` and `all_usage_limit` count as capacity: they fall
  back with exit 75 and run codex/OpenRouter. Every other reason is refused
  with exit 76 and logs `::error::AI_ENGINE_FALLBACK_REFUSED role= reason=`.

The Oct 7–9 cost analysis found OpenRouter callers that #6667 leaves untouched.
The user chose which ones move (answers Q24–Q34 in the planning session):

| Caller | File | Today | #6667 |
|---|---|---|---|
| Memory learnings extractor | `scripts/memory_maintenance_extract_learnings.py` (`request_extracted_learnings()`, model hard-coded `openai/gpt-6-luna`) | OpenRouter, monthly, fail-open | unchanged |
| Soft-error analyzer | `scripts/analyze_soft_errors.py` (`call_openrouter()`, `LOG_ANALYZER_MODEL` default `openai/gpt-6-luna`) via `.github/actions/run-soft-error-analyzer` | OpenRouter, 17 calls in 7 jobs of `test-and-mark-stable.yml`, non-blocking | unchanged |
| Interim judge | `scripts/review_run_judge_interim.sh`, step "Run interim judge" in `review_autofix.yml` | OpenCode on `MODEL_EDITOR` (`openai/gpt-6-sol`), reasoning `low`, gated by `JUDGE_INTERIM_ENABLED` (default `false`) | moved into the review sandbox, forced to `engine=codex` (`review_untrusted_sandbox.sh`: "The interim judge has no Claude engine role") |

Memory keyword extraction (`ai_memory_lib.py`, `gpt-6-luna`) and embeddings stay
on OpenRouter (answer Q24, option D not selected).

**How the pool gate works on #6667.** `scripts/claude_pool_token.sh` (run by
`.github/actions/claude-pool-token`) reads `gate_utilization` (0.9) once per
job.
- It probes each account with one Haiku call and calls `claude_engine.py
  choose --gate` (`choose_account()`). An account is gated when
  `status == "rejected"` or `max(five_hour, seven_day) >= gate`.
- It writes the usable accounts, least used first, to `${pool}/order`, and the
  reason to `$RUNNER_TEMP/claude-pool-reason`.
- `ai_engine.sh` `ai_engine_accounts` reads `order`. When the list is empty,
  `ai_engine_no_account_reason` returns `all_gated` or `no_credential`.
- No per-role threshold exists, and per-account utilization is not persisted
  past the action (it is only in the action's `probes` output).

Constraints that bind this plan:

- **CLAUDE.md §6.** No identifier is renamed. `gate_utilization`, `order`, the
  `claude-pool-reason` file, `LOG_ANALYZER_MODEL` / `LOG_ANALYZER_REASONING`
  and `JUDGE_INTERIM_*` keep their meaning.
- **§4.** Every new env var and config key has a default.
- **§18.** Everything runs from existing workflows; there are no manual
  scripts.
- **§20.** One changelog fragment per phase PR.
- **§27.** `review_autofix.yml` is 429,228 bytes on `main`, against the 480,000
  guard.

## Goals

- **G1.** Each pool role belongs to one tier: `core`, `standard` or `utility`.
  A role is offered only the accounts whose probed peak utilization is below
  its tier's gate. Defaults are core 0.9, standard 0.8, utility 0.7.
- **G2.** When every account is above a role's tier gate, the role falls back
  as a capacity fallback (`all_gated`, exit 75). It runs codex/OpenRouter, or
  skips for a skip-on-unavailable role such as `PANEL_REVIEWER`. Core roles
  keep the accounts between 0.7 and 0.9.
- **G3.** `MEMORY_LEARNINGS` (the extractor) runs on the pool, using Claude
  Sonnet 5.5 through the isolated `claude_run`: a read-only container with an
  empty workdir.
- **G4.** `SOFT_ERROR_ANALYZER` runs on the pool, using Claude Sonnet 5.5
  through the isolated `claude_run`, with a per-run input budget of 3,000,000
  characters on the Claude path.
- **G5.** `JUDGE_INTERIM` runs on the pool, using Claude Sonnet 5.5 at effort
  `low` in the review sandbox (`prepare-ephemeral claude`, read-only, like
  `PANEL_REVIEWER`).
- **G6.** For all three callers:
  - exit 75 still runs today's OpenRouter path;
  - exit 76 takes the caller's existing fail-open path and logs why;
  - a consumer that cannot mint an OIDC token keeps the OpenRouter path with
    one log line and no error.

Each goal is verified by the tests listed under "Tests".

## Non-goals

- Memory keyword extraction (`ai_memory_lib.py`) and embeddings stay on
  OpenRouter.
- Changing the broker (`tools/claude-pool-broker`), its `DEFAULT_GATE`, or the
  near-cap alert (`claude_pool_health_alert.sh` keeps reading the core gate).
- Re-gating accounts during a job. The gate stays a per-job snapshot taken
  when the pool is fetched, as today.
- Changing `AI_ENGINE_FALLBACK_POLICY` semantics, or adding new capacity
  reasons.
- Enabling the interim judge by default. `JUDGE_INTERIM_ENABLED` keeps its
  `false` default.
- Restructuring the release gate so the soft-error analysis runs in one job.

## Constraints

- **Precondition (answer Q27 A): dispatch only after #6667 merges into the
  default branch.** Every phase builds on `claude_run_selected`,
  `AI_ENGINE_FALLBACK_POLICY`, exit 76 and the 30-role `claude_engine.json`
  from #6667. The orchestrator must start each phase from a default branch
  that contains them.
  - Phase preflight: `grep -q '_AI_ENGINE_EXIT_REFUSED' scripts/ai_engine.sh`
    must succeed. If it fails, the phase stops and reports instead of
    reimplementing the plumbing.
- **§6 naming.**
  - New identifiers:
    - config key `tier_gates`;
    - per-role config field `tier`;
    - files `order.standard`, `order.utility`, `claude-pool-reason.standard`,
      `claude-pool-reason.utility`;
    - roles `MEMORY_LEARNINGS`, `SOFT_ERROR_ANALYZER` (and `JUDGE_INTERIM`,
      which already exists as a review-sandbox role name and gains an engine
      entry);
    - env vars `SOFT_ERROR_CLAUDE_CHAR_BUDGET`, `AI_ENGINE_RESOLVED_MEMORY_LEARNINGS`,
      `AI_ENGINE_RESOLVED_SOFT_ERROR_ANALYZER`, `AI_ENGINE_RESOLVED_JUDGE_INTERIM`;
    - report status `claude_refused`.
  - None of these exist on `main` or on the #6667 branch, except
    `JUDGE_INTERIM` as noted. `MEMORY_LEARNINGS` appears only as a prefix of
    `MEMORY_LEARNINGS_EXTRACT_ENABLED`, which stays unchanged.
- **§4 defaults.**
  - `tier_gates` defaults to `{core: 0.9, standard: 0.8, utility: 0.7}`.
  - `SOFT_ERROR_CLAUDE_CHAR_BUDGET` defaults to `3000000`.
  - A role without a tier is `core`.
- **§14 consumers.** Phases 1 and 4 change scripts that reach consumers through
  `@stable`. Phase 2 also changes the consumer wrapper template
  `workflow-templates/ai-memory-maintenance.yml`, which `update_workflows.yml`
  syncs to every repo in `.github/ai/consumer_repos.json`. Phase 3 is
  release-gate only (`test-and-mark-stable.yml` runs in this repository).
- **§15 GitHub API.** No phase adds a `gh api` or MCP call. The pool fetch
  goes to the broker, not GitHub.
- **§27 size.** Phase 4 adds fewer than 2 KB to `review_autofix.yml`. Check
  with `wc -c`; if the file crosses 480,000 bytes, move the step body to
  `scripts/review_autofix_step_<slug>.sh` per `agents.md` "Workflow file size
  limit".
- **Security.** The tool-less callers never run Claude on the host: they go
  through `claude_run` → `codex_isolated_exec.sh run --engine claude --mode
  read-only` on an empty workdir. The interim judge goes through
  `review_untrusted_sandbox.sh` with the read profile. The OAuth token stays in
  `claude_anthropic_relay.py` on the host.

## Approach

### Capacity tiers (answers Q26 A, Q28 A, Q29 A)

**Config.** Add `tier_gates` to `claude_engine.json` and `DEFAULT_CONFIG`.
- `normalize_config` validates each value as a number in (0, 1], and requires
  core ≥ standard ≥ utility; a config that breaks the order is rejected with a
  warning and the defaults are used.
- `gate_utilization` stays as the core value (§6). If `tier_gates.core` is
  absent, it is taken from `gate_utilization`. If both are set and differ,
  `gate_utilization` wins for core and a warning is logged, so an operator's
  existing override keeps working.

**Role → tier.** A table in `claude_engine.py` sets each role's default tier:

| Tier | Roles |
|---|---|
| core (0.9) | `CLARIFY`, `CLARIFY_RESPOND`, `PLAN`, `IMPLEMENT`, `IMPLEMENT_REPAIR`, `IMPLEMENT_DIAGNOSE`, `ORCHESTRATE`, `REVIEW_EDITOR`, `REVIEW_CONSOLIDATOR`, `CONFLICT_RESOLVER`, `RB_JUDGE`, `WAVE_JUDGE`, `STALL_JUDGE`, `INTEGRATION_JUDGE`, `SECURITY_JUDGE`, `SECURITY_AUDIT` |
| standard (0.8) | `VALIDATE`, `VALIDATE_SELF_HEAL`, `VALIDATION_REFRESH`, `CHECK_TRIAGE`, `WORKFLOW_HEAL`, `ACTIVATION_VERIFY`, `UNBLOCK_JUDGE`, `LOG_ANALYSIS`, `LOG_AUDIT` |
| utility (0.7) | `LOG_SUMMARY`, `RETRO`, `MATERIALITY`, `SUMMARISER`, `BEHAVIOURAL_SMOKE`, `PANEL_REVIEWER`, `MEMORY_LEARNINGS`, `SOFT_ERROR_ANALYZER`, `JUDGE_INTERIM` |

- The table may name roles that are not yet in `ROLES`, such as
  `PANEL_REVIEWER` (PR #7024) and the three new roles. Unknown names are inert
  strings, which keeps every phase independently mergeable.
- An optional `tier` field in a role's `role_defaults` overrides the table.
- A role in neither the table nor `role_defaults` is `core`, so a newly added
  role is never throttled by accident.
- `resolve_role` returns the tier with the role, as `tier=<name>`.

**Pool fetch.** After probing, `claude_pool_token.sh` calls `choose` once per
tier gate (a local computation, with no extra probes). It writes:
- `order` with the core list, exactly as today;
- `order.standard` and `order.utility`;
- one reason file per tier: `claude-pool-reason` (core, unchanged path),
  `claude-pool-reason.standard` and `claude-pool-reason.utility`.

Each lower tier's list is a subset of the core list, so the token files already
kept for core cover every tier. With `probe=false`, utilization is unknown and
all three lists equal the broker's order.

**Run time.**
- `ai_engine_accounts` takes the role's tier, from `_ai_engine_py resolve` or a
  `role_tier` helper, and reads `order.<tier>`. If that file is missing (an
  older pool action version), it falls back to `order`.
- `ai_engine_no_account_reason` reads the tier's reason file, falling back to
  `claude-pool-reason`. An empty tier list therefore yields `all_gated`, a
  capacity fallback (exit 75).
- One log line per role and job: `AI_ENGINE_TIER role= tier= gate= accounts=`.
  This is a new stable prefix for `agents.md`.

### Tool-less callers through the isolated `claude_run` (answers Q25 A, Q30 A, Q31 A, Q32 A, Q33 A)

`MEMORY_LEARNINGS` and `SOFT_ERROR_ANALYZER` are added to `ROLES` and
`READ_ROLES`. In `claude_engine.json` both are `engine: claude`, model
`claude-sonnet-5-5`, profile `read`.

Each Python caller gets a small engine switch. When
`AI_ENGINE_RESOLVED_<ROLE>=claude`:

1. Write the existing prompt to a temp file. The system and user messages are
   concatenated under fixed `## System` / `## Task` headings.
2. Create an empty temp workdir and run
   `bash -c 'source "$1/ai_engine.sh" && claude_run <ROLE> "$2" "$3" "$4"'`
   with a wall-clock timeout equal to the OpenRouter timeout: 120 s for the
   extractor, 300 s for the analyzer. Run it under
   `env -u CODEX_ISOLATED_ROOT -u CODEX_ISOLATED_MODE`, the same pattern as
   `heal_isolated_implement.sh` `run_editor_claude`.
3. Handle the exit status:

   | Exit | Meaning | Action |
   |---|---|---|
   | 0 | Success | Parse the output file exactly like the OpenRouter response body |
   | 75 | Capacity fallback | Run today's OpenRouter call unchanged |
   | 76 | Refused fallback | Fail-open path with a reason (see below) |
   | Anything else | Other failure, including a timeout | Same as 76 |

   The fail-open path is per caller:
   - the extractor raises `ModelExtractionFailure("claude_refused"|"claude_failed")`
     and exits 12, which the workflow already turns into a warning;
   - the analyzer writes a stub report with the new status `claude_refused` or
     the existing `call_failed`.

**Pool unavailable at setup.** The `claude-pool-token` action never fails; it
reports `available=false` with a reason. The step that resolves the engine
reads the action's outputs.
- If the reason is `oidc_unavailable` or `broker_not_configured`, the role
  resolves to `codex`, with one line:
  `AI_ENGINE_SELECTED role=<ROLE> engine=codex reason=<reason>`.
  The caller then runs OpenRouter as today. This is not a refusal; it covers a
  consumer whose wrapper has not synced `id-token: write` yet.
- Any other unavailable reason (`all_gated`, `auth_failed`, …) leaves the role
  on `claude`. `claude_run` then applies the normal capacity policy:
  `all_gated` falls back to OpenRouter, anything else is refused and takes the
  fail-open path.

**Soft-error input budget (Q32 A).** On the Claude path only,
`analyze_soft_errors.py` sets the per-run budget (today `PER_RUN_CHAR_BUDGET =
None`) to `SOFT_ERROR_CLAUDE_CHAR_BUDGET` (default 3,000,000 characters, about
750K tokens).
- This activates the existing head/tail truncation with its
  `…[truncated for context window]…` marker.
- The OpenRouter path keeps `None`.
- Claude Sonnet 5.5 has a 1M-token context window, so 3,000,000 characters
  leaves room for the instructions and the answer.

**Report header.** The analyzer keeps the format `## Soft-error analyzer
(status: \`X\`, model: ..., reasoning: ...)`. The Claude path writes
`model: claude-sonnet-5-5 (claude pool)` and `reasoning: <effort>`.
`scripts/consolidate_soft_error_reports.py` and the action's header parser must
accept the new status `claude_refused`.

### Interim judge in the review sandbox (Q33 A)

`JUDGE_INTERIM` is added to `ROLES` and `READ_ROLES`, and to
`claude_engine.json` with `engine: claude`, model `claude-sonnet-5-5`, profile
`read`.

`review_untrusted_sandbox.sh` allows `JUDGE_INTERIM` on the `claude` engine
with the `read` access only. This replaces the forced `engine=codex` check
(INT lines ~310–312); the read-only rule and the "never transfer" rule stay.

`review_run_judge_interim.sh` reads `AI_ENGINE_RESOLVED_JUDGE_INTERIM`, which
review_autofix.yml's "Resolve AI engine" step adds the same way it resolves the
other review roles. When it is `claude`:

1. `prepare-ephemeral claude`.
2. `run <prompt> <output> claude-sonnet-5-5 low /dev/null claude JUDGE_INTERIM read`.
3. Validate the output with the existing `extract_and_validate_judge_interim_json`.

| Exit | Action |
|---|---|
| 75 | Run today's OpenCode path in a fresh `prepare-ephemeral codex` sandbox |
| 76, or any other failure | `judge_interim_log_fail` with reason `claude_refused` or `claude_failed`, then exit 0, the same fail-open behaviour as today |

The `JUDGE_INTERIM_TIMEOUT_S` default (120 s) applies to the Claude run too.

### Alternatives considered

- **Persist per-account utilization (`probes.json`) and filter at run time**
  (Q28 B): more flexible, but `ai_engine.sh` would have to parse JSON per role,
  and the gate would be applied in two places. Rejected for per-tier order
  files.
- **A per-role `gate` number** (Q29 B): no named tiers, so 39 numbers to keep
  consistent. Rejected.
- **Always fall back to OpenRouter for these callers** (Q30 B): this would hide
  non-capacity pool failures behind spend, which is what the capacity policy
  exists to prevent. Rejected.

## Decisions

### D1 — Which OpenRouter callers move to the pool

- **Chosen:** memory learnings, the soft-error analyzer and the interim judge
  (Q24 A+B+C).
- **Alternatives considered:** also memory keyword extraction (Q24 D).
- **Why:** keyword extraction costs cents a day and sits on the
  memory-retrieval path of most workflows; a pool run would add 20–40 s of
  container start-up to each.

### D2 — How the tool-less callers call Claude

- **Chosen:** the existing isolated `claude_run`, in a read-only container
  with an empty workdir (Q25 A).
- **Alternatives considered:** a lighter host-side call through the relay,
  with no container (Q25 B).
- **Why:** it has the same isolation as every other role and adds no new,
  less-isolated path.

### D3 — Capacity tiers

- **Chosen:** core 0.9 / standard 0.8 / utility 0.7, with the role mapping in
  the Approach section (Q26 A).
- **Alternatives considered:** keep the single 0.9 gate (Q26 B).
- **Why:** core roles keep the pool when it is busy, while utility work yields
  first.

### D4 — Where tiers are applied

- **Chosen:** one account list and one reason file per tier, written when the
  pool is fetched (Q28 A).
- **Alternatives considered:** persist `probes.json` and filter at run time
  (Q28 B).
- **Why:** it is a single gate computation in one place, and older readers
  fall back to `order`.

### D5 — Where tier config lives

- **Chosen:** `tier_gates` in `claude_engine.json`, a role → tier table in
  code, an optional per-role `tier` override, and unknown roles as core
  (Q29 A).
- **Alternatives considered:** a per-role `gate` number (Q29 B).
- **Why:** three named numbers instead of 39, and `gate_utilization` stays
  valid (§6).

### D6 — Refused fallback (exit 76) in the new callers

- **Chosen:** respect the policy and take each caller's fail-open path, with a
  logged reason (Q30 A).
- **Alternatives considered:** always fall back to OpenRouter (Q30 B).
- **Why:** it keeps non-capacity pool failures visible, as the capacity policy
  intends.

### D7 — Consumers without `id-token: write`

- **Chosen:** add it to the memory-maintenance wrapper template. Until it
  syncs, `oidc_unavailable` / `broker_not_configured` resolve the role to
  codex (OpenRouter) with one log line (Q31 A).
- **Alternatives considered:** enable the pool for this repository only
  (Q31 B).
- **Why:** consumers get the pool on the next sync and nothing breaks before
  it.

### D8 — Soft-error input size on Claude

- **Chosen:** a 3,000,000-character per-run budget on the Claude path only
  (Q32 A).
- **Alternatives considered:** no truncation; let an oversized prompt fail
  (Q32 B).
- **Why:** Sonnet 5.5's window is 1M tokens, and the existing head/tail
  truncation keeps the run useful.

### D9 — Models

- **Chosen:** Claude Sonnet 5.5 for all three roles; the interim judge at
  effort `low` (Q33 A).
- **Alternatives considered:** Opus 5.5 for the interim judge (Q33 B); Opus
  5.5 for the soft-error analyzer (Q33 C).
- **Why:** all three are utility-tier, advisory or non-blocking work.

### D10 — Phasing

- **Chosen:** four independently mergeable phases (Q34 A).
- **Alternatives considered:** tiers, then all three callers in one phase
  (Q34 B).
- **Why:** each caller can be reverted alone, and no phase depends on another.

### D11 — Ordering against #6667

- **Chosen:** dispatch after #6667 merges (Q27 A).
- **Alternatives considered:** make the plan independent of #6667 (Q27 B).
- **Why:** the alternative would duplicate `claude_run_selected` and the
  fallback-policy plumbing.

## Phases & Merge Strategy

There are four phases, one PR each (answer Q34 A). Each phase:
- needs #6667 on the default branch, the shared precondition above;
- needs no other phase of this plan;
- is production-safe on merge, because every new behaviour degrades to today's
  behaviour when a piece is missing.

1. **Phase 1: capacity tiers.**
   - **Scope:** `tier_gates`, the role → tier table, per-tier order and reason
     files, tier-aware account selection, and the `AI_ENGINE_TIER` log line.
   - **Files:** `scripts/claude_engine.py`, `.github/ai/claude_engine.json`,
     `scripts/claude_pool_token.sh`, `scripts/ai_engine.sh`, README, agents.md,
     tests, changelog fragment.
   - **Done when:**
     - with probes showing peaks of 0.75 and 0.85, a core role gets both
       accounts, a standard role gets one, and a utility role gets none and
       falls back with `all_gated` (exit 75);
     - with `order.<tier>` missing, every role uses `order`;
     - the existing pool, engine and health-alert tests pass unchanged.
   - **Rollback:** revert the PR. Older readers ignore the extra files, and
     `gate_utilization` was never changed.
2. **Phase 2: `MEMORY_LEARNINGS` on the pool.**
   - **Scope:** the role entry, the extractor's engine switch, and pool setup
     in `memory_maintenance.yml`.
     - Add a trusted workflow-source checkout step (the
       `.codex-workflow-src` pattern clarify.yml uses), so `ai_engine.sh` and
       the sandbox support files come from the workflow's own ref and not the
       consumer checkout.
     - Add `install-claude` and `claude-pool-token`, and resolve the engine.
   - **Wrappers:** add `id-token: write` to
     `.github/workflows/internal-memory-maintenance.yml` and
     `workflow-templates/ai-memory-maintenance.yml`.
   - **Done when:**
     - with a fake `claude_run`, exit 0 produces learnings, exit 75 calls the
       OpenRouter stub, exit 76 exits 12;
     - a run with no OIDC permission logs `reason=oidc_unavailable` and calls
       OpenRouter;
     - the workflow still starts for a caller that grants only
       `contents: write`.
   - **Rollback:** revert the PR. The wrapper permission is harmless if left
     in place.
3. **Phase 3: `SOFT_ERROR_ANALYZER` on the pool.**
   - **Scope:** the role entry, the analyzer's engine switch and Claude-path
     budget, the composite action, the consolidator status, and the 7
     `test-and-mark-stable.yml` jobs that call the action.
     - The composite action gets new inputs `engine` (default `codex`) and
       `support_dir`, and passes `AI_ENGINE_RESOLVED_SOFT_ERROR_ANALYZER`.
     - Each of those 7 jobs (`e2e-smoke-test`, `e2e-alt-model-test`,
       `orphan-workflows-test`, `orchestrate-decompose-test`,
       `validate-standalone-test`, `clarify-rejects-unsolvable-test`,
       `workflow-log-analysis-test`) gets `id-token: write`, one
       `install-claude` step and one `claude-pool-token` step before its first
       analyzer call, and passes `engine: claude` when the pool is available.
   - **Done when:**
     - the analyzer tests cover exit 0, 75 and 76 with a fake `claude_run`;
     - Claude-path truncation honours `SOFT_ERROR_CLAUDE_CHAR_BUDGET`, and the
       OpenRouter path is untruncated;
     - the consolidator accepts `claude_refused`;
     - a workflow contract test asserts each analyzer-calling job has the pool
       steps.
   - **Rollback:** revert the PR. The action's `engine` default is `codex`,
     so a partial revert of the workflow alone also restores today's
     behaviour.
4. **Phase 4: `JUDGE_INTERIM` on the pool.**
   - **Scope:** the role entry, the sandbox's claude-engine allowance for
     `JUDGE_INTERIM` (read only), `review_run_judge_interim.sh` engine
     handling, and the "Resolve AI engine" addition in `review_autofix.yml`.
   - **Done when:**
     - with a fake sandbox, Claude output is validated and written as
       `judge_interim.json`;
     - exit 75 runs the OpenCode path;
     - exit 76 logs `JUDGE_INTERIM_PASS_FAIL ... reason=claude_refused` and
       exits 0;
     - `JUDGE_INTERIM` with `write` access is refused by the sandbox;
     - `review_autofix.yml` stays under 480,000 bytes.
   - **Rollback:** revert the PR. Without the resolved env var the script runs
     OpenCode as today.

## Implementation Steps

### Phase 1: capacity tiers

1. `scripts/claude_engine.py`:
   - Add `DEFAULT_TIER_GATES = {"core": 0.9, "standard": 0.8, "utility": 0.7}`
     and the `ROLE_TIERS` table from the Approach section. Add `tier_gates` to
     `DEFAULT_CONFIG` (lines 126–138).
   - Validate `tier_gates` and the optional per-role `tier` field in
     `normalize_config` (lines 223–287), including the
     `gate_utilization`-wins rule and the monotonic check.
   - Add `role_tier(config, role)` and include `tier` in `resolve_role`'s
     output (lines 369–431).
   - Add a `tier-gates` CLI command that prints `core=<g> standard=<g>
     utility=<g>`, plus `--tier` to the `resolve` output.
2. `.github/ai/claude_engine.json`: add `"tier_gates": {"core": 0.9,
   "standard": 0.8, "utility": 0.7}` next to `gate_utilization` (line 5).
3. `scripts/claude_pool_token.sh`:
   - After the probes (lines 210–233), run `engine_py choose --gate <g>` once
     for each tier gate.
   - The core result drives the existing `finish` and `write_order` (lines
     72–97, 196–202) unchanged.
   - Write `order.standard` / `order.utility` and the two extra reason files
     (`claude-pool-reason.<tier>`, next to the existing one or under
     `CLAUDE_POOL_REASON_FILE.<tier>`). A tier whose `choose` result is not
     `selected` gets an empty order file and its reason.
4. `scripts/ai_engine.sh`:
   - `ai_engine_accounts` (lines 275–289) takes an optional tier, reads
     `order.<tier>`, and falls back to `order`.
   - `ai_engine_no_account_reason` (lines 203–218) reads the tier reason file
     first.
   - `claude_run` (lines 350–575) resolves the role's tier once and passes it
     on, then logs `AI_ENGINE_TIER role= tier= gate= accounts=`.
   - The review sandbox's Claude branch (`review_untrusted_sandbox.sh`) uses
     the same helper for its account list.
5. Docs:
   - README "Claude engine": a component-table row for tiers, and the
     role → tier table in "Which engine a role uses".
   - agents.md: the fallback-policy notes, the models table "Engine · Claude
     role" column, and the `AI_ENGINE_TIER` stable prefix.
6. Tests (see Tests), then the `ci.yml` steps.
7. `changelog.d/<PR>-claude-pool-capacity-tiers.md`.

### Phase 2: memory learnings

1. `scripts/claude_engine.py`: add `MEMORY_LEARNINGS` to `ROLES` and
   `READ_ROLES`. `.github/ai/claude_engine.json`: an entry with `engine:
   claude`, `claude_model: claude-sonnet-5-5`, `profile: read`.
2. `scripts/memory_maintenance_extract_learnings.py`:
   - Add `request_extracted_learnings_claude(prompt, support_dir)` next to
     `request_extracted_learnings()` (lines 244–298), implementing the exit
     table in the Approach section.
   - The existing `normalize_model_output()` parses the output file.
   - `main()` (lines 336–359) dispatches on
     `AI_ENGINE_RESOLVED_MEMORY_LEARNINGS` and keeps exit codes 10/11/12.
   - The usage line keeps `phase=memory-maintenance`, with `engine=claude`
     added.
3. `.github/workflows/memory_maintenance.yml`:
   - Before "Extract repository learnings (fail-open)", add the trusted
     workflow-source checkout, `.github/actions/install-claude`,
     `.github/actions/claude-pool-token` (config from the trusted checkout),
     and a resolve step that implements the `oidc_unavailable` /
     `broker_not_configured` → codex rule.
   - Pass `AI_ENGINE_RESOLVED_MEMORY_LEARNINGS` and the support dir to the
     step.
   - The `OPENROUTER_API_KEY` early exit (lines 57–60) applies only when the
     engine is `codex`.
   - Do **not** add a job-level `permissions:` block. A reusable job that
     requests more than its caller grants fails to start.
4. `.github/workflows/internal-memory-maintenance.yml` and
   `workflow-templates/ai-memory-maintenance.yml`: add `id-token: write` under
   `permissions`.
5. Docs: a README row for the role and `MEMORY_LEARNINGS_EXTRACT_ENABLED`'s
   engine note; agents.md models table.
6. Tests, the `ci.yml` step, and `changelog.d/<PR>-memory-learnings-claude-pool.md`.

### Phase 3: soft-error analyzer

1. `scripts/claude_engine.py` / `claude_engine.json`: `SOFT_ERROR_ANALYZER`
   (`ROLES`, `READ_ROLES`; Sonnet 5.5, read).
2. `scripts/analyze_soft_errors.py`:
   - Add `call_claude(messages, support_dir, timeout)` next to
     `call_openrouter()` (lines 304–360), with the exit table from the
     Approach section.
   - Apply `SOFT_ERROR_CLAUDE_CHAR_BUDGET` (default 3,000,000) as the per-run
     budget on the Claude path only, through the existing truncation (lines
     213–265).
   - Add status `claude_refused` to the stub writer (lines 440–456).
   - The header names `claude-sonnet-5-5 (claude pool)` and the effort.
3. `.github/actions/run-soft-error-analyzer/action.yml`:
   - New inputs `engine` (default `codex`) and `support_dir` (default empty).
   - Export `AI_ENGINE_RESOLVED_SOFT_ERROR_ANALYZER` and the support dir.
   - The header parser (lines 155–157) accepts `claude_refused`.
4. `scripts/consolidate_soft_error_reports.py`: treat `claude_refused` as a
   non-`ok` status in its counts, like `call_failed`.
5. `.github/workflows/test-and-mark-stable.yml`, in each of the 7 jobs:
   - job-level `permissions` gains `id-token: write`;
   - one `install-claude` step and one `claude-pool-token` step (with the
     `id`) before the first analyzer call;
   - each analyzer call passes `engine: ${{ steps.<pool>.outputs.available
     == 'true' && 'claude' || 'codex' }}` and `support_dir: scripts`.
   - Before the pool steps, verify that `oidc_unavailable` /
     `broker_not_configured` already produce `available=false`, and therefore
     `codex`.
6. Docs:
   - README `LOG_ANALYZER_MODEL` row: the Claude path and
     `SOFT_ERROR_CLAUDE_CHAR_BUDGET`;
   - the README env table;
   - agents.md models table.
7. Tests, the `ci.yml` step, and `changelog.d/<PR>-soft-error-analyzer-claude-pool.md`.

### Phase 4: interim judge

1. `scripts/claude_engine.py` / `claude_engine.json`: `JUDGE_INTERIM`
   (`ROLES`, `READ_ROLES`; Sonnet 5.5, read).
2. `scripts/review_untrusted_sandbox.sh`:
   - Allow `JUDGE_INTERIM` with engine `claude` and access `read`. The check
     at INT lines ~296–312 keeps refusing `write`.
   - Remove the "has no Claude engine role" comment, which is now stale.
3. `scripts/review_run_judge_interim.sh` (INT lines 316–357): add the
   Claude branch.
   - It is selected by `AI_ENGINE_RESOLVED_JUDGE_INTERIM`; effort comes from
     `JUDGE_INTERIM_REASONING`.
   - Exit 75 re-enters the existing codex branch.
   - 76 or another failure calls `judge_interim_log_fail` with
     `claude_refused` / `claude_failed`.
   - Logs keep the `REVIEW_UTILITY_ISOLATION role=JUDGE_INTERIM engine=<engine>` form.
4. `.github/workflows/review_autofix.yml`: in "Resolve AI engine" (around
   lines 3229–3252), resolve `JUDGE_INTERIM` and export
   `AI_ENGINE_RESOLVED_JUDGE_INTERIM` to the "Run interim judge" step (line
   ~4734). Check `wc -c` against 480,000.
5. Docs: README `JUDGE_INTERIM_*` rows; agents.md models table and the
   sandbox role list.
6. Tests, the `ci.yml` step, and `changelog.d/<PR>-interim-judge-claude-pool.md`.

## Files & Modules

- Phase 1:
  - `scripts/claude_engine.py`
  - `.github/ai/claude_engine.json`
  - `scripts/claude_pool_token.sh`
  - `scripts/ai_engine.sh`
  - `scripts/review_untrusted_sandbox.sh` (account list only)
  - `README.md`, `agents.md`
  - `.github/workflows/ci.yml`
  - `tests/test_claude_pool_tiers.py` [new], `tests/test_claude_engine.py`, `tests/test_ai_engine.py`, `tests/test_claude_pool_token.py`
  - `changelog.d/<PR>-claude-pool-capacity-tiers.md` [new]
- Phase 2:
  - `scripts/claude_engine.py`, `.github/ai/claude_engine.json`
  - `scripts/memory_maintenance_extract_learnings.py`
  - `.github/workflows/memory_maintenance.yml`
  - `.github/workflows/internal-memory-maintenance.yml`
  - `workflow-templates/ai-memory-maintenance.yml`
  - `README.md`, `agents.md`
  - `.github/workflows/ci.yml`
  - `tests/test_memory_learnings_claude_engine.py` [new], `tests/test_memory_maintenance_contract_noop.py`
  - `changelog.d/<PR>-memory-learnings-claude-pool.md` [new]
- Phase 3:
  - `scripts/claude_engine.py`, `.github/ai/claude_engine.json`
  - `scripts/analyze_soft_errors.py`
  - `.github/actions/run-soft-error-analyzer/action.yml`
  - `scripts/consolidate_soft_error_reports.py`
  - `.github/workflows/test-and-mark-stable.yml`
  - `README.md`, `agents.md`
  - `.github/workflows/ci.yml`
  - `tests/test_soft_error_analyzer_claude_engine.py` [new], `tests/test_consolidate_soft_error_reports.py`
  - `changelog.d/<PR>-soft-error-analyzer-claude-pool.md` [new]
- Phase 4:
  - `scripts/claude_engine.py`, `.github/ai/claude_engine.json`
  - `scripts/review_untrusted_sandbox.sh`
  - `scripts/review_run_judge_interim.sh`
  - `.github/workflows/review_autofix.yml`
  - `README.md`, `agents.md`
  - `.github/workflows/ci.yml`
  - `tests/test_review_judge_interim_claude_engine.py` [new], `tests/test_review_sandbox_claude_roles.py`, `tests/test_review_judge_interim_round_trip.py`
  - `changelog.d/<PR>-interim-judge-claude-pool.md` [new]

Phases 2–4 each add a role to `ROLES`/`READ_ROLES` and `claude_engine.json`.
These are additive edits to the same lists; whichever phase merges second
resolves a trivial textual conflict.

## Data Model / Index Changes

None. No MongoDB collection is touched (§10 does not apply).

## Tests

All new tests are unit or contract tests with fakes, and each gets its own
`ci.yml` step.

- **Phase 1, `tests/test_claude_pool_tiers.py`:**
  - `tier_gates` defaults, validation, the non-monotonic rejection, and
    `gate_utilization` precedence;
  - role → tier mapping, the `role_defaults.tier` override, and unknown roles
    defaulting to core;
  - `claude_pool_token.sh` with fake probes (peaks 0.65 / 0.75 / 0.85 / 0.95)
    writes `order` = {a,b,c}, `order.standard` = {a,b}, `order.utility` = {a},
    with the reason files;
  - `probe=false` writes three equal lists;
  - `claude_run` for a utility role with an empty `order.utility` returns 75
    with `reason=all_gated` while a core role still runs;
  - a missing `order.utility` falls back to `order`.

  The existing `tests/test_claude_pool_token.py`, `tests/test_ai_engine.py`,
  `tests/test_claude_engine.py` and `tests/test_claude_pool_health_alert.py`
  must pass unchanged, apart from new assertions.
- **Phase 2, `tests/test_memory_learnings_claude_engine.py`:**
  - with a fake `ai_engine.sh`, exit 0 parses JSON learnings, 75 calls a fake
    OpenRouter server, 76 and 1 exit 12, and a timeout exits 12;
  - the workflow contract: the trusted checkout precedes the pool steps, there
    is no job-level `permissions` in `memory_maintenance.yml`, both wrappers
    carry `id-token: write`, and the `oidc_unavailable` → codex resolution is
    present.
- **Phase 3, `tests/test_soft_error_analyzer_claude_engine.py`:**
  - exits 0/75/76;
  - Claude-path truncation at `SOFT_ERROR_CLAUDE_CHAR_BUDGET` with a small
    override, and the OpenRouter path untouched;
  - the header format, and the consolidator counting `claude_refused`;
  - a contract test that every `test-and-mark-stable.yml` job calling the
    action has `id-token: write`, the pool steps before the first call, and
    the `engine` expression.
- **Phase 4, `tests/test_review_judge_interim_claude_engine.py`:**
  - with a fake sandbox, the Claude JSON is validated, 75 runs the OpenCode
    branch, 76 logs `claude_refused` and exits 0;
  - the sandbox refuses `JUDGE_INTERIM` `write`;
  - `review_autofix.yml` passes the resolved env;
  - `tests/test_workflow_file_size_limit.py` passes.
- **End to end:**
  - After Phase 1 merges, the next jobs show `AI_ENGINE_TIER` lines.
  - After Phase 3, the next release-gate run (`test-and-mark-stable.yml`)
    shows `soft-error-report-*` artifacts whose header names `claude pool`.
  - After Phase 2, the next monthly memory maintenance run (or a
    `workflow_dispatch` of `internal-memory-maintenance.yml`) shows
    `AI_ENGINE_SELECTED role=MEMORY_LEARNINGS engine=claude`.
  - Phase 4 is only exercised in a repo with `JUDGE_INTERIM_ENABLED=true`.

## Risks & Mitigations

- **Utility roles fall back more often**, sending more PANEL_REVIEWER skips
  and OpenRouter calls to the soft-error analyzer and learnings when the pool
  is busy. ACCEPTED: this is the purpose of the tiers (Q26 A). The
  `AI_ENGINE_TIER` and `AI_ENGINE_FALLBACK reason=all_gated` lines make it
  measurable, and `tier_gates.utility` can be raised in config without a code
  change.
- **The gate is a per-job snapshot**, so a long job keeps using accounts that
  crossed a tier gate mid-job. ACCEPTED: the same as today's global gate;
  usage-limit handling still rotates accounts.
- **The soft-error analyzer adds a pool fetch to 7 release-gate jobs**, with
  one Haiku probe per account per job and an image pull. Mitigation: a single
  fetch per job, not per call. With the pull-first images from PR #7026 the
  sandbox setup is a pull, not a build. The pool action never fails a job, and
  the analyzer is non-blocking.
- **Truncating soft-error input on the Claude path could hide errors in the
  middle of a very large log.** Mitigation: truncation only applies above
  3,000,000 characters per run, keeps the head and tail, and is marked in the
  report.
- **A consumer whose wrapper lacks `id-token: write`** cannot use the pool for
  memory maintenance until `update_workflows.yml` syncs it. Mitigation: it
  keeps OpenRouter with one log line (Q31 A); nothing fails.
- **Model quality on Sonnet 5.5 vs gpt-6-luna / gpt-6-sol.** ACCEPTED (Q33 A):
  all three outputs are advisory or non-blocking, and their existing
  validators (learnings normalization, judge JSON validation, report header)
  reject malformed output.
- **The phases conflict on `ROLES` with PR #7024 (`PANEL_REVIEWER`).**
  Mitigation: additive list edits only; resolve on merge.

## Rollout

- Dispatch the plan only after #6667 merges. Each phase PR ships to production
  on merge.
- **Phase 1** takes effect on the next job in every repo: in this repo through
  `main` and the `@stable` promotion, in consumers through `@stable`.
  Rollback: revert the PR, or, without a code change, set all three
  `tier_gates` to 0.9 in `.github/ai/claude_engine.json`.
- **Phase 2** reaches consumers in two steps. The reusable workflow change
  ships with `@stable`; the wrapper's `id-token: write` arrives with the next
  `update_workflows.yml` sync (daily 04:00 UTC cron, or the `@stable`
  `repository_dispatch`). Until both land, consumers keep OpenRouter.
- **Phase 3** affects only this repository's release gate.
- **Phase 4** affects only repos with `JUDGE_INTERIM_ENABLED=true`.
- Phases 2–4 can each be turned back to OpenRouter without a revert by setting
  the role's `engine` to `codex` in `.github/ai/claude_engine.json`.

## References

- Project #6664 tracking, integration PR https://github.com/shubhodeep1/coding-workflows/pull/6667
  (branch `orchestrator/project-6664`, head `043f46ea5` at writing)
- `docs/plans/unattended-claude-pipeline-completion-plan.md` (the #6664 plan)
- PR https://github.com/shubhodeep1/coding-workflows/pull/7024 (`PANEL_REVIEWER`
  on the pool, Sonnet 5.5 with Haiku 5.5 fallback)
- PR https://github.com/shubhodeep1/coding-workflows/pull/7026 (prebuilt
  sandbox images, heal editor on Claude)
- CLAUDE.md §4, §6, §14, §15, §18, §20, §27
