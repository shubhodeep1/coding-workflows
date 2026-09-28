## Executive Summary

- **Move the prompt-budget check earlier.** CI run `36411570320` failed after 1,326 seconds because `prompts/mode-judge-review-blocked.txt` had 264 lines against a 250-line limit. Running the existing check immediately after checkout could save **up to ~21 minutes on a similar failure**, with no expected green-run speedup. **Confidence: high.**
- **Review execution dominates active review latency.** In review/autofix run `36416865588`, `Run reviewer models` took 2,345 of 2,743 seconds; the `minimax/minimax-m3` slot encountered repeated stall guards across two passes. Trial a shorter second-attempt stall cap while retaining the existing fallback and reviewer-quorum rules. **Potential impact: ~6–12 minutes on a similarly stalled run; confidence: medium.**
- **Published cost totals are inflated by duplicate log views.** Run `36416865588` appears in both combined and split step logs with slightly different subsecond timestamps. Counting its primary log once changes the window’s reported OpenRouter total from **33.63 million to 26.51 million tokens** and calls from **61 to 45**. This is an **accuracy fix, not a usage saving**. **Confidence: high.**
- **Semble alarms are test noise in this sample.** The published 44 `SEMBLE_FALLBACK` events reduce to 32 distinct events; all are CI contract tests deliberately targeting a missing executable, not observed runtime outages. Three distinct `reviewer-context` queries had no observed runtime fallback. **Impact: fewer false availability alarms; confidence: high for inspected logs.**
- **GitHub API and orchestration diagnoses lack call-site evidence.** No per-endpoint call counts, cache-hit counts, or rate-limit headers were available for the 12 poller runs or the 15-candidate all-skipped sweep in run `36423773471`. Add bounded, structured counters before changing batching or poll frequency. **Impact: prevents speculative changes; confidence: high.**

## Speed Optimizations

1. **Critical path — bound repeated reviewer stalls.** Run `36416865588` spent **2,345 seconds** in `review_codex-agent / Run reviewer models` (85.5% of its run). Its second `minimax/minimax-m3` attempts followed earlier failures and ended in `stall_guard` after roughly 11 minutes in each pass. **Root cause:** long retries of an already troubled slot. **Change:** trial a configurable five-minute cap *only on a second attempt after a stall guard*, then use the existing failback; log slot attempt duration, termination reason, and whether quorum was met. **Estimated saving:** roughly six minutes per affected second attempt, up to ~12 minutes in a run with this pattern; not a prediction for ordinary reviews. **Risk: medium**—compare review findings and quorum before retaining the cap.
2. **Critical path — measure and rebalance CI test shards.** In successful CI run `36417072376`, `lint / Orchestrate poll process unit tests` took **1,293 seconds**, 48% of its 2,706-second run, despite the existing four-shard configuration. **Root cause:** expensive poller subprocess tests; shard imbalance is *unverified*. **Change:** emit shard test count, elapsed time, slowest test durations, and completion reason, then rebalance only if a shard is consistently last. **Estimated saving:** unknown until shard timings are collected; a **0–200-second trial target**, not a measured gain. **Risk: low** for logging, medium for redistributing tests.
3. **Critical path — reduce serial memory-write time.** In review run `36416865588`, memory start-event, candidate, and completion steps took **56.9, 57.6, and 57.1 seconds**, respectively, with one push attempt each. **Root cause inference:** separate memory operations incur substantial step-level overhead; the logs do not isolate Git push time. **Change:** log operation versus push duration; if semantics permit, commit the candidate and completion event in one idempotent, fail-open write without delaying the start event. **Estimated saving:** up to ~57 seconds per qualifying active review. **Risk: medium** until independent failure behavior is verified.
4. **Failure-path win — run the cheap prompt gate first.** The `Prompt tier budget` step failed run `36411570320` near its end; the local gate uses the standard library and currently appears after other CI work in `.github/workflows/ci.yml`. **Change:** place the unchanged gate after checkout/Python setup, without relaxing its limit. **Estimated saving:** up to ~21 minutes per repeat; approximately zero on passing runs. **Risk: low.** The currently inspected prompt is at 250 lines, so this is prevention, not evidence of an ongoing failure.
5. **Micro-optimization — avoid work for ineligible sweep candidates.** Review sweep `36423773471` spent about **29 seconds** processing 15 candidates and skipping all 15. **Change:** log time spent fetching, eligibility-checking, and dispatching; reuse already-fetched eligibility state to short-circuit where possible. **Estimated saving:** at most the observed 29 seconds for an equivalent all-skipped sweep, likely less. **Risk: low** if dispatch eligibility remains unchanged.

