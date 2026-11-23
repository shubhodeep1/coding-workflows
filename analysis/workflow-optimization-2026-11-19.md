## Executive Summary

- **Data gaps.** `shubhodeep1/coding-workflows` is listed in `coverage.repositories_truncated`, and `page_limit_reached=true`. Only 1,000 runs were listed, covering about 2026-11-18 21:20 to 2026-11-19 04:50 UTC (around 7.5 hours). The run-logs directory `/home/runner/work/_temp/workflow-log-output` was not mounted, so none of the `errors/`, `slow/` or `recent/` logs could be read. Everything below rests on the run rows in `analysis_context.json` plus the current code. Confidence: high.
- **71% of CI failures (15 of 21) fail at the step "Verify the final-CI merge ref is current", not in a test.** The cause can't be split into real conflicts versus pipeline fetch or stale-ref failures. The script logs a `CI_FINAL_MERGE_REF ... outcome= reason=` line, but the collector never reads it. PR 9687 failed this way 3 times (runs 44425620485, 44433687193, 44447795119) before it passed in run 44455781141, about 4 hours later. Fix: have the collector parse that line (R1). Impact: separates PR conflicts from pipeline faults across 71% of CI failures. Confidence: high on the count, low on the cause.
- **Runner saturation is the main latency driver (inference).** At least 8 final-CI runs (`CI [pr:N]`) started within 3 minutes (21:20:46 to 21:22:46). Each took 1.4 to 2 hours (44400752516 7,016s, 44400849257 7,120s). A final CI in a quiet period took 709 to 871s (44457599657, 44455781141). Each final run fans out to 24 `tests-system-python` shards plus about 10 other jobs. Run 44460965054 spent 137 of its 147s waiting on "All GitHub-hosted runners … busy". Fix: a fail-open cap on in-flight final-CI dispatches (S1). Estimate: early PRs in a burst finish in about 15 to 25 minutes instead of 90 to 120. Confidence: medium.
- **Six review runs used all 98.07M OpenRouter tokens, and two used 56% (55.1M).** Runs 44432729574 (32.41M) and 44430397475 (22.73M). The cache hit rate is a healthy 0.926, so the spend is volume, not cache misses. It can't be pinned to a phase because per-run telemetry drops `or_phases` and the Claude counters. Three review runs of 2.3 to 3 hours report zero tokens. Fix: carry those fields per run (C1). Confidence: high on the numbers, medium on the gap.
- **Base push CI was red four times in the window.** Runs 44433197015, 44442112178, 44447007404 and 44453299520. While the base is red, the review sweep holds back its base sync, so PRs whose merge-ref check failed stay stuck (inference). The contract and size-guard step runs three scripts under `set -e`, so the log does not say which one failed (R3). Confidence: medium.
- **Review/autofix runs are cancelled late and at high cost.** 42 of 173 runs (24%) were cancelled, some deep into model steps: 44447007564 after 6,554s in "Run reviewer models", and 44438174127 after 3,102s in "Apply fixes with editor model". Fix: classify cancellations as superseded or not, in the collector only (C2). Confidence: medium.

## Speed Optimizations

**S1 (critical path). Cap in-flight final-CI dispatches; defer the excess to the next sweep tick.**
- **Evidence:**
  - Burst of `CI [pr:N]` dispatch runs: 44400501335 (21:20:46, 5,170s), 44400674395 (5,464s), 44400680297 (6,309s), 44400689507 (5,456s), 44400698294 (6,842s), 44400752516 (7,016s), 44400801413 (6,049s), 44400849257 (7,120s).
  - Review runs in the same minutes took 5,780 to 6,038s (44400447016, 44400526149, 44400761038).
  - Quiet-period final CI: 44457599657 at 709s and 44455781141 at 871s.
  - The `log_summary` of 44460965054 says the job queued from 04:49:46 to 04:52:03 because "All GitHub-hosted runners with label [ubuntu-latest] are busy".
  - CI family p50 is 962s and p95 is 6,088s.
- **Root cause (inference):**
  - `tests-system-python` has a 24-leg matrix (`ci.yml:4261`), so each final run puts about 34 jobs into the shared pool.
  - `review_final_ci_sweep.sh` caps candidates checked per tick (`FINAL_CI_SWEEP_MAX_PER_TICK`, default 10), not runs in flight.
  - `_pr_ensure_final_ci` (`pr_checks_lib.sh:984`) dispatches whenever its own head has no run, whatever else is queued.
