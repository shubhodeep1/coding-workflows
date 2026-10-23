Checkpoint:
- No files edited. Read: `ci.yml` (no workflow-level `concurrency:`, job timeouts of 15, 20 and 5 min), `integration-pr-readiness.yml`, `ci_cancelled_rerun.py` (where `listing_truncated` comes from), `semble_helpers.sh` (the lazy-bootstrap `SEMBLE_AVAILABLE=false` path), `collect_workflow_logs.py` (`compute_run_metrics` already receives `jobs`), and 13 earlier analysis reports.
- The run-logs directory `/home/runner/work/_temp/workflow-log-output` does not exist, so there are no deep-dive excerpts.

## Executive Summary

- **The run-logs folder was not mounted, so there is no deep-dive evidence.** `/home/runner/work/_temp/workflow-log-output` does not exist. `errors/`, `slow/`, `recent/` and `summary.json` could not be read. Everything below rests on `analysis_context`, the 17 gpt-6-luna `log_summary` rows and the repo source. The two CI failures have a known failing step but no excerpt. **Impact:** limits root-cause depth. **Confidence:** high that the gap exists.
- **Runner queueing is about 60% of wall-clock and is still not measured; this is the top systemic pattern.** Across the 11 summarized runs that report a wait, about 2,146 of 3,557 s (60%) was spent queued behind "All GitHub-hosted runners … are busy". Examples:
  - 39970066736: 318 of 329 s
  - 39970066786: 248 of 256 s
  - 39970234125: 188 of 200 s
  - 39970791440: 408 of 793 s

  This is the ≥10th report to flag runner queueing, and the collector still has no queue field. `compute_run_metrics` (`collect_workflow_logs.py:1112`) already receives `jobs`, so splitting queue time out costs 0 extra API calls. **Impact:** prerequisite for every speed fix. **Confidence:** high.
- **CI is the likely main source of runner demand (inference).** CI runs of 3,852–4,392 s happened in the 10:47–10:48 cohort (39941611487, 39941658714, 39941653575, 39941619305). Yet every `ci.yml` job is capped at 15–20 min, plus 5 + 5 min for the `needs:` jobs. So at least 35–40 min of each of those runs was queueing (inference). `ci.yml` still has no workflow-level `concurrency:`, which `analysis/workflow-optimization-2026-10-23-3.md` already proposed. **Impact:** p95 CI 3,950 s, possibly down 30–50% at peak (inference). **Confidence:** medium.
- **Cancelled-CI auto-rerun did nothing for any PR in the sampled sweep, and the log line can't say why.** Sweep 39971247714 logged `CI_CANCELLED_RERUN … action=skip reason=listing_truncated` for all 5 PRs. `list_runs()` (`ci_cancelled_rerun.py:84-86`) raises one undifferentiated reason for 5 different conditions across 4 status listings. **Impact:** cancelled CI is never recovered while this persists. **Confidence:** high for this tick, unknown frequency.
- **Review/autofix cost is driven by uncached prompt tokens, not cache misses.** The 3 runs with OpenRouter (OR) telemetry used 47.24M tokens in total over 30 calls, with `cache_hit_rate` 0.864. Uncached prompt was still 4.74M (about 158K per call), against 45,712 Semble bytes (about 0.24%, estimated). The `or_phases` breakdown exists in `cost_audit.py` but is not exported, so the expensive calls can't be identified. **Impact:** about 10% of tokens, but likely the largest cost share. **Confidence:** medium.
- **Reliability is otherwise high.**
  - 2 failures in 1,000 runs (0.2%). Both were deterministic stub-based CI tests (39954156960, 39953571733) that ran alongside successful review/autofix runs on the same push. These are likely PR regressions that CI caught, not flakes (inference).
  - `BREAK_GLASS`=0 and `CONTEXT_BUDGET_WARN`=0 across all 115 runs with log telemetry.
  - All 40 Semble fallbacks are `contract-test`, with 0 runtime fallbacks.

  **Confidence:** high.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Split queue time from execution time in the collector.** Diagnostic, and the prerequisite for everything else on the critical path.
   - **Evidence:** `duration_seconds` is `updated_at − run_started_at` (`collect_workflow_logs.py:1123-1127`), so it hides job-level queueing. Queue shares from the summaries:

     | Run | Queued / total |
     |---|---|
     | 39969359551 | 261 s / 274 s |
     | 39971967922 | 202 s / 212 s |
     | 39972276900 | 83 s / 98 s |
     | 39969381689 | about 258 s / 970 s (`created_at` 14:24:39 → start 14:28:57) |

   - **Root cause:** there is no queue field. Every earlier report had to reconstruct waits from LLM summaries.
   - **Change:** additive fields in `compute_run_metrics`, using the `jobs` it already receives. This adds 0 API calls (§14).

     ```python
     # Additive (no new API calls): per-job runner queue from the jobs payload already passed in.
     _queue_waits = []
     for _job in jobs or []:
         _c = _parse_iso8601(_job.get("created_at"))
         _s = _parse_iso8601(_job.get("started_at"))
         if _c and _s and _s >= _c:
             _queue_waits.append(int((_s - _c).total_seconds()))
     row["runner_queue_seconds_max"] = max(_queue_waits) if _queue_waits else None
     row["runner_queue_seconds_total"] = sum(_queue_waits) if _queue_waits else None
     ```

     Aggregate p50 and p95 per family in `analyze_workflow_logs.py`. Fall back to `None` when `jobs_fetch_status` shows the jobs weren't fetched.
   - **Time savings:** 0 s directly. It turns about 40 min of hidden CI p95 time into something measurable.
   - **Risk:** low. Fields are additive and fail open to `None`.

2. **Cancel superseded PR CI runs. Critical path, already proposed in 2026-10-23-3 and still not landed.**
   - **Evidence:** `ci.yml:13-16` triggers on `pull_request` (all branches) plus `push` and has no top-level `concurrency:`. Each run fans out to 7 jobs plus the `orchestrate-poll` matrix (`ci.yml:2082`). The 10:47–10:54 window started at least 6 CI runs and 3 review runs together. Sweep 39971247714 waited on `ubuntu-24.04` too, which points to an account-wide concurrency cap rather than a label-specific one (inference).
   - **Change:** a workflow-level `concurrency` that cancels only on `pull_request`, so push CI on `main`, `stable` and `orchestrator/project-*` keeps running for check-failure triage. See the snippet in the 2026-10-23-3 report.
   - **Time savings:** depends on the supersession rate, which is unmeasured. Collect it first by counting CI runs per PR per hour from `head_sha` and PR fields already in the run payload.
   - **Risk:** medium. A cancellation interacts with `ci_cancelled_rerun.py`, which reruns `cancelled` conclusions. The `superseded_head` guard (`:328-330`) should prevent reruns of old heads; verify with `DRY_RUN=true`.

3. **Short PR hooks: no change recommended.**
   - Lint 39970066786 executed for about 3 s; readiness 39970066736 for about 3 s.
   - Readiness deliberately runs on every PR so the required status exists (`integration-pr-readiness.yml:45-49`). A job-level `if:` would leave the required check pending.
   - The smaller win is in item 2, which frees runners for these hooks.

4. **Micro-optimization: clarify-respond gates allocate a runner only to skip.**
   - 39972276900 spent 98 s, 83 s of it queued, to log `gate=orchestrator_metadata reason=not_orchestrator_managed` and `gate=standalone reason=issue_closed` on issue #7978.
   - 137 of 139 `orchestrate_clarify_respond` runs were skipped at job level, so this path is rare.
   - **Change:** none until the trigger payload is confirmed to carry the labels and state. Then consider a job-level `if:` for `issue.state == 'closed'` only.
   - **Savings:** about 90 s of runner time per occurrence. **Risk:** low to medium.

## Cost Optimizations

1. **Expose per-call and per-phase usage to find the 158K-token uncached calls.**
   - **Evidence:** the 3 telemetry runs, 10 OR calls each:

     | Run | Uncached prompt | Cache read | Cache write | Completion | Total | Hit rate |
     |---|---|---|---|---|---|---|
     | 39942331669 | 1,643,994 | 16,709,093 | 562,569 | 104,168 | 19,019,824 | 0.883 |
     | 39942176603 | 1,605,160 | 16,484,211 | 546,090 | 122,297 | 18,757,758 | 0.885 |
     | 39943323370 | 1,490,667 | 7,375,163 | 536,480 | 57,520 | 9,459,830 | 0.784 |

     The rate equals read / (read + uncached + write); for example 16,709,093 / 18,915,656 = 0.8833.
   - **Root cause (inference):** the uncached prompt (about 149–164K per call) and cache writes (about 54–56K per call) are nearly constant across runs. Only the reads vary. So the lower rate in 39943323370 reflects a shorter agent session, not cache fragmentation.
   - **Change:** include `or_phases` from `cost_audit.py` (already aggregated at about `:705`) in the analysis context, so the analyst can see which review call (reviewer, aggregator or editor) carries the uncached volume.
   - **Savings:** unknown until measured. If one phase dominates, moving its dynamic suffix (diff, findings) after a stable prefix is the usual lever.
   - **Quality risk:** none for the diagnostic.

2. **Semble is cheap, but its benefit can't be proven from this data.**
   - 3 queries, 45,712 bytes, 22 sources, about 15.2 KB per query, `static_dup_bytes`=0 in review runs 39942331669, 39943323370 and 39942176603.
   - Bootstrap took 46.2 s, 45.5 s and 30.2 s (121,890 ms in total), about 1% of 3,762–4,565 s runs.
   - There is no duplication noise, but there is no control arm, so "reduces prompt expansion" is unverified.
   - **Change:** none. Keep it. Serena logged 0 queries, 0 probes and 0 fallbacks, so it is neither replacing work nor adding noise this window.

