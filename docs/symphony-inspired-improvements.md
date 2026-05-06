# Plan: Symphony-Inspired Improvements to the Orchestrator

This plan captures a set of changes inspired by OpenAI's Symphony orchestration
spec (`openai/symphony` SPEC.md, March 2026) that we can adopt in this
repository **without giving up our review/validation/memory stack**. Symphony
is a deliberately small daemon: a tracker poller, a per-issue workspace, a
single multi-turn agent session, and a strict in-repo prompt template. We are
the opposite — a thick GitHub Actions pipeline with a multi-reviewer panel,
Docker-based validation harness, an `ai-memory` retrieval layer, and named
stall-recovery actions.

The work below borrows Symphony's **mechanisms** (thread reuse, event-based
stall, force-tick, strict templating, persistent workspace, lifecycle hooks)
without importing its **policy** (open-ended turn loop, single-agent trust
model, tracker-write delegation). The net effect on per-issue LLM spend is
**cost-down or cost-neutral**: continuation-turn reuse and event-based stall
detection both cut wasted tokens, while no chunk on its own adds new LLM
calls to the steady-state path.

The chunks are grouped into three independently shippable projects:

1. **Project S — Per-Run Efficiency.** Strict prompt rendering, Codex-event
   stall detection, force-tick on phase end, continuation-turn reuse, and
   per-state concurrency caps. Lowest risk, fastest payback, no architectural
   changes.
2. **Project W — Workspace and Lifecycle.** Per-issue persistent workspaces,
   workspace lifecycle hooks, filesystem safety invariants, and run-attempt
   sub-states emitted into the `ai-memory` ledger. Medium risk; touches
   `implement.yml` and `validate.yml` workspace setup.
3. **Project U — Policy Surface Unification.** A single in-repo
   `WORKFLOW.md`-style overlay per consumer repo, blocker-aware runtime
   dispatch, and a state snapshot artifact. Higher risk and not strictly
   required; ship last.

The three projects are independent and can run in parallel. Within each
project, sub-issues are ordered so a partial roll-out still produces a
working pipeline. Every chunk ships behind a feature flag with the
fail-closed default set to today's behavior; flags are flipped on by default
only after the chunk's acceptance criteria are met on the self-test matrix.

---

## Cross-Cutting Goals

1. **Cut wasted LLM tokens** by reusing Codex threads on retries instead of
   re-rendering the full mode prompt each cycle.
2. **Cut wasted wall-clock** by killing zombie sessions on Codex-event
   timeout (not phase-age) and by force-ticking the orchestrator on phase
   end instead of waiting for the next 5-minute cron.
3. **Catch silent prompt-contract drift** by switching prompt rendering to a
   strict engine where unknown variables and unknown filters fail rendering.
4. **Bound blast radius** by adding per-state concurrency caps so a
   regression cannot pile up dozens of issues in one phase simultaneously.
5. **Make in-flight state legible** by emitting run-attempt sub-states into
   the existing `ai-memory` runs ledger and by publishing one snapshot
   artifact per orchestrator tick.
6. **Reduce setup churn** by reusing per-issue workspaces across attempts
   with explicit `after_create` / `before_run` / `after_run` hooks.

## Cross-Cutting Non-Goals

- Replacing GitHub Actions with a long-running daemon. Symphony is a daemon;
  we remain a cron-driven workflow set, and every adaptation here is shaped
  to fit that model.
- Removing the multi-reviewer panel, the validation harness layer, or the
  `ai-memory` retrieval system.
- Migrating off Linear/GitHub Issues. Tracker abstraction is out of scope.
- Symphony's open-ended `max_turns` continuation policy. Our existing
  per-phase iteration caps (`MAX_AUTOFIX_ITERATIONS`,
  `MAX_VALIDATE_CYCLES`, `MAX_STALL_RECOVERIES_PER_ISSUE`,
  `MAX_REVIEW_BLOCKED_RETRIES`) bound work per issue more tightly than a
  single global turn cap and are kept as-is.
- Hot-reload of policy files. GitHub Actions picks up the latest default
  branch on each run, so hot-reload is unnecessary.
- HTTP server / dashboard. The closest viable analog is a JSON snapshot
  artifact (Project U) or pinned status issue, not a live HTTP endpoint.
- Tracker-writes-via-agent-tools refactor. Our orchestrator's label and
  comment writes are mature and well-tested; reshaping that boundary is a
  much larger project not justified by Symphony parity alone.

## Current-State Summary

Verified in `scripts/orchestrate_lib.py`, `scripts/orchestrate_poll_process.sh`,
`scripts/render_prompt.sh`, `.github/workflows/orchestrate_poll.yml`,
`.github/workflows/implement.yml`, `.github/workflows/review_autofix.yml`,
and `prompts/mode-*.txt`:

1. **Prompt rendering** uses `scripts/render_prompt.sh` to expand only the
   `{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}` and
   `{{SERENA_EFFICIENCY_BLOCK_READ_WRITE}}` placeholders (sourced from
   `prompts/serena-efficiency-block.txt`); the script fails if a
   placeholder is unresolved but performs no general per-mode variable
   templating. Other variable substitution happens via shell heredocs
   and env passthrough in the workflow YAML, with no declared per-mode
   variable contract.
2. **Stall detection** is phase-age based, but driven by orchestrator
   state rather than raw tracker label timestamps:
   `scripts/orchestrate_lib.py::detect_stalls()` compares
   `now - status_since_ts` against `STALL_THRESHOLD_*_MINUTES` per phase,
   where `status_since_ts` is persisted when the orchestrator first
   *observes* a phase-label change. The measured age can therefore differ
   from the tracker's real label-change time. The poller also applies an
   Actions-runs age filter to treat long-running `in_progress` runs as
   zombies. A live-but-stuck Codex session inside a phase still burns
   tokens until either the observed phase-age threshold or the
   zombie-run filter trips.
3. **Orchestrator cadence** is `cron: '*/5 * * * *'` on
   `.github/workflows/internal-orchestrate-poll.yml` (the reusable
   `orchestrate_poll.yml` is `workflow_call`-only), plus one immediate
   `workflow_dispatch` shortcut (`gh workflow run internal-orchestrate-poll.yml`)
   from `scripts/review_conflict_resolve.sh` on resolver failure. All
   other phase-end events wait up to 5 minutes for the next tick.