- **Exact change:**
  - Add `FINAL_CI_MAX_IN_FLIGHT` (default `0` = off, to preserve behaviour; recommended rollout value about 4).
  - In the sweep, count queued and in-progress `workflow_dispatch` runs from the `runs?per_page=100` listing it already reads, and stop calling `_pr_ensure_final_ci` once the cap is reached. This needs no new API call.
  - In `_pr_ensure_final_ci`, when the cap is set, return `outcome=deferred reason=in_flight_cap` instead of dispatching. The sweep then picks the head up, since it already handles heads that are approved but have no run.
  - Fail open: if the listing can't be read, dispatch as today.
- **Estimated savings (inference):** in a burst of 8 or more, the first PRs get CI in about 15 to 25 minutes instead of all finishing at 90 to 120. Review runs stop competing with about 270 CI jobs. Total throughput is unchanged, because the pool is the bound.
- **Risk:** medium. It adds up to one sweep tick of latency per deferred PR, and per §26 the default stays off until it is measured.
- Finding check: new. No prior verdict on capping final-CI runs in flight. The sweep caps only per-tick candidates (`scripts/review_final_ci_sweep.sh:20,279`).

**S2 (measurement for S1). Derive per-job queue wait from job data the collector already fetches.**
- **Evidence:** the queue share of 44460965054 is known only from its `log_summary` (137s of 147s). `collect_workflow_logs.py` computes only `run_started_at` to `updated_at` (lines 1150 to 1154), even though `jobs_fetch_status=success` on all 21 failing runs.
- **Change:** add `queue_seconds_p50` and `queue_seconds_max` per run, computed as job `started_at` minus `created_at` from the jobs payload already in hand. No new API call.
- **Savings:** none directly. It confirms or refutes the S1 inference before the cap is turned on.
- **Risk:** low, additive fields only.
- Finding check: reopened. "Queue timestamps" was rejected as "no defect shown" in `analysis/recommendation-processing-report.md:1607,1633,2053`. This window shows the defect: runner-busy queueing and final-CI runs 7 to 10 times their quiet-period duration.

## Cost Optimizations

**C1. Keep `or_phases` and the Claude counters in per-run cost telemetry.**
- **Evidence:**
  - All 98,070,826 OpenRouter tokens (48 calls) come from six review runs: 44432729574 (32.41M, 9 calls, 557,706 completion), 44430397475 (22.73M), 44409287787 (15.84M), 44412099044 (15.20M), 44402487688 (7.60M) and 44402504702 (4.29M).
  - One run, 44432729574, used 43% of all completion tokens.
  - The review runs 44402578311 (10,983s), 44415595858 (9,317s) and 44417007560 (8,368s) all report `or_calls=0` and `codex_calls=0`.
- **Root cause:**
  - `build_run_cost_telemetry` (`cost_audit.py:609-613`) copies only `RUN_COST_TELEMETRY_FIELDS`.
  - `or_phases` (line 704) and `CLAUDE_COUNT_KEYS` / `claude_cost_usd` are parsed but dropped before they reach `analysis_context.json`.
  - So reviewer and editor spend can't be told apart, and Claude-engine runs look free (the zero-token runs being Claude-engine is an inference).
- **Exact change:** in `build_run_cost_telemetry`, add `telemetry["or_phases"] = parsed["or_phases"]` and the `CLAUDE_COUNT_KEYS` and `claude_cost_usd` values. These are new keys, so `aggregate_run_cost_telemetry` ignores them and nothing downstream changes.
- **Estimated savings:** none directly. It points the next cut at the 56% of spend concentrated in two runs.
- **Quality risk:** none.
- Finding check: reopened. "Cost 2–5 per-phase usage" was rejected as having "no defined consumer" (`analysis/recommendation-processing-report.md:1633`). This window shows unattributable concentration (33% of tokens in one run) and three multi-hour runs with zero recorded tokens. The consumer is this report's Cost section.

**C2. Classify cancelled review and poller runs in the collector as superseded or not.**
- **Evidence:**
  - Late review cancellations: 44447007564 after 6,554s at "Run reviewer models"; 44400761038 (6,038s) and 44400727497 (5,943s), same step; 44438174127 after 3,102s at "Apply fixes with editor model"; 44400651243 after 5,951s at "Record review run completion in memory (fail-open)".
  - Review runs cancelled before their first step after about 20 minutes: 44456638595, 44456620269, 44456596014.
  - Poller: 17 of 37 `orchestrate_poll` runs cancelled, including 44460988126 (`cancelled_before_first_step`, 360s).
