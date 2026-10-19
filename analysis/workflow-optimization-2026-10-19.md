## Executive Summary

- **Data gap:** the run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was not mounted in this sandbox, so `summary.json`, `errors/`, `slow/` and `recent/` could not be read. Everything below comes from the embedded `analysis_context.json`: per-family aggregates, `cost_telemetry` and `log_summary` fields. I also checked the repo sources (`scripts/cost_audit.py`, `scripts/semble_helpers.sh`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/ci.yml`). `insufficient_data=false`, but there are no step-level excerpts for the one failure (run 39117893124). **Confidence: high** that the gap exists.
- **The poller takes longer than its own schedule.** `internal-orchestrate-poll.yml` runs every 5 minutes (`*/5 * * * *`). `orchestrate_poll` takes p50 575s and p95 1,046s. Of 103 runs, 22 (21%) were cancelled. The job uses `concurrency: ai-orchestrate-poll-${{ github.repository }}` with `cancel-in-progress: false`. In GitHub's concurrency model a newer pending run replaces an older pending one, so roughly every other tick is thrown away (inference). Each run also writes about 827k characters of log, which hides all of its own telemetry. **Impact:** about 1,400 dead runs a week and no way to see where poll time goes. **Confidence: high** on the numbers, medium on the mechanism.
- **Semble is not working in `implement`.** It logged 9 runtime fallbacks and 0 queries across the 4 implement runs with telemetry (39116207667: 4, 39120466094: 1, 39116188254: 2, 39115643933: 2). In `review_autofix` it bootstrapped 5 out of 5 times with 0 fallbacks. The poller env shows `SEMBLE_AVAILABLE: false` and `SEMBLE_INDEX_AVAILABLE: false` (39177984483, 39178539462). This looks like a broken rollout hidden by fail-open, not occasional noise. **Confidence: medium.** The fallback `reason=` field is emitted but not aggregated, so the root cause is unconfirmed.
- **Review/autofix is the main token cost.** It used 54.9M OpenRouter tokens over 54 calls in just 5 runs. Of that, 8.15M was uncached prompt, 2.60M cache write and 43.76M cache read. The derived hit rate is 80.3% (cache read ÷ (cache read + prompt + cache write)). Per run it ranges from 63.9% (39126581381) to 84.4% (39116883807). Bringing the weakest runs up to the best run's level would save about 1–1.3M uncached prompt tokens per affected run (estimate). **Confidence: medium.**
- **Reliability is otherwise good.** There was 1 failure in 1,000 runs: CI 39117893124, job `static-checks`, step `Validation self-test unit tests`, 895s. That step runs 10 test files one after another under `set -euo pipefail` (`ci.yml:461-473`), so the first failing file hides the results of the rest. Adding per-file result lines would make this kind of failure triage in one pass. **Confidence: high.**
- **There is plenty of GitHub API headroom.** Sweep `GH_PAT_BUDGET` lines show the shared token at 4,631–4,752 remaining just after each hourly reset and 3,853–3,969 about 30 minutes later. That is roughly 680–850 calls per half hour, around 1,400–1,700 per hour, or about 30–35% of a 5,000 budget (derived). There were no 429s. The poller's own start/end budget lines exist (`orchestrate_poll.yml:116`, `:1982`) but are not surfaced in any summary. **Confidence: medium.**
- **AI memory telemetry is invisible.** No `AI_MEMORY_TELEMETRY:` lines appear in any `log_summary`. Most likely they are inside the truncated poller logs. **Confidence: low** on whether they are being emitted at all.

## Speed Optimizations

**1. Poller run length versus the 5-minute schedule (critical path for orchestration latency)**
- **Evidence:** p50 575s, p95 1,046s, average 564s, 22 cancelled out of 103. In every summarised run the `poll` step is 6.5–10.7 minutes of the total: 39159330715 ran 10m40s, 39178539462 about 10m, 39176089260 about 6.5m. The slowest pollers were 39123141017 (1,338s), 39128482980 (1,118s) and 39125267133 (1,088s).
- **Root cause:** not knowable from this data, because about 827k characters of `poll` output are truncated in every run and the poller logs no per-phase timing. A grep of `scripts/orchestrate_poll*.sh` found no phase-timing prefix.
- **Exact change:**
  1. Have `scripts/orchestrate_poll_process.sh` print one summary line at the end of the poll step, for example `ORCH_POLL_PHASE_SUMMARY prefetch_ms=… candidates_ms=… conflict_heal_ms=… judge_ms=… dispatch_ms=… gh_calls=… total_ms=…`. A new log prefix needs an `agents.d/<id>-orch-poll-phase-summary.md` fragment under `Stable log prefixes (contractual)`, per §25.
  2. Once a week of phase data exists, either cut the dominant phase or set the cron to `*/10`. Because of the cancellations, the effective cadence is already about 10 minutes.
- **Estimated savings:** the cron change alone removes about 21% of poller runs (the cancelled ones) with no loss of throughput. The phase data is needed to target the 9.5-minute body.
- **Risk:** low for the logging change. Low to medium for the cron change, which is a behaviour change, so only make it after the phase data confirms the cadence.

**2. Waiting for hosted runners (critical path)**
- **Evidence:** pollers waited for `ubuntu-latest` for 4m40s (39161191425), about 3.5m (39160588784), about 2.5m (39178539462), 2m28s (39165729819), 2m18s (39147329180), about 2m (39151636015), 95s (39156489852), 64s (39165113922) and 50s (39155840560). Short jobs such as `cancel_on_pr_close` and the sweep waited 0–2s.
- **Root cause (inference):** account-level runner concurrency is saturated at times. Long CI runs (p50 852s), review/autofix runs (up to 2,144s) and implement runs (up to 2,086s) overlap with a poller that is busy nearly all the time.
- **Exact change:** measure first. Have the collector record `started_at − created_at` per job from the jobs data it already fetches; no new API call. Report it as `queue_wait_p50_ms` / `queue_wait_p95_ms` per family.
- **Estimated savings:** 1–4.5 minutes per poll tick during peaks, once the contention is understood.
- **Risk:** none for the collector change.

**3. Review/autofix and implement wall clock (critical path, but cannot be broken down yet)**
- **Evidence:** review/autofix p95 1,626s, with the slowest runs at 2,144s (39119503954), 1,761s (39126581381) and 1,660s (39116883807). Implement ran 1,567–2,086s.
- Semble bootstrap totals 215,944ms over 5 runs, about 43s each, which is 2–2.7% of review wall clock. That is a micro-optimisation and not worth pursuing ahead of the items above.
- **Change:** none until step timings are available from the deep-dive logs.

**4. Validate has a suspiciously fixed duration (micro, inference)**
- The 4 validate runs took 806–813s (39128274068, 39124145439, 39124390159, 39128420892). A spread that tight suggests a fixed wait or poll timeout rather than compute.
- **Next step:** check the validate step logs for a sleep or timeout loop.

## Cost Optimizations

**1. Prompt-cache variance in review/autofix (largest token lever)**
- **Evidence:**

  | Run | Calls | Hit rate | Uncached prompt | Cache write |
  |---|---|---|---|---|
  | 39116883807 | 11 | 84.4% | 1.42M | 0.49M |
  | 39119503954 | 11 | 77.4% | 1.73M | 0.47M |
  | 39126604204 | 10 | 77.6% | 1.54M | 0.56M |
  | 39126581381 | 10 | 63.9% | 1.81M | 0.52M |
  | 39117893207 | 12 | null | 1.65M | 0.56M |

  For 39117893207 the derived hit rate is 86.8%, but the reported `cache_hit_rate` is null. That run also had 1 usage-unavailable call.
- **Root cause (inference):** 0.47–0.56M tokens are written to cache in every run, which means each run re-establishes its prefix. Likely causes are per-PR content (diff, Semble block, findings) placed before the stable rubric or system text, and reviewer/aggregator/editor stages that don't share a prefix.
- **Exact change:** put the static role text and rubric first and all per-run content (diff, Semble context, prior findings) after it. Add a `prefix_sha` / `prefix_bytes` field to the existing per-call usage line, so cache misses can be traced to prefix churn.
- **Estimated savings:** lifting 63.9% to about 84% saves roughly 1.3M uncached tokens on a run like 39126581381, around 20% of its input.
- **Quality risk:** none if only the order of content changes.

**2. Semble cost and value**
- `review_autofix` made 5 queries totalling 78,029 bytes (about 15.6KB each), with 36 sources and 1,041 static duplicate bytes (1.3%, all in 39126581381). Against about 1.6M uncached prompt tokens per run, Semble adds well under 1% of prompt volume. It is not driving prompt growth, and duplication is negligible.
- Whether it actually *reduces* expansion can't be shown without a run that had Semble disabled to compare against.
- `implement` made 0 queries, so it gets no value from Semble (see Reliability 2).
- Serena made no calls anywhere, so there is nothing to evaluate.

**3. Implement Codex token telemetry looks wrong (data quality)**
- All 4 implement runs report exactly `codex_calls=11` and `codex_tokens_used=1,322,139`: 39116207667, 39120466094, 39116188254, 39115643933. Four different runs with identical counts is implausible.
- **Likely cause:** the parser picks up a cumulative or shared value rather than per-run usage.
- **Change:** in `cost_audit.py`, flag runs whose `(codex_calls, codex_tokens_used)` pair is identical across different run IDs, and print a `codex_usage_suspect_duplicate=N` count in the report.
- The 5.29M Codex total should not be used for cost decisions until this is resolved.

**4. Scheduled no-op runs (minor)**
- `cancel_on_pr_close` ran 81 times, mostly scheduled, at about 22s average. Every visible run logged `MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0`.
- `workflow_failure_heal_intake` recovery scans logged `count=0` (for example 39177860637 and 39164778973).
- The cost is about 30 runner-minutes a day. Only worth changing if runner contention (Speed 2) turns out to be real.

## Reliability Improvements

**1. CI `static-checks` → `Validation self-test unit tests` (1 of 28 CI runs, 3.6%)**
- **Evidence:** run 39117893124, 895s, 4 Semble contract-test fallbacks. Those are classified as contract-test, so they are expected. It was created at the same moment (10:05:36Z) as review run 39117893207, so it is likely the same PR head (inference).
- **Root cause category:** test failure. The specific file is unknown because there is no excerpt and the step stops at the first non-zero exit (`ci.yml:461-473`).
- **Diagnostic change:** run each test file inside a loop that records its result and prints `VALIDATION_SELFTEST_RESULT file=<f> rc=<rc> ms=<ms>`. Then print one summary line and exit non-zero if any file failed. The step still fails exactly as before.
- **Impact:** triage takes one pass instead of a rerun per hidden failure.
- **Rollback:** a single-step revert.

**2. Semble `implement` fallbacks: 9 out of 9, 0 queries**
- **Classification:** a broken rollout masked by fail-open, not healthy occasional fallback. The same Semble feature bootstraps fine in review/autofix (5 out of 5, about 43s each). The poller env shows the binary and index as unavailable (`SEMBLE_BIN` empty in 39177984483).
- **Root cause:** unconfirmed. `semble_helpers.sh` emits `reason=` as one of `binary-unavailable`, `index-unavailable`, `lazy-bootstrap-failed`, `timeout`, `exit=…` or `empty-result` (lines 214–367). `cost_audit.py` breaks fallbacks down only by `target` and by contract-test versus runtime (lines 775–780), never by reason.
- **Diagnostic change:** add a `semble_fallback_reasons` breakdown keyed by `reason` alongside `semble_targets` in `cost_audit.py`. That is an additive field, so the §10 rule against renames is respected.
- **Likely fix (inference):** make `implement.yml` take the same lazy-bootstrap path as `review_autofix.yml`.
- **Fail-open:** keep the current fallback behaviour unchanged.

**3. `cache_hit_rate` is null when any call lacks usage data**
- **Evidence:** 39117893207 has `or_usage_unavailable_calls=1` and `cache_hit_rate=null` despite 14.49M cache-read tokens. The per-repo and per-family `cache_hit_rate` is also null, even though the components exist.
- **Fix:** compute the rate from calls that do have usage, and print `cache_hit_rate_coverage=available/total` next to it.
- **Impact:** the main cache metric becomes trackable.

**4. `CONTEXT_BUDGET_WARN`: 1 occurrence, `BREAK_GLASS`: 0**
- The one warning was in review run 39117893207. It is also the review run with the most cache-read tokens (14.49M) and the most calls (12).
- This points to prompt size, not pressure on policy or rubric. One occurrence in 55 runs is not systemic; watch it weekly.

**5. Node.js 20 deprecation warning**
- Every poller run warns that `actions/cache/restore@v4` is being forced onto Node.js 24. It is harmless today.
- Pin to a Node 24-native release when one is available.

## AI Memory Health

There are no `AI_MEMORY_TELEMETRY:` lines in any `log_summary` or `cost_telemetry` field, and the deep-dive logs were not mounted. Retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, `fail_open` and `enabled:false` counts, and push-retry counts **cannot be computed**.

The most likely place for these lines is the roughly 827k truncated characters of each poller log.

**Recommendations:**
1. Have the collector grep the full untruncated step logs for `AI_MEMORY_TELEMETRY:` before truncating, and aggregate counts per `op`.
2. Include a memory roll-up in the proposed `ORCH_POLL_PHASE_SUMMARY` line: `mem_retrieves=… mem_hits=… mem_fail_open=…`.

## GH API Call Audit

| Source | Evidence | Assessment |
|---|---|---|
| `review_autofix_sweep` / `sweep` | `used_in_job` 2–11 per run (39159065487: 11; 39172550607: 8; most 2–6) | Efficient; no action |
| `workflow-failure-heal-intake` / `intake` | `used_in_job=23` (39170993476) | Fine for a dispatch-driven intake |
| `orchestrate_poll` / `poll` | Start/end `GH_PAT_BUDGET` emitted (`orchestrate_poll.yml:116`, `:1982`), not visible in any summary | **Gap.** Most likely the dominant consumer (inference) |
| Shared token, hourly | Reset windows 1792345892 → 1792371139: about 4,640–4,750 remaining at :00 and 3,850–3,970 at :30 | About 680–850 calls per 30 min; 30–35% utilisation; no 429s |
| `cancel_on_pr_close` scheduled | No cancel calls made; "preserved 3 active run(s) without pull-request linkage" (39178657480) | Low volume; the warning repeats every run without action |

**Recommendations:**
1. Make the collector extract the poller's `GH_PAT_BUDGET phase=end … used_in_job=N` line from the full log. If the poller is about 80% of the hourly 1,400–1,700 calls (inference), batching work under §14 should focus there, through `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql` and the caches they fill.
2. Without per-call hot-spot data from the poller, I cannot point to a specific unbatched loop. Collect that data next.
3. Estimated rate-limit risk today: low.

## Prompt Cache & Memory System

- **Derived `cache_hit_rate`:** 80.3% overall for review/autofix (43.76M read, 8.15M uncached, 2.60M written). Per run it ranges 63.9–86.8%. The aggregator reports null at repo and family level (Reliability 3).
- **Fragmentation signal:** a steady 0.47–0.56M cache-write tokens per run means the cached prefix is rebuilt in every run. The 10–12 calls per run then read about 0.6–1.2M cached tokens each. Within a run the cache works; across runs it does not (inference).
- **Likely causes:** per-run dynamic content (PR diff, Semble block at about 15KB, findings) placed early, and different stage prompts. A Semble block in the prefix alone would be enough to break cross-run reuse.
- **`CONTEXT_BUDGET_WARN`:** 1 occurrence (39117893207), on the run with the highest cache reads. Prompt growth there is real but not systemic.
- **Changes:** see Cost 1 (reorder content, add a `prefix_sha` field) and Reliability 3 (report coverage with the hit rate).
- **Expected impact:** 10–20% fewer uncached prompt tokens per review run. Latency gains are proportional but unquantified.
- **Memory retrieval effectiveness:** no data (see AI Memory Health).

## Orchestrator Health

- **Wave and phase activity:**
  - `clarify` 141 runs (3 success), `plan` 138 (3 success), `implement` 139 (4 success) and `orchestrate_clarify_respond` 138 (1 success). The remaining 135–138 runs in each family are "other" with p50 1s.
  - The near-identical counts suggest all four are triggered by the same event stream and gated by job-level `if:` conditions (inference). That is 545 skipped runs, which are noise rather than failures.
  - Real work in the window: 3 plans (814–896s), 4 implements, and 4 validates covering 2 tracking projects (`tracking:7614` and `tracking:7604`, two runs each). One `unblock_judge_dispatch` ran (96s), which is the only judge-cycle proxy.
- **Stuck or terminal states:** none visible. `workflow_failure_heal` ran 44 times, all "other", with 0 successes, so heal never actually triggered. Recovery scans returned `count=0` throughout.
- **Merge train:** every scheduled release scan logged `examined=0 released=0`.
- **Pain point:** the cadence and opacity of the poller (Speed 1).
- **Indicators to track weekly:**
  - poller `cancelled / total` (21% this week)
  - poller p50 versus the cron interval
  - `ORCH_POLL_PHASE_SUMMARY` phase shares, once added
  - poller `used_in_job`
  - `workflow_failure_heal` successful runs (0)
  - validate runs per tracking issue

## Pipeline Flow Bottlenecks

| Stage | Compute (p50 / p95 or observed) | Queueing | Retry | Merge/conflict |
|---|---|---|---|---|
| clarify / plan | Plan 814–896s (3 runs) | not measured | 0 | n/a |
| implement | 1,567–2,086s (4 runs); Semble 100% fallback | not measured | 0 | n/a |
| review/autofix | p50 19s (sweep), p95 1,626s, max 2,144s | not measured | 0 | n/a |
| CI | p50 852s, p95 1,246s | not measured | 0 (1 failure) | n/a |
| validate | about 810s fixed | not measured | 0 | n/a |
| orchestrate_poll | p50 575s, p95 1,046s | 50s–4m40s runner wait (9 runs) | 22 superseded | `examined=0` merge train |

**In order of end-to-end impact:**
1. Poller cadence and runner contention (Speed 1 and 2). Every phase transition waits on a poll tick.
2. The serial implement → review (about 30 min each) → CI (about 14 min) chain. Step timings are needed to cut it.
3. Validate's fixed roughly 13.5 minutes.
4. Retry overhead is effectively zero: 0 retries in every run.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:**
  - Poller at 575s p50 on a 300s cron, with 22 cancellations.
  - Runner queue waits of up to 4m40s.
  - Review/autofix and implement runs of about 26–36 minutes.
- **Top failure modes:**
  - CI `static-checks` / `Validation self-test unit tests` (39117893124), the only failure in 1,000 runs.
  - Semble silently failing in `implement` (9 out of 9 fallbacks).
  - A null `cache_hit_rate` that hides cache health.
- **Highest-cost drivers:**
  - Review/autofix OpenRouter usage: 54.9M tokens, 8.15M uncached, over 5 runs.
  - Codex in implement: 5.29M reported, but the data is suspect.
- **Top 3 actions:**
  1. Add the `ORCH_POLL_PHASE_SUMMARY` line (with an agents.d fragment) and surface the poller's `GH_PAT_BUDGET` end line in the collector. Then decide on `*/10` or a phase cut.
  2. Add a `semble_fallback_reasons` breakdown to `cost_audit.py` and fix Semble bootstrapping in `implement`.
  3. Add per-file result lines to the CI validation self-test step, and report `cache_hit_rate` with coverage instead of null.

## Metrics Appendix

**Run outcomes (window: 1,000 runs, success sample rate 0.07)**

| Scope | Total | Success | Failure | Cancelled | Other | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| Repo total | 1,000 | 323 | 1 | 22 | 654 | 1.0 | 844.2 |
| orchestrate_poll | 103 | 81 | 0 | 22 | 0 | 575 | 1,046 |
| ci | 28 | 27 | 1 | 0 | 0 | 852.5 | 1,246 |
| review_autofix | 55 | 55 | 0 | 0 | 0 | 19 | 1,626 |
| cancel_on_pr_close | 81 | 81 | 0 | 0 | 0 | 10 | 119 |
| implement | 139 | 4 | 0 | 0 | 135 | 1 | 11 |
| clarify | 141 | 3 | 0 | 0 | 138 | 1 | 10 |
| plan | 138 | 3 | 0 | 0 | 135 | 1 | 11 |
| orchestrate_clarify_respond | 138 | 1 | 0 | 0 | 137 | 1 | 10 |
| workflow_failure_heal_intake | 54 | 20 | 0 | 0 | 34 | 9 | 169 |
| workflow_failure_heal | 44 | 0 | 0 | 0 | 44 | 1 | 9 |
| validate | 4 | 4 | 0 | 0 | 0 | 810.5 | 813 |
| issue_pr_status | 7 | 7 | 0 | 0 | 0 | 150 | 239 |

The repo-wide p50 of 1s is dominated by skipped runs. The collector should report p50 excluding "other" runs.

**Tokens and cache**

| Metric | Value |
|---|---|
| or_prompt / completion / total | 8,145,744 / 425,713 / 54,931,166 |
| or_cache_read / cache_write | 43,760,919 / 2,598,790 |
| cache_hit_rate (reported) | null (repo, family) |
| cache_hit_rate (derived) | 80.3% overall; 63.9–86.8% per run |
| or_calls (usage available / unavailable) | 54 (53 / 1) |
| codex_calls / tokens | 44 / 5,288,556 (suspect: 4 identical runs) |
| wall_clock_p50_ms / p99_ms (repo) | 14,000 / 2,066,960 (115 samples) |
| break_glass_count | 0 |
| context_budget_warn_count | 1 (review_autofix, 39117893207) |

**Semble by workflow**

| Workflow | query_calls | logged_bytes | fallbacks | contract_test | runtime | bootstraps (failed / unused) | bootstrap_ms | sources | static_dup_bytes |
|---|---|---|---|---|---|---|---|---|---|
| review_autofix | 5 | 78,029 | 0 | 0 | 0 | 5 (0 / 0) | 215,944 | 36 | 1,041 |
| implement | 0 | 0 | 9 | 0 | 9 | 0 | 0 | 0 | 0 |
| ci | 0 | 0 | 8 | 8 | 0 | 0 | 0 | 0 | 0 |
| **Total** | 5 | 78,029 | 17 | 8 | 9 | 5 | 215,944 | 36 | 1,041 |

Echo lines dropped: 0. Fallback reasons are not aggregated (gap).

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | query_calls | response_bytes | tool_calls | fallbacks |
|---|---|---|---|---|---|---|---|
| (all) | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

Other MCP servers observed: none.

**GitHub API (`GH_PAT_BUDGET`, sampled)**

| Run | Job | used_in_job | remaining |
|---|---|---|---|
| 39159065487 | sweep | 11 | 3,864 |
| 39172550607 | sweep | 8 | 3,969 |
| 39170993476 | intake | 23 | 4,447 |
| 39178894404 | sweep | 2 | 4,680 |
| orchestrate_poll (all) | poll | not surfaced | n/a |

No 429s or secondary rate limits observed.

**Runner queue wait (orchestrate_poll, from `log_summary`):** 39161191425 about 280s, 39160588784 about 207s, 39178539462 about 148s, 39165729819 about 148s, 39147329180 about 138s, 39151636015 about 131s, 39156489852 about 95s, 39165113922 about 64s, 39155840560 about 50s.

**What to collect next:**
1. Mount the run-logs directory.
2. Have the collector grep the full untruncated logs for `GH_PAT_BUDGET phase=end`, `AI_MEMORY_TELEMETRY:` and `SEMBLE_FALLBACK reason=`.
3. Record per-job queue wait (`started_at − created_at`).
4. Pull the step log for 39117893124 `static-checks`.