4. **Codex invocation** in implement, review-autofix, validation-self-heal,
   and conflict-resolver paths re-renders the full mode prompt on each
   attempt. Thread reuse / continuation-turn semantics are not used today.
5. **Concurrency** is wave-serialized at the orchestrator level
   (one wave at a time per tracking issue) but uncapped per phase across
   tracking issues. A regression that strands ten PRs in
   `ai:review-blocked` will cause ten autofix loops to run in parallel.
6. **Workspaces** are ephemeral and scoped by workflow run or attempt:
   implement uses `RUNTIME_DIR=/tmp/codex-implement-${GITHUB_RUN_ID}`,
   clarify uses `/tmp/codex-issue-${GITHUB_RUN_ID}`, and the poller uses
   `/tmp/codex-orchestrate-poll-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}`.
   Setup work (clone, deps, harness render) is repeated on every
   retry/attempt.
7. **Run-attempt observability** is via job logs and `ai-memory`
   `runs/<run-id>/ledger/events.jsonl` events. There is no explicit
   sub-state vocabulary (`PreparingWorkspace`, `BuildingPrompt`, etc.) and
   no consolidated per-tick snapshot.
8. **Policy surface** is split across `CLAUDE.md`, `agents.md`,
   `unattended_system_instructions.md`
   ~25 files in `prompts/`, env vars in `.github/workflows/*.yml`, and
   per-repo `.ai/validate.yml`. No single in-repo overlay file.

## Architecture Overview

```
                           ┌──────────────────────────────────┐
                           │  orchestrate_poll.yml (cron 5m)  │
                           │  + workflow_dispatch tick        │
                           └──────────────┬───────────────────┘
                                          │
              ┌───────────────────────────┴────────────────────────┐
              │                                                    │
              ▼                                                    ▼
   ┌────────────────────┐                              ┌──────────────────────┐
   │ Per-state caps     │  Project S                   │ Snapshot writer      │  Project U
   │ (queue gate)       │                              │ (state.json artifact)│
   └─────────┬──────────┘                              └──────────────────────┘
             │
             ▼
   ┌────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
   │ Blocker check      │ ─► │ Per-issue workspace  │ ─► │ Lifecycle hooks      │  Project W
   │ (Project U)        │    │ (Project W)          │    │ (after_create, ...) │
   └────────────────────┘    └──────────┬───────────┘    └──────────────────────┘
                                        │
                                        ▼
                            ┌──────────────────────────┐
                            │ Strict prompt render     │  Project S
                            │ (fail on unknown var)    │
                            └──────────┬───────────────┘
                                       │
                                       ▼
                            ┌──────────────────────────┐
                            │ Codex turn, with         │  Project S
                            │ continuation thread reuse│
                            │ on retries               │
                            └──────────┬───────────────┘
                                       │
                                       ▼
                            ┌──────────────────────────┐
                            │ Codex-event stall guard  │  Project S
                            │ + sub-state ledger emit  │  Project W
                            └──────────┬───────────────┘
                                       │
                                       ▼
                            ┌──────────────────────────┐
                            │ Force-tick on phase end  │  Project S
                            └──────────────────────────┘
```

The dashed boundaries show which project owns which control point. Projects
S and W are mostly orthogonal; Project U builds on artifacts produced by S
and W (per-state caps and sub-state ledger) and is therefore last.

---

## Project S — Per-Run Efficiency

### Goals

1. Eliminate full-prompt re-rendering on retries by reusing Codex threads.
2. Detect stuck Codex sessions on event-idleness, not phase-age.
3. Remove the up-to-5-minute lag between phase end and the next
   orchestrator action.
4. Catch silent prompt-contract drift at render time.
5. Cap per-state concurrency so a regression cannot fan out indefinitely.

### Non-Goals

- Changing which models are called or their reasoning levels.
- Touching the multi-reviewer panel composition or two-pass logic.
- Reshaping the wave/dependency-DAG decomposition.
- Replacing the existing `workflow_dispatch` tick mechanism with
  `repository_dispatch`, a webhook receiver, or any other transport.
  S3 reuses today's `gh workflow run internal-orchestrate-poll.yml`
  invocation.

### Sub-Issues

#### S1 — Strict prompt rendering with declared per-mode variable contracts

**Problem.** `scripts/render_prompt.sh` only substitutes the
`{{SERENA_EFFICIENCY_BLOCK_*}}` placeholders. All other dynamic prompt
content reaches Codex through workflow YAML — shell heredocs, `cat
prompt-file`, and env passthrough — with no per-mode declared variable
contract. When a workflow heredoc references e.g. `${INTEGRATION_BRANCH}`
but the workflow step forgot to export it, the heredoc emits an empty
string and the prompt is silently broken. Mode prompts under `prompts/`
also contain bare `${VAR}` and `{{ TOKEN }}` patterns whose substitution
contracts live implicitly in whichever workflow happens to invoke them;
drift between a mode prompt and its caller surfaces only when the agent
produces obviously-wrong output.

**Fix.** Replace `render_prompt.sh` with a strict renderer
(`scripts/render_prompt.py` using `chevron` for Mustache strict semantics,
or `jinja2` with `StrictUndefined` and `extensions=[]`). The renderer:

1. Loads a per-mode contract file `prompts/contracts/<mode>.yml` declaring
   required variables, optional variables with defaults, and forbidden
   variables (to catch leaking workflow-internal env into prompts).
2. Fails rendering with a non-zero exit and a structured error
   (`missing_required: [VAR]`, `unknown_in_template: [VAR]`,
   `forbidden_present: [VAR]`) if the contract is violated.
3. Writes a render-receipt JSON next to the rendered prompt with the
   contract version, variable values used (redacted for known-secret keys),
   and the prompt body SHA.

The legacy `render_prompt.sh` is kept as a thin shim that calls the new
renderer with `--legacy-mode-name <mode>` for one release so existing
workflow steps keep working. The shim is removed in a follow-up once all
call sites pass `--mode <name>` explicitly.

**Files touched.** `scripts/render_prompt.sh` (shim), new
`scripts/render_prompt.py`, new `prompts/contracts/*.yml` (one per mode),
`tests/render_prompt/*` (golden tests per mode).

**Feature flag.** `STRICT_PROMPT_RENDER` env var, default `false` for one
release. When `false`, renderer logs contract violations as warnings but
still produces output. When `true`, violations fail the workflow step.

