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