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
## Deep Audit — Workflows & Scripts (2026-10-19)

**Scope and method.** This pass went deep on a subset of files rather than skimming all of them. Files read in full or at the relevant ranges:
- Workflows: `.github/workflows/orchestrate_poll.yml`, `cancel_on_pr_close.yml`, `sync_ai_labels.yml`, `workflow-log-analysis.yml`; selected ranges of `implement.yml`, `clarify.yml`, `plan.yml` and `review_autofix.yml`.
- Scripts: `scripts/gh_helpers.sh`, `scripts/tg_helpers.sh`, `scripts/label_helpers.sh`, `scripts/stage_workflow_support.sh`, `scripts/implement_handle_guard_block.sh`; the loop sites in `scripts/orchestrate_poll_process.sh`.
- Tests: `tests/test_workflow_file_size_limit.py`.

No `scripts/*.py` file was audited in depth.

**Relation to the existing report.** The report's "GH API Call Audit" said it "cannot point to a specific unbatched loop" in the poller. API-001 to API-007 below name those loops and answer that gap. They also bear on "Speed Optimizations 1" (poller run length). That link is an inference: no per-phase timing exists yet to prove it.

### Section 1: Bug & Correctness Sweep

- **BUG-001** — `.github/workflows/orchestrate_poll.yml:385-396` (versus `scripts/orchestrate_poll_process.sh:682-701`)
  - **Severity:** Medium
  - **Category:** bug
  - **Description:** Two detectors decide whether a tracking issue is a smoke-test fixture, and they disagree.
    - The workflow step uses `NON_SMOKE="$(echo "${TRACKING_ISSUES}" | jq '[.[] | select(.title | test("^\\[Orchestrator\\] E2E "; "i") | not)] | length')"`. When the count is 0 it writes `ALERT_MSG_LEVEL=SILENT` to `$GITHUB_ENV`.
    - The poller's own detector, `tracking_issue_is_smoke_fixture`, matches only `[E2E *`, `[orchestrator] [e2e orchestrate smoke *`, `[orchestrator] e2e orchestrate smoke *`, or the `e2e-smoke-test` label. Its comment at :689-692 says a real project such as `[Orchestrator] E2E tests for checkout` "keeps its alerts".
    - The workflow regex does the opposite in two ways:
      1. It silences every Telegram alert for a real project titled `[Orchestrator] E2E …` when that is the only open tracking issue.
      2. It misses the decomposed fixture title `[Orchestrator] [E2E Orchestrate Smoke N] …`, because of the bracket after the prefix. The fixture description is created at `test-and-mark-stable.yml:5466`, and the title shape is documented at `orchestrate_poll_process.sh:677`.
  - **Recommended fix:** Make the workflow step use the same rule as `tracking_issue_is_smoke_fixture`. Use one jq expression that matches `^\[E2E `, or `^\[orchestrator\] \[?e2e orchestrate smoke ` (case-insensitive), or the `e2e-smoke-test` label. Add a case to `tests/test_smoke_alert_silencing_contract.py` that pins both detectors to the same fixture titles.

- **BUG-002** — `scripts/label_helpers.sh:222-256` (`set_issue_phase_label_resilient`)
  - **Severity:** Medium
  - **Category:** bug
  - **Description:** The function reads the issue's labels (`gh api --paginate "repos/${repo}/issues/${issue_number}/labels"`), computes `(. - $p) + [$t]`, and then replaces the whole label set with `gh api -X PUT …/labels --input -`. This is a read-modify-write with no concurrency guard. If another job adds a label between the GET and the PUT, the PUT removes it. Examples are `ai:security`, the merge-train priority labels, or `ai:review-blocked`. The poller's own `set_issue_phase_label` (`orchestrate_poll_process.sh:4082-4108`) avoids this: it only adds or removes specific labels with `gh issue edit --remove-label … --add-label …`.
  - **Recommended fix:** Replace the PUT with targeted operations: `POST …/labels` for the target label, plus `DELETE …/labels/<name>` only for phase labels that are present in `_cur`. Alternatively, call the same `gh issue edit --add-label/--remove-label` pattern as `orchestrate_poll_process.sh:4085-4108`. Labels outside the phase set are then never rewritten. Keep the `SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME` contract unchanged.

- **BUG-003** — `scripts/tg_helpers.sh:394-440` (`tg_cleanup_phase_msgs`) and `scripts/tg_helpers.sh:463-511` (`tg_cleanup_msgs`)
  - **Severity:** Medium
  - **Category:** bug
  - **Description:** Both functions request `…/comments?per_page=100&page=${page}`, delete matching tracking comments with `curl -s -X DELETE …/issues/comments/${comment_id}`, and then move on with `page=$((page + 1))`. Each deletion on page *k* shifts later comments one position earlier. When page *k+1* is fetched, the first *d* comments that would have been on it (where *d* is the number deleted) have already moved onto page *k*, so they are never inspected. On busy issues with more than 100 comments, tracking comments and their Telegram messages are left behind. The comment at :194-197 shows this repo has already seen 447 `tg_cleanup` comments on one issue.
  - **Recommended fix:** First collect all matching `(id, body)` rows across every page without deleting anything. For example, use `gh api --paginate --slurp` through `gh_retry`, as `orchestrate_poll_process.sh:58` does. Then delete them in a second loop. Alternatively, keep re-reading page 1 until no matches remain.

