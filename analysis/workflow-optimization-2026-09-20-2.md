## Executive Summary

- **Review/autofix queueing is the dominant bottleneck.** Agent jobs waited 31–89 minutes in runs `35464726455`, `35451202045`, and `35456048680`; 17 review runs were cancelled after consuming 13.2 hours of elapsed time. Fixing duplicate successor dispatches could save **30–160 minutes per affected PR**. **Confidence: high.**
- **Review memory telemetry is producing a failure storm.** Eight slow reviews emitted **610 separate 120-second `record-run-event` timeouts**; runs `35456048680` and `35464726455` additionally logged 13 memory-push failures after 16 retries. A per-run circuit breaker should save **minutes per review** and reduce process contention. **Confidence: high.**
- **Editor retries are overly expensive.** Five stall-guard kills occurred across runs `35456293637`, `35466097830`, and `35473950832`; two runs retried the primary editor twice before succeeding on attempt three. Immediate fallback after the first stall could save **10–25 minutes per affected run**. **Confidence: high.**
- **Prompt pressure is material despite strong cache reads.** Review emitted 13 context warnings, including prompts at 89–100% of model windows. OpenRouter reported 91.18M cache-read tokens, but `cache_hit_rate` was unavailable and 31.4% of usage calls lacked token data. Trimming dynamic context should reduce input volume **10–20%** with careful canarying. **Confidence: medium.**
- **Cost telemetry is inflated.** Run `35473950832` contains both a monolithic job log and duplicate per-step logs. This double-counted 14 OpenRouter calls and approximately **5.10M total tokens**, overstating window totals by about 4.6%. **Confidence: high.**
- **Outcome reliability is high when work starts:** 295 successes versus one failure, or 99.66% success excluding skipped/cancelled runs. However, failure `35452034565` ran 7,688 seconds with no agent log or step-level diagnosis. **Confidence: high.**

## Speed Optimizations

1. **Critical path — eliminate duplicate review successor runs**
   - **Evidence:** Five cancelled `Internal: AI Review & Autofix` runs were created 13–15 seconds before successful direct `Codex PR Self-Healing Semantic Agent` runs. Example: cancelled `35466085922` waited 9,530 seconds while `35466097830` ran successfully for 9,552 seconds.
   - **Root cause:** Productive autofix commits unconditionally bypass the peer-run check before directly dispatching `review_autofix.yml`, while the push also triggers `internal-review.yml`; both enter `pr-autofix-<PR>` with `cancel-in-progress: false`.
   - **Exact change:** Bypass the peer check only when `AUTOFIX_SKIP_SELF_TRIGGERED=true`. With the default false, let the synchronize-triggered run be the successor and suppress the direct dispatch.
   - **Savings:** 31–159 minutes of queued elapsed time per duplicate; at least 7.7 hours across the five confirmed pairs.
   - **Risk:** Low; retain current direct-dispatch behavior behind the existing self-trigger-skip setting.

2. **Critical path — switch editor model after the first stall**
   - **Evidence:** `35456293637` and `35473950832` each had two `codex_stall_killed` primary attempts; `35466097830` had one.
   - **Root cause:** `openai/gpt-5.6-sol` is retried before the configured `openai/gpt-5.5` fallback.
   - **Exact change:** Classify `stall_guard` as immediate model-fallback eligibility; keep ordinary transient retries unchanged.
   - **Savings:** Approximately 10–25 minutes on affected reviews.
   - **Risk:** Low-medium; fallback already produced accepted edits.

3. **Critical path — circuit-break memory substate writes**
   - **Evidence:** 610 timeout warnings across eight slow reviews, plus 13 authentication-related push failures.
   - **Root cause:** Frequent reviewer substates independently invoke a bounded but expensive memory-store push.
   - **Exact change:** After the first timeout/auth failure, set a run-local circuit-open sentinel, append later substates to local JSONL, and attempt one final fail-open batch flush.
   - **Savings:** Estimated 2–10 critical-path minutes plus substantially lower process and Git contention.
   - **Risk:** Low; telemetry already fails open.

4. **Critical path — shorten unchanged check-run waits**
   - **Evidence:** Runs `35451202045`, `35456048680`, and `35465649857` each reached `CHECK_RUNS_WAIT_TIMEOUT` after 300 seconds.
   - **Exact change:** Stop after three unchanged, self-excluded snapshots and record `collection_status=pending_unchanged`; retain completed failure details.
   - **Savings:** Up to 3–4 minutes on affected runs.
   - **Risk:** Medium; preserve the current 300-second behavior behind a rollout variable.

