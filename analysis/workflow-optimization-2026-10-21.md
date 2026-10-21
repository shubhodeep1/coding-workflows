# Workflow Optimization Report: shubhodeep1/coding-workflows

**Window:** 1,000 runs, about 2026-10-20 09:14Z to 2026-10-21 00:36Z (≈15.4 h). `insufficient_data=false`.

**Evidence limits:** the run-logs folder `/home/runner/work/_temp/workflow-log-output` was not in this sandbox, so `summary.json`, `errors/`, `slow/` and `recent/` could not be read. The findings below come from `analysis_context`, the gpt-6-luna `log_summary` fields (runs 39522091781, 39520815066, 39519587633, 39518718958, 39518535952), and the repository source code. Success sample rate is 0.07.

## Executive Summary

- **Review & Autofix is the slowest step before merge, not CI.** Review and CI start at the same second, and review finishes 57–1,229 s later in every paired case:
  - 39518391306: 2,104 s vs CI 39518391131: 875 s
  - 39508943038: 1,661 s vs 39508942972: 919 s
  - 39404674221: 1,527 s vs 39404673876: 894 s
  - 39414958067: 974 s vs 39414957754: 917 s

  **Impact:** high (sets PR turnaround). **Confidence:** high on timing, medium on the cause (20–24 model calls per run).
- **The orchestrate poller uses 44% of all runner time:** 91 runs × 578 s avg = 52,596 s of 118,436 s. The cron fires every 5 minutes (`internal-orchestrate-poll.yml:5`) but a run takes 642 s at p50, so the queue is always full. 17 runs (18.7%) were cancelled before their first step, for example 39521545770 after 453 s. The 11-minute `poll` step has no timing markers: no `*_TIMING`, `elapsed_ms=` or `duration_ms=` in `scripts/orchestrate_poll_process.sh`. **Impact:** high. **Confidence:** high.
- **Semble is not working in implement, and the fail-open behaviour hides it.**
  - Implement: 34 runtime `SEMBLE_FALLBACK`, 0 `SEMBLE_BOOTSTRAP`, 0 `SEMBLE_QUERY`, across 39518887691 (18), 39518865727 (10), 39512526380 (4) and 39402044801 (2).
  - review_autofix is healthy: 8 bootstraps, 8 queries, 0 fallbacks.

  **Impact:** medium (implement repair prompts lose retrieval context). **Confidence:** medium.
- **Implement and plan token cost cannot be seen.** `codex_calls=0` and `or_calls=0` across 22 implement and 19 plan runs whose logs were parsed. Yet every implement success took 1,118–2,081 s and plan successes up to 970 s. `scripts/codex_isolated_exec.sh` contains no token-usage forwarding. **Impact:** high (the largest gap in cost data). **Confidence:** high.
- **Reliability is good.** 3 failures in 1,000 runs (0.3%), 0 retries.
  - Two are 0-second runs named after their file path (39520745014, 39520744520) whose log archives return 404. They look like the zero-job "workflow file issue" pattern (inference).
  - One deterministic bug is confirmed: the `check_failure_triage.yml:537-539` cleanup deletes the `install-claude` action that its own post step still needs (run 39522091781). **Impact:** low–medium. **Confidence:** medium.
- **Telemetry dilution.** 692 of 1,000 runs (69.2%) were skipped. Every `issue_comment: created` starts 4 caller workflows (`internal-{clarify,plan,implement,orchestrate-clarify-respond}.yml`), each filtered by a job-level `if:`. These used up the 1,000-run cap, so the window covers only ≈15 h. **Impact:** medium (analysis coverage). **Confidence:** high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

**1. Find out what takes the poller 11 minutes (critical path for every orchestrated issue).**
- **Evidence:**
  - 39519587633: about 3 min waiting for a runner (00:09:33 → 00:12:29), then the `poll` step from 00:12:33 to about 00:23:37.
  - p50 642 s, p95 895 s. Slow examples: 39407041717 (1,122 s), 39414953955 (1,083 s), 39411183469 (1,060 s).
  - `concurrency: group: ai-orchestrate-poll-…, cancel-in-progress: false` (`orchestrate_poll.yml:68-73`) with a `*/5` cron means each new pending run replaces the older pending one. That is the 17 cancelled-before-first-step runs.