- **Root cause:** unknown. A cancellation could be a newer head, the concurrency group or manual, and the model spend of these runs is not in the deep-dive set.
- **Change:** analysis only, with no new emitter. For each cancelled run, check the listing it already holds for a newer run of the same workflow and the same `[pr:N]` that was created before the cancellation. Mark the run `superseded=true/false`.
- **Savings (inference):** if most late cancellations are not superseded, they are wasted reviewer and editor sessions at about 2M tokens per call.
- **Risk:** low.
- Finding check: reopened. "Cancellation-provenance telemetry" was rejected because the incidents "do not reproduce" and it had no reader (`analysis/recommendation-processing-report.md:1310`). This window reproduces the incidents (5 runs cancelled at model or memory steps after 51 to 109 minutes), and the change needs no new emitter.

Semble and Serena:
- Semble added 6 queries, 86,780 bytes and 46 sources, with `static_dup_bytes=0`. That is about 0.4% of the 5.53M uncached prompt tokens (inference, at about 4 bytes per token), so it is not inflating prompts.
- Its 6 bootstraps took 285,568 ms in total (47.6s on average). That is about 2% of the p50 review duration of 2,049s, which makes it a micro-optimization and not recommended.
- Serena logged no lines at all.

## Reliability Improvements

**R1. Have the collector parse the `CI_FINAL_MERGE_REF` outcome (diagnostic; highest priority).**
- **Failure evidence:**
  - 15 of 21 failures happen at "Verify the final-CI merge ref is current": 44400698294, 44400680297, 44400674395, 44400501335, 44445920977, 44409493728, 44441425813, 44425868575, 44425834902, 44426960886, 44447795119, 44425620485, 44433687193, 44432928051 and 44432157506.
  - Together they account for 44,664s of run wall-clock.
  - The failing job differs from run to run: `static-checks`, `lint`, `tests-system-python (17)`, `(21)`, `tests-sandbox-python (review)` and others.
  - PR 9687 failed 3 times and PR 9705 twice.
- **Root cause category:** unknown, and that is the gap. `ci_final_merge_ref_fresh.sh` exits 1 for several different reasons:
  - `conflict` (`not_mergeable` or `merge_conflict`): a real PR problem.
  - `stale` with `merge_fetch_failed`, `merge_base_too_deep`, `merge_tree_unsupported` or `merge_failed`: pipeline or base problems.
  - The script logs `CI_FINAL_MERGE_REF pr= head= outcome= reason= … pin=` (line 108), but a search of `scripts/` finds no parser outside the script itself.
- **Exact fix:** in the collector's per-run log parsing, extract the last `CI_FINAL_MERGE_REF` line of the failing job into `final_ci_merge_ref_outcome`, `final_ci_merge_ref_reason` and `final_ci_merge_ref_pin`. Add a count by reason to the summary.
- **Expected impact:** directly classifies 71% of CI failures. It decides whether R2 is needed.
- **Fail-open:** a missing line leaves the fields null.
- Finding check: new. The processing report has no prior verdict on merge-ref telemetry, and the parser is absent on this ref.

**R2. Retry the transient `git fetch` calls in the merge-ref script (do this only if R1 confirms fetch failures).**
- **Evidence (inference):**
  - A real conflict is deterministic for the run's pinned base, so it should fail every job of the run.
  - Yet the failure point is a single, different job in each run. In 44445920977 it is `tests-system-python (21)` of 24.
  - That pattern fits per-job transient fetch failures better. `ci_final_merge_ref_merge_locally` turns `merge_fetch_failed` into `stale` and exit 1 whenever an earlier fetch in the same step worked (lines 238 to 240 and 459 to 469). Up to 34 jobs each fetch 200 commits deep at once.
- **Exact fix:**
  - Wrap the three `git fetch` calls in the script (lines 201, 213 and 238) in a retry loop: at most 3 attempts, 5s apart.
  - Add `attempts=` to the `CI_FINAL_MERGE_REF` line.
  - Add `CI_FINAL_MERGE_REF_FETCH_ATTEMPTS` with default 3.
- **Impact:** if R1 shows `merge_fetch_failed`, this could remove up to 15 failures and their base-sync cycles. PR 9687 alone took about 4 hours to go green.
- **Rollback:** set the env var to 1. Conflict handling is unchanged.
- Finding check: new. No prior verdict, and the fetches have no retry on this ref.

