## Executive Summary

The run-logs directory named in the input (`/home/runner/work/_temp/workflow-log-output`) does not exist in this sandbox, and no `log_excerpts` or `log_summary` fields were supplied. Everything below comes from the `analysis_context` run rows plus the workflow and script source in the repo. Any claim about a root cause inside a step's log is marked unverified.

- **Runners were saturated between about 23:50 and 00:45 UTC. Queue wait, not compute, set the wall-clock time.** Several runs lasted far longer than their own job timeouts allow:
  - `lint-pr-body-auto-close.yml` has one job with `timeout-minutes: 5`, yet run 43336849873 took 654s. That is at least 354s of queueing.
  - `integration-pr-readiness.yml` is also one job with a 5-minute timeout. Run 43336388214 took 815s, so at least 515s was queueing.
  - In CI, all 6 test/static jobs run in parallel, each capped at 20 minutes, and `lint` adds at most 5 more. That puts a ceiling of 1,500s on compute, but CI's median was 2,352s and its maximum 2,792s (run 43336041385), so at least 852–1,292s per run was not compute.
  - Impact: high. Confidence: high that queueing exists; medium on how big it is.
- **No PR-scoped workflow cancels superseded runs.** `ci.yml`, `integration-pr-readiness.yml` and `lint-pr-body-auto-close.yml` have no `concurrency:` block, and 13 CI runs (about 78 parallel jobs) started within 34 minutes. Cancelling superseded PR runs is the biggest low-risk speed lever. Impact: high. Confidence: medium, because the share of superseded runs can't be measured without head-SHA data.
- **92.3% of runs (920 of 997) are skipped fan-out.** Every `issue_comment.created` event starts `internal-clarify.yml`, `internal-plan.yml`, `internal-implement.yml` and `internal-orchestrate-clarify-respond.yml`, and each skips at its job-level `if:`. These runs use no runner, but they flood the run list. The log collector also spent all 15 `recent/` log downloads that failed on these runs, each ending in `empty_log_archive`. Impact: medium (observability and API calls). Confidence: high.
- **All token, cache and memory telemetry reads zero because nothing was collected, not because nothing was used.** No real AI-phase run had its logs downloaded. Examples: clarify 43340496828 (704s), plan 43335941478 (385s), implement 43334423404 (135s), review-autofix 43339392189 (376s), all `not_selected`. `cache_hit_rate` is null everywhere. Impact: high (blind spot). Confidence: high.
- **The orchestrator poller completed only 2 of 7 ticks in about 77 minutes.** The other 5 were `cancelled_before_first_step`. Four of the five were cancelled within 3 seconds of the next poll run being created, which is the pending-slot replacement that `cancel-in-progress: false` produces. The real limiter is long ticks: 43333993097 ran 1,762s and 43336357276 ran 1,400s, and the poll job has no `timeout-minutes`. Impact: medium. Confidence: high.
- **CI failed 2 of 13 runs (15.4%)**, at two different steps, and the causes are unknown because the logs weren't available. Semble emitted 44 fallbacks, all `context=contract-test` (4 per CI run across 11 runs) and none at runtime. That is healthy test-fixture output and matches the 2026-10-16 analysis. Confidence: high on the classification, low on the CI root causes.

## Speed Optimizations

These are ranked by expected latency reduction. Items 1 and 2 are on the critical path; item 3 is diagnostic.

1. **Cancel superseded PR runs for CI, the readiness check and the PR-body lint.** Critical path.
   - **Evidence:**
     - The 13 CI runs were created between 23:50:57 and 00:24:43.
     - Clusters of starts: 00:13:50 (2 CI runs, 43335377329 and 43335384561 at 00:13:53, plus 4 readiness runs and 1 lint run); 00:20:41; 00:21:16; and 00:24:31/00:24:43.
     - No `concurrency:` block exists in `ci.yml`, `integration-pr-readiness.yml` or `lint-pr-body-auto-close.yml`.
     - The lint workflow also fires on `edited`, so each PR body edit queues a run.
   - **Root cause:** each new PR push or edit adds a full run (6 parallel CI jobs plus `lint`) on top of runs whose SHA is already stale. This competes for the account's concurrent-job cap.
   - **Exact change:** add the block below to `ci.yml` (2-space YAML). Push runs get a group unique per run, so main, stable and integration-branch push CI behaves exactly as today. This matters because budget-watch, heal intake and the base comparison in check-failure triage all rely on push results.
     ```yaml
     concurrency:
       group: ${{ github.event_name == 'pull_request' && format('ci-pr-{0}', github.event.pull_request.number) || format('ci-run-{0}', github.run_id) }}
       cancel-in-progress: ${{ github.event_name == 'pull_request' }}
     ```
     Apply the same pattern to `integration-pr-readiness.yml` and `lint-pr-body-auto-close.yml`. Both are `pull_request`-only, so they can simply use `group: <wf>-pr-${{ github.event.pull_request.number }}` with `cancel-in-progress: true`.
   - **Estimated savings:** each superseded PR CI run cancelled early frees up to 6 runners for the rest of its run (up to 20 minutes each). With the observed queueing of at least 852s at the median, removing even 2–3 superseded runs per burst should cut queue wait by several minutes for every other run. This is an inference; measure it with item 2.
   - **Risk:** low to medium. Before rollout, check whether `internal-check-failure-triage`, the heal-intake workflow or the `lint` aggregate treat a `cancelled` PR CI conclusion as a failure to act on. Rollback is deleting the block.

2. **Measure queue wait inside the existing budget-watch.** Diagnostic logging, critical-path enabler.
   - **Evidence:** `scripts/ci_budget_watch.py:94` computes `_seconds(job.get("started_at"), job.get("completed_at"))` only. It can't see a 39-minute CI run in which no single job nears its timeout.
   - **Exact change:** in `job_duration_findings`' caller, using the jobs list it already fetches (no new API call), print one `CI_QUEUE_WAIT: job=<name> queued_s=<started_at - created_at> run_id=<id>` line per job. Report only; open no issue at first.
   - **Estimated savings:** none directly. It turns the queueing evidence above from lower bounds into measured values.
   - **Risk:** very low. The step already ignores non-zero exit codes.

3. **Add per-stage timing to the poller.** Diagnostic, critical path for the orchestrator.
   - **Evidence:** ticks of 1,762s (43333993097) and 1,400s (43336357276) held the single `ai-orchestrate-poll-<repo>` group (`orchestrate_poll.yml:97-102`). Pending runs queued behind them were replaced.
   - **Exact change:** in `scripts/orchestrate_poll_process.sh`, emit `POLL_STAGE_TIMING: stage=<name> ms=<n> tracking_issues=<n>` at each major stage boundary. Also emit `POLL_TICK_TIMING: pending_s=<job start - run created> total_ms=<n>` once per tick. Any new log prefix must be registered through an `agents.d/` fragment per §25.
   - **Estimated savings:** this is what makes it possible to find which stage dominates the ticks of 1,400s and more.
   - **Risk:** very low.

Micro-optimizations: none recommended without step-level data.

## Cost Optimizations

1. **Collect logs from the runs that actually spend tokens.** This is the largest cost-visibility gap.
   - **Evidence:**
     - `codex_tokens_used=0`, `or_total_tokens=0` and `cache_hit_rate=null` in every family, even though AI-phase runs succeeded.
     - Those runs were all `log_download_status: not_selected`: clarify 43340496828 (704s) and 43341369442 (393s), plan 43335941478 (385s), implement 43334423404 (135s), review-autofix 43339392189, 43341564992 and 43333662788, and unblock-judge 43336140542, 43339177013 and 43333633398.
   - **Root cause:** in `scripts/collect_workflow_logs.py:1469-1473`, `categories["recent"]` takes the newest eligible runs with no conclusion filter. In this window those were all 1–10s skipped runs.
   - **Exact change:** see GH API item 1.
   - **Estimated savings:** it can't be estimated until tokens are visible. This is a prerequisite for any model, reasoning-level or cache tuning.
   - **Quality risk:** none.

2. **Semble.** 0 `SEMBLE_QUERY` calls, 0 logged bytes and 0 bootstraps. All 44 fallbacks are contract-test fixtures from CI.
   - No conclusion is possible about whether Semble reduces prompt expansion, because no AI-phase logs were parsed.
   - Change nothing until item 1 lands.

3. **Serena.** 0 queries, 0 response bytes, 0 probes. Whether it is replacing other tool or model work can't be assessed.

4. **Skipped fan-out (920 runs).** Job-level `if:` false on a reusable-workflow call uses no runner minutes, so the dollar cost is about zero.
   - Merging the four `issue_comment` wrappers into one dispatcher would cut the run count by about 75%. It is not recommended now: it is architectural churn and would change the check and run names that operators read.

5. **Avoidable reruns.** All 997 runs have `run_attempt: 1` and `retries: 0`, so no rerun waste was observed.

## Reliability Improvements