- **Root cause:** unknown. The step body is opaque and the summary notes 837,841 log characters were truncated.
- **Exact change (diagnostic):**
  - Wrap the major sections of `scripts/orchestrate_poll_process.sh` (candidate prefetch, the stall/linked-PR GraphQL batches, the judge calls, and the per-tracking-issue loop) with `POLL_PHASE name=<section> ms=<elapsed> items=<n> run=${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}`.
  - This is a new log prefix, so register it through an `agents.d/<id>-poll-phase-timing.md` fragment under `Stable log prefixes (contractual)` (§25).
- **Estimated savings:** none directly. If the result lets the step drop from ~11 to ~5 min, that saves ≈91 × 6 min ≈ 9 runner-hours per 15 h window, and the cron cadence is actually honoured (estimate).
- **Risk:** very low (stderr-only log lines).

**2. Shorten the Review & Autofix critical path.**
- **Evidence:** the pairs above. Token-bearing review runs average 704k tokens per call (52.1M / 74 calls). 39515418638 made 24 calls, 22.8M tokens, and raised 2 `CONTEXT_BUDGET_WARN`.
- **Root cause (inference):** many sequential model calls with very large prompts. 39518391306 averaged up to 105 s per call (2,104 s / 20 calls).
- **Exact change:** first emit the existing per-phase `or_phases` breakdown, which `scripts/cost_audit.py` already builds, into the analysis context so it is clear which phase (reviewer, aggregator or editor) dominates. Then trim the dominant phase's prompt; see Cost item 1.
- **Estimated savings:** 2–5 min per review run if the top phase's prompt shrinks by about 30% (estimate).
- **Risk:** low for the diagnostic part. Prompt trimming carries quality risk.

**3. Reuse the Semble index within a review run.**
- **Evidence:** 39518391306 bootstrapped twice (61,558 ms) and 39515418638 twice (92,096 ms). Across 8 bootstraps the total was 332,594 ms (41.6 s average), about 3.4% of the 9,861 s wall time of the six token-bearing review runs.
- **Exact change:** when a second job or iteration in the same run needs the index, reuse it from the run-scoped `SEMBLE_BOOTSTRAP_STATE_FILE`/index path instead of rebuilding.
- **Estimated savings:** about 45 s for each extra bootstrap.
- **Risk:** low (fail-open stays as it is).
- **Classification:** micro-optimization.

**4. Split the slowest CI job (not on the merge-critical path).**
- **Evidence:** in 39520815066, `tests-release-and-log-analysis` ran about 8.7 min of an 891 s run. CI p50 is 891 s, p95 948 s, and CI used 23,400 runner-seconds in the window.
- **Exact change:** split that job's test list into two parallel jobs.
- **Estimated savings:** 3–4 min of CI wall time per run. Because review is the critical path, this saves runner minutes rather than merge latency.
- **Risk:** low.

## Cost Optimizations

**1. Raise the review prompt-cache hit rate (largest token pool).**
- **Evidence:** review_autofix used 52,096,452 total tokens: 12,396,184 uncached prompt, 35,588,208 cache read, 3,578,784 cache write, 533,276 completion. The computed hit rate is 69.0%. Per run:

  | Run | Cache hit rate |
  |---|---|
  | 39518391306 | 53.9% (4.58M uncached) |
  | 39403287089 | 58.2% |
  | 39410343617 | 60.8% |
  | 39404674221 | 69.3% |
  | 39508943038 | 75.6% |
  | 39515418638 | ≈76.1% (computed) |

- **Root cause (inference):** the prompt prefix changes between calls. Per-call variance in uncached prompt size is large.
- **Exact change:** move per-iteration dynamic blocks (findings, diffs, Semble output) after the static rubric and repo context. Have the collector report per-phase cache read and write so the run with the worst prefix is visible.
- **Estimated savings:** moving from 69% to 80% would shift about 5.7M tokens per window from uncached to cache-read (estimate).
- **Quality risk:** none if only block order changes.

