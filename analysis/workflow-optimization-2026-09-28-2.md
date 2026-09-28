## Executive Summary

- **CI is the dominant operational failure pattern.** Nine of 14 CI runs were cancelled after 2,707–2,719 seconds; run 36383013856 ended during `lint / Orchestrate poll process unit tests`. Those nine runs account for approximately **6.8 runner-hours**. Prioritize test-progress diagnostics and enough runtime reduction to create headroom below the 45-minute limit. **Impact: high; confidence: high.**
- **Successful CI has almost no margin.** Run 36382913289 finished in 2,697 seconds, three seconds below the configured 45-minute limit. Its poll-process test step took 1,306 seconds; the promote-cycle test group took 326 seconds. Measure per-test and subprocess time before changing coverage. **Potential impact: minutes per CI run; confidence: high on the bottleneck, low on achievable savings.**
- **The sole reported failure was an authorization rejection, not a validation-test failure.** Validate run 36378523375 found a missing or ambiguous explicit-target PR binding. Subsequent cleanup steps also failed because checkout and runtime setup had not occurred. Preserve the fail-closed authorization check; make post-failure reporting safe before setup. **Impact: clearer diagnosis and fewer secondary errors; confidence: high.**
- **AI cost and cache savings cannot be priced from this window.** The assembled context reports 115 runs with log telemetry, but no measured model calls or tokens and no `cache_hit_rate`; only 15 logs were downloaded for deep dives. Instrument actual AI invocations before changing models or prompts. **Impact: enables cost decisions, savings unquantifiable; confidence: high.**
- **Semble fallback totals need qualification.** The collector reports 48 `SEMBLE_FALLBACK` occurrences, all CI contract-test fallbacks targeting `overflow`, with zero runtime fallbacks. Two runs contain both whole-job and per-step views of the same lines; the selected logs show **40 distinct test events**. Deduplicate before alerting. **Impact: fewer false rollout alarms; confidence: high.**

## Speed Optimizations

1. **Critical path — diagnose and shorten CI poll tests.** CI run 36382913289 spent 1,306 seconds in `lint / Orchestrate poll process unit tests`; run 36383013856 spent 1,274 seconds there before cancellation. Four-way sharding is **already enabled**, so adding it is not a new fix. Emit shard start, periodic completed-test count, slow subprocess/test timings, and a cancellation-safe final progress line. Then target measured slow fixtures without removing assertions. A **10% reduction in this step is about 2.1 minutes per run**; feasibility is unverified. Implementation risk: low for logging, medium for fixture changes.
2. **Critical-path cushion — profile the next test groups.** In successful CI run 36382913289, `Promote cycle and blocked-verdict unit tests` took 326 seconds and `Integration-ahead-by gate regression tests` took 175 seconds. Add per-test timing to the former and inspect repeated real waits before substituting controllable test clocks. A 25% reduction in the 326-second group would save approximately **82 seconds**, conditionally; risk: medium until timing-dependent behavior is verified.
3. **Workflow latency — separate wait from work.** `orchestrate_poll` had p50/p95 durations of 368/641 seconds across 10 runs; `review_autofix` had 13/1,412 seconds across 157. There are no selected execution traces for the long review runs, so their cause is unknown. Log queue, setup, model, retry, and merge/conflict spans before changing scheduling. Savings: **not estimable**; logging risk: low. Do not optimize the one-second skipped runs as though they were critical-path work.

## Cost Optimizations

1. **Avoid CI work that cannot finish.** The nine cancelled CI runs consumed approximately 6.8 runner-hours, though the logs do not establish why each was cancelled. Preserve the full gate, add progress/cancellation diagnostics, and investigate the measured test hotspots above. Recoverable runner time: **up to the affected work, not a guaranteed 6.8 hours**; quality risk: low for instrumentation, potentially high if tests are dropped.
2. **Measure model spend before selecting a cheaper model or reasoning level.** Review summaries for runs 36389722025 and 36389697549 show `openai/gpt-6-luna` configured for materiality and summarization, but recorded model calls and tokens are zero. Emit per-invocation model, reasoning level, phase, prompt/completion/cache tokens, and usage-availability status. Dollar savings and quality trade-offs **cannot yet be estimated**; do not step down models on configuration evidence alone.
3. **Measure context tools against downstream work.** Review run 36389722025 reports Semble enabled and Serena disabled, but this window has **zero measured `SEMBLE_QUERY` calls or logged query bytes**, and no Serena queries or response bytes. Thus neither reduced prompt expansion nor noisy low-value context can be established. Log target, request/response bytes, latency, whether the result entered the prompt, and downstream tool/model calls avoided. Savings: unknown; quality risk: low for bounded, redacted telemetry.

Repeated prompt expansion, cache-hit savings, context-budget pressure, and avoidable AI reruns likewise have no measured denominator here. Release run 36389862083 reports dispatching `test-and-mark-stable.yml` again after one failed attempt, despite its own `run_attempt=1`; log parent and dispatched-run IDs to price that cross-workflow retry rather than counting it as a GitHub run retry.