3. **No avoidable reruns or model-reasoning waste observed.**
   - Every run had `retries`=0. Clarify, plan and implement were mostly skipped.
   - `MODEL_REASONING_EFFORT: high` with `openai/gpt-6-sol` (39970791440, 39972276900) can't be evaluated, because those runs show 0 OR calls (see the telemetry gap under Reliability).

## Reliability Improvements

1. **`CI_CANCELLED_RERUN reason=listing_truncated` gives no breakdown. Diagnostic, highest priority.**
   - **Evidence:** 39971247714 skipped all 5 PRs (#7977, #7976, #7969, #7959, #7958) with `run=none action=skip reason=listing_truncated`.
   - **Root cause:** `list_runs()` raises the same reason for the `completed`, `queued`, `in_progress` and `pending` listings. The checks include `count != len(runs)` for any active status and `len(runs) < 100 and count != len(runs)` for completed. With 51 CI runs of 30–70 min in the window, the cause could be a `total_count` mismatch on an active listing. That is an inference, and the current log can't confirm it.
   - **Fix:** add an additive line right before the raise. Do not change the existing `reason=` values (§10). Register the new prefix in an `agents.d/` fragment (§25).

     ```python
     if len(runs) > 100 or count < len(runs) or (status != "completed" and (count != len(runs) or len(runs) == 100)) \
             or (status == "completed" and len(runs) < 100 and count != len(runs)):
         # status comes from a fixed tuple; count/len are ints: safe to log.
         print(f"CI_CANCELLED_RERUN_LISTING status={status} total_count={count} returned={len(runs)} outcome=truncated")
         raise ValueError("listing_truncated")
     ```

   - **Impact:** turns a silent 100% skip into a diagnosable cause. It stays fail-closed, and rollback is a single-line revert.

2. **CI test failures leave no test-level detail in collected telemetry.**
   - **Evidence:**
     - 39954156960 failed at `tests-release-and-log-analysis` / `Test-and-mark-stable plan polling guard regression tests` (1,321 s), which runs `tests/test_test_and_mark_stable_plan_polling_guard.py` (`ci.yml:1637-1640`).
     - 39953571733 failed at `tests-heal-plan-and-validation` / `Implement leftover issue-branch replacement tests` (863 s), which runs `tests/test_implement_stale_branch_check.py` (`ci.yml:1245-1248`).
   - **Category (inference):** PR regressions. Both tests use stubbed `gh` with no wall-clock dependency. Each failed CI run was created within 12–25 s of a successful review/autofix run (39954157095 and 39953598224), so they are probably on the same push.
   - **Fix:** a diagnostic-only `::error title=ci-test-failure::step=<step> test=<file>` annotation on non-zero exit in these script-style steps. Annotations already feed `ci_cancelled_rerun.py`'s classification, so this keeps `genuine` detection precise.
   - **Rollback:** remove the annotation. It does not change pass/fail.

3. **Model runs show zero token telemetry outside review_autofix.**
   - Clarify 39970791440 ran a model (793 s, `MODEL_EDITOR: openai/gpt-6-sol`) but has `or_calls`=0 and `codex_calls`=0.
   - Its summary reports about 303,089 chars of step logs truncated. Poll 39969381689 lost about 865K chars, and promote 39969701980 about 598K.
   - **Fix:** emit one end-of-job token roll-up line from the existing usage counters in the last step, so it survives truncation. Also make sure the collector greps untruncated step files.
   - **Impact:** extends cost coverage beyond 3 of the 115 runs with log telemetry.

4. **Semble and Serena health:**
   - 40 Semble fallbacks, all `context=contract-test` (`cost_audit.py:381-382`): 32 in CI (4 in each of 8 runs) and 8 in TMS 39955031510. Runtime fallbacks were 0. This is healthy fixture behavior, not a masked broken rollout.
   - Poll 39969381689 had `SEMBLE_AVAILABLE=false` and `SEMBLE_INDEX_AVAILABLE=false` with `SEMBLE_BOOTSTRAP_MODE=lazy`. That is the expected pre-bootstrap state for a job that never queried (see `semble_helpers.sh`), and `semble_bootstraps_unused`=0.
   - Serena probe ok/failed/skipped are all 0 in every family. Probe availability can't be told apart from "not wired". Smallest mitigation: verify that a `SERENA_PROBE result=skipped reason=disabled` line is emitted once per review job when Serena is off.

5. **`BREAK_GLASS` and `CONTEXT_BUDGET_WARN`:** 0 and 0 across all families. There is no policy or prompt-size pressure.

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` lines appear in any `log_summary`, and no deep-dive excerpts were available. Retrieve hit rate, `estimated_tokens` against budget, `keyword_method` mix, `fail_open` and push retries can't be assessed.
- `MEMORY_INJECTION_SCAN_ENABLED: true` was observed in clarify 39970791440, so memory paths are configured. Their emission was probably lost to the 303K-char truncation (inference).
- **Recommendation:** once logs are mounted, verify emission with a grep for `AI_MEMORY_TELEMETRY` over `recent/*/step-*.log`. Extend the end-of-job roll-up from Reliability item 3 with `mem_retrieves=`, `mem_hits=` and `mem_fail_open=` counts. This repeats the 2026-10-19 proposal, which is still unobserved.

## GH API Call Audit

| Source | Evidence | Assessment / action |
|---|---|---|
| `review_autofix_sweep` / `sweep` | `GH_PAT_BUDGET phase=end … remaining=3947 used_in_job=6` (39971247714) | Efficient. No action. |
| `ci_cancelled_rerun.py` in the sweep | Up to 4 run-list GETs plus 1 paginated PR listing per tick (docstring `:6-15`). This tick aborted at `listing_truncated`, so its listing calls produced no decisions. | Low volume, zero yield. Fix the diagnostic (Reliability item 1) before tuning. No batching change: the calls are already repository-wide, not per PR. |
| `orchestrate_poll` / `poll` | `GH_PAT_BUDGET` occurs 6× in `orchestrate_poll.yml`, but none is visible because about 865K chars were truncated in 39969381689. | **Gap.** Likely the largest consumer (inference). Collector should grep untruncated step logs for `GH_PAT_BUDGET`. |
| `cancel_on_pr_close` scheduled | 39969359551 and 39971967922: "no cancellable … runs", no cancel POSTs, no 429 | Low volume. |
| Rate limiting | No 429, secondary-limit or backoff lines in any summary | No pressure observed. |

There are no unbatched per-item loops in the visible evidence. Estimated reduction is 0 calls until poll telemetry is visible.

## Prompt Cache & Memory System

- **`cache_hit_rate`:** 0.864 overall (review_autofix only); per run 0.883, 0.885 and 0.784.
- **Fragmentation:** none detected. Cache writes are stable at about 54–56K per call across runs, which suggests a stable prefix. The 0.784 run had fewer cache reads (7.38M against about 16.5M), not more writes. `OPENROUTER_PROMPT_CACHE_DISABLED: false` was confirmed in 39970791440.
- **Erosion:** no `CONTEXT_BUDGET_WARN` observed.
- **Lever:** the remaining uncached volume of about 158K per call. Locate it per phase (Cost item 1) before restructuring prompts.
- **Memory retrieval:** not observable (see AI Memory Health).

## Orchestrator Health

- **Poll cadence is limited by poll duration.**
  - 17 of 34 `orchestrate_poll` runs were cancelled. The poll is on `cron: '*/5 * * * *'` (`internal-orchestrate-poll.yml:5`), but runs take 970–2,260 s (39969381689, 39944043860, 39960458965 at 1,711 s, 39956327837 at 1,650 s).
  - Pending ticks are replaced, so the effective cadence is about 16–38 min, not 5 min (inference).
  - **Indicator:** tick start lag, from the cron slot to `run_started_at`.
  - **Mitigation:** a per-phase `ORCH_POLL_PHASE_SUMMARY` duration line (proposed 2026-10-19 and still absent from the workflows) to find the long phase.
- **Stale queued runs are preserved indefinitely.** The same runs appeared in 39969359551 and 39971967922:
  - 34748435167 and 34748435180 on `claude/rb-judge-pending-comment-dedupe-3965`, created 2026-09-13 (about 40 days queued)
  - 32215723558 on `ai/issue-3768`, created 2026-08-19 (about 65 days queued)

  This warning has repeated since at least 2026-10-16. **Mitigation:** add `oldest_age_days=<n>` to the existing warning line (diagnostic). An age-bounded cancel is a separate, flag-gated decision.
- **Release flow is healthy.**
  - Promote 39969701980 fast-forwarded `stable` and dispatched TMS 39969947939 (v1.39.0).
  - Follower 39970234125 reported `outcome=frozen`.
  - Forward-merge 39969948749 was a no-op (`AHEAD=0`).
  - Validate `[tracking:7925]` 39947929664 succeeded in 1,142 s.
- **Clarification loops:** there are no clarify-respond loops. 137 of 139 runs were skipped, and the only executed one (39972276900) was a closed E2E negative-test issue.

## Pipeline Flow Bottlenecks

| Stage | Evidence | Dominant overhead | Fix (ordered by end-to-end impact) |
|---|---|---|---|
| Short PR hooks (lint, readiness, follow, cancel) | 188–318 s queue against 3 s of work (39970066786, 39970066736, 39970234125) | **Queue** | Free runners through CI concurrency (Speed item 2) |
| Clarify | 39970791440: 408 s of 793 s queued | Queue, then compute | Same as above. Token visibility (Reliability item 3) |
| Implement / plan | 136 of 139 and 135 of 138 skipped; executed outliers 1,714 s (39957674870) and 1,288 s (39950223380) | Compute (unobserved) | Queue split (Speed item 1) |
| Review / autofix | p50 1,341 s, p95 3,418 s; 47.2M tokens over 3 runs | Compute | `or_phases` exposure (Cost item 1) |
| CI | p50 1,881 s, p95 3,950 s; 64–73 min runs with jobs capped at ≤20 + 5 + 5 min | **Queue (inferred)** | Speed items 1 and 2 |
| Cancelled-CI recovery | 5 of 5 `listing_truncated` | Silent no-op | Reliability item 1 |
| Orchestrate poll | 50% of ticks cancelled; 970–2,260 s runs | Compute and serialization | Per-phase timing line |
| Release (TMS) | 39955031510: 4,844 s | Unobserved | Queue split |

Retry overhead is 0 (every run has `retries`=0). Merge-train overhead is 0 (`MERGE_TRAIN_RELEASE_SUMMARY examined=0 released=0`).

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** hosted-runner queue (about 60% of summarized wall-clock). CI p95 of 3,950 s. Review/autofix p50 of 1,341 s. Poll cycles of 16–38 min.
- **Top failure modes:**
  - 2 CI test failures (39954156960, 39953571733), likely PR regressions (inference)
  - cancelled-CI recovery blind (`listing_truncated` for all PRs in 39971247714)
  - 3 stale queued runs aged 40–65 days
- **Highest-cost drivers:** review/autofix, at about 15.7M tokens per run, mainly uncached prompt (about 1.5–1.6M per run).
- **Top 3 actions:**
  1. Queue-split fields in `compute_run_metrics` (0 API calls).
  2. `CI_CANCELLED_RERUN_LISTING` diagnostic line in `ci_cancelled_rerun.py`.
  3. PR-scoped `concurrency` in `ci.yml` after a `DRY_RUN` check of the rerun interaction.

## Metrics Appendix

**Run outcomes (1,000 runs, success sample rate 0.07, `insufficient_data`=false)**

| Metric | Value |
|---|---|
| success / failure / cancelled / other | 240 / 2 / 19 / 739 |
| failure rate | 0.2% |
| p50 / p95 duration | 1 s / 1,796 s |
| runs with log telemetry | 115 |

**Key families**

| Family | Runs | Success | Fail | Cancelled | p50 s | p95 s |
|---|---|---|---|---|---|---|
| ci | 51 | 49 | 2 (3.9%) | 0 | 1,881 | 3,950 |
| review_autofix | 53 | 52 | 0 | 1 | 1,341 | 3,418 |
| orchestrate_poll | 34 | 17 | 0 | 17 | 613.5 | 1,720.8 |
| test_and_mark_stable | 1 | 1 | 0 | 0 | 4,844 | 4,844 |
| clarify / plan / implement | 144 / 138 / 139 | 4 / 3 / 3 | 0 | 0 | 1 | 10–11 |
| cancel_on_pr_close | 23 | 22 | 0 | 1 | 270 | 723.6 |

**Review-telemetry metrics**

| Metric | Value |
|---|---|
| `cache_hit_rate` | 0.864015 (review_autofix only; null elsewhere) |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 2,000 / 4,540,780 overall; review_autofix 3,762,000 / 4,544,560; ci 3,670,000 / 4,380,310 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

**Tokens (OpenRouter; Codex 0)**

| prompt (uncached) | cache read | cache write | completion | total | calls |
|---|---|---|---|---|---|
| 4,739,821 | 40,568,467 | 1,645,139 | 283,985 | 47,237,412 | 30 (30 with usage, 0 without) |

**Runner queue (summarized runs)**

| Run | Family | Queued s | Total s |
|---|---|---|---|
| 39970791440 | clarify | 408 | 793 |
| 39970066736 | integration_pr_readiness | 318 | 329 |
| 39969359551 | cancel_on_pr_close | 261 | 274 |
| 39969381689 | orchestrate_poll | about 258 | 970 |
| 39970066786 | lint_pr_body_auto_close | about 248 | 256 |
| 39971967922 | cancel_on_pr_close | 202 | 212 |
| 39970234125 | promote_candidate_follow | 188 | 200 |
| 39971247714 | review_autofix (sweep) | 132 | 156 |
| 39972276900 | orchestrate_clarify_respond | 83 | 98 |
| 39969701980 | promote_main_to_stable | 25 | 109 |
| 39969948749 | forward_merge_stable_to_main | 23 | 160 |
| **Total** | | **about 2,146 (60%)** | **3,557** |

**GH API**

| Signal | Value |
|---|---|
| `GH_PAT_BUDGET` observed | sweep 39971247714: `used_in_job=6`, `remaining=3947` |
| 429 / secondary limits | 0 observed |
| Poll budget lines | not visible (truncated) |

**Semble**

| Metric | Value |
|---|---|
| queries / logged bytes | 3 / 45,712 (about 15.2 KB per query) |
| sources / static_dup_bytes | 22 / 0 |
| bootstraps / failed / unused | 3 / 0 / 0 (121,890 ms in total) |
| fallbacks: total / contract-test / runtime | 40 / 40 / 0 (ci 32, test_and_mark_stable 8) |

**Serena and MCP availability**

| Target | query_calls | response_bytes | tool_calls | fallbacks | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|---|---|---|---|
| all families | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

Other MCP servers observed: none.

**Data gaps**
- The run-logs directory is absent, so there are no `errors/`, `slow/` or `recent/` excerpts and no `summary.json`.
- No `AI_MEMORY_TELEMETRY` lines were found.
- Token telemetry covers only 3 review runs.
- `or_phases` is not exported to the analysis context.
- Run 39969381689 appears twice in `recent_runs` and `slow_runs`, with differing queue estimates of about 2 min and about 4 min. Collector dedup is recommended. The about 258 s used above comes from `created_at`.
## Deep Audit — Workflows & Scripts (2026-10-23)

> Scope: deep, evidence-first pass over the files behind today's report signals: `ci_cancelled_rerun.py`, `review_autofix_sweep.yml`, `cancel_on_pr_close.yml`, `orchestrate_poll.yml` (pre-poll steps), `workflow-log-analysis.yml`, `comprehensive-test-and-release.yml`, the `test-and-mark-stable.yml` Phase 1/2 waiters, and the helpers they call (`review_merge_train.sh`, `label_helpers.sh`, `retarget_merged_base.sh`, `promote_candidate_follow.sh`, `gh_helpers.sh`, `stage_workflow_support.sh`, `dispatch_and_watch_workflow_run.sh`). `review_autofix.yml`, `implement.yml` and `orchestrate_poll_process.sh` were only spot-checked.
>
> These earlier findings are still open and are **not re-filed** here:
> - The dead `RATE_LIMIT_BACKOFF` guards in `test-and-mark-stable.yml` (`analysis/workflow-optimization-2026-10-22.md` DEAD-001).
> - `gh_api_safe` echoing `::error::` to stdout (`comprehensive_test_and_release_gh_api.sh:153`; 2026-10-22).
> - `ci.yml` has no workflow-level `concurrency:` (2026-10-23-3).
>
> BUG-001 below extends this report's Reliability item 1: it gives the probable root cause behind the `listing_truncated` diagnostic.

### Section 1: Bug & Correctness Sweep

**BUG-001: Cancelled-CI recovery aborts the whole tick on a total-count mismatch the merge train already handles**
- **File:** `scripts/ci_cancelled_rerun.py:84-86` (strict check), `:313-322` (one failed listing skips every PR)
- **Severity:** High · **Category:** `bug`
- **Description:**
  - For active statuses, `list_runs()` raises `listing_truncated` whenever `count != len(runs)`. It never re-reads.
  - `process()` reads all four listings inside one `try`. One mismatch therefore marks every eligible PR `skip reason=listing_truncated`. That is the 5/5 outcome in sweep 39971247714.
  - The repo already measured this GitHub behaviour. `scripts/review_merge_train.sh:1216-1218`: "GitHub's count and its listing disagree for several seconds while runs churn, so three reads 2 s apart all came back short (run 38043169762, Q59)". Also `:1225-1226`: "One run starting mid-read used to abort the whole release tick (run 38003551535)".
  - The train fixed this with a bounded re-read plus union acceptance (`review_merge_train.sh:1205-1236`, `MERGE_TRAIN_RUNS_LISTING_SHIFT_RETRIES` default 2, `:1163`). `ci_cancelled_rerun.py` has neither.
  - That this is the cause of 39971247714 specifically is an inference. The current log line cannot say which status mismatched. [NEEDS VERIFICATION]
- **Recommended fix:** port the train's re-read to the three active listings only.
  - Keep `completed` unchanged.
  - Keep failing closed when the union still does not cover `total_count`, or when `count > 100` (pagination is still out of scope).
  - Add the diagnostic line this report proposed (Reliability item 1). Register it through an `agents.d/` fragment (CLAUDE.md §30).
  - Sketch:
    ```python
    def list_active_runs(repo: str, status: str) -> list[dict]:
    	# Mirrors review_merge_train.sh:1205-1236 (2 re-reads, 2 s apart).
    	union: dict[int, dict] = {}
    	for attempt in range(3):
    		runs, count = fetch_listing(repo, status)  # today's shape checks, unchanged
    		union.update({run["id"]: run for run in runs})
    		if count <= 100 and (len(runs) >= count or len(union) >= count):
    			return list(union.values())
    		time.sleep(2)
    	print(f"CI_CANCELLED_RERUN_LISTING status={status} total_count={count} returned={len(runs)} outcome=truncated")
    	raise ValueError("listing_truncated")
    ```
  - A stale run kept in the union only adds `active_run` skips, which is the fail-safe direction.
  - The cost is at most +2 GETs per active status per tick, and only when a mismatch occurs.

**BUG-002: The TMS Phase 2 "other active Plan runs" branch skips the inactivity timeout**
- **File:** `.github/workflows/test-and-mark-stable.yml:1244-1247` (the `continue`), `:1253-1258` (timeout check it skips), `:1062-1075` (`fail_plan_confirm_retry_if_idle`)
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - When the newest Plan run is `completed` but another Plan run with the same title is `queued` or `in_progress`, the loop `continue`s before the `[ $IDLE -ge $INACTIVITY_LIMIT ]` check.
  - Unlike the "unable to confirm" paths at `:1198` and `:1239`, it does not call `fail_plan_confirm_retry_if_idle`. The comment at `:1062-1067` says those retries must apply the limit themselves.
  - A run stuck in `queued` (the sweep documents un-cancellable wedged queued runs, `review_autofix_sweep.yml:43-54`; today's report lists queued runs aged 40–65 days) keeps this step polling until the job's `timeout-minutes`, not `PLAN_PHASE_TIMEOUT`.
  - Not filed in earlier reports. [NEEDS VERIFICATION] for how often our own Plan run wedges.
- **Recommended fix:** call `fail_plan_confirm_retry_if_idle` before the `continue` at `:1246`. The `sleep "$POLL_INTERVAL"` at `:1176` already paces the loop.

**BUG-003: The comprehensive release Phase 3 tracking-issue baseline fails open to `[]`, so an existing issue can be taken for the new one**
- **File:** `.github/workflows/comprehensive-test-and-release.yml:301-315`, `:337-341`, `:436-450`
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - `list_tracking_issue_numbers` returns 0 with `TRACKING_ISSUE_NUMBERS_JSON='[]'` when `gh_api_safe` fails (`:310-312`).
  - If the baseline call at `:337` fails, `BEFORE_TRACKING_JSON` is `[]`. On the first successful poll, every open `ai:orchestrator-tracking` issue counts as "new":
    - one pre-existing issue is then output as `tracking_issue` (`:441-446`, `:476`);
    - two or more fail the job as "multiple new".
  - The run-ID baseline in the same step already retries 3× and fails closed (`:343-360`).
- **Recommended fix:** make the helper `return 1` on failure.
  - Retry the baseline 3× like `:343-360` and `exit 1` if it is still unreadable.
  - In the poll loop, skip the new-issue comparison on a failed read rather than comparing against `[]`.

**BUG-004: Close cleanup never cancels `pending` runs or PR-named `workflow_dispatch` review runs**
- **File:** `.github/workflows/cancel_on_pr_close.yml:165-186` (only `status=queued` and `status=in_progress`, both `event=pull_request`), `:299-300`, `:335`
- **Severity:** Medium · **Category:** `bug`
- **Description:**
  - `internal-review.yml:49-51` uses a workflow-level group with `cancel-in-progress: false`, so a superseded run waits as `pending`. The sweep documents this at `review_autofix_sweep.yml:322-330`.
  - Sweep, orchestrator and train dispatches are `workflow_dispatch` runs named `Internal: AI Review & Autofix [pr:<N>]` (`internal-review.yml:8`, `review_autofix_sweep.yml:540-543`). The `event=pull_request` filter excludes them in both modes.
  - The behaviour matches the documented contract (`agents.md:4916`), so this is a coverage gap rather than drift.
  - Impact is runner time on review runs for closed PRs (inference: `review_autofix.yml` gates on `PR_CLOSED`). [NEEDS VERIFICATION]
- **Recommended fix:**
  - Add `pending` and `waiting` listings. The train already reads requested/pending/queued/waiting/in_progress (`review_merge_train.sh:1057-1065`).
  - In event mode, also select `workflow_dispatch` runs whose trusted `pr:<N>` key equals the closed PR. Reuse the sweep's `dispatch_pr` jq definition (`review_autofix_sweep.yml:376-385`), including its default-branch/path provenance checks.
  - Cost: +2 paginated GETs per invocation.

**BUG-005: The promote-candidate follower can fast-forward the branch after a cycle has frozen it**
- **File:** `scripts/promote_candidate_follow.sh:168-179` (tag read), `:231-243` (PATCH); `.github/workflows/promote-candidate-follow.yml:23-25`
- **Severity:** Medium · **Category:** `bug` (race)
- **Description:**
  - The follower reads `promote-cycle-frozen` and only later PATCHes `promote-candidate` to the main tip.
  - Its concurrency group (`promote-candidate-follow-<repo>`) is not the promote cycle's. The cycle freezes by moving the branch to the candidate and then tagging it (`promote_main_cycle.sh:60-64`).
  - If a push-triggered follower reads "no tag" just before the cycle freezes, its `force=false` PATCH still succeeds when the new main tip descends from the candidate. That moves the pinned branch mid-cycle.
  - `pin_head_mismatch` (`promote_main_cycle.sh:52-53`) then fails the cycle, so this is fail-closed, but it wastes a full TMS gate (about 80 min). Inference from the code; not observed in logs. [NEEDS VERIFICATION]
- **Recommended fix:** re-read `git/ref/tags/promote-cycle-frozen` right before the PATCH, on the advance path only (+1 GET). Emit `outcome=frozen reason=tag_appeared` if it now exists.

**BUG-006: `set_issue_phase_label_resilient` relies on the caller's `pipefail`**
- **File:** `scripts/label_helpers.sh:225-237`
- **Severity:** Low · **Category:** `bug`
- **Description:**
  - `_cur="$(gh_retry gh api --paginate … --jq '[.[].name]' 2>/dev/null | jq -cs 'add // []')"` takes the pipeline's status.
  - The helper is sourced and does not set `pipefail`. In a caller without it, a failed GET becomes `[]` with exit 0. The helper then POSTs the target label and never DELETEs the stale phase labels, leaving two phase labels on the issue.
  - Which callers lack `pipefail` is [NEEDS VERIFICATION].
- **Recommended fix:** check `"${PIPESTATUS[0]}"` explicitly, or fetch through `gh_retry_to_file` (`gh_helpers.sh:1149`) and run `jq` on the file.

**BUG-007: Sweep step ordering skips CI recovery on any enumerate failure and under-counts the PAT budget**
- **File:** `.github/workflows/review_autofix_sweep.yml:577-588` (budget end), `:590-598` (CI rerun, no `if:`), `:600-609` (security holds use `GH_PAT`)
- **Severity:** Low · **Category:** `bug`
- **Description:**
  - The snapshot is written at `:239`, but any later failure in the enumerate step skips "Re-run cancelled CI failed jobs once", because that step has the default `success()`.
  - "Record GH_PAT budget at sweep end" runs before the `GH_PAT`-using security-hold step, so `used_in_job` (6 in 39971247714) leaves those calls out.
- **Recommended fix:**
  - Give the CI step `if: ${{ !cancelled() }}`. The script already fails closed on a missing snapshot (`ci_cancelled_rerun.py:405-412`).
  - Move the budget-end step to the end of the job.

**BUG-008: The poll silently caps active projects at 20 tracking issues**
- **File:** `.github/workflows/orchestrate_poll.yml:301-306` (`--limit 20`); consumers `scripts/orchestrate_poll_process.sh:5818-5828`, `:20990-20993`, `:23520`
- **Severity:** Low · **Category:** `bug`
- **Description:** the poller iterates only `tracking_issues.json`. With more than 20 open trackers, `gh issue list`'s default newest-first order starves the oldest projects, and nothing logs it.
- **Recommended fix:** raise the limit to 100 and emit `::warning::` when `COUNT` equals the limit. Better, take the list from API-002's batched query.

### Section 2: GitHub API Call Redundancy Audit

**API-001: The merge-train `release` scan runs twice per 5 minutes**
- **File:** `.github/workflows/orchestrate_poll.yml:239-266` (every `*/5` tick); `.github/workflows/cancel_on_pr_close.yml:391-427`, reached on schedule through `internal-cancel-on-pr-close.yml:6-7` (`*/5`)
- **Severity:** Medium · **Category:** `api-redundancy`
- **Description:**
  - Each `release` costs at least 2 calls: the default-branch read (`review_merge_train.sh:1057`) and the open-PR list (`:83`).
  - When a queued PR passes the base filter it adds about 10 run listings (`:83-90`).
- **Current vs projected:**
  - Today: ≥576 duplicated calls/day at idle, up to about 3,400/day with a non-empty train.
  - Projected: half of that.
- **Recommended fix:**
  - In `cancel_on_pr_close.yml`, run `release` on `schedule` only when no poller backstop exists. The step comment says "core-profile repos have no poll backstop" (`:394`), so gate on `vars.WORKFLOW_PROFILE == 'core'` [NEEDS VERIFICATION that this var reliably identifies poll-less installs].
  - Always keep it for `pull_request` close events.

**API-002: The poll's pre-gate makes 2–3 separate reads every tick**
- **File:** `.github/workflows/orchestrate_poll.yml:288-298` (REST `issues?labels=ai:provider-outage`), `:301-306` (`gh issue list` for trackers), `:352-371` (aliased GraphQL probe on no-project ticks)
- **Severity:** Medium · **Category:** `api-batching`
- **Description:** all three read open-issue data for one repo and could share one aliased GraphQL query.
- **Current vs projected:** 2–3 calls/tick → 1 (about 288–576 calls/day).
- **Recommended fix:**
  - Extend the existing `probe_query` with two aliases:
    - `outage: issues(states: OPEN, labels: ["ai:provider-outage"], first: 100) { nodes { body authorAssociation } }`
    - `tracking: issues(states: OPEN, labels: ["ai:orchestrator-tracking"], first: 100) { nodes { number title labels(first: 50) { nodes { name } } } }`
  - Write the current shapes of `provider_outage_trackers.json` and `tracking_issues.json` from it.
  - Follow `_fetch_candidate_issue_details_graphql`'s batching contract and fail open to today's calls.
  - [NEEDS VERIFICATION] which fields consumers of `provider_outage_trackers.json` read.

**API-003: The sweep reads `default_branch` over REST every tick**
- **File:** `.github/workflows/review_autofix_sweep.yml:285-290`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** the job already uses `github.event.repository.default_branch || github.ref_name` (`:147`). 1 call/tick (48/day).
- **Recommended fix:** pass that value through `env:`. Keep the REST read only as a fallback when the context value is empty, preserving the issue #6629 provenance rule. [NEEDS VERIFICATION] that the schedule payload carries `repository`.

**API-004: Nine separate active-status run listings in one sweep job**
- **File:** `.github/workflows/review_autofix_sweep.yml:337-348`, `:437-439` (2 workflows × 3 statuses, paginated); `scripts/ci_cancelled_rerun.py:316` (3 more for `ci.yml`)
- **Severity:** Low · **Category:** `api-batching`
- **Current vs projected:** 9 listings → 3 repo-wide `actions/runs?status=<s>` listings, filtered client-side on `path`.
- **Description:** the CI script's docstring deliberately rejects the review cache (`ci_cancelled_rerun.py:9-11`). A repo-wide listing also needs pagination and per-workflow completeness checks, which interact with BUG-001. [NEEDS VERIFICATION]
- **Recommended fix:** defer until BUG-001 lands, then evaluate one shared repo-wide snapshot file.

**API-005: `ensure_label_exists` POSTs on every phase transition**
- **File:** `scripts/label_helpers.sh:223`, `:163-197`
- **Severity:** Low · **Category:** `api-redundancy`
- **Description:** every `set_issue_phase_label_resilient` issues a `gh label create`, which normally fails with "already exists". That is 1 content-creating request per transition, and those count toward secondary limits.
- **Recommended fix:** memoize per process (for example a `_AI_LABEL_ENSURED` associative array) after the first success or "already exists". This saves N−distinct-labels calls per process.

No tight retry loops were found in `gh_retry` (`gh_helpers.sh:1060-1138`: classified permanent errors, rate-limit reset waits, 1/2/4/8 s backoff, single attempt for unsafe creates).

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001: Support-file staging is reimplemented inline instead of using `stage_workflow_support.sh`**
- **File:** `.github/workflows/orchestrate_poll.yml:613-895` (about 12 copy-with-`main`-fallback loops), `:208-237`; `.github/workflows/cancel_on_pr_close.yml:117-141`
- **Severity:** Medium · **Category:** `duplication`
- **Description:**
  - The `.codex-workflow-src-main/scripts/${…}` fallback pattern appears 50 times across 7 workflows (orchestrate_poll 18, implement 12, orchestrate 6, plan/clarify/orchestrate_clarify_respond 4 each, cancel_on_pr_close 2).
  - `scripts/stage_workflow_support.sh` already owns `copy_from_ref_or_local` (`:554-597`), `stage_required_entry` / `stage_optional_copy_entry` (`:678-735`) and `emit_consumer_gitignore` (`:1050`).
- **Recommended fix:**
  - Add a manifest-driven entry point, `stage_support_manifest <manifest.json>`, reusing `json_array_lines` (`:599`).
  - Add per-workflow manifests with `required[]`, `optional[]` and `notices{}`.
  - Migrate orchestrate_poll first. This also removes the EXPR-001 body.

**DUP-002: Dispatch → register → correlate is duplicated in `comprehensive-test-and-release.yml`**
- **File:** `.github/workflows/comprehensive-test-and-release.yml:97-169` vs `:343-407`; helpers `sanitize_single_line` / `is_positive_integer` at `:58-66` vs `:291-299`
- **Severity:** Medium · **Category:** `duplication`
- **Description:**
  - The two blocks are identical apart from the workflow file.
  - `test-and-mark-stable.yml` uses `scripts/dispatch_and_watch_workflow_run.sh` for the same job (`:5403`, `:5547`, `:5590`, `:5633`, `:5730`, `:5959`).
- **Recommended fix:**
  - Add `--ref <ref>` to `dispatch_and_watch_workflow_run.sh` (its `gh workflow run` at `:70` has none).
  - Replace both blocks with `--snapshot-only` registration calls that read `run_id` from `GITHUB_OUTPUT`.

**DUP-003: Four `workflow-log-analysis.yml` jobs repeat the same ~12-step agent bootstrap**
- **File:** `.github/workflows/workflow-log-analysis.yml:251-383` (retro), `~828-995` (analyze), `1671-1860` (deep audit), `2278-2460` (API redundancy)
- **Severity:** Medium · **Category:** `duplication`
- **Description:**
  - "Resolve AI engine" is byte-identical apart from `WLA_ENGINE_ROLES` (`:257-288` vs `:1677-1708`).
  - The Codex cache/install/persist/restore steps (`:310-347` vs `:1730-1776`) and the semble trio are also copied. "Persist Codex CLI to tool cache" appears 4× here and 1× in `plan.yml`.
  - Job-level `CODEX_VERSION` is repeated 4× (`:242`, `:817`, `:1661`, `:2268`).
- **Recommended fix:** add a composite action `.github/actions/setup-wla-agent/action.yml` with inputs `engine_roles`, `model`, `reasoning`, `semble_enabled`, following `.github/actions/setup-runtime`. Move `CODEX_VERSION` to the workflow-level `env:`.

**DUP-004: Phase-failure emitters and label helpers are copied three times in `workflow-log-analysis.yml`**
- **File:**
  - `_wla_label_retry` / `_wla_label_create` at `.github/workflows/workflow-log-analysis.yml:1078-1079` and `:1881-1882`, plus the API-redundancy copy;
  - `emit_log_analysis_phase_failure` at `:1100-1193`;
  - `emit_deep_audit_failure` at `:1894-~1990`;
  - the API-redundancy emitter at `~2505-2560`.
- **Severity:** Low · **Category:** `duplication`
- **Recommended fix:**
  - Create `scripts/workflow_log_analysis_helpers.sh` with:
    ```bash
    wla_emit_phase_failure()
    {
    	# <failed_step> <failure_mode> <attempt_count> <summary>; reads GITHUB_RUN_ID,
    	# GITHUB_RUN_ATTEMPT, GITHUB_WORKFLOW, GITHUB_SERVER_URL, GITHUB_REPOSITORY.
    	:
    }
    ```
  - Use `ensure_label_exists` from `label_helpers.sh` for the label. This also fixes CONSIST-001.

**DUP-005: The same six-output failure guard is repeated on several `implement.yml` steps**
- **File:** `.github/workflows/implement.yml:6588`, `:6684`, `:6737` (adds `diagnose_post_codex_failure.handled`), `:6814`; `:6967` [NEEDS VERIFICATION]
- **Severity:** Low · **Category:** `duplication`
- **Description:** adding a new "blocked" output means editing every copy, which invites drift.
- **Recommended fix:** add an `if: always()` step `id: failure_guard` that turns those outputs (passed through `env:`) into `blocked=true|false`. Then use `if: (failure() || cancelled()) && steps.failure_guard.outputs.blocked != 'true'`.

### Section 4: Expression Size Limit Risk Assessment

Method: each estimate is the body's line count × the average line width read off the file (base indentation stripped). Every estimate is [NEEDS VERIFICATION]: run `python3 tests/test_workflow_file_size_limit.py` with a printing variant (see EXPR-004). The CI guard fails at 18,000, so a green CI implies each body below is under 18,000.

**EXPR-001: `orchestrate_poll.yml` "Stage workflow support files"**
- **File:** `.github/workflows/orchestrate_poll.yml:608-897`
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:**
  - The 289-line body contains `${{ vars.PROMPT_PRELUDE_REFACTOR_ENABLED … }}` (`:753`), `${{ vars.UNATTENDED_IDENTITY_REINJECT_ENABLED … }}` (`:754`) and `${{ github.repository }}` (`:875`), so the whole body is one expression.
  - **Estimated size:** about 16,000–18,000 characters.
  - **Headroom:** about 3,000–5,000 characters to 21,000; about 0–2,000 to the CI guard.
  - It is the most likely body to trip the guard next, because the required-scripts list at `:614` grows with every new support file.
- **Recommended fix:**
  - Move the three values into the step's `env:`, for example `PRELUDE_REFACTOR_ENABLED_VAR: ${{ vars.PROMPT_PRELUDE_REFACTOR_ENABLED || 'false' }}`.
  - Use the runner's `GITHUB_REPOSITORY` at `:875`.
  - The body then contains no expression and is no longer subject to the limit. DUP-001 can follow.

**EXPR-002: `workflow-log-analysis.yml` agent-pass bodies**
- **File:** `.github/workflows/workflow-log-analysis.yml:1068-1401` ("Run workflow log analysis"), `:1873-2128` ("Run deep audit pass"), `:2473-2726` ("Run API redundancy pass")
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:**
  - These bodies interpolate `${{ github.run_id }}`, `${{ github.run_attempt }}`, `${{ github.workflow }}`, `${{ github.server_url }}` and `${{ github.repository }}`:
    - analysis pass: `:1113-1115`, `:1128`, `:1134-1136`, `:1173-1175`;
    - deep audit: `:1905-1960`;
    - API redundancy: `:2505-2560`.
  - **Estimated sizes and headroom to 21,000:**

    | Body | Lines | Estimated chars | Headroom |
    |---|---|---|---|
    | Analysis | 333 | about 15,500–17,900 | about 3,100–5,500 |
    | Deep audit | ~256 | about 12,000–14,000 | about 7,000–9,000 |
    | API redundancy | ~254 | about 12,000–14,000 | about 7,000–9,000 |

  - Every value has an identical runner env var.
- **Recommended fix:** replace each interpolation with `${GITHUB_RUN_ID}`, `${GITHUB_RUN_ATTEMPT}`, `${GITHUB_WORKFLOW}`, `${GITHUB_SERVER_URL}` and `${GITHUB_REPOSITORY}`. That leaves zero expressions and removes all three bodies from the limit. Then extract them (DUP-004).

**EXPR-003: `test-and-mark-stable.yml` Phase 2 (and Phase 1) waiters**
- **File:** `.github/workflows/test-and-mark-stable.yml:996-1263`
- **Severity:** Medium · **Category:** `expression-limit`
- **Description:**
  - The 267-line body interpolates `${{ steps.create-issue.outputs.created_after }}` three times (`:1014`, `:1026`, `:1044`).
  - **Estimated size:** about 15,000–17,500 characters. **Headroom:** about 3,500–6,000 to 21,000.
  - Phase 1 (`:842-971`, about 9,000–10,500 characters) has one interpolation at `:882`.
  - Overlaps the 2026-10-22 EXPR series; this is the remaining unfixed instance.
- **Recommended fix:** add `CREATED_AFTER: ${{ steps.create-issue.outputs.created_after }}` to both steps' `env:` and use `"${CREATED_AFTER}"`. The same file already does this at `:1948` and `:2780` ("Promoted to env so the run: script body contains no ${{ }}").

**EXPR-004: The expression guard is blind to headroom and to non-`run:` surfaces**
- **File:** `tests/test_workflow_file_size_limit.py:66-98`, `:101-110`
- **Severity:** Low · **Category:** `tech-debt`
- **Description:**
  - The test only fails at 18,000 and only scans `jobs.*.steps[*].run`.
  - It never reports headroom, and it does not check `if:` or `with:` strings or composite actions (`.github/actions/*/action.yml`).
  - Current `if:` predicates are all below about 1,000 characters (for example `implement.yml:6737`, `review_autofix.yml:3800`), and the composite actions pass inputs through `env:`, so nothing is at risk today.
- **Recommended fix:**
  - Have `main()` print the 10 largest interpolated bodies with their lengths.
  - Add a 15,000-character warning tier to `scripts/ci_budget_watch.py`, the existing early-warning issue path (`agents.md:2495-2505`).

**Workflow file size:** the binding limit is **512,000 bytes**, with the CI guard at 480,000 (`tests/test_workflow_file_size_limit.py:26-27`; `agents.md:2506-2523`), not 1 MB (see CONSIST-005).
- No workflow can be near 800 KB while CI is green.
- The largest by line count are `test-and-mark-stable.yml` (7,875 lines), `implement.yml` (7,159) and `review_autofix.yml` (5,755).
- Exact byte sizes were not measured here. Check `test-and-mark-stable.yml` against the 440,000-byte `CI_BUDGET_WORKFLOW_WARN_BYTES` early warning with `wc -c`. [NEEDS VERIFICATION]

### Section 5: Cross-Cutting Concerns

**DEAD-001: `source "${GH_API_RETRY_HELPERS:-/dev/null}"` is a no-op in workflows with no bootstrap step**
- **File:**
  - `.github/workflows/cancel_on_pr_close.yml:43`
  - `.github/workflows/orchestrate_poll.yml:135`, `:531`
  - `.github/workflows/orchestrate.yml:214`, `:388`
  - `.github/workflows/plan.yml:360`, `:3170`
  - `.github/workflows/clarify.yml:393`, `:2361`
  - `.github/workflows/workflow_failure_heal.yml:118`
  - `.github/workflows/check_failure_triage.yml:323`
  - `.github/workflows/review_autofix_sweep.yml:219`
- **Severity:** Low · **Category:** `dead-code`
- **Description:**
  - Only `validate.yml:167`, `review_autofix.yml:376`, `implement.yml:178`, `security-audit.yml:122` and `orchestrate_clarify_respond.yml:132` set the variable. README `:3576` says it is "set by the `Bootstrap GitHub API retry helper` step".
  - Everywhere else the inline fallback `gh_api_retry() { … gh api "$@"; }` always runs, so the support-ref branch reads get no retry. That is fail-safe (they fall back to `github.sha` or `stable`).
  - At `review_autofix_sweep.yml:219` the line is also redundant: `:216-217` already sourced `scripts/gh_helpers.sh`.
  - [NEEDS VERIFICATION] per file that no earlier step exports it under another route.
- **Recommended fix:** either add the bootstrap step (pattern at `review_autofix.yml:376`) or drop the dead `source` line and keep the explicit fallback.

**CONSIST-001: Label description drift for `ai:log-analysis-failed`**
- **File:** `.github/workflows/workflow-log-analysis.yml:1184-1187` ("Workflow log analysis failed") vs `.github/ai/label_contract.v1.json:84-86` and `scripts/label_helpers.sh:109` ("…failed before producing an analysis report")
- **Severity:** Low · **Category:** `consistency`
- **Recommended fix:** use `ensure_label_exists` (DUP-004).

**CONSIST-002: Boolean env flags are parsed differently from file to file**
- **File:**
  - `scripts/ci_cancelled_rerun.py:415-416`: only `"true"` counts, so `1`, `yes` or `on` disables auto-rerun.
  - `scripts/retarget_merged_base.sh:110`, `:134`: only a literal `false` disables, and it is case-sensitive.
  - `scripts/promote_candidate_follow.sh:89-93`: only `false` disables, but case-insensitive.
  - `.github/workflows/review_autofix_sweep.yml:269-271`: `true|1|yes|on`.
- **Severity:** Low · **Category:** `consistency`
- **Recommended fix:** adopt the `true|1|yes|on` rule (`is_truthy` in `orchestrate_poll_process.sh`, referenced at `orchestrate_poll.yml:320`) in the Python and shell helpers. Existing values keep their meaning.

**CONSIST-003: Sweep header and summary-line drift**
- **File:** `.github/workflows/review_autofix_sweep.yml:58-60`, `:274`, `:288`, `:488-490`, `:552`
- **Severity:** Low · **Category:** `consistency`
- **Description:**
  - The header says the "30 min cadence matches the existing orchestrator poll cadence", but the poll is `*/5` (`internal-orchestrate-poll.yml:5`).
  - Two `AUTOFIX_SWEEP_END` variants omit `snapshot_incomplete=` (`:274`) or both `skipped_merge_queued=` and `snapshot_incomplete=` (`:288`).
  - `claude_capacity_wait` skips are not counted, so the counters do not add up to `candidates=`.
- **Recommended fix:** correct the comment. Emit one `AUTOFIX_SWEEP_END` field set everywhere, adding `skipped_capacity_wait=` (additive, §6).

**CONSIST-004: `retarget_merged_base.sh` uses raw `gh api` with no retry**
- **File:** `scripts/retarget_merged_base.sh:54`, `:64`, `:150`
- **Severity:** Low · **Category:** `consistency`
- **Description:** a transient 5xx becomes `reason=lookup_failed` and no retarget, while neighbouring helpers use `gh_retry` / `gh_api_retry`. Also, `per_page=20` at `:54` can miss the latest merged PR on a heavily reused branch name.
- **Recommended fix:**
  - Source `gh_helpers.sh` when present and wrap the two reads in `gh_api_retry --optional`, keeping the 404 detection via `GH_API_RETRY_LAST_STATUS`.
  - Leave the PATCH as a single attempt.

**CONSIST-005: The audit prompt states the wrong workflow size limit**
- **File:** `prompts/mode-workflow-audit.txt:100`, `prompts/_templates/mode-workflow-audit.txt:93` ("1 MB (1,048,576 characters) … exceeding 800 KB")
- **Severity:** Low · **Category:** `consistency`
- **Description:** this contradicts the measured 512,000-byte limit and the 480,000-byte guard (`tests/test_workflow_file_size_limit.py:4-5`, `:26-27`; `agents.md:2506-2512`). An audit following the prompt would call a 600 KB workflow, which GitHub refuses to run, healthy.
- **Recommended fix:** change both files (template first, then re-render) to "512,000 bytes; flag ≥ 440,000 (`CI_BUDGET_WORKFLOW_WARN_BYTES`) and ≥ 480,000 (CI guard)".

**SHELL-001: Unquoted test operands (SC2086)**
- **File:** `.github/workflows/test-and-mark-stable.yml:962`, `:1254`, `:1617`, `:2640`, `:3980` (`if [ $IDLE -ge $INACTIVITY_LIMIT ]`)
- **Severity:** Low · **Category:** `shellcheck`
- **Description:** both values are arithmetic-derived, so this is not exploitable. Fix it when EXPR-003 touches these bodies.
- **Recommended fix:** quote both operands.

**SEC-001: Step outputs and context values interpolated into shell strings instead of `env:`**
- **File:**
  - `.github/workflows/implement.yml:1958-1971` (`steps.baseline_refctx.outputs.branch` / `.status`, `steps.checkout_baseline.outcome`)
  - `.github/workflows/cancel_on_pr_close.yml:33-34`
  - `.github/workflows/orchestrate_poll.yml:125-126`, `:521-522`, `:875`
- **Severity:** Low · **Category:** `security` (hardening)
- **Description:**
  - These are safe today. The branch output is emitted only after `re.fullmatch(r"ai/reissue-baseline/pr-(\d+)-([0-9a-f]{12})-\d+-\d+", …)` (`implement.yml:1167`), and the other values are repository- or SHA-shaped.
  - Git ref names may legally contain `$(`, so a future relaxation of that regex would turn `:1964` and `:1969` into command injection.
- **Recommended fix:** pass the values through `env:`, as the file already does for `IMPLEMENT_RESOLVED_FALLBACK_REF` (`:1953`).

**TODO/FIXME/HACK markers:** none in `scripts/` or `.github/workflows/`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 12 | BUG-002, BUG-003, BUG-004, BUG-005, API-001, API-002, DUP-001, DUP-002, DUP-003, EXPR-001, EXPR-002, EXPR-003 |
| Low | 19 | BUG-006, BUG-007, BUG-008, API-003, API-004, API-005, DUP-004, DUP-005, EXPR-004, DEAD-001, CONSIST-001, CONSIST-002, CONSIST-003, CONSIST-004, CONSIST-005, SHELL-001, SEC-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/ci_cancelled_rerun.py`, `agents.d/<new>.md`, `tests/` (listing-shift cases) | Small |
| API call optimization | `orchestrate_poll.yml`, `cancel_on_pr_close.yml`, `review_autofix_sweep.yml`, `scripts/label_helpers.sh` | Medium |
| Code modularization | `scripts/stage_workflow_support.sh`, `orchestrate_poll.yml` (+5 phase workflows), `comprehensive-test-and-release.yml`, `scripts/dispatch_and_watch_workflow_run.sh`, `workflow-log-analysis.yml`, new `.github/actions/setup-wla-agent/`, new `scripts/workflow_log_analysis_helpers.sh`, `implement.yml` | Large |
| Expression size reduction | `orchestrate_poll.yml`, `workflow-log-analysis.yml`, `test-and-mark-stable.yml`, `tests/test_workflow_file_size_limit.py` | Small |
| Medium/Low fixes | `test-and-mark-stable.yml`, `comprehensive-test-and-release.yml`, `cancel_on_pr_close.yml`, `promote_candidate_follow.sh`, `label_helpers.sh`, `review_autofix_sweep.yml`, `retarget_merged_base.sh`, `prompts/mode-workflow-audit.txt` (+ template), 8 workflows for DEAD-001 | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-10-23)

### Safety Tag Legend
`SAFE_TO_MERGE` means static reading proves every precondition: same endpoint or a strict field subset, the same pagination, filters and token, the same function or step with no mutation in between, unchanged error and retry behaviour, no concurrency boundary crossed, and any documented cache contract kept. The implement stage may act on these without further review. `NEEDS_VERIFICATION` means the change looks sound, but at least one precondition cannot be proven from source alone; the named check must pass first. `RISKY_SKIP` means the call is in a pagination path, a retry or race-defending path, an identity or auth flow, or a path whose log keys other tooling reads. It is listed only for visibility and must not be auto-implemented. This pass found **no** `SAFE_TO_MERGE` findings.

### Consolidation Candidates (MERGE-###)

**MERGE-001: Backfill makes a separate `totalCount` GraphQL call before its first paginated PR page**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:** `scripts/workflow_failure_heal_non_retryable_backfill.sh:172` (`COUNT_QUERY`) and `:177` (count call); `:161` (`QUERY`) and `:233-235` (first page call).
- **Current → proposed:** 1 count call + ⌈MAX_PRS/10⌉ page calls (default 1 + 5) per tick → 0 + 5 on ticks where `total_open ≤ MAX_PRS`. That saves 1 call per tick, about 48/day at the `*/30` sweep cadence (`review_autofix_sweep.yml:79-80`). Rotation ticks stay at the same count.
- **Endpoint:** GraphQL `repository.pullRequests(states: OPEN)`, both calls.
- **Evidence:**
  - Both queries read the same `pullRequests(states: OPEN)` connection.
  - The count only chooses the start cursor (`:188-226`). When `total_open ≤ MAX_PRS`, `cursor` stays `""`, so the first page call is exactly the query that would run anyway.
    ```bash
    COUNT_QUERY='query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { pullRequests(states: OPEN) { totalCount } } }'
    ```
- **Proposed fix (for manual review only):** add `totalCount` to `QUERY`'s `pullRequests` selection.
  - Issue the first page with `cursor=""`.
  - If `totalCount ≤ MAX_PRS`, keep that page and drop `COUNT_QUERY`.
  - Otherwise, throw the page away and run today's rotation path (`:188-226`).
- **Safety rationale:** this merges into a manually paginated, window-rotated listing (`:165-170`, `:195-224`, `:229-260`). That is a pagination trigger, and it risks shifting page boundaries and the `listing_truncated` / `window_rotated` log semantics.
- **Downstream signal:** do not auto-implement. Before changing anything, a reviewer must confirm three things:
  - the rotated window's start (`window * MAX_PRS`) is unchanged;
  - a discarded first page on rotation ticks does not count toward `fetched`;
  - the `outcome=info reason=window_rotated` line is still emitted with the same fields.

**MERGE-002: The open-PR set is listed up to 5 times in one `review_autofix_sweep` job**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:**

  | Step | Location | Call | When it runs |
  |---|---|---|---|
  | Enumerate | `.github/workflows/review_autofix_sweep.yml:228-237` | REST `GET pulls?state=open`, `--paginate` | every tick |
  | CI recovery | `scripts/ci_cancelled_rerun.py:117-145`, called at `:317` | REST `GET pulls?state=open`, `--paginate --slurp` | every tick with eligible PRs |
  | Backfill | `scripts/workflow_failure_heal_non_retryable_backfill.sh:161`, `:229-260` | GraphQL pages of 10, `orderBy UPDATED_AT ASC` | every tick |
  | Security holds | `scripts/review_security_hold_sweep.py:47-65`, `:276-280` | GraphQL pages of 50, `orderBy UPDATED_AT DESC` | only when `SINGLE_ISSUE_SECURITY_PASS_ENABLED == 'true'` (`review_autofix_sweep.yml:601`) |
  | Provider outage | `scripts/provider_outage.py:386-395` | GraphQL pages of 50 | only on outage recovery (docstring `:52-54`) |

- **Current → proposed:**
  - Today: ⌈N/100⌉ + ⌈N/100⌉ + (1 + ≤5) + ⌈N/50⌉ + ⌈N/50⌉ in the worst case.
  - Proposed: unchanged in this pass. A later design could share one GraphQL snapshot between the backfill and the security-hold pass. Both read `headRefOid` and `comments(last: 100)` with author and body, which would save ⌈N/50⌉ calls per tick when the security pass is on.
- **Endpoints:** REST `repos/{r}/pulls`; GraphQL `repository.pullRequests`.
- **Evidence:** every listing reads open PRs and their head SHA. `ci_cancelled_rerun.py:8-10` rejects reuse on purpose:
  ```text
  The sweep's earlier PR snapshot cannot prove a head is still current after
  the review-dispatch loop
  ```
- **Proposed fix:** none to implement now. Do not reuse the enumerate snapshot (`${RUNNER_TEMP}/ci-cancelled-pr-snapshot.json`, `:239`) in place of the CI step's live re-read. That re-read is the `superseded_head` guard (`ci_cancelled_rerun.py:328-330`).
- **Safety rationale:**
  - Every call is paginated.
  - The CI re-read deliberately defends against heads moving during the dispatch loop.
  - The two GraphQL listings use opposite orderings, page sizes and windows (`MAX_PRS` rotation against all PRs).
  - The security pass may run under a different token (`GH_PAT || github.token`, `review_autofix_sweep.yml:603`).
- **Downstream signal:** do not auto-implement. A shared backfill/security snapshot needs a design review covering:
  - ordering (ASC vs DESC);
  - the MAX_PRS window against full coverage;
  - token scope (`SECURITY_HOLD_SWEEP_TOKEN_MODE`, `review_security_hold_sweep.py:12-21`);
  - the `hasPreviousPage` comment-history fallback (`:296-305`).

**MERGE-003: The promote-candidate follower makes 3–4 separate ref reads per invocation**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:** `scripts/promote_candidate_follow.sh`:
  - `:148` `branches/${FOLLOW_BRANCH}`
  - `:168` `git/ref/tags/${FOLLOW_TAG}`
  - `:181` `git/ref/heads/main`
  - `:210` `git/tags/${TAG_OBJECT_SHA}`, only for an annotated tag
- **Current → proposed:** 3 (or 4) REST GETs → 1 aliased GraphQL query, in theory: three `ref(qualifiedName:)` aliases, with `... on Tag { message }` folding in the tag-object read.
- **Endpoints:** REST `branches/{b}`, `git/ref/tags/{t}`, `git/ref/heads/main`, `git/tags/{sha}`.
- **Evidence:**
  - All four calls are sequential reads in step 1 or step 3, with no write in between (`:145-229`).
  - `follow_api` routes them through `gh_api_retry` (`:108-114`).
- **Proposed fix (for manual review only):** one GraphQL query following the alias pattern of `_fetch_candidate_issue_details_graphql` (`scripts/orchestrate_poll_process.sh`).
- **Safety rationale:** this path explicitly defends against a freeze-tag race (Deep Audit BUG-005). Merging would also change the error semantics:
  - REST 404 → `BRANCH_EXISTS=false` or `TAG_EXISTS=false` (`:150`, `:170`), while GraphQL returns `null`. The fail-closed `tag_read_failed` behaviour (`:173-174`) has to be kept.
  - REST `protected` (`:158-162`) has no exact GraphQL equivalent that covers both rulesets and classic protection.
- **Downstream signal:** do not auto-implement. BUG-005 adds a fresh tag re-read before the PATCH (`:237-238`), which runs against this consolidation. Settle BUG-005 first, then have a reviewer decide whether a GraphQL `protected` substitute is acceptable.

**MERGE-004: The retarget lookup makes 2 REST calls per hop that one GraphQL query could cover**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Calls involved:** `scripts/retarget_merged_base.sh:54-55` (closed-PR search by head) and `:64` (head ref read), both inside `retarget_lookup_merged`. The function is called once per hop from `retarget_follow` (`:84-95`, at most `RETARGET_MERGED_BASE_MAX_HOPS`, default 3).
- **Current → proposed:** 2 calls per hop → 1 call per hop (up to 6 → 3 per invocation). Only non-default branches trigger it (`:110`, `:134`).
- **Endpoints:** REST `repos/{r}/pulls?state=closed&head={owner}:{b}&per_page=20` and `repos/{r}/git/ref/heads/{b}` → GraphQL `repository { ref(qualifiedName: "refs/heads/<b>") { target { oid } } pullRequests(headRefName: "<b>", states: MERGED, last: 20) { nodes { number baseRefName headRefOid mergedAt headRepositoryOwner { login } } } }`.
- **Evidence:** both calls take the same `branch` in the same function with no mutation in between. The second call depends only on the first's `head_sha` for a client-side comparison (`:74`), not for its URL.
  ```bash
  if ! merged_json="$(gh api "repos/${repo}/pulls?state=closed&head=${head_query}&per_page=20" \
  ...
  if ! tip="$(gh api "repos/${repo}/git/ref/heads/$(retarget_urlencode "${branch}")" --jq '.object.sha // ""' 2>"${tip_error_file}")"; then
  ```
- **Proposed fix:** replace both calls in `retarget_lookup_merged` with one GraphQL call that keeps the output contract (`"<merged_pr_number> <base_ref>"` or empty, return 1 on lookup failure). Wrap it in `gh_api_retry --optional` (this also covers Deep Audit CONSIST-004).
- **Safety rationale:** error semantics are not provably identical.
  - The REST 404 on the ref means "deleted, retarget allowed" (`:65-71`). GraphQL returns `ref: null`, which also happens for permission or visibility failures.
  - `head=owner:branch` limits matches to same-owner heads. GraphQL `headRefName` also matches fork PRs unless they are filtered on `headRepositoryOwner`.
  - Today's REST call takes the 20 most recent *closed* PRs and then filters to merged. GraphQL `states: MERGED, last: 20` selects differently.
- **Downstream signal:** before implementing, verify each of these with a fixture in `tests/`:
  - (a) a deleted ref, a permission error and an existing ref map to the same three outcomes as today;
  - (b) the fork-PR filter on `headRepositoryOwner.login == owner` keeps today's owner scoping;
  - (c) choosing the latest merged PR by `mergedAt` matches `sort_by(.merged_at) | last` (`:55`) for a reused branch name.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001: `set_issue_phase_label_resilient` GETs the issue's labels even though the POST it makes next returns the full label set**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:** `scripts/label_helpers.sh:226-227` (paginated GET `issues/{n}/labels`) and `:266-267` (POST `issues/{n}/labels`, output discarded to `/dev/null`).
- **Current → proposed:** 1 GET (≥1 page) + 1 POST + k DELETE per transition → 1 POST + k DELETE. That saves 1 call per phase transition, on top of API-005's saving.
- **Endpoint:** REST `repos/{r}/issues/{n}/labels`, GET and POST.
- **Evidence:**
  - The GET only computes `_stale`, the phase labels other than the target (`:253-254`).
  - The POST already runs before any DELETE (`:240-251`: "POST the target first, so the issue is never without a phase label").
  - GitHub's "Add labels to an issue" endpoint answers `200` with an array of label objects. The docs search did not find an explicit statement that this is the issue's full set after the add ([REST API endpoints for labels](https://docs.github.com/en/rest/issues/labels)).
- **Proposed fix (for manual review only):**
  - Capture the POST's stdout instead of discarding it.
  - Compute `_stale` from `[.[].name]` of that response with the existing jq filter (`:253-254`).
  - Drop the GET.
  - Keep the "POST failed → `SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME=failed`" branch.
  - This also removes the BUG-006 `pipefail` exposure at `:226-227`.
- **Safety rationale:**
  - The GET uses `--paginate` (pagination trigger), while the POST response is a single unpaginated body, so a large label set could change page-boundary semantics.
  - Removing the GET also removes the `GET labels failed — falling back to POST add` warning (`:228`), which is an observable log change.
- **Downstream signal:** do not auto-implement. A reviewer must:
  - confirm against a live repo that the POST response lists every label on an issue with more than 30 labels, so it is not truncated;
  - confirm that no log parser or test greps the `GET labels failed` string (`tests/`, `scripts/cost_audit.py`).

**REUSE-002: The GH_PAT login is resolved up to 3 times per sweep job**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:**
  - `scripts/workflow_failure_heal_non_retryable_backfill.sh:150`: every tick, `GH_TOKEN: secrets.GH_PAT`, `review_autofix_sweep.yml:566`.
  - `scripts/review_security_hold_sweep.py:229`: legacy token mode, which is the default here; no workflow sets `SECURITY_HOLD_SWEEP_TOKEN_MODE`. Runs when the security pass is enabled. `GH_TOKEN: GH_PAT || github.token`, `review_autofix_sweep.yml:603`.
  - `scripts/provider_outage.py:349-353`: memoized per process, called only when the tracker list is non-empty (docstring `:47-48`).
- **Current → proposed:** up to 3 `GET /user` calls per tick → 1, by passing the login forward through a step output.
- **Endpoint:** REST `GET /user`.
- **Evidence:** all three read `.login` for the same PAT, in steps of the same job.
- **Proposed fix (for manual review only):** none. Each helper uses the login as a trust anchor for marker authorship, so it is resolved inside the helper on purpose.
- **Safety rationale:** this is an identity/auth flow (an auth trigger).
  - `review_security_hold_sweep.py:203-246` deliberately scopes which token resolves which identity, and can fall back to `github.token`, so the identities can differ.
  - Passing a login between steps would widen the trust boundary.
- **Downstream signal:** do not auto-implement. It needs a security review of marker-author trust before the login is shared across helpers.

**REUSE-003: Merge-train `release` reads `default_branch` over REST even on a `pull_request` close event that carries it**
- **Safety tag:** `RISKY_SKIP`
- **Calls involved:** `scripts/review_merge_train.sh:1138-1149` (`_mt_resolve_default_branch`, REST `GET repos/{r}`). It is reached lazily through `_mt_inflight_review_branches` (`:1155`), which runs at most once per release, and only once a queued PR passes the base filter (`:1359-1360`). The callers are `.github/workflows/cancel_on_pr_close.yml:424` and `.github/workflows/orchestrate_poll.yml:263`.
- **Current → proposed:** 1 → 0 per release that evaluates a queued PR, by seeding `MT_DEFAULT_BRANCH` from `${{ github.event.repository.default_branch }}` through `env:`.
- **Endpoint:** REST `repos/{r}`.
- **Evidence:** `MT_DEFAULT_BRANCH=""` (`:1137`) is the memo. Because `_mt_inflight_review_branches` runs in a `$(...)` subshell (`:1360`), the memo never outlives that one call, so the read cannot already be deduplicated by the memo.
- **Proposed fix (for manual review only):** pre-set `MT_DEFAULT_BRANCH` only when the env value matches `^[A-Za-z0-9._/-]+$` (the existing validation, `:1143`). Fall back to the REST read otherwise.
- **Safety rationale:**
  - The read guards the issue #6629 provenance check ("no fallback, so a failed read is never mistaken for main", `:1132-1136`) inside a listing that explicitly defends against status-transition races (`:1068-1080`, double status pass at `:1166`). That is a race-defense trigger.
  - Changing where the value comes from changes the trust model.
- **Downstream signal:** do not auto-implement. A reviewer must confirm three things:
  - the event payload's `repository.default_branch` is trusted equally for `pull_request` (closed) and `schedule` events;
  - the `MERGE_TRAIN_RUNS_LISTING outcome=incomplete reason=default_branch_unavailable` log line keeps its meaning;
  - the gain is worth it. Per the timing note under API-001, this call only happens when the train is non-empty.

### Dead Calls (DEAD-API-###)
No findings. Every call reviewed in this pass has its result consumed:
- the sweep listings at `review_autofix_sweep.yml:228-439`;
- the cancel/close listings and the PR-state batch at `cancel_on_pr_close.yml:155-345`;
- the follower reads at `promote_candidate_follow.sh:148-243`;
- every `ci_cancelled_rerun.py` listing;
- the backfill count, skip and page calls;
- every merge-train release read.

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP. Removing the scheduled `release` from `cancel_on_pr_close.yml:391-427` changes observable `MERGE_TRAIN_*` log output that this report's analysis reads (`MERGE_TRAIN_RELEASE_SUMMARY`). Correction to its numbers: an idle release costs 1 call (open PRs, `review_merge_train.sh:1343`), because the default-branch read is lazy (`:1359-1360`), so idle duplication is about 288 calls/day, not ≥576. `vars.WORKFLOW_PROFILE == 'core'` already appears as a gate value at `review_autofix_sweep.yml:173`.
- API-002: NEEDS_VERIFICATION. This resolves its open item: the only consumer of `provider_outage_trackers.json` (`orchestrate_poll.yml:1140`) reads only `.pull_request`, `.author_association` and `.body`. GraphQL `issues` already excludes PRs, and `authorAssociation` uses the same OWNER/MEMBER/COLLABORATOR values. Three constraints apply:
  - Keep the failure split: the outage read fails open (`:294-296`), while the tracking listing is fatal under `set -e` (`:301`).
  - Keep the `search`-based probe (`:352-364`) conditional on `COUNT == 0`, not folded into every tick.
  - Keep `first: 20` until BUG-008 lands.
- API-003: NEEDS_VERIFICATION. Alternate fix: scheduled runs (`*/30`, `:79-80`) run on the default branch, so `GITHUB_REF_NAME` is the default branch when `GITHUB_EVENT_NAME=schedule`. Reuse it only then, and keep the REST read at `:285` for `workflow_dispatch` (`:81`). The read uses `READ_TOKEN` (`github.token`), so the saving lands on the GITHUB_TOKEN budget, not GH_PAT.
- API-004: RISKY_SKIP. The listings are paginated (`review_autofix_sweep.yml:340-343` `--paginate`), and `ci_cancelled_rerun.py:84-86` enforces strict per-status completeness, so a shared repo-wide listing changes page-boundary semantics. Defer it, as the Deep Audit says.
- API-005: NEEDS_VERIFICATION. The memo must be keyed `repo:label`, set only on success or "already exists" (`label_helpers.sh:177-193`), and be process-local, because the helper is sourced by many workflows. Pairs with REUSE-001 for the GET in the same function.

### Summary Counts

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | MERGE-004 |
| RISKY_SKIP | 6 | MERGE-001, MERGE-002, MERGE-003, REUSE-001, REUSE-002, REUSE-003 |

The counts cover this pass's new findings only. The Deep Audit cross-references are tagged in the section above: API-002, API-003 and API-005 are NEEDS_VERIFICATION; API-001 and API-004 are RISKY_SKIP.

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.