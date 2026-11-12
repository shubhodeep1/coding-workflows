## Executive Summary

- **The collection window is far smaller than it looks, and almost all of it is skipped runs.** 903 of the 1,000 collected runs ended `skipped` or another non-success/failure/cancelled state. 821 of them come from four AI-phase families: `clarify` 208/211, `implement` 206/208, `plan` 202/206 and `orchestrate_clarify_respond` 205/206. The cap filled within about 1h51m (2026-11-12 17:36 to 19:27 UTC). The cause is in code: `list_runs_for_repo` in `scripts/collect_workflow_logs.py` (lines 697–717) counts every listed run toward `max_runs`, skipped or not. **Impact:** the window could cover about 10× more executed runs. **Confidence:** high.
- **There is no token or cost telemetry for this window.** `codex_calls`, `or_calls`, Claude counts and `cache_hit_rate` are 0 or null across all 115 runs with parsed logs. Every AI-phase run that actually executed was `log_download_status: not_selected`, for example implement 43278758698 (1,683 s), plan 43278305427 (796 s) and review_autofix 43285697875 (772 s). **Impact:** no cost recommendation can be grounded this cycle. **Confidence:** high.
- **CI is the critical-path bottleneck, probably from runner queueing (inference).** CI p50 is 2,968 s and p95 is 3,605 s, with a maximum of 3,640 s (run 43278162439). Yet every test job in `.github/workflows/ci.yml` has `timeout-minutes: 20`, and `static-checks` has 15. At the same time, lightweight workflows stretched: `lint_pr_body_auto_close` took 24 s in run 43277312634 (18:11) and 1,044 s in run 43278412636 (18:18). Both were created at the same second as a CI run. **Impact:** about 25–30 minutes per PR CI run. **Confidence:** medium.
- **The orchestrator's view of completed runs is crowded out by skipped runs (inference).** `_load_actions_runs_cached` in `scripts/orchestrate_poll_process.sh:17777` fetches only `status=completed&per_page=20` "so stall-judge diagnostics can inspect recent review/autofix workflow outcomes". At about 7.4 skipped runs per minute (821 / 111 min), that window covers under 3 minutes and is almost all skipped clarify/plan/implement runs. **Impact:** the stall judge probably never sees review/autofix conclusions. **Confidence:** medium.
- **One CI failure:** run 43281039796, job `tests-hooks-and-orchestrator`, step "Merge-train and gh_helpers rate-limit tests (CLAUDE.md §15)". That is 1 failure in 11 CI runs (9.1%). The full log was not available in this sandbox, so the root cause is unknown. **Confidence:** low on the cause, high that it happened.
- **8 of 11 `orchestrate_poll` runs were cancelled before their first step, after waiting 503–787 s.** This fits a newer pending run replacing an older one. The cron is `*/5` in `internal-orchestrate-poll.yml`, the concurrency group is `ai-orchestrate-poll-…` with `cancel-in-progress: false`, and the three successful polls ran 1,841 s, 2,081 s and 2,590 s. **Impact:** these are expected, not failures, but they inflate the cancelled count. **Confidence:** high.
- **Semble fallbacks are healthy.** All 36 `SEMBLE_FALLBACK` lines are contract-test fixtures (4 per CI run across 9 CI runs) and none are runtime fallbacks. No `SEMBLE_QUERY`, `SERENA_*` or `AI_MEMORY_TELEMETRY` lines were observed, so production behaviour of these systems is unmeasured. **Confidence:** high.

> **Data gap:** the run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was not mounted in this sandbox. No `summary.json`, `errors/`, `slow/` or `recent/` files could be read. All findings come from `analysis_context.json` and the repository source.

## Speed Optimizations

1. **CI wall-clock: tell queue time apart from compute time first, then cancel superseded PR runs** (critical path)
   - **Evidence:**
     - CI p50 2,968 s and p95 3,605 s against a 20-minute per-job ceiling plus a 5-minute `lint` aggregator (`needs:`).
     - Lightweight workflows started at the same moment as CI grew as CI runs stacked up between 18:11 and 18:33:

       | Workflow | Run | Created (UTC) | Duration |
       |---|---|---|---|
       | lint_pr_body_auto_close | 43277312634 | 18:11:55 | 24 s |
       | lint_pr_body_auto_close | 43277959550 | 18:15:38 | 114 s |
       | lint_pr_body_auto_close | 43278162630 | 18:16:49 | 693 s |
       | lint_pr_body_auto_close | 43278412636 | 18:18:15 | 1,044 s |
       | integration_pr_readiness | 43278412764 | 18:18:15 | 771 s |
       | cancel_on_pr_close | 43285237378 | 18:57:52 | 1,417 s |

   - **Root cause (inference):** runner concurrency saturation. `ci.yml` has no top-level `concurrency:` block, so a newer push to the same PR does not cancel the older CI run.
   - **Change, step 1 (diagnostic, no behaviour change):** have the collector record per-job queue time (`started_at - created_at`) for CI and lightweight families, not only for failed or cancelled runs. Today `jobs_fetch_status` is `not_required` for successful runs.
   - **Change, step 2 (only if step 1 shows several runs on the same PR ref):**
     ```yaml
     concurrency:
       group: ci-${{ github.event.pull_request.number || github.ref }}
       cancel-in-progress: ${{ github.event_name == 'pull_request' }}
     ```
     Push CI on `main`, `stable` and `orchestrator/project-*` is never cancelled, which keeps the base-branch results that check-failure triage depends on.
   - **Estimated savings (inference):** 10–25 minutes per PR CI run during bursts.
   - **Risk:** medium. Autofix, judge or triage logic that reads the CI conclusion of an older SHA must treat `cancelled` as superseded, not as failed. Check that before enabling.

