## Executive Summary

The sampled window is about 14.5 hours, from 2026-10-19 10:11 to 2026-10-20 00:38 UTC, in `shubhodeep1/coding-workflows`. It has 1,000 runs: 325 succeeded, 0 failed, 21 were cancelled and 654 were skipped (`other`). `insufficient_data` is false. The full-log folder (`/home/runner/work/_temp/workflow-log-output`) was not mounted. No `summary.json`, `errors/`, `slow/` or `recent/` folders were available, so this report rests on `analysis_context` and the `log_summary` fields, checked against the repository source.

- **The poller runs continuously and has the longest wall time.** `orchestrate_poll` has p50 622s and p95 1,159s on a `*/5` cron (`internal-orchestrate-poll.yml:5`), with `cancel-in-progress: false` (`orchestrate_poll.yml:68-73`). Each run starts within 2–6s of the previous one finishing:
  - 39296171234 finished 17:50:36 → 39297386354 started 17:50:39
  - 39297386354 finished 18:01:11 → 39298553614 started 18:01:17
  - 39344072332 finished 00:01:53 → 39344910160 started 00:01:55

  So the real cycle time is about 10 minutes, twice the configured 5. The 15 cancelled poll runs are queued runs that a newer run replaced (inference). We cannot see what fills each 10-minute `poll` step: every summary reports about 832k log characters truncated. `POLL_PHASE_TIMING` has now been recommended in five earlier reports (2026-09-24, 10-16, 10-16-3, 10-18) and is still not in `scripts/`. **Impact: unlocks about 5 minutes of orchestrator latency per hand-off. Confidence: high on the saturation, low on its cause.**
- **All 5 cancelled CI runs look like job timeouts, and the auto-rerun repeated one.**
  - Runs 39246114824, 39246119806, 39257316903, 39257324532 and 39253562024 stopped in job `orchestrate-poll (2)`, step `Promote cycle and blocked-verdict unit tests`, after 1,222–1,226s. That matches `timeout-minutes: 20` (`ci.yml:1961`). Inference: job timeouts.
  - 39253562024 is `run_attempt=2`. `scripts/ci_cancelled_rerun.py:398-399` reruns any `cancelled` run with reason `cancelled_ci` and does not check whether it was a timeout. The rerun hit the same deterministic timeout and wasted 1,226s.
  - Since then, #7697 and #7699 moved the step to `tests-release-and-log-analysis` and split it into 4 shards (`ci.yml:1593`). The 6 CI runs after 13:07 all succeeded (770–1,116s).
  - **Impact: removes 14% CI cancellations and 1.7 runner-hours of wall time per window. Confidence: medium.**
- **Many workflows slowed at once from about 15:20 to 16:40 UTC on 2026-10-19.** Workflows that normally take about 10s took 20–30 minutes. They are in separate concurrency groups, which points to a shared cause, probably a GitHub-hosted runner queue (inference).
  - `cancel_on_pr_close` 39279161942 took 1,978s and was cancelled before its first step.
  - Review autofix sweep 39279001573 took 1,512s and 39282174157 took 1,585s.
  - `workflow_failure_heal_intake` 39280647566 took 1,368s.
  - Poller runs 39278886195, 39280336003 and 39281344654 took 2,649s, 2,432s and 2,288s.

  This window skews the p95 values (`cancel_on_pr_close` p95 786s against a normal 10s). The collector does not split queue time from execution time. **Impact: metric accuracy. Confidence: medium.**
- **Model spend is concentrated in review/autofix and cache reuse is good.** Three Review & Autofix runs made all 32 OpenRouter calls, for 24.39M tokens and `cache_hit_rate` 0.722. Per run:
  - 39253562083: 11.30M tokens, hit rate 0.767
  - 39238096905: 6.94M tokens, hit rate 0.706
  - 39265218438: 6.15M tokens, hit rate 0.655

  Completion tokens are only 1.1% of the total. Uncached prompt is about 160k tokens per call. **Impact: raising every run to 0.767 saves about 1.1M uncached tokens per 3 runs (about 4.5% of input). Confidence: medium, since only 3 of at least 17 review runs report tokens.**
- **The token data has an integrity problem.** The two Implement runs 39237184726 and 39238780375 report exactly the same Codex usage: `codex_calls=11` and `codex_tokens_used=1,322,139`. Both also have `semble_runtime_fallbacks=1`. This may be double-counting (inference). **Impact: the codex total (2.64M) is unreliable. Confidence: medium.**
- **Semble is cheap but slow to start, and Implement never used it.** In review it made 4 queries totalling 58,279 bytes (about 0.06% of tokens), with `static_dup_bytes=0` and no echo lines dropped. Each bootstrap took about 43.4s (173,712ms / 4). In Implement, Semble fell back in both sampled runs and made 0 queries. Serena reported nothing at all: 0 queries and 0 probes. **Confidence: medium.**
- **No AI-memory, `BREAK_GLASS` or `CONTEXT_BUDGET_WARN` signal is visible.** All three counts are 0 across 115 runs with telemetry. The 2026-10-19-2 report counted 4 `CONTEXT_BUDGET_WARN`. With only 3 full review runs parsed, the drop to 0 may be a coverage effect rather than a fix (inference).

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

1. **Find out what fills the poller's 10-minute cycle (critical path).** No code change is made before the measurement.
   - **Evidence:** The `poll` step runs about 9–10 minutes, for example 590s in 39323252041 and 595s in 39311183552. Back-to-back hand-offs (Executive Summary) show the poller is never idle while busy. Each state change (plan done → implement dispatch, review done → merge) waits on average half a cycle plus processing time. That is about 5–10 minutes per hand-off (inference).
   - **Root cause:** unknown. The step log is about 832k characters and truncated in every one of the more than 30 poll summaries.
   - **Exact change:** In `scripts/orchestrate_poll_process.sh`, emit one line per top-level phase:

     ```
     POLL_PHASE_TIMING phase=<name> ms=<int> items=<int> gh_calls=<int>
     ```

     At the end of the cycle, emit one closing line with total `ms`, the slowest phase and the `GH_PAT_BUDGET` delta. Register the prefix through an `agents.d/` fragment (§25).
   - **Estimated savings:** none directly. If one phase takes more than 50% of the cycle, halving it saves about 2.5 minutes per orchestrator hand-off.
   - **Risk:** low (logging only).
   - **Do not** change the `*/5` cron for now. Runs already serialize, so the cron is not the limit.
2. **Keep CI well under its 20-minute limits (critical path for every PR).**
   - **Evidence:** CI p50 is 1,090s and p95 1,225s, against 1,200s limits (`ci.yml:511,862,1171,1443,1961`). According to the `changelog.d/7699` entry, `tests-release-and-log-analysis` used 17m04s of its 20 minutes in 39257527801 before the shards landed.
   - **Exact change:** Have the existing `budget-watch` job (`ci.yml:2466`, `scripts/ci_budget_watch.py`) record the duration of each job after #7699. Split further only any job still above 80% of its limit.
   - **Estimated savings:** about 4–5 minutes per CI run if the shards deliver the 12–13 minutes estimated in the changelog (not yet measured).
   - **Risk:** medium (test grouping).
3. **Semble bootstrap in review/autofix (micro-optimization).**
   - **Evidence:** 4 bootstraps took 42.2–45.1s each, inside 1,702–1,847s runs (about 2.4% of the run).
   - **Exact change:** Log `install_ms` and `index_ms` separately. `build_semble_wrapper.sh:40` already emits both. Check which one dominates before considering an index cache keyed on the base SHA.
   - **Estimated savings:** up to about 40s per review run.
   - **Risk:** low.
4. **Do not tune the skipped runs.** 654 skipped runs have p50 1s and use essentially no runner time. Clarify, plan and implement have family p50 of 1s only because of skips. Their real successful runs take 701–1,048s (plan) and 1,200–2,091s (implement).

## Cost Optimizations

Ranked by expected token savings.