## Cost Optimizations

1. **First correct the accounting baseline.** Across the three token-bearing review runs (`36405519570`, `36407481306`, `36416865588`), the collector reports **61 calls and 33,634,671 tokens**; counting combined logs once gives **45 calls and 26,513,170 tokens**. The collector’s structured-event dedupe compares full timestamped lines, while combined and split copies in run `36416865588` differ by microseconds. **Change:** match structured events within their job/step hierarchy using normalized event content and bounded timestamp matching; retain genuinely repeated events and add regression tests. **Estimated real token/dollar saving: zero**; measured reporting correction: **7,121,501 tokens**. **Quality risk: none** to review behavior.
2. **Target costly reviewer retries, not blanket model downgrades.** The three active reviews used **14, 15, and 16 distinct model calls**. Run `36416865588` records `minimax/minimax-m3` stall, server-error, failback, and model-provider rate-limit classifications; its final slot failed in both passes. The workflow already has review tiers and cheaper-reasoning retry settings. **Change:** measure tokens and elapsed time by slot/attempt and pilot the bounded retry change above; use existing tier routing for smaller diffs only after logging eligibility and comparing findings. **Estimated saving:** up to the calls and elapsed time of avoided stalled attempts; their token and dollar cost cannot be established because usage is unavailable for **6 of 45 distinct calls**, and prices are absent. **Quality risk: medium**; preserve reviewer quorum and compare outcomes. Do not lower reasoning globally on this evidence.
3. **Measure prompt components before trimming context.** Distinct `CONTEXT_BUDGET_WARN` events occurred once in `36405519570` and twice in `36416865588`, at **155,293; 161,511; and 161,934 prompt tokens** against a 200,000-token window. **Root cause of excess is unmeasured.** Log byte/token estimates separately for instructions, diff, Semble output, memory, and prior-round material; remove verified duplication while retaining relevant evidence. **Estimated saving:** unquantifiable now; a 5% reduction in the **8.50 million recorded uncached prompt tokens** would represent ~425,000 tokens *if* attainable. **Quality risk: medium** if evidence is pruned without a review-quality comparison.
4. **Keep MCP and skipped-run costs in perspective.** Three distinct `SEMBLE_QUERY target=reviewer-context` events supplied **40,912 logged bytes**, 12 chunks each, without observed runtime fallback. There is no before/after prompt-size baseline, so reduced prompt expansion is **not established**; log source bytes and bytes actually inserted into prompts. Serena had **zero calls or response bytes**, and recent review summaries report it disabled, so replacement of downstream work cannot be assessed. Awaiting-session gate runs such as `36424279556` and `36424276654` used no recorded model tokens; optimizing them chiefly saves Actions time, not measured model spend.

## Reliability Improvements