2. **Orchestrator poll cycle runs 31–43 minutes**
   - **Evidence:** successful polls 43274104935 (1,841 s), 43284447725 (2,081 s) and 43278389265 (2,590 s).
   - **Root cause:** unknown. The script already emits `ORCH_POLL_CYCLE_SUMMARY elapsed_ms=… sleep_ms=…` (line 142) and `ORCH_POLL_PROJECT_TIMING` (line 90), so part of this may be a deliberate loop with sleeps. Nothing under `scripts/` other than the poller itself reads either prefix.
   - **Change:** parse both prefixes in `scripts/cost_audit.py` / the collector and report `elapsed_ms - sleep_ms` per cycle and per project.
   - **Savings:** none directly; this locates the slow project. **Risk:** low.

3. **Micro:** the skipped AI-phase runs take 0–12 s each and use no runner jobs, so there is no critical-path win there. Their cost is in telemetry and API terms (see below).

## Cost Optimizations

1. **Always deep-dive the AI-phase runs that actually executed.** The success sample rate is 0.07, and it missed every executed implement, plan, clarify and review_autofix run (2, 4, 3 and 3 successes respectively). Change the collector's selection so that at least one non-skipped run per AI family is always downloaded. **Savings:** cost visibility goes from none to a measurable baseline. **Quality risk:** none; this only adds a few log downloads.
2. **Model selection, reasoning level, caching, context-budget warnings:** these cannot be evaluated. `codex_tokens_used=0`, `or_total_tokens=0`, `cache_hit_rate=null`, `context_budget_warn_count=0` and `break_glass_count=0` everywhere reflect missing data, not zero spend.
3. **Semble and Serena:** `semble_query_calls=0`, `semble_query_bytes=0` and `serena_query_calls=0`. There is no evidence either way on whether Semble reduces prompt expansion or whether Serena saves model work. Re-check once item 1 lands.

## Reliability Improvements

1. **CI merge-train step failure** (run 43281039796, `tests-hooks-and-orchestrator`)
   - **Failure rate:** 1 in 11 CI runs (9.1%); 10 successes.
   - **Root cause:** unknown; the log was not available to read.
   - **What the step does today:** `ci.yml` lines 878–967 run 3 pytest shards plus a "rest" group in parallel, then print the last 40 lines of each failing shard.
   - **Diagnostic addition:** emit one line per shard, for example `MERGE_TRAIN_SHARD id=<n> rc=<rc> tests=<count> elapsed_s=<s>`, from the existing reap loop (lines 934–952). This separates a timeout or slow shard from an assertion failure. It runs inside the step's existing shell and fails open.
   - **Expected impact:** faster triage of the recurring flake class. **Rollback:** delete the echo line.
2. **Cancelled-before-first-step runs from concurrency supersession**
   - **Evidence:** 8 `orchestrate_poll` runs (43276114701, 43280033343, 43286145165, 43271250377, 43287678412, 43272664546, 43283033197, 43281604791) and 3 `cancel_on_pr_close` runs (43287424543, 43271197062, 43280623761).
   - **Assessment:** for the poller this is expected `cancel-in-progress: false` behaviour, not a reliability problem. The cancel_on_pr_close cases fit the same pattern, but I did not check that workflow's concurrency group.
   - **Change:** in `scripts/collect_workflow_logs.py`, classify `cancelled_before_first_step` runs in concurrency-grouped workflows as `superseded`, so the 13-run cancelled total is not read as instability.
3. **Semble fallbacks:** 36 total, all with `context=contract-test`, so `semble_runtime_fallbacks=0`. These come from fixtures in `tests/test_semble_helpers.py` (lines 240, 275 and 307 assert the marker) and are classified correctly by `_is_contract_test_semble_line` (`cost_audit.py:381`). This is healthy fail-open test behaviour, not a broken rollout. No action needed.
4. **Serena probes:** `probe_ok`, `probe_failed` and `probe_skipped` are all 0, so availability is unmeasured.