**R3. Label each script in the contract and size-guard step so a failure names its script.**
- **Evidence:** "Review autofix step-script contract and workflow file size guard tests" failed in 44400752516 (`CI [pr:9359]`) and in the non-PR runs 44442112178 and 44453299520. The step (`ci.yml:2170-2175`) runs three scripts under `set -euo pipefail`. The failure point can't say whether the contract test, `test_workflow_file_size_limit.py` (the §24 guard) or the mark-stable contract test failed.
- **Exact fix:** before each script, print a line naming it, `CI_STEP_SCRIPT name=<file>`. Behaviour and exit codes stay the same.
- **Impact:** gives each base-red event a named cause on its first occurrence. A §24 size-limit regression would also stop GitHub from starting `review_autofix.yml` runs at all.
- **Risk:** none.
- Finding check: new. No prior verdict. The step bundles three scripts without markers on this ref. Whether `review_autofix.yml` is currently near 480,000 bytes is unverified, because `wc -c` was not available in this sandbox.

**R4. Fix the red base CI that blocks base syncs.**
- **Evidence:**
  - "Orchestrate poll process unit tests" failed in 44447007404 (`orchestrate-poll (1)`) and 44433197015 (`orchestrate-poll (2)`).
  - The `log_summary` of 44460965054 reports `tests/test_orchestrate_poll_process.py` timing out, `tests/test_orchestrate_poll_promote_cycle.py` hitting `baseline_timeout`, and an inherited failure in `tests/test_ci_poll_test_sharding.py`.
  - The sweep skips base syncs "while the base's newest completed push run … failed" (`review_final_ci_sweep.sh:68-71`).
- **Fix:** none can be specified without the logs. Use the per-test events (`TEST_CASE_EVENT`) that already exist to find the slow or failing cases.
- Finding check: unverified. The run logs were not mounted, so the cause could not be read.

`BREAK_GLASS` and `CONTEXT_BUDGET_WARN`: 0 and 0 across all 36 telemetry runs. There is no policy or prompt-size pressure.

