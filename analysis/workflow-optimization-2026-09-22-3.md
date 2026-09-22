## Executive Summary

- **Move CI invariants to a fast preflight.** CI failed 29/33 runs (87.9%); 22 failed `Script-workflow cross-reference` and seven `Inventory parity`. Nine deep dives repeated the same missing `scripts/helper.sh` reference only after 1,991–2,563 seconds. Estimated recovery: **~10–40 minutes per failed run, up to ~14 runner-hours/window**. **Confidence: high.**
- **Stop same-head review reruns.** PR #4259 at head `f9d7196...` failed four times with `editor_empty_noop`; the three repeats after the first consumed **14,672 seconds and 51.3M reported tokens**. Estimated saving: **58–99 minutes per suppressed rerun**. **Confidence: high.**
- **Preflight the editor’s isolated credentials.** Run `35755050146` spent 5,451.6 seconds in reviewer models before failing because `OPENROUTER_API_KEY` was absent from editor isolation. The final summary incorrectly classified this as `empty_noop`. **Confidence: high.**
- **Telemetry is double-counting aggregate and split logs.** Run `35755050146` duplicated one `SEMBLE_QUERY` and one `CONTEXT_BUDGET_WARN`; CI run `35758949049` duplicated four contract-test fallbacks. Reported counts are therefore inflated. **Confidence: high.**
- **Orchestration is reliable but expensive.** `orchestrate_poll` succeeded 74/75 runs, but p50 was 304 seconds. Run `35767039713` spent 45.2 seconds fetching all branches and about 101 seconds on two AI-memory pushes. **Confidence: high.**
- **Prompt caching is useful but prompt pressure remains.** Broad telemetry recorded 87.6M cache-read tokens, approximately 76.3% of prompt-plus-cache-read volume, but `cache_hit_rate` is null and six logical context warnings reached 71.6–97.7% of model windows. **Confidence: medium** due incomplete usage coverage.

## Speed Optimizations

1. **Critical path: run static CI invariants first**
   - **Evidence:** 22 cross-reference failures and seven inventory failures; sampled cross-reference checks themselves completed in under a second after ~33–43 minutes of prior work.
   - **Root cause:** deterministic repository-shape checks run late in a monolithic lint job.
   - **Exact change:** create an initial `static-preflight` job, or move `check_workflow_script_refs.py` and inventory parity immediately after checkout.
   - **Savings:** 10–40 minutes per invalid run.
   - **Risk:** low; preserve the existing checks unchanged.

2. **Critical path: same-head terminal failure circuit**
   - **Evidence:** runs `35728735316`, `35737368348`, `35748016396`, and `35755050146` repeatedly processed PR #4259 at the same SHA.
   - **Root cause:** `editor_empty_noop` does not suppress future sweeps for an unchanged head.
   - **Exact change:** persist a terminal marker keyed by PR, head SHA, editor configuration fingerprint, and failure class. Skip after the first confirmed non-transient failure; clear on head/config changes or force dispatch.
   - **Savings:** 58–99 minutes per suppressed run.
   - **Risk:** low with fail-open marker reads and an operator override.

3. **Critical path: reuse one AI-memory checkout per run**
   - **Evidence:** poll run `35767039713` spent 50.6 seconds recording start and 50.2 seconds recording completion. Review run `35755050146` spent another 217 seconds on candidate/failure memory steps.
   - **Root cause:** each memory operation clones/pushes independently.
   - **Exact change:** queue run events locally and commit/push once during finalization; reuse one checkout when an immediate start event remains necessary.
   - **Savings:** roughly 50–200 seconds per active AI run.
   - **Risk:** medium; flush queued failure events in `always()` cleanup.

4. **Critical path: narrow orchestrator checkout**
   - **Evidence:** run `35767039713` spent 45.2 seconds fetching every branch.
   - **Exact change:** fetch only the default branch, state-snapshot branch, and issue branches actually referenced by the cycle.
   - **Savings:** about 40–45 seconds per poll; potentially ~50 minutes across 75 runs.
   - **Risk:** medium; retain an on-demand fetch fallback.

5. **Micro-optimization: stop repeatedly probing unavailable live logs**
   - **Evidence:** release run `35696603462` made 275 failed live-log fetches over approximately one hour.
   - **Exact change:** after three failures, use a five-minute live-log cooldown while continuing status polling.
   - **Savings:** limited wall-clock benefit but about **95% fewer failed live-log requests**.
   - **Risk:** low.

## Cost Optimizations