- **BUG-004** — `scripts/tg_helpers.sh:227-276` (`tg_store_msg_id`) and `scripts/tg_helpers.sh:296-344` (`tg_store_phase_msg_id`)
  - **Severity:** Low
  - **Category:** bug
  - **Description:** The function reads the newest tracking comment, appends `,${msg_id}` with `sed`, and writes it back with `PATCH`. Two jobs notifying on the same issue at the same time (for example a poller tick and a review run) will each write their own version, and one message ID is lost. If neither finds a marker, both `POST` a new tracking comment. Lost IDs are never cleaned up, so Telegram messages are orphaned. All writes are raw `curl … || true`, so these failures are silent.
  - **Recommended fix:** Write one marker comment per message (`<!-- tg_cleanup:${msg_id} -->`) using only `POST`, and have `tg_cleanup_msgs` collect every marker. That removes the read-modify-write entirely. The cost is extra comments on the issue, which should be weighed against the 447-comment incident at :194-197. If one comment per issue must be kept, re-read the comment after the `PATCH` and retry once if the ID is missing.

- **BUG-005** — `.github/workflows/implement.yml:6307-6308` and `.github/workflows/implement.yml:6359-6360`
  - **Severity:** Medium
  - **Category:** bug
  - **Description:** Both steps run `gh_retry gh issue edit "${ISSUE_NUMBER}" … --add-label 'ai:done' --remove-label 'ai:implementing' >/dev/null 2>&1 || true` (the second uses `ai:awaiting-approval`). They have no fallback. If the remove part fails, the add is lost and `|| true` hides it. The poller documents that removing an absent label "can cause `gh issue edit` to return an error" (`orchestrate_poll_process.sh:4076-4081`). The same workflow already handles this at `implement.yml:5548-5551` by retrying with `--add-label` only. A lost `ai:done` leaves the issue at `ai:implementing`, which the poller's stall loop treats as stuck (inference). [NEEDS VERIFICATION] Exact `gh` behaviour when the label exists in the repo but is not on the issue.
  - **Recommended fix:** Copy the two-command fallback from `implement.yml:5548-5551` into both steps, or call `set_issue_phase_label_resilient` once BUG-002 is fixed.

- **BUG-006** — `.github/workflows/orchestrate_poll.yml:280-292`
  - **Severity:** Low
  - **Category:** bug
  - **Description:** The step reads `TRACKING_ISSUES="$(gh_retry gh issue list … --label "ai:orchestrator-tracking" --state open --json number,title,labels --limit 20)"`. If there are more than 20 open tracking issues, the extra projects are dropped without any log line. Their waves stop advancing, which is the same symptom as the #2734 stall in §21. [NEEDS VERIFICATION] Whether 20 is a deliberate capacity cap.
  - **Recommended fix:** Emit `::warning::` when `COUNT` equals the limit, and raise the limit (for example to `--limit 200`). If the cap is deliberate, document it next to the call.

- **BUG-007** — `.github/workflows/cancel_on_pr_close.yml:14-16`
  - **Severity:** Low
  - **Category:** bug
  - **Description:** The job sets `concurrency: group: cancel-on-pr-close-${{ github.repository }}` with `cancel-in-progress: false`. A GitHub concurrency group holds at most one pending run, so when several PRs close in a burst, the middle closes' runs are replaced and never run. Their head-branch runs are then only cancelled by the scheduled backstop (lines 237-274), which keeps runner minutes busy until the next schedule tick. This matches the prior report's 81 `cancel_on_pr_close` runs, mostly scheduled (inference). [NEEDS VERIFICATION] Whether the merge-train release at :349-385 relies on this repo-wide serialisation.
  - **Recommended fix:** Split the job. Run the cancellation part in a per-PR group (`…-${{ github.event.pull_request.number || 'schedule' }}`) and keep the merge-train release in the repo-wide group.

- **SEC-001** — `.github/workflows/orchestrate_poll.yml:151-216`, `.github/workflows/cancel_on_pr_close.yml:59-120`, `.github/workflows/sync_ai_labels.yml:70-78`
  - **Severity:** Medium
  - **Category:** security
  - **Description:** When `SCRIPT_REF == 'stable'`, these workflows behave as follows:
    - If the stable checkout fails, they fall back to `ref: main` (`orchestrate_poll.yml:151-160`).
    - They always check out a `.codex-workflow-src-main` snapshot (`:176-185`).
    - Any helper missing from stable is installed from main (`:192-193`, `:206-210`; `cancel_on_pr_close.yml:101-119`).

    So consumer repos run unreleased `main` helpers with `GH_PAT`. These include `gh_helpers.sh`, `review_merge_train.sh` and `label_helpers.sh`, and the merge-train helper mutates PRs. The review path is stricter: `stage_workflow_support.sh:11-17` refuses anything but a verified 40-hex SHA ("Never stage executable support from … a moving ref"), and `review_autofix.yml:2048-2079` pins `SCRIPT_REF` to `needs.gate.outputs.review_support_sha`.
  - **Recommended fix:** Resolve `stable` to a commit SHA once, using `git -C .codex-workflow-src rev-parse HEAD` after checkout. Stage only from that verified checkout. Make the `main` fallback opt-in through a repo variable (default off) and log `SUPPORT_REF_FALLBACK ref=main` when it is used. §8 still applies: the new variable needs a default.