1. **Preflight prompt invariants without weakening them.** The sole recorded failed run, CI `36411570320`, ended at `lint / Prompt tier budget`: **264 lines exceeded the DEFAULT limit of 250**. **Category:** deterministic validation, surfaced late. **Fix:** move the same gate earlier and emit prompt name, tier, measured lines, limit, and elapsed-before-failure. **Expected impact:** similar violations fail promptly rather than after long CI work; the one observed failure does not establish a recurring failure rate. **Rollback:** restore step order; retain the limit.
2. **Make reviewer fallback outcomes explicit.** In `36416865588`, `minimax/minimax-m3` exhausted a slot after classified failures including `stall_guard`, `server_error`, and a **model-provider** `rate_limit`; `x-ai/grok-4.20` also had a non-retryable failure. **Category:** model execution/retry, not proven GitHub API throttling. **Fix:** emit per-pass slot attempts, cumulative wait, fallback model, terminal reason, and quorum result; trial the bounded second-attempt cap. **Expected impact:** less exposure to repeated stalls, with failure-rate impact unmeasured. **Rollback/fail-open:** restore the prior cap and retain existing fallback/quorum behavior.
3. **Classify cancellations separately from failed steps.** Review/autofix had **6 cancellations**; runs `36424344199`, `36423550826`, and `36423386421` were cancelled while `Free disk space` was the last observed step. That does **not** show disk cleanup failed. **Category:** cancellation provenance gap. **Fix:** log cancellation/concurrency reason, last started step, and whether review execution began; never auto-retry solely from the last-step label. **Expected impact:** fewer false failure diagnoses and avoidable reruns; direct failure-rate improvement unknown. **Rollback:** logging-only.
4. **Separate synthetic MCP fallback from availability.** The collector reports **44 Semble fallbacks**, all tagged `context=contract-test`; inspection yields **32 distinct** events at `target=overflow`, with **0 observed runtime fallbacks**. **Category:** telemetry classification/deduplication, not evidence of a broken rollout. **Fix:** deduplicate split logs and exclude contract-test events from production availability alerts, while retaining their test counts. No `SERENA_PROBE` was observed; do not interpret zero failures as a successful probe. **Expected impact:** removes these 32 synthetic events from runtime-alert consideration. **Rollback:** retain raw events alongside classified counts.
5. **Expose release retry causes.** Auto-release run `36424440238` succeeded in 20 seconds, but its `release-check` summary says `test-and-mark-stable.yml` already had **two failed attempts against a budget of three**. **Category:** downstream gate retry, cause unavailable. **Fix:** log target run conclusion, retry reason class, attempt, remaining budget, and terminal decision. **Expected impact:** earlier identification of repeat failures; no justified rerun-rate estimate. **Rollback:** logging-only.

`BREAK_GLASS` was **0** in collected telemetry. The **3 distinct** context warnings indicate prompt-size pressure, not demonstrated policy or rubric pressure.

## AI Memory Health

In the three inspected active review logs (`36405519570`, `36407481306`, `36416865588`), `AI_MEMORY_TELEMETRY` contains **3 `retrieve` operations: 3/3 hits (100%)**, selecting **25, 25, and 23 records**. Estimated context averaged **1,390 tokens against a 1,400-token budget** (99.3%); `keyword_method` was **`llm`: 3, `plain`: 0, `none`: 0**. No inspected retrieval returned zero records, reported `enabled: false`, or reported `fail_open: true`. Each run also logged a start event, candidate, and completion event with **one push attempt** apiece; no high push-retry count was observed.

**Recommendation:** retain retrieval—it is small beside the warned review prompts—but log retrieval duration and candidates considered, plus explicit `fail_open` and push-duration fields. The ~57-second memory steps in `36416865588` warrant separating operation time from push time. `finalize-task`, `promote`, `compact`, and processed-command operations were not present in these selected logs; sample their owning workflows before making system-wide memory-health claims.

## GH API Call Audit

**No defensible GitHub REST/GraphQL call-count or rate-limit total is available in this window.** Model-provider `rate_limit` in review run `36416865588` must not be counted as a GitHub event. CI dry-run label messages are likewise not production API calls.

| Candidate hotspot | Evidence | Smallest safe audit/change | Estimated calls saved |
|---|---|---|---|
| Review sweep | `36423773471`: 15 candidates, all skipped, ~29-second sweep; `36424235790`: 17 candidates, 11 dispatched, 4 active-skipped, 2 `skip_ai`-skipped | Emit fetch, eligibility, and dispatch call counts and elapsed times; reuse fetched candidate state before proposing batching | **Unknown**; zero if already cached |
| Orchestrator poll | 12 successful runs; p50 **390 seconds**; no poller step/API logs in the deep-dive set | Emit endpoint-template counts, retry counts, and hit/miss counts for `ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`, and `_candidate_details_json` | **Unknown** |
| Issue–PR status sync | 12 successful runs; p50 **96 seconds**, p95 **139.3 seconds**; no call-site breakdown | Count PR/issue lookups by endpoint template and cache outcome before changing the loop | **Unknown** |