1. **Suppress unchanged-head review failures**
   - Five telemetry-backed editor failures consumed **71.1M reported OpenRouter tokens**, subject to duplicate-log inflation.
   - The three repeated `f9d7196...` runs alone reported 51.3M tokens.
   - **Estimated saving:** tens of millions of tokens per recurring incident.
   - **Quality risk:** none when keyed to unchanged head and configuration.

2. **Make reviewer health treat repeated `unknown` failures as actionable**
   - Across 11 deep review summaries:
     - `google_gemini-3_1-flash-lite`: 2 successes, 9 failures.
     - `z-ai_glm-5_2`: 2 successes, 9 failures.
     - Qwen and xAI: 11/11 successes each.
     - DeepSeek: 10/11; MiniMax: 9/11 with 19 attempts.
   - **Exact change:** open the existing health circuit after three consecutive unknown/empty-usage failures, while retaining at least three healthy reviewers.
   - **Estimated saving:** up to ~30% of reviewer attempts in the sampled runs.
   - **Quality risk:** medium; use adaptive quarantine rather than permanent removal.

3. **Reduce prompt expansion before lowering model quality**
   - Run `35755050146` assembled a 555,486-byte editor prompt: 302,033 bytes static plus 253,059 bytes editor body.
   - **Exact change:** deduplicate instructions, move dynamic metadata after a stable prefix, cap repeated logs/diffs, and reference artifacts instead of embedding them repeatedly.
   - **Estimated saving:** 10–20% of uncached prompt tokens.
   - **Quality risk:** low if evidence blocks and output contracts remain intact.

4. **Keep Semble; fix measurement before tuning it**
   - Assembled telemetry reports 25 queries and 294,083 bytes with zero runtime fallbacks. Deep-dive unique queries were small—approximately 14 KB for reviewer context and 7–9 KB for overflow context.
   - Semble is adding targeted context at sub-second latency, not driving the 500+ KB prompts.
   - All reported fallbacks were contract tests, not runtime failures.
   - Serena recorded zero queries/probes and was disabled in sampled recent runs; efficiency cannot be assessed.

5. **Do not downgrade editor reasoning to mask deterministic failures**
   - `xhigh` editor reasoning is expensive, but missing credentials and unchanged-head no-ops are the primary waste.
   - Only consider `high`/`medium` for retry attempts after fixing classification and circuit-breaking.
   - **Quality risk:** high if changed prematurely.

## Reliability Improvements

1. **Fix the broken `implement.yml` reference and add preflight logging**
   - Emit `CI_PREFLIGHT_V1 check=workflow_script_refs outcome=... missing_count=... elapsed_ms=...`.
   - Expected impact: eliminate 22 deterministic failures and expose recurrence within a minute.
   - Rollback: restore original job ordering; check semantics remain unchanged.

2. **Differentiate editor infrastructure failure from valid no-op**
   - Run `35755050146` failed with missing editor-isolation credentials but finalized as `editor_empty_noop`.
   - Add `EDITOR_ATTEMPT_V1` with model, attempt, exit class/code, stdout/stderr/summary bytes, elapsed time, isolated-key-present boolean, and dirty-worktree state.
   - Preflight the isolated environment before reviewer fanout.
   - Fail closed for missing credentials; fail open only for optional diagnostics.

3. **Deduplicate telemetry at source**
   - Add a stable `event_id` to `SEMBLE_*`, `SERENA_*`, usage, context-warning, and memory lines.
   - Alternatively, skip aggregate job logs when split step logs are available.
   - Expected impact: accurate cost, fallback, warning, and call counts without changing workflow behavior.

4. **Repair AI-memory failure recording**
   - Five sampled review failures emitted both `record-candidate` and `record-run-event` with `ok:false, fail_open:true`, losing the most valuable failure evidence.
   - Add `reason`, `error_class`, checkout/push timings, and retry outcome.
   - Keep workflows fail-open, but upload a compact local failure artifact when the memory push fails.

5. **Instrument metadata-integrity failures**
   - Run `35729893830` failed at `Collect PR metadata`: “Could not publish the linked-issue metadata integrity digest.”
   - Add digest destination, response class, attempt count, and whether core PR metadata remained valid.
   - Retry once; only fail open if the digest is auxiliary and core metadata passes validation.

6. **Classify runner termination separately**
   - Implement run `35739823689` ended on a runner shutdown signal.
   - Emit `RUNNER_TERMINATION_V1 phase=implement productive_commit=false`.
   - Safely re-dispatch once only when no commit/push occurred.

7. **Policy signals**
   - `BREAK_GLASS`: zero—no evidence of rubric/policy override pressure.
   - `CONTEXT_BUDGET_WARN`: seven reported, six logical after deduplication. These indicate prompt-size risk, not policy pressure.
   - Semble: 44 reported fallbacks, approximately 40 unique; all were `context=contract-test`. Runtime fallback count was zero.

