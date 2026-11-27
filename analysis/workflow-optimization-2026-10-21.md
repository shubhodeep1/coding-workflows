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
## Deep Audit — Workflows & Scripts (2026-10-21)

**Scope and method:** this pass read these files in depth:
- `scripts/orchestrate_poll_process.sh` (header, label/linked-PR helpers, wave loops at 25195–25830, validation terminal paths at 14749–15060, the merge helpers)
- `scripts/gh_helpers.sh`, `scripts/tg_helpers.sh`, `scripts/label_helpers.sh`
- `.github/workflows/check_failure_triage.yml`, `implement.yml` (selected steps), `test-and-mark-stable.yml` (selected steps), `review_autofix.yml` (selected steps), `update_workflows.yml` (selected steps), `cancel_on_pr_close.yml`, `orchestrate_poll.yml:1-140`
- the `internal-{review,implement,orchestrate-poll}.yml` callers

Other workflows and scripts were covered only by targeted greps. Line counts come from the repository. Byte and character figures are **estimates** (lines × observed average width) because no `wc -c` was run; each is marked as such.

**Cross-references to the existing report (not raised again):**
- `install-claude` post-step cleanup: Reliability Improvements, item 1.
- Missing fields on `SEMBLE_FALLBACK`: Reliability, item 2.
- Codex token forwarding in `codex_isolated_exec.sh`: Cost Optimizations, item 2.
- Poller phase timing (`POLL_PHASE`) and the `*/5` cron versus `cancel-in-progress: false` interaction: Speed Optimizations, item 1.

### Section 1: Bug & Correctness Sweep

**BUG-001: the ready-to-merge auto-merge path can rebase, force-push and squash-merge a PR that only mentions the issue**
- **File:** `scripts/orchestrate_poll_process.sh:25520-25611`. Also `25667-25679` (in-progress conflict path), `25814-25820` (review-blocked path) and `2092-2205` (`_sync_integration_and_rebase_subissue`).
- **Severity:** High | **Category:** `bug`
- **Description:**
  - The RTM loop resolves `RTM_PR="$(_issue_cross_ref_pr_number_last "${rtm_issue}" ...)"` (25523). That helper returns the *last* PR of any kind that cross-references the issue.
  - The file itself documents that this is unsafe:
    - `_pr_json_is_issue_implementation_pr` (18589-18610) records the #3817/#3825 incident, where a §19-correct `Refs #N` PR was adopted as the issue's PR.
    - `_resolve_issue_implementation_pr` (18764-18789) records poller run 32891613081, which pushed onto the wrong PR.
  - The reconcile loop at 25259-25278 correctly filters candidates through `_pr_json_is_issue_implementation_pr`.
  - The RTM, IP and RB loops do not. Once `_rtm_pr_json` reports `open` and `mergeable=true`, the RTM path:
    1. runs `_sync_integration_and_rebase_subissue`, which rebases `head_ref` onto the integration tip and force-pushes it (`git push --force-with-lease="${head_ref}:${head_sha}" origin "${rebased_sha}:refs/heads/${head_ref}"`, 2185-2186);
    2. calls `_orch_squash_merge_bound "${RTM_PR}" ... auto` (25601). Nothing in that path checks `.base.ref` or the head branch convention.
  - A mention-only PR whose base is `main` could therefore be rebased onto the integration branch and auto-merged.
  - [NEEDS VERIFICATION]: whether this is reachable depends on an issue in `ready-to-merge` status having a newer mention-only cross-reference. That is the same precondition as the documented #3816 incident.
- **Recommended fix:**
  - Replace the three `_issue_cross_ref_pr_number_last` lookups (25523, 25669, 25815) with `_resolve_issue_implementation_pr`. It already prefers `ai/issue-<n>` through `_linked_prs_by_branch_name` and verifies candidates.
  - Alternatively, after `_fetch_pr_json`, require `_pr_json_is_issue_implementation_pr "${rtm_issue}" "${_rtm_pr_json}"` and `.base.ref == RTM_INTEGRATION_BRANCH` before calling `_sync_integration_and_rebase_subissue` or `_orch_squash_merge_bound`.
  - Log `LINKED_PR_CROSS_REF_REJECTED ... reason=not_implementation_pr` in the same way as line 25267.

**BUG-002: a bare `set_tracking_phase_label` failure aborts the whole poll tick after the state has already been written**
- **File:** `scripts/orchestrate_poll_process.sh:21` (`set -euo pipefail`), `4060-4136`, and call sites `14801`, `14848`, `14884`, `14909`, `15011`, `15048`. There are 28 bare `set_tracking_phase_label "<label>"` statements in the file.
- **Severity:** High | **Category:** `bug`
- **Description:**
  - `set_issue_phase_label` returns 1 in three cases: the label contract is missing (4069), `ai_labels.py resolve-phase` fails (4080), or `gh issue edit` fails with an error that is not a missing label (4124-4126).
  - The terminal validation paths call it as a plain command right after `jq ... > "${STATE_FILE}.tmp" && mv` and `post_state_comment`, for example 14904-14909.
  - The per-tracking-issue loop (`for ((tidx=0; ...))`, 22941) runs at top level, not in a conditional, and the call at 23917 sits in an `if` body. So errexit is in force (inference).
  - A transient label-edit failure, after `gh_retry` has made 5 attempts, therefore exits the script. That skips the remaining tracking issues, the Telegram alert, `needs_human_park_project`, and the label fix itself.
  - The state comment already records `status=failed`, so a later tick may not re-enter `mark_validation_failed`, and the label can stay stale (inference). [NEEDS VERIFICATION] that no caller wraps this in `|| true` or a subshell. Only local `set +e` toggles were found (3531, 12257, 14025, 14058).
- **Recommended fix:**
  - At the call sites, write `set_tracking_phase_label "ai:validation-failed" || echo "::warning::PHASE_LABEL_APPLY_FAILED tracking=${TRACKING_NUM} label=ai:validation-failed"`.
  - Or, preferably, change `set_tracking_phase_label` (4133-4136) to always return 0 and log, mirroring `set_issue_phase_label_resilient` in `scripts/label_helpers.sh:203-267`, which "Always returns 0" and exposes `SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME`.
  - A new log key needs an `agents.d/` fragment (§25).

**BUG-003: the Telegram cleanup deletes comments while paginating, which skips tracking comments**
- **File:** `scripts/tg_helpers.sh:396-440` (`tg_cleanup_phase_msgs`) and `465-511` (`tg_cleanup_msgs`)
- **Severity:** Medium | **Category:** `bug`
- **Description:**
  - Both loops read `comments?per_page=100&page=${page}`, then `DELETE` matching comments (430-434, 501-505), then advance with `page=$((page + 1))` while the pre-deletion `count` is 100 (438-439, 509-510).
  - Each deletion shifts later comments one position earlier, so the first k comments of the old page 2 move onto page 1 and are never visited.
  - Those tracking comments and their Telegram message IDs survive cleanup. Their Telegram messages are never deleted.
- **Recommended fix:** collect all matching `(id, body)` pairs first, with `gh_retry gh api --paginate --slurp ".../comments?per_page=100"` (the pattern at `orchestrate_poll_process.sh:58`), then delete. A cheaper option is to re-read the same `page` after any deletion on it, and only advance when a page had no deletions.