## AI Memory Health

- **Finding:** no `AI_MEMORY_TELEMETRY:` lines were available. No deep-dive logs could be read, and none of the run rows carry a `log_summary`. Retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, `fail_open` and push retries are all unmeasured.
- **Indirect signal:** the poller's actions-runs cache calls `python3 scripts/ai_memory.py actions-runs-cache put` when `_memory_enabled` is true (`orchestrate_poll_process.sh:17749–17755`). Whether memory is enabled in this repo cannot be confirmed from the data.
- **Recommendation:** once executed AI-phase runs are always selected (Cost item 1), grep their logs for `"op":"retrieve"` and report the hit rate. If none appear even in executed runs, check that the emitter is wired.

## GH API Call Audit

No GH API call-count or rate-limit telemetry was present this window. The two hotspots below are grounded in source code.

1. **Poller completed-runs window is crowded out** (`scripts/orchestrate_poll_process.sh:17777`)
   - `_load_actions_runs_cached` makes 3 REST calls per refresh: `in_progress` (with ETag), `queued`, and `completed&per_page=20`.
   - At about 7.4 skipped runs per minute, the 20 completed runs are almost entirely skipped clarify/plan/implement/clarify-respond runs. The review/autofix outcomes the comment says it needs are probably absent (inference).
   - **Change (same call count, §14-compliant):** raise that request to `per_page=100` and drop skipped runs before merging:
     ```bash
     completed_runs="$(gh_retry _safe_gh_jq "repos/${repo}/actions/runs?status=completed&per_page=100" --jq '[.workflow_runs[] | select(.conclusion != "skipped")]' || echo '[]')"
     ```
   - **Diagnostic addition:** log `ACTIONS_RUNS_WINDOW completed_listed=<n> completed_skipped=<k> span_s=<s>` so the crowding can be measured before and after.
   - **Call-count change:** 0. **Rate-limit risk:** unchanged. The payload is larger but the cached blob holds fewer entries.
2. **Collector spends its run budget on skipped runs** (`scripts/collect_workflow_logs.py:697–717`)
   - 903 of 1,000 budgeted runs were not executed runs; 821 of them were skipped AI-phase runs.
   - **Change:** `continue` past `run.get("conclusion") == "skipped"` before the `max_runs` check, and count them in `response_meta["skipped_runs_excluded"]`. This adds a new key alongside the existing ones and renames nothing.
   - **Impact:** about 10× more executed runs per collection. Paging cost depends on `max_pages`; skipped runs are still listed but no longer use the cap.
3. **Source of the fan-out:** every `issue_comment` triggers four reusable-workflow callers. For example, `internal-clarify.yml` gates on `/reclarify` plus author association at job level. Bursts like 19:22:24–19:22:32 produced more than 20 skipped runs. Merging the triggers into one router would be an architectural change, so it is not recommended here. Items 1 and 2 remove the downstream cost.

## Prompt Cache & Memory System

- `cache_hit_rate` is null for every family, and `or_cache_read_tokens` / `or_cache_write_tokens` are 0. There is no evidence of cache creation, reads, hits, misses or fragmentation. `context_budget_warn_count=0` is a coverage artefact, not a sign of headroom.
- **Recommendation:** after Cost item 1, compute the cache-read share per phase from Claude and OpenRouter usage lines. If a prefix varies between runs of the same phase, check the prompt assembly for dynamic content such as timestamps or run IDs placed ahead of the static system block. That is a hypothesis to test, not an observed fact.

## Orchestrator Health

- **Poll cadence:** cron `*/5` with cycles of 31–43 minutes means only one cycle is effectively in flight, and pending runs are replaced (8 of 11 cancelled before first step). Supersession itself is healthy. The real question is cycle length, which needs `ORCH_POLL_CYCLE_SUMMARY` and `ORCH_POLL_PROJECT_TIMING` surfaced in the analyzer.
- **Judge activity:** 3 `unblock_judge_dispatch` runs, all successful, for #9235 (168 s), #9242 (896 s) and #9331 (668 s).
- **Clarify loop:** `orchestrate_clarify_respond` had 1 executed run (43274155350, 524 s) and 205 skipped.
- **Stall-judge input risk:** see GH API item 1.
- **Indicators to track:**
  - `ORCH_POLL_CYCLE_SUMMARY elapsed_ms - sleep_ms` at p50 and p95
  - `projects_processed / projects_seen`
  - superseded-poll count
  - the share of review/autofix runs within the completed-runs window

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence |
|---|---|---|
| Implement / plan / clarify (executed) | Compute, unmeasured | 43278758698 implement 1,683 s; 43278305427 plan 796 s; 43289006584 clarify 485 s |
| Review/autofix | Compute, unmeasured | p50 551 s, p95 750 s, 3 runs |
| CI gate | **Queueing (inference) on top of ≤20-minute jobs** | p50 2,968 s; lint/readiness siblings inflated 24 s → 1,044 s |
| Merge-readiness | Queueing (inference) | integration_pr_readiness p50 517 s, p95 1,015 s |
| Orchestrate | Long cycle plus supersession | 1,841–2,590 s cycles; 8 superseded |
| Failure heal | Mostly skipped | workflow_failure_heal 21/25 and intake 33/39 skipped or other |