## AI Memory Health

- **Retrieval:** 12 logical deep-dive retrieves; 12 selected records—**100% hit rate**.
- **Budget use:** average 1,405.9 estimated tokens against a 1,416.7-token average budget—**99.2% utilization**, leaving little headroom.
- **Keyword methods:** 11 `llm`, one `plain`, zero `none`.
- **No zero-record or disabled retrieves** were observed.
- **Writes:** four operations needed two push attempts. Five failed review runs lost both candidate and run-event writes through fail-open behavior.
- **Healthy fail-open:** `finalize-task` in run `35766419516` reported `reason=no_linked_issues`; this is a valid no-op.
- **Recommendation:** target 85–90% retrieval-budget utilization, reuse one memory checkout, and log explicit write failure reasons. This preserves retrieval quality while reducing latency and making lost failure learning visible.

## GH API Call Audit

- **Production aggregate call counts are not emitted**, so endpoint-level totals and rate-limit utilization remain a material data gap.
- **Largest observed redundancy:** run `35696603462` attempted 275 unavailable live-log fetches. A cooldown would cut this to roughly 15 attempts.
- **Positive hygiene:** `issue_pr_status` uses a batched GraphQL lookup; scheduled `cancel_on_pr_close` resolves PR states through aliased GraphQL batches. This follows the repository rules in `agents.md` to use `gh_retry`, batching, and cycle-local caches.
- **Known test-only call profiles:** conflict repair exercised 13 calls when fixed, 11 when still conflicted, and one when already mergeable.
- **No real rate-limit event was found** in sampled logs; HTTP 429 matches were comments/test fixtures, not emitted failures.
- **Required logging:** instrument `gh_retry` with `GH_API_CALL_V1 endpoint_group method attempt status elapsed_ms batch_size cache_hit rate_remaining rate_reset`. Avoid logging URLs containing sensitive query data.
- **Expected reduction:** live-log cooldown ~95%; enforcing cycle-local reuse for active-run and PR-state lookups should remove repeated per-item calls, but current telemetry cannot quantify the remainder.

## Prompt Cache & Memory System

- Broad telemetry: 27.23M prompt, 1.16M completion, 87.63M cache-read, and 0.16M cache-write tokens across 194 calls; 25 calls lacked usage data.
- Derived broad cache-read share is approximately **76.3%**; selected deep logs were approximately **79.9%**. The official aggregate `cache_hit_rate` remains null.
- Explicit sampled rates ranged from 0% to 81.6%, indicating fragmented coverage and substantial per-run variance.
- Six logical context warnings included:
  - `35701080313`: 125,098/128,000 tokens, ratio 0.9773.
  - `35755050146`: 160,947/200,000, ratio 0.8047.
  - `35737368348`: ratio 0.7923.
- **Fragmentation causes:** very large dynamic diff/review blocks, repeated logs, run-specific paths and metadata, and dynamic material preceding reusable instructions.
- **Recommendations:**
  1. Stable system/rubric/policy prefix first.
  2. Dynamic PR metadata, paths, timestamps, and logs last.
  3. Hash and reference unchanged static context rather than re-embedding it.
  4. Cap memory retrieval below 90% of budget.
  5. Emit cache status for every call, including unavailable/unsupported reason.
- Expected impact: 10–20% fewer uncached tokens, lower latency, and fewer context-limit failures.

## Orchestrator Health

- `orchestrate_poll`: 75 runs, 74 successes, one cancellation; p50 304 seconds and p95 708 seconds.
- Run `35767039713` successfully initialized Semble from unavailable to available/indexed, processed issue #4255, found no fresh review run, completed a conflict sweep with zero fixes, and persisted start/end memory events.
- Recent autofix sweeps correctly suppressed duplicate dispatches for active PRs #4259, #4276, and #4280.
- Liveness concern: cancel-cleanup repeatedly preserved three active runs without PR linkage but logged only the count.
- Add:
  - `POLL_ISSUE_V1 issue phase decision reason elapsed_ms gh_calls dispatches`.
  - IDs/workflow/head refs for unlinked active runs.
  - Separate setup, issue-processing, conflict-sweep, snapshot, and memory-push timers.
- Track: same-head terminal skips, stalled issues by phase, active-run age, conflict-recovery count, judge invocations, and memory-push latency.

## Pipeline Flow Bottlenecks