5. **Memory maintenance — batch record/promote pushes**
   - **Evidence:** Run `35479014990` spent about 418 of 502 seconds extracting five learnings. Each record and promotion took roughly 40 seconds, resulting in ten serial pushes.
   - **Exact change:** Write and promote all extracted candidates in one transaction and push once.
   - **Savings:** Approximately 5–7 minutes per maintenance run.
   - **Risk:** Low if the batch operation remains atomic and fail-open.

6. **Micro-optimization — conditionally skip disk cleanup**
   - **Evidence:** `jlumbroso/free-disk-space` consumed 40–123 seconds in sampled reviews.
   - **Exact change:** Run it only below a documented free-space threshold.
   - **Savings:** 40–120 seconds per active review.
   - **Risk:** Low with a conservative threshold.

## Cost Optimizations

1. **Fix collector deduplication before making budget decisions**
   - `35473950832` duplicated every timestamped OpenRouter, Semble, and memory event across combined and split logs.
   - Deduplicate by `(run_id, timestamp, marker, payload)` or prefer split logs whenever both formats exist.
   - **Estimated correction:** OpenRouter totals fall from 111.04M to approximately **105.93M tokens**; calls fall from 137 to approximately **123**.
   - **Quality risk:** None.

2. **Reduce dynamic review context**
   - **Evidence:** Warning prompts ranged from 187,716 to 236,553 tokens; run `35465649857` reached 199,990/200,000 tokens.
   - **Exact change:** Emit per-component token counts, cap check-run/log-tail sections, deduplicate PR body and repeated workflow instructions, and move volatile metadata after the stable cache prefix.
   - **Estimated savings:** 10–20% of review input volume; at least 0.4–0.7M tokens across the 13 warned calls.
   - **Quality risk:** Medium; preserve full context for the existing always-full path regex.

3. **Circuit-break the Mistral reviewer slot**
   - **Evidence:** Across eight deep-dive summaries, `mistralai/mistral-small-2603` succeeded once, failed three times, and was skipped after rate limits four times; 13 attempts produced one successful slot result.
   - **Exact change:** Enable the existing reviewer health circuit breaker for this slot, or remove it from the panel until its rolling success rate recovers.
   - **Estimated savings:** One to two low-value calls and several minutes per review.
   - **Quality risk:** Low; five other reviewers remain, including two with 8/8 success.

4. **Avoid repeated primary-editor calls**
   - Immediate fallback after a stall removes one or two costly calls in affected runs.
   - Exact token savings are unavailable because 43/137 reported OpenRouter calls lacked usage data.
   - **Quality risk:** Low-medium.

5. **Semble appears beneficial, not noisy**
   - Corrected unique telemetry: 24 queries, 246,593 bytes, averaging 10.3 KB/query.
   - Targets: reviewer context 8 calls/116,658 bytes; overflow 15/118,848; conflict resolver 1/11,087.
   - Only one runtime fallback occurred, `target=overflow reason=budget-exhausted`.
   - Semble is supplying bounded fragments rather than full-file expansion; retain it and deduplicate collector accounting.

6. **Serena cannot be evaluated**
   - Zero queries, probes, fallbacks, response bytes, or tool calls; observed logs explicitly had `SERENA_ENABLED=false`.
   - Emit `SERENA_PROBE target=<target> result=skipped reason=disabled` so disabled rollout is distinguishable from missing instrumentation.

## Reliability Improvements

1. **Memory write circuit breaker**
   - **Category:** Dependency/authentication and retry amplification.
   - **Fix:** Stop further remote substate writes after the first failure; flush locally at completion.
   - **Expected impact:** Removes hundreds of repeated warnings and prevents helper contention from delaying review.
   - **Rollback:** Disable batching and restore current fail-open writes.

2. **Classify pre-runner failures**
   - **Evidence:** Failure `35452034565` lasted 128 minutes, failed at `review / codex-agent`, and produced only gate logs with zero model telemetry.
   - **Likely cause:** Inference—failure while queued or before the first agent step.
   - **Fix:** Collector should emit job `started_at`, runner assignment, first-step timestamp, queue duration, and classification such as `pre_runner_failure` or `cancelled_before_first_step`.
   - **Expected impact:** Converts opaque two-hour failures into actionable queue/concurrency incidents.

3. **Enable reviewer health isolation**
   - Open the provider circuit after three recent `rate_limit`, `stall_guard`, or empty-output failures.
   - Fail open to the remaining panel; automatically probe after the existing TTL.
   - Expected reduction: fewer retry-amplified reviews without blocking output.