## Reliability Improvements

1. **CI timeout/cancellation — execution-budget category.** Nine CI cancellations cluster near 45 minutes, including runs 36383013856 and 36381450632 at 2,719 seconds; run 36382913289 succeeded at 2,697 seconds. Emit a warning when remaining job budget falls below the measured test-step p95, plus shard progress and cancellation cause. Optimize only verified slow tests; retain the 45-minute setting and full suite as the rollback baseline. Expected impact: potentially fewer incomplete CI gates, **rate reduction unmeasured**.
2. **Validate failure cascade — pre-setup cleanup category.** In run 36378523375, `Authorize explicit validation target` rejected a missing/ambiguous PR binding. Later steps reported absent `validate_process.sh` and memory helpers, an empty runtime path, and artifact scanning under `/boot/efi`. Keep authorization fail-closed. For `always()` steps, distinguish `not_initialized` from validation failure, require the runtime directory before writing status, and upload only a known prepared artifact path. Expected impact: removal of misleading secondary errors; rollback: restore existing reporting steps without relaxing authorization.
3. **Test-only MCP fallback noise — classification category.** CI run 36383013856 logged four distinct `SEMBLE_FALLBACK target=overflow` events from `lint / Targeted file context contract tests`, deliberately using a missing executable; the collector counted eight through duplicate log views. Classify by `context=contract-test` and deduplicate by run, step, and event identity. Alert separately on runtime fallbacks. Expected impact: more trustworthy rollout alarms; retain the existing fail-open behavior. **No probe-availability failure or broken runtime rollout is established.**

Measured `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts are zero in parsed coverage. That is **not** proof of absent policy pressure or prompt-size risk in unselected AI runs.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` entries were found in the selected deep-dive logs. Retrieve hit rate, average `estimated_tokens` versus budget, `keyword_method` distribution, zero-record retrieves, `fail_open`, `enabled=false`, and memory push-retry rates are therefore **unavailable**, not zero. The summary of clarify run 36389844224 reports ledger `push_attempts: 3`, but does not identify a memory operation or cause. Verify emission for `retrieve`, record, finalize, promote, compact, and processed-command operations on **executing** AI runs; retain operation, budget, selected-record count, keyword method, enabled/fail-open flags, and push attempts without record contents.

## GH API Call Audit

- **Measured calls are incomplete.** Validate run 36378523375 executed one *paginated PR-list operation* in `Authorize explicit validation target`; the number of HTTP pages is unavailable. Record sanitized endpoint template, page count, latency, status class, retry count, and rate-limit category. Do not cache away its independent authorization binding.
- **Instrument a plausible N-call hotspot before redesigning it.** `scripts/orchestrate_poll_process.sh` lists standalone open PRs, then conditionally reads each candidate’s full PR payload for mergeability. If N candidates are inspected, that path entails N per-PR reads in addition to the list; **N was not observed in poller logs**. Log candidate count and actual reads. If field and freshness parity can be verified, reuse a cycle-local batched result; theoretical reduction is **N − batched-call count**, with no defensible window-wide estimate yet.
- **Preserve existing hygiene.** The poller already prefetches stalled linked-PR state in batches of 25 and uses cycle-local caches. Follow `CLAUDE.md` §15: extend an existing fetch before adding calls, and retain the smallest safe fallback on cache misses. Log batch hit/miss and fallback reads rather than recommending another unmeasured prefetch. Rate-limit events and endpoint totals were not supplied.

## Prompt Cache & Memory System

`cache_hit_rate` is **unavailable**; recorded prompt, cache-write, and cache-read tokens are all zero with no measured model calls. There is no basis to diagnose unstable-prefix fragmentation, semantic-cache effectiveness, or token savings. Add per-invocation cache-read/write and prompt-token telemetry plus a non-content prefix/version identifier; compare misses by phase and prefix before moving dynamic context after stable instructions. Log `CONTEXT_BUDGET_WARN` with phase and prompt/window ratio when it occurs. Keep cache misses and retrieval failures fail-open. Token, latency, and reliability gains remain **unquantifiable until covered AI invocations are sampled**.

## Orchestrator Health

Across the window, `clarify`, `plan`, and `implement` respectively had 170/178, 172/178, and 167/174 skipped runs; `orchestrate_clarify_respond` was skipped in all 175. Recent plan run 36390064979 has an empty log archive, so skips cannot be classified as healthy event filtering versus lost work. Emit a low-cardinality skip reason and correlation ID **before job-level gates where possible**.

Review summaries show `AUTOFIX_GATE_SKIP reason=claude_fixer_awaiting_session` for PRs 4546 (run 36389722025) and 4604 (run 36389714300). Record wait age and the event that resumes each PR; a successful gate run is not evidence that review advanced. Clarify run 36389844224’s three ledger push attempts warrant retry-cause logging. Wave progression, deferrals, conflict-heal attempts, and terminal-state ages were not provided; track counts and age by state before declaring a stuck-loop pattern.

