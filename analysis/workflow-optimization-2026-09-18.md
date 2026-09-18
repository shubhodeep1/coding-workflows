## Executive Summary

- **Review/autofix is the dominant bottleneck:** 30.01 of 47.62 runner-hours (63%). Full reviews took 5,047–7,175s and usually made 12 OpenRouter calls; run `35312976998` made 16. **Impact: 10–40 minutes/run; confidence: high.**
- **Concurrency creates hour-long duplicate waits:** runs `35324993978` and `35340561680` waited 5,062s and 5,509s before `codex-agent`; three long cancelled peers consumed 3.88 hours of wall-clock visibility. **Impact: up to 84–92 minutes per duplicate; confidence: high.**
- **CI is a single 107-step critical path:** 17/17 succeeded, but p50 was 1,931s and p95 1,993.6s. **Potential impact: 8–15 minutes through measured sharding; confidence: medium.**
- **Poller overhead is systemic:** 103 successful runs consumed 7.62 runner-hours at p50 270s. In runs `35355562803` and `35353300488`, memory start/end writes alone consumed 95–106s. **Potential impact: 30–60s/run; confidence: high.**
- **Token volume is concentrated in repeated review panels:** 98.70M total tokens across 113 calls. Numeric telemetry implies an 82.8% cache-read ratio, although aggregate `cache_hit_rate` is suppressed because three calls lacked usage. **Potential savings: 10–25%; confidence: medium.**
- **Operational health is nominal but degraded signals are masked:** zero hard failures, but eight cancellations, provider retry cascades, tolerated `gawk` failures, Git cleanup errors, and firewall warnings occurred. **Reliability impact: medium; confidence: high.**

## Speed Optimizations

1. **Prevent duplicate review jobs before reusable-workflow concurrency — critical-path queueing win**
   - **Evidence:** `35324993978` waited 5,061.5s and `35340561680` waited 5,509.4s before `codex-agent`. Three paired internal-review runs were cancelled after 3,603–6,598s. Productive continuation dispatches deliberately bypass the peer check in `.github/workflows/review_autofix.yml`.
   - **Root cause:** the direct continuation and `pull_request:synchronize` paths enter the same PR concurrency group; the redundant run waits behind the designated successor.
   - **Exact change:** add a lightweight, fail-open preflight in `.github/workflows/internal-review.yml` that skips a synchronize-triggered call when a same-head `review_autofix.yml` workflow-dispatch continuation is active.
   - **Logging addition:** `REVIEW_CONCURRENCY_DECISION pr=... head_sha=... peer_run_id=... queue_ms=... action=run|skip reason=...`.
   - **Savings:** eliminate up to 84–92 minutes of duplicate queueing and reduce active-run sweep blockage.
   - **Risk:** low-medium; on API uncertainty, run normally.

2. **Bound reviewer stragglers after quorum — critical-path compute win**
   - **Evidence:** run `35301629065` completed five first-pass reviewers by approximately 03:37 UTC, while `moonshotai/kimi-k3` completed at 04:14 UTC. Run `35312976998` spent retries on `stall_guard`, `server_error`, and `rate_limit`.
   - **Root cause:** every six-model pass waits for the slowest slot even after sufficient reviewer diversity may exist.
   - **Exact change:** shadow-test a five-of-six quorum, then permit straggler cancellation only after five valid results and consolidator schema validation.
   - **Logging addition:** `REVIEW_SLOT_TIMING pass=... slot=... attempt=... duration_ms=... outcome=...` and `REVIEW_QUORUM_DECISION succeeded=... pending=... action=wait|cancel`.
   - **Savings:** 10–37 minutes on observed straggler runs.
   - **Risk:** medium; preserve full-panel mode for high-risk paths and rollback via a variable.