4. **Act on context-budget warnings**
   - All 13 warnings occurred in review/autofix; `BREAK_GLASS` count was zero.
   - These indicate prompt-size risk, not policy/rubric pressure.
   - Add a hard, fail-open context compaction step above 85% of a model window and record removed component sizes.

5. **Semble fallback behavior is healthy**
   - One runtime fallback among 24 corrected queries is rare fail-open behavior.
   - Four additional CI fallbacks are contract-test events, not evidence of a broken runtime rollout.

6. **Improve cleanup diagnostics**
   - Implement run `35478440254` logged git-submodule cleanup failure; `35478467751` reported an unsafe/unset `RUNTIME_DIR`.
   - Emit `CLEANUP_SUMMARY attempted=<n> failed=<n> unsafe_runtime_dir=<bool>` and keep cleanup non-blocking.

## AI Memory Health

- **Retrieval:** 8/8 unique slow-review retrieves selected records: **100% hit rate**.
- **Budget use:** Average estimated context was **1,391/1,400 tokens (99.4%)**.
- **Keyword method:** `llm` in 8/8 retrieves; no `plain` or `none`.
- **No zero-record, disabled, or retrieval fail-open events** were observed.
- **Write health:** Four explicit fail-open events occurred in runs `35456048680` and `35464726455`; their logs also contain 13 failed 16-attempt memory pushes.
- **Learning yield:** All 16 observed consolidate/editor `write_lessons_learned` operations returned `count=0`. Add `skip_reason=no_novel_lesson|filtered|parse_empty` to distinguish healthy deduplication from extraction failure.
- **Maintenance:** `35479014990` recorded and promoted five learnings, then compacted 504 candidates and one ledger. `prune=true` removed zero records; log the retained-reason distribution to verify this is intentional.

## GH API Call Audit

Runtime GH call counts are unavailable: no `GH_API_CALL`, `GH_API_TELEMETRY`, or `GH_API_SUMMARY` markers were emitted. No GitHub rate-limit event was identified; observed rate limits were OpenRouter/provider failures.

- **Confirmed redundant pattern:** At least five direct workflow dispatches duplicated synchronize-triggered review runs. Removing them saves at least five dispatch API calls and five queued workflow runs.
- **Probable hotspot:** Three 300-second check-run waits repeatedly queried the same SHA. An unchanged-snapshot cutoff would likely remove **2–4 logical API snapshots per affected run**, or roughly 6–12 calls across the three observed timeouts.
- **Good hygiene:** The review gate reuses `/pulls/{n}` data, and `review_autofix_sweep.yml` snapshots active runs once per workflow instead of querying per PR. This aligns with `CLAUDE.md §15`.
- **Required instrumentation:** Add one job-end marker containing logical calls, underlying requests, pagination pages, retries, cache hits, endpoint templates, and rate-limit sleep seconds. This is necessary to enforce §15’s batching and cycle-local-cache rules quantitatively.

## Prompt Cache & Memory System

- Reported cache-read tokens: **91.18M**, or 82.1% of reported OpenRouter total tokens.
- Dedup-corrected estimate: **87.58M cache-read / 105.93M total**, about 82.7%.
- `cache_hit_rate` is unavailable for the repository and review family; memory maintenance reported 0.0 for its single run.
- High cache-read volume indicates stable-prefix caching is valuable, but context warnings show dynamic suffixes have grown too large.
- **Recommended additions:**
  - `PROMPT_COMPONENT_TOKENS phase=<phase> static=<n> diff=<n> memory=<n> checks=<n> instructions=<n>`
  - Stable-prefix hash and cache breakpoint position.
  - Per-call cache status even when provider usage is unavailable.
- **Expected impact:** 10–20% lower input tokens, lower latency, and fewer context-window edge cases.

## Orchestrator Health