Repository §14 in `unattended_system_instructions.md` already requires checking existing calls, preferring the batched GraphQL helpers `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql`, reusing cycle-local caches, and failing open to the smallest legacy call on cache miss. Preserve those contracts. **Conditional illustration only:** replacing 15 verified per-candidate reads with one batch could avoid up to 14 requests; the sweep logs do *not* establish that 15 reads occur.

Add one redacted aggregate per job/step: endpoint **template**, calls, cached hits, retries, status classes, elapsed milliseconds, and rate-limit remaining/reset. Exclude response bodies, credentials, and raw query parameters. This would make call-count and rate-limit-risk reductions measurable without new infrastructure.

## Prompt Cache & Memory System

The corrected distinct-call sample records **17,583,921 cache-read tokens**, **8,499,611 prompt tokens**, and **0 reported cache-creation tokens**. Cache reads are **67.4% of those recorded input-token categories**, **not** a measured `cache_hit_rate`: that field is **null** because six distinct calls have unavailable usage. Zero recorded creation tokens also does not prove cache creation never occurs. In `36416865588`, reviewer-slot telemetry marks Gemini Flash Lite cache status `unsupported` while other inspected slots report `supported`; compare cache behavior by model rather than aggregating all slots as misses.

**Recommendation:** emit per-call usage availability, model, pass, eligible prefix hash/length, cache-read and creation tokens, and a reason when unsupported. Keep stable reviewer instructions ahead of variable PR/diff, Semble, and memory material **if a prefix comparison confirms fragmentation**; dynamic-prefix fragmentation is presently an inference, not a measured cause. Log component sizes to test whether prompt growth behind the three distinct context warnings is eroding reuse. Keep memory’s observed ~1,390-token retrieval intact unless component evidence shows low value. Token, latency, and reliability gains from prefix changes remain unquantified until cache-hit and prompt-component coverage improves.

## Orchestrator Health

The source-repo window contains **12 successful orchestrator-poller runs** (p50 **390 seconds**, p95 **460.9 seconds**) but no selected poller step logs. `plan` and `implement` each have **161 skipped runs and no successes in this window**; `workflow_failure_heal` has **36 skipped runs**. Skips are outcomes, **not evidence of stuck projects**. Review gate summaries for PRs `4546` and `4590`, among others, explicitly say `claude_fixer_awaiting_session`; they show waiting decisions but not eventual session completion.

**Recommendation:** emit one state-transition record per project tick with prior/new phase, wave, deferral reason, oldest-wait age, judge attempt/result, conflict-heal attempt, and terminal reason. Track *waiting-to-resumed time*, repeated same-state ticks, wave advancement, judge cycles, and conflict retries. Do not shorten poll intervals or force retries until those indicators distinguish useful waiting from a stall. No clarification-loop, merge-conflict, or terminal-state rate can be calculated from the supplied rows.

## Pipeline Flow Bottlenecks

| Flow component | Observed bottleneck | Queue / compute / retry / merge distinction | First action |
|---|---|---|---|
| Clarify → plan → implement | Clarify: **22 successes**, **151 skips**; plan and implement: **161 skips each** | Active clarify run `36423754260` took 201 seconds; these rows cannot reconstruct cross-run handoffs | Log correlated task and phase-transition timestamps, including skip reasons |
| Review/autofix | Family p50 **13 seconds** is dominated by quick gates; p95 **1,899 seconds**; active run `36416865588` spent **2,345 seconds** in reviewer models | Model compute/retry is observed; three ~57-second memory steps are additional step time | Bound measured stalls; time memory writes |
| Validate/CI | CI p50 **2,698 seconds**; run `36417072376` spent **1,293 seconds** in poller tests | Test compute is observed; run `36411570320` demonstrates late deterministic failure | Log shard timings; move prompt preflight early |
| Orchestrate/merge | Poller p50 **390 seconds**; release gate in `36424440238` reports two prior failed attempts | Poll compute versus API wait and merge/conflict overhead cannot be separated | Log tick-stage, API, retry, and conflict timings |