**Acceptance.** Every mode in `prompts/mode-*.txt` has a contract file
under `prompts/contracts/`. Running the self-test matrix with
`STRICT_PROMPT_RENDER=true` produces zero contract violations. A
deliberately-broken contract (delete a required key from a workflow step's
env) fails rendering with a non-zero exit and a structured error message.

**Risk / rollback.** Low. Default-off for one release; `STRICT_PROMPT_RENDER=false`
restores today's behavior instantly. Contract files are additive and
deletable.

**Cost impact.** Neutral. Same prompts, just validated. Catches errors
that today cause re-runs (which would otherwise burn tokens).

---

#### S2 — Codex-event-based stall detection

**Problem.** Today's stall detector
(`scripts/orchestrate_lib.py::detect_stalls()`) trips on
`now - status_since_ts > STALL_THRESHOLD_*_MINUTES`, where
`status_since_ts` is set when the orchestrator first observes a phase
label change. A live but stuck Codex session — one that has stopped
emitting events but has not exited — burns tokens for the full
phase-age threshold (commonly 30–60 minutes) before either the
threshold or the zombie-runs filter kills it. The phase-age threshold
is also too coarse to distinguish "agent is genuinely working" from
"agent is hung after a network blip."

**Fix.** Add a parallel stall channel keyed on Codex event idleness:

1. Every Codex invocation in the repo (`implement`, review-autofix
   reviewers, editor, conflict-resolver, validation self-heal,
   wave-judge, RB-judge) writes a **per-pid** heartbeat file
   `${RUNNER_TEMP}/codex-heartbeats/codex-${pid}.json` containing
   `{ run_id, issue, mode, last_event_at, last_event_kind, pid }`,
   updated on each Codex protocol event (one append per stream chunk is
   enough; coalescing is fine). Per-pid filenames are required because
   `review_autofix.yml` runs five reviewers in parallel inside a single
   job and a shared filename would clobber.
2. A new step `scripts/codex_stall_guard.sh` runs as a background
   sidecar inside each Codex-invoking step. It scans every heartbeat
   file in `${RUNNER_TEMP}/codex-heartbeats/`, checks
   `now - last_event_at > CODEX_STALL_TIMEOUT_SECONDS` (default `600`)
   per file, and on trip kills the matching Codex pid with SIGTERM,
   then SIGKILL after `CODEX_STALL_KILL_GRACE_SECONDS` (default `30`).
3. The killed Codex pid returns `137` on SIGKILL. The invoking shell
   script (e.g. `scripts/review_run_reviewers.sh`,
   `scripts/review_apply_fixes.sh`) **must explicitly check the
   Codex exit status and propagate it**: a backgrounded Codex whose
   exit code is not `wait`-ed will allow the script to continue
   silently. Each invoking script gets a wrapper helper
   `codex_run_with_stall_guard()` that runs Codex, captures the exit
   code, emits a `codex_stall_killed` ledger event when the code is
   `137` and the heartbeat shows a guard-side termination, and exits
   the calling script with the same non-zero code. Adding the wrapper
   is part of S2's scope, not a follow-up.
4. `scripts/orchestrate_poll_process.sh` keeps the phase-age threshold as
   an outer safety net, raised to `90` minutes once event-based killing
   is the primary path.

**Files touched.** New `scripts/codex_stall_guard.sh`, edits to
`scripts/run_validation_repo_checks.sh`,
`scripts/review_run_reviewers.sh`, `scripts/review_apply_fixes.sh`,
`scripts/review_conflict_resolve.sh`,
`scripts/self_heal_validation.sh`, `scripts/review_rb_judge.sh`,
and the `implement.yml` Codex step. New env keys in
`.github/workflows/orchestrate_poll.yml`.

**Feature flag.** `CODEX_STALL_GUARD_ENABLED` env var, default `false` for
one release. When `false`, the guard runs in observe-only mode and only
emits the `codex_stall_observed` ledger event.

**Acceptance.** A self-test that pauses a Codex stub for
`CODEX_STALL_TIMEOUT_SECONDS + 60s` is killed within 60 seconds of the
threshold and produces a `codex_stall_killed` ledger event. Phase-age
threshold for the same scenario does not trip first. With the guard
disabled, the same self-test takes the full phase-age window to kill.

**Risk / rollback.** Medium. Could kill a legitimately-slow session if
`CODEX_STALL_TIMEOUT_SECONDS` is set too low. Mitigated by observe-only
default and by sizing the timeout from real-run distribution data
(p99 inter-event delay) before flipping the flag.

**Cost impact.** Decreases. Zombie sessions today burn tokens for the
full phase-age window (often 30–60 minutes); event-based killing caps
this at the new timeout (default 10 minutes).

---

#### S3 — Force-tick on phase end via `workflow_dispatch`

**Problem.** `scripts/review_conflict_resolve.sh` already uses an EXIT
trap that calls `gh workflow run internal-orchestrate-poll.yml` (a
`workflow_dispatch`) on resolver failure when
`IS_INTEGRATION_SYNC=true`, which bypasses the cron wait. Every other
phase-end event (`ai:done` set, `ai:ready-to-merge` set,
`ai:validation-passed` set, `ai:review-blocked` set) waits up to 5 minutes
for the next cron tick before the poller advances state.

**Fix.** Generalize the existing EXIT-trap pattern (today's
`gh workflow run internal-orchestrate-poll.yml` from
`scripts/review_conflict_resolve.sh`) into a small helper
`scripts/orchestrate_force_tick.sh` that:

1. Invokes `gh workflow run internal-orchestrate-poll.yml` with
   inputs `{ reason, source_workflow, issue, run_id }` — the same
   `workflow_dispatch` mechanism the resolver already uses, so S3 is
   strictly a generalization rather than a new dispatch surface.
2. Is idempotent via a cooldown timestamp persisted on the
   `ai-memory` branch under
   `runs/force_tick/<tracking_issue>.json` (using the existing
   memory-branch helpers in `scripts/memory_helpers.sh`). If the last
   recorded timestamp is within `FORCE_TICK_COOLDOWN_SECONDS`
   (default `30`) the call is a no-op. Putting cooldown state on
   `ai-memory` keeps it inside our existing persistent-state surface
   and avoids adding new GitHub API round-trips for pinned comments
   or gists.
3. Is called from EXIT traps in `implement.yml` (after PR push),
   `review_autofix.yml` (after merge or after `ai:review-blocked`),
   `validate.yml` (after final pass/fail label), and the existing
   `review_conflict_resolve.sh` site (which is upgraded to use the
   helper instead of inlining the dispatch).