Semble fallbacks: 28 `SEMBLE_FALLBACK` lines, all contract-test (`semble_runtime_fallbacks=0`), 4 each in 7 CI runs (44442112178, 44453299520, 44447007404, 44425868575, 44425834902, 44433197015, 44433816047). This is healthy synthetic fail-open and not a recommendation; it was dropped as already implemented. Serena probes: 0 ok, 0 failed, 0 skipped.

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` lines were available because the deep-dive logs were not mounted. Retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, and `fail_open` or `enabled:false` entries can't be computed.
- One related signal: 44400651243 was cancelled at "Record review run completion in memory (fail-open)" after 5,951s, which suggests a memory write may have been lost (inference).
- **Recommendation:** re-run this analysis with the logs directory mounted, and check that `AI_MEMORY_TELEMETRY` `retrieve` and `record-run-event` lines appear in the review deep-dive logs.
- Finding check: unverified. The run logs (`/home/runner/work/_temp/workflow-log-output`) could not be read.

## GH API Call Audit

There are no GitHub API call counts or rate-limit events in this window's context, which is a data gap. One hotspot can be derived from the code:

**G1. Each final-CI job repeats `GET pulls/<n>` in "Verify the final-CI merge ref is current".**
- **Evidence:**
  - The step appears in 10 jobs of `ci.yml` (lines 164, 902, …, 4864). One of them is the 24-leg `tests-system-python` matrix.
  - Each job calls `ci_final_merge_ref_pull '.base.ref'` (`ci_final_merge_ref_fresh.sh:337`), plus up to 6 more `pulls/<n>` reads when the ref is stale (lines 371 to 372).
  - That makes at least about 34 identical reads per final run, and about 270 in the 21:20 burst of 8 runs. The `GITHUB_TOKEN` budget is per repository.
  - This is inferred from the code, not from counted calls.
- **Exact change:**
  - Add an optional `workflow_dispatch` input `base_ref` (default `""`), set by `_pr_ensure_final_ci` when its caller already knows the base.
  - In the script, use it only as a hint for the fresh case: take the tip with `git ls-remote` (no API call). If the merge ref's first parent equals that tip, the tree tested is the same one GitHub merged whatever the name, so skip the GET.
  - Otherwise fall back to the existing `pulls/<n>` read. A wrong or forged hint can't change which tree is tested.
- **Estimated reduction:** about 33 GETs per fresh final run, from about 34 to 0 or 1.
- **Rate-limit risk:** lower. Today a failed read is fail-open (`base_ref_unreadable`), which means the run tests without verification.
- Finding check: new. No prior verdict. The per-job read is documented as unavoidable in the script header (line 102) only because it assumes no earlier source of the base ref.

## Prompt Cache & Memory System

- `cache_hit_rate` is 0.926 overall: 89,615,567 cache-read tokens against 1,623,370 cache-write and 5,530,839 uncached prompt tokens. Reads outnumber writes 55 to 1.
- Per run, the rate ranges from 0.849 (44402504702, 178,631 written) and 0.887 (44402487688, 250,798 written) up to 0.950 (44432729574). The two lowest runs both started at 21:32 inside the burst. Whether they wrote a cold prefix after a base move, or a per-phase prefix changed, can't be told without `or_phases` (inference).
- There were no `CONTEXT_BUDGET_WARN` lines, so prompt growth is not eroding cache value.
- **Action:** C1 (per-phase usage per run) is the only change needed to find where the cache writes come from. No separate prompt-ordering change is recommended, since stable-prefix ordering is already implemented according to the report (line 1311).
- Finding check: reopened (the same finding as C1, `analysis/recommendation-processing-report.md:1633`).

## Orchestrator Health

- `orchestrate_poll`: 37 runs, 20 successful, 17 cancelled (46%), 0 failed. p50 is 867s, p95 is 2,456s, and the longest run was 44401425841 at 4,174s.
- Run 44460988126 was cancelled before its first step after 360s, which looks like the next poll replacing it while it waited for a runner (inference).
- Clarify, plan and implement are almost entirely skipped wrapper runs (77 of 80, 76 of 78 and 74 of 77). Only 3, 2 and 3 runs did work. No clarification loops or conflict-heal retries can be seen without the logs.
- Indicators to track: the poller's cancelled share, split by C2's superseded flag; `POLL_PROGRESS` per tick; and the `REVIEW_FINAL_CI_BASE_SYNC outcome=skipped` reasons while the base is red (R4).
- **Action:** C2's superseded classification covers poller cancellations as well. Cadence retuning stays deferred according to the prior report (line 795), and no new evidence answers that.
- Finding check: reopened (the same finding as C2, `analysis/recommendation-processing-report.md:1310`).

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact:
1. **Queueing at final CI and review (dominant).** CI p95 is 6,088s and review_autofix p95 is 8,187s, against quiet-period final runs of 709 to 871s. Fix: S1, measured by S2.
2. **Retry and merge overhead at the merge-ref check.** 15 failed final runs. Each failed head waits at least 30 minutes for a base sync (`FINAL_CI_SWEEP_BASE_SYNC_MIN_AGE_MINUTES`, default 30, at most 3 per PR) and then runs the full suite again. PR 9687 took 3 cycles, about 4 hours. Fix: R1, then R2.
3. **A red base blocks recovery.** Four failed non-PR CI runs (R3, R4) halt base syncs for every PR with a failed merge-ref check.
4. **Queue noise from skipped runs.** 304 skipped runs come from the four `issue_comment` wrappers (clarify, plan, implement, clarify-respond), which fire together on the same second (for example 04:50:33 for 44461057196, 44461057259, 44461057283 and 44461057287). They take no runner but flood the queue and the API (§26). Plan item 15 in `docs/plans/reduce-actions-run-volume-plan.md`, the opt-in `AI_COMMENT_SIGNING_SECRET`, is the existing remedy. Whether it is enabled can't be seen from this window.
   - Finding check: unverified. The repository secret state can't be read, and the ratio of about 99% skipped suggests it is off (inference).

## Per-Repo Breakdown

### shubhodeep1/coding-workflows (truncated: 1,000 runs, about a 7.5-hour window)

- **Top bottlenecks:**
  - Runner queueing in final-CI and review bursts (final CI at 21:20 to 21:22 took 1.4 to 2 hours).
  - Base-sync recovery after a failed final run.
- **Top failure modes:**
  - The merge-ref freshness step: 15 of 21 failures, cause unclassified.
  - Red base CI in the contract and size-guard step (3) and the orchestrate-poll unit tests (2).
  - Late review cancellations: 42 of 173.
- **Highest-cost drivers:** review/autofix OpenRouter sessions. Six runs used 98.07M tokens, two of them 55.1M, at about 2M tokens per call. Three multi-hour review runs recorded no tokens at all.
- **Prioritized actions:**
  1. Parse `CI_FINAL_MERGE_REF` outcome and reason in the collector (R1).
     Finding check: new. No parser exists outside `scripts/ci_final_merge_ref_fresh.sh`, and there is no prior verdict.
  2. Add a fail-open cap on in-flight final-CI dispatches, default off (S1).
     Finding check: new. `scripts/review_final_ci_sweep.sh` caps candidates per tick only, and there is no prior verdict.
  3. Keep `or_phases` and the Claude counters per run (C1).
     Finding check: reopened. The rejection at `analysis/recommendation-processing-report.md:1633` cited no consumer; this window shows one run with 33% of tokens and three runs with zero tokens.

## Metrics Appendix

**Runs and outcomes (sampled; `success_sample_rate=0.07`)**

| Scope | Runs | Success | Failure | Cancelled | Other/skipped | Failure rate | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| Repo total | 1000 | 478 | 21 | 65 | 436 | 2.1% | 111 | 5162 |
| ci | 98 | 73 | 21 | 4 | 0 | 21.4% | 962 | 6088 |
| review_autofix | 173 | 131 | 0 | 42 | 0 | 0% | 2049 | 8187 |
| orchestrate_poll | 37 | 20 | 0 | 17 | 0 | 0% | 867 | 2456 |
| clarify / plan / implement / clarify_respond | 80 / 78 / 77 / 77 | 3 / 2 / 3 / 0 | 0 | 0 | 77 / 76 / 74 / 77 | 0% | 1 | ≤11 |

**CI failure points (21)**

| Step | Count | Run IDs |
|---|---|---|
| Verify the final-CI merge ref is current | 15 | 44400698294, 44400680297, 44400674395, 44400501335, 44445920977, 44409493728, 44441425813, 44425868575, 44425834902, 44426960886, 44447795119, 44425620485, 44433687193, 44432928051, 44432157506 |
| Review autofix step-script contract and workflow file size guard tests | 3 | 44400752516, 44442112178, 44453299520 |
| Orchestrate poll process unit tests | 2 | 44447007404, 44433197015 |
| System-Python test shard | 1 | 44409500182 |

**Tokens and cache**

| Metric | Value |
|---|---|
| or_total / prompt (uncached) / completion | 98,070,826 / 5,530,839 / 1,301,183 |
| or_cache_read / or_cache_write | 89,615,567 / 1,623,370 |
| `cache_hit_rate` | 0.92607 (per run: 0.849 to 0.950) |
| or_calls (usage available / unavailable) | 48 (48 / 0) |
| codex_calls / codex_tokens | 0 / 0 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` (repo, 34 samples) | 1,661,500 / 10,730,550 |
| review_autofix `wall_clock_p50_ms` / `wall_clock_p99_ms` | 9,470,000 / 10,921,800 |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |

**Semble, Serena and other MCP servers**

| Metric | Value |
|---|---|
| Semble queries / bytes / avg bytes per query | 6 / 86,780 / 14,463 |
| Semble sources / static_dup_bytes | 46 / 0 |
| Semble bootstraps / failed / unused / total ms | 6 / 0 / 0 / 285,568 (avg 47.6s) |
| Semble fallbacks (contract-test / runtime) | 28 (28 / 0) |
| Serena queries / response bytes / tool calls / fallbacks | 0 / 0 / 0 / 0 |
| Serena probe_ok / probe_failed / probe_skipped | 0 / 0 / 0 |
| Other MCP servers observed | none |

**GH API:** no call-count or rate-limit telemetry in the window. The only figure is G1's code-derived estimate of at least 34 `GET pulls/<n>` per final run.

**Data gaps:**
- The repository was truncated at 1,000 runs (`page_limit_reached`).
- The run-logs directory was not mounted, so there are no deep-dive excerpts and no `AI_MEMORY_TELEMETRY`, `CI_FINAL_MERGE_REF` or `GH_API_RETRY` lines.
- There is no merged-PR count.
- `or_phases` and the Claude counters are absent per run.
- The workflow file sizes (`wc -c`) were not measured.
- `analysis/recommendation-processing-report.md` was readable.

**Findings check**

| Status | Count |
|---|---|
| new | 5 (S1, R1, R2, R3, G1) |
| recurring | 0 |
| reopened | 3 (S2, C1, C2) |
| unverified | 3 (R4, AI Memory, Pipeline item 4) |
| dropped: already fixed on this ref | 1 (separating contract-test from runtime Semble fallbacks; `cost_audit.py`, report line 1835) |
| dropped: previously decided | 2 (`issue_comment` router workflow, a run-volume plan non-goal; Semble query parallelism, rejected at report line 1307) |