1. **Close the review/autofix cache-hit gap.**
   - **Evidence:**
     - 39265218438: 0.655 hit rate, 421,272 cache-write tokens, 1,668,565 uncached prompt tokens
     - 39253562083: 0.767 hit rate, 601,208 cache-write tokens
   - **Root cause:** unknown. The collector already aggregates `or_phases` (`scripts/cost_audit.py`, the `or_phases` field), but `analysis_context` does not include it.
   - **Exact change:** Put the per-phase `or_phases` breakdown into `analysis_context.json`. Add a stable-prefix hash (`prefix_sha=`) to each OpenRouter usage line so that a varying prompt prefix becomes visible.
   - **Estimated savings:** about 1.1M uncached tokens per 3 review runs (about 4.5% of input), moved to cached reads.
   - **Quality risk:** none (telemetry only).
2. **Large uncached suffix per call.**
   - **Evidence:** about 160k uncached prompt tokens per call (5,127,282 / 32), against about 544k cached reads.
   - **Root cause (inference):** large per-call inputs such as diffs and earlier findings sit after the cache breakpoint.
   - **Exact change:** Log `component_bytes=` (diff, rubric, Semble, memory, prior findings) for each call. The 2026-10-19-2 report proposed this too.
   - **Savings:** not estimable until the components are measured.
3. **Keep Semble in review.** At about 14.6KB per query, 7.25 sources per query and `static_dup_bytes=0`, Semble adds very little context and no duplicated context. Nothing shows it inflating prompts. Its savings cannot be shown either, because there is no comparison run without it.
4. **Avoid deterministic reruns.** The repeated timeout (39253562024 attempt 2) cost 1,226s of CI wall time. See Reliability item 1.
5. **Model or reasoning changes are not supported by this data.** The poll env shows `MODEL_EDITOR: openai/gpt-6-sol` and `MODEL_REASONING_EFFORT_JUDGE: high`. No per-model spend is reported (`claude_models` is absent), so no downgrade is recommended.

## Reliability Improvements

Ranked by expected cut in failures and reruns.

1. **Stop auto-rerunning CI runs that hit their time limit.**
   - **Evidence:** All 5 cancelled CI runs stopped at about 1,225s at the same step. 39253562024's rerun failed the same way.
   - **Root cause:** `ci_cancelled_rerun.py:398-399` reruns every `cancelled` candidate. Only `failure` goes through `classify_runner_shutdown` (`:379-397`).
   - **Exact fix:** For `cancelled`, reuse the same capped per-tick annotation read (`MAX_FAILURE_CLASSIFICATIONS_PER_TICK`). When the annotation says the maximum execution time was exceeded, emit `decision(..., "skip", "job_time_limit")`. Using `job_time_limit` as the reason avoids clashing with the `job_timeout*` names in `review_autofix_step_partial_finalize_safety.sh`.
   - **Impact:** removes about 20 minutes of wasted CI per timed-out PR head.
   - **Fail-open:** if the annotation is unreadable, keep today's behavior and rerun.
2. **Split queue time from execution time in the collector.**
   - **Evidence:** In the 15:20–16:40 window, runs whose steps take about 10s took 280–1,978s. 39287129702 ran steps for 4s of 280s. 39279161942 was cancelled before its first step after 1,978s.
   - **Exact fix:** Have the collector record `queue_ms` (job `started_at` minus run `created_at`) and `exec_ms` for each run. Report p50/p95 on `exec_ms`.
   - **Impact:** stops a platform incident from looking like a regression.
   - **Risk:** none. This is additive JSON and fits within the existing jobs listing, where available.
3. **Find out why Semble fell back in Implement.**
   - **Evidence:** `semble_runtime_fallbacks=1` and `semble_query_calls=0` in both 39237184726 and 39238780375. In review, 4 of 4 bootstraps succeeded with 0 failed and 0 unused.
   - **Interpretation:** a 2/2 failure rate in one workflow, against 0/4 in another, points to a rollout that the fail-open fallback hides, not a rare fallback.
   - **Exact fix:** Make the collector aggregate `SEMBLE_FALLBACK` lines by `reason=` and `target=`. Check that Implement passes the same `SEMBLE_BIN` and index as review.
   - **Separate case:** The 4 `semble_contract_test_fallbacks` in CI 39250289501 are fallbacks the tests trigger on purpose. They are healthy and should be left out of health metrics.
   - **Poller:** `SEMBLE_AVAILABLE: false` with lazy mode and no bootstrap is correct, since nothing was used.
4. **Check the CI step from the 2026-10-19-2 report.** Its proposed `CI_TEST_FAILURE` line per failed test is still useful. In this window there were 0 failures, so the impact here is limited to keeping the timeout diagnosis available.
5. **`BREAK_GLASS` and `CONTEXT_BUDGET_WARN`:** both 0 across 115 runs with telemetry. That indicates no policy or prompt-size pressure, with low confidence because only 3 full review runs are covered.
6. **Node 20 deprecation.**
   - **Evidence:** `actions/cache/restore@v4` is forced onto Node 24 in every poll run. Call sites: `orchestrate_poll.yml:1183,1626,1639,2041`, `review_autofix.yml:2714`, `clarify.yml:1188`, `orchestrate_clarify_respond.yml:744`.
   - **Fix:** Move to the Node-24 major of `actions/cache` once that release is confirmed. The tag was not verified here.
   - **Risk:** low. This is a pinning change.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines are available. The deep-dive folders were not mounted, and every `log_summary` reports none in the visible part of its log. The poll logs, where `orchestrate_poll_process.sh` emits memory operations, are truncated by about 832k characters.

The emitters exist in `scripts/ai_memory.py`, `scripts/memory_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/review_rb_judge.sh`, `scripts/review_consolidate.sh` and `scripts/review_apply_fixes.sh`. As a result:

- Retrieve hit rate is unknown.
- Average `estimated_tokens` against budget is unknown.
- The `keyword_method` mix (`llm` / `plain` / `none`) is unknown.
- `fail_open: true`, `enabled: false` and push-retry counts are unknown.

**Recommendation:** Have the collector scan the full, untruncated step logs for `AI_MEMORY_TELEMETRY:` and add these counters to `cost_telemetry`, the same way as Semble:

- `memory_retrieve_calls`
- `memory_retrieve_hits` (`records_selected>0`)
- `memory_est_tokens_total`
- `memory_fail_open`
- `memory_disabled`

Until then, memory health is **unverified**.

## GH API Call Audit

- **Measured load:** In each rate-limit window, the sweep's `GH_PAT_BUDGET` readings show how fast all other token users are spending calls:

  | Reset epoch | First reading | Second reading | Calls used by others |
  |---|---|---|---|
  | 1792432470 | 4,819 (17:00) | 4,098 (17:33) | 721 |
  | 1792436076 | 4,787 (18:01) | 4,050 (18:40) | 737 |
  | 1792439687 | 4,911 (19:00) | 4,147 (19:31) | 764 |
  | 1792443315 | 4,862 (20:00) | 4,139 (20:35) | 723 |
  | 1792446917 | 4,904 (21:00) | 4,079 (21:32) | 825 |
  | 1792450521 | 4,847 (22:00) | 4,163 (22:32) | 684 |

  That is about 19–26 calls per minute, or about 1,150–1,550 calls per hour. On a 5,000 per hour limit, that is about 23–31% (inference: assumes the standard limit). There were no HTTP 429s or secondary limits.
- **By workflow:**
  - **Sweep:** 2–10 calls per run (`used_in_job`); one run reported `unknown` (39311770218). This is efficient.
  - **`lint_pr_body_auto_close`:** one paginated `pulls/<n>/commits` call per PR. Efficient.
  - **Poller:** this is the likely main user. At about 3 cycles per 32 minutes, it accounts for at most about 190–260 calls per cycle (upper bound, inference). `orchestrate_poll.yml:116,1988` emits `GH_PAT_BUDGET` start and end lines, but no summary quotes the end line, so per-cycle usage is not measured.
  - **`cancel_on_pr_close`:** runs every 5 minutes and found `examined=0 released=0` in every summarized run. The log is truncated by about 33.5k characters, so its call count is unknown.