**2. Make implement and plan token spend visible.**
- **Evidence:** 0 Codex or OpenRouter usage lines across 22 implement and 19 plan runs with parsed logs. Implement runs Codex inside the isolated container (`implement.yml:3021-3022` comment), and `scripts/codex_isolated_exec.sh` has no usage forwarding (its only "usage" match is the CLI usage string at line 101).
- **Exact change:** in `codex_isolated_exec.sh run`, after the container exits, echo the Codex `tokens used` trailer from the captured output to the job log so `CODEX_TOKENS_RE` in `cost_audit.py` can parse it.
- **Estimated savings:** enables cost control for about 9,869 runner-seconds of implement work per window.
- **Risk:** low. Print counts only, never transcript content.

**3. Semble's contribution is small and clean in review; keep it.**
- 8 queries logged 124,272 bytes (15.5 KB per query, roughly 31k tokens in total), with 51 sources and `static_dup_bytes_total=0`. That is about 0.25% of review's uncached prompt tokens, and none of it duplicates static context.
- It is not adding noisy context. Whether it reduces prompt expansion cannot be measured without a with/without comparison.
- No change recommended.

**4. Context-budget pressure is limited to one run.**
- The only 2 `CONTEXT_BUDGET_WARN` lines are both in 39515418638, which also had the highest tokens per call (951k) and 2 `or_usage_unavailable_calls`. This is prompt-size risk, not rubric pressure (`break_glass_count=0`).
- **Change:** have the collector report the `phase=` and `ratio=` fields of these lines.

## Reliability Improvements

**1. Fix the `install-claude` post-step error in check-failure triage.**
- **Evidence:** in 39522091781: `##[error]Can't find 'action.yml'... under .codex-workflow-src/.github/actions/install-claude`. The run still succeeded.
- **Root cause (code-verified):** when the engine is Claude, `check_failure_triage.yml:537-539` keeps only the `claude-pool-token` action directory. `install-claude` (used at line 396) also runs a post step at job end.
- **Exact fix:**
  ```bash
  find .codex-workflow-src/.github/actions -mindepth 1 -maxdepth 1 ! -name claude-pool-token ! -name install-claude -exec rm -rf {} +
  ```
- **Expected impact:** removes one error annotation per Claude-engine triage run and stops it from contaminating triage and heal signals.
- **Rollback:** a single-line revert. It only keeps one more directory.

**2. Implement Semble fallbacks: masked broken rollout.**
- **Evidence:** 34 runtime fallbacks, with 0 bootstraps and 0 queries in implement. In contrast:
  - Review: 8 of 8 bootstraps succeeded, 0 fallbacks.
  - CI: 16 fallbacks, all correctly classified as contract-test fallbacks (8 per run in 39520745173 and 39520815066), which is healthy.
- **Code path:**
  - `implement.yml:3013` only enables Semble for targeted context when `SEMBLE_INDEX_AVAILABLE=true`, which is never true before a lazy bootstrap.
  - So the fallbacks most likely come from the post-Codex repair prompt (`implement.yml:4952-4953`, `semble_query_block`).
  - `semble_ensure_ready` returns without a `SEMBLE_BOOTSTRAP` line in three cases: the mode/enable gate (`semble_helpers.sh:201`), a stored `state=failed` (line 204), or the `flock` paths (lines 211-222). Then line 304 logs `reason=binary-unavailable`.
- **Exact fix (diagnostic first):** add the resolved mode, `SEMBLE_ENABLED`, and the bootstrap state as extra fields on the line-304 and line-308 fallbacks. These are additive key=value fields; check them against `_validated_mcp_telemetry_event` in `cost_audit.py`.
  ```bash
  _semble_log_event "SEMBLE_FALLBACK" "target=${target}" "reason=binary-unavailable" \
    "mode=${_SEMBLE_RESOLVED_BOOTSTRAP_MODE:-unset}" "enabled=${SEMBLE_ENABLED:-unset}" \
    "state=$(grep -m1 '^state=' "${SEMBLE_BOOTSTRAP_STATE_FILE}" 2>/dev/null | cut -d= -f2 || echo none)"
  ```