- **SEC-002** — `.github/workflows/orchestrate_poll.yml:404-413`, `.github/workflows/review_autofix.yml:2037-2046`, `.github/workflows/review_autofix.yml:4639`
  - **Severity:** Low
  - **Category:** security
  - **Description:** These steps run `git remote set-url origin "https://x-access-token:${GH_TOKEN}@…"`, which stores the PAT in plain text in the workspace `.git/config`, on top of the `actions/checkout` extraheader. `review_autofix.yml:2010-2013` calls this the "credential-bearing host checkout". Any process that reads the workspace `.git/config` can read the PAT. [NEEDS VERIFICATION] Whether any agent or tool runs against the host checkout rather than inside `review_untrusted_sandbox.sh` or `codex_isolated_exec.sh`.
  - **Recommended fix:** Pass the credential per command (`git -c http.extraheader="AUTHORIZATION: basic …" push`, the pattern already used at `review_autofix.yml:355`) or through `GIT_ASKPASS`. Otherwise, restore a token-free URL in an `if: always()` step.

- **SEC-003** — `scripts/tg_helpers.sh:124`, `:156`, `:186`
  - **Severity:** Low
  - **Category:** security
  - **Description:** `curl -s -X POST "https://api.telegram.org/bot${TG_BOT_SECRET}/sendMessage"` puts the bot token in curl's argv, where other processes running as the same user on the runner can read it from `/proc/<pid>/cmdline`. Actions log masking does not cover argv.
  - **Recommended fix:** Pass the URL through `curl --config -` on stdin (`url = "…"`) so the token never appears in argv.

### Section 2: GitHub API Call Redundancy Audit

- **API-001** — `scripts/label_helpers.sh:175-191` (`ensure_label_exists`), `scripts/orchestrate_poll_process.sh:4029-4046` (poller copy), `.github/workflows/implement.yml:5544`, `:6304`, `:6356`
  - **Severity:** High
  - **Category:** api-redundancy
  - **Description:** These sites run `gh_retry gh label create …`, and "already exists" is the normal outcome. `gh_retry` does not treat it as a permanent failure:
    - `_is_gh_permanent_failure` (`gh_helpers.sh:192-195`) has no `already exists` pattern.
    - `_gh_cmd_is_unsafe_post` (`gh_helpers.sh:811-814`) does not list `label create`.
    - The repo says so itself: "gh_retry does not treat it as permanent … gh_retry repeats `gh label create`" (`implement_handle_guard_block.sh:30-33`).

    Each existing label therefore costs `GH_RETRY_MAX_ATTEMPTS` (default 5) POSTs plus 1+2+4+8 = 15 s of backoff sleep before the caller's `already exists` grep runs. Impact:
    - Every successful implement run pays this at `implement.yml:6304`.
    - Every first use of a label in a poller process pays it (`_ENSURED_LABELS_CACHE`, `:657`).
    - The poller has 67 `ensure_label_exists`/`set_issue_phase_label_resilient` references.

    [NEEDS VERIFICATION] The exact `gh` stderr text for an existing label. The in-repo comment above is the evidence used here.
  - **Calls:** currently 5 POSTs and 15 s per existing label; proposed 1 POST.
  - **Recommended fix:** Copy `guard_block_ensure_label` (`implement_handle_guard_block.sh:35-66`): make one raw `gh label create` first, return 0 on `already[ _-]*exists`, and use `gh_retry` only for other errors. Apply this to both `ensure_label_exists` copies (see DUP-004) and to the three raw `gh_retry gh label create` sites in `implement.yml`.

- **API-002** — `scripts/orchestrate_poll_process.sh:22206-22219` (`_has_active_autofix_run`) and `scripts/orchestrate_poll_process.sh:17013-17021`
  - **Severity:** High
  - **Category:** api-redundancy
  - **Description:** For each PR, the code runs `for wf_candidate in ai-review.yml internal-review.yml review_autofix.yml; do gh_retry gh run list … --workflow "${wf_candidate}" --branch "${head_ref}" …`. That is 3 calls minimum. `ai-review.yml` does not exist in this repo's `.github/workflows/`, and consumers presumably lack `internal-review.yml` and `review_autofix.yml`. `gh run list --workflow <missing>` fails with a "could not find any workflows named" error that does not match the permanent patterns. Each missing candidate is therefore retried 5 times with 15 s of sleep, and each attempt resolves the workflow through about 2 REST calls. The function is called from `:12669`, `:12748` and `:22628`. [NEEDS VERIFICATION] The `gh` error text, and the workflow files present in consumer repos.
  - **Calls:** currently 3 to 30 calls and up to 15–30 s of sleep per check; proposed 1.
  - **Recommended fix:** Make one `gh_retry gh api "repos/${GITHUB_REPOSITORY}/actions/runs?branch=${head_ref}&per_page=20"` call and filter in jq on `.path | endswith("/ai-review.yml") or endswith("/internal-review.yml") or endswith("/review_autofix.yml")`. `gh run list --json` has no `path` field, which is why the REST endpoint is needed. Reuse the result for both loops in the same tick.