3. **Reuse the poller’s memory checkout**
   - **Evidence:** `Record poll run start/end` took 48.8s + 46.0s in `35355562803` and 58.9s + 47.0s in `35353300488`.
   - **Root cause:** each event repeats Git branch preparation and push work.
   - **Exact change:** initialize one memory workspace per poll job and reuse it for both events while retaining separate durable pushes.
   - **Logging addition:** extend `AI_MEMORY_TELEMETRY` with `duration_ms`, `fetch_ms`, `commit_ms`, `push_ms`, and `conflict_retries`.
   - **Savings:** estimated 30–60s per 270–300s poll.
   - **Risk:** low if existing push-retry semantics remain unchanged.

4. **Shard the measured CI hotspot**
   - **Evidence:** `.github/workflows/ci.yml` has 107 named steps in one `lint` job; CI p50 is 32.2 minutes.
   - **Root cause:** independent static checks and large orchestrate-poll tests execute serially.
   - **Exact change:** first emit per-step/test-group timings, then split the orchestrate-poll module and static checks into deterministic parallel jobs.
   - **Logging addition:** `CI_GROUP_TIMING group=... duration_ms=... tests=... failures=...`.
   - **Savings:** estimated 8–15 minutes critical-path latency.
   - **Risk:** medium; additional setup runner-minutes and possible hidden ordering dependencies.

**Micro-optimization:** the 78 skipped/other runs consumed only 257s total. Tightening their triggers is lower priority.

## Cost Optimizations

1. **Adapt the second review pass**
   - **Evidence:** numeric slow reviews normally made exactly 12 calls—six first-pass and six second-pass. Aggregate usage was 98.70M tokens.
   - **Root cause:** every reviewer runs again regardless of first-pass agreement or contribution.
   - **Exact change:** in shadow mode, identify pass-one slots producing no unique actionable findings; skip only those slots in pass two after consolidator validation.
   - **Estimated savings:** 10–25% of review calls/tokens; up to 50% on a fully clean first pass.
   - **Quality risk:** medium; retain full pass two for workflow, security, migration, and low-consensus changes.

2. **Enable the existing reviewer health circuit breaker**
   - **Evidence:** `35312976998` made 16 calls versus the normal 12, with three usage-unavailable attempts, two failbacks and terminal `stall_guard`/`rate_limit` failures.
   - **Root cause:** `REVIEWER_CIRCUIT_BREAKER_ENABLED=0`, despite existing health thresholds.
   - **Exact change:** enable it with the existing three-failure/1,800s settings, initially for retryable failures only.
   - **Estimated savings:** 1–4 calls and 15–30 minutes during a provider incident.
   - **Quality risk:** low-medium; temporarily reduced model diversity, with automatic expiry.

3. **Stabilize cache prefixes**
   - **Evidence:** numeric cache ratios ranged from 49.8% (`35333337581`) to 90.1% (`35301629065`); aggregate numeric ratio was 82.8%.
   - **Root cause:** **inference:** dynamic PR metadata, memory and Semble results likely vary before or within the reusable prefix.
   - **Exact change:** place stable rubric/system text first; append timestamps, SHAs, comments and retrieved context after the cache breakpoint.
   - **Logging addition:** `CACHE_PREFIX phase=... model=... prefix_sha256=... stable_bytes=... dynamic_bytes=...`.
   - **Estimated savings:** raising the ratio to 87% would avoid roughly 4.1M uncached prompt tokens in this window.
   - **Quality risk:** low; prompt content remains unchanged.

4. **Keep Semble enabled; measure its avoided expansion**
   - **Evidence:** 23 queries returned 217,530 bytes, averaging 9.5KB; runtime fallbacks were zero. Deep-dive calls took only 350–656ms.
   - **Assessment:** Semble is adding small, targeted context rather than noisy bulk expansion.
   - **Exact change:** log `prompt_bytes_before`, `prompt_bytes_after`, and `candidate_bytes_avoided` per query.
   - **Savings:** not directly measurable without this baseline; current latency cost is negligible.

**Serena:** zero queries, probes or fallbacks were recorded, so no efficiency conclusion is possible.

## Reliability Improvements