- **Possible double counting (low confidence):** every fallback count is even (2, 4, 8, 10, 18, 34). Check whether each event appears twice in the log, once from the stderr `printf` in `_semble_log_event` and once from the `emit_event` echo.
- **Fail-open:** behaviour stays unchanged.

**3. Zero-job failures 39520745014 (`internal-review.yml`) and 39520744520 (`internal-workflow-failure-heal.yml`).**
- **Evidence:** 0 s duration, the workflow name equals the file path, and the log archive returns 404. They were created at 00:20:43Z alongside CI 39520745173.
- **Root cause (inference):** a workflow-file validation failure of the kind §24 describes. Both callers use `@main` reusable workflows. The next runs were skipped or succeeded, so the failure was transient.
- **Exact change:** make the collector classify zero-duration, zero-job, path-named failures as `workflow_file_issue`, and fetch the check-suite annotation rather than the log archive. This also removes the two 404 log calls.
- **Expected impact:** these runs stop counting as unexplained failures, and a real file-size or YAML regression can be told apart from noise.

**4. CI 39520745173: `static-checks` / "Validation self-test unit tests" (`ci.yml:466-472`).**
- **Evidence:** failed after 798 s. CI 39520815066 on `refs/pull/7749/merge` succeeded 42 s later.
- **Root cause:** unknown, because there is no log excerpt (`errors/` is missing).
- **Exact change:** the step runs four or more test files back to back under `set -e`. Add `echo "::group::<file>"` around each call so the failing file shows in the summary.
- **Expected impact:** diagnosis time only.

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` lines appear in any available evidence. The `log_summary` fields for 39519587633, 39518718958 and 39518535952 explicitly report none.
- So retrieve hit rate, `estimated_tokens` against budget, `keyword_method` distribution, `fail_open` and `enabled: false` counts, and push retries cannot be assessed.
- The likely emitter, the poller, lost 837,841 log characters to truncation in 39519587633.
- **Recommendation:** have the collector grep untruncated logs for `AI_MEMORY_TELEMETRY:` and summarise counts by `op`.
- **Verification:** confirm emission by checking that at least one `op=retrieve` line exists per poller run that has work.

## GH API Call Audit

- **Observed budget:** only one line. In sweep 39518718958: `GH_PAT_BUDGET phase=end workflow=review_autofix_sweep job=sweep remaining=4929 … used_in_job=2`.
- **Rate limiting:** no HTTP 429 or secondary rate limit appears in any of the 5 log summaries.
- **Likely hotspot (inference):** the poller, at 91 runs of about 11 minutes each. `orchestrate_poll.yml` already emits `GH_PAT_BUDGET`, but those lines are lost to truncation.
- **Change:** have the collector pull `GH_PAT_BUDGET` start and end lines into structured per-run fields (as it does for cost telemetry), so `used_in_job` can be ranked per workflow. Expected result: the first per-workflow call-count table, with no new API calls.
- **Collector efficiency:** skip the log fetch for runs with `duration_seconds=0` and no jobs. This saves 2 failing calls in this window (the 404s) and keeps the error list clean.
- **Fan-out:** the four `issue_comment`-triggered callers skip at job level (for example `internal-orchestrate-clarify-respond.yml:15-29`), so their scripts make no API calls. Their cost is run records and analysis dilution, not rate limits.
- **Batching:** no per-item, unbatched call loops can be seen without untruncated poller logs. Recheck `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql` cache use once `POLL_PHASE` timing is in place.

## Prompt Cache & Memory System

- **`cache_hit_rate`:** null at summary and family level, even though the inputs exist. Computed review-wide: 35.59M / (35.59M + 12.40M + 3.58M) = **69.0%**. Per-run range: 53.9%–76.1%. This is an aggregation gap in the collector; compute it from summed tokens.
- **Cache writes:** 3.58M, about 6.9% of input. 39403287089 wrote 386,500 tokens against only 897,966 read. That suggests the prefix is rebuilt per call (inference).
- **Fragmentation:** the lowest hit-rate run (39518391306, 53.9%) had 20 calls. Recommendation: fixed static rubric first, then repo context, then dynamic findings, diff and Semble block.
- **Budget pressure:** 2 `CONTEXT_BUDGET_WARN` lines, both in 39515418638, which still reached about 76% hit rate. Pressure there is prompt size, not cache erosion.
- **Unknowns:** fail-open behaviour and memory retrieval effectiveness, because no memory telemetry was found (see AI Memory Health).

## Orchestrator Health

**Families in this window:**

| Family | Runs | Outcome | Notes |
|---|---|---|---|
| orchestrate_poll | 91 | 74 success, 17 cancelled, 0 failed | Cancellations are superseded pending runs (`cancel-in-progress: false`, `*/5` cron vs 642 s p50). Queue wait up to about 3 min (39519587633). |
| orchestrate | 3 | 3 success | 676–726 s (39399279469, 39510455737, 39516572630) |
| validate | 1 | success | 830 s (39418310851, tracking:7731) |
| integration_pr_readiness | 8 | 8 success | |
| issue_pr_status | 7 | 7 success | |

- **Clarification loop:** 146 of 146 `orchestrate_clarify_respond` runs were skipped by the `Clarification required` body filter, so no clarification activity happened this window.
- **Conflict healing:** no conflict-heal or stuck-state signals in the evidence.
- **Triage:** 39522091781 on PR 7748 (base `orchestrator/project-7745`) ended with `skip reason=pr_specific_failure`, which is correct routing.
- **Indicators to track:**
  - Poller cancelled/total ratio (now 18.7%)
  - Poller queue wait (job evaluation to runner online)
  - `POLL_PHASE` ms per section (proposed)
  - Effective cycle interval (now about 10 min against the 5-min intent)
- **Smallest safe mitigation:** add timing first. Do not change the cron or concurrency until the slow phase is known.

## Pipeline Flow Bottlenecks

Approximate per-issue flow, using successful-run durations:

| Stage | Duration | Evidence |
|---|---|---|
| clarify | ≤ 11 s per gate run | substantive clarify runs not sampled |
| plan | ≈ 680–970 s | 39517400397: 970 s, 39400505813: 880 s |
| implement | ≈ 1,118–2,081 s | 39512526380: 2,081 s |
| review ∥ CI | review 974–2,104 s; CI 827–954 s | starts at the same time |
| orchestrate/validate | ≈ 700–830 s | |
| poller | ≈ 10–15 min | one cycle can sit between each hand-off |

How the overhead splits:
- **Compute:** review and implement dominate per-issue latency. The poller dominates runner time (44%).
- **Queueing:** about 3 min of runner wait per poller run; 17 runs superseded.
- **Retry:** 0 run-level retries. Implement-internal repair attempts are likely (inference from the repair-path Semble fallbacks) but not counted.
- **Merge/conflict:** no evidence. `MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0` (39518535952).

Fixes in order of end-to-end impact:
1. Poller phase timing, then fixing the slow phase.
2. Review phase token breakdown, then prompt reordering and trimming.
3. Implement repair-attempt counters: emit attempt count and outcome per run.
4. Reuse the Semble index within a run.
5. Split the slowest CI job.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - The poller: 52,596 s, 44% of runner time, with an opaque 11-minute step.
  - Review & Autofix on the merge path: p50 1,527 s in telemetry runs.
  - Implement: up to 2,081 s.
- **Top failure modes:**
  - Zero-job path-named failures (39520745014, 39520744520).
  - The CI self-test step failure (39520745173).
  - The `install-claude` post-step error (39522091781).
  - Semble not working in implement (34 runtime fallbacks).
- **Highest-cost drivers:** review OpenRouter usage (52.1M tokens, 74 calls, 69% cache hit). Implement and plan spend is unmeasured.
- **Top 3 actions:**
  1. Add `POLL_PHASE` timing to `scripts/orchestrate_poll_process.sh`, with an `agents.d` prefix fragment.
  2. Forward Codex `tokens used` from `scripts/codex_isolated_exec.sh`, and add mode/enabled/state fields to `SEMBLE_FALLBACK` in `scripts/semble_helpers.sh`.
  3. Keep `install-claude` in the `check_failure_triage.yml:539` cleanup.

## Metrics Appendix

**Run outcomes (window: 1,000 runs)**

| Metric | Value |
|---|---|
| success / failure / cancelled / skipped | 288 / 3 / 17 / 692 |
| failure rate (all) / (conclusive runs only) | 0.3% / 0.97% (3/308) |
| p50 / p95 duration | 2 s / 831 s |
| runner-seconds total | 118,436 |
| runs with log telemetry | 111 |
| wall_clock_p50_ms / p99_ms (repo) | 1,000 / 2,067,200 (107 samples) |

**Per family**

| Family | Runs | Success | Fail | Cancel | Skipped | p50 s | p95 s | Runner-s |
|---|---|---|---|---|---|---|---|---|
| orchestrate_poll | 91 | 74 | 0 | 17 | 0 | 642 | 895 | 52,596 |
| ci | 27 | 26 | 1 | 0 | 0 | 891 | 948 | 23,400 |
| review_autofix | 53 | 52 | 1 | 0 | 0 | 19 | 1,581 | 18,689 |
| implement | 146 | 6 | 0 | 0 | 140 | 1 | 10 | 9,869 |
| plan | 148 | 7 | 0 | 0 | 141 | 1 | 11 | 5,444 |
| clarify | 155 | 6 | 0 | 0 | 149 | 1 | 11 | 2,110 |
| orchestrate | 3 | 3 | 0 | 0 | 0 | 695 | 723 | 2,097 |
| orchestrate_clarify_respond | 146 | 0 | 0 | 0 | 146 | 1 | 10 | 367 |
| workflow_failure_heal | 60 | 0 | 1 | 0 | 59 | 1 | 9 | 142 |
| validate | 1 | 1 | 0 | 0 | 0 | 830 | 830 | 830 |

**Tokens and cache (review_autofix only; all other families report 0)**

| or_calls | prompt (uncached) | cache_read | cache_write | completion | total | cache_hit_rate (computed) | usage unavailable |
|---|---|---|---|---|---|---|---|
| 74 | 12,396,184 | 35,588,208 | 3,578,784 | 533,276 | 52,096,452 | 69.0% (summary field: null) | 2 |

`codex_calls` = 0 and `codex_tokens_used` = 0 repo-wide. `claude_*` usage is not reported.

**Policy and budget signals**

| Signal | Value |
|---|---|
| break_glass_count | 0 |
| context_budget_warn_count | 2 (both in run 39515418638) |

**Semble**

| Workflow | Queries | Logged bytes | Bytes/query | Sources | static_dup | Bootstraps (failed/unused) | Bootstrap ms | Fallbacks (contract/runtime) |
|---|---|---|---|---|---|---|---|---|
| review_autofix | 8 | 124,272 | 15,534 | 51 | 0 | 8 (0/0) | 332,594 | 0 (0/0) |
| implement | 0 | 0 | — | 0 | 0 | 0 | 0 | 34 (0/34) |
| ci | 0 | 0 | — | 0 | 0 | 0 | 0 | 16 (16/0) |
| **Total** | 8 | 124,272 | | 51 | 0 | 8 | 332,594 | 50 (16/34) |

Per-target Semble rows are not present in the analysis context.

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | Queries | response_bytes | tool_calls | Fallbacks |
|---|---|---|---|---|---|---|---|
| serena (all workflows) | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

No Serena activity was observed. Other MCP servers observed: none.

**GH API**

| Source | Value |
|---|---|
| `GH_PAT_BUDGET` (39518718958 sweep) | used_in_job=2, remaining=4929 |
| 429 / secondary rate-limit events | 0 visible |
| Collector log-fetch 404s | 2 (39520745014, 39520744520) |

**Data gaps**
- The deep-dive folder was absent from this sandbox.
- No `AI_MEMORY_TELEMETRY` was found.
- No implement or plan token telemetry.
- Summary-level `cache_hit_rate` is null.
- No per-target Semble breakdown or `or_phases` in the context.
- The poller step's logs were truncated.
- 69% of the run cap went to skipped runs. Excluding `skipped` from the 1,000-run cap, or reporting skipped runs separately, would roughly triple the window of runs that did work (inference).