1. **Retrieve and triage the two CI failures.** This is the highest failure-rate driver: CI is at 15.4% against 0% for every other family.
   - **Run 43332263394:** failed in `tests-heal-plan-and-validation`, step "Alert inbox capture and store tests" (2,352s).
     - That step name does not appear anywhere in `.github/workflows/` on this checkout. Inference: it exists only on the PR head under test, i.e. a new test step failing on its own PR.
   - **Run 43335290177:** failed in `tests-hooks-and-orchestrator`, step "Security dependency hold tests (issue 4934)" (2,019s).
     - That step is `ci.yml:804-807`: `pytest -q -p no:cacheprovider tests/test_security_dependency.py tests/test_pr_dependency.py tests/test_check_triage_resolution.py`.
   - **Root cause category:** unknown (test failure); the logs were `success` for the collector but are not mounted here.
   - **Fix:** read `errors/43332263394/` and `errors/43335290177/` once the mount works. Check whether check-failure-triage 43337564980 (1,059s) and 43339783613 (737s) and heal intake 43337203531 (1,062s) handled these runs; the linkage is unverified.
   - **Fail-open:** not applicable.

2. **Fix the analysis input mount.** This addresses the failure of this report pipeline itself.
   - **Evidence:** the Run-logs directory path in the prompt is absent from the sandbox, so every deep-dive claim is unverifiable.
   - **Fix:** make the analysis step stage `workflow-log-output/` into the path the prompt names, or pass the real path.
   - **Impact:** turns this report from best-effort into fully evidence-backed.

3. **Classify superseded poller cancellations separately.** Metric hygiene.
   - **Evidence:** 5 of 7 poll runs were cancelled, a 71% "cancel rate".
     - 43332270477 was cancelled at about 23:59:22; 43333370431 was created at 23:59:20.
     - 43333370431 was cancelled at about 00:03:50; 43333993097 was created at 00:03:47.
     - 43335750828 was cancelled at about 00:21:05; 43336357276 was created at 00:21:02.
     - 43340164475 was cancelled at about 00:53:32; 43340652784 was created at 00:53:29.
     - This is the documented one-pending-slot behaviour, not a fault.
   - **Fix:** in the collector, tag `cancelled_before_first_step` runs in the `orchestrate_poll` family as `superseded_pending` and leave them out of failure KPIs.
   - **Rollback:** trivial.

4. **No BREAK_GLASS, CONTEXT_BUDGET_WARN, SEMBLE runtime fallback, SERENA_FALLBACK or SERENA_PROBE events were observed.**
   - The 44 Semble fallbacks are `context=contract-test`, i.e. fixture output (classified by `_is_contract_test_semble_line`, `scripts/cost_audit.py:381-382`). Exactly 4 per CI run is consistent with fixed test output. This is healthy fail-open, not a masked broken rollout.
   - Caveat: no AI-phase logs were parsed, so runtime absence is unproven.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines were available: there were no log excerpts and the logs directory was missing. Retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, and `fail_open` / `enabled:false` / push-retry counts are all unknown.

Recommendation: once the collector fix (GH API item 1) puts real clarify, plan, implement and review-autofix runs into `recent/`, confirm that each one emits at least one `op=retrieve` line. If a downloaded AI-phase log has none, treat it as an emission regression.

## GH API Call Audit

The input has no GH API call counts, hotspots or rate-limit events.

1. **Collector downloads log archives for skipped runs.**
   - **Evidence:** 15 runs in `recent_runs` have `diagnostic_failure_reason: "logs: empty_log_archive"`. Examples: 43342466251, 43342466339, 43342466341, 43342462847, 43342462826, 43342463000, 43342462781, 43342423561, 43342423795, 43342412497, 43342412539, 43342412657, 43342412550, 43342410096, 43342410075. All are `skipped`.
   - **Root cause:** `select_runs_for_log_export_categories` (`scripts/collect_workflow_logs.py:1422-1474`) applies no conclusion filter for `recent` or `slow`.
   - **Exact change** (update `tests/test_collect_workflow_logs.py` in the same PR):
     ```python
     # Skipped runs have no job logs (empty archive); keep their slots for real runs.
     non_skipped = [run for run in eligible if (run.get("conclusion") or "").lower() != "skipped"]
     recent_runs = _sort_runs_by_created_desc(non_skipped)
     ```
     Apply the same filter before the `slow_runs` per-repo loop.
   - **Estimated reduction:** about 15 wasted `/actions/runs/{id}/logs` calls per collection, with those slots reassigned to AI-phase runs.
   - **Rate-limit risk:** reduced. No new calls.

2. **Queue-time telemetry needs no new calls.** `ci_budget_watch.py` already holds job objects with `created_at` and `started_at`, so Speed item 2 reuses that payload.
   - This is preferred over adding `jobs` fetches for sampled successes in the collector (`jobs_fetch_status: not_required` today), which would add one REST call per run.

3. **Poller API hotspots are unknown.** The poll runs were `not_selected`. After the collector fix, confirm that the poller uses the cycle caches `_candidate_details_json` and `STALL_MANAGED_LINKED_PR_CACHE` (§14) and look for per-issue REST calls during the 1,400–1,762s ticks.

## Prompt Cache & Memory System

- `cache_hit_rate` is null and cache read/write tokens are 0 in every family. There is no evidence of cache creation or reads, and no `CONTEXT_BUDGET_WARN` pressure (count 0), but the cause is missing collection, not absent caching.
- Cache fragmentation can't be assessed: no prompt-prefix or token data.
- Next collection step: after GH API item 1, compare `or_cache_read_tokens / or_prompt_tokens` (or the Claude cache fields) for clarify, plan and review-autofix. Flag any phase below about 30% cache read for a prefix-stability review. That threshold is a suggestion, not a repo-defined value.

## Orchestrator Health

- **Tick throughput:** 2 completed ticks (43333993097 created 00:03:47, 1,762s; 43336357276 created 00:21:02, 1,400s) in about 77 minutes, against a nominal `*/5` cron (`internal-orchestrate-poll.yml:5`). The repo already notes that GitHub delivers that cron every 25–60 minutes (`orchestrate_poll.yml:1896`).
- **Group contention:** the poll group was held or pending almost continuously from 23:51 to about 00:44.
  - Inference: 43336357276 waited about 12 minutes pending behind 43333993097 (which finished around 00:33), so its real compute was about 690s.
  - Cancelled run 43340164475 (00:49:49) was pending behind a holder that is not in the run list. This is a coverage gap; it may be a follow-up dispatch from the "Queue a follow-up poll after progress" step.
- **No timeout on the poll job:** the only `timeout-minutes` in `orchestrate_poll.yml` is line 2229, for the daily-usage job. A hung tick could hold the group for GitHub's 360-minute default. Add the stage timing first (Speed item 3), then set an explicit timeout above the observed p99. Doing it blind risks killing a conflict heal mid-flight, which the concurrency comment warns about.
- **Judge and unblock activity:** 3 unblock-judge dispatches, all successful: #9233 (43333633398, 344s), #9235 (43336140542, 509s) and #9242 (43339177013, 351s). No clarification loops, wave deferrals or stuck states are visible; that needs poll logs.
- **Indicators to track:** completed ticks per hour; `POLL_TICK_TIMING pending_s` p50/p95; superseded pending count; tick `total_ms` p95; and the number of ticks that queued a follow-up.

## Pipeline Flow Bottlenecks

| Stage | Observed | Dominant overhead |
|---|---|---|
| clarify | 2 real runs (704s, 393s); 217 skipped | unknown; the 704s run (43340496828) is unexplained |
| plan | 1 real run (43335941478, 385s); 214 skipped | unknown |
| implement | 1 real run (43334423404, 135s); 215 skipped | unknown |
| review/autofix | 3 sweeps, p50 156s, p95 354s | unknown |
| CI / validate | 13 runs, p50 2,352s, at least 852s queue at p50 | **queueing**, then the 15.4% failure retry loop |
| readiness / PR lint | 17 + 11 runs; lower-bound queue wait up to 515s / 354s | **queueing** |
| orchestrate | 2 of 7 ticks completed | **long ticks holding a single concurrency group** |
| release | promote main→stable 730s (43336520685), candidate follower 296s, auto-release 604s (43339630976) | healthy |

Fixes in order of end-to-end impact:
1. PR concurrency cancellation (Speed item 1). This shortens every PR feedback loop.
2. Queue and stage telemetry (Speed items 2 and 3). This makes the CI and poller bottlenecks measurable.
3. The collector skipped-run filter. This opens up the AI-phase compute view.
4. Triage of the two CI failures.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - Runner queueing across CI, readiness and PR-body lint (at least 852–1,292s on CI runs).
  - Long poller ticks (1,400–1,762s) holding the single poll group.
- **Top failure modes:**
  - CI test failures at "Alert inbox capture and store tests" (43332263394) and "Security dependency hold tests (issue 4934)" (43335290177).
  - Poller pending-slot replacement (5 runs), which is benign but inflates the cancel metrics.