- **API-003** — `scripts/orchestrate_poll_process.sh:20386-20402`
  - **Severity:** Medium
  - **Category:** api-batching
  - **Description:** The code makes 7 sequential `gh_retry gh issue list … --label "${lbl}" --json number --limit 1000` calls, one per pipeline label, and then calls `_fetch_standalone_marker_issues_graphql` separately. `orchestrate_poll.yml:331-343` already shows a single aliased GraphQL query with `l0`…`l6` aliases for exactly these labels.
  - **Calls:** currently 7 or more REST pages plus 1 GraphQL; proposed 1 GraphQL (or 2 when `hasNextPage` triggers).
  - **Recommended fix:** Extend `_fetch_standalone_marker_issues_graphql` (`:18059`). Add aliases `l0…l6: issues(states: OPEN, labels: ["<label>"], first: 100) { nodes { number } pageInfo { hasNextPage } }` next to the existing `state`/`clarify` searches, and keep the existing REST fallback when a page reports `hasNextPage`.

- **API-004** — `scripts/orchestrate_poll_process.sh:27428-27455` and `:27843-27851`
  - **Severity:** Medium
  - **Category:** api-batching
  - **Description:** After review-blocked handling and re-issue, labels are re-read with one `gh_retry _safe_gh_jq "repos/…/issues/${inum}/labels"` call per issue. The JSON at :27428-27443 is also built by string concatenation. The batched helper `_fetch_issue_labels_batch_graphql` (`:4323-4336`) is already used for the same data at `:25161-25177`, with per-issue REST only for cache misses.
  - **Calls:** currently N (wave issues) plus M (re-issued); proposed one batched GraphQL call per batch, plus REST only for misses.
  - **Recommended fix:** Replace both loops with `LABELS_JSON="$(_fetch_issue_labels_batch_graphql "<json array of ISSUE_NUMS ∪ REISSUED_NUMS>")"`, then the existing miss-only fallback loop from :25170-25177. This also removes the hand-built JSON.

- **API-005** — `scripts/orchestrate_poll_process.sh:6946-6963` and `:27565-27586`
  - **Severity:** Medium
  - **Category:** api-batching
  - **Description:** Blocker issue states are read with one `gh_retry _safe_gh_jq "repos/…/issues/${blocker_issue}" --jq '.state'` call per blocker, and the same loop appears twice (see DUP-005).
  - **Calls:** currently N REST; proposed 1 GraphQL per 50 blockers.
  - **Recommended fix:** Add `_fetch_issue_states_batch_graphql` alongside `_fetch_blocked_issue_routing_graphql` (`:102-142`), using the same alias pattern `i${n}: issue(number: ${n}) { number state }`. Document the §14 batching contract and fall back to the REST loop when the batch fails. First check whether blockers are already in `ISSUE_STATES_JSON` or `_current_wave_details_json` (`:25156`). [NEEDS VERIFICATION]

- **API-006** — `scripts/orchestrate_poll_process.sh:7884-7893`
  - **Severity:** Low
  - **Category:** api-batching
  - **Description:** One `gh_retry gh api "repos/…/issues/${row_issue}" --jq '{state, labels}'` call is made per advisory follow-up row.
  - **Calls:** currently N; proposed 1.
  - **Recommended fix:** Fetch all row issue numbers once with `_fetch_candidate_issue_details_graphql` (`:18146`, which returns state and labels) before the loop, and read from that cache. Keep the per-row REST read only for misses.

- **API-007** — `scripts/orchestrate_poll_process.sh:4732-4744`
  - **Severity:** Low
  - **Category:** api-batching
  - **Description:** For each cross-referenced PR, `gh_retry gh api "repos/…/pulls/${pr}"` is called only to read `merged_at` and `body`.
  - **Calls:** currently N; proposed 1.
  - **Recommended fix:** Use one aliased GraphQL query (`p${n}: pullRequest(number: ${n}) { number mergedAt body }`) over the `sort -rn -u` list, then apply the same newest-first closing-keyword match. Keep the current "read failure → return 0" fail-open behaviour.

- **API-008** — `scripts/tg_helpers.sh:204-218` (`_tg_newest_comments_json`)
  - **Severity:** Low
  - **Category:** api-batching
  - **Description:** Each tracked Telegram message costs 2 REST reads: the issue (for its comment count) and then the last comments page. The comment at :198-200 accepts this because REST has no `direction` parameter.
  - **Calls:** currently 2 reads per tracked message; proposed 1.
  - **Recommended fix:** Use `gh api graphql` with `repository { issue(number: N) { comments(last: 100) { nodes { databaseId body } } } }`. This returns the newest 100 comments in one call. Map `databaseId` to `.id` for the existing jq filters.