Fixes in order of end-to-end impact:
1. Measure, then cut, CI queue time (Speed item 1).
2. Restore the stall judge's view of completed runs (GH API item 1).
3. Make the poll cycle breakdown visible (Speed item 2).

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - CI wall-clock: p50 2,968 s, p95 3,605 s.
  - Orchestrator poll cycle: 1,841–2,590 s.
  - Probable runner-queue spillover into lightweight workflows: lint_pr_body_auto_close p50 691 s.
- **Top failure modes:**
  - CI merge-train step failure in run 43281039796 (cause unknown).
  - 13 cancellations, mostly concurrency supersession and not real failures.
- **Highest-cost drivers:** unknown, because no AI-phase token telemetry was collected. Actions minutes are likely dominated by CI: 11 runs averaging 3,027 s of wall-clock each (inference).
- **Top 3 actions:**
  1. In the collector, exclude skipped runs from `max_runs` and always deep-dive executed AI-phase runs.
  2. Widen the poller's completed-runs window and drop skipped runs, with no extra API calls (`orchestrate_poll_process.sh:17777`).
  3. Collect CI per-job queue time, then add PR-only `concurrency` cancel-in-progress to `ci.yml` if same-ref overlap is confirmed.

## Metrics Appendix

**Run outcomes** (window about 17:36–19:27 UTC, 2026-11-12; `insufficient_data=false`)

| Scope | Runs | Success | Failure | Cancelled | Other/skipped | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| Repo total | 1000 | 83 | 1 (0.1%) | 13 | 903 | 1.0 | 637 |
| ci | 11 | 10 | 1 (9.1%) | 0 | 0 | 2968 | 3605 |
| orchestrate_poll | 11 | 3 | 0 | 8 | 0 | 534 | 2335.5 |
| cancel_on_pr_close | 12 | 9 | 0 | 3 | 0 | 616.5 | 1247 |
| security_audit | 6 | 4 | 0 | 2 | 0 | 628.5 | 1043.75 |
| clarify | 211 | 3 | 0 | 0 | 208 | 1.0 | 11 |
| implement | 208 | 2 | 0 | 0 | 206 | 1.0 | 10.65 |
| plan | 206 | 4 | 0 | 0 | 202 | 1.0 | 10 |
| orchestrate_clarify_respond | 206 | 1 | 0 | 0 | 205 | 1.0 | 10 |
| review_autofix | 3 | 3 | 0 | 0 | 0 | 551 | 749.9 |
| integration_pr_readiness | 11 | 11 | 0 | 0 | 0 | 517 | 1015 |
| lint_pr_body_auto_close | 10 | 10 | 0 | 0 | 0 | 691 | 908.55 |
| workflow_failure_heal_intake | 39 | 6 | 0 | 0 | 33 | 2.0 | 1059.4 |

**Review telemetry**

| Metric | Value |
|---|---|
| cache_hit_rate | null (all families) |
| wall_clock_p50_ms (repo) | 1,000 |
| wall_clock_p99_ms (repo) | 3,557,500 |
| wall_clock samples | 111 |
| wall_clock_p50_ms / p99_ms (ci) | 3,164,000 / 3,634,400 |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |
| runs_with_log_telemetry | 115 |

**Tokens:** codex 0 (0 calls); OpenRouter prompt 0, completion 0, cache read 0, cache write 0 (0 calls); Claude none recorded. These are coverage gaps, not measurements.

**GH API:** no call-count or rate-limit telemetry in the input.

**Semble / Serena**

| Metric | Value |
|---|---|
| semble_query_calls / bytes | 0 / 0 |
| semble_fallbacks (contract-test / runtime) | 36 (36 / 0); 4 per CI run × 9 runs |
| semble_bootstraps / failed / unused / ms | 0 / 0 / 0 / 0 |
| semble_sources_total / static_dup_bytes | 0 / 0 |
| serena_query_calls / response_bytes / tool_calls / ms | 0 / 0 / 0 / 0 |
| serena_fallbacks | 0 |

**Per-target MCP availability**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| (none observed) | 0 | 0 | 0 |

Other MCP servers observed: none.

Sources: none (no web search used).