- **Highest-cost drivers:** unmeasurable, since 0 tokens were collected. Runner minutes are dominated by CI: 13 runs × 6 parallel jobs.
- **Top 3 actions:**
  1. Add PR-only `concurrency` with `cancel-in-progress` to `ci.yml`, `integration-pr-readiness.yml` and `lint-pr-body-auto-close.yml`. Push runs stay unchanged.
  2. Filter `skipped` runs out of `recent`/`slow` log selection in `scripts/collect_workflow_logs.py`.
  3. Add `CI_QUEUE_WAIT` to `ci_budget_watch.py`, plus `POLL_STAGE_TIMING` and `POLL_TICK_TIMING` to the poller.

## Metrics Appendix

**Runs and outcomes**

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | Failure rate | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Repo total | 997 | 70 | 2 | 5 | 920 | 0.20% (2.6% of 77 non-skipped) | 1 | 344 |
| ci | 13 | 11 | 2 | 0 | 0 | 15.4% | 2,352 | 2,664 |
| orchestrate_poll | 7 | 2 | 0 | 5 | 0 | 0% | 291 | 1,653 |
| integration_pr_readiness | 17 | 17 | 0 | 0 | 0 | 0% | 399 | 793 |
| lint_pr_body_auto_close | 11 | 11 | 0 | 0 | 0 | 0% | 320 | 601 |
| security_audit | 5 | 5 | 0 | 0 | 0 | 0% | 454 | 754 |
| review_autofix | 3 | 3 | 0 | 0 | 0 | 0% | 156 | 354 |
| unblock_judge_dispatch | 3 | 3 | 0 | 0 | 0 | 0% | 351 | 493 |
| cancel_on_pr_close | 4 | 4 | 0 | 0 | 0 | 0% | 235 | 557 |
| clarify / plan / implement / clarify_respond | 219 / 215 / 216 / 218 | 2 / 1 / 1 / 0 | 0 | 0 | 217 / 214 / 215 / 218 | 0% | 1 | 10 |
| check_failure_triage / heal / heal_intake | 18 / 22 / 23 | 2 / 2 / 3 | 0 | 0 | 16 / 20 / 20 | 0% | 1 | 785 / 316 / 422 |

**Lower-bound queue wait (run duration minus the maximum possible compute)**

| Run | Workflow | Duration (s) | Compute ceiling (s) | Queue at least (s) |
|---|---|---:|---:|---:|
| 43336041385 | CI | 2,792 | 1,500 | 1,292 |
| 43336823274 | CI | 2,579 | 1,500 | 1,079 |
| 43336388214 | Integration PR readiness | 815 | 300 | 515 |
| 43336849683 | Integration PR readiness | 787 | 300 | 487 |
| 43336849873 | Lint PR body | 654 | 300 | 354 |
| 43336823191 | Lint PR body | 547 | 300 | 247 |

**Cost and review telemetry**

| Metric | Value |
|---|---|
| codex_tokens_used / codex_calls | 0 / 0 |
| or_prompt / completion / total tokens | 0 / 0 / 0 |
| or_cache_write / read tokens | 0 / 0 |
| cache_hit_rate | null (all families) |
| wall_clock_p50_ms / p99_ms (repo, 108 samples) | 1,000 / 2,578,930 |
| wall_clock_p50_ms / p99_ms (ci, 11 samples) | 2,410,000 / 2,770,700 |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |
| runs_with_log_telemetry | 115 of 997 |

**GH API**

| Item | Value |
|---|---|
| API call counts / rate-limit events | not present in input |
| Log downloads that returned an empty archive (skipped runs) | 15 |
| Runs with `log_download_status: success` | 11 CI + 5 other (from rows) |

**Semble**

| Metric | Value |
|---|---|
| query_calls / logged bytes | 0 / 0 |
| fallbacks total / contract-test / runtime | 44 / 44 / 0 (CI only, 4 per run × 11 runs) |
| bootstraps / failed / unused / bootstrap_ms | 0 / 0 / 0 / 0 |
| sources_total / static_dup_bytes / echo_lines_dropped | 0 / 0 / 0 |
| per-target breakdown | not included in the input |

**Serena and MCP availability**

| Target | probe_ok | probe_failed | probe_skipped | queries | response_bytes | fallbacks |
|---|---:|---:|---:|---:|---:|---:|
| (none observed) | 0 | 0 | 0 | 0 | 0 | 0 |

Other MCP servers observed: none.

**Data gaps**
- The logs directory was missing, so there were no `errors/`, `slow/` or `recent/` reads.
- No `log_summary` fields were supplied.
- There is no PR or head-SHA data, so superseded runs can't be quantified.
- There is no GH API telemetry.
- No AI-phase logs were parsed, so tokens, cache, memory, Semble runtime and Serena are all blind.
## Deep Audit — Workflows & Scripts (2026-11-13)

Scope and method. I read the workflow and script sources directly: `ci.yml`, `lint-pr-body-auto-close.yml`, `integration-pr-readiness.yml`, `lint-plan-archival.yml`, `cancel_on_pr_close.yml`, `orchestrate_poll.yml`, and selected bodies of `implement.yml`, `plan.yml`, `review_autofix.yml` and `workflow-log-analysis.yml`. I also read `scripts/gh_helpers.sh`, `scripts/tg_helpers.sh`, `scripts/label_helpers.sh`, `scripts/stage_workflow_support.sh`, `scripts/check_integration_pr_readiness.py`, parts of `scripts/orchestrate_poll_process.sh` and `tests/test_workflow_file_size_limit.py`. I could not take byte or character measurements in this sandbox, so every size figure below is an estimate from line counts and is marked as one. Two topics are left out because the Executive Summary of this report already covers them: missing PR `concurrency:` blocks (Speed Optimizations item 1) and the missing poll-job `timeout-minutes` (Orchestrator Health).

### Section 1: Bug & Correctness Sweep

**SEC-001** · `scripts/tg_helpers.sh:20-29`, `scripts/gh_helpers.sh:120-127`, `.github/workflows/review_autofix.yml:2109-2117`, `5214-5216`, `5305-5307`, `5458-5460` · High · `security`
- **Description:**
  - Before anything else, `tg_helpers.sh` runs `if [ -f "scripts/gh_helpers.sh" ]; then source scripts/gh_helpers.sh; fi`. That path is relative to the current working directory, not to the helper's own directory.
  - In `review_autofix.yml`, "Checkout repo" (2109-2117) checks out the PR head (`ref: ${{ needs.gate.outputs.review_checkout_sha || github.sha }}`) into the workspace root with no `path:`. The workspace root is the default working directory for `run:` steps.
  - Seven steps run `source "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh"`, for example 5215, 5306 and 5459. Each has `GH_TOKEN: ${{ secrets.GH_PAT }}` and `TG_BOT_SECRET` in `env:` (5211-5213, 5302-5304, 5448-5450).
  - In this repository every PR head contains `scripts/gh_helpers.sh`, so the top-level code of the **PR's own copy** runs with the PAT in the environment. The trusted copy is only sourced one line later (5216), after that code has already run.
  - This breaks the trust contract in `scripts/stage_workflow_support.sh:31-33` and `112-113` ("never execute the PR copy") and in `agents.md:2961-2962`.
  - There is a second, narrower path. `gh_helpers.sh:124-126` falls back to `source scripts/emit_event.sh` from the working directory when the staged bundle has no `emit_event.sh`, and that file is optional (`stage_workflow_support.sh:137-144`).
  - [NEEDS VERIFICATION] I did not check consumer repos; they are only exposed if they ship a `scripts/gh_helpers.sh`. I also did not check every one of the seven steps for a `working-directory:` override; none of the three steps I read has one.
- **Recommended fix:**
  - Resolve the dependency next to the helper itself, as `scripts/review_head_gate.sh:209` and `scripts/review_merge_train.sh:132-134` already do. Remove the working-directory fallback from both helpers:
    ```bash
    _tg_helpers_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
    if [ -n "${_tg_helpers_dir}" ] && [ -f "${_tg_helpers_dir}/gh_helpers.sh" ]
    then
    	# shellcheck disable=SC1091
    	source "${_tg_helpers_dir}/gh_helpers.sh"
    fi
    unset _tg_helpers_dir
    ```
  - Add a regression test that sources `tg_helpers.sh` from a temp directory containing a booby-trapped `scripts/gh_helpers.sh`, and asserts that the trap never fires.
  - `orchestrate_poll.yml:297-302` already passes a trusted `tg_helpers.sh` path, so it is unaffected.

**SEC-002** · `.github/workflows/lint-pr-body-auto-close.yml:49-56,112-115`; `.github/workflows/integration-pr-readiness.yml:39,51-54,75-79`; `.github/workflows/lint-plan-archival.yml:34-41,69-72` · Medium · `security`
- **Description:**
  - All three PR gates check out the `pull_request` default merge ref and then run the checker script from that checkout: `scripts/lint_pr_body_auto_close.py`, `scripts/check_integration_pr_readiness.py` and `scripts/lint_plan_archival_completeness.py`.
  - A PR that edits the checker therefore decides its own gate result.
  - The readiness job also holds `statuses: write` (line 39) and posts the required `orchestrator/integration-pr-not-ready` status itself (`check_integration_pr_readiness.py:111-137`). A modified script on an `orchestrator/project-*` head can post `success` for its own SHA.
  - Fork PRs get a read-only token. The exposed path is same-repo heads, i.e. the AI-written `ai/issue-*` and `orchestrator/project-*` branches. That is an inference from GitHub's token rules.