**BUG-004: the read-modify-write on the `tg_cleanup` / `tg_phase` tracking comment races with concurrent jobs**
- **File:** `scripts/tg_helpers.sh:240-275` and `309-343`
- **Severity:** Medium | **Category:** `bug`
- **Description:**
  - `tg_store_msg_id` reads the newest tracking comment body, appends `,${msg_id}` with `sed`, and PATCHes the whole body (261-267).
  - Two jobs on the same issue (for example the poller and review_autofix) can read the same body, and the second PATCH discards the first job's ID.
  - Any read failure creates a **new** tracking comment (244-248, 312-316). This is the duplicate-comment failure mode the header notes (447 `tg_cleanup` comments, 195-197).
  - The write calls are raw `curl` with `|| true` and no retry or breaker accounting.
- **Recommended fix:**
  - Write one marker comment per message, `<!-- tg_cleanup:<id> -->`, instead of appending to a shared body. The cleanup (BUG-003) already treats every matching comment as a source of IDs.
  - This removes both the PATCH race and the read-before-write.
  - If one comment must stay, use `gh_api_retry --idempotent -X PATCH` and re-read after the write to confirm the ID is present.

**BUG-005: a failed no-op comment POST stops implement before the failure label is applied**
- **File:** `.github/workflows/implement.yml:5534-5551`
- **Severity:** Medium | **Category:** `bug`
- **Description:**
  - The fall-through path runs `gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` under `set -euo pipefail` (5325) with no `|| true`.
  - Because `_gh_cmd_is_unsafe_post` treats it as a POST create, `gh_retry` makes exactly one attempt (`scripts/gh_helpers.sh:1062-1069`).
  - A single 502 ends the step before `ensure_label_exists "ai:implementation-failed"` and `gh issue edit ... --add-label 'ai:implementation-failed'` (5540-5551).
  - The issue is left in `ai:implementing`/`ai:done`, so the orchestrator's re-issue path keyed on `ai:implementation-failed` never fires (inference).
- **Recommended fix:** apply the label first and post the comment second, with `>/dev/null 2>&1 || echo "::warning::no-op comment post failed for #${ISSUE_NUMBER}"`. This matches the ordering the success-no-op guard already uses at 5359-5372.

**BUG-006: the no-op close paths can leave stale phase labels on closed issues**
- **File:** `.github/workflows/implement.yml:5362-5368` and `5496-5502`
- **Severity:** Low | **Category:** `bug`
- **Description:**
  - One `gh issue edit` removes 7 labels and adds `ai:closed`. If it fails (the poller notes at `orchestrate_poll_process.sh:4084-4089` that removing an absent label can error), the fallback only adds `ai:closed`.
  - `ai:implementing`, `ai:done` and the other phase labels then stay on a closed issue. The stall and reconcile logic reads those labels.
- **Recommended fix:** source the staged `label_helpers.sh` (already done at 5540-5541) and call `set_issue_phase_label_resilient "${ISSUE_NUMBER}" "ai:closed" "${REPO}"`. It computes `(current - _AI_PHASE_LABELS) + [target]` and PUTs the full set (`scripts/label_helpers.sh:236-256`).

**SEC-001: free-text step outputs are interpolated straight into a shell body**
- **File:** `.github/workflows/update_workflows.yml:1786-1806`; sources at `582-600` and `898-907`/`923`
- **Severity:** Medium | **Category:** `security`
- **Description:**
  - The "Summary" step expands `AUDIT_GATE_MESSAGE="${{ steps.audit_gate.outputs.message }}"` and `FAILURE_REASON_DETAIL="${{ steps.update.outputs.failure_reason_detail }}"` inside `run:`.
  - `failure_reason_detail` embeds `'${SELECTED_PROFILE}'`, which comes from `vars.WORKFLOW_PROFILE` (875, 923), plus consumer file paths (954, 957).
  - A `"`, `` ` `` or `$(` in those values is evaluated in the consumer repo's sync job.
  - `message` is printed by Python and written with `echo "message=${message}" >> "$GITHUB_OUTPUT"` (600). A multi-line message breaks the single-line output format and can inject additional `key=value` outputs.
  - The current sources are repo-admin or upstream controlled, so practical risk is limited (inference). The construct is still the classic second-order expression-injection pattern.
- **Recommended fix:**
  - Move every `${{ steps.*.outputs.* }}` at 1788-1806 into the step's `env:` block and reference them as `"${AUDIT_GATE_MESSAGE}"`. The step already does this for `VERIFY_OUTCOME` and `COMMIT_*` (1779-1784).
  - At 600, write `message` with the `message<<EOF_<random>` multi-line delimiter syntax, or strip newlines first as `fail_with_reason` does (901-902).

**SEC-002: repo variables are written raw into `$GITHUB_ENV`**
- **File:** `.github/workflows/check_failure_triage.yml:417`; `.github/workflows/review_autofix.yml:2099-2100`
- **Severity:** Low | **Category:** `security`
- **Description:** `echo "UNATTENDED_IDENTITY_REINJECT_ENABLED=${{ vars.UNATTENDED_IDENTITY_REINJECT_ENABLED || 'false' }}" >> "$GITHUB_ENV"` substitutes the variable into the script text. A newline in the variable value would inject arbitrary environment variables, such as `BASH_ENV`, into every later step. Only repo admins can set it.
- **Recommended fix:** pass the value through `env:` (`REINJECT_RAW: ${{ vars.UNATTENDED_IDENTITY_REINJECT_ENABLED || 'false' }}`), then normalise it with `case "${REINJECT_RAW}" in true|false) ;; *) REINJECT_RAW=false ;; esac` before writing `$GITHUB_ENV`.

**BUG-007: Telegram sends treat HTTP errors as success**
- **File:** `.github/workflows/test-and-mark-stable.yml:7707-7710`; `.github/workflows/implement.yml:5429-5433`
- **Severity:** Low | **Category:** `bug`
- **Description:** `curl -sS -X POST ".../sendMessage" ... >/dev/null 2>&1 || echo "::warning::Telegram send failed"`. Without `-f`, curl exits 0 on HTTP 400/403 (bad chat id, revoked bot), so the warning never fires and a broken release notification channel goes unnoticed.
- **Recommended fix:** add `--fail-with-body`, or capture the body and check `jq -e '.ok == true'`. Keep the existing fail-open `|| echo "::warning::..."`.

**BUG-008: `get_issue_labels_json` is not paginated**
- **File:** `scripts/orchestrate_poll_process.sh:4138-4141`
- **Severity:** Low | **Category:** `bug`
- **Description:**
  - `_safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/labels" --jq '[.[].name]'` returns only the first page (30 labels by default).
  - `set_issue_phase_label` uses this list to decide which `--remove-label` flags to send (4096-4103), so labels beyond page 1 are never removed.
  - `scripts/label_helpers.sh:223-224` paginates the same endpoint.
- **Recommended fix:** use `gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${issue_num}/labels" --jq '[.[].name]' | jq -cs 'add // []'`, the same as `label_helpers.sh:223-224`.

**SHELL-001: the RTM and IP loops run in pipe subshells**
- **File:** `scripts/orchestrate_poll_process.sh:25520` and `25667`
- **Severity:** Low | **Category:** `shellcheck`
- **Description:**
  - `echo "${WAVE_STATUS}" | jq -r ... | while read -r rtm_issue; do` runs the loop body in a subshell (SC2031).
  - `refresh_integration_backpressure_gate_after_merge` (5669-5683, called at 25607) updates `INTEGRATION_BACKPRESSURE_BLOCK_MERGES` and `CWS_AHEAD_BY`, and those updates are lost when the loop ends.
  - Nothing reads them after 25642 today, so this is latent. The RB loop at 25806 already uses a `while read` without the pipe.
- **Recommended fix:** convert both loops to `while read -r rtm_issue; do ...; done < <(printf '%s' "${WAVE_STATUS}" | jq -r '...')`.

### Section 2: GitHub API Call Redundancy Audit