- **Recommendations:**
  1. Have the collector parse the `GH_PAT_BUDGET phase=end` line for the poll job, and add `gh_calls=` to the proposed `POLL_PHASE_TIMING` lines. Then check §14's existing batch helpers (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`) against whichever phase uses the most calls.
  2. In `cancel_on_pr_close`, skip the merge-train release listing when the previous scan found nothing and no PR event has arrived since. This is low priority because the call volume is unmeasured.
- **Risk reduction:** current headroom is about 70%. Call-count savings cannot be estimated until per-phase counts exist.

## Prompt Cache & Memory System

- **`cache_hit_rate`:** 0.722 overall (17,407,413 cache reads / 24,119,774 input tokens). Per run: 0.767, 0.706 and 0.655. Cache writes average about 49.5k tokens per call, and cache reads about 544k per call. Usage was reported for all calls (32 available, 0 unavailable).
- **Likely fragmentation causes (inference):**
  - The spread between runs, together with about 160k uncached tokens per call, suggests a large dynamic section such as diffs or findings sits after the cache breakpoint.
  - Writes in the lowest-hit run (39265218438) may come from a prefix that changes between calls.
  - Without `or_phases` or a prefix hash, it cannot be shown which.
- **Context-budget pressure:** 0 `CONTEXT_BUDGET_WARN`, so prompt growth is not visibly hurting cache value in this sample.
- **Memory:** retrieval effect unknown (see AI Memory Health).
- **Recommendations:**
  1. Surface `or_phases` and add `prefix_sha=` to OpenRouter usage lines.
  2. Keep timestamps, run IDs and per-iteration findings after the stable rubric and repository context.

  Expected effect: about 1.1M fewer uncached tokens per 3 runs, a small latency gain, and no reliability risk.

## Orchestrator Health

- **Poll cadence:** the poller is saturated, with an effective cycle of about 10 minutes against a 5-minute cron. Of 85 runs, 15 (17.6%) were cancelled; these look like queued runs superseded under `cancel-in-progress: false`, which is benign (inference). Some gaps of about 20 minutes between run creations (18:25→18:45, 20:00→20:21, 22:58→23:18) may be superseded or delayed cron ticks.
- **Clarification loops:** none seen. `orchestrate_clarify_respond` had 130 runs, all skipped.
- **Heal:** all 62 `workflow_failure_heal` runs were skipped. Every summarized `workflow_failure_heal_intake` scan logged `count=0` (for example 39343606495 and 39337180022). There is nothing to heal, which is consistent with 0 failures.
- **Merge train:** all summarized `cancel_on_pr_close` runs logged `MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0`. 39347103240 and 39344306852 warned "preserved 3 active run(s) without pull-request linkage". This is benign, but if it persists, add the run IDs to that warning.
- **Validate:** ran 3 times for `tracking:7679` within 75 minutes (39250902309, 39255841347, 39261068588), taking 775–874s each. Repeated validation of the same tracker is plausible after new wave merges, but the trigger is not logged. Recommendation: add `trigger_reason=` and `head_sha=` to the start line of the validate dispatch.
- **Indicators to track:**
  - poll handoff gap (next run start minus previous run end)
  - poll `exec_ms` p50
  - share of superseded poll runs
  - `CI_CANCELLED_RERUN reason=` counts
  - validate runs per tracking issue per day

## Pipeline Flow Bottlenecks

| Stage | Evidence (successful runs) | Dominant overhead |
|---|---|---|
| clarify | 4 success; 132 skipped | Skip noise only |
| plan | 701–1,048s (39248990234, 39260670411) | Compute |
| implement | 1,200–2,091s (39246349087, 39237184726) | Compute (11 Codex calls per run) |
| CI | p50 1,090s, p95 1,225s; 5 timeouts | Compute close to the limit, plus one repeated timeout |
| review/autofix | at least 17 runs of 784–1,847s; 3 runs at about 8.1M tokens each | Compute and model time |
| validate | 775–874s, 3 runs for one tracker | Compute, possibly repeated |
| poll hand-offs | about 10-minute serialized cycle | Queueing: about 5–10 minutes per transition (inference) |
| platform | 15:20–16:40 window: up to 1,978s on 10s workflows | Runner or queue wait (inference) |

Fixes by end-to-end impact:

1. Measure poll phases and cut the dominant one (affects every transition).
2. Confirm CI headroom after #7699 and add the timeout-aware rerun skip.
3. Separate queue time from execution time in the collector.
4. Investigate review cache and suffix size.

The data shows no merge or conflict overhead: there are no conflict-heal signals, and the merge train released 0.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottlenecks:** the saturated poller (about 10-minute serialized cycles), CI p95 1,225s against 20-minute limits, and review/autofix runs of up to 1,847s.
- **Top failure modes:**
  - 5 CI job timeouts at `Promote cycle and blocked-verdict unit tests`, one of them auto-rerun to the same timeout
  - 15 superseded poll runs (benign)
  - runner or queue stalls between 15:20 and 16:40
  - Semble runtime fallback in 2 of 2 Implement runs
- **Highest-cost drivers:**
  - review/autofix OpenRouter usage: 24.39M tokens over 32 calls in 3 runs
  - Implement Codex usage: 2.64M tokens, possibly duplicated
  - poller runner time: about 15 hours of wall time across 85 runs, including queue
- **Top 3 actions:**
  1. Ship `POLL_PHASE_TIMING` and a closing summary line in `orchestrate_poll_process.sh`.
  2. Add a `job_time_limit` skip to `ci_cancelled_rerun.py` for `cancelled` runs.
  3. Have the collector split `queue_ms`/`exec_ms`, surface `or_phases`, and remove duplicate Codex usage records.

## Metrics Appendix

**Run outcomes by workflow family**

| Family | Runs | Success | Cancelled | Skipped | p50 s | p95 s | Avg s |
|---|---|---|---|---|---|---|---|
| orchestrate_poll | 85 | 70 | 15 | 0 | 622 | 1,159 | 637.4 |
| ci | 35 | 30 | 5 | 0 | 1,090 | 1,225.3 | 1,072.4 |
| review_autofix | 61 | 61 | 0 | 0 | 58 | 1,585 | 473.1 |
| cancel_on_pr_close | 66 | 65 | 1 | 0 | 12 | 786.25 | 119.6 |
| validate | 3 | 3 | 0 | 0 | 832 | 869.8 | 827.0 |
| issue_pr_status | 9 | 9 | 0 | 0 | 231 | 304.2 | 197.2 |
| implement | 131 | 6 | 0 | 125 | 1 | 12 | 73.1 |
| plan | 131 | 4 | 0 | 127 | 1 | 11 | 28.8 |
| clarify | 136 | 4 | 0 | 132 | 1 | 10.25 | 15.4 |
| orchestrate_clarify_respond | 130 | 0 | 0 | 130 | 1 | 10.55 | 2.8 |
| workflow_failure_heal | 62 | 0 | 0 | 62 | 1 | 10 | 3.0 |
| workflow_failure_heal_intake | 54 | 14 | 0 | 40 | 1 | 12 | 29.3 |
| check_failure_triage | 38 | 0 | 0 | 38 | 1 | 11 | 3.4 |
| integration_pr_readiness | 27 | 27 | 0 | 0 | 11 | 41.3 | 13.8 |
| lint_pr_body_auto_close | 24 | 24 | 0 | 0 | 10.5 | 25.65 | 12.7 |
| check_failure_triage_historical | 6 | 6 | 0 | 0 | 8.5 | 13.5 | 9.3 |
| auto_release_stable | 2 | 2 | 0 | 0 | 11.5 | 11.95 | 11.5 |
| **Total** | **1,000** | **325** | **21** | **654** | **2** | **1,072.9** | **151.2** |

The failure rate is 0.0% and the cancellation rate is 2.1%.

**Tokens and cache**

| Metric | Value |
|---|---|
| OR calls (usage available / unavailable) | 32 (32 / 0) |
| OR prompt (uncached) / completion | 5,127,282 / 267,810 |
| OR cache write / read | 1,585,079 / 17,407,413 |
| OR total | 24,387,584 |
| `cache_hit_rate` | 0.721707 (per run: 0.767299 / 0.706346 / 0.654868) |
| Codex calls / tokens | 22 / 2,644,278 (two identical 11 / 1,322,139 records) |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 15,000 / 2,411,840 (115 samples) |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |

**GitHub API**

| Source | Value |
|---|---|
| Sweep `used_in_job` | 2–10 per run (one `unknown`) |
| Consumption by other token users | 684–825 calls per about 31–39 minutes |
| HTTP 429 / secondary limits | 0 visible |
| Poller per-cycle calls | not measured |

**Semble and Serena**

| Metric | Value |
|---|---|
| Semble queries / bytes / sources | 4 / 58,279 (about 14.6KB per query) / 29 |
| Semble bootstraps (failed / unused) / total ms | 4 (0 / 0) / 173,712 (about 43.4s each) |
| Semble fallbacks: contract-test / runtime | 4 (CI 39250289501) / 2 (Implement 39237184726, 39238780375) |
| `static_dup_bytes` / echo lines dropped | 0 / 0 |
| Serena queries / response bytes / tool calls / fallbacks | 0 / 0 / 0 / 0 |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| serena (all workflows) | 0 | 0 | 0 |

Other MCP servers observed: none.

**Data gaps and assumptions**

- The full-log folder `/home/runner/work/_temp/workflow-log-output` was missing, so no deep-dive excerpts were read.
- Token telemetry covers only 3 review runs and 2 Implement runs.
- Poll and `cancel_on_pr_close` logs are truncated in every summary.
- Skipped runs make up 65.4% of the 1,000-run cap, which shortens the window to about 14.5 hours. **Next collection step:** drop `skipped` runs before applying the cap.
- Treating the 1,222–1,226s cancellations as timeouts, and the 15:20–16:40 slowdown as a runner or queue issue, are inferences.
- No repository files were changed in this run. The deliverable is this report.
## Deep Audit — Workflows & Scripts (2026-10-20)

Scope and method: this pass read `CLAUDE.md` (§15 GitHub API hygiene and the headings), `agents.md` ("Repo-specific batching helpers"), `.github/ai/label_contract.v1.json` and `tests/test_workflow_file_size_limit.py`. It then went deep on these files:

- **Workflows:** `cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`, `lint-pr-body-auto-close.yml`, `sync_ai_labels.yml`, `review_autofix_sweep.yml`, `orchestrate_poll.yml` (lines 1–260), `implement.yml` (selected steps) and `workflow-log-analysis.yml` (the analysis, deep-audit and API-redundancy steps).
- **Scripts:** `scripts/gh_helpers.sh` (lines 1–1356), `scripts/review_merge_train.sh`, `scripts/label_helpers.sh`, `scripts/lint_pr_body_auto_close.py`, and selected regions of `scripts/orchestrate_poll_process.sh` (30,562 lines) and `scripts/validate_process.sh`.

Findings already in this report are cross-referenced, not repeated:

- the `ci_cancelled_rerun.py` timeout rerun (Reliability item 1)
- the `actions/cache` Node 24 pin (Reliability item 6)
- `POLL_PHASE_TIMING` (Speed item 1)
- the `cancel_on_pr_close` release listing (GH API Call Audit, recommendation 2)

### Section 1: Bug & Correctness Sweep

- **BUG-001** — `.github/workflows/implement.yml:5534-5551`
  - **Severity:** Medium
  - **Category:** `bug`
  - **Description:** The "Handle no-op implementation" step runs under `set -euo pipefail` (line 5325) and posts its fall-through comment with `gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"`. That call has no `|| true`.
    - `gh_retry` makes only one attempt for a POST create (`gh_helpers.sh:1067-1069`, `_gh_cmd_is_unsafe_post`). One transient failure therefore ends the step.
    - The step dies before lines 5540-5551 apply `ai:implementation-failed` and remove `ai:done`. The issue is left without the label the orchestrator re-issues on. Inference: it stays in `ai:implementing` until stall recovery picks it up.
    - The sibling guards in the same step do tolerate this failure: lines 5371-5372 and 5414-5415 both end in `>/dev/null 2>&1 || true`.
  - **Recommended fix:** Apply the labels before posting the comment, or add `|| echo "::warning::Failed to post no-op comment on #${ISSUE_NUMBER}; labels still applied."` to line 5535. This matches the pattern at lines 5414-5415.

- **BUG-002** — `.github/workflows/cancel_on_pr_close.yml:146-167,239,283,349-385`
  - **Severity:** Medium
  - **Category:** `bug`
  - **Description:** `_cpr_fetch_active_runs` pipes `gh_retry gh api --paginate … | jq -s …`. This runs under `set -euo pipefail` (line 125) and is called bare (`_cpr_fetch_active_runs > "${active_runs_file}"`).
    - If either run listing fails after its retries, the "Cancel queued/in-progress runs" step fails.
    - The next step, "Release merge-queued PRs (merge train)" at line 349, has no `if:`, so it inherits `success()` and is skipped.
    - The merge-train release, which the workflow documents as the immediate release path (lines 350-352: "core-profile repos have no poll backstop"), is lost whenever a run listing fails transiently.
  - **Recommended fix:** Add `if: ${{ !cancelled() }}` to the release step (it already fails open internally, lines 381-385). Make `_cpr_fetch_active_runs` failures non-fatal: wrap the calls at lines 239 and 283 in `if ! …; then echo "::warning::…"; printf '{"workflow_runs":[]}' > "${active_runs_file}"; fi`, so that an empty list means "cancel nothing".

- **BUG-003** — `scripts/orchestrate_poll_process.sh:2106,2111,25908,26255,26364,27117,27139,28522,29629`; `scripts/orchestrate_stall_retired_conflict.sh:37`
  - **Severity:** Medium
  - **Category:** `bug`
  - **Description:** The global `DEFAULT_BRANCH` is assigned only at line 28522 (inside the judge path) and line 29629 (the standalone sweep). Several consumers read `${DEFAULT_BRANCH:-main}` earlier in file order:
    - line 2106 (`_sync_integration_and_rebase_subissue`)
    - line 26255 (`: "${BASE_REF:=${DEFAULT_BRANCH:-main}}"`)
    - lines 27117 and 27139, which also hedge with `|| [ "${BASE_REF}" = "main" ]`
    - `orchestrate_stall_retired_conflict.sh:37`

    `orchestrate_poll.yml` does not export `DEFAULT_BRANCH` (no match in the workflow). In a consumer repo whose default branch is not `main`, the first pass through these paths uses `main`:
    - line 2111 (`git fetch … origin "${default_branch}" …`) fails, and `|| return 0` silently skips the integration sync
    - follow-up PRs can get `BASE_REF=main`

    [NEEDS VERIFICATION]: whether these consumers always run after line 28522 within a cycle. File order is evidence; the runtime order is an inference.
  - **Recommended fix:** Resolve the default branch once near the top of the main body with the memoized helper from API-002. Assign `DEFAULT_BRANCH` from it before the tracking loop, and fail closed (skip the sync and the follow-up base) when it is empty, instead of defaulting to `main`.

- **BUG-004** — `scripts/orchestrate_poll_process.sh:4091,4096-4103,4138-4141`
  - **Severity:** Medium
  - **Category:** `bug`
  - **Description:** `set_issue_phase_label` reads the current labels with `get_issue_labels_json`, which returns `'[]'` on any failure (`… --jq '[.[].name]' || echo '[]'`). It then adds a `--remove-label` only for labels present in that list.
    - When the read fails, no removal flags are built and only `--add-label` runs, so the issue keeps its old phase label next to the new one. Example: `ai:validating` plus `ai:validated`, the exact symptom the comment at 4084-4089 says this code prevents.
    - The endpoint also has no `--paginate`, so an issue with more than 30 labels (REST default page size) is read incompletely. Inference: this is rare.
  - **Recommended fix:** Separate "read failed" from "no labels". On failure, add every `.remove[]` label from `phase_changes` and rely on the existing non-fatal "not found" handling at lines 4119-4122. Add `?per_page=100` to the labels endpoint.