- **Recommended fix:**
  - Add a second, sparse checkout of the checker at `${{ github.event.pull_request.base.sha }}` into a separate path (e.g. `.pr-gate-src`) with `persist-credentials: false`, and run the script from there.
  - This follows the pinned-script checkout that `test-and-mark-stable.yml` uses (`agents.md:2978-2980`).
  - Keep the PR checkout only as data; for example, `lint-plan-archival.yml` still needs it for its `git diff`.

**SEC-003** · `.github/workflows/plan.yml:215-217,244,257,267` (same code in `clarify.yml:138`, `validate.yml:253`, `implement.yml:634`, `orchestrate_clarify_respond.yml:276`) · Low · `security`
- **Description:**
  - The resolver builds `resolver_git_auth_header="$(printf 'x-access-token:%s' "${GH_TOKEN}" | base64 | tr -d '\n')"` and passes it in argv as `git -c "http.extraHeader=Authorization: Basic …"`.
  - The clone-log redaction `sed -E 's#x-access-token:[^@]+@#…#'` only covers the URL form, not the base64 header.
  - [NEEDS VERIFICATION] I did not confirm whether the runner auto-masks base64 forms of secrets.
- **Recommended fix:** emit `echo "::add-mask::${resolver_git_auth_header}"` right after computing it, in all five copies. This becomes one edit if DUP-002 lands.

**BUG-001** · `.github/workflows/integration-pr-readiness.yml:25-33,56-65`; `scripts/check_integration_pr_readiness.py:190,233,247` · Medium · `bug`
- **Description:**
  - Override detection reads labels from the event payload snapshot: `PR_LABELS_JSON: ${{ toJSON(github.event.pull_request.labels) }}`.
  - The workflow fires on both `labeled` and `unlabeled` and has no concurrency group. Two runs for the same head SHA can therefore finish out of order. Every run posts the same status context on the same SHA, so the last writer wins.
  - Example: add then quickly remove `ai:override-incomplete-merge`. If the `labeled` run finishes last, the required status stays `success` with the override already removed (a race).
- **Recommended fix:**
  - Read the labels live inside the script with one `gh pr view <n> --json labels` call. The script already shells out to `gh` (`_fetch_issue`, 73-88).
  - Also add `concurrency: {group: integration-pr-readiness-${{ github.event.pull_request.number }}, cancel-in-progress: true}`, which cross-references Speed Optimizations item 1.

**BUG-002** · `.github/workflows/implement.yml:5591-5595`, `5726-5729`; `scripts/gh_helpers.sh:840-851,1212-1217` · Medium · `bug`
- **Description:**
  - The code is `gh_retry gh issue close N --comment "${body}" || gh_retry gh api …/comments -f body="${body}"`.
  - `_gh_cmd_is_unsafe_post` treats only `issue create|issue comment|pr create|pr comment|workflow run|release create` as non-idempotent (849). So `issue close --comment` gets up to 5 attempts, and each attempt can post the comment again.
  - If the close keeps failing, the fallback only comments. The issue stays **open** with `ai:closed`, and the marker comment can be duplicated.
  - [NEEDS VERIFICATION] I assumed the gh CLI posts the comment before closing.
- **Recommended fix:**
  - Split the two actions: post the comment once with `gh_retry gh api "repos/${REPO}/issues/${ISSUE_NUMBER}/comments" -f body=…`, which is single-attempt by design, then run `gh_retry gh issue close "${ISSUE_NUMBER}" --repo "${REPO}"` with no `--comment`.
  - Alternatively, extend the `case` at `gh_helpers.sh:849` so that `issue close` and `pr close` count as unsafe whenever `--comment` is present.

**BUG-003** · `.github/workflows/implement.yml:5545,5757-5774` · Medium · `bug`
- **Description:**
  - Under `set -euo pipefail`, the fall-through no-op path runs `gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` with no `|| …` and no stdout redirect.
  - The POST is single-attempt (`gh_helpers.sh:1215-1217`). One transient failure exits the step before lines 5763-5774 add `ai:implementation-failed`, which is the label the orchestrator re-issue path keys on.
  - On success, the full comment JSON is dumped into the log.
  - The earlier guards in the same step already use `>/dev/null 2>&1 || true` (5637-5638).
- **Recommended fix:** append `>/dev/null || echo "::warning::Could not post the no-op comment on #${ISSUE_NUMBER}; labelling anyway."` so that labelling always runs.

**BUG-004** · `scripts/tg_helpers.sh:26-29,434-448` · Low · `bug`
- **Description:**
  - When `gh_helpers.sh` is not found, `curl_gh_api() { curl "$@"; }` is defined. Callers pass `-s` without `-f`, so HTTP 4xx/5xx error bodies come back with exit 0.
  - `_tg_newest_comments_json` returns the second call's output directly (444-447). An error object such as `{"message":…}` then reaches callers as if it were a comments page.
  - [NEEDS VERIFICATION] I did not check how those callers handle a non-array.
- **Recommended fix:** make the fallback `curl_gh_api() { curl -fsS "$@"; }`. After SEC-001 the fallback should become unreachable in staged runs.

**SHELL-001** · `.github/workflows/review_autofix.yml:5214-5217`, `5305-5308`, `5458-5460` · Low · `shellcheck`
- **Description:**
  - The Telegram and PR-comment notification steps have no `set -euo pipefail`, and they source `"${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh"` without checking that it exists.
  - If the bundle is missing, the step logs `tg_send_tracked: command not found` and still succeeds, so the operator alert is lost silently.
  - `plan.yml:3644-3647` shows the guarded pattern.
- **Recommended fix:** add `set -uo pipefail`, and when the helper is missing emit `[ -f "${SUPPORT_SCRIPTS_DIR:-}/tg_helpers.sh" ] || { echo "::warning::tg_helpers.sh not staged; notification skipped."; exit 0; }`. This keeps the steps fail-open.

### Section 2: GitHub API Call Redundancy Audit

**API-001** · `scripts/gh_helpers.sh:1434-1508` · Medium · `api-redundancy`
- **Description:**
  - `gh_api_json_to_file` never calls `_is_gh_permanent_failure`. `gh_retry` (1262) and `gh_retry_to_file` (1347) both do.
  - So a 404, 401, 400 or 422 is retried up to `GH_RETRY_MAX_ATTEMPTS` (5) times, with 1+2+4+8 s of sleep.
  - The helper has 33 call sites in 6 scripts, 19 of them in `scripts/workflow_failure_heal_intake.sh`.
- **Calls:** currently 5 per permanent failure; 1 after the fix.
- **Recommended fix:** add the same `_is_gh_permanent_failure "${stderr_content}"` early return that `gh_retry_to_file` uses, inside the non-zero-exit branch (1475-1496). This is best done through DUP-006.

**API-002** · `scripts/gh_helpers.sh:1523-1576`; `scripts/tg_helpers.sh:316-327` · Medium · `api-redundancy`
- **Description:**
  - `curl_gh_api` retries every non-2xx response, including 401, 404 and 422, with exponential backoff.
  - It also has no unsafe-POST guard. That is not a problem today, because all four callers in `tg_helpers.sh` are GETs.
  - `_tg_tracking_author_login` works around this by capping `/user` at 2 attempts ("A 403 from /user is permanent for installation tokens").
- **Calls:** currently 5 per permanent failure; 1 after the fix.
- **Recommended fix:** after the rate-limit branch, return 1 immediately for `400|401|404|422`, matching `_is_gh_permanent_failure`. Then remove the per-call cap in `tg_helpers.sh:316-320`.

**API-003** · `scripts/label_helpers.sh:196-214`; `scripts/gh_helpers.sh:230`; `.github/workflows/implement.yml:5581-5583,5629-5631,5716-5718,5767-5769` · Low · `api-redundancy`
- **Description:**
  - `ensure_label_exists` and the inline `gh_retry gh label create … || true` calls only treat "already exists" as success after `gh_retry` has used up all its attempts.
  - `_is_gh_permanent_failure` matches `HTTP 422` but not the text `already exists`.
  - `workflow-log-analysis.yml:1106` (`_wla_label_create`) checks for `*"already exists"*` before retrying, which suggests the retry problem has already been seen. [NEEDS VERIFICATION] I have not confirmed that `gh label create` stderr lacks `HTTP 422`.
- **Calls:** currently up to 5 per existing label per process (the cache at `label_helpers.sh:187-191` only helps within one process); 1 after the fix.
- **Recommended fix:** add `already exists` to the regex at `gh_helpers.sh:230`, or reuse `_wla_label_create`'s single-attempt pre-check inside `ensure_label_exists`.

**API-004** · `.github/workflows/cancel_on_pr_close.yml:20-21,371-387` · Low · `api-redundancy`
- **Description:**
  - The concurrency comment says "cancel POSTs are idempotent", yet `gh_retry gh api -X POST …/runs/${run_id}/cancel` runs once (`gh_helpers.sh:1215-1217`).
  - A transient 5xx therefore leaves a closed PR's run running.
  - The per-run loop itself can't be avoided: there is no bulk-cancel endpoint.