The cron `*/5 * * * *` schedule remains unchanged as the safety net.

**Files touched.** New `scripts/orchestrate_force_tick.sh`, edits to
`scripts/review_conflict_resolve.sh` (drop inline dispatch, call helper),
`.github/workflows/implement.yml`,
`.github/workflows/review_autofix.yml`,
`.github/workflows/validate.yml`, and the matching files in
`workflow-templates/`.

**Feature flag.** `FORCE_TICK_ENABLED` env var, default `true` from the
first release (the existing inlined dispatch in `review_conflict_resolve.sh`
proves the pattern). Helper still respects cooldown.

**Acceptance.** When implement.yml pushes a PR for an orchestrator
tracking issue, the next orchestrator tick runs within 30 seconds (not
up to 5 minutes). The cooldown prevents more than one tick per 30 seconds
per tracking issue across rapid phase changes.

**Risk / rollback.** Low. `FORCE_TICK_ENABLED=false` returns to cron-only
behavior. Cooldown bounds dispatch storms.

**Cost impact.** Neutral on LLM tokens. Decreases wall-clock latency.

---

#### S4 — Continuation-turn reuse on Codex retries

**Problem.** Implement-diagnose-repair, review-autofix iterations,
validation self-heal cycles, and conflict-resolver retry attempts each
re-render the full mode prompt and start a fresh Codex session. With
prompt caching the system-instruction portion is cached, but the rendered
issue body, plan, prior diffs, and prior failure summary are re-paid on
every retry. The Codex App Server supports thread reuse with a
continuation prompt; we are not using it.

**Fix.** Plumb a `thread_id` through retry-capable phases:

1. The first attempt of a phase invokes Codex with no `thread_id` and
   the full rendered prompt. The Codex protocol assigns a
   `thread_id`; the wrapper records it to a phase-scoped state file
   `${RUNNER_TEMP}/codex-thread.<phase>.json`.
2. Subsequent attempts in the same workflow run pass that `thread_id`
   on the Codex App Server invocation and render a different prompt
   from `prompts/mode-<phase>-continuation.txt` containing only the
   delta (new test failure summary, new reviewer findings, new conflict
   markers) — never the full original prompt.
3. The continuation prompt for each phase is added as a new file with a
   contract under `prompts/contracts/`. Initial set:
   `mode-implement-repair-continuation.txt`,
   `mode-implement-diagnose-continuation.txt`,
   `mode-validate-self-heal-continuation.txt`,
   `mode-review-apply-fixes-continuation.txt`,
   `mode-review-conflict-resolver-continuation.txt`.
4. Across separate workflow runs (e.g. orchestrator restarts the phase
   after a stall recovery), the `thread_id` is **not** reused — each
   workflow run starts a fresh thread. Cross-run thread reuse is
   explicitly out of scope to keep the state simple.

**Files touched.** New `prompts/mode-*-continuation.txt` and matching
contracts. Edits to `scripts/review_apply_fixes.sh`,
`scripts/review_conflict_resolve.sh`,
`scripts/self_heal_validation.sh`, the implement.yml Codex retry block,
and the wave-judge and RB-judge invocation sites (judges do not retry
today, so they are out of scope for this chunk).

**Feature flag.** `CODEX_THREAD_REUSE_ENABLED` env var, default `false`
for one release; flipped to `true` after acceptance.

**Acceptance.** A two-iteration review-autofix run with the flag enabled
makes the second iteration's Codex call with a `thread_id` set and a
continuation prompt < 25% the size of the first-iteration prompt. The
final code change produced is functionally equivalent (golden test:
same files modified, same diff modulo timing) to the flag-off run.

**Risk / rollback.** Medium. If the Codex App Server changes
thread-reuse semantics, behavior could shift. Mitigated by per-phase
flag gating and by keeping the full-prompt path live.

**Cost impact.** Decreases. Token savings per retry depend on prompt
size: review-autofix continuations save the most (large reviewer
bundle), implement-repair the least (small failure summary). Estimated
20–40% reduction in retry-loop token spend across the pipeline.

---

#### S5 — Per-state concurrency caps

**Problem.** Wave serialization caps concurrent in-flight issues per
tracking issue but not across tracking issues. A regression that strands
ten PRs in `ai:review-blocked` simultaneously will run ten parallel
review-autofix loops, each at xhigh reasoning, until the per-PR cap
exhausts. The same applies to `ai:implementing` and
`ai:validation-failed`.

**Fix.** Add a queue gate at the top of `orchestrate_poll_process.sh`:

1. Read caps from `.github/ai/concurrency_caps.yml`:
   ```yaml
   max_concurrent_by_state:
     "ai:implementing": 4
     "ai:review-blocked": 2
     "ai:validation-failed": 3
     "ai:planning": 6
   global_max_concurrent: 12
   ```
2. At the **start** of each `orchestrate_poll.yml` tick, prefetch the
   list of currently-running workflow runs via one paginated GitHub
   API call (`gh api ...workflows/.../runs?status=in_progress`) and
   build a per-state count map. This map is held in memory for the
   duration of the tick (or written to
   `${RUNTIME_DIR}/running_runs_by_state.json` for child scripts to
   consume) so per-issue dispatch decisions are O(1) lookups, not
   per-issue API calls. This honors the API-hygiene rule in
   `unattended_system_instructions.md` §14 (don't fan out API calls inside
   per-issue loops).
3. Before dispatching a phase action for an issue in state S, consult
   the cycle-local map. If the count for state S is at-or-over cap,
   defer the action to the next tick and emit a `phase_capped` ledger
   event.
4. Caps apply to dispatch only; in-flight runs are not killed.
5. The cron tick continues; capped issues are retried next tick.

**Files touched.** New `.github/ai/concurrency_caps.yml`, edits to
`scripts/orchestrate_poll_process.sh` and `scripts/orchestrate_lib.py`,
matching files in `workflow-templates/`.