- **API-009** — `scripts/gh_helpers.sh:1365-1398` (`curl_gh_api`) and `scripts/gh_helpers.sh:1302-1322` (`gh_api_json_to_file`)
  - **Severity:** Low
  - **Category:** api-redundancy
  - **Description:** Neither helper checks for permanent failures:
    - `curl_gh_api` retries every non-2xx, non-rate-limit status (401, 404, 422) 5 times with backoff.
    - `gh_api_json_to_file` never calls `_is_gh_permanent_failure`.
    - `gh_retry` and `gh_retry_to_file` both stop at once on these errors (`:1099-1106`, `:1184-1192`).
    - `curl_gh_api` also lacks the single-attempt POST guard (Q2=A, #6634) that `gh_retry` applies. It has no POST callers today, because `tg_helpers.sh` POSTs through raw `curl`.
  - **Calls:** currently 5 calls and 15 s per permanent error; proposed 1.
  - **Recommended fix:** In `curl_gh_api`, `break` on `400|401|404|410|422`, and default to `max_attempts=1` when `-X POST` is present without `--idempotent`. In `gh_api_json_to_file`, add the same `_is_gh_permanent_failure` check that `gh_retry_to_file` has.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — Support-ref resolver, checkout fallback and pin-verification blocks
  - **Severity:** Medium
  - **Category:** duplication
  - **Locations:** 16 resolver copies in 10 workflows (each range is the `echo "SCRIPT_REF=…"` pair):
    - `sync_ai_labels.yml:44-56`, `cancel_on_pr_close.yml:33-45`, `orchestrate.yml:204-216`, `orchestrate.yml:357-369`, `orchestrate_poll.yml:125-137`, `orchestrate_poll.yml:500-512`
    - `implement.yml:446-458` (as `DUPLICATE_NOTICE_SCRIPT_REF`), `implement.yml:1288-1300`, `implement.yml:6848-6860`
    - `clarify.yml:381-393`, `clarify.yml:2330-2342`, `plan.yml:350-362`, `plan.yml:3153-3165`
    - `workflow_failure_heal.yml:108-120`, `orchestrate_clarify_respond.yml:395-407`, `check_failure_triage.yml:313-325`

    There are also 12 copies of the "refusing to run support code from another ref" check. The heal-report job is identical apart from the workflow filename in `implement.yml:6841-6891`, `clarify.yml:2323-2373` and `plan.yml:3146-3196`.
  - **Description:** The blocks differ only in the workflow filename inside the regex. A fix to the pin logic (for example SEC-001) has to be applied in 16 places by hand. Moving the resolver to a script would not help, because it runs before any checkout exists.
  - **Recommended fix:**
    1. Keep the pre-checkout resolver inline. Add a contract test, `tests/test_support_ref_resolver_parity.py`, that requires every copy to match a canonical template after substituting the workflow filename.
    2. Move the post-checkout "Ensure …" and "Stage gh retry helper" bodies into a new function, `stage_minimal_support()`, in `scripts/stage_workflow_support.sh`. Signature: `stage_minimal_support <required_helper> [optional_helper…]`. It enforces the 40-hex pin match and installs into `scripts/`.
    3. Update callers at `orchestrate_poll.yml:162-216` and `cancel_on_pr_close.yml:70-120`. Keep step `name:`/`id:`/`if:` per §10 and §24.

- **DUP-002** — `.github/workflows/workflow-log-analysis.yml:251-384`, `:828-995`, `:1667-1857`, `:2274-2456`
  - **Severity:** Medium
  - **Category:** duplication
  - **Description:** The weekly-retro, analyze-commit-notify, deep-audit and api-redundancy jobs each repeat the same setup steps: "Resolve AI engine", "Install Claude Code CLI", "Resolve Claude credential", "Setup Python", "Cache Codex CLI", "Install Codex CLI", "Persist Codex CLI to tool cache", "Restore Codex CLI from cache", "Create Codex config", "Setup uv (Semble)", "Install semble" and "Build semble index". The Codex tool-cache copy commands are byte-identical at :328-345, :910-931, :1749-1770 and :2356-2377. That is roughly 130–190 lines × 4.
  - **Recommended fix:** This workflow checks out its own repository (step "Checkout repository", :83), so it can use a local composite action, `./.github/actions/log-analysis-agent-setup`, with inputs `codex_version` and `semble_enabled`. Each of the four jobs would then call it in one step. This also shrinks the file (EXPR-003).

- **DUP-003** — `.github/workflows/workflow-log-analysis.yml:1100-1193` (`emit_log_analysis_phase_failure`), `:1890-1966` (`emit_deep_audit_failure`), the api-redundancy copy (`${{ github.run_id }}` at :2501-2556), and the push-retry loop at `:2150-2207` (with a sibling in "Commit and push API redundancy section", :2723)
  - **Severity:** Medium
  - **Category:** duplication
  - **Description:** Three near-identical failure helpers build the same `AI_PHASE_FAILURE_V1` jq payload, comment, event and `ai:log-analysis-failed` label. They differ only in `failed_step_name`, the heading, and whether a comment failure prints a warning (:1166-1167) or is ignored (:1948). The helper `_wla_label_create` is redefined at :1079 and :1878. [NEEDS VERIFICATION] That the api-redundancy push block matches :2150-2207 line for line.
  - **Recommended fix:** Create `scripts/wla_phase_failure.sh` with `wla_emit_phase_failure <failed_step_name> <failure_mode> <attempt_count> <failure_summary> <heading>`, and `scripts/wla_push_report_section.sh` with `wla_push_report_section <report_file> <commit_message> <label>`. Source them from the three run blocks. Read `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_WORKFLOW` and `GITHUB_SERVER_URL` from the environment; this also fixes EXPR-001 and EXPR-002.

- **DUP-004** — Four label-ensure implementations
  - **Severity:** Medium
  - **Category:** duplication
  - **Locations:** `scripts/label_helpers.sh:161-195`, `scripts/orchestrate_poll_process.sh:3995-4050`, `scripts/implement_handle_guard_block.sh:35-66`, `.github/workflows/workflow-log-analysis.yml:1079` and `:1878`
  - **Description:** The copies have drifted:
    - **Colour source:** the catalog in `label_helpers.sh:164-165` versus `.github/ai/label_contract.v1.json` read from the current directory in the poller (`:4019-4024`).
    - **Caching:** only the poller has `_ENSURED_LABELS_CACHE` (`:657`).
    - **Retry:** the poller and `label_helpers.sh` use 5 attempts (API-001), while the guard-block and `_wla` copies use one raw attempt.
    - **Return code:** the poller returns 0 on failure (:4049), `label_helpers.sh` returns 1 (:194).
  - **Recommended fix:** Keep one `ensure_label_exists <label> [repo]` in `scripts/label_helpers.sh` with:
    1. a process-level cache;
    2. a raw first attempt (the API-001 fix);
    3. colours from the contract file when present, otherwise the catalog.

    Then remove the poller copy (the poller already stages `label_helpers.sh` per `orchestrate_poll.yml:206`). Have `guard_block_ensure_label` wrap it only for its `edit_existing` behaviour, and replace `_wla_label_create`. Keep the poller's always-0 return through a thin wrapper so existing callers don't change (§10).

- **DUP-005** — `scripts/orchestrate_poll_process.sh:6946-6963` and `:27565-27586`
  - **Severity:** Low
  - **Category:** duplication
  - **Description:** The same blocker-state loop and open/closed/unknown summary are written twice, with different variable prefixes (`blocker_*` and `IF_BLOCKER_*`).
  - **Recommended fix:** Extract `_summarize_blocker_states <blockers_json>`, which sets `BLOCKER_OPEN_COUNT`, `BLOCKER_UNKNOWN_COUNT` and `BLOCKER_STATUS_SUMMARY` and reads from the API-005 batch helper. Update both callers.

- **DUP-006** — `GH_PAT_BUDGET` inline parsers
  - **Severity:** Low
  - **Category:** duplication
  - **Description:** There are 19 inline `gh api rate_limit --jq …` parsers in 5 workflows (`review_autofix.yml` ×12, `clarify.yml` ×3, `workflow-failure-heal-intake.yml` ×2, `orchestrate_poll.yml` ×1, `validation-improvements-intake.yml` ×1), even though `gh_pat_budget` exists (`gh_helpers.sh:31-77`). End-of-job steps such as `clarify.yml:2375-2384` run after staging, so they can use the helper; `orchestrate_poll.yml:1974-1985` already does.
  - **Recommended fix:** Change every `phase=end` step to the `orchestrate_poll.yml:1980-1985` pattern. Keep the inline parser only for `phase=start` steps that run before staging (documented at `orchestrate_poll.yml:107-108`).

### Section 4: Expression Size Limit Risk Assessment

The CI guard `tests/test_workflow_file_size_limit.py:28-32,66-98` fails any `run:` body that contains `${{` and is 18,000 characters or longer. It also fails any workflow file of 480,000 bytes or more (`:27`, `:44-59`). So nothing at or above 18,000 characters can merge, and there are no High findings in this section. The guard does not measure `if:` expressions or `env:` values. The longest `if:` seen is `implement.yml:6363`, at roughly 400 characters, which is far below the limit.

The character counts below are estimates from line counts and the line lengths observed in this pass. Confirm them with the test's own `len(run)` measurement before acting on headroom.

- **EXPR-001** — `.github/workflows/workflow-log-analysis.yml:1068-1401` (step "Run workflow log analysis", job `analyze-commit-notify`)
  - **Severity:** Medium
  - **Category:** expression-limit
  - **Description:** The body is about 333 lines and includes several lines longer than 200 characters (:1079, :1162, :1255, :1260, :1334). Estimated length is about 15,000–17,000 characters, which leaves about 4,000–6,000 characters below 21,000 and about 1,000–3,000 below the 18,000 CI guard. [NEEDS VERIFICATION] The only expressions in it are `${{ github.run_id }}`, `${{ github.run_attempt }}`, `${{ github.workflow }}`, `${{ github.server_url }}` and `${{ github.repository }}` (:1113-1115, :1128, :1134-1136, :1173-1175). All of them are available as the default env vars `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_WORKFLOW`, `GITHUB_SERVER_URL` and `GITHUB_REPOSITORY`.
  - **Recommended fix:** Replace those 10 interpolations with the env vars. The body then has no `${{ }}` and is no longer subject to the limit at all. Extracting the failure helper (DUP-003) removes about 95 more lines.

- **EXPR-002** — `.github/workflows/workflow-log-analysis.yml:1869-2124` ("Run deep audit pass") and `:2457-2722` ("Run API redundancy pass")
  - **Severity:** Low
  - **Category:** expression-limit
  - **Description:** These bodies are about 255 and 265 lines, estimated at 12,000–14,000 characters each, which leaves about 7,000–9,000 characters of headroom. [NEEDS VERIFICATION] Their only interpolations are the same `github.*` values (:1901-1903, :1915, :1921-1923, :1954-1956; :2501-2503, :2515, :2554-2556). Prompt assembly sits inline and grows with every audit-scope change.
  - **Recommended fix:** Same as EXPR-001: switch to the `GITHUB_*` env vars, then extract the shared helper (DUP-003).

- **EXPR-003** — Workflow file size
  - **Severity:** Low
  - **Category:** expression-limit
  - **Description:** No workflow can reach the 800 KB threshold in the prompt, because the CI guard fails at 480,000 bytes. The real limit is 512,000 bytes (§24), not 1 MB. By line count, the largest files are `test-and-mark-stable.yml` (7,585 lines), `implement.yml` (6,891), `review_autofix.yml` (5,711), `plan.yml` (3,196) and `workflow-log-analysis.yml` (2,833). `test-and-mark-stable.yml` would cross the 480,000-byte guard at an average of 63.3 bytes per line, so its real headroom is unknown and may be small. [NEEDS VERIFICATION] Run `wc -c` on it.
  - **Recommended fix:** If `test-and-mark-stable.yml` is within 50,000 bytes of the guard, move its largest inline `run:` bodies into `scripts/` following the §24 / `review_autofix_step_<slug>.sh` pattern. For `workflow-log-analysis.yml`, DUP-002 removes about 400–600 lines (estimate).

- **EXPR-004** — Inline `${{ github.repository }}` in shell bodies
  - **Severity:** Low
  - **Category:** expression-limit
  - **Locations:** `implement.yml:934`, `:1990`, `:2166`, `:2287-2288`, `:2392`, `:2411-2412`, `:2434`, `:2620`, `:2633`, `:2647`, `:5534-5550`, `:6093`, `:6304-6309`, `:6356-6359`, `:6543-6575`; `orchestrate_poll.yml:267`, `:281`
  - **Description:** Each of these makes a body that could otherwise be expression-free subject to the limit and counted by the guard.
  - **Recommended fix:** Use `${GITHUB_REPOSITORY}`, which is already used elsewhere in the same files, for example `orchestrate_poll.yml:326`.

### Section 5: Cross-Cutting Concerns

- **DEAD-001** — `.github/workflows/orchestrate_poll.yml:2000-2002`
  - **Severity:** Low
  - **Category:** dead-code
  - **Description:** `[ -n "${fallback_support_dir}" ]` is always true, because the variable is assigned the literal `"${GITHUB_WORKSPACE}/.codex-workflow-src"` on the line before.
  - **Recommended fix:** Remove the always-true test and keep the `-f`/`! -L` checks.

- **DEAD-002** — `scripts/gh_helpers.sh:91` and `:108` (`gh_review_pr_state`)
  - **Severity:** Low
  - **Category:** dead-code
  - **Description:** The code reads `gh api "repos/${repo}/pulls/${number}" --jq .state` and then checks against `open|closed|merged`. The REST pulls `state` field is only `open` or `closed`, so the `merged` branch can never match.
  - **Recommended fix:** Leave the regex as it is (harmless), but add a comment, or read `.merged` if callers need to tell merged from closed. Do not change the return contract (§10).

- **CONSIST-001** — `.github/workflows/sync_ai_labels.yml:125` versus `:170`
  - **Severity:** Low
  - **Category:** consistency
  - **Description:** The same step body uses `if [ "${{ inputs.dry_run }}" = "true" ]` inline, while the next step passes `DRY_RUN: ${{ inputs.dry_run }}` through `env:`. Grep found this to be the only `inputs.*` interpolated inside a shell body across all workflows.
  - **Recommended fix:** Add `DRY_RUN: ${{ inputs.dry_run }}` to the step's `env:` and test `"${DRY_RUN}"`.

- **CONSIST-002** — `.github/workflows/orchestrate_poll.yml:109-116` versus `:1980-1982` and `scripts/gh_helpers.sh:50-60`
  - **Severity:** Low
  - **Category:** consistency
  - **Description:** The two budget readings come from different sources. The start snapshot reads `.resources.core` from the body; `gh_pat_budget end` prefers the `x-ratelimit-*` headers when `x-ratelimit-resource: core`. Mixing the two can skew `used_in_job`, the very number the existing report wants surfaced in its "GH API Call Audit".
  - **Recommended fix:** Make the start step parse the body only, as now, and add an option to `gh_pat_budget` to also read from the body only. Or record which source was used in the log line (`source=body|header`) as a new additive field (§10).

- **CONSIST-003** — `.github/workflows/review_autofix.yml:4639` versus `:2043-2046` and `orchestrate_poll.yml:410-413`
  - **Severity:** Low
  - **Category:** consistency
  - **Description:** Line 4639 hardcodes `@github.com/`, while the other two sites derive the host from `GITHUB_SERVER_URL`. Pushes from line 4639 would break on GitHub Enterprise Server.
  - **Recommended fix:** Reuse the `SERVER_HOST` derivation from `:2043-2045`, or the per-command auth from SEC-002.

- **CONSIST-004** — Raw API calls next to retry-wrapped ones
  - **Severity:** Low
  - **Category:** consistency
  - **Locations:** `.github/workflows/orchestrate_poll.yml:1955-1958` (raw `curl -fsSL` to the jobs API); `.github/workflows/implement.yml:2166`, `:2392`, `:2411-2412`, `:2434` (raw `gh api` reads)
  - **Description:** The same files use `gh_retry` or `curl_gh_api` for equivalent reads elsewhere (for example `implement.yml:1990` and `:2620`), so these reads fail on the first transient 5xx.
  - **Recommended fix:** Wrap these read-only calls in `gh_retry` (or `gh_retry_to_file`). Comment POSTs stay as single attempts because `_gh_cmd_is_unsafe_post` already handles them.

- **SHELL-001** — `scripts/tg_helpers.sh:422-428` and `:492-498`
  - **Severity:** Low
  - **Category:** shellcheck
  - **Description:** `IFS=','; for tg_id in ${id_list}; do` expands unquoted (SC2086). Glob expansion is not a risk today because `id_list` comes from `sed` matching `[0-9,]*`. However, the global `IFS` stays changed if the loop body ever returns early.
  - **Recommended fix:** Use `IFS=',' read -ra tg_ids <<< "${id_list}"; for tg_id in "${tg_ids[@]}"; do`.

- **SHELL-002** — `scripts/tg_helpers.sh:331`, `:420`
  - **Severity:** Low
  - **Category:** shellcheck
  - **Description:** `${phase}` is put into a `sed` regex without escaping. Current callers pass only `clarify` and `plan` (`plan.yml:1226`, `:2493`, `:2909`; `implement.yml:2470`; `clarify.yml:2093`; `orchestrate_clarify_respond.yml:1269`), so this is safe today.
  - **Recommended fix:** Check `[[ "${phase}" =~ ^[a-z_]+$ ]] || return 0` at the top of `tg_store_phase_msg_id` and `tg_cleanup_phase_msgs`. This also matches the `[a-z_]*` that `tg_cleanup_msgs` expects at `:490`.

- **DEBT-001** — `scripts/gh_helpers.sh:1338-1342`
  - **Severity:** Low
  - **Category:** tech-debt
  - **Description:** The header comment of `curl_gh_api` says it "sleeps 30 s and retries" on a rate limit. The code actually uses `_parse_reset_header` and `_sleep_until_reset` (:1384-1387), which honour `Retry-After`, cap the wait at 600 s, and fall back to 30 s only when no reset time is available.
  - **Recommended fix:** Update the comment to match the code and the file-level description at `:14-21`.

- **DEBT-002** — `scripts/orchestrate_poll_process.sh:12-19`
  - **Severity:** Low
  - **Category:** tech-debt
  - **Description:** The poller is over 27,800 lines; reads reached line 27,851. ShellCheck's extended dataflow analysis is disabled for it because it timed out CI (#6680). That leaves the file with the most API loops (Section 2) and the most label writes with the weakest static coverage.
  - **Recommended fix:** Split the poller into sourced modules under `scripts/orchestrate_poll_*.sh` (for example labels, stall, conflict and wave), each small enough for full analysis. Start with the label helpers (DUP-004) and the run-lookup helpers (API-002).

- **TODO/FIXME/HACK markers:** grep found no `TODO`, `FIXME`, `HACK` or `XXX` markers in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 2 | API-001, API-002 |
| Medium | 13 | BUG-001, BUG-002, BUG-003, BUG-005, SEC-001, API-003, API-004, API-005, DUP-001, DUP-002, DUP-003, DUP-004, EXPR-001 |
| Low | 24 | BUG-004, BUG-006, BUG-007, SEC-002, SEC-003, API-006, API-007, API-008, API-009, DUP-005, DUP-006, EXPR-002, EXPR-003, EXPR-004, DEAD-001, DEAD-002, CONSIST-001, CONSIST-002, CONSIST-003, CONSIST-004, SHELL-001, SHELL-002, DEBT-001, DEBT-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/label_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/implement.yml` (API-001, API-002) | Small |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/tg_helpers.sh`, `scripts/gh_helpers.sh` (API-003 to API-009) | Medium |
| Code modularization | `scripts/stage_workflow_support.sh`, `scripts/label_helpers.sh`, new `scripts/wla_phase_failure.sh` and `scripts/wla_push_report_section.sh`, new `.github/actions/log-analysis-agent-setup`, `.github/workflows/workflow-log-analysis.yml`, `orchestrate_poll.yml`, `cancel_on_pr_close.yml`, plus a resolver-parity test (DUP-001 to DUP-006) | Large |
| Expression size reduction | `.github/workflows/workflow-log-analysis.yml`, `.github/workflows/implement.yml`, `.github/workflows/orchestrate_poll.yml`; possibly `test-and-mark-stable.yml` (EXPR-001 to EXPR-004) | Small |
| Medium/Low fixes | `.github/workflows/orchestrate_poll.yml`, `cancel_on_pr_close.yml`, `sync_ai_labels.yml`, `implement.yml`, `review_autofix.yml`, `scripts/label_helpers.sh`, `scripts/tg_helpers.sh`, `scripts/gh_helpers.sh` (BUG-001 to BUG-007, SEC-001 to SEC-003, DEAD, CONSIST, SHELL, DEBT) | Medium |