- **Calls:** N cancel POSTs today and after the fix; extra calls happen only on transient failure.
- **Recommended fix:** call `gh_retry --idempotent gh api -X POST …`, using the existing `--idempotent` flag (`gh_helpers.sh:1213`).

**BATCH-001** · `scripts/orchestrate_poll_process.sh:8796-8817` · Medium · `api-batching`
- **Description:**
  - `security_pass_unblock_filed_advisory_followups` makes one REST `issues/{n}` GET per follow-up row inside a `while read` loop (8801). It adds a paginated comments GET for every row labelled `ai:blocked` (8814).
  - The function comment (8771-8775) says the candidate-details GraphQL batch is out of scope. The batching pattern still applies.
- **Calls:** currently N + B (B is the number of blocked rows, at least 1 page each); about ⌈N/50⌉ after batching.
- **Recommended fix:**
  - Use one aliased GraphQL query per ≤50 rows, `i<n>: issue(number:<n>){state labels(first:100){nodes{name}} comments(last:100){totalCount nodes{body authorAssociation author{login}}}}`.
  - Extend the alias-builder pattern of `_fetch_candidate_issue_details_graphql`, or the 50-per-batch loop in `cancel_on_pr_close.yml:203-263`.
  - Fall back to REST comments only when `totalCount > 100`.

**BATCH-002** · `scripts/orchestrate_poll_process.sh:186-205` · Low · `api-batching`
- **Description:**
  - The listing `gh issue list … --json number` (193-194) is followed, for each queued issue, by a full `issues/{n}` GET (202) and a paginated comments read (205).
  - The GET only checks state, the label and that the item is not a PR. All three are available from the listing.
  - [NEEDS VERIFICATION] The comment at 187 deliberately asks for a "live issue read", and the listing is only seconds older.
- **Calls:** currently 2N + 2; N + 2 after the fix.
- **Recommended fix:** change the listing to `--json number,state,labels` and drop the per-issue GET. Alternatively, alias-batch both issue state and comments as in BATCH-001.

**BATCH-003** · `.github/workflows/implement.yml:5681-5703` · Low · `api-batching`
- **Description:**
  - Each hop of the ancestor-chain no-op walk makes two calls: the issue body (5691) and the paginated comments (5695-5696).
  - Hops depend on each other, so the walk can't be batched across hops. Within a hop, the two reads can be merged.
- **Calls:** currently ≤ 2 × `IMPL_NOOP_ANCESTRY_THRESHOLD` (4 with the default); ≤ threshold (2) after the fix.
- **Recommended fix:** use one GraphQL `issue(number:$n){body comments(last:100){nodes{body}}}` per hop, and fall back to REST pagination when `totalCount > 100`.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** · Exemplars: `.github/workflows/orchestrate_poll.yml:147-188`, `.github/workflows/cancel_on_pr_close.yml:32-74`, `.github/workflows/implement.yml:474-478` · Medium · `duplication`
- **Description:**
  - The "Resolve workflow support ref" block (`promote_candidate_support_ref="${{ github.sha }}"`, the promote-candidate protection check and the consumer `<file>@<sha>` pin) appears **16 times in 9 workflows**.
  - Counts per file: `implement.yml` 4, `clarify.yml` 2, `plan.yml` 2, `orchestrate.yml` 2, `orchestrate_poll.yml` 2, and 1 each in `cancel_on_pr_close.yml`, `check_failure_triage.yml`, `orchestrate_clarify_respond.yml` and `workflow_failure_heal.yml`.
  - Only the workflow-file regex fragment differs between copies.
- **Recommended fix:**
  - Create a SHA-pinned composite action `.github/actions/resolve-support-ref/action.yml`. Inputs: `workflow_file` and `job_json` (`${{ toJSON(job) }}`). Output: `script_ref`. It also writes `SCRIPT_REF` to `GITHUB_ENV`.
  - Reference it as `shubhodeep1/coding-workflows/.github/actions/resolve-support-ref@<sha>`, the same way `install-codex` is pinned (`clarify.yml:384`). It must be SHA-pinned because it decides which support code runs.