1. **Stop retry cascades on unhealthy reviewer slots**
   - **Evidence:** `35312976998` exhausted Minimax through server/stall failures and Kimi through stall/rate-limit failures. `35325012757` recorded two non-retryable DeepSeek failures while succeeding overall.
   - **Root category:** provider/model health.
   - **Fix:** enable the circuit breaker and emit a final per-slot health summary.
   - **Expected impact:** prevent repeated degraded-model retries and reduce tail variance.
   - **Rollback/fail-open:** expire health state after 30 minutes; continue with remaining reviewers.

2. **Make continuation/synchronize ownership explicit**
   - **Evidence:** six review cancellations; three long cancelled jobs account for 3.88 hours. Two successful duplicates later waited 84–92 minutes before doing little or no model work.
   - **Root category:** orchestration/concurrency.
   - **Fix:** caller preflight plus `continuation_owner_run_id` telemetry.
   - **Expected impact:** remove duplicate pending jobs and active-run sweep suppression.
   - **Fail-open:** dispatch when peer detection is unavailable.

3. **Treat tolerated test failures as structured degradation**
   - **Evidence:** review runs `35338302845` and `35333315736` reported one failed test because `gawk` was unavailable, yet concluded success.
   - **Root category:** runner dependency gap.
   - **Fix:** install `gawk` in the review test environment or replace the test dependency with portable `awk`.
   - **Logging addition:** `SOFT_TEST_FAILURE suite=... test=... missing_binary=gawk policy=continue`.
   - **Expected impact:** eliminate false-red internal validation and make genuine tolerated failures auditable.
   - **Rollback:** retain current continue behavior until the dependency fix is verified.

4. **Remove recurring cleanup noise**
   - **Evidence:** `35333827334`, `35333337581`, and `35332266812` reported `git-submodule cannot be used without a working tree`; `35349897098` skipped unsafe `RUNTIME_DIR` cleanup and killed an orphan `gh` process.
   - **Root category:** lifecycle cleanup ordering.
   - **Fix:** do not remove/move the checkout before action post-steps; guard submodule cleanup with a worktree check.
   - **Logging addition:** `CLEANUP_SUMMARY worktree_valid=... runtime_dir_valid=... orphan_count=... result=...`.
   - **Expected impact:** fewer masked cleanup defects and orphan processes.
   - **Fail-open:** warnings must remain nonfatal.

5. **Emit explicit MCP availability probes**
   - **Evidence:** 29 Semble fallbacks were all contract-test fixtures; runtime fallbacks were zero. Poll run `35355562803` showed early `SEMBLE_AVAILABLE=false` but post-install availability was true. Serena emitted no probes.
   - **Fix:** emit post-install `SEMBLE_PROBE` and existing-schema `SERENA_PROBE`, then extend `scripts/cost_audit.py` for Semble probe counters.
   - **Expected impact:** distinguish harmless pre-install state from broken rollout.
   - **Rollback:** probe failure remains fail-open.

`BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts were both zero; there is no observed policy-pressure or context-window incident in this window.

## AI Memory Health

- **Retrieval hit rate:** 9/9 slow-run retrieves selected records—100%.
- **Budget:** average 1,379 estimated tokens versus 1,400 budget, or 98.5% utilization.
- **Selection:** eight retrieves selected 30 records; one selected 29.
- **Keyword method:** `llm` for all nine; no `plain` or `none`.
- **Health exceptions:** no zero-record, `fail_open:true`, or `enabled:false` retrieve events.
- **Push reliability:** 4 of 25 sampled push-bearing operations needed two attempts; none exceeded two.
- **Learning output:** seven `record-candidate` operations succeeded, while 12 `write_lessons_learned` operations produced zero records.

**Recommendation:** retain retrieval, but add `records_considered`, `records_truncated`, score distribution and operation timings. The 98.5% budget saturation leaves little headroom even though no context warnings occurred.

## GH API Call Audit

The repository broadly follows `CLAUDE.md` §15: extend existing calls, batch GraphQL lookups, cache per cycle, and fail open on misses.

- **Review sweep:** `.github/workflows/review_autofix_sweep.yml` correctly fetches PRs once and snapshots active runs once per workflow/status rather than per PR. With candidates, this is one PR-list call plus six status snapshots before pagination.
- **Merge train:** `scripts/review_merge_train.sh` caches one file-list fetch per distinct PR per release tick.
- **Poller:** `scripts/orchestrate_poll_process.sh` contains batch-size-25 GraphQL helpers and cycle-local caches for labels and linked PRs.
- **Observed rate limits:** no GitHub API rate-limit event was found. The `rate_limit` in `35312976998` was a model-provider failure, not GitHub.
- **Primary gap:** there are no runtime endpoint-family call counters, page counts or cache-hit metrics. The 75s `Process each tracking issue` step cannot therefore be divided into API, local compute or retry time.

**Recommended instrumentation in `scripts/gh_helpers.sh`:**

`GH_API_CALL helper=gh_retry endpoint_family=pulls method=GET attempt=1 duration_ms=... result=ok pages=1 cache=miss rate_limited=false`

Emit a final `GH_API_SUMMARY` grouped by endpoint family. Normalize endpoints and omit query values to avoid leaking issue content.

## Prompt Cache & Memory System

- Reported cache-read tokens: **80,966,807**; prompt tokens: **16,798,389**.
- Numeric cache ratio: **82.8%**. Aggregate `cache_hit_rate` remains N/A because three of 113 calls lacked usage.
- Cache-creation tokens were reported as zero; this does not prove that providers created no caches.
- Review logs consistently emitted `REVIEWER_CACHE status=supported prompt_reused=true`.
- No `CONTEXT_BUDGET_WARN` occurred, but AI-memory retrieval consumed 98.5% of its own budget.
- Semble context was compact and fast; no runtime fallback was observed.

**Priority changes:** stable prompt prefixes, cache-prefix hashes, and per-call cache attribution. Expected impact is approximately 4.1M fewer uncached prompt tokens if the numeric ratio reaches 87%.

## Orchestrator Health

- `orchestrate_poll` completed **103/103** runs successfully with p50 **270s** and p95 **296.7s**.
- Run `35355562803` processed tracking issue `#3965`; security-pass fix issue `#4113` remained in progress. The run completed without dispatch churn.
- Review sweeps repeatedly skipped PRs `#4119` and `#4120` because active review jobs existed—for example runs `35332461892`, `35335201493`, and `35337520497`. Deduplication is working, but hour-long review/concurrency tails delay cadence.
- Forward-merge run `35332198829` detected a conflict, opened PR `#4120`, and dispatched review. Run `35332266812` subsequently resolved a conflict and pushed edits—a positive recovery signal.
- Clarify, plan and clarify-response each produced 20 skipped/other runs; implement produced 18 skipped runs. Their combined cost is minor.

**Logging addition:** emit one `ORCH_ISSUE_DECISION` per tracking issue with state before/after, action, blocker and duration. Add `ORCH_STAGE_TIMING` around global prefetches: latest polls spent roughly 62 seconds before printing the first issue-processing line.

## Pipeline Flow Bottlenecks