**Feature flag.** Caps file presence is the gate. If the file is missing
or empty, no caps are applied (today's behavior). Per-state cap of
`-1` disables that state's cap explicitly.

**Acceptance.** With `ai:review-blocked` capped at 2, a self-test that
labels three PRs `ai:review-blocked` simultaneously produces exactly two
running `review_autofix.yml` runs at the next tick, with the third
emitting a `phase_capped` ledger event. The third run begins on the
tick after one of the first two completes.

**Risk / rollback.** Low. Empty/missing caps file = today's behavior.
Caps are advisory; in-flight runs complete normally.

**Cost impact.** Decreases in incident scenarios. Steady-state neutral.

---

## Project W — Workspace and Lifecycle

### Goals

1. Reuse per-issue workspaces across attempts so setup work
   (clone, dependencies, harness render) is paid once per issue, not
   once per workflow run.
2. Move scattered setup/teardown shell into named lifecycle hooks with
   well-defined fatal-vs-ignored semantics and timeouts.
3. Enforce filesystem safety invariants on workspace paths.
4. Emit run-attempt sub-states into the existing `ai-memory` runs ledger
   so external tooling can observe phase progress at finer granularity
   than the `ai:*` label set.

### Non-Goals

- Cross-job workspace persistence on the GitHub-hosted runner pool. The
  hosted runner is ephemeral; per-issue persistence is implemented by
  caching the workspace via the `actions/cache` API keyed on the
  sanitized issue identifier. This is a Symphony-style workspace
  reuse adapted for ephemeral runners — not a true persistent FS.
- Replacing the `ai-memory` retrieval system; we only add new ledger
  event kinds.

### Sub-Issues

#### W1 — Per-issue workspace cache with `created_now` semantics

**Problem.** `implement.yml` creates
`/tmp/codex-implement-${GITHUB_RUN_ID}`, `clarify.yml` creates
`/tmp/codex-issue-${GITHUB_RUN_ID}`, and the poller creates
`/tmp/codex-orchestrate-poll-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}`,
all fresh on every workflow run. A retry of the implement phase for
issue 1234 re-clones the repo, re-installs deps, and re-renders the
validation harness from scratch. On a typical retry this is 2–4 minutes
of pure setup before any agent work.

**Caveat — `actions/cache` mutability.** `actions/cache@v4` does **not**
overwrite an existing cache entry: when a job's `key` matches an
already-stored entry, the post-job save step is a no-op. A naive
`key = workspace-v1-<issue>` would freeze the very first snapshot
forever; an `actions/cache` key that includes `${GITHUB_RUN_ID}` would
save every run but fragment the cache namespace and rapidly hit the
10 GB per-repo cache cap (LRU-evicting older entries before they can
be reused). The fix below uses **versioned keys plus `restore-keys`
prefix matching**, with a maintenance workflow that prunes stale
entries — and falls back to a sibling-branch persistence model if the
cache budget is still inadequate in practice.

**Fix.** Introduce a workspace key derived from the sanitized issue
identifier and use it as the `actions/cache` key:

1. New helper `scripts/workspace_init.sh` computes
   `WORKSPACE_KEY = sanitize(issue_identifier)` (regex
   `[A-Za-z0-9._-]`, replace others with `_`) and
   `WORKSPACE_PATH = ${RUNNER_TEMP}/workspaces/${WORKSPACE_KEY}`.
2. The workflow step calls `actions/cache@v4` with:
   - `key: workspace-v1-${{ env.WORKSPACE_KEY }}-${{ env.WORKSPACE_FINGERPRINT }}-${{ github.run_id }}`
     where `WORKSPACE_FINGERPRINT` is `hashFiles('package-lock.json',
     '.ai/validate.yml', 'package.json')`. Including `run_id`
     guarantees the post-job save always writes a new entry.
   - `restore-keys:` ordered prefix list, longest-to-shortest:
     1. `workspace-v1-${WORKSPACE_KEY}-${WORKSPACE_FINGERPRINT}-`
        (newest run with same fingerprint)
     2. `workspace-v1-${WORKSPACE_KEY}-`
        (newest run for this issue, any fingerprint — may need a
        hook re-run; W2 `after_create` is invoked)
   - This combination produces a true append-and-evolve cache: every
     run writes a fresh entry, every restore picks the latest entry
     for the issue, and stale fingerprints are upgraded by re-running
     `after_create`.
3. `workspace_init.sh` exports `CREATED_NOW=true` if the cache
   restore was a complete miss **or** matched only the looser
   second-tier `restore-keys` prefix (fingerprint changed),
   `CREATED_NOW=false` only if the first-tier exact-fingerprint
   prefix matched. Subsequent steps use this to gate one-time setup
   (W2 hooks).
4. **Cache-budget maintenance.** A new nightly workflow
   `workspace-cache-maintenance.yml` calls
   `gh actions caches list` and deletes:
   - All but the newest 3 entries per `workspace-v1-<key>-<fingerprint>-` prefix.
   - All entries for tracking-issue keys whose tracking issue is
     closed.
   This bounds per-issue cache spend to roughly
   `3 × workspace_size` and keeps total usage well under the 10 GB
   repo cap. The maintenance workflow ships in the same PR as W1.
5. **Fallback to sibling-branch persistence.** If steady-state cache
   eviction is still observed despite maintenance (tracked via a
   `cache_evicted_unexpectedly` ledger event when `CREATED_NOW=true`
   for an issue that previously had `CREATED_NOW=false`), the W1
   helper switches at flag time to a sibling-branch model: each
   workspace lives at `ai-workspaces/<WORKSPACE_KEY>` (an orphan
   branch), checked out at start and committed at end. This is
   storage-unbounded (subject to repo size limits) and avoids the
   cache cap entirely. The fallback is implemented as
   `WORKSPACE_BACKEND={cache,branch}` in `workspace_init.sh`,
   default `cache`.
6. Workspace path validation:
   `WORKSPACE_PATH` must have `${RUNNER_TEMP}/workspaces/` as a prefix;
   the script aborts the step on violation. (W3 generalizes this.)

**Files touched.** New `scripts/workspace_init.sh`, new
`.github/workflows/workspace-cache-maintenance.yml`, edits to
`.github/workflows/implement.yml`,
`.github/workflows/validate.yml`,
`.github/workflows/review_autofix.yml` (resolver step only),
matching files in `workflow-templates/`.

**Feature flag.** `WORKSPACE_REUSE_ENABLED` env var, default `false`
for one release. When `false`, the script falls back to today's
per-run-id path with `CREATED_NOW=true` always.

**Acceptance.** A second implement.yml run for the same issue restores
a populated workspace, sets `CREATED_NOW=false`, and skips one-time
setup hooks (W2). End-to-end retry wall-clock drops by at least 90
seconds on the self-test matrix.

**Risk / rollback.** Medium. Stale workspace state could pollute a
retry. Mitigated by including a manifest hash
(`hashFiles('package-lock.json', '.ai/validate.yml')`) in the cache
key so dependency or harness changes invalidate the cache.

**Cost impact.** Neutral on LLM tokens. Decreases CI minutes (cheaper
billing). Indirectly may decrease tokens by reducing retries that fail
during setup rather than agent work.

---

#### W2 — Workspace lifecycle hooks (after_create, before_run, after_run, before_remove)

**Problem.** Setup shell — venv creation, dep install, validation
harness render, integration-branch checkout, fingerprint capture — is
spread across many workflow steps and per-script preludes. There is no
single place to add a step that runs once per workspace, or once per
run, or once at workspace removal, with a documented timeout and
failure semantics.

**Fix.** Add a hooks runner `scripts/run_workspace_hook.sh`:

1. Reads hook scripts from `.github/ai/workspace_hooks/<phase>/`:
   `after_create.sh`, `before_run.sh`, `after_run.sh`,
   `before_remove.sh`.
2. Executes the relevant hook with `bash -lc`, cwd set to
   `WORKSPACE_PATH`, timeout from `WORKSPACE_HOOK_TIMEOUT_SECONDS`
   (default `600`).
3. Failure semantics (Symphony-aligned): `after_create` and
   `before_run` failures are fatal to the phase; `after_run` and
   `before_remove` failures are logged-and-ignored.
4. `after_create` is gated on `CREATED_NOW=true` (from W1) so it runs
   once per cached workspace lifetime.
5. Hook stdout/stderr is captured to
   `${RUNNER_TEMP}/workspace-hooks/<phase>-<hook>.log` and truncated
   to last 10 KB on failure for inclusion in the structured error
   message.

**Files touched.** New `scripts/run_workspace_hook.sh`, new
`.github/ai/workspace_hooks/` directory with placeholder hooks per
phase, edits to `.github/workflows/implement.yml` and
`.github/workflows/validate.yml` to invoke the hooks at the right
points, matching files in `workflow-templates/`.

**Feature flag.** Hook directory presence. Empty or missing hook
file = no-op. The flag is implicit in whether hooks have been
authored.

**Acceptance.** A consumer repo with a non-empty
`.github/ai/workspace_hooks/implement/after_create.sh` sees that
script run exactly once per workspace lifetime (verified across two
back-to-back retry runs of the same issue). A failing
`before_run.sh` aborts the phase with a structured error referencing
the captured log.

**Risk / rollback.** Low. Hooks are opt-in. Empty hooks directory =
no behavior change.

**Cost impact.** Neutral.

---

#### W3 — Filesystem safety invariants on workspace paths

**Problem.** Workspace path computation is currently inlined into
multiple workflows. There is no single place that enforces
"`cwd == workspace_path` before agent launch" or "workspace path must
be inside the workspace root."

**Fix.** Add `scripts/workspace_safety_check.sh` invoked immediately
before any Codex launch in implement, validate, review-autofix, and
conflict-resolver paths. The check verifies:

1. `WORKSPACE_PATH` resolves under `${RUNNER_TEMP}/workspaces/`
   (realpath comparison, not string prefix).
2. The current working directory (`pwd -P`) equals the resolved
   `WORKSPACE_PATH`.
3. `WORKSPACE_KEY` matches `^[A-Za-z0-9._-]+$`.

Failures abort the step with exit code 78 (documented as
`workspace_safety_violation` in the ledger event vocabulary).

**Files touched.** New `scripts/workspace_safety_check.sh`, edits to
the same set of files as W1 (one `workspace_safety_check.sh` invocation
just before each Codex launch).

**Feature flag.** Always on. The check is cheap and no consumer flow
should violate the invariants. If a violation surfaces a real bug, the
expected response is to fix the caller, not relax the check.

**Acceptance.** A self-test that deliberately sets `WORKSPACE_PATH` to
`/tmp/escape` aborts before Codex launches and emits a
`workspace_safety_violation` ledger event. All real-flow self-tests
pass without violations.

**Risk / rollback.** Low. The check has no failure mode in real flows
that already use `workspace_init.sh` from W1.

**Cost impact.** Neutral.

---

#### W4 — Run-attempt sub-states emitted as ledger events

**Problem.** `ai:*` labels capture pipeline phase but not intra-run
progress. A user looking at a long-running implement step has no way
to tell whether Codex is in `BuildingPrompt` (template render),
`StreamingTurn` (active agent work), or `Finishing` (post-processing
diff and pushing). Stall classification (S2) and any future budget
attribution would benefit from knowing this.

**Fix.** Define a sub-state vocabulary mirroring Symphony's:
`PreparingWorkspace`, `BuildingPrompt`, `LaunchingAgentProcess`,
`InitializingSession`, `StreamingTurn`, `Finishing`, `Succeeded`,
`Failed`, `TimedOut`, `Stalled`. Emit each as a ledger event via the
existing `ai-memory` runs ledger:

1. New helper `scripts/ledger_emit_substate.sh` writes one line to
   `ai-memory:runs/${RUN_ID}/ledger/events.jsonl` of the form
   `{ ts, kind: "run_substate", substate, phase, issue, mode }`.
2. Each phase script (implement, validate, review-autofix branches,
   conflict-resolver) calls the helper at the boundaries above.
3. The helper is idempotent on duplicate emissions of the same
   `substate` within one phase invocation.

The new events are additive to the existing ledger schema; no schema
version bump is required because new `kind` values are explicitly
forward-compatible per the schema's open-set rule (verify in
`schemas/runs_ledger.v1.json`).