- **BUG-005** — `scripts/review_merge_train.sh:1397-1409`
  - **Severity:** Low
  - **Category:** `bug`
  - **Description:** The release claims a PR by retiring the queue marker (line 1398) and then removing the label with `gh_retry gh api -X DELETE …/labels/<label>`. `_gh_cmd_is_unsafe_post` does not classify DELETE as a create, so it is retried up to 5 times.
    - If the first DELETE succeeds but its response is lost (5xx or network error), the retry gets 404.
    - `_is_gh_permanent_failure` (`gh_helpers.sh:192-195`) classifies that 404 as permanent, so the code logs `MERGE_TRAIN_RELEASE_CLAIM_SKIPPED … action=leave_to_peer` and moves on.
    - The PR is left with no queue label, a retired marker and no review dispatch, and stays that way until another PR event re-evaluates it. [NEEDS VERIFICATION]: how often a lost response happens.
  - **Recommended fix:** Call the DELETE through `gh_api_retry` (`gh_helpers.sh:910-1035`). When `GH_API_RETRY_LAST_STATUS=404` on an attempt after the first, treat the claim as this run's own and dispatch.

- **BUG-006** — `.github/workflows/cancel_on_pr_close.yml:14-16`; `.github/workflows/internal-cancel-on-pr-close.yml:3-7`
  - **Severity:** Low
  - **Category:** `bug`
  - **Description:** The PR-close runs and the `*/5` schedule runs share the group `cancel-on-pr-close-${{ github.repository }}` with `cancel-in-progress: false`. GitHub keeps only one pending run per group, so a pending PR-close run can be replaced by the next schedule tick or by another PR's close.
    - This report's own data shows run 39279161942 cancelled before its first step after 1,978s.
    - The schedule mode (lines 237-274) backstops the cancellation, so the impact is latency of up to 5 minutes, not lost work.
  - **Recommended fix:** Key the group by event: `cancel-on-pr-close-${{ github.repository }}-${{ github.event.pull_request.number || 'schedule' }}`. The release path is already safe to run concurrently: it claims by label removal (`review_merge_train.sh:1403-1405`).

- **SEC-001** — `.github/workflows/lint-pr-body-auto-close.yml:49-56,100-115`
  - **Severity:** Medium
  - **Category:** `security`
  - **Description:** This required check (comment at lines 105-107) enforces CLAUDE.md §19. It runs `python3 scripts/lint_pr_body_auto_close.py` from the default `actions/checkout@v5`, which on `pull_request` is the PR merge ref.
    - A PR that edits `scripts/lint_pr_body_auto_close.py` therefore controls the code that checks it, and can neuter the guard. That is the #2734 incident class the workflow header (lines 2-24) cites.
    - Permissions are read-only, so this is a bypass of the integrity check, not a secret exposure.
  - **Recommended fix:** Check out the base commit for the lint script only. Add a step using `actions/checkout@v5` with `ref: ${{ github.event.pull_request.base.sha }}`, `path: .lint-base`, `sparse-checkout: scripts/lint_pr_body_auto_close.py` and `persist-credentials: false`, then run `.lint-base/scripts/lint_pr_body_auto_close.py`.

- **Security note (no finding):** All attacker-controlled event fields found, such as `github.event.pull_request.title/body/head.ref` and `github.event.comment.body`, reach shell through `env:`. Examples: `lint-pr-body-auto-close.yml:60-61`, `issue_pr_status.yml:208-215`, `plan.yml:699`, `clarify.yml:658`. The only direct `run:` interpolation of an input is `sync_ai_labels.yml:125` (`"${{ inputs.dry_run }}"`). It is a boolean, so it is safe, but it is inconsistent with the env pattern used elsewhere.

### Section 2: GitHub API Call Redundancy Audit

- **API-001** — `scripts/gh_helpers.sh:1271-1335`
  - **Severity:** Medium
  - **Category:** `api-redundancy`
  - **Description:** `gh_api_json_to_file` has no `_is_gh_permanent_failure` check, unlike `gh_retry` (lines 1099-1106) and `gh_retry_to_file` (lines 1184-1192). A 404, 401 or 422 is retried 5 times with sleeps of 1+2+4+8 = 15s.
    - Several callers probe endpoints where 404 is expected: `workflow_failure_heal_intake.sh:498` (`pulls/${ISSUE_NUMBER}`, a PR-or-issue probe), `:505` and `:1794` (`compare/…`), and `workflow_failure_heal_report.sh:235` (`pulls/${ISSUE_NUMBER}`).
    - **Current:** 5 calls and 15s per expected-404 probe. **Proposed:** 1 call and 0s.
  - **Recommended fix:** Insert the same block as lines 1184-1192 into the failure branch at lines 1302-1322: warn, truncate `${outfile}`, and `return 1` on a permanent failure. Batching pattern: none needed. This is retry classification.

- **API-002** — `scripts/orchestrate_poll_process.sh:3837,5122,5586,6337,14928,22410,23216,23388,23679,23830,23875,24281,24351,24428,28522,28867,29629`
  - **Severity:** Medium
  - **Category:** `api-redundancy`
  - **Description:** There are 17 call sites of `gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch'`. Several run once per tracking issue (`DEFAULT_BRANCH_TRACKING` at 23216, 23388, 23679, 23830, 24281 and 24351).
    - The fallbacks also disagree: `|| echo "main"` at 14928, 23216 and 29629, against `|| echo ""` at 3837, 5586 and 24428. On an API error, some paths act on `main`, which feeds BUG-003.
    - **Current:** up to about 17 calls per cycle, plus about 6 per tracking issue. **Proposed:** 1 per cycle.
  - **Recommended fix:** Add a cycle-local memo next to the other caches named in agents.md, using new names that do not collide (none exist today):

    ```bash
    _POLL_DEFAULT_BRANCH_CACHE=""
    _poll_default_branch()
    {
    	if [ -z "${_POLL_DEFAULT_BRANCH_CACHE}" ]; then
    		_POLL_DEFAULT_BRANCH_CACHE="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' 2>/dev/null || true)"
    	fi
    	printf '%s' "${_POLL_DEFAULT_BRANCH_CACHE}"
    }
    ```

    Replace the 17 call sites with it. Each caller keeps its own empty-value handling, made explicit. A failure is not cached, so the next caller retries (fail open). Same pattern as `_candidate_details_json`.

- **API-003** — `scripts/orchestrate_poll_process.sh:22978,23178,23213`; `.github/workflows/orchestrate_poll.yml:280-290`
  - **Severity:** Medium
  - **Category:** `api-redundancy`
  - **Description:** "Find active tracking issues" already runs `gh issue list --label ai:orchestrator-tracking --json number,title,labels` and writes `tracking_issues.json`, which the script loads at 22978 (`TRACKING_ISSUES="$(cat …)"`). Line 23178 still re-fetches `TRACKING_LABELS="$(get_issue_labels_json "${TRACKING_NUM}")"` for every tracking issue on every cycle.
    - **Current:** 1 REST call per tracking issue per cycle. **Proposed:** 0, by seeding from `TRACKING_ISSUES`; re-fetch only after this iteration changes labels (as 23213 already does after `set_tracking_phase_label`).
    - [NEEDS VERIFICATION]: whether anything between the listing and line 23178 changes tracking-issue labels. Example: the merge-train step at `orchestrate_poll.yml:218-245` touches only PR labels.
  - **Recommended fix:** `TRACKING_LABELS="$(printf '%s' "${TRACKING_ISSUES}" | jq -c --argjson n "${TRACKING_NUM}" '[.[] | select(.number == $n) | .labels[].name]')"`. Fall back to `get_issue_labels_json` when the result is empty or jq fails (fail open). Batching pattern: cycle-local cache (CLAUDE.md §15).

- **API-004** — `scripts/orchestrate_poll_process.sh:14783-14794,14830-14835,14891-14896,14993-14995,15030-15033`
  - **Severity:** Medium
  - **Category:** `api-batching`
  - **Description:** Each failure or completion transition repeats the same sequence:
    1. `get_issue_labels_json` (1 call)
    2. `set_tracking_phase_label`, which calls `get_issue_labels_json` again (line 4091) and then `gh issue edit` (2 calls)
    3. separate `gh issue edit --remove-label "ai:validate-failed"` and `--remove-label "ai:harness-broken"` calls (2 calls)

    The two standalone removals run whether or not the label is present, so most of them end in a "not found" 4xx.
    - **Current:** 5 calls per transition. **Proposed:** 2 (one read, one combined edit).
  - **Recommended fix:** Let `set_issue_phase_label` take optional trailing arguments for the labels JSON already read and for extra labels to remove. Use new, unique local names, for example `labels_json_override` and `extra_remove_csv`. Add the extra removals to `edit_args` only when they are present in the labels JSON. The callers at the listed lines pass `"${_tracking_labels}"` and `"ai:validate-failed,ai:harness-broken"`.