## Pipeline Flow Bottlenecks

| Stage or overhead | Observed signal | Next diagnostic or fix |
|---|---|---|
| Clarify → plan → implement | Predominantly skipped; successful clarify examples 36389846114/36389844224 took 131/124 seconds; slow plan run 36375309681 took 538 seconds; implement run 36375721209 took 1,022 seconds. | Correlate issue and dispatch IDs; log gate reason, queue/start, and productive-stage timings. |
| Review/autofix | p95 **1,412 seconds**, versus p50 **13 seconds**; awaiting-session gate skips are observed. | Separate short gate exits, active review/model time, session wait, and conflict/merge time. |
| Validate | Run 36378523375 failed authorization in **10 seconds**, before checkout. | Keep the guard; log sanitized binding result and suppress setup-dependent cleanup failures. |
| CI/compute | CI p50/p95 **2,717/2,719 seconds**; nine cancellations. | Highest end-to-end priority: shard heartbeat and measured test-runtime reduction without losing coverage. |
| Queue/retry/merge | No queue-time or merge-conflict totals; one cross-workflow release redispatch is summarized in run 36389862083. | Emit linked-run IDs and separate queued, retry-backoff, conflict-heal, and compute spans. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottleneck:** CI’s 45-minute boundary, with a roughly 21–22-minute poll-process test step in runs 36383013856 and 36382913289. **Top failure modes:** nine CI cancellations and one fail-closed validate authorization rejection with noisy post-failure cleanup. **Highest-cost drivers:** cancelled runner time; AI token spend is unmeasured.

**Prioritized actions:** (1) add CI shard heartbeats and per-test timings, then reduce verified slow fixtures; (2) make pre-checkout validate diagnostics and artifact handling safe while preserving authorization; (3) instrument actual AI usage, memory operations, API calls, and gate-skip reasons before altering model, cache, or dispatch behavior.

## Metrics Appendix

Window: **September 28, 2026, 03:50:42–07:08:55 UTC**, one repository. Collector summary has 1,000 rows but **998 unique run IDs**; runs 36383769890 and 36376203750 appear twice. Rates and duration percentiles below use the supplied row-based aggregates, not an invented deduplicated percentile.

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | Success / failure rate, all rows | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---|---|
| All workflows | 1,000 | 262 | 1 | 16 | 721 | 26.2% / 0.1% | 1 / 437 s |
| CI | 14 | 5 | 0 | 9 | 0 | 35.7% / 0% | 2,717 / 2,719 s |
| Review/autofix | 157 | 153 | 0 | 2 | 2 | 97.5% / 0% | 13 / 1,412 s |
| Orchestrate poll | 10 | 10 | 0 | 0 | 0 | 100% / 0% | 368 / 641 s |
| Validate | 1 | 0 | 1 | 0 | 0 | 0% / 100% | 10 / 10 s |

| Telemetry, assembled context | Value | Coverage caveat |
|---|---:|---|
| Runs with parsed log telemetry | 115/1,000 | Source `summary.json` has **15 downloaded/parsed deep-dive logs**; assembled context includes additional parsed recent runs. |
| Codex calls / tokens; OpenRouter calls / prompt / completion / total tokens | 0 / 0; 0 / 0 / 0 / 0 | Observed in covered logs; **not** a spend estimate for all runs. |
| OpenRouter cache-write / cache-read tokens; `cache_hit_rate` | 0 / 0; **N/A** | No measured invocation denominator. |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 1,000 / 2,719,000 | 113 assembled samples; **run-wall samples, not model-call latency**. Deep-dive-only 15-sample p50 is 2,717,000 ms. |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 | Parsed coverage only. |
| GH API calls / rate-limit events | **N/A / N/A** | One paginated validation PR-list operation observed; page and aggregate call counts unavailable. |
| Semble queries / logged query bytes | 0 / 0 | No prompt-reduction rate measurable. |
| Semble fallbacks | 48 collector occurrences; **40 distinct selected-log events** | All `context=contract-test`, target `overflow`; runtime fallbacks 0. Query-based fallback rate undefined. |
| Serena queries / response bytes / tool calls / query ms | 0 / 0 / 0 / 0 | No per-tool usage breakdown beyond **none observed**. |
| Serena fallbacks; probe OK / failed / skipped | 0; 0 / 0 / 0 | Availability was not probed in selected logs. |

| MCP target availability in selected logs | `probe_ok` | `probe_failed` | `probe_skipped` | Interpretation |
|---|---:|---:|---:|---|
| Semble `overflow` | 0 | 0 | 0 | Contract-test fallback target; **no probe result**. |
| Serena — no target observed | 0 | 0 | 0 | No availability result; review summaries report Serena disabled. |

**Other MCP servers observed:** none in the selected logs or supplied summaries. Tool-byte efficiency, memory retrieval effectiveness, queue-time distribution, actual GH API volume, and AI-token totals require collection from executing runs.
