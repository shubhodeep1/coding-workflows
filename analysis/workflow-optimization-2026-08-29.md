## Executive Summary

- **Move CI policy guards to the start of `lint`.** Five runs—33230824404, 33232922397, 33234826104, 33243011464, and 33245461068—failed after 551–593s with the identical missing `codex_config_assemble` guard. Potential saving: **up to 47 minutes per comparable window**. **Confidence: high.**
- **Review/autofix is the dominant tail and cancellation source.** Its p95 was **6,392.6s**; 17/84 runs were cancelled, consuming **29,346s (8.15h) of workflow wall time**. Seven were labelled `cancelled_before_first_step`, accounting for 4.94h of queue/wait time. **Confidence: high; cancellation cause is medium-confidence.**
- **Three reviewer failures shared one deterministic render defect.** Runs 33245886964, 33246465915, and 33246480829 failed on unresolved `REFERENCE_SECURITY_MONEY_LENS` content after lengthy check-run waits. Pre-render validation could save roughly **five minutes per deterministic failure**. **Confidence: high.**
- **Wrapper routing creates substantial noise.** Clarify, plan, implement, and clarify-response produced **718 skipped runs** and 1,993s of lifecycle time. Mirroring reusable-workflow predicates in the wrappers can remove nearly all of these runs. **Confidence: high.**
- **Prompt caching is effective but incompletely measured.** OpenRouter cache hit rate was **85.53%**, with 34.88M cache-read tokens; increasing this to 90% would shift approximately **1.82M tokens** from uncached prompts to cache reads. Only 39/116 usage records contained numeric totals. **Confidence: medium.**
- **Telemetry aggregation is inflated.** Run 33246562953 contains both combined-job and per-step copies: 116 OpenRouter calls dedupe to 101, and 18 Semble queries dedupe to 16. **Confidence: high.**

## Speed Optimizations

| Rank | Finding and evidence | Exact change | Estimated saving | Risk |
|---|---|---|---|---|
| 1 — Critical path | The cheap guard at `.github/workflows/ci.yml:743-810` ran after roughly nine minutes in all five CI failures. | Move “Shared shell-block anti-regression checks” directly after checkout/setup. Emit `CI_GUARD_FAILURE guard=codex_config_assemble file=... expected=...`. | Up to **551–593s per affected run; 47m total observed**. | Low |
| 2 — Routing | 718 phase workflows were skipped: clarify 180, plan 179, implement 179, clarify-response 180. | Copy each reusable workflow’s complete event/author/body predicate into its `internal-*` wrapper. Most importantly, filter `/reclarify` and `Clarification required` at wrapper level. | Removes **718 workflow records and 33.2m lifecycle time**; likely reduces burst queueing. | Low–medium |
| 3 — Critical path | Reviewer runs waited for checks before encountering a deterministic prompt-render failure. Runs 33245886964 and 33246465915 explicitly reached `CHECK_RUNS_WAIT_TIMEOUT` after 300s. | Perform prompt assembly/hydration preflight before check-run polling. Keep strict hydration for trusted templates; leave unresolved placeholders intact only in untrusted assembled diff content. | Approximately **300s per deterministic render failure**. | Low |
| 4 — Queueing | Seven review runs were cancelled before their first step; three waited 5,801–6,004s. Sweep runs 33249093537 and 33250276921 skipped PRs because active runs existed. | Extend existing sweep snapshots with `head_sha` and run ID, using fields already available from current API responses. Suppress only runs matching the current PR head; cancel superseded queued heads. | Could recover most of the observed **4.94h pre-step wait**. | Medium; fail safe when SHA is unknown |
| 5 — Diagnostic prerequisite | Poller p50 was 266.5s against a five-minute schedule, with serialized concurrency. | Emit `ORCHESTRATOR_PHASE_START/END` for state fetch, reconstruction, stall scan, judge, writes, and cleanup before changing cadence. | Enables identification of the dominant portion of **13,285s across 46 polls**. | None |

The existing four-way poll-test sharding is a successful critical-path fix: previous sequential runs produced eight 1,817–1,820s cancellations; current CI documents approximately 3.6× test acceleration.

## Cost Optimizations