- **API-005** — `scripts/review_merge_train.sh:1393,1413-1414,871-887,800-806`
  - **Severity:** Low
  - **Category:** `api-redundancy`
  - **Description:** For each released PR:
    - `_mt_find_marker_comment_id "${num}" "${MT_MARKER}"` pages through all comments (line 1393).
    - `_mt_upsert_comment` is then called without its 4th argument (line 1413), so it pages through all comments again for `MT_RELEASED_MARKER` (line 875).
    - It then makes a third read for the comment body by id (line 879).
    - **Current:** 3 reads (at least 2 paginated) before the write. **Proposed:** 1 paginated read.
  - **Recommended fix:** Add a helper that runs one `--paginate` listing and emits `id<TAB>marker<TAB>body-sha` for both markers. Pass the released-marker id as the existing 4th argument of `_mt_upsert_comment`, and compare a body hash instead of re-reading the body.

- **API-006** — `.github/workflows/review_autofix_sweep.yml:337-348,437-439`
  - **Severity:** Low
  - **Category:** `api-batching`
  - **Description:** The sweep makes one `gh_api_retry --paginate …/actions/workflows/${workflow}/runs -f status=…` call for each of 2 workflows times 3 statuses, every 30 minutes. `cancel_on_pr_close.yml:146-165` already uses the repo-wide `repos/${REPOSITORY}/actions/runs?status=…` endpoint, and the jq at line 380 already filters on `.path`.
    - **Current:** 6 paginated listings per tick. **Proposed:** 3 repo-wide listings filtered on `.path` in jq.
    - [NEEDS VERIFICATION]: repo-wide pages may be larger in busy repos.
  - **Recommended fix:** Loop over the statuses only, list repo-wide, and partition by `.path` in the existing reduce.

- **API-007** — `scripts/lint_pr_body_auto_close.py:143-178,257-268`
  - **Severity:** Low
  - **Category:** `api-batching`
  - **Description:** The lint makes one `gh issue view N --json labels` call per unique `(repo, issue)` reference across the PR body and every commit message, which can be up to 250 commits (`lint-pr-body-auto-close.yml:79-98`). Failed lookups are not cached (lines 267-268), so a failing reference repeats its call.
    - **Current:** U calls, where U is the number of unique references, plus repeats on failure. **Proposed:** ceil(U/50) aliased GraphQL calls per repo.
  - **Recommended fix:** Prefetch all unique numbers with one aliased GraphQL query, modelled on `_cpr_fetch_pr_state_batches` (`cancel_on_pr_close.yml:175-235`) and `_fetch_issue_labels_batch_graphql` (`orchestrate_poll_process.sh:4344-4412`). Cache `None` results as well.

- **API-008** — `.github/workflows/cancel_on_pr_close.yml:349-385`; `.github/workflows/orchestrate_poll.yml:218-245`
  - **Severity:** Low
  - **Category:** `api-redundancy`
  - **Description:** The global merge-train release scan (`_mt_list_open_prs`, paginated) runs both on every 5-minute `cancel_on_pr_close` schedule tick and on every poll tick, in different concurrency groups.
    - **Current:** about 2 open-PR listings per 5–10 minutes. **Proposed:** 1.
    - See the GH API Call Audit, recommendation 2; this adds the concrete gate.
  - **Recommended fix:** In schedule mode, run the release step only when `vars.WORKFLOW_PROFILE == 'core'`, the case the comment at line 352 says has no poll backstop. [NEEDS VERIFICATION]: confirm which install profiles run the poll wrapper.

- **API-009** — `scripts/orchestrate_poll_process.sh:4506-4542,15989,16086`
  - **Severity:** Low
  - **Category:** `api-batching`
  - **Description:** `_find_all_linked_prs` makes 3 calls per issue:
    - a timeline fetch (`--paginate`)
    - `gh pr list --head`
    - `gh pr list --search "#N in:body"`, which draws on the search bucket (30 per minute)

    The callers at lines 15989 and 16086 run per stalled issue. [NEEDS VERIFICATION]: whether these run inside a multi-issue loop in one cycle.
    - **Current:** 3N calls. **Proposed:** about 2 + ceil(N/25) calls: one batched open-PR listing (number, headRefName, body) shared by the branch and body strategies, plus a timeline batch.
  - **Recommended fix:** Prefetch open PRs once per cycle, like `STANDALONE_PRS` at line 29619 but with `body`. Run both regex strategies locally, and keep the timeline lookup through `_fetch_linked_pr_status_graphql`.

### Section 3: Code Duplication & Modularization Opportunities

- **DUP-001** — 10 workflows; the support-ref resolver appears 16 times, but the pinned-commit check only 12 times
  - **Severity:** Medium
  - **Category:** `duplication`
  - **Description:** "Resolve workflow support ref" plus the checkout, fallback, "Ensure" and main-snapshot steps are copied into `sync_ai_labels.yml:37-92`, `cancel_on_pr_close.yml:25-94`, `orchestrate_poll.yml:118-185`, `implement.yml:438-493,720-753,1280-1349,6911-6943`, `clarify.yml:390-431,2355-2370`, `plan.yml:357-396,3160-3175`, `orchestrate.yml`, `orchestrate_clarify_respond.yml`, `workflow_failure_heal.yml` and `check_failure_triage.yml`.
    - The copies have already diverged. The fallback checkout has `continue-on-error: true` in `cancel_on_pr_close.yml:61` but not in `sync_ai_labels.yml:70-78`. Some copies omit the pinned-commit check (see CONSIST-001).
    - The `type gh_retry … || gh_retry() { "$@"; }` shim appears 49 times across 12 workflows.
  - **Recommended fix:** `uses:` cannot take an expression and the support source is not checked out yet, so these blocks cannot become a script. Instead, add a parity test, `tests/test_support_ref_resolver_parity.py`, modelled on `tests/test_skip_ai_marker_rule.py`. It should normalize the workflow file name in the regex and assert that every copy is byte-identical, and that every checkout of `env.*SCRIPT_REF` is followed by the `rev-parse HEAD` pin check before any support code is sourced.

- **DUP-002** — `scripts/label_helpers.sh:161-195`; `scripts/orchestrate_poll_process.sh:4003-4058`; `scripts/validate_process.sh:1222-1256`; `scripts/review_rb_judge.sh:924`; `scripts/review_autofix_step_mark_ready_to_merge.sh:15`; `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:11`
  - **Severity:** Medium
  - **Category:** `duplication`
  - **Description:** There are six `ensure_label_exists` definitions, and they behave differently:
    - Where colors come from: `label_helpers.sh` uses the hardcoded `_AI_LABEL_COLORS` catalog, while the poller and validate versions read `label_contract.v1.json`.
    - Failure return: `label_helpers.sh` returns 1, the others return 0.
    - Caching: only the poller version caches, in `_ENSURED_LABELS_CACHE`.
    - Failure reporting: `validate_process.sh` reports failures through `tg_notify`.

    There are also inline `gh label create` literals at `implement.yml:5359,5406,5493,5544` and `workflow-log-analysis.yml:1079,1878,2478`.
  - **Recommended fix:** Make `label_helpers.sh::ensure_label_exists <label> [repo]` the single owner. Use `label_contract.v1.json` when it is readable and fall back to the catalog. Add a process-level cache under a new unique name (for example `_LABEL_HELPERS_ENSURED_CACHE`). Keep `return 1` on hard failure; callers that need fail-open already append `|| true`. Then delete the poller and validate copies and source the helper, keeping the function name (§10 naming immutability).

