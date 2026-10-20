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