**Files touched.** New `scripts/ledger_emit_substate.sh`, edits to
the same Codex-invoking scripts as S2 (one emit at each phase
boundary), edits to `scripts/ai_memory_lib.py` only if a schema
helper for the new event kind is needed.

**Feature flag.** `LEDGER_SUBSTATES_ENABLED` env var, default `true`
from first release (the events are additive and consumed only by
opt-in tooling).

**Acceptance.** A complete implement.yml run produces, in order, at
least one ledger event per sub-state from `PreparingWorkspace` to
`Succeeded` or a terminal failure substate. The substate log can be
reconstructed from the ledger alone by filtering on `kind=run_substate`.

**Risk / rollback.** Low. Events are additive.

**Cost impact.** Neutral.

---

## Project U — Policy Surface Unification

### Goals

1. Give consumer repos one in-repo file (`WORKFLOW.md`) to override
   policy knobs and prompt overlays, versioned with their code.
2. Add a runtime blocker-check at dispatch time so DAG drift between
   decompose-time and label-state cannot dispatch premature work.
3. Publish a per-tick state snapshot artifact so external tooling and
   humans have one place to read in-flight state.

### Non-Goals

- Replacing `CLAUDE.md`, `agents.md`, `unattended_system_instructions.md`
  or `unattended_system_instructions.md`. These remain the
  authoritative system instructions; `WORKFLOW.md` is an **overlay**
  with a narrow allowed-key schema.