- `orchestrate_poll`: 76/76 successful, p50 299 seconds, p95 331.5 seconds.
- **Inference, medium confidence:** Poll runs occupying nearly the full five-minute cadence risk persistent overlap or lock contention. Only two runs had log telemetry and no poller deep dive was selected.
- Clarify, plan, and implement gates usually skip quickly: 175/181 clarify, 169/174 plan, and 165/173 implement runs were non-success “other” outcomes, mostly one-second skips.
- Exception: implement run `35478467751` took 772 seconds despite `phase_precheck reason=wrong_phase outcome=skip`.
- All eight slow-review summaries skipped the RB judge; `break_glass_count=0`.
- **Track:** tick work count, projects advanced, no-op reason distribution, lock wait, conflict-heal attempts, terminal-state count, and next scheduled action.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Priority fix |
|---|---|---|---|
| Clarify → plan | Mostly healthy gating | p50 1s; most runs skipped | Preserve current early gates |
| Implement | Agent compute and occasional late gating | Active runs 726–801s; `35478467751` wrong-phase skip after 772s | Move phase validation before setup/model work |
| Review/autofix queue | Duplicate triggers and serialization | 31–89m queue waits; 13.2h cancellations | Conditional peer-check bypass |
| Review compute | Six-model panel plus provider retries | Mistral 1/8 success; prompts up to 236k | Circuit-break unhealthy slots; trim context |
| Editor/autofix | Stall retries | Five stall kills; up to three attempts | Immediate model fallback |
| Merge/conflict | Head movement during long reviews | `35451202045` ended `stale_base_skip` after 4,343s; `35459829030` required conflict resolution | Reduce duplicate concurrency; recheck head before editor |
| CI | Long compute, no failures | 18/18 success; p50 2,130s, p95 2,394s | Collect step-level timings before changing |
| Release validation | Blocked downstream by review | `35478030497` cancelled while waiting for review/autofix | Fix review latency first |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** Review queueing/serialization, reviewer panel runtime, editor stall retries, 35-minute CI.
- **Top failure modes:** Opaque pre-agent failure `35452034565`, duplicate queued review runs, memory push timeout/auth storms, provider-specific reviewer failure.
- **Highest cost drivers:** Review/autofix OpenRouter usage, 187k–236k prompts, repeated reviewer/editor attempts, and collector double-counting.
- **Top three actions:**
  1. Make continuation peer-check bypass conditional on `AUTOFIX_SKIP_SELF_TRIGGERED=true`.
  2. Add a run-local memory-write circuit breaker and batch final flush.
  3. Deduplicate collector events and switch editor models after the first stall.

## Metrics Appendix

### Run outcomes

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Repository | 1,000 | 295 | 1 | 21 | 683 | 1s | 1,996s |
| Review/autofix | 96 | 77 | 1 | 17 | 1 | 44s | 8,270.8s |
| CI | 18 | 18 | 0 | 0 | 0 | 2,130s | 2,394.2s |
| Implement | 173 | 5 | 0 | 3 | 165 | 1s | 11s |
| Orchestrate poll | 76 | 76 | 0 | 0 | 0 | 299s | 331.5s |
| Copilot reviewer | 37 | 37 | 0 | 0 | 0 | 267s | 489.4s |

### Cost and cache telemetry

| Metric | Reported | Dedup-corrected estimate |
|---|---:|---:|
| Codex tokens | 5,304,764 | 5,304,764 |
| OpenRouter calls | 137 | 123 |
| Prompt tokens | 18,962,795 | 17,542,092 |
| Completion tokens | 897,205 | 820,665 |
| Total OpenRouter tokens | 111,035,652 | 105,934,918 |
| Cache-read tokens | 91,182,869 | 87,578,222 |
| Cache-write tokens | 10,552 | 10,552 |
| Usage unavailable | 43/137 | 39/123 |
| `cache_hit_rate` | unavailable | unavailable |
| Wall-clock p50 / p99 | 7,000ms / 10,073,100ms | unchanged |
| `break_glass_count` | 0 | 0 |
| `context_budget_warn_count` | 13 | 13 |

### MCP telemetry

| System/target | Queries | Bytes | Fallbacks | Probe OK | Probe failed | Probe skipped |
|---|---:|---:|---:|---:|---:|---:|
| Semble reviewer-context | 8 | 116,658 | 0 | — | — | — |
| Semble overflow | 15 | 118,848 | 1 | — | — | — |
| Semble conflict-resolver-context | 1 | 11,087 | 0 | — | — | — |
| Semble CI contract tests | 0 | 0 | 4 | — | — | — |
| Serena, no target observed | 0 | 0 | 0 | 0 | 0 | 0 |

- Collector-reported Semble totals were 27 calls/276,800 bytes; unique corrected totals are 24/246,593.
- Serena response bytes, query time, and tool-call breakdown: all zero.
- Other MCP servers observed: none.

### Data gaps

- Full log telemetry existed for 115/1,000 runs; cost telemetry existed for 18 runs.
- No GH API call-count telemetry.
- No deep-dive CI or orchestrator-poll logs.
- `cache_hit_rate` absent for review/autofix.
- Failure `35452034565` has no agent-step log.