The supplied `created_at` and `run_started_at` rows yield zero-second differences for these families, but do **not** provide a reliable runner-queue breakdown. Log job queued/start and first-step timestamps before attributing delay to queueing. Run rows also lack a common task identifier for calculating end-to-end clarify-to-merge latency.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** CI p50 **2,698 seconds** (`36417072376` poller-test step: **1,293 seconds**); active review model execution (`36416865588`: **2,345 seconds**); poller p50 **390 seconds**. **Top failure modes:** one late prompt-budget CI failure (`36411570320`), classified reviewer stalls/fallbacks (`36416865588`), and ambiguous cancellation last-step labels—not evidence of disk failures. **Highest recorded cost driver:** three active review runs account for all **26.51 million distinct-log OpenRouter tokens** in the inspected cost-bearing sample.

**Top three actions, in order:** (1) move the unchanged prompt-budget check to CI preflight; (2) fix combined/split telemetry deduplication and add reviewer-slot/shard timings; (3) trial a bounded second-attempt reviewer stall cap while preserving fallback and quorum.

## Metrics Appendix

**Window:** 1,000 Actions runs, one repository. Percentages below use all runs unless noted; skips and cancellations are not failures.

| Scope | Runs | Success | Failure | Cancelled | Skipped | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| All workflows | 1,000 | 310 (31.0%) | 1 (0.1%) | 12 (1.2%) | 677 (67.7%) | 2 / 297 s |
| CI | 18 | 16 | 1 | 1 | 0 | 2,698 / 2,790.9 s |
| Review/autofix | 172 | 160 | 0 | 6 | 6 | 13 / 1,899.1 s |
| Orchestrator poll | 12 | 12 | 0 | 0 | 0 | 390 / 460.9 s |
| Clarify | 173 | 22 | 0 | 0 | 151 | 1 / 159 s |
| Issue–PR status | 12 | 12 | 0 | 0 | 0 | 96 / 139.3 s |

The failure rate among **success-or-failure conclusions only** is **1/311 (0.32%)**; treating 677 skips as failures would misstate health.

| Cost/cache metric | Collector-reported | Distinct primary-log count where applicable |
|---|---:|---:|
| OpenRouter calls; usage available / unavailable | 61; 52 / 9 | **45; 39 / 6** |
| Prompt / completion / total tokens | 11,299,134 / 535,332 / **33,634,671** | 8,499,611 / 429,638 / **26,513,170** |
| Cache read / reported creation tokens | 21,800,205 / 0 | 17,583,921 / 0 |
| `cache_hit_rate` | **null** | **not measurable** with unavailable usage |
| `break_glass_count` / `context_budget_warn_count` | 0 / 5 | 0 / **3** |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | **7,000 / 3,125,560** in assembled context, 113 samples | Folder `summary.json`: **2,721,500 / 3,478,400**, 16 samples; different coverage, not a deduped comparison |

| MCP metric or target | Collector-reported | Distinct inspected events | `probe_ok` / `probe_failed` / `probe_skipped` |
|---|---:|---:|---:|
| Semble `reviewer-context` queries; logged bytes | 4; 54,851 B | **3; 40,912 B**, 12 chunks each | 0 / 0 / 0 observed |
| Semble `overflow` contract-test fallbacks | 44 | **32** across 8 CI runs; runtime fallbacks **0** | 0 / 0 / 0 observed |
| Serena queries / response bytes / tool calls / fallbacks | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0; **no per-tool breakdown available** | 0 / 0 / 0 observed; no target emitted |

**Other MCP servers observed:** none in inspected structured lines. **GitHub API calls, endpoint hotspots, GitHub rate-limit events, cache hits, and API retry counts:** not collected at usable call-site granularity. The assembled context reports **115** runs with log telemetry, while the folder’s `summary.json` reports **16**; reconcile that coverage distinction before comparing wall-clock percentiles or treating zero-valued rows as full observations.