- Hot-reload. GitHub Actions checks out the latest default branch on
  each run; that is sufficient.
- An HTTP server. The snapshot is a JSON artifact, optionally
  committed to a state branch or pinned in a tracking-issue comment.
- Symphony's full Linear-tracker contract. We stay on GitHub Issues.

### Sub-Issues

#### U1 — Per-consumer-repo `WORKFLOW.md` overlay

**Problem.** Consumers tune our pipeline today by setting workflow
inputs, GitHub Actions secrets, and ad-hoc env vars in their forked
template wrappers under `.github/workflows/internal-*.yml`. A consumer
who wants to adjust e.g. `MAX_AUTOFIX_ITERATIONS` or override the
`mode-implement.txt` system prompt has to edit a wrapper YAML
(losing template-update auto-merge) or fork the prompt. There is no
single overlay surface.

**Fix.** Define a `WORKFLOW.md` file at the consumer repo root with
optional YAML front matter (config) and optional Markdown body
(prompt overlay):

1. Front matter schema (typed, validated by `scripts/load_workflow_overlay.py`):
   ```yaml
   ---
   workflow_overlay_version: 1
   limits:
     max_autofix_iterations: 3
     max_validate_cycles: 3
     max_stall_recoveries_per_issue: 5
     max_concurrent_by_state: { ... }   # surface for S5
   prompt_overrides:
     - mode: implement
       append_path: .ai/prompts/implement.append.md
     - mode: plan
       replace_path: .ai/prompts/plan.replace.md
   workspace:
     hooks_dir: .github/ai/workspace_hooks   # surface for W2
   tracker_writes:
     enable_label: ai:foo                      # opt-in custom labels
   ---
   ```
2. The orchestrator and per-phase workflows call
   `scripts/load_workflow_overlay.py` early; it parses, validates
   against `schemas/workflow_overlay.v1.json`, and exports the
   resolved values via `$GITHUB_ENV`. Unknown keys fail validation
   (Symphony-aligned strict semantics).
3. Prompt overrides are applied by the strict renderer (S1) at
   render time: `append_path` is concatenated after the base mode
   prompt; `replace_path` substitutes wholesale. Both are subject
   to the per-mode contract, so an overlay cannot reference
   undeclared variables.
4. Absent `WORKFLOW.md` is the no-overlay default (today's behavior).

**Files touched.** New `scripts/load_workflow_overlay.py`, new
`schemas/workflow_overlay.v1.json`, edits to `orchestrate_poll.yml`,
`implement.yml`, `plan.yml`, `clarify.yml`, `validate.yml`,
`review_autofix.yml`, and the matching files in `workflow-templates/`.
Documentation update in `README.md`.

**Feature flag.** `WORKFLOW_OVERLAY_ENABLED` env var, default `true`
when `WORKFLOW.md` exists at the consumer repo root, `false`
otherwise. Implicit gating by file presence.

**Acceptance.** A consumer repo with a `WORKFLOW.md` that sets
`limits.max_autofix_iterations: 1` runs review-autofix with one
iteration cap regardless of the template default. Removing the file
restores the template default. An overlay with an unknown key fails
validation with a structured error and aborts the affected workflow
step.

**Risk / rollback.** Medium. Schema mistakes propagate to all
consumers. Mitigated by versioning the schema (`v1`) and rejecting
unknown keys; v2 changes are additive only or routed through a
migration helper.

**Cost impact.** Neutral.

**Depends on.** S1 (strict renderer) for prompt-override application;
S5 (per-state caps) for the `max_concurrent_by_state` overlay key.

---

#### U2 — Blocker-aware runtime dispatch

**Problem.** The wave/dependency-DAG decomposition fixes the order in
which sub-issues are dispatched, but state drift can still produce
out-of-order dispatch: a sub-issue gets relabelled by a human, a
parent's `ai:merged` label is removed by a stall-recovery action and
not re-applied, or a fix-up issue's blocker is added after dispatch
selection has already run. There is no runtime check that a sub-issue
about to be dispatched still has all of its declared blockers in a
terminal state.

**Fix.** Before dispatching any phase action against a sub-issue,
`scripts/orchestrate_poll_process.sh` consults
`scripts/blocker_check.py`:

1. Reads the sub-issue's declared blockers from the tracking-issue
   body's `dependency_edges` block (already authored by the
   orchestrator decomposition).
2. Fetches each blocker's current state (label set + closed/open).
3. Returns `eligible=true` only if every blocker is in a terminal
   state (`ai:merged`, `ai:closed`, or PR merged).
4. On `eligible=false`, the dispatcher emits a
   `dispatch_deferred_blocker` ledger event and skips the sub-issue
   for this tick.

This is defense-in-depth against drift; it does not change the
decompose-time DAG.

**Files touched.** New `scripts/blocker_check.py`, edits to
`scripts/orchestrate_poll_process.sh` and `scripts/orchestrate_lib.py`.

**Feature flag.** `RUNTIME_BLOCKER_CHECK_ENABLED` env var, default
`true` from first release. The check is read-only and additive to
the existing dispatch logic.

**Acceptance.** A self-test that sets up a sub-issue with one open
blocker (no `ai:merged` label) confirms dispatch is deferred for that
tick and the `dispatch_deferred_blocker` event is emitted. After the
blocker is labeled `ai:merged`, the next tick dispatches normally.

**Risk / rollback.** Low. Disabling the flag returns to today's
DAG-only behavior.

**Cost impact.** Decreases. Avoids dispatching premature work that
would be thrown away.

---

#### U3 — Per-tick state snapshot artifact

**Problem.** In-flight orchestrator state is observable today only by
reading individual workflow run logs and label sets across many
issues. There is no single artifact that summarizes "what is the
orchestrator doing right now," which makes incident response slower
and budget tracking harder.

**Fix.** At the end of each `orchestrate_poll.yml` tick, emit one
JSON artifact `state.json` capturing:

