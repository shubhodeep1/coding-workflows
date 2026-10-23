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