**DUP-002** · `.github/workflows/clarify.yml:108` onward (the next step's `run:` is at 360), `plan.yml:184-463`, `implement.yml:604` onward (the next `run:` is at 778), `validate.yml:200` onward (next `run:` at 360), `orchestrate_clarify_respond.yml:246` onward (next `run:` at 383) · Medium · `duplication`
- **Description:**
  - The integration-ref resolver staging block appears in 5 workflows. Each copy contains the same `resolver_stage_root=`, `resolver_git_auth_header=`, `resolver_stage_fail_closed()`, exit-3/75/2 handling and, in clarify and plan, the `label_missing` hold (`clarify.yml:232,252`; `plan.yml:313,357`).
  - The plan copy says it behaves "as in clarify" (309-311). Fixes such as SEC-003 currently need five edits.
- **Recommended fix:**
  - Create a SHA-pinned composite action `.github/actions/resolve-integration-ref`.
  - Inputs: `issue_number`, `routing_enabled`, `hold_skip_env` (`SKIP_PLAN` or `SKIP_CLARIFY`), `phase`.
  - Outputs: `ref`, `resolver_status`, `integration_branch_meta`, `pr_not_open`, `pr_not_open_pr`, `label_missing_hold`.
  - Update the body-extracting contract tests (e.g. `tests/test_workflow_checkout_integration_ref_audit.py`) in the same PR.
  - This also resolves EXPR-002 and EXPR-003.

**DUP-003** · `.github/workflows/workflow-log-analysis.yml:310-347` (and its copies at 934-958, 1777-1801, 2384-2408), `.github/workflows/plan.yml:147-171` · Low · `duplication`
- **Description:** the Cache, Persist and Restore Codex CLI triplet appears 5 times, wrapped around the existing `install-codex` composite action.
- **Recommended fix:** move the `actions/cache` restore, persist and restore logic into `.github/actions/install-codex/action.yml` behind a `cache: "true"` input, then bump the SHA pin at every call site.

**DUP-004** · `.github/workflows/workflow-log-analysis.yml:1127-1220` (and its copies after 1900 and after 2500) · Low · `duplication`
- **Description:**
  - The three passes (analysis, deep audit, API redundancy) each define a near-identical `emit_*_phase_failure`.
  - The matching lines sit at the same offsets in each copy: 1140/1932/2532, 1155/1946/2546 and 1200/1985/2585.
- **Recommended fix:** create `scripts/workflow_log_analysis_helpers.sh` containing `wla_emit_phase_failure <phase> <resume_action> <failed_step> <failure_mode> <attempt_count> <summary>`. It should read `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_WORKFLOW` and `GITHUB_SERVER_URL` from the environment. This also removes the expressions behind EXPR-001 and EXPR-004.

**DUP-005** · `.github/workflows/review_autofix.yml:5225-5240`, `5316-5331` · Low · `duplication`
- **Description:**
  - The `autofix-failure-fingerprint` marker block is copied verbatim.
  - The comment says the same evidence order is also used by "Assemble failure evidence" (`review_autofix_step_assemble_failure_evidence.sh`), which makes three copies.
- **Recommended fix:**
  - Add an `autofix_failure_marker_suffix` function in a new `scripts/review_autofix_failure_marker.sh`.
  - Register it in `REQUIRED_BOOTSTRAP_SCRIPTS` (`stage_workflow_support.sh:57`) and in `REVIEW_AUTOFIX_STEP_SCRIPTS`, following `agents.md:2952-2955`.

**DUP-006** · `scripts/gh_helpers.sh:1259-1287`, `1344-1373`, `1476-1495`, `1550-1567` · Low · `duplication`
- **Description:**
  - The failure classification and backoff branch is written four times: in `gh_retry`, `gh_retry_to_file`, `gh_api_json_to_file` and `curl_gh_api`.
  - The copies have already drifted; API-001 and API-002 are the result.
- **Recommended fix:** add `_gh_retry_handle_failure <attempt> <max_attempts> <stderr_text> <cmd…>`, returning 0 to retry after waiting and 1 to stop. Call it from all four loops.

**DUP-007** · `scripts/orchestrate_poll_process.sh:3490-3517`, `3606-3623` · Low · `duplication`
- **Description:** `post_tracking_comment` and `post_issue_comment_json` repeat the same 65,536-byte size check and the stdin `jq -Rs '{body: .}'` payload encoding.
- **Recommended fix:** add `_gh_comment_payload_file <issue_num> <body>`, which prints the path or returns 1. Use it in both functions.

**DUP-008** · `.github/workflows/implement.yml:5581-5595`, `5716-5729` · Low · `duplication`
- **Description:** the "create `ai:closed`, swap labels, close with comment" sequence appears twice in one step. It also carries BUG-002.
- **Recommended fix:** create a helper `implement_close_as_ai_closed <body>` and fold it into the extracted `scripts/implement_step_handle_noop.sh` proposed under EXPR-005.

### Section 4: Expression Size Limit Risk Assessment

CI already enforces an 18,000-character ceiling on every interpolated `run:` body in `.github/workflows/*.yml` (`tests/test_workflow_file_size_limit.py:66-98`). So on a green main no body can be at or above the High threshold. The figures below are estimates from line counts and observed line widths, not measurements. To verify one, run `python3 -c` with `yaml.safe_load` and `len(step["run"])` on the named step. The cheapest fix for all of them is the one the guard's own message gives (line 97): replace `${{ github.repository|sha|run_id|run_attempt|workflow|server_url }}` with `${GITHUB_REPOSITORY}`, `${GITHUB_SHA}`, `${GITHUB_RUN_ID}`, `${GITHUB_RUN_ATTEMPT}`, `${GITHUB_WORKFLOW}` and `${GITHUB_SERVER_URL}`, and pass step outputs through `env:`. That takes the body out of expression evaluation entirely.

**EXPR-001** · `.github/workflows/workflow-log-analysis.yml:1095-1428` ("Run workflow log analysis") · Medium · `expression-limit`
- **Description:**
  - The body is 334 lines and holds the prompt-assembly block (1328-1345) and the engine-fallback logic, both of which are likely to grow.
  - Its interpolations are `github.run_id`, `run_attempt`, `workflow`, `server_url` and `repository` (1140-1142, 1155, 1161-1163, 1200-1202).
  - Estimated length: about 15,000–17,900 characters, leaving about 3,100–6,000 characters of headroom against 21,000. [NEEDS VERIFICATION]
- **Recommended fix:** switch to the `GITHUB_*` env vars (11 occurrences); after that the body has no `${{ }}` left. Moving the phase-failure function out (DUP-004) cuts another ~95 lines.

**EXPR-002** · `.github/workflows/plan.yml:184-463` ("refctx") · Medium · `expression-limit`
- **Description:**
  - The body is 280 lines. It includes three prose lines of 300–400 characters (419-421) and inline Python (392-411).
  - It has only 4 interpolations (204-205, 295, 356).
  - Estimated length: about 15,500–17,900 characters; headroom about 3,100–5,500. [NEEDS VERIFICATION]
- **Recommended fix:** use `${GITHUB_REPOSITORY}` and `${GITHUB_SHA}`, or extract the block via DUP-002.

**EXPR-003** · `.github/workflows/clarify.yml:108` onward (the next step's `run:` is at 360) · Medium · `expression-limit`
- **Description:**
  - This is the same resolver block as EXPR-002, about 250 lines, with interpolations at 127-128, 209 and 251.
  - Estimated length: about 14,000–17,000 characters; headroom about 4,000–7,000. [NEEDS VERIFICATION]
- **Recommended fix:** same as EXPR-002.

**EXPR-005** · `.github/workflows/implement.yml:5544-5774` ("Handle no-op implementation") · Medium · `expression-limit`
- **Description:**
  - The body is 231 lines and contains several 250–400-character comment-body lines (5574, 5576, 5617, 5624, 5711, 5713).
  - Interpolations are at 5549, 5551, 5757, 5765, 5767, 5771 and 5773.
  - Estimated length: about 14,500–17,000 characters; headroom about 4,000–6,500. [NEEDS VERIFICATION]
- **Recommended fix:** use the `GITHUB_*` env vars (`RUN_URL` and `REPO` are already locals). Preferably also move the body to `scripts/implement_step_handle_noop.sh`, using the staged-support wrapper pattern (`agents.md:2939-2960`).

**EXPR-006** · `tests/test_workflow_file_size_limit.py:84-90` · Medium · `expression-limit`
- **Description:**
  - The guard measures `len(run)` on the parsed string.
  - When GitHub turns a templated string into its `format(...)` form, literal `{`/`}` become `{{`/`}}` and `'` becomes `''`. This inference matches the runner's templating as I understand it. [NEEDS VERIFICATION]
  - Bash bodies use `${…}` on most lines, so the real expression can be 5–10% longer than `len(run)`. The 3,000-character headroom may not cover that for brace-heavy bodies such as EXPR-001.
- **Recommended fix:** measure `len(run) + run.count("{") + run.count("}") + run.count("'")` (a conservative upper bound), keep `RUN_EXPRESSION_GUARD_CHARS = 18_000`, and print both values in the failure message.

**EXPR-004** · `.github/workflows/workflow-log-analysis.yml:1900` onward (the next `run:` is at 2161) and `2500` onward (the next `run:` is at 2759) · Low · `expression-limit`
- **Description:** two near-copies of EXPR-001, about 255–260 lines each, estimated at 12,000–15,000 characters each. [NEEDS VERIFICATION]
- **Recommended fix:** same as EXPR-001, together with DUP-004.

**EXPR-007** · `tests/test_workflow_file_size_limit.py:35-36,74-81` · Low · `expression-limit`
- **Description:** the guard only scans `.github/workflows/*.y*ml` `jobs.*.steps`. Composite-action steps (`.github/actions/*/action.yml` `runs.steps`) and `workflow-templates/*.yml` are not checked.
- **Recommended fix:** extend `_workflow_files()` and the step walk to cover both.

**EXPR-008** · Watch list: `.github/workflows/validate.yml:200` (about 155 lines), `orchestrate_clarify_respond.yml:246` (about 135 lines), `implement.yml:604` (about 170 lines), `review_autofix.yml:5305-5443` (139 lines) · Low · `expression-limit`
- **Description:** all four are estimated at 8,000–11,000 characters, so they are not at risk today. All of them interpolate only `github.*` values that have env equivalents.
- **Recommended fix:** apply the same `GITHUB_*` substitution opportunistically when these steps are next edited.

Bodies without interpolation are exempt and not counted: `implement.yml:2932-4220` (1,288 lines), `plan.yml:3045-3593`, `plan.yml:1194-1488`, `implement.yml:4346-4745` and `4760-5288`. Adding a single `${{ }}` to any of them would trip the CI guard, so the guard protects them. The largest `if:` conditions (`review_autofix.yml:5446`, about 430 characters; `implement.yml:5537`, about 330 characters) are negligible.

File size: the audit brief's 800 KB / 1 MB thresholds are above GitHub's real 512,000-byte limit (`agents.md:2913-2919`). The repo's 480,000-byte guard (`tests/test_workflow_file_size_limit.py:27`) already caps every workflow, so none can reach 800 KB. By line count the largest are `implement.yml` (7,362), `test-and-mark-stable.yml` (6,963) and `review_autofix.yml` (6,012). I did not measure bytes. [NEEDS VERIFICATION] Whether `implement.yml` has passed budget-watch's 440,000-byte warning line (`agents.md:2902-2907`).

### Section 5: Cross-Cutting Concerns

**DEAD-001** · `.github/workflows/lint-plan-archival.yml:57-62` · Low · `dead-code`
- **Description:** `: > /tmp/plan-archival-lint/added.txt` comes straight before `git diff … > /tmp/plan-archival-lint/added.txt`, which truncates the file anyway. The comment ("Skip empty output … by touching the output file first") describes a behaviour the code does not need.
- **Recommended fix:** remove the `:` line and its comment.

**CONSIST-001** · Working-directory-relative `source scripts/gh_helpers.sh`: `scripts/tg_helpers.sh:24`, `orchestrate_poll_process.sh:176`, `validate_process.sh:363`, `check_failure_triage.sh:371`, `workflow_failure_heal_intake.sh:260`, `workflow_failure_heal_report.sh:54`, `workflow_failure_heal_verify.sh:67`, `workflow_failure_heal_recovery_scan.sh:60`, `consumer_health_check.sh:70`, `self_heal_validation.sh:51`, `plan_split_dispatch.sh:46`, `run_plan_codex.sh:3`, `stable_e2e_step_verify_bait_removed.sh:65` · Medium · `consistency`
- **Description:**
  - These 13 scripts resolve the helper from the working directory.
  - Five others resolve it next to themselves: `review_head_gate.sh:209`, `review_merge_train.sh:132-134`, `implement_handle_guard_block.sh:12`, `implement_stale_branch_check.sh:53`, and `gh_helpers.sh:120` itself.
  - The working-directory form is the root cause of SEC-001.
  - Where a script silences errors (`2>/dev/null || true`), running it from the wrong directory just drops retries. Callers' `gh_retry() { "$@"; }` fallback then hides that.
- **Recommended fix:** standardise on `"$(dirname "${BASH_SOURCE[0]}")/gh_helpers.sh"`. Keep the working-directory form only where the workflow installs a trusted copy into `scripts/` (`orchestrate_poll.yml:250-251`, `cancel_on_pr_close.yml:138-139`).

**CONSIST-002** · `.github/workflows/implement.yml:5581-5582`, `5629-5630`; `.github/ai/label_contract.v1.json:100-102,156-158`; `scripts/label_helpers.sh:176-180` · Medium · `consistency`
- **Description:**
  - The inline `gh label create` literals differ from the label contract:
    - `ai:closed` is created as `6b7280`, "Issue closed by automation …"; the contract says `6a737d`, "Linked PR closed without merge".
    - `ai:needs-human` is created as `b60205`; the contract says `e11d48`.
  - There are 8 inline label creations in `implement.yml` and 1 in `clarify.yml`. Elsewhere the code uses `ensure_label_exists`, which reads the contract maps (`_AI_LABEL_COLORS` and `_AI_LABEL_DESCS`).
  - In a fresh repo, the inline literal is whatever gets created first.
- **Recommended fix:** replace the inline creations with `ensure_label_exists "<label>" "${REPO}"`, as `implement.yml:5763-5765` already does where `label_helpers.sh` is staged.

**SHELL-002** · `.github/workflows/ci.yml:438-440`, `478-480`, `509-511` · Low · `shellcheck`
- **Description:**
  - `python3 -c "import json; json.load(open('$f'))" && echo … || { …; exit 1; }` embeds the filename inside Python source. A `'` in a filename breaks it.
  - All three loops use the `A && B || C` pattern (SC2015).
- **Recommended fix:** use `python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$f"`, and replace the `&&`/`||` chains with `if …; then …; else …; fi`.

**SHELL-003** · `.github/workflows/review_autofix.yml:5387-5388` · Low · `shellcheck`
- **Description:** `ls -1 "${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*.err | wc -l` and `ls -1 … | sort -V | tail -n 1` parse `ls` output (SC2012).
- **Recommended fix:** use `shopt -s nullglob; editor_err_files=("${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*.err); editor_attempts="${#editor_err_files[@]}"`, then pick the last file with `printf '%s\n' "${editor_err_files[@]}" | sort -V | tail -n 1`.

TODO, FIXME and HACK markers: a word-bounded search over `scripts/` and `.github/workflows/` found none.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | SEC-001 |
| Medium | 16 | SEC-002, BUG-001, BUG-002, BUG-003, API-001, API-002, BATCH-001, DUP-001, DUP-002, EXPR-001, EXPR-002, EXPR-003, EXPR-005, EXPR-006, CONSIST-001, CONSIST-002 |
| Low | 19 | SEC-003, BUG-004, SHELL-001, API-003, API-004, BATCH-002, BATCH-003, DUP-003, DUP-004, DUP-005, DUP-006, DUP-007, DUP-008, EXPR-004, EXPR-007, EXPR-008, DEAD-001, SHELL-002, SHELL-003 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/tg_helpers.sh`, `scripts/gh_helpers.sh`, a new regression test under `tests/` | Small |
| API call optimization | `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/implement.yml`, `.github/workflows/cancel_on_pr_close.yml` | Medium |
| Code modularization | 9 workflows with the support-ref block, 5 with the resolver block, `workflow-log-analysis.yml`, `review_autofix.yml`, new composite actions under `.github/actions/`, new `scripts/*_helpers.sh` / step scripts, `stage_workflow_support.sh`, `tests/review_autofix_step_scripts.py` | Large |
| Expression size reduction | `workflow-log-analysis.yml`, `plan.yml`, `clarify.yml`, `implement.yml`, `tests/test_workflow_file_size_limit.py`, plus body-extracting contract tests | Medium |
| Medium/Low fixes | `integration-pr-readiness.yml`, `scripts/check_integration_pr_readiness.py`, `lint-pr-body-auto-close.yml`, `lint-plan-archival.yml`, `implement.yml`, `review_autofix.yml`, `ci.yml`, the 13 working-directory-sourcing scripts in CONSIST-001 | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-11-13)

### Safety Tag Legend
- **`SAFE_TO_MERGE`:** reading the code proves every merge precondition. The calls hit the same endpoint with the same fields, filters and auth. They sit in the same function with nothing changing the data in between. Errors and retries behave the same afterwards. No job or concurrency boundary is crossed. The implement stage may apply these without further review.
- **`NEEDS_VERIFICATION`:** the change looks correct, but at least one precondition can't be proven by reading the code. Run the listed check first.
- **`RISKY_SKIP`:** at least one hard trigger applies: the call sits in a retry or backoff loop, an identity or permission check, paginated reads, a rate-limit probe, `orchestrate_poll_process.sh` (or another path that guards against races), or it changes log keys that other code greps for. These are listed so they are visible, and must not be implemented automatically.

### Consolidation Candidates (MERGE-###)

**MERGE-001** · `RISKY_SKIP` · `scripts/review_enable_auto_merge.sh:154-170`, `:221-228` (and `:91-109` on the e2e early exit)
- **Calls today:** 2 or more on the main path: one or more label pages, then one full PR GET. The e2e early exit also makes 2: label pages, then a head-SHA GET.
- **Calls after the change:** 1.
- **Endpoints:**
  - `GET /repos/{o}/{r}/issues/{n}/labels?per_page=100` (`--paginate`)
  - `GET /repos/{o}/{r}/pulls/{n}`
- **Evidence:**
  - The e2e guard reads labels on their own:
    ```bash
    if PR_LABELS_RAW="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels?per_page=100" --jq '.[].name' 2>"${_label_err_file}")"; then
    ```
  - About 66 lines later, nothing having been written in between, the script fetches the full PR object (`_ORCH_PR_META_JSON`, line 221). That object already has a `labels[]` array, and the script also reads `.head.ref`, `.head.sha`, `.base.ref` and `.body` from it (lines 230, 244, 264, 356).
  - On the e2e path, `reviewed_head_is_current_for_labels` (line 100) makes yet another `pulls/{n}` GET for `.head.sha`.
- **Proposed fix:**
  - Move the line-221 fetch above line 154.
  - Get the e2e decision from `.labels[].name` in `_ORCH_PR_META_JSON`.
  - Give `reviewed_head_is_current_for_labels` an optional `.head.sha` argument taken from that same object, so the e2e branch makes no second read.
- **Safety rationale:**
  - The label read is paginated on purpose. The comments at lines 144-153 record a regression where a label on page 2 was dropped (PR #1823).
  - The read guards a race with the e2e gate, so changing it hits both the pagination trigger and the race-guard trigger.
- **Downstream signal:** do not implement automatically. A person must first confirm, from GitHub's REST docs or a live PR carrying more than 100 labels, that `GET /pulls/{n}` returns the PR's complete `labels` list without truncation. They must then accept that the e2e head-freshness check would use a read taken a few milliseconds earlier.

**MERGE-002** · `RISKY_SKIP` · `scripts/orchestrate_poll_process.sh:191`, `:304`, `:356-360`
- **Calls today:** 2 `rate_limit` reads per sweep-only run.
- **Calls after the change:** 1.
- **Endpoint:** `GET /rate_limit`.
- **Evidence:**
  - The sweep-only branch calls the two functions back to back:
    ```bash
    if [ "${RECLARIFY_REQUEUE_SWEEP_ONLY:-false}" = "true" ]; then
      replay_failed_reclarify_commands
      detect_unrouted_blocked_comments || true
    ```
  - Each function reads `.resources.core.remaining` itself (lines 191 and 304) and checks it against the same `500` floor.
  - `orchestrate_poll.yml:977` sets `RECLARIFY_REQUEUE_SWEEP_ONLY: "true"`.
- **Proposed fix:** take the reading once at line 356 and pass it to both functions as an optional argument. Keep the per-function read as a fallback when no argument is given.
- **Safety rationale:**
  - This is a rate-limit probe inside `orchestrate_poll_process.sh`.
  - The second reading is deliberately fresher: the first function can spend budget on a paginated comment read per issue (lines 202-245).
  - Per [GitHub's REST rate-limit docs](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api), `GET /rate_limit` does not count against the primary limit, so merging saves no quota, only one round trip.
- **Downstream signal:** do not implement. There is no quota benefit, and the change touches a rate-limit gate in the poller. Reconsider only if poll-stage timing (Speed item 3) shows this round trip matters.

**MERGE-003** · `RISKY_SKIP` · `scripts/tg_helpers.sh:434-448`
- **Calls today:** 2 per newest-page read.
- **Calls after the change:** 1, either through one GraphQL `issue{comments(last:100)}` query, or by reading page 1 with response headers and stopping when there is no `rel="last"` link (this only covers issues with 100 or fewer comments).
- **Endpoints:**
  - `GET /repos/{o}/{r}/issues/{n}`
  - `GET /repos/{o}/{r}/issues/{n}/comments?per_page=100&page={last}`
- **Evidence:** the header comment states the cost: "API calls: 2 (the issue read for its comment count, then the last page)" (line 428). The issue GET is used only for `.comments`, to work out `last_page` (lines 441-443).
- **Proposed fix:** either option above. GraphQL would also need the callers' REST-shape jq (`tg_trusted($login)` reads `.user.login` and `.id`) to be mapped across.
- **Safety rationale:**
  - It computes the page number by hand (`(total + 99) / 100`), which is a pagination trigger.
  - The function exists because of a past page-boundary bug: 447 duplicate `tg_cleanup` comments, documented at lines 424-427.
  - Any merge changes either the response shape or the page-boundary behavior.
- **Downstream signal:** manual review only. A reviewer must confirm that every caller of `_tg_newest_comments_json` accepts the new shape, and add a regression test for comment counts of exactly 100, 101 and 200.

**MERGE-004** · `RISKY_SKIP` · `.github/workflows/review_autofix.yml:765-782` (gate end) and `:800-812` (post-merge-validate-dispatch start), plus the other 11 `gh api rate_limit` budget steps in that file
- **Calls today:** one per job boundary.
- **Calls after the change:** one fewer per adjacent job pair, in theory.
- **Endpoint:** `GET /rate_limit`.
- **Evidence:** the gate job's end reading and the next job's start reading run moments apart, in sequence (`needs: gate`), and read the same counters.
- **Proposed fix:** none recommended.
- **Safety rationale:**
  - Merging crosses a job boundary: the budget files sit in each job's own `runner.temp`.
  - It would remove `GH_PAT_BUDGET phase=start|end … job=<name>` log lines that the budget telemetry parses.
  - The endpoint doesn't count against the primary quota.
- **Downstream signal:** do not implement. Listed only so later passes don't raise it again.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001** · `RISKY_SKIP` · `.github/workflows/implement.yml:2134-2141` ("Fetch issue metadata") → `:2480` ("Unblock judge bulk-delete override")
- **Calls today:** 2 `GET /user` reads per run, when an `override=bulk_delete -->` marker is present.
- **Calls after the change:** 1.
- **Endpoint:** `GET /user`.
- **Evidence:**
  - Line 2134 reads the PAT login and exports it after a shape check:
    ```bash
    echo "IMPLEMENT_SCOPE_PIPELINE_LOGIN=${pipeline_login}" >> "$GITHUB_ENV"
    ```
  - Both steps run in job `implement` under the same `if: env.SKIP_IMPLEMENT != 'true'` (lines 2080 and 2472) with the same `GH_TOKEN: ${{ secrets.GH_PAT }}`.
  - Line 2480 reads it again: `pipeline_login="$(gh api user --jq '.login' 2>/dev/null || true)"`.
  - The comment at lines 2131-2133 only rules out reuse in the other direction.
- **Proposed fix:** in the override step, use `pipeline_login="${IMPLEMENT_SCOPE_PIPELINE_LOGIN:-}"` and fall back to `gh api user` only when it is empty.
- **Safety rationale:**
  - This is an identity check that decides which comment authors may lift the bulk-delete guard, which is an authorization flow.
  - `GITHUB_ENV` can be rewritten by any of the roughly 10 steps between lines 2190 and 2417.
  - The live read is the stronger trust anchor.
- **Downstream signal:** manual security review only. Before relying on the `GITHUB_ENV` value, confirm that none of those steps can write `IMPLEMENT_SCOPE_PIPELINE_LOGIN` from content the branch controls. The saving is at most one call, and only on runs that carry a marker.

**REUSE-002** · `RISKY_SKIP` · `scripts/check_integration_pr_readiness.py:200-214` and `:216-223`; caller `scripts/orchestrate_poll_process.sh:4727-4733`
- **Calls today:** 2 identical `gh issue view <n> --json labels,body` calls. This happens when `--tracking-labels-file` is non-empty, the body file is missing or empty, and the file's labels don't include the tracking label.
- **Calls after the change:** 1.
- **Endpoint:** `GET /repos/{o}/{r}/issues/{n}`, through the gh CLI (`_fetch_issue`, lines 73-76).
- **Evidence:**
  - Line 201 runs `fetched_issue = _fetch_issue(args.repo, tracking_num)`. Lines 211-212 keep the file labels whenever they are non-empty, so the fresh labels already in hand are dropped.
  - Line 221 then makes the identical request again: `fresh_issue = _fetch_issue(args.repo, tracking_num)`.
  - Nothing is written between the two calls; the commit-status POSTs only happen after line 225.
- **Proposed fix:**
  - Keep `fetched_issue` in scope, initialised to `None`.
  - At line 216, when `fetched_issue` is not `None` and has labels, use them instead of calling `_fetch_issue` again.
  - Call `_fetch_issue` only when the body came from a file (so no fetch happened). This keeps the documented "keep file labels on failure" behaviour.
  - Extend `tests/test_check_integration_pr_readiness.py`.
- **Safety rationale:**
  - The re-read explicitly guards against a stale or racing poller label snapshot (comment at lines 217-220).
  - The script is called from `orchestrate_poll_process.sh`, so two hard triggers apply even though the reuse looks equivalent.
  - In practice the path is narrow: the poller always passes `--tracking-body-file`, and the PR workflow (`integration-pr-readiness.yml:75-79`) passes no labels file.
- **Downstream signal:** manual review. Confirm that `desired_body_file` at `orchestrate_poll_process.sh:4732` can actually be empty; if it can't, the duplicate never runs and the change isn't worth the risk. If it can, a reviewer must accept that the re-check uses a read taken milliseconds earlier in the same process.

**REUSE-003** · `NEEDS_VERIFICATION` · `scripts/unblock_judge.sh:502-505` (`unblock_check_triage_resolved`) and `:361-364` (`unblock_pr_check_green_condition`)
- **Calls today:** up to 2 `GET /repos/{o}/{r}` calls per run, only when `git symbolic-ref` fails both times.
- **Calls after the change:** at most 1.
- **Endpoint:** `GET /repos/{o}/{r}` (`--jq '.default_branch'`).
- **Evidence:**
  - Both functions use the same fallback:
    ```bash
    default_branch="$(git -C "${TARGET_DIR:-${SUPPORT_DIR}}" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
    if [ -z "${default_branch}" ]; then
    	default_branch="$(gh api "repos/${REPOSITORY}" --jq '.default_branch // ""' 2>/dev/null || true)"
    ```
  - The value isn't cached. `triage_resolution.json` (lines 356-358, 515) only caches the verdict, and only when the gate's `rc==0`.
- **Proposed fix:** add a script-level cache such as `UNBLOCK_DEFAULT_BRANCH_CACHE`, set by the first successful lookup (git or API) and read first by both functions. First check that the name isn't already used in the script (§10).
- **Safety rationale:** both reads use the same endpoint, field, token and no filters, and nothing in between changes them. However, the two calls are in different functions, and I could not confirm that `unblock_check_triage_resolved` always runs before `unblock_pr_check_green_condition`, or that `TARGET_DIR` is the same at both points.
- **Downstream signal:** before implementing:
  1. Trace where both functions are called in `unblock_judge.sh` and confirm the order and that `TARGET_DIR` and `SUPPORT_DIR` don't change in between.
  2. Confirm that caching an empty or failed result doesn't stop the second function from retrying when it should.
  3. Note the saving is at most 1 call per run, and only off the happy path.

### Dead Calls (DEAD-API-###)
No findings. I checked whether each fetched value is used in the identity reads (`plan.yml:3321/3428/3463`, `clarify.yml:804/860`, `workflow_failure_heal_report.sh:172/210`, `workflow_failure_heal_intake.sh:1305/2078/2252`), the post-write latch checks in `implement_handle_guard_block.sh`, and the PR reads in `review_enable_auto_merge.sh`, `review_rb_judge.sh` and `implement.yml:6518`. Every fetched value is used.

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP — the fix sits inside the retry loop of `gh_api_json_to_file` (`scripts/gh_helpers.sh:1475-1496`) and changes failure handling for all 33 callers; land it only through the DUP-006 shared failure handler, with tests.
- API-002: RISKY_SKIP — this is `curl_gh_api`'s retry loop, and its main capped caller is the `/user` identity read (`tg_helpers.sh:316-327`); remove that cap only after the 401/403/404 early return is tested.
- API-003: RISKY_SKIP — `_is_gh_permanent_failure` (`gh_helpers.sh:230`) is shared by every `gh_retry` caller, and the `already exists` stderr text is unverified; prefer the call-site pre-check in `ensure_label_exists` over widening the global regex.
- API-004: NEEDS_VERIFICATION — `--idempotent` adds retries rather than removing calls. The regex at `gh_helpers.sh:230` has no 409, so if GitHub answers 409 for a run that has already finished (the common case, per `cancel_on_pr_close.yml:383-385`), each such cancel would take 5 attempts and about 15 s of sleep. Confirm the status code first, and treat it as permanent for this call.
- BATCH-001: RISKY_SKIP — it is in `orchestrate_poll_process.sh` (`:8796-8817`), and `comments(last:100)` would replace a `--paginate` read; the duplicate-`/answer` marker check (`:8823-8828`) needs full comment history unless `totalCount` exceeds 100.
- BATCH-002: RISKY_SKIP — it is in the poller, the comment at `:186-188` asks for a live issue read on purpose, and the same function also holds the rate-limit read in MERGE-002 and a `/user` identity read (`:198`).
- BATCH-003: RISKY_SKIP — the per-hop comment read is `--paginate` and counts no-op markers anywhere in the history (`implement.yml:5695-5696`; the loop is `:5690-5703`, not `:5681`). `comments(last:100)` would undercount on long threads unless the REST fallback for `totalCount` over 100 is mandatory.

### Summary Counts
| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | REUSE-003 |
| RISKY_SKIP | 6 | MERGE-001, MERGE-002, MERGE-003, MERGE-004, REUSE-001, REUSE-002 |

### Implement-Stage Handoff
No SAFE_TO_MERGE findings in this pass.