| Stage | Dominant delay | Evidence | Priority fix |
|---|---|---|---|
| Clarify/plan | Broad-trigger skips | 20/20 other in each family | Deprioritize; only 257s across all skipped runs |
| Implement | Phase mismatch events | `35349975875` and `35349897098` skipped wrong phase | Log event origin and expected/current phase |
| Review/autofix queue | Duplicate continuation/synchronize jobs | 5,062s and 5,509s queue waits | Caller-level active-continuation preflight |
| Review/autofix compute | Six-model, two-pass panels and stragglers | 5,047–7,175s; 12–16 calls | Quorum/straggler policy and adaptive pass two |
| Validate/CI | One serial 107-step job | p50 1,931s | Timing instrumentation, then deterministic sharding |
| Orchestrate | Memory Git writes and silent prefetch | 95–106s memory writes; ~62s pre-issue gap | Reuse memory checkout and emit stage timings |
| Merge/conflict | Forward-merge conflict review | PR `#4120`, runs `35332198829` and `35332266812` | Existing recovery worked; retain and measure retries |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix consumes 63% of runner time; CI p50 is 32.2 minutes; poller p50 is 4.5 minutes.
- **Top failure modes:** duplicate concurrency waits, model stall/rate-limit cascades, tolerated missing-`gawk` tests, and cleanup warnings.
- **Highest cost driver:** 113 OpenRouter calls and 98.70M tokens, concentrated entirely in review/autofix.
- **Top three actions:**
  1. Suppress redundant synchronize runs when a continuation owns the PR head.
  2. Add reviewer slot/quorum timing and shadow an adaptive second pass.
  3. Instrument and optimize poller memory/API stages.

## Metrics Appendix

### Outcomes and duration

| Workflow family | Runs | Success | Failure | Cancelled | Other | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Total** | 342 | 256 | 0 | 8 | 78 | 17s | 3,630s |
| review_autofix | 86 | 80 | 0 | 6 | 0 | 9.5s | 6,361s |
| orchestrate_poll | 103 | 103 | 0 | 0 | 0 | 270s | 296.7s |
| ci | 17 | 17 | 0 | 0 | 0 | 1,931s | 1,993.6s |
| copilot_pull_request_reviewer | 14 | 14 | 0 | 0 | 0 | 203s | 404.4s |
| clarify | 20 | 0 | 0 | 0 | 20 | 1s | 11s |
| plan | 20 | 0 | 0 | 0 | 20 | 1s | 9s |
| implement | 20 | 2 | 0 | 0 | 18 | 1s | 12.1s |
| orchestrate_clarify_respond | 20 | 0 | 0 | 0 | 20 | 1s | 9.1s |
| validation_refresh | 1 | 1 | 0 | 0 | 0 | 1,188s | 1,188s |

Overall success was 74.85% of all runs and 96.97% of success-or-cancel terminal outcomes; hard failure rate was 0%.

### Token and cache telemetry

| Metric | Value |
|---|---:|
| OpenRouter calls | 113 |
| Usage available / unavailable | 110 / 3 |
| Prompt tokens | 16,798,389 |
| Completion tokens | 938,578 |
| Cache-read tokens | 80,966,807 |
| Cache-creation tokens reported | 0 |
| Total tokens | 98,698,605 |
| Numeric cache ratio | 82.8% |
| Reported aggregate `cache_hit_rate` | N/A |
| `wall_clock_p50_ms` | 13,000 |
| `wall_clock_p99_ms` | 7,011,440 |
| Wall-clock samples | 115 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

### MCP telemetry

| System/target | Queries | Bytes | Fallbacks | Probe OK | Probe failed | Probe skipped |
|---|---:|---:|---:|---:|---:|---:|
| Semble aggregate | 23 | 217,530 | 29 contract-test / 0 runtime | N/A | N/A | N/A |
| Semble `reviewer-context`¹ | 7 | 89,370 | 0 | N/A | N/A | N/A |
| Semble `overflow`¹ | 7 | 53,278 | 0 | N/A | N/A | N/A |
| Serena, no target markers | 0 | 0 | 0 | 0 | 0 | 0 |

¹Target rows cover 14 of 23 queries from selected slow-run logs.

- Serena tool calls: **0**; response bytes: **0**; query time: **0ms**.
- Other MCP servers observed: **none**.

### GH API telemetry

| Metric | Result |
|---|---|
| Runtime call-count telemetry | Not emitted |
| GitHub rate-limit events observed | 0 |
| Sweep batching | One PR snapshot; active runs prefetched per workflow/status |
| Poller batching | GraphQL batches of 25 plus cycle-local caches |
| Main data gap | No endpoint, page-count, cache-hit or duration summary |