**API-001: each wave issue's timeline and PR are fetched again in every later loop of the same tick**
- **File:** `scripts/orchestrate_poll_process.sh:25235-25279`, `25520-25525`, `25667-25674`, `25806-25815`
- **Severity:** High | **Category:** `api-redundancy`
- **Description:**
  - The reconcile loop calls `_issue_cross_ref_pr_numbers_unique "${inum}"` for every issue in `ISSUE_NUMS` (25250). That is a timeline fetch through `gh_issue_timeline_with_cross_refs` (4428-4444). It then calls `_fetch_pr_json` for each candidate (25261) and works out `LINKED_PR_NUM`, but stores only `PR_STATES_JSON` (state and merged).
  - The RTM, IP and RB loops then call `_issue_cross_ref_pr_number_last` for the same issues, which fetches the timeline again, and `_fetch_pr_json` again.
  - **Current:** for an issue that is `done` or `in_progress`, at least 2 timeline fetches (each possibly paginated) and at least 2 `pulls/<n>` GETs per tick; `ready-to-merge` issues are similar.
  - **Projected:** 1 timeline fetch and 1 PR GET per issue.
- **Recommended fix:**
  - In the reconcile loop, also record `{pr: LINKED_PR_NUM, pr_json}` into a cycle-local map, for example `_CYCLE_IMPL_PR_JSON` (check the name for uniqueness first, per §10). Have RTM, IP and RB read from it before falling back to `_resolve_issue_implementation_pr`.
  - This also fixes BUG-001, because the cached PR has already passed `_pr_json_is_issue_implementation_pr`.
  - Longer term, extend `_fetch_linked_pr_status_graphql` (cache `STALL_MANAGED_LINKED_PR_CACHE`) with `headRefName`, `baseRefName`, `mergeable` and `headRefOid`, so the per-candidate REST `pulls/<n>` calls become one aliased GraphQL call per 25 PRs.
  - [NEEDS VERIFICATION]: the `mergeable` freshness the RTM path needs.

**API-002: terminal validation paths fetch the tracking issue's labels twice and edit them three times**
- **File:** `scripts/orchestrate_poll_process.sh:14799-14810`, `14846-14851`, `14884-14886`, `14907-14912`, `15009-15011`, `15046-15049`
- **Severity:** Medium | **Category:** `api-redundancy`
- **Description:**
  - Each path runs `_tracking_labels="$(get_issue_labels_json "${TRACKING_NUM}")"` (1 GET).
  - It then calls `set_tracking_phase_label`, which calls `get_issue_labels_json` again (4091, 1 GET) and makes 1 `gh issue edit`.
  - It then makes 1–2 more `gh issue edit --remove-label` calls for `ai:validate-failed` and `ai:harness-broken`.
  - **Current:** 4–5 calls per transition. **Projected:** 2 (1 GET, 1 edit).
- **Recommended fix:**
  - Add an optional third argument to `set_issue_phase_label`, the pre-fetched labels JSON, so it skips line 4091 when that argument is supplied. Add a fourth argument for extra labels to remove, and append them to `edit_args` only when they are present.
  - Pass `${_tracking_labels}` and `ai:validate-failed ai:harness-broken`.
  - This also stops sending removals for absent labels, which today are silently swallowed by `|| true`.

**API-003: the no-op ancestor walk makes 2 REST calls per hop**
- **File:** `scripts/orchestrate_poll_process.sh:16170-16201` (`count_noop_ancestors`); `.github/workflows/implement.yml:5466-5480`
- **Severity:** Medium | **Category:** `api-batching`
- **Description:**
  - Each hop issues `GET /issues/{n}` for the body, then `GET /issues/{parent}/comments --paginate`. The comments call can be several pages on long-lived issues.
  - The poller calls this from three re-issue paths (17355, 21554, 27779). implement.yml repeats it inline.
  - **Current:** 2×depth calls, plus extra pages. **Projected:** depth calls.
- **Recommended fix:**
  - Replace each hop with one GraphQL query: `issue(number:$n){ body } parent: issue(number:$p){ comments(last:100){ nodes { body } } }`. The parent number is only known after the body is read, so a hop can instead fetch `issue(number:$n){ body comments(last:100){nodes{body}} }` and look at the *current* issue's comments one step ahead.
  - Follow the batching-contract docblock style of `_fetch_blocked_issue_routing_graphql` (102-114).
  - Fail open by returning the partial count, as today.

**API-004: storing one Telegram message ID costs 2 reads and 1 write**
- **File:** `scripts/tg_helpers.sh:204-218` (`_tg_newest_comments_json`), `227-276`, `296-344`
- **Severity:** Medium | **Category:** `api-redundancy`
- **Description:**
  - Every `tg_send_tracked` / `tg_send_phase_tracked` call issues `GET /issues/{n}` (to get the comment count) and `GET /issues/{n}/comments?page=last`, then a PATCH or POST.
  - The helpers are referenced 65 times across 11 files, 14 of them in `orchestrate_poll_process.sh`.
  - A poller tick that alerts several times on the same tracking issue repeats both reads each time.
  - **Current:** 3 calls per message. **Projected:** 1 POST per message with the per-message marker from BUG-004, or 1 PATCH after the first lookup with a per-process cache.
- **Recommended fix:** adopt the per-message marker comment from BUG-004, which needs no read at all. If the shared comment stays, cache `issue -> (comment_id, body)` in an associative array for the life of the process (the pattern of `_ENSURED_LABELS_CACHE` at 4009).

**API-005: `curl_gh_api` retries non-transient errors**
- **File:** `scripts/gh_helpers.sh:1350-1403`
- **Severity:** Medium | **Category:** `api-redundancy`
- **Description:**
  - Every non-2xx response other than a rate limit goes to the backoff branch (1389-1395): 404, 401, 410 and 422 are each retried 5 times with 1+2+4+8 s of sleep.
  - `gh_api_retry` uses `_gh_api_classify` (831-881) to return immediately on `permanent`, and `gh_retry` uses `_is_gh_permanent_failure`.
  - The current callers are GETs on issues and comments (`tg_helpers.sh:207`, `214`, `398`, `467`). A deleted issue costs 5 calls and about 15 s per helper invocation.
  - **Current:** 5 calls on a permanent error. **Projected:** 1.
- **Recommended fix:** after computing `http_code`, run `_gh_api_classify "${http_code}" "${header_file}" /dev/null "${body_file}"` and `return 1` on `permanent`. Also add the `_gh_cmd_is_unsafe_post`-style single-attempt rule for `-X POST`, to keep the #6634 contract if a POST caller is ever added.

**API-006: an inline `gh_retry` in review_autofix.yml ignores the shared retry contract**
- **File:** `.github/workflows/review_autofix.yml:1144-1166`
- **Severity:** Low | **Category:** `api-redundancy`
- **Description:** the inline wrapper retries every failure 4 times, including permanent ones, and ignores `GH_RETRY_IDEMPOTENT` and the POST single-attempt rule. Its callers in this step are label POSTs and `gh pr merge --auto --match-head-commit` (1317, 1354, 1388, 1428), so duplicate side effects are unlikely today. It is still a second, divergent retry policy.
- **Recommended fix:** the step already sparse-checks out `scripts/gh_helpers.sh` (1101). Source it from that verified checkout and remove the inline copy, keeping a `type gh_retry` fallback.

**API-007: reclarify replay does 2 REST reads per queued issue**
- **File:** `scripts/orchestrate_poll_process.sh:53-100`
- **Severity:** Low | **Category:** `api-batching`
- **Description:**
  - Per queued issue: `GET /issues/{n}` (55) plus a paginated `GET /issues/{n}/comments` (58).
  - **Current:** 2N or more calls. **Projected:** ceil(N/25) GraphQL calls for the issue state and labels, plus N comment reads.
  - The comment history must stay complete ("Incomplete or malformed comment history never authorizes replay", 59), so only the issue read can safely be batched. [NEEDS VERIFICATION]