| Rank | Evidence/root cause | Exact change | Estimated saving | Quality risk |
|---|---|---|---|---|
| 1 | Cache hit rate was 85.53%; logical OpenRouter input was 40.79M tokens. | Keep stable instructions, tool schemas, and rubric text before the cache breakpoint. Move SHAs, timestamps, diffs, check results, and memory results to a canonical dynamic suffix. Log prefix fingerprint and static/dynamic byte counts. | Reaching 90% would shift approximately **1.82M uncached tokens** to cache reads. | Low |
| 2 | Two-pass review runs logged 13–15 calls. Both small- and large-diff pass-two reasoning currently default to `xhigh`. | Canary `REVIEWER_PASS2_REASONING_SMALL=medium` for diffs below the existing 200-LOC threshold; retain `xhigh` for large/sensitive changes. | Expected **10–25% pass-two latency/completion reduction** on qualifying PRs. | Medium; monitor unique findings |
| 3 | Cancelled review runs consumed 8.15h of workflow wall time. | Apply current-head-aware queue cleanup described above. Emit cancellation reason, concurrency group, superseding run ID, and queue age. | Significant runner/queue savings; token saving is unquantified because cancelled runs lacked usage telemetry. | Low–medium |
| 4 | Copilot runs 33249586102 and 33250310937 built 45,514- and 49,338-token prompts. | Log prompt sections separately and deduplicate repeated repository instructions, check summaries, and memory excerpts before model invocation. | Potential **5–15% prompt reduction**, pending section telemetry. | Low |
| 5 | Semble reported 189,144 bytes over 18 calls and only 10.46s total query time. All 45 full-window fallbacks were contract tests, not runtime failures. | Keep Semble enabled. Add `candidate_bytes`, `selected_bytes`, `bytes_avoided`, and `chunks_considered` to prove context reduction. | Current latency cost is negligible; token benefit cannot yet be quantified. | None |

Model deletion is not justified. Moonshot accounted for 20.05M reported tokens, or 48.6%, but most were cache reads and only 39/116 usage records were numeric. Add per-model `unique_findings`, `consensus_adopted`, and `editor_actioned` metrics first.

Serena recorded zero queries, tool calls, response bytes, fallbacks, or probes. There is no evidence that it replaced downstream work in this window.

## Reliability Improvements

1. **Fix untrusted placeholder handling.**  
   - **Evidence:** Runs 33245886964, 33246465915, and 33246480829 failed with the same missing reference file.
   - **Category:** Deterministic input/rendering defect.
   - **Fix:** Fail open only for unresolved `REFERENCE_*` tokens originating from untrusted assembled content; retain strict trusted-template behavior. Emit `PROMPT_RENDER_RESULT strict=... unresolved_count=... source=untrusted_diff`.
   - **Impact:** Removes the entire observed reviewer-failure cluster.
   - **Rollback:** Restore strict mode without changing trusted rendering.

2. **Make policy-guard failures immediately actionable.**  
   - The existing annotation lost the offending call-site detail in collected logs. Emit the file, matched line, expected helper, scanned files, and guard version before exiting.
   - Expected impact: faster diagnosis and no repeated blind reruns.

3. **Instrument cancellation semantics.**  
   - `Free disk space` was merely the active step in seven cancellations, not demonstrated disk failure.
   - Emit `WORKFLOW_CANCELLATION reason=concurrency|manual|pr_closed|superseded superseding_run_id=... queue_ms=... active_step=...`.
   - Fail open when GitHub does not expose a reason.

4. **Monitor the new CI sharding fix.**  
   - Add per-shard start/end timestamps, test count, duration, slowest test, and timeout status. Current logs provide test counts but no shard durations.
   - Alert only if a shard exceeds its historical p95; preserve sequential fallback.

5. **Expose MCP availability failures.**  
   - Poll runs 33249588669, 33249951589, and 33250260532 reported `SEMBLE_ENABLED=true`, `SEMBLE_AVAILABLE=false`, and `SEMBLE_INDEX_AVAILABLE=false`, but emitted no `SEMBLE_PROBE`.
   - Emit separate install and index probes with target, result, reason, and duration. Continue failing open.

6. **Deduplicate collector inputs.**  
   - Run 33246562953 duplicated 15 OpenRouter events and two Semble events between `step-001-codex-agent.log` and step-specific logs.
   - Prefer step-specific logs when present, or add a stable telemetry `event_id`. Rollback is trivial.

`BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts were both zero, so this window shows neither rubric-pressure escapes nor collector-detected context-window pressure.

## AI Memory Health

- **Retrieval:** 10/10 valid deep-dive retrieves selected records: **100% hit rate**.
- **Budget use:** Average estimated context was **1,391.9 of 1,400 tokens (99.4%)**.
- **Keyword method:** `llm` 10, `plain` 0, `none` 0.
- **Zero-result/disabled retrieves:** None observed.
- **Push reliability:** 29 events reported push attempts; six required retries. Run 33232695908 needed three attempts for completion, but all reported success.
- **Empty learning writes:** Eight `write_lessons_learned` events produced `count=0`, `did_push=false`. Add `reason=no_candidates|deduplicated|disabled`.
- **Healthy fail-open:** Run 33249762124 emitted `finalize-task` with `reason=no_linked_issues`; this is an expected no-op.
- No evidence was available for `promote`, `compact`, or processed-command operations. Verify their telemetry emission paths.

Memory retrieval is healthy and too small to be a priority cost target. The main gap is retry-cause and relevance telemetry.

## GH API Call Audit

- **Exact call counts are not collected.** No logged rate-limit, HTTP 429, or secondary-rate-limit events were found.
- Repository hygiene is strong: `CLAUDE.md:439-473` requires reuse, batched GraphQL, cycle-local caches, and fail-open fallback.
- The review sweep already snapshots active runs per workflow/status instead of performing N×workflow lookups. With four candidates in runs 33249093537 and 33250276921, this avoids the earlier per-PR fanout pattern.
- **Highest unquantified risk:** `orchestrate_poll`, because it ran 46 times and contains multiple issue/PR/check-run loops. This is an inference from code structure, not measured call volume.

Add wrapper-level telemetry without new API calls:

- `GH_API_CALL endpoint_class=... method=... attempt=... duration_ms=... result=... cache_hit=...`
- `GH_API_SUMMARY total=... retries=... rate_limits=... permanent_failures=... cache_hits=...`
- Normalize endpoints and omit query contents, tokens, bodies, and identifiers that may contain sensitive data.

For sweep active-run checks, include run IDs, status, age, and head SHA in `AUTOFIX_SWEEP_SKIP`; these fields already exist in current responses.

## Prompt Cache & Memory System

- **Cache hit rate:** 85.5272%.
- **Cache reads:** 34,883,076 tokens.
- **Cache writes:** 0 reported. High reads with zero writes indicate provider/collector observability mismatch rather than proof that no cache entries were created.
- **Uncached prompt:** 5,902,869 tokens.
- **Context warnings:** 0.
- **Memory:** Retrievals were consistently successful but consumed almost their complete 1,400-token budget.

Likely fragmentation sources—an inference—are dynamic SHAs, timestamps, check-run snapshots, and changing memory content appearing before the cache boundary. Add:

- stable-prefix SHA/fingerprint;
- static prefix and dynamic suffix byte/token counts;
- cache lookup result per call;
- cache creation/read fields as returned by the provider;
- memory record count and score distribution.

Do not reduce the memory budget until relevance or downstream-use telemetry indicates low value.

## Orchestrator Health

- Polling completed successfully in all **46 runs**, but p50 was 266.5s and p95 482.25s. Since concurrency is serialized and the schedule is every five minutes, median utilization is roughly 89% of the interval.
- Recent polls 33249588669, 33249951589, and 33250260532 each found one active tracking issue while Semble remained unavailable.
- Review sweeps repeatedly found all candidates blocked by active runs: PRs #3883, #3882, #3880, and #3848 in run 33250276921.
- The latest promotion and forward merge were healthy: runs 33250620994 and 33250638467 completed in 35s and 41s.
- No evidence of conflict-heal loops or terminal-state corruption was supplied.

Track per poll:

- state-fetch/reconstruction/judge/write durations;
- active issue count and actionable issue count;
- skipped recovery counts by reason;
- queued poll delay;
- active review run age/head SHA;
- conflict dispatches and dedupe suppressions.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Priority fix |
|---|---|---|---|
| Clarify/plan/implement routing | Broad event fanout | 718 skipped runs | Mirror predicates in wrappers |
| Review/autofix | Two sequential review passes, long model/editor calls, queue churn | p95 6,392.6s; model gaps 500–624s; editor gaps 300–839s | Current-head cancellation cleanup; small-diff reasoning canary |
| CI | Large process-level test suite and late guards | p50 1,202s; eight historical 30m cancellations; five late guard failures | Keep sharding; move guards first |
| Orchestrate poll | Cadence nearly saturated | p50 266.5s on five-minute schedule | Add phase timers, then optimize dominant phase |
| Copilot review | Large prompt/model processing | p50 191s; 45–49k-token prompts | Section-level prompt telemetry and dedupe |
| Validate | Sparse evidence | One 409s success | Collect additional samples |
| Merge/promote | No material bottleneck | Latest runs 35–41s | No change |

Queueing and redundant run creation are safer first targets than reducing reviewer coverage.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix p95 106.5m; CI p50 20m; poller p50 4.4m.
- **Top failure modes:** five identical late CI guard failures; three identical prompt-render failures; 25 cancellations.
- **Highest-cost drivers:** 41.2M reported OpenRouter tokens, two-pass six-model review, and 8.15h of cancelled-run wall time.
- **Top actions:**
  1. Move deterministic CI guards immediately after setup and add structured failure fields.
  2. Complete untrusted-reference fail-open handling and run prompt preflight before check polling.
  3. Add head-aware cancellation plus deduplicated GH/API/model/MCP telemetry.

## Metrics Appendix

### Run outcomes

| Scope | Runs | Success | Failure | Cancelled | Other/skipped | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Overall | 1,000 | 248 | 8 | 25 | 719 | 2s | 609s |
| CI | 33 | 20 | 5 | 8 | 0 | 1,202s | 1,819.4s |
| Review/autofix | 84 | 63 | 3 | 17 | 1 | 31.5s | 6,392.6s |
| Orchestrate poll | 46 | 46 | 0 | 0 | 0 | 266.5s | 482.25s |
| Copilot reviewer | 25 | 25 | 0 | 0 | 0 | 191s | 266s |

### AI and cache metrics

| Metric | Value |
|---|---:|
| Codex calls / tokens | 22 / 32,422 |
| OpenRouter calls | 116 reported; 101 deduped |
| Prompt tokens | 5,902,869 |
| Completion tokens | 433,817 |
| Cache-read tokens | 34,883,076 |
| Cache-write tokens | 0 |
| Total tokens | 41,217,524 |
| Cache hit rate | 85.5272% |
| Numeric usage coverage | 39/116 calls |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |
| Full-window wall-clock p50 / p99 | 8,000ms / 6,731,440ms |
| Runs with assembled log telemetry | 118 |

The downloaded deep-dive `summary.json` covered 33 selected logs and therefore had a selection-biased wall-clock p50 of 561,000ms.

### Cancellation and failure clusters

| Cluster | Count | Wall time |
|---|---:|---:|
| Review cancellations | 17 | 29,346s |
| Cancelled before first step | 7 | 17,768s |
| CI poll-test cancellations | 8 | 14,548s |
| Late CI guard failures | 5 | 2,828s |
| Reviewer render failures | 3 | 2,281s |

### Semble

| Target | Calls | Bytes | Query time |
|---|---:|---:|---:|
| Reviewer context | 8 reported | 116,167 | 4,453ms |
| Overflow | 10 reported | 72,977 | 6,008ms |
| Total | 18 reported / 16 deduped | 189,144 reported / 167,878 deduped | 10,461ms |

Full assembled telemetry recorded **45 fallbacks**, all `target=overflow`, `context=contract-test`; runtime fallbacks were zero.

### Serena and MCP availability

| System/target | Queries | Response bytes | Tool calls | Fallbacks | Probe OK | Probe failed | Probe skipped |
|---|---:|---:|---:|---:|---:|---:|---:|
| Serena / no target emitted | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Semble / orchestrate-poll | — | — | — | 0 runtime | 0 | 0 | 0 |

Semble unavailability was visible only through environment booleans, not probe events. **Other MCP servers observed:** none.

### GH API signals

| Metric | Result |
|---|---|
| Exact calls | Not collected |
| Rate-limit events | 0 observed |
| Retry events | 0 observed |
| Primary structural hotspot | Scheduled orchestrate poller |
| Existing batching | Review sweep snapshots; orchestrator GraphQL prefetch/cache |