- **DUP-003** — `.github/workflows/workflow-log-analysis.yml:1077-1208,1876-1966,2476-2560`; Codex cache steps at 323-348, 905-934, 1744-1773, 2351-2380
  - **Severity:** Low
  - **Category:** `duplication`
  - **Description:** `emit_log_analysis_phase_failure` (1100), `emit_deep_audit_failure` (1890) and `emit_api_redundancy_failure` (2490) are near-identical, about 90 lines each. They build the same `AI_PHASE_FAILURE_V1` payload and differ only in `failed_step_name` and the comment title. `_wla_label_retry` and `_wla_label_create` are defined three times. The "Persist/Restore Codex CLI" steps are copied four times.
  - **Recommended fix:** New `scripts/workflow_log_analysis_phase_failure.sh` with `wla_emit_phase_failure <failed_step_name> <failure_mode> <attempt_count> <failure_summary> <comment_title>`, reading `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_WORKFLOW` and `GITHUB_SERVER_URL` from the default env. The three steps source it. This also removes most of their `${{ }}` usage (EXPR-001/002).

- **DUP-004** — `scripts/apply_analysis_on_main.sh:170`; `scripts/auto_release_stable.sh:93`; `scripts/review_parse_consolidator.sh:102`; `scripts/promote_main_cycle.sh:207`; `scripts/review_consolidate.sh:47`; `scripts/orchestrate_poll_process.sh:964`
  - **Severity:** Low
  - **Category:** `duplication`
  - **Description:** `is_truthy()` is defined six times. Separately, the same truthy normalization is written inline at `review_autofix_sweep.yml:269-271` and `implement.yml:800`. [NEEDS VERIFICATION]: whether all six accept the same token set (`1|true|yes|on`).
  - **Recommended fix:** Move the function to an existing sourced helper, for example `scripts/label_helpers.sh` or a new `scripts/shell_helpers.sh`, guarded with `type is_truthy >/dev/null 2>&1 ||` so existing definitions win while callers migrate.

- **DUP-005** — `scripts/check_failure_triage.sh:207-212`; `scripts/workflow_failure_heal_intake.sh:170-175`; `scripts/workflow_failure_heal_report.sh:56`
  - **Severity:** Low
  - **Category:** `duplication`
  - **Description:** Three fallback `gh_api_json_to_file` shims (`"$@" > "${outfile}"`) are defined for when `gh_helpers.sh` is not staged. They skip JSON validation and do not accept the helper's leading `--idempotent` flag (`gh_helpers.sh:1273-1274`).
  - **Recommended fix:** Keep one shim in `gh_helpers.sh`'s stub path, or document that these scripts require the staged helper. The shim should at least strip `--idempotent` and check `jq empty`.

### Section 4: Expression Size Limit Risk Assessment

CI already enforces an 18,000-character guard on any `run:` body that contains `${{` (`tests/test_workflow_file_size_limit.py:31-32,66-98`). Every body on a green `main` is therefore below the High threshold. The character counts below are estimates from line counts and sampled line lengths, not measurements.

- **EXPR-001** — `.github/workflows/workflow-log-analysis.yml:1068-1401` ("Run workflow log analysis")
  - **Severity:** Medium
  - **Category:** `expression-limit`
  - **Description:** The body is about 333 lines with long lines (for example 1255 and 1334). Estimated length is about 16,000–17,900 characters: roughly 0–2,000 below the CI guard and about 3,000–5,000 below the 21,000 limit. [NEEDS VERIFICATION]: run the test's measurement. All 9 interpolations are `github.run_id`, `github.run_attempt`, `github.workflow`, `github.server_url` and `github.repository` (lines 1113-1115, 1128, 1134-1136, 1173-1175). Each has a default runner env equivalent.
  - **Recommended fix:** Replace them with `${GITHUB_RUN_ID}`, `${GITHUB_RUN_ATTEMPT}`, `${GITHUB_WORKFLOW}`, `${GITHUB_SERVER_URL}` and `${GITHUB_REPOSITORY}`. The body then has no `${{`, which takes it out of the limit entirely (headroom becomes unbounded) at zero behaviour change. Then extract the failure helpers (DUP-003).

- **EXPR-002** — `.github/workflows/workflow-log-analysis.yml:1869-2124` ("Run deep audit pass") and `:2469-2722` ("Run API redundancy pass")
  - **Severity:** Low
  - **Category:** `expression-limit`
  - **Description:** These bodies are about 255 and 253 lines, estimated at about 12,000–14,500 characters each, with 6,500–9,000 characters of headroom [NEEDS VERIFICATION]. Their interpolations are the same `github.*` values (for example 1901-1903, 1915, 1921-1923, 1954-1956 and 2501-2556). Both bodies grow whenever the prompt-assembly logic changes.
  - **Recommended fix:** Use the same default-env substitution as EXPR-001.

- **EXPR-003** — `.github/workflows/implement.yml:5324-5551` ("Handle no-op implementation")
  - **Severity:** Medium
  - **Category:** `expression-limit`
  - **Description:** The body is about 227 lines, including multi-paragraph `printf` comment templates (5351-5358, 5391-5405, 5485-5492). Estimated length is about 14,500–16,500 characters: near the 15,000 Medium line, with about 4,500–6,500 characters to the limit [NEEDS VERIFICATION]. The interpolations at 5329, 5331, 5534, 5542, 5544, 5548 and 5550 are all `github.server_url`, `github.repository` and `github.run_id`.
  - **Recommended fix:** Switch to the default env (`REPO="${GITHUB_REPOSITORY}"` and `RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"`). Move the three comment templates to `prompts/` or to a `scripts/implement_handle_noop.sh` that keeps the step's `name:`, `if:` and `env:` (§24 pattern).

- **EXPR-004** — workflow file sizes (all `.github/workflows/*.yml`)
  - **Severity:** Low
  - **Category:** `expression-limit`
  - **Description:** No workflow is near 800 KB. The real hard limit is 512,000 bytes (§24, `tests/test_workflow_file_size_limit.py:26`), not 1 MB, and CI fails at 480,000. The largest files by line count are `test-and-mark-stable.yml` (7,613 lines), `implement.yml` (6,961) and `review_autofix.yml` (5,718). Byte sizes were not measured here [NEEDS VERIFICATION]. `if:` conditions are small; the largest seen, `implement.yml:5317` and `93-109`, are under 600 characters.
  - **Recommended fix:** Have `budget-watch` (`scripts/ci_budget_watch.py`, which already knows a `workflow_size` kind per `tests/test_ci_budget_watch.py:54`) also report the longest interpolated `run:` body per workflow, so growth toward 18,000 shows up before CI fails.

### Section 5: Cross-Cutting Concerns

- **DEBT-001** — `scripts/orchestrate_poll_process.sh:1-30562`; `scripts/orchestrate_stall_retired_conflict.sh:10-16`
  - **Severity:** High
  - **Category:** `tech-debt`
  - **Description:** The poller, which sits on every orchestrator hand-off (Speed item 1), is one 30,562-line file. `orchestrate_stall_retired_conflict.sh:10-12` records that "ShellCheck's CI run on the ~27,000-line poller did not finish". Since then the file has grown by about 3,500 lines.
    - In practice, static analysis does not cover the most complex script in the repo.
    - The file also mixes indentation styles inside one block: spaces at 23160-23202, tabs at 23203-23228.
    - The size also limits the `POLL_PHASE_TIMING` work recommended in Speed item 1.
  - **Recommended fix:** Extract function families into sourced modules, following the precedent of `orchestrate_stall_retired_conflict.sh` (fail-closed stubs when missing). Candidates:
    - `security_pass_*` (about 6192-9150) → `scripts/orchestrate_poll_security_pass.sh`
    - `validation_history_*` and `mark_validation_*` (about 11533-11765, 14700-15060)
    - `comprehensive_*` (13609-13900)

    Each module should be shellchecked in CI on its own. Register the new files in `REQUIRED_BOOTSTRAP_SCRIPTS` and the staging lists, keeping function names unchanged (§10).