- **Recommended fix:** fold the state and label check into `_fetch_blocked_issue_routing_graphql` (115-142) or a sibling helper. Keep the paginated REST comment read.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001: `ensure_label_exists` is defined 9 times with different signatures**
- **File:** `scripts/label_helpers.sh:161-195`; `scripts/orchestrate_poll_process.sh:4003-4058`; `scripts/validate_process.sh:1222`; `scripts/review_rb_judge.sh:924`; `scripts/review_autofix_step_mark_ready_to_merge.sh:15-29`; `scripts/review_autofix_step_mark_review_blocked_workflow_failure.sh:11`; `.github/workflows/review_autofix.yml:1168-1171`, `1586-1590`, `4411-4422`
- **Severity:** Medium | **Category:** `duplication`
- **Description:**
  - `review_autofix.yml:1168-1171` takes four arguments, `(label, repo, color, desc)`, while `label_helpers.sh` takes `(label, [repo])`. A step that sources both gets whichever definition loaded last.
  - The fallbacks hard-code subsets of colors (for example `1d76db` "AI workflow label" for every unknown label, 4419), so a label's metadata depends on which code path creates it first.
- **Recommended fix:**
  - Make `scripts/label_helpers.sh` the only owner, with `ensure_label_exists <label> [repo]` reading `_AI_LABEL_COLORS` / `_AI_LABEL_DESCS`. Add a CI check that those maps match `.github/ai/label_contract.v1.json`.
  - Have the poller source `label_helpers.sh` and keep only its `_ENSURED_LABELS_CACHE` wrapper.
  - Reduce the inline fallbacks to `gh label create "$1" --repo "$2"` with no color or description, so they never write divergent metadata.

**CONSIST-001: implement.yml creates labels with metadata that contradicts the label contract**
- **File:** `.github/workflows/implement.yml:5359-5360`, `5406-5407`, `5493-5494`
- **Severity:** Medium | **Category:** `consistency`
- **Description:**
  - `ai:closed` is created with color `6b7280` and description "Issue closed by automation (superseded, duplicated, or already complete)". The contract has `6a737d` / "Linked PR closed without merge" (`.github/ai/label_contract.v1.json:156-158`, `scripts/label_helpers.sh:62,126`).
  - `ai:needs-human` is created with `b60205` versus the contract's `e11d48` (contract 100-102; `scripts/implement_handle_guard_block.sh:98-99` uses the contract value).
  - Whichever creator runs first in a consumer repo sets the label permanently, so repos drift from the contract.
- **Recommended fix:** replace the three inline `gh label create` calls with `ensure_label_exists '<label>' "${REPO}"` from the staged `label_helpers.sh`, as the same step already does at 5540-5542.

