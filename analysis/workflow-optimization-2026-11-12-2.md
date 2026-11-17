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
## Deep Audit — Workflows & Scripts (2026-11-12)

> Scope note: this is a depth-first pass. It covers `orchestrate_poll.yml`, `cancel_on_pr_close.yml`, `internal-*.yml` callers, `review_autofix_sweep.yml`, the large `implement.yml` steps, the `ci.yml` merge-train step, `lint-pr-body-auto-close.yml`, `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `scripts/review_merge_train.sh`, `scripts/workflow_failure_heal_intake.sh`, and the actions-runs cache in `scripts/orchestrate_poll_process.sh`. Three topics are already in the report above and are not raised again here: collector skipped-run budgeting, CI `concurrency:`, and the poller's `completed&per_page=20` window (GH API Call Audit items 1–2). They are only cross-referenced. No script-injection findings: every untrusted event field checked (`comment.body`, `issue.title/body`, `pull_request.title/body/head.ref`) reaches the shell through `env:`.

### Section 1: Bug & Correctness Sweep

**BUG-001 — Orchestrator poll job has no `timeout-minutes`, so one hung tick can block orchestration for up to 6 h**
- **File:** `.github/workflows/orchestrate_poll.yml:94-102` (job `poll`). The only `timeout-minutes` in the file is at `:2229`, on the `claude_pool_daily_usage` job.
- **Severity:** High
- **Category:** `bug`
- **Description:** The `poll` job uses concurrency group `ai-orchestrate-poll-${{ github.repository }}` with `cancel-in-progress: false` (`:97-102`), and the cron is `*/5` (`internal-orchestrate-poll.yml:5`). With no job or step timeout, the runner default of 360 minutes applies. A wedged gh call or judge call holds the group, and every 5-minute tick queues and is then replaced, which is the "cancelled before first step" pattern in the report. Healthy cycles ran 1,841–2,590 s per the report. So a 6 h hang means about 70 lost ticks with no project dispatch.
- **Recommended fix:** Add `timeout-minutes: 75` to `jobs.poll` (about 1.7× the slowest observed 43-minute cycle). This follows the `ci.yml` and budget-watch convention of timeouts sized from measured runtime (`ci.yml:23-57`). Then add `orchestrate_poll.yml` to whatever budget-watch reads, so a cycle that approaches the cap files an issue.

**BUG-002 — The 304 Not Modified branch of `_load_actions_runs_cached` can never run; each 304 costs 5 calls, 15 s of sleep and a stale fallback**
- **File:** `scripts/orchestrate_poll_process.sh:17718-17766` and `:17806-17821`; test mock at `tests/test_orchestrate_poll_process.py:3172-3192`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** The conditional fetch is `api_cmd=(gh_retry gh api -i ".../actions/runs?status=in_progress&per_page=50")` plus `If-None-Match`. On a 304, gh exits 1 and prints `gh: HTTP 304` to stderr. This was reported against live GitHub by downstream projects, not the official docs: [rjwalters/loom#10755](https://github.com/rjwalters/loom/issues/10755), [marcusquinn/aidevops#33844](https://github.com/marcusquinn/aidevops/issues/33844). Maintainers describe non-zero exit on non-2xx as intended ([cli/cli#9927](https://github.com/cli/cli/issues/9927)).
  - `gh_retry` classifies that stderr as neither permanent (`_is_gh_permanent_failure`, `gh_helpers.sh:228-231`) nor a rate limit (`:140-143`). It retries 5 times with 1+2+4+8 s backoff (`:1276-1283`) and discards each attempt's stdout (`:1250-1254`).
  - `response_file` therefore ends up empty, `status_line` is never set, and the `' 304( |$)'` branch at `:17743` is unreachable.
  - Control falls through to `reason=fetch_unconfirmed` (`:17812`) and serves the stale `cached_runs`. The cache entry's `fetched_at` is not refreshed, so the next cycle repeats the whole sequence.
  - The test mock exits 0 on a 304 (`tests/...:3190-3192`), which hides the defect.
  - Whether this happens often depends on how often the in-progress listing ETag stays stable, and on `_memory_enabled`. [NEEDS VERIFICATION]
- **Recommended fix:** Run the conditional call without `gh_retry` (as `gh_api_retry` does with its own `-i` parsing at `gh_helpers.sh:1093-1103`). Treat `rc!=0` with either an `HTTP/x 304` status line on stdout or `gh: HTTP 304` on stderr, when an ETag was sent, as a 304. Make the mock exit 1 on 304 to match gh.

**BUG-003 — The ETag only covers the in-progress listing, but a cached blob also includes the queued and completed listings**
- **File:** `scripts/orchestrate_poll_process.sh:17743-17765` and `:17771-17790`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** On a 200, the stored blob is `in_progress + queued + completed` (`:17779`, cache put at `:17784-17790`), and its stored ETag is the in-progress one. On a 304 (once BUG-002 is fixed), `:17744-17764` returns `cached_runs` unchanged, so the queued window and the completed window (which the stall judge reads) are as old as the last 200. This compounds the crowding problem in the report's GH API item 1.
- **Recommended fix:** On a 304, still refresh the two cheap listings (`queued`, `completed`, 2 calls) and merge them with the cached in-progress subset, tagged by status. Or store per-status ETags in the `ai_memory.py actions-runs-cache` payload as a new key, keeping the existing `etag` key.

**BUG-004 — The promote-candidate support-ref read is never retried, and a failed read is logged as "unprotected"**
- **File:** `.github/workflows/cancel_on_pr_close.yml:47-59`. The same block appears in `clarify.yml:403-406`, `:2520-2523`; `plan.yml:522-525`, `:3851-3854`; `orchestrate.yml:216-219`, `:390-393`; `check_failure_triage.yml:329-332`; `workflow_failure_heal.yml:115-118`; `orchestrate_clarify_respond.yml:426`; `orchestrate_poll.yml:161`, `:587`; `implement.yml:484`, `:789`, `:1368`, `:7305`.
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - Each block runs `source "${GH_API_RETRY_HELPERS:-/dev/null}"` and then falls back to `gh_api_retry() { ...; gh api "$@"; }`. `GH_API_RETRY_HELPERS` is only exported in `validate.yml:188`, `review_autofix.yml:399` and `security-audit.yml:122`, so in every other workflow the fallback is a bare, unretried `gh api`.
  - A single transient 5xx on `branches/promote-candidate` empties `promote_candidate_sha`, and the step echoes `SUPPORT_REF source=promote_candidate_unprotected` (`cancel_on_pr_close.yml:58`).
  - Support scripts then come from `github.sha` while the workflow body comes from `promote-candidate`, which is a code/script version skew. The log line wrongly blames branch protection.
- **Recommended fix:** In each copy, split the outcome into a read failure and a successful read showing `protected != true`. Emit `SUPPORT_REF source=promote_candidate_read_failed`, then retry once with `sleep 2` before falling back. A clean way to do this across all copies is DUP-001.

**BUG-005 — The implement no-op fall-through can exit under `set -e` before `ai:implementation-failed` is applied**
- **File:** `.github/workflows/implement.yml:5757-5758` and `:5763-5765`; `scripts/label_helpers.sh:216-217`
- **Severity:** Medium
- **Category:** `bug`
- **Description:**
  - The step runs with `set -euo pipefail` (`:5545`). `gh_retry gh api ".../issues/${ISSUE_NUMBER}/comments" -f body=...` has no `|| true`. `gh_retry` treats a comment POST as an unsafe create, so it makes exactly one attempt (`gh_helpers.sh:1216-1218`). A single transient failure aborts the step before the label edit at `:5771-5774`.
  - Likewise, `ensure_label_exists` returns 1 on any non-"already exists" error (`label_helpers.sh:216-217`), and it is called at `:5765` without `|| true`.
  - Inference: the run then ends in the generic failure path (`Comment on issue failure`, `:6939`) instead of the `ai:implementation-failed` re-issue path the orchestrator depends on.
- **Recommended fix:** Apply the label first, and add `|| echo "::warning::..."` to both the comment POST and `ensure_label_exists`. This matches the fail-open pattern already used in the same step at `:5637-5638` and `:5728-5729`.

**BUG-006 — Several other jobs run without `timeout-minutes` (360-minute default)**
- **File:**
  - `.github/workflows/review_autofix.yml`: jobs `gate` `:309`, `post-merge-validate-dispatch` `:784`, `post-merge-force-poll` `:1014`, `deterministic-skip-merge` `:1106`, `claude-fixer-auto-merge` `:1577`, `fingerprint-cap-block` `:1585`. Only `codex-agent` `:1924` has one.
  - `.github/workflows/review_autofix_sweep.yml:114-115` (`sweep`).
  - `.github/workflows/internal-cancel-on-pr-close.yml:19-27` (`heal-pr-reconcile`).
- **Severity:** Medium
- **Category:** `bug`
- **Description:** The `gh_retry` rate-limit waits can each sleep up to 600 s (`_sleep_until_reset` cap, `gh_helpers.sh:236-239`). `review_autofix_sweep.yml` serialises ticks with a workflow-level group (`:109-111`, `cancel-in-progress: false`), so one stuck sweep stalls every later 30-minute tick. The `gate` job of `review_autofix.yml` (≈475 lines) is on every PR's critical path.
- **Recommended fix:** Add job-level timeouts sized like `cancel_on_pr_close.yml:13` (`5`) and `lint-pr-body-auto-close.yml:47`: `gate: 20`, the dispatch and force-poll jobs `10`, `sweep: 30`, `heal-pr-reconcile: 15`. Re-tune from budget-watch data.

**SEC-001 — The clarify job-level gate starts a secrets-inheriting job for non-collaborator comments that begin with a release marker**
- **File:** `.github/workflows/internal-clarify.yml:22-23`, `.github/workflows/clarify.yml:34-35`; author check at `clarify.yml:765-829`
- **Severity:** Low
- **Category:** `security`
- **Description:** The job `if:` explicitly admits `!contains(fromJson('["OWNER","MEMBER","COLLABORATOR"]'), ...author_association)` when the body starts with `"/reclarify\n<!-- ai:pr-dependency-released:"` or `"...partial-delivery-restart:"`. The caller uses `secrets: inherit` (`internal-clarify.yml:26`). The real check (comment author must equal the `GH_PAT` login, fail-closed) only happens inside a run step (`clarify.yml:802-811`). An outside commenter therefore cannot get a release accepted, but can start runners, consume minutes and mint the job's `id-token: write` OIDC token. No attacker-controlled code is executed (inference). [NEEDS VERIFICATION: whether any step before `:740` uses secrets in a way that matters]
- **Recommended fix:** Add a job-level login check alongside the existing ones: `(vars.AI_PIPELINE_RELEASE_LOGIN == '' || github.event.comment.user.login == vars.AI_PIPELINE_RELEASE_LOGIN)`. This is a new variable that defaults to empty, which preserves today's behaviour (§8). Keep the in-step verification as the authority.

### Section 2: GitHub API Call Redundancy Audit

Cross-reference: the poller's `completed&per_page=20` window (`orchestrate_poll_process.sh:17777`) is already covered by the report's GH API Call Audit item 1. BUG-002 and BUG-003 add the conditional-request defects in the same helper.

**API-001 — `gh_api_json_to_file` retries permanent failures (404/422) 5 times**
- **File:** `scripts/gh_helpers.sh:1447-1496`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:**
  - Unlike `gh_retry` (`:1259-1266`) and `gh_retry_to_file` (`:1344-1352`), the non-zero-exit branch (`:1472-1492`) never calls `_is_gh_permanent_failure`. A 404 or 422 is retried with backoff.
  - Callers that expect 404s pay for it: `workflow_failure_heal_intake.sh:627` (`issues/N`), `:646` (`pulls/N` for an autofix report), `:653` (compare against a possibly unknown SHA), `:675`, `:1317`, `:1695`, `:1726`.
  - Other callers already work around the gap with `GH_RETRY_MAX_ATTEMPTS=1` (`:2796`, `:2809`, `:3184`, `:3196`, `:3222`, `:3777`), which is evidence of the inconsistency.
- **Current call count:** 5 calls and 15 s of sleep per permanent failure.
- **Projected:** 1 call.
- **Pattern to extend:** copy the `_is_gh_permanent_failure` early return from `gh_retry_to_file` (`:1344-1352`) into the `else` branch at `:1472`.

**API-002 — Batched GraphQL lookups with a partial error are retried as transient, and the whole batch is dropped**
- **File:** `.github/workflows/cancel_on_pr_close.yml:230-247`; `scripts/review_merge_train.sh:558-565`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** `gh api graphql` exits non-zero when the response contains `errors`, even if `data` is present. [NEEDS VERIFICATION against the pinned gh version] Wrapped in `gh_retry`, a single unresolvable alias (for example `Could not resolve to a PullRequest`) is retried 5 times, and stdout is discarded. The batch of up to 50 PRs (`cancel_on_pr_close`) is then "preserved" wholesale, or every PR in `fetch_nums` is marked `_MT_META_FAILED` (`review_merge_train.sh:561-563`). The per-alias tolerance written into both files (`cancel_on_pr_close.yml:245-251`, `review_merge_train.sh:566-582`) never takes effect (see DEAD-001).
- **Current call count:** 5 GraphQL calls per bad batch.
- **Projected:** 1 call, with per-alias handling.
- **Pattern to extend:** add a helper in `gh_helpers.sh` (proposed name `gh_graphql_partial_to_file`; check it for collisions per §10). It runs `gh api graphql` once per attempt, keeps stdout when `rc!=0`, and returns success if `.data.repository | type == "object"`. It retries only on rate-limit or 5xx stderr. Then reuse the existing alias-batching contracts in `_cpr_fetch_pr_state_batches` (`cancel_on_pr_close.yml:198-263`).

**API-003 — `GET /user` can run twice in one heal-intake run**
- **File:** `scripts/workflow_failure_heal_intake.sh:1303-1306` and `:2073-2079` (`:2249-2253` already reuses the earlier value)
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** `PROVENANCE_LOGIN` is set from `gh api user` at `:1305` for non-`workflow_run` sources. When `PHASE_COMMENT_AUTHOR` is empty, `HEAL_TRUSTED_AUTHOR` at `:2078` makes a second `gh api user` call with the same token. The comment at `:2076-2077` acknowledges the other read but does not reuse it. The recovery path at `:2249-2250` shows the intended reuse.
- **Current call count:** 2 calls.
- **Projected:** 1 call.
- **Fix:** `HEAL_TRUSTED_AUTHOR="${PHASE_COMMENT_AUTHOR:-${PROVENANCE_LOGIN:-}}"` before the fallback call at `:2078`.

**API-004 — The review sweep makes one REST call per smoke PR inside the PR loop**
- **File:** `.github/workflows/review_autofix_sweep.yml:476-526` (call at `:517`)
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** For every pipeline-authored `e2e-smoke-test` PR on an `ai/issue-<N>` head, `gh api "repos/${REPOSITORY}/issues/${smoke_issue_number}" --jq '.state'` runs inside `for row in "${rows[@]}"`. The report cites 7 such PRs in one incident (`:80-81`). Per §15 this is a per-iteration call in a loop.
- **Current call count:** N calls for N smoke PRs.
- **Projected:** ceil(N/50) calls.
- **Pattern to extend:** before the loop, collect the smoke issue numbers from `prs_json` with `jq` and resolve them with one aliased GraphQL query (`issue(number:N){state}`). This is the `_cpr_fetch_pr_state_batches` shape (`cancel_on_pr_close.yml:203-263`) or `_fetch_candidate_issue_details_graphql` in `orchestrate_poll_process.sh`.

**API-005 — The ancestor no-op walk makes two REST calls per hop**
- **File:** `.github/workflows/implement.yml:5690-5703`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** Each hop fetches `issues/${current}` (body) and then paginates `issues/${parent_num}/comments`. The chain is sequential, so the hops cannot be merged, but each hop's two reads can be. The default threshold is 2 (`:5686`).
- **Current call count:** 2×threshold (4) or more, when comments paginate.
- **Projected:** threshold (2), with one GraphQL `issue(number:){body comments(last:100){nodes{body}}}` per hop. Falling back to the REST path on failure keeps it fail-open.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001 — The "Resolve workflow support ref" block is copied about 25 times**
- **File:**
  - `clarify.yml:388`, `:2506`
  - `plan.yml:507`, `:3837`
  - `orchestrate.yml:202`, `:376`
  - `implement.yml:469`, `:772`, `:1353`, `:7291`
  - `orchestrate_poll.yml:147`, `:572`
  - `validate.yml:225`, `:493`, `:1925`
  - `cancel_on_pr_close.yml:32`
  - `check_failure_triage.yml:315`
  - `workflow_failure_heal.yml:101`
  - `orchestrate_clarify_respond.yml:411`
  - `memory_maintenance.yml:16`
  - `sync_ai_labels.yml:37`
  - `issue_pr_status.yml:1274`
  - `review_autofix.yml:448`, `:2180`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** The copies differ only in the workflow file name inside the `^shubhodeep1/coding-workflows/\.github/workflows/<file>\.yml@refs/heads/promote-candidate$` regex. BUG-004 has to be fixed in every copy. The block runs before any support checkout, so it cannot simply source a `scripts/` file.
- **Recommended fix:** Add a single canonical template and a generator check rather than a composite action. A consumer-side `uses: ./.github/actions/...` would not resolve in consumer repos, and a remote action would need its own ref pin. Concretely:
  - Add `scripts/render_support_ref_block.py --workflow <file>`, which prints the canonical `run:` body.
  - Add a `static-checks` step that diffs every copy against it, following the existing "drift check" in `ci.yml` static checks (`ci.yml:28-29`).
  - Fix BUG-004 once in the template.

**DUP-002 — `gh_helpers.sh` has four retry loops whose failure classification has drifted apart**
- **File:** `scripts/gh_helpers.sh:1050-1208` (`gh_api_retry`), `:1209-1298` (`gh_retry`), `:1309-1382` (`gh_retry_to_file`), `:1431-1505` (`gh_api_json_to_file`)
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** The rate-limit branch (`_gh_ratelimit_tg_alert`, `_gh_rate_limit_trip_breaker`, `_gh_rate_limit_wait_for`) and the `2 ** (attempt - 1)` backoff are copied in three of the functions. The copies have already diverged: API-001 (the permanent-failure check is missing in one) is a direct result.
- **Recommended fix:** Add a private helper, proposed signature `_gh_retry_after_failure <attempt> <max_attempts> <stderr_file> <cmd...>`. It returns 0 to retry, 1 to stop on a permanent failure, or 2 when attempts are exhausted. It owns the permanent, rate-limit and backoff branches. The three loops keep their own stdout handling (buffered replay, file truncation, JSON validation) and call it. Public names and behaviour stay unchanged (§10). Existing tests: `tests/test_gh_retry_*.py`, `tests/test_gh_api_retry.py`.

**DUP-003 — Inline label swaps with a fallback edit instead of `set_issue_phase_label_resilient`**
- **File:** `.github/workflows/implement.yml:5584-5590`, `:5632-5636`, `:5719-5725`, `:5771-5774`, `:6964-6966`; helper at `scripts/label_helpers.sh:227-316`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** The same pattern, `gh issue edit --remove-label … --add-label X || gh issue edit --add-label X || true`, appears five times. The helper already does an additive POST plus a DELETE of stale phase labels, and reports `SET_ISSUE_PHASE_LABEL_RESILIENT_OUTCOME`.
- **Recommended fix:** Source `label_helpers.sh` (already done conditionally at `:5763-5764`) and call `set_issue_phase_label_resilient "${ISSUE_NUMBER}" 'ai:closed' "${REPO}"`. Keep the inline path only as the fallback when the helper is missing.

**DUP-004 — The `[skip ai]` awk rule is duplicated, and its comment points at the wrong file**
- **File:** `.github/workflows/review_autofix_sweep.yml:282-287`; `scripts/review_autofix_step_evaluate_gate.sh:28`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** The comment says "Identical to the gate's copy in review_autofix.yml", but the gate copy now lives in `scripts/review_autofix_step_evaluate_gate.sh:28`. The sweep already checks out and sources `scripts/gh_helpers.sh` (`:224-225`).
- **Recommended fix:** Move the program to `scripts/skip_ai_marker.awk` and run `awk -f` from both callers. Keep `tests/test_skip_ai_marker_rule.py` running against that one file.

**DUP-005 — The clarify trigger gate is duplicated in the caller and the callee**
- **File:** `.github/workflows/internal-clarify.yml:17-24`; `.github/workflows/clarify.yml:29-36`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** The same ~2,300-character expression (inference from line widths) is maintained twice. Any change to the label exclusions, for example adding a new tracker label, has to land in both.
- **Recommended fix:** Keep the full gate in `clarify.yml` (the reusable workflow consumers call). Reduce `internal-clarify.yml` to the cheap pre-filter (`event_name` / `action` / `pull_request == null`), or add a parity test like the ones in `tests/` for other duplicated gates.

### Section 4: Expression Size Limit Risk Assessment

The repo's own guard (`tests/test_workflow_file_size_limit.py:28-98`) fails CI for any `run:` that contains `${{` and is 18,000 characters or longer. With CI green, there are no High (≥18,000) blocks. All character counts below are estimates from line counts and visible long lines. They were not measured, because this sandbox cannot run `wc`.

**EXPR-001 — implement.yml "Handle no-op implementation" is close to the Medium band**
- **File:** `.github/workflows/implement.yml:5544-5774`
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** A 231-line `run:` body that contains `${{ github.server_url }}`, `${{ github.repository }}` and `${{ github.run_id }}` (`:5549`, `:5551`, `:5757`, `:5765`, `:5767`, `:5771`, `:5773`), so the whole body counts as one expression. It holds several long prose templates (`:5574`, `:5576`, `:5612`, `:5617`, `:5624`, `:5711`, `:5713`, each ~240–330 characters). Estimated ~13,000–15,500 characters, leaving ~5,500–8,000 of headroom. [NEEDS VERIFICATION: measure with the guard test's `len(run)`]
- **Recommended fix:** Replace every `${{ github.repository }}` with `${GITHUB_REPOSITORY}`, and `RUN_URL` with `${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}`. These are default runner env vars, so the body no longer contains any `${{ }}` and drops out of the limit entirely. Optionally move the four comment templates to `prompts/` or a `scripts/implement_handle_noop.sh` (§24 extraction pattern).

**EXPR-002 — Other implement.yml bodies are interpolated only for `github.repository`**
- **File:** `.github/workflows/implement.yml:6213-6384` ("Create Pull Request", `${{` at `:6241`, `:6267`, `:6303`, `:6351`, `:6356`); `:6943-7015` ("Comment on issue failure", `:6947`, `:6961`, `:6964`, …)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** Estimated ~8,000–10,000 and ~4,000–5,000 characters (inference), leaving 11,000 or more of headroom. Both carry only context values that exist as default env vars.
- **Recommended fix:** Make the same `${GITHUB_REPOSITORY}` / `${GITHUB_SERVER_URL}` / `${GITHUB_RUN_ID}` substitution as in EXPR-001, so future edits cannot reach the limit.

**EXPR-003 — The guard does not check `if:` or `with:` expressions**
- **File:** `tests/test_workflow_file_size_limit.py:66-98`; examples at `.github/workflows/clarify.yml:29-36`, `internal-clarify.yml:17-24`, `internal-implement.yml:16-46`
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** Only `step["run"]` is measured. Job and step `if:` strings and `with:` values are also single expressions. The largest seen is the clarify gate: ~2,300 characters, with `clarify.yml:32` alone ~1,100. That leaves ~18,700 of headroom, so the risk is low today, but these gates grow by one `!contains(...)` clause per new tracker label.
- **Recommended fix:** Extend the test to walk `jobs.*.if`, `jobs.*.steps[*].if` and string `with:` values containing `${{`, using the same `RUN_EXPRESSION_GUARD_CHARS`.

**Workflow file size:** no workflow can exceed 800 KB. CI fails at 480,000 bytes (`tests/test_workflow_file_size_limit.py:27`), and the repo's documented hard limit is 512,000 bytes (CLAUDE.md §27), which is stricter than the 1 MB this audit was asked to use. The largest files by line count are `implement.yml` (7,362), `test-and-mark-stable.yml` (6,963) and `review_autofix.yml` (6,012). Byte sizes were not measured.

### Section 5: Cross-Cutting Concerns

**DEAD-001 — Partial-GraphQL-error handling code is effectively unreachable**
- **File:** `.github/workflows/cancel_on_pr_close.yml:245-247`; `scripts/review_merge_train.sh:579-581`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** Both branches run only when `gh api graphql` succeeds with `errors[]` present. Per API-002, gh exits non-zero in that case and `gh_retry` drops stdout, so control goes to the whole-batch failure path at `:235-236` / `:560-564`. [NEEDS VERIFICATION]
- **Recommended fix:** Make the branches reachable through the API-002 helper rather than deleting them.

**CONSIST-001 — Inline `gh label create` colours and descriptions contradict the label contract**
- **File:** `.github/workflows/implement.yml:5581-5582`, `:5716-5717` (`ai:closed` `6b7280`, "Issue closed by automation…"); `:5629-5630` (`ai:needs-human` `b60205`, "Automation halted…"); `:6961-6962` (`ai:blocked` "Blocked: human input required")
- **Severity:** Medium
- **Category:** `consistency`
- **Description:** The contract (`.github/ai/label_contract.v1.json:100-102`, `:156-158`) and the mirror (`scripts/label_helpers.sh:48`, `:62`, `:115`, `:129`, `:117`) define `ai:needs-human` as `e11d48` "Escalated for human intervention…", `ai:closed` as `6a737d` "Linked PR closed without merge", and `ai:blocked` as "Orchestrator auto-answer loop blocked…". The label is created only when absent, so whichever path runs first in a repo fixes the colour and description there. `scripts/implement_handle_guard_block.sh:98-99` uses the contract values, so drift is per-repo and depends on order.
- **Recommended fix:** Replace the inline creates with `ensure_label_exists "<label>" "${REPO}" || true` after sourcing `label_helpers.sh`. Where the helper may be missing, use the contract literals.

**CONSIST-002 — `AUTOFIX_SWEEP_END` log lines have different field sets**
- **File:** `.github/workflows/review_autofix_sweep.yml:299`, `:313`, `:633`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** `:313` omits `skipped_merge_queued`. `:299` and `:313` omit `snapshot_incomplete`. Only `:633` has the full set. Log parsers (the collector or `cost_audit.py`) see missing keys on early exits.
- **Recommended fix:** Emit all fields in all three places, using `snapshot_incomplete=false` on the early exits.

**CONSIST-003 — The sweep mixes raw `gh` calls with retried ones**
- **File:** `.github/workflows/review_autofix_sweep.yml:236`, `:310`, `:505`, `:517`, `:532-535`, `:624`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** The active-run snapshot uses `gh_api_retry` (`:366`). The PR listing (`:236`, which aborts the tick under `set -e`), the default-branch read, the issue-state read, the PR close PATCH and `gh workflow run` do not. A transient 5xx on `:236` drops the whole 30-minute tick.
- **Recommended fix:** Wrap the reads and the idempotent PATCH in `gh_api_retry --idempotent`. Leave the comment POST and `gh workflow run` as single attempts (non-idempotent creates, §6634 rule), but log their failure reasons.

**CONSIST-004 — The sweep header's cadence claim is out of date**
- **File:** `.github/workflows/review_autofix_sweep.yml:59-60`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** The header says "30 min cadence matches the existing orchestrator poll cadence", but the poller cron is `*/5` (`internal-orchestrate-poll.yml:5`).
- **Recommended fix:** Correct the comment.

**SHELL-001 — Mixed tab and space indentation inside one function**
- **File:** `scripts/orchestrate_poll_process.sh:17699-17712` (tabs), `:17714-17721` (spaces), `:17768-17791` (tabs, with tab+space continuation at `:17788-17789`)
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** This breaks CLAUDE.md §9 (tabs) within `_load_actions_runs_cached`, and makes the `if`/`fi` nesting at `:17768-17796` hard to read during review.
- **Recommended fix:** Re-indent the function with tabs when BUG-002 and BUG-003 are fixed. No other changes.

**SHELL-002 — Useless use of `cat` (SC2002)**
- **File:** `.github/workflows/review_autofix_sweep.yml:376`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** `cat "${snap_raw}" | jq -c -s …`
- **Recommended fix:** `jq -c -s … "${snap_raw}"`.

**DEBT-001 — Line-number cross-references in comments have gone stale**
- **File:** `.github/workflows/review_autofix_sweep.yml:574` (cites `orchestrate_poll_process.sh:7792-7814`) and `:611` (cites `:7862-7868`)
- **Severity:** Low
- **Category:** `tech-debt`
- **Description:** Those lines now hold security-pass reissue code (`orchestrate_poll_process.sh:7790-7815`). The referenced logic is `_has_active_autofix_run` (`:26811`) and `_dispatch_review_for_conflicts` (`:27211`).
- **Recommended fix:** Cite function names instead of line numbers.

**DEBT-002 — `orchestrate_poll_process.sh` is a single file of more than 27,000 lines**
- **File:** `scripts/orchestrate_poll_process.sh` (functions defined at `:26811` and `:27211`)
- **Severity:** Low
- **Category:** `tech-debt`
- **Description:** Line references rot (DEBT-001, and the report itself cites `:17777`), and helper families such as the actions-runs cache (`:17660-17822`) cannot be tested on their own without the large mock harness (`tests/test_orchestrate_poll_process.py`).
- **Recommended fix:** Extract self-contained helper families into sourced modules, starting with the actions-runs cache as `scripts/orchestrate_actions_runs_cache.sh` and its tests. Function names stay the same (§10), and the new module goes in `REQUIRED_BOOTSTRAP_SCRIPTS`.

**TODO/FIXME/HACK markers:** none found in `scripts/` or `.github/workflows/`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 10 | BUG-002, BUG-003, BUG-004, BUG-005, BUG-006, API-001, DUP-001, DUP-002, EXPR-001, CONSIST-001 |
| Low | 18 | SEC-001, API-002, API-003, API-004, API-005, DUP-003, DUP-004, DUP-005, EXPR-002, EXPR-003, DEAD-001, CONSIST-002, CONSIST-003, CONSIST-004, SHELL-001, SHELL-002, DEBT-001, DEBT-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `.github/workflows/orchestrate_poll.yml` | Small |
| API call optimization | `scripts/gh_helpers.sh`, `.github/workflows/cancel_on_pr_close.yml`, `scripts/review_merge_train.sh`, `scripts/workflow_failure_heal_intake.sh`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/implement.yml` | Medium |
| Code modularization | 13 workflows carrying the support-ref block, `scripts/gh_helpers.sh`, `scripts/label_helpers.sh` callers in `.github/workflows/implement.yml`, `scripts/review_autofix_step_evaluate_gate.sh`, `.github/workflows/internal-clarify.yml`, `.github/workflows/clarify.yml`, new render script plus CI check | Large |
| Expression size reduction | `.github/workflows/implement.yml`, `tests/test_workflow_file_size_limit.py` | Small |
| Medium/Low fixes | `scripts/orchestrate_poll_process.sh`, `tests/test_orchestrate_poll_process.py`, `.github/workflows/implement.yml`, `.github/workflows/review_autofix.yml`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/internal-cancel-on-pr-close.yml`, `.github/workflows/cancel_on_pr_close.yml` (and the BUG-004 copies), `.github/workflows/clarify.yml`, `.github/workflows/internal-clarify.yml` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-11-12)

### Safety Tag Legend
- **`SAFE_TO_MERGE`** means every merge precondition was proven by reading the code: same endpoint and auth scope, same function or step, no mutation in between, unchanged error and retry behaviour, no concurrency boundary crossed, and no cache contract broken. The implement stage may act on these without further review.
- **`NEEDS_VERIFICATION`** means the change is probably safe, but at least one precondition can't be proven from static reading. The listed checks must pass first.
- **`RISKY_SKIP`** means at least one hard trigger applies: a retry or stall-recovery path, an auth or identity probe, pagination, a rate-limit-sensitive path, `orchestrate_poll_process.sh`, race-defending code, or a change to log keys. These are listed for visibility only and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

**MERGE-001: the merge-train release loop lists the same PR's comments twice for each released PR** — `RISKY_SKIP`
- **Files:**
  - `scripts/review_merge_train.sh:1662` (queue-marker lookup through `_mt_find_marker_comment_id`)
  - `:1685-1686` (released-marker upsert through `_mt_upsert_comment` with no 4th argument, so `:910` runs the lookup again)
  - The listing itself is `_mt_find_marker_comment` at `:836-837`.
- **Endpoint:** `GET /repos/{owner}/{repo}/issues/{pr}/comments?per_page=100`, called with `--paginate`.
- **Current call count:** 2×P per released PR, where P = ceil(comments/100). **Proposed:** P.
- **Evidence:** both lookups fetch every comment on the same PR and filter locally by marker prefix. Only the prefix differs: `MT_MARKER="<!-- merge-train:queued -->"` vs `MT_RELEASED_MARKER="<!-- merge-train:released -->"` (`:146-147`).
  ```bash
  if ! _mt_resolve_automation_login || ! release_queue_comment_id="$(_mt_find_marker_comment_id "${num}" "${MT_MARKER}")"; then
  ...
  _mt_upsert_comment "${num}" "${MT_RELEASED_MARKER}" "${release_comment_body}" \
  ```
  Three mutations happen between the two lookups:
  - the queue-marker PATCH to `MT_RETIRED_MARKER` (`:1668`);
  - the label DELETE claim (`:1677`);
  - the dispatch (`:1682`).

  None of them creates or removes a `merge-train:released` comment, so one listing that returns both markers' latest "id created_at" would be equivalent for our own writes. The file's API-budget header (`:105-109`) counts one comments-list per unblocked queued PR plus a "best-effort released-comment upsert", but it doesn't count the second list inside that upsert.
- **Proposed fix:** add a sibling of `_mt_find_marker_comment` that runs the `:836` listing once and returns one line per marker. In the release loop, pass the released-marker id to `_mt_upsert_comment` as its existing optional 4th argument. Leave `_mt_find_marker_comment`'s output format unchanged for its other callers (`:975`, `:994`).
- **Safety rationale:** two hard triggers apply. The listing is a `--paginate` call, and the loop explicitly defends against concurrent release runs ("Concurrent release invocations cannot both claim it", `:1674-1676`).
- **Downstream signal:** manual review only. A reviewer must confirm that a concurrent peer can't write a released marker between the merged read (before the claim) and the upsert (after the claim). They must also update the API-budget comment and the merge-train tests together.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001: `_mt_upsert_comment` GETs the comment body that the marker listing just iterated over** — `RISKY_SKIP`
- **Files:** `scripts/review_merge_train.sh:914` (single-comment GET) and `:836-837` (the listing it follows, reached through `:910`, or through the caller's own lookup when `$4` is passed: gate at `:994`/`:1029`, release at `:1685`).
- **Endpoint:** `GET /repos/{owner}/{repo}/issues/comments/{id}`.
- **Current call count:** 1 extra GET per upsert that finds an existing marker. **Proposed:** 0.
- **Evidence:**
  ```bash
  existing_body="$(gh_retry gh api "repos/${MT_REPO}/issues/comments/${existing_id}" --jq '.body' 2>/dev/null || true)"
  if [ "${existing_body}" = "${body}" ]; then
  ```
  The listing's `--jq` already reads `.body` to apply `startswith(marker)` but only emits `"\(.id) \(.created_at)"`.
- **Proposed fix:** have the marker listing (or a new variant of it) also emit the matched body, for example as base64 to stay newline-safe. Compare against that in `_mt_upsert_comment` and keep the `:914` GET only as a fallback when no body was captured.
- **Safety rationale:** the data would come from a `--paginate` call (pagination trigger). The `|| true` failure path ("unknown body, so PATCH anyway") must also be preserved exactly.
- **Downstream signal:** manual review only. Ship it together with MERGE-001, since both change the same listing helper. Check that a body captured before the `:1668` PATCH can't be the one compared at `:915`; today it is a different marker, so it shouldn't be.

**REUSE-002: the review sweep reads `repos/{repo}.default_branch` while every PR in its own snapshot carries it** — `RISKY_SKIP`
- **Files:**
  - `.github/workflows/review_autofix_sweep.yml:310` (the read)
  - `:236-247` (the PR snapshot, same step, same `READ_TOKEN`)
- **Endpoint:** `GET /repos/{owner}/{repo}`.
- **Current call count:** 1 per tick that has at least one candidate. **Proposed:** 0.
- **Evidence:**
  ```bash
  sweep_default_branch="$(GH_TOKEN="${READ_TOKEN}" gh api "repos/${REPOSITORY}" --jq .default_branch 2>/dev/null || true)"
  ```
  - The read only runs when `total > 0` (the early exit is at `:298-301`), so `prs_json` holds at least one PR whose `base.repo` is this repository.
  - The `:238-247` projection drops `.base.repo.default_branch`.
  - Two zero-call alternatives are unreliable:
    - Reading `$GITHUB_EVENT_PATH`, as `workflow_failure_heal_intake.sh:1293-1296` does, may fail on `schedule` ticks. Public reports say the cron payload has no `repository` object ([arduino/arduino-cli#1507](https://github.com/arduino/arduino-cli/pull/1507), [community discussion #54817](https://github.com/orgs/community/discussions/54817)), though a Gitea PR claims the opposite for GitHub ([go-gitea/gitea#38446](https://github.com/go-gitea/gitea/pull/38446)).
    - The step's own checkout already falls back to `github.ref_name` for the same reason (`:152`).
- **Proposed fix:** add `base_default_branch: (.base.repo.default_branch // "")` to the `:238` projection. Set `sweep_default_branch` from the first non-empty value, and fall back to the existing `:310` call when it is empty. The name `base_default_branch` is unused in the repo.
- **Safety rationale:** the data would come from a `--paginate` listing (pagination trigger). The value also feeds the issue #6629 provenance check (`:303-309`, "no fallback: a failed read skips every dispatch"), which defends against forged run names.
- **Downstream signal:** manual review only. A reviewer must confirm three things:
  - the list-pulls `base.repo` object includes `default_branch`;
  - `AUTOFIX_SWEEP_SKIPPED reason=default_branch_unavailable` behaviour is unchanged when every value is empty;
  - no test pins the call at `:310`.

**REUSE-003: the per-project poller loop re-reads the default branch after `DEFAULT_BRANCH_TRACKING` is already set** — `RISKY_SKIP`
- **Files:** `scripts/orchestrate_poll_process.sh`, all inside the per-project `for ((tidx=0; tidx<COUNT; tidx++))` loop (`:27636`–`:34607`):
  - `:27840` resets `DEFAULT_BRANCH_TRACKING=""`
  - `:28530-28532` fills it on every iteration that reaches the sync block
  - `:28576` (`FINAL_DEFAULT_BRANCH`, `merge_conflict` branch) and `:33627` (`DEFAULT_BRANCH`, before the check-runs read at `:33628`) re-read it
  - `:33984` already uses the intended pattern: `FINAL_DEFAULT_BRANCH="${DEFAULT_BRANCH_TRACKING:-$(gh_retry _safe_gh_jq ...)}"`
- **Endpoint:** `GET /repos/{owner}/{repo}`.
- **Current call count:** up to 2 per project per cycle. **Proposed:** 0 when `DEFAULT_BRANCH_TRACKING` is set.
- **Evidence:**
  ```bash
  if [ -z "${DEFAULT_BRANCH_TRACKING}" ]; then
    DEFAULT_BRANCH_TRACKING="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  fi
  ...
  FINAL_DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"
  ```
  - Both sites use the same `|| echo "main"` fallback.
  - The file already has a per-process memo, `_review_run_default_branch` (`:26882-26934`), but it fails closed rather than falling back to `"main"`, so the two can't simply be unified.
  - About 20 `.default_branch` reads exist in the file (for example `:4263`, `:5580`, `:6099`, `:17335`, `:34794`).
- **Proposed fix:** at `:28576` and `:33627`, use `${DEFAULT_BRANCH_TRACKING:-$(gh_retry _safe_gh_jq ...)}`, mirroring `:33984`.
- **Safety rationale:** the code is in `orchestrate_poll_process.sh` (hard trigger). A `"main"` fallback cached by `:28531` after a transient failure would also stick for the rest of the iteration instead of being retried.
- **Downstream signal:** manual review only. A reviewer must confirm that no code between `:28531` and `:33627` changes `DEFAULT_BRANCH_TRACKING` to a non-default value; `:28364` sets it from `EXTFIN_DEFAULT_BRANCH`. They must also accept the sticky-fallback trade-off before editing the poller.

**REUSE-004: clarify reads `/user` twice in one job for release-marker and capacity-replay comments** — `NEEDS_VERIFICATION`
- **Files:**
  - `.github/workflows/clarify.yml:804` (step "Decide clarify route", where `_rel_login` is already reused within the step at `:859-861`)
  - `:1107` (step "Set clarification phase label")
  - Both steps run with `GH_TOKEN: ${{ secrets.GH_PAT }}` (`:719`, `:1086`) in the same `clarify` job.
- **Endpoint:** `GET /user`.
- **Current call count:** 2 on runs whose comment is a PR-dependency release marker or a capacity-wait replay. **Proposed:** 1.
- **Evidence:**
  ```bash
  if _rel_login="$(source scripts/gh_helpers.sh && gh_retry gh api user --jq .login)" \
  ...
  hold_release_login="$(gh api user --jq .login 2>/dev/null || true)"
  ```
  The §14 comment at `:796-798` says no earlier call in the job returns the token's identity, but it doesn't account for the later read at `:1107`.
- **Proposed fix:** when `_rel_login` matches the login regex at `:805`/`:862`, write `CLARIFY_ROUTE_PIPELINE_LOGIN=<login>` to `$GITHUB_ENV`. The name is new and has no collisions. At `:1107`, use `hold_release_login="${CLARIFY_ROUTE_PIPELINE_LOGIN:-$(gh api user --jq .login 2>/dev/null || true)}"`.
- **Safety rationale:** it is the same endpoint and token, but it crosses a step boundary. The login also gates which hold markers get released, so this is an identity probe.
- **Downstream signal:** before implementing, verify that:
  - no step between `:715` and `:1083` changes `GH_TOKEN` or runs under a different token;
  - writing the login to `$GITHUB_ENV` is acceptable; it is a public login, not a secret;
  - `:1107` keeps its current fail-open behaviour when the env value is empty.

**REUSE-005: the first hop of the implement no-op ancestry walk re-fetches the current issue already cached in `ISSUE_META_FILE`** — `NEEDS_VERIFICATION`
- **Files:**
  - `.github/workflows/implement.yml:5691` (hop 1 runs with `current="${ISSUE_NUMBER}"`, set at `:5689`)
  - The cache is written at `:1030-1031` and exported through `GITHUB_ENV` at `:325-326`.
- **Endpoint:** `GET /repos/{owner}/{repo}/issues/{ISSUE_NUMBER}`.
- **Current call count:** 1 on hop 1. **Proposed:** 0 when the cache matches.
- **Evidence:**
  ```bash
  parent_body="$(gh_retry gh api "repos/${REPO}/issues/${current}" --jq '.body // ""' 2>/dev/null || echo "")"
  ```
  The step only needs the `Re-issued from #N` line (`:5692`), which the re-issue flow writes when it creates the issue.
- **Proposed fix:** on hop 1 only, if `jq -e --arg n "${ISSUE_NUMBER}" '(.number|tostring)==$n' "${ISSUE_META_FILE}"` succeeds, read `.body // ""` from the file. Otherwise fall back to the existing call.
- **Safety rationale:** it isn't paginated, isn't in the poller, and isn't a retry or stall path. But the source was written in an earlier step, and the metadata can be emptied on mismatch (`:1002-1003`, `:1021-1022`), so equivalence isn't proven.
- **Downstream signal:** before implementing, verify that no step between `:1031` and `:5536` rewrites the issue body in a way that could change the `Re-issued from #N` line, and that `ISSUE_META_FILE` is still present in this step's environment.

### Dead Calls (DEAD-API-###)
No findings. The candidates I checked all have their results used:
- the sweep's `/user` read at `:505`, used at `:516`;
- the merge-train `/user` read at `:824`, memoized in `MT_AUTOMATION_LOGIN`;
- the heal-report `/user` reads at `:172` and `:210`, memoized;
- the intake's provenance comments at `:1308`, consumed at `:1324` and `:1827`.

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP — this changes the retry loop of the shared `gh_api_json_to_file` helper. Some callers may rely on retrying a 404 while GitHub catches up, for example the run GET at `workflow_failure_heal_intake.sh:1317` and the compare against a just-pushed SHA at `:653`. Each caller needs a review before 404/422 stops being retried.
- API-002: RISKY_SKIP — this changes the retry classification for batched GraphQL in `review_merge_train.sh:558` and `cancel_on_pr_close.yml:230`. It also depends on how the pinned gh version exits on partial `errors[]`, which is unconfirmed.
- API-003: SAFE_TO_MERGE — new detail:
  - `workflow_failure_heal_intake.sh` never reassigns `GH_TOKEN`, so the reads at `:1305` and `:2078` use the same token through the same `gh_retry`.
  - The file already does this reuse at `:2249-2250`.
  - The duplicate only happens for `SOURCE_KIND` values `issue`, `pull_request` and `autofix_failure`, because `phase_failure` already reuses the login at `:1830`.
  - The fallback call must stay for `workflow_run` and for non-self `phase_failure`, where `PROVENANCE_LOGIN` is empty.
- API-004: NEEDS_VERIFICATION — new detail:
  - The per-PR read at `review_autofix_sweep.yml:517` only runs after the label check (`:503`), login resolution (`:504-512`) and author match (`:516`). A batch query must apply the same pre-filter, or it will read issues for smoke PRs the pipeline didn't author.
  - A missing or errored alias must map to an empty state (no close), matching the current `|| true`.
  - The batch must use `READ_TOKEN`.
- API-005: RISKY_SKIP — correction to the proposed fix:
  - Each hop reads the body of `current` (`implement.yml:5691`) and the comments of a different issue, `parent_num` (`:5695`). The pair that can actually be merged is hop k's comments(`parent_num`) with hop k+1's body(`parent_num`).
  - Replacing the REST `--paginate` comment listing with GraphQL `comments(last:100)` would drop no-op markers beyond 100 comments, which is the pagination trigger.
  - The safe part of this is REUSE-005.

### Summary Counts
These counts cover both the new findings above and the Deep Audit IDs tagged in the cross-references.

| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 1 | API-003 |
| NEEDS_VERIFICATION | 3 | REUSE-004, REUSE-005, API-004 |
| RISKY_SKIP | 7 | MERGE-001, REUSE-001, REUSE-002, REUSE-003, API-001, API-002, API-005 |

### Implement-Stage Handoff
- API-003: in `scripts/workflow_failure_heal_intake.sh:2074`, set `HEAL_TRUSTED_AUTHOR="${PHASE_COMMENT_AUTHOR:-${PROVENANCE_LOGIN:-}}"`. Keep the existing `gh_retry gh api --method GET user` fallback at `:2075-2079` unchanged for when the value is empty. Update the §14 comment at `:2076-2077` to say the provenance read is reused.