- **CONSIST-001** — `.github/workflows/implement.yml:741-753,767-807`
  - **Severity:** Low
  - **Category:** `consistency`
  - **Description:** The CI-triage PR-not-open hand-off checks out `env.PR_NOT_OPEN_SCRIPT_REF` and sources and runs `.codex-workflow-src/scripts/*`. Unlike the sibling paths at 509-514, 1325-1338 and 6953-6958, it never verifies `git rev-parse HEAD == PR_NOT_OPEN_SCRIPT_REF`, although the comment at 771 says "Only the verified support copy is sourced".
    - Line 804 also lists `.codex-workflow-src-main/scripts/plan_superseded_merged_pr.py` as a candidate helper. That directory is only created at 484-493, which is skipped once `SKIP_IMPLEMENT=true`, and is deleted at 519. The candidate is dead in practice, and if it ever existed it would violate a commit pin.
  - **Recommended fix:** Add the standard pin check (copy lines 6953-6958, then call `blocked_handoff support_ref_mismatch`), and remove the `-main` candidate.

- **CONSIST-002** — `.github/workflows/implement.yml:5359-5360,5406-5407,5493-5494`; `.github/ai/label_contract.v1.json:100-102,156-158`
  - **Severity:** Low
  - **Category:** `consistency`
  - **Description:** Inline label creation conflicts with the contract:
    - `ai:closed` is created with `6b7280` and "Issue closed by automation (superseded, duplicated, or already complete)"; the contract says `6a737d` and "Linked PR closed without merge".
    - `ai:needs-human` is created with `b60205`; the contract says `e11d48`.

    Whichever path creates the label first wins until `sync_ai_labels.yml` corrects it.
  - **Recommended fix:** Replace these with `ensure_label_exists` from the staged `label_helpers.sh`, as lines 5540-5546 already do (DUP-002).

- **CONSIST-003** — `.github/workflows/lint-pr-body-auto-close.yml:85-86`; `.github/workflows/review_autofix_sweep.yml:228-237,285`; `.github/workflows/workflow-log-analysis.yml:1164,1948`; `.github/workflows/implement.yml:2166,2392,2411-2412,2434`
  - **Severity:** Low
  - **Category:** `consistency`
  - **Description:** These calls use raw `gh api` or `gh issue comment` with no retry, even though the same files elsewhere use `gh_retry` or `gh_api_retry` (for example `review_autofix_sweep.yml:340`). In the sweep, line 228 runs under `set -euo pipefail` (line 213), so one transient 5xx while listing PRs fails the whole tick.
  - **Recommended fix:** Use `gh_api_retry` for reads (exit codes 0/1/2/75 are already handled in the same step). For non-idempotent comment posts, keep a single attempt but log a warning.

- **DEAD-001** — `scripts/gh_helpers.sh:91,107-108`
  - **Severity:** Low
  - **Category:** `dead-code`
  - **Description:** `gh_review_pr_state` accepts `merged` from `repos/{r}/pulls/{n} --jq .state`. The REST pulls `state` field is only ever `open` or `closed`, so that branch never matches.
  - **Recommended fix:** Drop `merged` from the regexes, or read `.merged` explicitly if callers need the distinction.

- **DEAD-002** — `.github/workflows/implement.yml:103-108`; `.github/workflows/internal-implement.yml:26-31`
  - **Severity:** Low
  - **Category:** `dead-code`
  - **Description:** The `github-actions[bot]` plus `[auto-approved-by-plan]` trigger branch cannot fire. Comments posted with `GITHUB_TOKEN` do not start workflow runs. The auto-approval at `plan.yml:2871-2872` goes through `gh_retry gh api …/comments` with the step's token, so it matches the `User` branch when that token is `GH_PAT`. [NEEDS VERIFICATION]: the `GH_TOKEN` bound for that `plan.yml` step.
  - **Recommended fix:** If the token is confirmed as `GH_PAT`, remove the Bot branch from both files, or document why it is kept.

- **DEAD-003** — `.github/workflows/implement.yml:2204-2210,2241-2254`
  - **Severity:** Low
  - **Category:** `dead-code`
  - **Description:** The comment at 2241-2247 says labels come from the snapshot fetched in "Fetch issue metadata". In fact, line 2204 just re-fetched the issue and line 2210 overwrote `ISSUE_META_FILE`. The API fallback at 2252-2254 is reachable only if jq fails on a payload that was just fetched successfully.
  - **Recommended fix:** Fix the comment to describe the deliberate re-check before the phase change, and drop the redundant fallback call.

- **SHELL-001** — `.github/workflows/review_autofix_sweep.yml:350,445-448`; `scripts/review_merge_train.sh:1349-1352`; `scripts/orchestrate_poll_process.sh:4371-4372`
  - **Severity:** Low
  - **Category:** `shellcheck`
  - **Description:** These lines fork jq too often:
    - SC2002 (useless `cat`): `cat "${snap_raw}" | jq …`
    - four jq forks per row (sweep 445-448, merge train 1349-1352)
    - one jq fork per array element when building the GraphQL fragment (4372)
  - **Recommended fix:** Use one `jq -r '[.number,.head,.base,(.labels|join(","))] | @tsv'` per stream with `IFS=$'\t' read -r …`, `jq … "${snap_raw}"` directly, and `jq -r '.[]'` once into an array before the fragment loop.

- **SHELL-002** — `scripts/orchestrate_poll_process.sh:23160-23228` (and the file generally)
  - **Severity:** Low
  - **Category:** `shellcheck`
  - **Description:** Tabs and spaces are mixed within one control block. CLAUDE.md §9 asks for tabs. This makes nesting hard to review in a file ShellCheck cannot finish (DEBT-001).
  - **Recommended fix:** Normalize indentation only inside modules as they are extracted under DEBT-001. Do not reformat the whole file (§9 minimal change).

- **TODO/FIXME/HACK markers:** a scan for `#.*\b(TODO|FIXME|HACK)\b` across `scripts/` found none. Earlier matches on `XXXXXX` were `mktemp` templates, not markers. There is no marker-based debt to report.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | DEBT-001 |
| Medium | 12 | BUG-001, BUG-002, BUG-003, BUG-004, SEC-001, API-001, API-002, API-003, API-004, DUP-001, DUP-002, EXPR-001 |
| Low | 20 | BUG-005, BUG-006, API-005, API-006, API-007, API-008, API-009, DUP-003, DUP-004, DUP-005, EXPR-002, EXPR-003, EXPR-004, CONSIST-001, CONSIST-002, CONSIST-003, DEAD-001, DEAD-002, DEAD-003, SHELL-001, SHELL-002 |

Correction: EXPR-003 is rated Medium in Section 4, so the Medium row should list 13 findings and the Low row 20:

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | DEBT-001 |
| Medium | 13 | BUG-001, BUG-002, BUG-003, BUG-004, SEC-001, API-001, API-002, API-003, API-004, DUP-001, DUP-002, EXPR-001, EXPR-003 |
| Low | 20 | BUG-005, BUG-006, API-005, API-006, API-007, API-008, API-009, DUP-003, DUP-004, DUP-005, EXPR-002, EXPR-004, CONSIST-001, CONSIST-002, CONSIST-003, DEAD-001, DEAD-002, DEAD-003, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/orchestrate_poll_process.sh`, new `scripts/orchestrate_poll_*.sh` modules, staging/bootstrap lists (DEBT-001) | Large |
| API call optimization | `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/review_merge_train.sh`, `scripts/lint_pr_body_auto_close.py`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/cancel_on_pr_close.yml` | Medium |
| Code modularization | `scripts/label_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `scripts/validate_process.sh`, `scripts/review_rb_judge.sh`, two `scripts/review_autofix_step_*.sh`, `.github/workflows/workflow-log-analysis.yml`, new `scripts/workflow_log_analysis_phase_failure.sh`, new parity test under `tests/` | Medium |
| Expression size reduction | `.github/workflows/workflow-log-analysis.yml`, `.github/workflows/implement.yml` | Small |
| Medium/Low fixes | `.github/workflows/implement.yml`, `.github/workflows/cancel_on_pr_close.yml`, `.github/workflows/lint-pr-body-auto-close.yml`, `.github/workflows/internal-implement.yml`, `scripts/review_merge_train.sh`, `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh` | Medium |