**DUP-002: the support-ref resolution block is copied 31 times across 10 workflows**
- **File:** for example `.github/workflows/check_failure_triage.yml:306-326`, `.github/workflows/orchestrate_poll.yml:118-138`. Occurrences per file: `implement.yml` 5, `orchestrate.yml` 4, `clarify.yml` 4, `plan.yml` 4, `orchestrate_poll.yml` 4, and 2 each in `sync_ai_labels.yml`, `cancel_on_pr_close.yml`, `workflow_failure_heal.yml`, `orchestrate_clarify_respond.yml`, `check_failure_triage.yml`.
- **Severity:** Medium | **Category:** `duplication`
- **Description:** each copy differs only in the workflow file name inside the regex `^shubhodeep1/coding-workflows/\.github/workflows/<file>\.yml@[0-9a-f]{40}$`. A security fix to the pin logic (issue tele-funtoken#4566) has to be applied 31 times.
- **Recommended fix:** add a composite action `.github/actions/resolve-support-ref` (inputs `workflow_file`, `job_json`; output `script_ref`). Consume it SHA-pinned, like `install-codex@91b30f6f…` at `check_failure_triage.yml:302`. Keep the step `name:` and `id:` in each workflow (§10).

**DUP-003: support-file staging loops are repeated per workflow**
- **File:** `for f in gh_helpers.sh ...` loops appear 13 times across 10 workflows, for example `check_failure_triage.yml:420-433` and `491-510`
- **Severity:** Low | **Category:** `duplication`
- **Description:** each workflow keeps its own hand-maintained list of required scripts and its own missing-file error text. `scripts/stage_workflow_support.sh` (`REQUIRED_BOOTSTRAP_SCRIPTS`, `agents.md:2374-2375`) already provides this for review_autofix.
- **Recommended fix:** extend `stage_workflow_support.sh` with a `--profile <workflow>` manifest, for example `stage_workflow_support.sh --profile check_failure_triage --dest scripts`, and call it from each workflow. [NEEDS VERIFICATION] the script's current interface.

**DUP-004: two copies of the no-op ancestor walk**
- **File:** `scripts/orchestrate_poll_process.sh:16170-16201`; `.github/workflows/implement.yml:5462-5480`
- **Severity:** Low | **Category:** `duplication`
- **Description:** the logic is identical: the same `noop_marker`, the same `Re-issued from #N` regex and the same `--paginate --jq` with `awk` sum. The comment at 16162-16164 says it is used by "all three orchestrator re-issue paths", but implement.yml keeps its own copy.
- **Recommended fix:** move `count_noop_ancestors <issue> [max_depth]` into a new `scripts/issue_lineage_helpers.sh`. Source it from the poller and from implement.yml through `${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}`, and add it to the implement staged-support list. Apply API-003 in the shared copy.

**DUP-005: the git basic-auth header is built 3 times in one script**
- **File:** `scripts/workflow_failure_heal_intake.sh:596`, `1832`, `2242`
- **Severity:** Low | **Category:** `duplication`
- **Description:** `"AUTHORIZATION: basic $(printf 'x-access-token:%s' "${GH_TOKEN}" | base64 | tr -d '\n')"` is copied three times.
- **Recommended fix:** add `_heal_intake_git_auth_header()` that prints the header, and call it at all three sites. This keeps one place to audit token handling.

**DUP-006: the check-name sanitizer is copied 3 times in check_failure_triage.yml**
- **File:** `.github/workflows/check_failure_triage.yml:271-275`, `649-662`, `682-686`
- **Severity:** Low | **Category:** `duplication`
- **Description:** three near-identical inline Python sanitizers, with lengths 200, 200 (plus key neutralisation) and 160.
- **Recommended fix:** add `scripts/sanitize_check_name.py --max-len N [--neutralize-keys]`, staged into `${CHECK_TRIAGE_TRUSTED_SUPPORT_DIR}/scripts` with the other trusted helpers (491-510).

### Section 4: Expression Size Limit Risk Assessment

**EXPR-001: test-and-mark-stable.yml Phase 3b run body is close to the limit**
- **File:** `.github/workflows/test-and-mark-stable.yml:1269-1529` ("Phase 3b: Wait for PR creation (implement phase)")
- **Severity:** High | **Category:** `expression-limit`
- **Description:** the body has one interpolation, `IMPL_CREATED_AFTER="${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}"` (1307), so the whole ≈260-line body counts as one expression. Many lines run 120–200 characters (1326-1334, 1435-1438). **Estimated** size is ~19,000–20,500 characters, about 0.5–2K of headroom [NEEDS VERIFICATION with a character count of lines 1270-1529].
- **Recommended fix:** move line 1307 into the step's `env:` (`IMPL_CREATED_AFTER: ${{ steps.approve.outputs.approved_at || steps.create-issue.outputs.created_after }}`). With no `${{ }}` left, the body is excluded from the limit. Optionally move the body to `scripts/` later.

**EXPR-002: implement.yml "Handle no-op implementation" run body is close to the limit**
- **File:** `.github/workflows/implement.yml:5324-5551`
- **Severity:** High | **Category:** `expression-limit`
- **Description:** interpolations at 5329, 5331, 5534, 5542, 5544, 5548 and 5550, in a ≈227-line body with several 200–300-character prose lines (5354, 5356, 5394, 5401, 5488, 5490). **Estimated** size is ~18,500–20,000 characters, about 1–2.5K of headroom [NEEDS VERIFICATION].
- **Recommended fix:**
  - Replace `${{ github.server_url }}`, `${{ github.repository }}` and `${{ github.run_id }}` with the runner's `${GITHUB_SERVER_URL}`, `${GITHUB_REPOSITORY}` and `${GITHUB_RUN_ID}`. That removes every interpolation.
  - Better, move the body to `scripts/implement_handle_noop.sh`, following the `scripts/implement_handle_guard_block.sh` pattern, since three guards and their comment bodies will keep growing.

**EXPR-003: test-and-mark-stable.yml "Send Telegram notification" run body is in the Medium band**
- **File:** `.github/workflows/test-and-mark-stable.yml:7490-7710`
- **Severity:** Medium | **Category:** `expression-limit`
- **Description:** more than 20 `${{ needs.* }}` / `${{ github.* }}` interpolations (7498-7518, 7547-7590, 7703) in a ≈220-line body that is mostly comments. **Estimated** size is ~14,500–16,000 characters, about 5–6.5K of headroom.
- **Recommended fix:** move the `needs.*` results into `env:` (as `GATE_ONLY` and `RELEASE_ENABLED` already are, 7485-7486) and use `${GITHUB_RUN_ID}` at 7703. Optionally move the body to `scripts/release_notify_telegram.sh`.

**EXPR-004: three workflows are approaching the repo's file-size guard**
- **File:** `.github/workflows/test-and-mark-stable.yml` (7,710 lines), `.github/workflows/implement.yml` (6,961 lines), `.github/workflows/review_autofix.yml` (5,728 lines)
- **Severity:** Medium | **Category:** `expression-limit`
- **Description:**
  - No workflow is near the 800 KB / 1 MB threshold in the task brief. The binding limit for this repo is lower: GitHub silently refuses files over 512,000 bytes, and CI fails at 480,000 (`agents.md:2335-2352`).
  - **Estimated** sizes from line counts are ~430–480K for `test-and-mark-stable.yml` and ~420–460K for `implement.yml`. Both could be inside the 440,000-byte `CI_BUDGET_WORKFLOW_WARN_BYTES` band [NEEDS VERIFICATION with `wc -c`].
  - The largest interpolation-free bodies are good extraction candidates:
    - `test-and-mark-stable.yml` Phase 4: 1856-2552 (≈700 lines)
    - `test-and-mark-stable.yml` Phase 4b: 2637-3292 (≈655 lines)
    - `implement.yml` "Run Codex implementation": 2770-4024 (≈1,250 lines)
    - `implement.yml` "Attempt post-Codex syntax repair": 4564-5088 (≈520 lines)
- **Recommended fix:** measure with `wc -c`. For any file at or above 440,000 bytes, move the bodies above into `scripts/` using the §24 wrapper pattern (keep `name:`, `id:`, `if:`, `env:`; register in `REQUIRED_BOOTSTRAP_SCRIPTS`), aiming for at least 50,000 bytes of headroom.

**EXPR-005: several mid-size interpolated blocks are trending upward**
- **File:** `.github/workflows/test-and-mark-stable.yml:808-940` (interpolation at 848, ~9–10K); `.github/workflows/implement.yml:552-720` (571/572/656/681, ~12K); `.github/workflows/review_autofix.yml:5179-5330` (~11K) and `5026-5166` (~10K)
- **Severity:** Low | **Category:** `expression-limit`
- **Description:** all are below the 15,000-character Medium threshold (estimates), but each carries only a few `github.*` interpolations that could be removed for free.
- **Recommended fix:** the next time each block is edited, replace `${{ github.repository }}`, `${{ github.sha }}`, `${{ github.run_id }}` and `${{ github.server_url }}` with the runner's `GITHUB_*` variables, and move `steps.*` values into `env:`. No `if:` condition near the limit was found; the longest seen is `implement.yml:5317`, at about 350 characters.

### Section 5: Cross-Cutting Concerns

**DEBT-001: the poller script is too large for full static analysis, and its own size comment is stale**
- **File:** `scripts/orchestrate_poll_process.sh:12-19`
- **Severity:** Medium | **Category:** `tech-debt`
- **Description:**
  - ShellCheck extended analysis is disabled file-wide because the file is too large (issue #6680). So SC2031/SC2030-class issues such as SHELL-001 and the errexit interaction in BUG-002 are never checked.
  - The header says "~27,700-line", but code exists at least to line 29,073.
- **Recommended fix:**
  - Split the file into sourced modules by domain, for example `orchestrate_security_pass.sh` (6407-9096), `orchestrate_validation.sh` (13577-15240) and `orchestrate_integration_conflict.sh` (9167-13186). Keep the entry point and function names unchanged (§10), so extended analysis can be turned back on per module.
  - Update the line count in the header comment.

**DEAD-001: the Bot-authored `/approved` branch probably never fires**
- **File:** `.github/workflows/internal-implement.yml:26-31`; `.github/workflows/implement.yml:105-107`
- **Severity:** Low | **Category:** `dead-code`
- **Description:**
  - The branch accepts `user.login == 'github-actions[bot]'` plus `[auto-approved-by-plan]`.
  - A `github-actions[bot]` comment can only be written with `GITHUB_TOKEN`, and GitHub does not start `issue_comment` workflows for events created with `GITHUB_TOKEN`.
  - The auto-approval at `plan.yml:2855-2856` is posted through `gh_retry gh api`, probably with the PAT, which makes the author a `User`. [NEEDS VERIFICATION] which `GH_TOKEN` that step uses.
- **Recommended fix:** confirm the token at `plan.yml:2855`. If it is the PAT, document the Bot branch as a legacy fallback or remove it in a reviewed change; per §10 it is a contract surface.

**CONSIST-002: brace placement is mixed in the poller script**
- **File:** `scripts/orchestrate_poll_process.sh` — for example `42`, `115`, `4003`, `4060` use `name() {`, while `305`, `2092`, `2220`, `2257` use `name()` with the brace on the next line
- **Severity:** Low | **Category:** `consistency`
- **Description:** §11 of the unattended instructions requires the opening brace on a new line. The file mixes both styles, and `scripts/tg_helpers.sh` and `scripts/gh_helpers.sh` follow the rule.
- **Recommended fix:** do not reformat existing code (§9). New functions should use the brace-on-next-line style, which reviewers can enforce.

**CONSIST-003: tg_helpers.sh sends writes outside the shared retry and breaker path**
- **File:** `scripts/tg_helpers.sh:244-248`, `263-267`, `270-274`, `312-316`, `332-336`, `338-342`, `430-434`, `501-505`
- **Severity:** Low | **Category:** `consistency`
- **Description:** reads go through `curl_gh_api` (rate-limit aware, and it trips the breaker), but every POST, PATCH and DELETE is raw `curl -s ... || true`. A write that hits a rate limit neither retries nor trips `_GH_RATE_LIMIT_BREAKER_FILE`. The token also appears on the curl command line (`-H "Authorization: token ${GH_TOKEN}"`), where the process list can see it, unlike `gh`'s environment-based auth.
- **Recommended fix:** route PATCH and DELETE through `gh_api_retry --idempotent -X PATCH|DELETE "repos/${GITHUB_REPOSITORY}/issues/comments/${id}"`. Route POST through `gh_api_retry` with the default single attempt. This uses `gh`'s environment auth and the shared breaker.

**SHELL-002: one SC2155 instance**
- **File:** `scripts/orchestrate_poll_process.sh:11796`
- **Severity:** Low | **Category:** `shellcheck`
- **Description:** `local now_epoch="$(date +%s)"` declares and assigns in one statement, which masks the command's exit status. It is the only `local x="$(...)"` in `scripts/*.sh`, so the risk is negligible.
- **Recommended fix:** `local now_epoch; now_epoch="$(date +%s)"`.

**TODO/FIXME/HACK/XXX markers:** none found in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml`. No debt-marker finding is raised.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | |
| High | 5 | BUG-001, BUG-002, API-001, EXPR-001, EXPR-002 |
| Medium | 14 | BUG-003, BUG-004, BUG-005, SEC-001, API-002, API-003, API-004, API-005, DUP-001, DUP-002, CONSIST-001, EXPR-003, EXPR-004, DEBT-001 |
| Low | 16 | BUG-006, BUG-007, BUG-008, SEC-002, SHELL-001, SHELL-002, API-006, API-007, DUP-003, DUP-004, DUP-005, DUP-006, EXPR-005, DEAD-001, CONSIST-002, CONSIST-003 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/orchestrate_poll_process.sh` (BUG-001, BUG-002); `agents.d/<id>-phase-label-apply-failed.md` for the new log key | Medium |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `scripts/tg_helpers.sh`, `scripts/gh_helpers.sh`, `.github/workflows/implement.yml`, `.github/workflows/review_autofix.yml` | Medium |
| Code modularization | `scripts/label_helpers.sh`, new `scripts/issue_lineage_helpers.sh`, new `.github/actions/resolve-support-ref/action.yml`, `scripts/stage_workflow_support.sh`, 10 workflows using the support-ref block, `scripts/workflow_failure_heal_intake.sh` | Large |
| Expression size reduction | `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/implement.yml` (plus optional new `scripts/implement_handle_noop.sh`, `scripts/release_notify_telegram.sh`) | Small |
| Medium/Low fixes | `scripts/tg_helpers.sh`, `.github/workflows/update_workflows.yml`, `.github/workflows/check_failure_triage.yml`, `.github/workflows/review_autofix.yml`, `.github/workflows/implement.yml`, `.github/workflows/test-and-mark-stable.yml`, `scripts/orchestrate_poll_process.sh` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-21)

### Safety Tag Legend
`SAFE_TO_MERGE` means static reading shows that every precondition holds: the same endpoint and auth scope, the same job or step, no mutation in between, unchanged error handling, no concurrency boundary crossed, and no cache contract broken. The implement stage may apply the one-line directive directly. `NEEDS_VERIFICATION` means the change looks right, but at least one precondition cannot be proven from source alone. The listed verification steps must pass before anyone implements it. `RISKY_SKIP` means a hard trigger applies: a retry or backoff loop, pagination, a rate-limit-sensitive path, `orchestrate_poll_process.sh`, stall recovery, or a path that defends against races. It is reported only for visibility and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

**MERGE-001: the standalone-validate dispatch reads labels once per fallback issue; one aliased GraphQL read could replace the N reads**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files / lines:**
  - `.github/workflows/review_autofix.yml:882` builds the fallback nodes with `labels: null`.
  - `.github/workflows/review_autofix.yml:889-904` does the per-issue read inside the dispatch loop.
  - The loop input is at `review_autofix.yml:928`.
- **Current → proposed calls:** N `gh issue view --json labels` calls (each is a GraphQL request made by `gh`) → 1 aliased GraphQL request. This applies only when `closingIssuesReferences` is empty and the issue numbers came from the PR title/body fallback (`review_autofix.yml:850-883`).
- **Endpoint(s):** GraphQL `repository.issue(number:).labels`, issued once per issue by `gh issue view`.
- **Evidence:** every fallback node is built with unknown labels, so every loop iteration makes its own read:
  ```bash
  issue_nodes_json="$(printf '%s\n' "${issue_numbers}" | jq -Rsc 'split("\n") | map(select(length > 0)) | map({number: tonumber, labels: null})')"
  ...
  if [ "${labels_known}" != "true" ]; then
    if issue_labels="$(gh_retry gh issue view "${issue_number}" --repo "${REPOSITORY}" --json labels --jq '.labels[].name' 2>/dev/null)"; then
  ```
  Inside the loop, the only writes are `gh workflow run` (916) and `gh issue edit --remove-label` on the *current* issue (925). Neither can change another issue's labels.
- **Proposed fix:** right after line 882, prefetch the labels for every `labels: null` node in one aliased query (`i0: issue(number: N) { number labels(first: 100) { nodes { name } } }`, following the alias pattern in `_fetch_candidate_issue_details_graphql` in `scripts/orchestrate_poll_process.sh` and the existing per-issue REST fallback at `issue_pr_status.yml:649-683`). Rewrite `issue_nodes_json` with `labels` filled in, so line 892 skips the read. Keep the per-issue `gh issue view` as a fallback when the batch fails or an alias comes back null.
- **Safety rationale:** the merge changes error handling. Today one failed read marks only that issue `validation_label_unknown=1` (900-902). A failed batch would mark every issue, unless the per-issue fallback is kept.
- **Downstream signal (verification):**
  1. Confirm what `issue(number:)` returns when `extract_repo_scoped_issue_refs_from_text` yields a pull-request number. `gh issue view` may resolve PR numbers that `issue(number:)` rejects with NOT_FOUND. If it does, use `issueOrPullRequest(number:)`.
  2. Confirm that `labels(first: 100)` matches the label limit `gh issue view --json labels` uses.
  3. Confirm that a duplicated issue number in `issue_numbers` cannot make the line-925 removal change the prefetched labels of a later iteration.

**MERGE-002: implement.yml reads the same issue up to 3 times. Do not merge: these are deliberate re-check gates**
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - `.github/workflows/implement.yml:1989-1990`: conditional refetch when `ISSUE_META_FILE` fails `issue_meta_matches_issue`.
  - `.github/workflows/implement.yml:2204`: `DEPENDENCY_META`.
  - `.github/workflows/implement.yml:2620`: "Recheck security dependency before edits".
- **Current → proposed calls:** 2–3 `GET /repos/{o}/{r}/issues/{n}` per run → keep 2–3.
- **Endpoint(s):** REST `GET /repos/{owner}/{repo}/issues/{number}`.
- **Evidence:** both later reads explain themselves as freshness gates:
  ```bash
  # Recheck just before advancing the phase. /approved must not
  # bypass a still-open security prerequisite or a failed API read.
  DEPENDENCY_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" 2>/dev/null || true)"
  ```
  The line-2620 read sets `SKIP_IMPLEMENT=true` on failure (2621-2622).
- **Proposed fix:** none. The calls are listed so that a later pass does not "consolidate" them.
- **Safety rationale:** RISKY_SKIP trigger: the code defends against an upstream race (a security prerequisite reopening between the phase advance and the edits) and acts as a fail-closed permission gate.
- **Downstream signal (manual review):** do not auto-implement. Any merge would let a stale snapshot authorize edits. That needs a security owner's sign-off that the prerequisite state cannot change between lines 2204 and 2620.

**MERGE-003: validate_process.sh reads the tracking issue at context assembly and again at the tracker-open guard. Do not merge**
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:** `scripts/validate_process.sh:3523` (`.body`) and `scripts/validate_process.sh:4854` (`.state`).
- **Current → proposed calls:** 2 → keep 2.
- **Endpoint(s):** REST `GET /repos/{owner}/{repo}/issues/{number}`.
- **Evidence:** the same endpoint is read twice. Asking for `{body, state}` at line 3523 would look like a free merge, but the second read is a freshness guard taken after the validation run:
  ```bash
  # Tracker-open guard: never open a fix-up against a closed tracker.
  TRACKER_STATE="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_RAW}" --jq '.state' ...
  ```
- **Proposed fix:** none.
- **Safety rationale:** RISKY_SKIP trigger: the code explicitly defends against an upstream race (the tracker closing during a long validation run). Reusing the line-3523 snapshot would defeat the guard.
- **Downstream signal (manual review):** do not auto-implement. Keep both reads.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001: the smoke-test PR backstop re-runs `ensure_label_exists` for two labels that the same job has already ensured as a fatal gate**
- **Safety tag:** `SAFE_TO_MERGE`
- **Files / lines:**
  - First (fatal) ensure: `.github/workflows/implement.yml:5700-5754`, step "Ensure E2E smoke-test labels exist on repo".
  - Redundant re-ensure: `.github/workflows/implement.yml:6092-6110`, step "Backstop E2E smoke-test labels on PR after creation".
  - Helper: `scripts/label_helpers.sh:161-195`.
- **Current → proposed calls:** 2 redundant `gh label create` POSTs per smoke-test implement run that reaches PR creation. Each returns `already_exists`, which `label_helpers.sh:188-190` treats as success. Proposed: 0. The PR label POST at 6112-6114 is unchanged.
- **Endpoint(s):** REST `POST /repos/{owner}/{repo}/labels`.
- **Evidence:**
  - The first step fails the job if either ensure fails:
    ```bash
    ensure_label_exists "force-review" "${REPOSITORY}" || {
      echo "::error::Could not ensure 'force-review' label exists; aborting smoke-test PR creation (gh pr create --label would fail)"
      exit 1
    }
    ```
  - The backstop repeats the same calls and swallows every outcome:
    ```bash
    ensure_label_exists "force-review" "${REPOSITORY}" >/dev/null 2>&1 || true
    ensure_label_exists "e2e-smoke-test" "${REPOSITORY}" >/dev/null 2>&1 || true
    ```
  - Both steps are in the single `implement:` job (lines 92-6881) and both use `GH_TOKEN: ${{ secrets.GH_PAT }}` (5713, 6078).
  - The backstop's `if:` (6076) contains every term of the ensure step's `if:` (5711), plus `pr_url != ''`.
  - Neither step sets `continue-on-error` (the nearest are 5314 and 6341). `IS_SMOKE_TEST` is only written at 2113, and `SKIP_IMPLEMENT` is only ever set to `true`, always before line 5700. So the backstop can only run after the fatal ensure succeeded.
  - No `gh label delete` exists anywhere in the repository.
  - No test in `tests/` matches the line-6109 warning text, and a grep found no test asserting `ensure_label_exists "force-review"`.
- **Proposed fix:** in the "Backstop E2E smoke-test labels on PR after creation" step, remove the `label_helpers.sh` source and the `ensure_label_exists` block (6092-6110). Keep the PR number parse (6086-6090) and the `issues/${PR_NUMBER}/labels` POST with its warning and success lines (6112-6118) exactly as they are. Update the step comment to say the labels were ensured by the earlier step.
- **Safety rationale:** same endpoint, label names, repo and token, in the same job with no label-deleting step in between. The removed calls' failures were already discarded (`>/dev/null 2>&1 || true`), so no caller's error handling or retry behaviour changes, and no contractual log key is removed.
- **Downstream signal (directive):** delete `implement.yml:6092-6110` (the label_helpers source and the two `ensure_label_exists` calls) in the backstop step, and keep 6112-6118 byte-identical.

**REUSE-002: the inline no-op ancestor walk in implement.yml re-fetches the current issue's body, which `ISSUE_META_FILE` already holds**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files / lines:**
  - `.github/workflows/implement.yml:5466-5468`: hop 1 runs with `current="${ISSUE_NUMBER}"`.
  - `.github/workflows/implement.yml:2620`: the last write of `ISSUE_META_FILE`, a full `GET /issues/{n}` payload.
  - Gate on the no-op step: `implement.yml:5317`.
- **Current → proposed calls:** up to 2×threshold (the comment at 5458 says "up to 2 * threshold calls") → 2×threshold − 1. This saves the hop-1 body GET on every no-op run that walks the chain.
- **Endpoint(s):** REST `GET /repos/{owner}/{repo}/issues/{number}`.
- **Evidence:**
  ```bash
  current="${ISSUE_NUMBER}"
  for hop in $(seq 1 "${threshold}"); do
    parent_body="$(gh_retry gh api "repos/${REPO}/issues/${current}" --jq '.body // ""' 2>/dev/null || echo "")"
  ```
  - All writes to `ISSUE_META_FILE` are at 202, 929, 948, 957, 1990, 2210 and 2620. None comes after 2620; the later references (5787, 5856, 5964-5965, 6287) only read it.
  - The no-op step requires `env.SKIP_IMPLEMENT != 'true'` (5317), and line 2620 sets `SKIP_IMPLEMENT=true` on failure (2621-2622). So whenever the step runs, the file holds a successful issue read.
- **Proposed fix:** before the loop, use `jq -r '.body // ""' "${ISSUE_META_FILE}"` as the hop-1 body. Fall back to the existing GET when the file fails the `issue_meta_matches_issue` shape check (the inline definition at `implement.yml:1978-1987`) or is empty. Hops 2+ stay unchanged. If DUP-004 lands first, add an optional "hop-1 body" argument to the shared `count_noop_ancestors`. The poller's callers would not pass it.
- **Safety rationale:** the snapshot is about 2,800 lines and possibly many minutes older than the live read. Its integrity across the Codex execution steps cannot be proven statically.
- **Downstream signal (verification):**
  1. Confirm that `${RUNTIME_DIR}/issue_meta.json` is not mounted read-write into the isolated Codex container (`scripts/codex_isolated_exec.sh`). Codex-controlled content must not be able to forge or remove a `Re-issued from #N` line and so change the #1292 no-op cap.
  2. Confirm that no workflow step edits the issue body between line 2620 and line 5324.

**REUSE-003: on poller terminal paths, `tg_cleanup_msgs` re-paginates tracking-issue comments that the tick already holds in `COMMENTS`**
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - Tick-level fetch: `scripts/orchestrate_poll_process.sh:22958-22978`, `gh_retry_to_file ... gh api --paginate ".../issues/${TRACKING_NUM}/comments?per_page=100"`, which sets `COMMENTS_FETCH_OK`.
  - Re-scan: `scripts/tg_helpers.sh:449-512`, a manual `page=` loop at 465-511.
  - Poller call sites with `TRACKING_NUM`: `orchestrate_poll_process.sh:5889`, `14818`, `14864`, `14925`, `15023`, `15061`, `23752`, `23852`, `28336`, `28831`, `28906`, `28960`.
- **Current → proposed calls:** ceil(C/100) comment-page GETs + k DELETEs → 0 GETs + k DELETEs, where C is the comment count. This is only possible if the cache is proven complete.
- **Endpoint(s):** REST `GET /repos/{o}/{r}/issues/{n}/comments` (paginated) and `DELETE /repos/{o}/{r}/issues/comments/{id}`.
- **Evidence:**
  - `COMMENTS` is complete only as of the start of the tick. The poller's `tg_notify` (727-754) calls `tg_send_tracked "${TRACKING_NUM}"` (749), which writes or patches `tg_cleanup` marker comments through `tg_store_msg_id` (`tg_helpers.sh:227-276`) and never updates `COMMENTS`.
  - Only the completion-status writer patches `COMMENTS` in place (`orchestrate_poll_process.sh:3757-3763`).
  - A cleanup driven by `COMMENTS` would therefore miss markers posted earlier in the same tick, and their Telegram messages would never be deleted.
- **Proposed fix (for manual review only):** make `tg_store_msg_id` report the comment id it wrote back to the caller, so the poller can append it to `COMMENTS`. Then add a `tg_cleanup_msgs_from_json` variant that takes the comment array and is used only when `COMMENTS_FETCH_OK=true` and `COMMENTS_FOR_TRACKING_NUM == TRACKING_NUM`. Otherwise fall back to today's live scan. This also sidesteps BUG-003's skip-while-deleting pagination defect for the cached path.
- **Safety rationale:** RISKY_SKIP triggers: the call is reached from `orchestrate_poll_process.sh`, and it replaces a paginated read (a manual `page` loop compared with `--paginate`), which risks changing page-boundary semantics.
- **Downstream signal (manual review):** do not auto-implement. It needs a design that keeps `COMMENTS` coherent with `tg_store_msg_id` writes, and a test showing that same-tick markers are cleaned.

**REUSE-004: the 2 lookup reads of a store right after `tg_cleanup_msgs` look for a marker the cleanup has just deleted**
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - Cleanup-then-notify pairs: `scripts/orchestrate_poll_process.sh:5889-5890`, `14818-14819`, `14864-14865`, `14925-14926`.
  - The lookup: `scripts/tg_helpers.sh:204-218` (`_tg_newest_comments_json`), called from `tg_store_msg_id` at `tg_helpers.sh:241`.
- **Current → proposed calls:** 3 per pair (`GET /issues/{n}`, `GET /issues/{n}/comments?page=last`, then POST or PATCH) → 1 POST.
- **Endpoint(s):** REST `GET /repos/{o}/{r}/issues/{n}`, `GET /repos/{o}/{r}/issues/{n}/comments`, `POST /repos/{o}/{r}/issues/{n}/comments`.
- **Evidence:**
  ```bash
  tg_cleanup_msgs "${TRACKING_NUM}"
  tg_notify "Project #${TRACKING_NUM} validation harness is broken (raw_status=harness_error). ..." "CRITICAL"
  ```
  `tg_cleanup_msgs` deletes every `<!-- tg_(cleanup|phase): -->` comment it sees (`tg_helpers.sh:481-505`). The next `tg_store_msg_id` then spends 2 reads to find the newest `tg_cleanup` marker (`tg_helpers.sh:252-256`). That marker is now absent unless BUG-003's page skip left one behind, or a concurrent job wrote one.
- **Proposed fix (for manual review only):** have `tg_cleanup_msgs` set a process-local `_TG_CLEANED_ISSUE="${issue_num}"`, after checking that the name is unique per §10. `tg_store_msg_id` would then POST a new marker directly, without the 2 reads, when that variable equals the target issue. This complements API-004 and BUG-004.
- **Safety rationale:** RISKY_SKIP trigger: the code is reached from `orchestrate_poll_process.sh`. Skipping the lookup also depends on the BUG-003 pagination behaviour, and could leave a surviving marker unpatched, which adds one more marker comment.
- **Downstream signal (manual review):** do not auto-implement. Fix BUG-003 first. Then confirm that no concurrent job (review_autofix, issue_pr_status) writes `tg_cleanup` markers on tracking issues between the cleanup and the notify.

### Dead Calls (DEAD-API-###)
No findings. I checked these candidates:
- the GH_PAT login lookup in `issue_pr_status.yml:230-243` (memoized, and its result is consumed);
- the output-discarded existence probes at `scripts/check_failure_triage.sh:567-571`, `scripts/orchestrate_poll_process.sh:5899`, `scripts/workflow_failure_heal_intake.sh:2215` and `.github/workflows/orchestrate.yml:1272` (each result is consumed through the exit code);
- the post-write label verification reads at `scripts/validate_process.sh:4501-4503` and `scripts/implement_handle_guard_block.sh:110-117` and `402-409` (they are consumed, and they deliberately confirm a write).

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP — it is in `orchestrate_poll_process.sh`, and the timeline read is paginated. Net-new details:
  - The reconcile `for` loop (25235) runs in the main shell, so a cycle-local cache written there survives. Writes made inside the piped RTM and IP loops (SHELL-001) would not.
  - The reconcile loop `break`s on the first merged candidate (25275-25277), so its `LINKED_PR_NUM` is "first merged or last verified unmerged", not the open PR the RTM loop needs.
  - Cache the verified PR *number* only, and keep a fresh `_fetch_pr_json` in RTM, because `mergeable` is computed lazily. That still removes the second timeline fetch.
- API-002: RISKY_SKIP — it is in `orchestrate_poll_process.sh`. Net-new details:
  - The harness path also issues a separate `--add-label "ai:harness-broken"` edit at 14810, after the remove at 14803. Both could join the single `edit_args` edit in `set_issue_phase_label` (4111-4116), since `ensure_label_exists` (14804) already runs earlier.
  - Before passing `_tracking_labels` (14799) into `set_issue_phase_label`, check whether `handle_comprehensive_release_callback_if_needed` (14800) can mutate labels. If it can, the prefetched list is stale before line 4091.
- API-003: RISKY_SKIP — this is stall-recovery and re-issue logic, and the parent-comment read is `--paginate` (16189). Net-new details:
  - The proposed `comments(last:100)` is not equivalent to that read: an ancestor with more than 100 comments whose no-op marker is older would be undercounted, which weakens the #1292 cap. Any GraphQL form must page with `hasPreviousPage`.
  - The real saving is fetching the parent's body and comments together, because the next hop's body read is the parent's body: 2×depth → depth+1 calls.
  - For the implement.yml copy, see REUSE-002.
- API-004: NEEDS_VERIFICATION — `tg_helpers.sh` is shared by 11 files. A per-process `(comment_id, body)` cache is only valid while no other job writes markers on the same issue. Verify against the concurrent writers named in BUG-004. See also REUSE-004.
- API-005: RISKY_SKIP — this changes the retry and backoff loop and the rate-limit branch of `curl_gh_api` (`gh_helpers.sh:1350-1403`, per the Deep Audit). It needs manual review of breaker accounting.
- API-006: RISKY_SKIP — this replaces a retry wrapper (`review_autofix.yml:1144-1166`, per the Deep Audit; not re-read in this pass). Changing retry policy on label POSTs and `gh pr merge --auto` needs manual review of the single-attempt POST contract.
- API-007: RISKY_SKIP — it is in `orchestrate_poll_process.sh`, the comment read is paginated, and it is an authorization-bearing path ("Incomplete or malformed comment history never authorizes replay").

### Summary Counts
The counts cover the new findings in this pass plus the Deep Audit IDs cross-referenced above.

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 1 | REUSE-001 |
| NEEDS_VERIFICATION | 3 | MERGE-001, REUSE-002, API-004 |
| RISKY_SKIP | 10 | MERGE-002, MERGE-003, REUSE-003, REUSE-004, API-001, API-002, API-003, API-005, API-006, API-007 |

### Implement-Stage Handoff
- REUSE-001: in the "Backstop E2E smoke-test labels on PR after creation" step, delete `.github/workflows/implement.yml:6092-6110`, the redundant `label_helpers.sh` source and the two `ensure_label_exists` calls. Keep the PR label POST and its log lines at 6112-6118 unchanged.