| Stage | Evidence | Dominant overhead | Priority fix |
|---|---|---|---|
| Clarify | p50 1s; mostly skipped/no-op runs | Event fan-out noise | Retain fast gating |
| Plan | p50 1s, p95 383s | Occasional model execution | No immediate change |
| Implement | p95 830s; run `35755460053` took 2,768s with stall recovery | Model compute, queueing, recovery | Classify stalls and runner shutdowns |
| Review/autofix | p95 6,067s; 11 editor-step failures | Reviewer fanout, same-head reruns, editor isolation | Highest AI-path priority |
| CI | p50 2,030s; 29/33 failed | Late deterministic checks | Highest fleet priority |
| Orchestrate poll | p50 304s | Full fetch, API processing, two memory pushes | Narrow fetch and batch memory |
| Validate/refresh | 475s / 1,066s | Test compute | Optimize after CI/review |
| Release gate | 6,759s sampled run | Cross-workflow waits and failed live-log probes | Backoff probes; preserve functional coverage |

Queueing is also material: run `35760389805` waited about 130 seconds for a hosted runner, while review run `35753213375` was cancelled after 3,161 seconds before its first step.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** CI p50 33.8 minutes; review p95 101.1 minutes; release validation 112.7 minutes; poll p50 5.1 minutes.
- **Top failure modes:** missing workflow script reference, inventory drift, repeated editor no-op, editor-isolation credential failure, metadata-integrity publication failure.
- **Highest cost drivers:** six-model reviewer fanout, same-head reruns, 500+ KB prompts, repeated memory pushes.
- **Top three actions:**
  1. Add CI static preflight and fix `scripts/helper.sh` reference.
  2. Add same-head editor-failure suppression plus isolated editor preflight.
  3. Add source event IDs and shared AI-memory/GH API instrumentation.

## Metrics Appendix

### Outcomes

| Metric | Value |
|---|---:|
| Total runs | 1,000 |
| Success | 333 (33.3%) |
| Failure | 44 (4.4%) |
| Cancelled | 14 (1.4%) |
| Other/skipped | 609 |
| Terminal success rate | 85.2% |
| Global p50 / p95 | 6s / 2,493s |
| Success sampling rate | 7% |

### Key workflow families

| Family | Runs | Success | Failure | Cancelled | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|
| CI | 33 | 4 | 29 | 0 | 2,030s | 2,557s |
| Review/autofix | 111 | 85 | 14 | 10 | 280s | 6,067s |
| Implement | 138 | 8 | 1 | 3 | 1s | 830s |
| Orchestrate poll | 75 | 74 | 0 | 1 | 304s | 708s |
| Test/mark stable | 1 | 1 | 0 | 0 | 6,759s | 6,759s |

### Cost and cache telemetry

| Metric | Assembled value |
|---|---:|
| Codex tokens / calls | 2,648,329 / 22 |
| OpenRouter prompt tokens | 27,232,049 |
| Completion tokens | 1,164,545 |
| Cache-read tokens | 87,630,146 |
| Cache-write tokens | 160,141 |
| Total tokens | 116,024,756 |
| OpenRouter calls | 194 |
| Usage available / unavailable | 169 / 25 |
| `cache_hit_rate` | null |
| Derived cache-read share | ~76.3% |
| `wall_clock_p50_ms` / `p99_ms` | 15,000 / 6,739,200 |
| `break_glass_count` | 0 |
| Reported / logical context warnings | 7 / 6 |

### Failure points

| Failure point | Count |
|---|---:|
| CI / Script-workflow cross-reference | 22 |
| Review / Apply fixes with editor model | 11 |
| CI / Inventory parity | 7 |
| Review / Collect PR metadata | 3 |
| Implement job | 1 |

### MCP telemetry

| Server/target | Queries | Bytes | Fallbacks | Probe OK / failed / skipped |
|---|---:|---:|---:|---:|
| Semble, assembled total | 25 | 294,083 | 44 reported; runtime 0 | Not emitted |
| Semble `reviewer-context`, deep deduplicated | 11 | 158,487 | 0 | Not emitted |
| Semble `overflow`, deep deduplicated | 5 | 39,957 | ~40 contract-test-only | Not emitted |
| Serena | 0 | 0 | 0 | 0 / 0 / 0 |

No other MCP servers were observed.

### GH API evidence

| Signal | Value |
|---|---:|
| Production aggregate call count | Not emitted |
| Failed live-log probes, run `35696603462` | 275 |
| Conflict test calls: fixed / unresolved / not needed | 13 / 11 / 1 |
| Observed production rate-limit events | 0 |
| Primary gap | Endpoint, cache-hit, retry, and quota telemetry absent |

**Material data gap:** aggregate marker counts can be inflated when the same source event appears in both an aggregate job log and a split step log.