```json
{
  "tick_at": "2026-04-28T10:15:00Z",
  "tracking_issues": [
    { "number": 1608, "wave": 3, "phase_counts": { "ai:implementing": 2, "ai:done": 1 } }
  ],
  "running": [
    { "issue": 1620, "phase": "ai:implementing", "substate": "StreamingTurn",
      "run_id": "...", "thread_id": "...", "started_at": "...",
      "last_event_at": "...", "tokens": { "input": 1200, "output": 800 } }
  ],
  "deferred": [
    { "issue": 1631, "reason": "phase_capped", "cap_state": "ai:review-blocked" }
  ],
  "totals": { "input_tokens": 1234567, "output_tokens": 234567, "runs": 42 }
}
```

The artifact is:
1. Uploaded as a workflow artifact `state-snapshot` on every tick.
2. Optionally committed to a `state-snapshot` orphan branch as
   `state.json` (overwriting) when
   `STATE_SNAPSHOT_BRANCH_ENABLED=true` so external tooling can poll
   one URL. Branch lifecycle uses force-push on the orphan to keep
   history shallow (last `STATE_SNAPSHOT_HISTORY_DEPTH` ticks, default
   `100`).

The data is assembled from the ledger sub-states (W4), the
tracking-issue label aggregation already done by the poller, and the
per-issue token totals already recorded by the memory layer.

**Files touched.** New `scripts/build_state_snapshot.py`, edits to
`scripts/orchestrate_poll_process.sh`,
`.github/workflows/orchestrate_poll.yml` (artifact upload + optional
branch commit step), matching files in `workflow-templates/`.

**Feature flag.** `STATE_SNAPSHOT_ARTIFACT_ENABLED` env var, default
`true` (artifact-only mode is cheap). `STATE_SNAPSHOT_BRANCH_ENABLED`
default `false`.

**Acceptance.** Every `orchestrate_poll.yml` run uploads a valid
`state.json` artifact passing
`schemas/state_snapshot.v1.json` validation. The artifact reflects
every tracking issue currently labeled `ai:orchestrator-tracking`.
With branch publishing enabled, the `state-snapshot` branch holds the
latest snapshot and history is bounded.

**Risk / rollback.** Low. Artifact-only is read-only. Branch
publishing is opt-in.

**Cost impact.** Neutral on LLM tokens. Negligible CI minutes.

**Depends on.** W4 (run-substate ledger events) for `running.substate`
field. Without W4, the field is omitted; the chunk still ships.

---

## Rollout Order and Dependencies

```
S1 ─────────────────────────────► S4 ──┐
                                       │
S2 (independent)                       │
                                       ├─► U1 (overlay needs S1, S5)
S3 (independent)                       │
                                       │
S5 ─────────────────────────────────────┘

W1 ─► W2 (needs CREATED_NOW from W1)
W1 ─► W3 (path invariants align with workspace_init)
W4 (independent) ─► U3 (snapshot consumes substate ledger)

U2 (independent of S/W; can ship anytime in U)
U3 depends on W4
```

Recommended sequencing:

1. **Wave 1, in parallel:** S1, S2, S3, S5, W1, W4, U2.
   Each is independent; together they form the minimum viable
   Symphony-aligned baseline (strict rendering, fast stall, fast
   tick, capped concurrency, reusable workspaces, sub-state
   observability, blocker safety).
2. **Wave 2, in parallel after Wave 1 lands:** S4, W2, W3, U3.
   These either consume Wave 1 outputs (S4 needs the strict
   renderer's continuation prompts contracted; W2 needs `CREATED_NOW`
   from W1; W3 piggybacks on W1's path layout; U3 consumes W4's
   ledger events).
3. **Wave 3:** U1.
   The overlay surface depends on every other knob being already
   defined (per-state caps, hook directory, prompt contracts), so
   it ships last.

## Cross-Project Concerns

- **Self-test matrix.** Each chunk lands with golden tests under
  `tests/` exercising the flag-on and flag-off paths. The
  `nightly-validation-selftest.yml` workflow runs all chunks at
  flag-on against fixture issues to catch interaction bugs before
  defaults are flipped.
- **Telemetry.** All new ledger event kinds (`codex_stall_observed`,
  `codex_stall_killed`, `phase_capped`, `dispatch_deferred_blocker`,
  `run_substate`, `workspace_safety_violation`) must be added to
  `schemas/runs_ledger.v1.json` as additive open-set values and to
  the README's ledger event reference.
- **Documentation.** Each chunk updates `README.md` with the new
  flag, default value, and one-paragraph operator guidance. The
  `agents.md` quick-reference table gains rows for the new sub-state
  vocabulary (W4) and the overlay schema (U1) on landing.
- **Backward compatibility.** Per `unattended_system_instructions.md` §6
  (naming immutability), no existing workflow input, env var, or
  label is renamed by this plan. New env vars are introduced
  (listed per chunk) and old ones are kept until at least one full
  release after their replacement is the default.
- **No `/db/contracts/*.yml` changes.** This plan touches no
  database collections.

## Open Questions (Resolve Before Implementation)

1. **Q: Does the Codex App Server expose `thread_id` reuse on the
   command-line interface we use today, or only via the JSON-RPC
   protocol?** Answer determines whether S4 is a thin flag flip or
   needs a wrapper that speaks the protocol. Required before S4 is
   sized.
2. **Q: Will the W1 versioned-key + restore-keys + nightly-maintenance
   strategy keep total `actions/cache` usage under the 10 GB
   per-repo cap on a steady-state stream of orchestrator runs?** W1
   already specifies a sibling-branch fallback
   (`WORKSPACE_BACKEND=branch`) for the case where eviction is still
   observed. The remaining sizing question is the steady-state
   working-set size (number of active issues × workspace size); we
   need one week of cache-usage telemetry from a flag-on canary
   before deciding whether the fallback is needed in practice.
   Required before W1 is sized.
3. **Q: Does the existing memory layer already track Codex token
   totals per run with enough fidelity to populate U3's `totals`
   block, or is a new accumulator required?** Required before U3 is
   sized.
4. **Q: Should `WORKFLOW.md` (U1) live at the consumer repo root or
   under `.github/ai/`?** Symphony chose root for visibility; we
   already host other AI knobs under `.github/ai/`. Style call;
   pick before U1 is sized.

These questions are not blockers for filing this plan; they are
blockers for sizing the affected sub-issues and should be answered
during the planning phase of each chunk's PR.
