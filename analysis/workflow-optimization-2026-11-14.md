## Executive Summary

**Input gaps that limit this report.** The run-logs directory named in the input (`/home/runner/work/_temp/workflow-log-output`) does not exist in this sandbox. I also found no `workflow_log_report.json` anywhere under `/home/runner/work`. Everything below comes from three sources: the `analysis_context` run rows, two `log_summary` fields (runs 43543897625 and 43543882433), and the workflow and script source in the repo. Statements about what happened inside a step are labelled as inference. `insufficient_data` is `false`. The window holds 999 runs, which looks like the run-listing cap. It covers about 7 hours, from 2026-11-13 19:18 to 2026-11-14 02:18 UTC.

- **About 6 hours of runner starvation, and it is worse than the previous report.** A group of runs created between 20:02 and 20:10 UTC on 11-13 only finished between about 22:18 and 02:13. Time waiting for a runner, not compute, set every slow duration:
  - **Lint:** `lint-pr-body-auto-close.yml` is one job with `timeout-minutes: 5` (line 47). Run 43498937594 took 13,022s, so at least 12,722s was queueing.
  - **Readiness:** `integration-pr-readiness.yml` is also one 5-minute job (line 44). Run 43506153482 took 13,183s.
  - **Promote follower:** `promote-candidate-follow.yml` has a 10-minute job. Run 43509590865 took 14,079s.
  - **CI:** the parallel jobs cap at 20 minutes, and `budget-watch` and `lint` add 5 each, so compute is at most about 1,800s. All 4 CI runs took 21,302–21,736s (43497871785, 43497880916, 43497957982, 43497967353), so at least about 19,500s per run was not compute.
  - **Compared with the previous report:** the 2026-11-13 report measured a backlog of about 1 hour with at least 852s of queueing. This window is about 6× longer.
  - Impact: high, because this is the critical path for every phase. Confidence: high that the queueing exists. Medium on its cause; capacity limits on GitHub-hosted runners are an inference.
- **The recommended fix from the previous report has not landed.** `ci.yml` has no top-level `concurrency:` block (I grepped; only job-level `timeout-minutes` match). Neither `integration-pr-readiness.yml` nor `lint-pr-body-auto-close.yml` has one, and the lint workflow still fires on `edited` (line 34). Every superseded PR push still uses about 8 runner slots. Impact: high. Confidence: high.
- **Token and cache telemetry reads zero by construction on Claude-engine runs.** `scripts/cost_audit.py:609-613` `build_run_cost_telemetry` copies only `RUN_COST_TELEMETRY_FIELDS` (lines 209-245). That list has no `claude_calls`, `claude_input_tokens`, `claude_output_tokens`, `claude_cache_write_tokens`, `claude_cache_read_tokens` or `claude_cost_usd`. `compute_cache_hit_rate` (lines 454-465) reads only `or_*` fields. So runs whose logs were downloaded and parsed still show 0 tokens and `cache_hit_rate: null`:
  - 43498912533 (review_autofix, 21,838s)
  - 43499081435 (plan, 13,986s)
  - 43499104648 (unblock judge, 13,544s)
  - Impact: high, because cost and cache analysis is blind. Confidence: high.
- **No successful poller tick appears in the window.** `orchestrate_poll` shows 42 runs, all `cancelled`, all at `poll / poll` → `cancelled_before_first_step`. Their pending time was p50 508s and p95 779s. That pattern is consistent with pending-slot replacement under `group: ai-orchestrate-poll-…` with `cancel-in-progress: false` (`orchestrate_poll.yml:101-102`). In plain terms, the active tick, plus the runner queue, outlasts the */5 cron. Impact: medium-high for orchestrator wave progress. Confidence: medium, because how successful runs were sampled is unclear (see the Metrics Appendix).
- **Almost no real AI work ran.** Of the 999 runs, 812 (81.3%) were `skipped` fan-out: clarify 178, plan 175, implement 176, orchestrate_clarify_respond 178, plus 40 check_failure_triage, 44 heal_intake and 19 heal runs. The only real AI-phase successes were plan ×2, clarify ×1, implement ×0 and review_autofix ×18. Failures: 0. Impact: medium. Confidence: high.
- **The 16 Semble fallbacks are healthy test output.** All are `semble_contract_test_fallbacks`: 4 per CI run across 4 CI runs. `semble_runtime_fallbacks` is 0, and Serena counters are all 0. This matches the 2026-11-13 classification. Confidence: high.

## Speed Optimizations

Ranked by expected latency reduction. Items 1 and 2 are on the critical path; items 3–5 are diagnostic or small wins.

1. **Add PR-scoped concurrency to CI, the readiness check and the PR-body lint.** Critical path; carried over from 2026-11-13 and still not done.
   - **Evidence:**
     - 4 CI runs were created in 36 seconds (20:02:51–20:03:27) and each waited about 5.4 hours or more.
     - In the same group were 6 or more readiness runs (for example 43497880926, 43498516105, 43498668816, 43498911738, 43498937401) and lint runs (43497880836, 43498516096, 43498668886, 43498911769, 43498937594). Each pair was created at the same second, which suggests one push or label event per PR.
   - **Root cause:** every `synchronize`, `edited` or `labeled` event queues a full run even when a newer run for the same PR is already queued. Stale runs compete with live ones for the account's runner slots.
   - **Exact change** (2-space YAML; push CI stays one group per run, so push behaviour on main, stable and integration branches does not change):
     ```yaml
     # ci.yml (top level)
     concurrency:
       group: ${{ github.event_name == 'pull_request' && format('ci-pr-{0}', github.event.pull_request.number) || format('ci-run-{0}', github.run_id) }}
       cancel-in-progress: ${{ github.event_name == 'pull_request' }}
     # integration-pr-readiness.yml / lint-pr-body-auto-close.yml (top level)
     concurrency:
       group: ${{ github.workflow }}-pr-${{ github.event.pull_request.number }}
       cancel-in-progress: true
     ```
   - **Estimated savings:** each superseded CI run that gets cancelled frees about 8 runner slots. In this kind of backlog that should remove hours of queue, not minutes (inference; item 2 measures it).
   - **Risk:** low to medium. Before rollout, confirm that `internal-check-failure-triage` and heal intake do not treat a `cancelled` PR CI conclusion as a failure to act on. Rollback is deleting the block.

2. **Log queue wait instead of inferring it.** Diagnostic, enables item 1.
   - **Evidence:** `scripts/ci_budget_watch.py:94` computes only `_seconds(job["started_at"], job["completed_at"])`. Grepping for `QUEUE_WAIT` finds it only in `workflow_failure_heal_queue.sh` and `workflow_failure_heal_intake.sh`. CI and the collector emit nothing.
   - **Exact change:** in `ci_budget_watch.py`, reuse the jobs list it already fetches (no new API call) and print one line per job: `CI_QUEUE_WAIT: run_id=<id> job=<name> queued_s=<started_at - created_at>`. In the log collector, add a `queue_seconds` field to each run row computed from the per-job `created_at`/`started_at` it already receives when `jobs_fetch_status == success`. Default it to `null` when absent so it fails open.
   - **Savings:** none directly. It turns lower bounds like "at least 12,722s" into measured values.
   - **Risk:** very low.

3. **Check the cron load on the shared runner pool.** Inference; needs item 2's data first.
   - **Evidence:** `internal-cancel-on-pr-close.yml:6-7` runs `*/5`, `orchestrate_poll` runs `*/5`, and `promote-candidate-follow.yml:17` runs hourly. Every one of these takes a runner slot from the pool PR runs need.
   - **Exact change:** none until queue data exists. If `CI_QUEUE_WAIT` shows sustained queueing of 10 minutes or more, move the cancel-on-close scheduled sweep to `*/15`. PR-close events keep their own per-PR group (`cancel_on_pr_close.yml:22`), so close handling does not slow down.
   - **Risk:** low.

4. **Find out why review_autofix runs end as `skipped` after a long time.**
   - **Evidence:** run 43528572594 was `skipped` after 3,866s and 43541695927 after 2,154s.
   - **Root cause:** inference. They probably sat pending in the `internal-review-{event}-{ref}` group (`internal-review.yml:50`) before their job `if:` evaluated false. That holds a pending slot that could have gone to a real event.
   - **Exact change:** diagnostic only. Have the collector record the `event` field for each run so these can be attributed to a trigger.
   - **Risk:** none.

5. **Runner queue on short scheduled jobs.** Micro.
   - **Evidence:** per its `log_summary`, Security Audit 43543882433 waited about 4m17s of its 583s total. Per its `log_summary`, review sweep 43543897625 waited about 38s of its 81s total. Both quote "All GitHub-hosted runners with label [ubuntu-24.04] are busy".
   - **Fix:** this goes away once items 1 and 3 land. No separate change.

## Cost Optimizations

1. **Make Claude token and cost usage visible in the per-run projection.** This is a prerequisite for any cost decision.
   - **Evidence:**
     - Every family shows `codex_tokens_used=0`, `or_total_tokens=0` and `cache_hit_rate=null`, including runs where `log_download_status=success` and `log_parsed=true` (43498912533, 43499081435, 43499104648, 43498668976, 43498516433).
     - `cost_audit.py:128-134` defines `CLAUDE_COUNT_KEYS`, and `parse_log` aggregates them (around lines 1042-1045). But `build_run_cost_telemetry` (line 611) copies only `RUN_COST_TELEMETRY_FIELDS`, which excludes them, and `aggregate_run_cost_telemetry` sums only `AGGREGATABLE_COST_FIELDS`.
   - **Root cause:** a projection gap in the collector's per-run telemetry.
   - **Exact change** (additive; existing keys are unchanged per §10):
     - Append `*CLAUDE_COUNT_KEYS` to both `RUN_COST_TELEMETRY_FIELDS` and `AGGREGATABLE_COST_FIELDS`.
     - Add `claude_cost_usd` to the run projection and sum it as a float in `aggregate_run_cost_telemetry`, because `_to_int` would truncate it.
     - Have `compute_cache_hit_rate` fall back to `claude_cache_read_tokens / (claude_input_tokens + claude_cache_write_tokens + claude_cache_read_tokens)` when the `or_*` denominator is 0.
   - **Estimated savings:** none directly. It makes model, reasoning-level and cache changes measurable.
   - **Quality risk:** none. Report-only.

2. **Stop paying for superseded PR validation.** Same change as Speed item 1.
   - **Evidence:** 4 CI runs (about 8 jobs each), 13 readiness runs and 12 lint runs in about 7 hours, with clustered same-second creations.
   - **Savings:** inference. Every superseded CI run cancelled early avoids up to about 8 jobs × 20 minutes of runner time.

3. **Stop wasting compute on review_autofix runs that are later cancelled.**
   - **Evidence:** 14 of 34 review_autofix runs were `cancelled`. Their `failure_point` is null, not `cancelled_before_first_step`, which suggests they started work:
     - [pr:9455] 43516105320, 12,805s
     - [pr:9356] 43516115787, 12,470s
     - [pr:9272] 43523714953, 10,124s
     - [pr:9451] 43493941336, 9,348s
     - [pr:9470] 43492383798, 5,016s
     - [pr:9283] 43499213364, 2,366s
     - [pr:9359] 43543991501, 512s
   - **Root cause:** inference. Probably cancellation by the PR-close sweep after the PR merged or closed. Any model work already done in those runs is lost.
   - **Change:** diagnostic first. Log the cancel reason and the PR state in `cancel_on_pr_close.yml` when it cancels a review run, for example `CANCEL_ON_CLOSE: run_id=… pr=… pr_state=… reason=…`, so wasted Claude spend can be attributed once item 1 lands.

4. **Semble and Serena.** There were 0 `SEMBLE_QUERY` calls, 0 bytes, 0 bootstraps and 0 Serena calls or probes. I can't assess whether Semble is reducing prompt expansion or whether Serena is replacing tool calls, because no AI-phase logs with those lines were collected. No change recommended.

## Reliability Improvements

1. **Poller throughput** (0 successful ticks visible out of 42 rows).
   - **Failure evidence:**
     - 42 of 42 `orchestrate_poll` runs ended `cancelled_before_first_step`. The longest pending times were 43534729558 (902s), 43530851417 (810s), 43537239053 (780s), 43523065339 (763s), 43543199631 (734s) and 43540432644 (721s).
     - The poll job has no `timeout-minutes`; the only one in `orchestrate_poll.yml` is the daily-usage job's 15 at line 2229.
   - **Root cause:** capacity or queueing (inference). Either long active ticks or runner starvation keeps the single pending slot turning over.
   - **Exact fix:** first add a tick-duration line, which does not exist today (grepping shows only `POLL_FOLLOW_UP` lines). Record the start epoch in the first step and, in a final `if: always()` step, emit `POLL_TICK: run_id=<id> elapsed_s=<n> has_work=<bool> outcome=<…>`. Do not add `timeout-minutes` to the poll job yet. The comment at lines 98-100 warns that runs may be in the middle of conflict healing, and killing them is riskier than the queueing.
   - **Expected impact:** this lets the next report separate "ticks too long" from "no runner".
   - **Fail-open:** the step should use `continue-on-error: true`.

2. **Cancel-on-close: 36 of 39 runs cancelled.**
   - **Evidence:**
     - 36 cancelled, all at `cancel / cancel-active-runs` → `cancelled_before_first_step`. Examples: 43543664039 (1,157s), 43537404927 (1,025s), 43534732406 (949s), 43496882161 (939s).
     - The schedule sweep shares one repository group (`cancel_on_pr_close.yml:22`), while PR-close events get per-PR groups.
   - **Root cause:** inference. Scheduled sweeps replace each other while waiting for a runner. This is harmless in effect: cancel POSTs are idempotent, per the comment at lines 20-21.
   - **Fix:** add `event` to collector run rows so schedule cancellations can be filtered out of the cancellation rate. Then apply Speed item 3 if needed.
   - **Rollback:** not applicable.

3. **Promote-candidate follower: 9 of 10 cancelled.**
   - **Evidence:** 43526168385 (3,963s), 43491027634 (3,648s), 43518260511 (3,594s), 43539843841 (2,672s) and others. The single success, 43509590865, took 14,079s against a 10-minute job timeout.
   - **Root cause:** pending-slot replacement under `promote-candidate-follow-${repo}` with `cancel-in-progress: false` (lines 23-25). The script is a fast-forward, so the newest run supersedes older ones safely. The long pending times are the same runner starvation.
   - **Fix:** none needed beyond Speed items 1 and 3.
   - **Indicator to watch:** the gap between `main` and `promote-candidate`.

4. **`BREAK_GLASS` / `CONTEXT_BUDGET_WARN`:** 0 and 0 everywhere. These are not interpretable while AI-phase logs and the Claude fields are missing.

5. **Semble fallbacks:** 16, all `context=contract-test` (`_is_contract_test_semble_line`, `cost_audit.py:381-382`), 4 in each of CI runs 43497967353, 43497880916, 43497957982 and 43497871785. Runtime fallbacks: 0. This is healthy fixture output, not a masked rollout failure. Serena probes: ok 0, failed 0, skipped 0, so there is no availability signal.

## AI Memory Health

There were no `AI_MEMORY_TELEMETRY:` lines to analyse. No deep-dive excerpts are mounted, and the two `log_summary` rows state that AI_MEMORY lines are not visible in their retained portion: 43543897625 had about 89k chars truncated and 43543882433 had 722,892 chars truncated. I can't compute retrieve hit rate, `estimated_tokens` against budget, the `keyword_method` mix, `fail_open` or `enabled: false` counts.

Recommendations:
- Make the collector choose deep-dive runs only where the conclusion is not `skipped`. All 15 `recent/` selections here were skipped fan-out with `empty_archive` (for example 43545552173, 43545552250, 43544794946), so they carry no memory telemetry.
- Have the `log_summary` prompt grep and quote `AI_MEMORY_TELEMETRY` lines before truncating, so they survive large logs.

## GH API Call Audit

The input has no call counts, hotspot or rate-limit data, so these are design-level observations, not measured volumes.
- **Skipped fan-out:** 707 skipped runs across clarify, plan, implement and clarify-respond, plus 103 skipped triage and heal runs. These use no runner, but they flood the run list. They also consume the collector's run-list budget: 999 rows covered only about 7 hours. Paginating past them costs extra `actions/runs` calls (inference). Fix: filter with `status=completed` and exclude `conclusion=skipped` on the collector side, or query by workflow ID for the non-fan-out families.
- **Cancelled-run job fetches:** each cancelled run with `jobs_fetch_status=success` (103 rows) costs one jobs API call that only confirms `cancelled_before_first_step`. Fix: skip the jobs fetch when the run's `run_started_at` equals `updated_at` within a few seconds, or when it carries the pending-replacement pattern (inference about the collector's behaviour; verify in the collector before changing it).
- **Poller cache helpers:** `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql` remain the batching model under §14. Nothing in this window shows a regression, and none can be shown without `POLL_TICK`/API counters. Next collection step: emit `GH_API_CALLS: phase=… count=…` at the end of the poll tick, using the existing `gh_retry` wrappers' counters if they exist.

## Prompt Cache & Memory System

- `cache_hit_rate` is `null` in every family because `compute_cache_hit_rate` reads only OpenRouter fields and all of them are 0. Claude `cache_read_input_tokens` are parsed (lines 125-126) but never reach the rate or the run projection (Cost item 1). The prompt-cache hit rate is therefore **unknown**, not 0.
- The only cache evidence is unrelated to prompts: run 43543882433 restored the npm cache ("Cache hit occurred on the primary key claude-cli-npm-Linux-ccf58c68…", 153 ms).
- With 0 `CONTEXT_BUDGET_WARN` and no token data, I can't assess cache fragmentation such as unstable prefixes or dynamic noise. After Cost item 1 lands, compare `claude_cache_read_tokens/claude_input_tokens` per phase. Phases with high write and low read are the candidates for moving volatile content (issue body, run IDs, timestamps) after the static prefix.

## Orchestrator Health

- **Wave progression:** not observable. 0 successful `orchestrate_poll` rows, 42 cancelled pending runs (p50 508s), and every clarify, plan, implement and clarify-respond dispatch in the `recent_runs` sample was `skipped`.
- **Real phase throughput:** clarify 1 (43528560661, 8,858s), plan 2 (43499081435, 13,986s; 43498059262, 9,948s), implement 0, and unblock judge 1 (43499104648, 13,544s). All of their durations are dominated by the 20:00–02:00 backlog (inference).
- **Heal loop:** workflow_failure_heal_intake had 10 successes with p95 12,534s (for example 43513512766, 13,160s), and workflow_failure_heal had 1 success (43498934102, 12,857s). These are consistent with the same backlog; there were no heal failures.
- **Indicators to track:** `POLL_TICK elapsed_s` (new), `POLL_FOLLOW_UP outcome=` counts (existing), poll success ratio per hour, and the number of skipped versus real dispatches per tracking issue.
- **Smallest safe mitigation:** add the poll-tick instrumentation (Reliability item 1) and reduce runner contention (Speed item 1). Do not change the poll concurrency semantics.

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence | Fix, in order of end-to-end impact |
|---|---|---|---|
| PR validation (CI, readiness, lint) | **Queueing** | CI at least 19,500s of non-compute per run; lint and readiness at least 12,700s each against 5-minute timeouts | Speed item 1 (concurrency), Speed item 2 (measure queue) |
| review/autofix | Queueing, then cancellation waste | p50 1,573.5s, p95 15,605s; 14 cancelled after up to 12,805s | Speed item 1; Cost item 3 (attribute cancellations) |
| clarify → plan → implement | Queueing; low real volume | 3 real runs out of 710; plan runs of 9,948s and 13,986s | Flows from fixing the queue |
| orchestrate/poll | Pending-slot turnover (queue or long ticks) | 42 of 42 cancelled before the first step | Reliability item 1 (`POLL_TICK`) |
| merge/conflict | Not observable | No conflict-heal or integration logs | Collect integration-branch logs next window |
| Compute | Not the limiter | Every slow run exceeds its own job-timeout ceiling | — |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows
- **Top bottleneck:** a GitHub-hosted runner backlog of about 6 hours (2026-11-13 20:00 → 11-14 02:10), shown by run durations of 13,000–21,800s on workflows whose jobs are capped at 5–30 minutes.
- **Top failure modes:** zero hard failures. Cancellations: poll 42/42, cancel-on-close 36/39, review_autofix 14/34, promote follower 9/10, historical triage 2/3. Most are pending-slot replacement (inference), not errors.
- **Highest-cost drivers:** unknown, because Claude token fields are dropped from the projection. Likely drivers (inference): superseded CI runs, plus review_autofix runs cancelled after hours of work.
- **Top 3 actions:**
  1. Add PR-scoped `concurrency` with `cancel-in-progress` to `ci.yml`, `integration-pr-readiness.yml` and `lint-pr-body-auto-close.yml`.
  2. Add the `claude_*` fields and `claude_cost_usd` to `RUN_COST_TELEMETRY_FIELDS`/`AGGREGATABLE_COST_FIELDS`, with a Claude fallback in `compute_cache_hit_rate`.
  3. Emit `CI_QUEUE_WAIT` from `ci_budget_watch.py` and `POLL_TICK` from the poll job, and add `event` and `queue_seconds` to collector run rows.

## Metrics Appendix

**Run outcomes (window of 999 runs, about 7h)**

| Metric | Value |
|---|---|
| Total runs | 999 |
| Success | 84 (8.4%) |
| Failure | 0 (0.0%) |
| Cancelled | 103 (10.3%) |
| Other / skipped | 812 (81.3%) |
| p50 / p95 / avg duration | 1s / 8,945s / 851s |
| `success_sample_rate` / `sampled_success_runs` | 0.07 / 5. Their meaning is unclear: 13 runs show `log_download_status=success`. Treat success counts per family as possibly sampled. |

**Per family**

| Family | Runs | Success | Cancelled | Skipped | p50 s | p95 s |
|---|---|---|---|---|---|---|
| ci | 4 | 4 | 0 | 0 | 21,615.5 | 21,729.6 |
| review_autofix | 34 | 18 | 14 | 2 | 1,573.5 | 15,605.3 |
| integration_pr_readiness | 13 | 13 | 0 | 0 | 11,535 | 13,087.6 |
| lint_pr_body_auto_close | 12 | 12 | 0 | 0 | 11,668.5 | 12,724.5 |
| orchestrate_poll | 42 | 0 | 42 | 0 | 508 | 779.2 |
| cancel_on_pr_close | 39 | 3 | 36 | 0 | 552 | 9,302.4 |
| promote_candidate_follow | 10 | 1 | 9 | 0 | 2,433.5 | 9,526.8 |
| security_audit | 16 | 16 | 0 | 0 | 883 | 4,087 |
| workflow_failure_heal_intake | 54 | 10 | 0 | 44 | 4.5 | 12,534.1 |
| workflow_failure_heal | 20 | 1 | 0 | 19 | 1 | 649.5 |
| check_failure_triage | 40 | 0 | 0 | 40 | 1 | 9.1 |
| check_failure_triage_historical | 3 | 1 | 2 | 0 | 3,873 | 12,135.9 |
| clarify | 179 | 1 | 0 | 178 | 1 | 10 |
| plan | 177 | 2 | 0 | 175 | 1 | 10 |
| implement | 176 | 0 | 0 | 176 | 1 | 10 |
| orchestrate_clarify_respond | 178 | 0 | 0 | 178 | 1 | 10 |
| unblock_judge_dispatch | 1 | 1 | 0 | 0 | 13,544 | 13,544 |
| issue_pr_status | 1 | 1 | 0 | 0 | 8,848 | 8,848 |

**Queueing lower bounds (duration minus the workflow's maximum compute)**

| Run | Workflow | Duration s | Compute cap s | Queue at least s |
|---|---|---|---|---|
| 43497967353 | CI | 21,736 | about 1,800 | about 19,936 |
| 43509590865 | Promote follower | 14,079 | 600 | 13,479 |
| 43506153482 | Integration readiness | 13,183 | 300 | 12,883 |
| 43498937594 | PR-body lint | 13,022 | 300 | 12,722 |
| 43543882433 | Security Audit | 583 | — | 257 (measured, from `log_summary`) |
| 43543897625 | Review sweep | 81 | — | 38 (measured, from `log_summary`) |

**Cost, cache and review telemetry (repo total, 115 runs with parsed logs)**

| Metric | Value |
|---|---|
| codex_tokens_used / codex_calls | 0 / 0 |
| or_total_tokens / or_calls | 0 / 0 |
| claude_* tokens / claude_cost_usd | **not projected** (gap: `cost_audit.py:209-245`, `609-613`) |
| cache_hit_rate | null (OpenRouter-only formula, `cost_audit.py:454-465`) |
| wall_clock_p50_ms / wall_clock_p99_ms | 1,000 / 21,732,990 (108 samples) |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |
| GH API calls / rate-limit events | not collected |

**Semble and Serena**

| Metric | Value |
|---|---|
| semble_query_calls / bytes | 0 / 0 |
| semble_fallbacks (contract-test / runtime) | 16 (16 / 0), all in CI: 4 each in 43497967353, 43497880916, 43497957982, 43497871785 |
| semble_bootstraps (failed / unused / ms) | 0 (0 / 0 / 0) |
| semble_sources_total / static_dup_bytes / echo_lines_dropped | 0 / 0 / 0 |
| serena_query_calls / tool_calls / response_bytes / ms | 0 / 0 / 0 / 0 |
| serena_fallbacks | 0 |

**MCP availability per target**

| Target | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| (no `SERENA_PROBE` targets observed) | 0 | 0 | 0 |

Other MCP servers observed: none.

**Data gaps and next collection steps**
1. Mount the run-logs directory. It was missing in this run and in the 2026-11-13 run.
2. Project the `claude_*` fields.
3. Add `queue_seconds` and `event` to run rows.
4. Choose deep-dive logs from non-skipped runs.
5. Emit `POLL_TICK` and `CI_QUEUE_WAIT`.
6. Clarify what `success_sample_rate` means in the analysis context.
## Deep Audit — Workflows & Scripts (2026-11-14)

Scope and method. This pass goes deep on the PR-gate workflows (`ci.yml`, `integration-pr-readiness.yml`, `lint-pr-body-auto-close.yml`) and the close/cleanup paths (`cancel_on_pr_close.yml`, `internal-cancel-on-pr-close.yml`). It also covers `promote-candidate-follow.yml`, selected steps of `orchestrate_poll.yml`, `implement.yml`, `review_autofix.yml`, `test-and-mark-stable.yml` and `update_workflows.yml`, and the scripts `gh_helpers.sh`, `label_helpers.sh`, `tg_helpers.sh`, `promote_candidate_follow.sh`, `workflow_failure_heal_pr_reconcile.sh`, `workflow_failure_heal_recovery_scan.sh`, `comprehensive_test_and_release_gh_api.sh`, `stage_workflow_support.sh` and parts of `orchestrate_poll_process.sh`.

Byte and character counts are estimates from line counts, because this audit had no shell; each one is marked [NEEDS VERIFICATION].

Cross-references to the report above, not repeated here:
- PR-scoped `concurrency:` for CI, readiness and lint is Speed item 1.
- The `cost_audit.py` Claude-field projection gap is Cost item 1.

No `TODO`/`FIXME`/`HACK`/`XXX` markers exist in `.github/workflows/*.yml`, `scripts/*.sh` or `scripts/*.py`.

### Section 1: Bug & Correctness Sweep

**BUG-001** — `scripts/orchestrate_poll_process.sh:26268-26280`, `26380-26390` — Severity: **High** — Category: `bug`
- **Description:** Stall recovery reads the wrong end of the comment history on long issues.
  - `extract_recommended_answers` (line 26277-26279) and `stall_recovery_clarify_answer` (line 26387-26389) fetch `issues/<n>/comments?sort=created&direction=desc&per_page=50` with no pagination.
  - The docstring at 26380-26381 says this returns "the 50 newest", and the comment at 26283 says "Search from newest to oldest (direction=desc)".
  - The per-issue comment endpoint only accepts `since`, `per_page` and `page` ([GitHub Docs: REST API endpoints for issue comments](https://docs.github.com/en/rest/issues/comments)), so `sort` and `direction` are ignored and the call returns the **oldest** 50 comments.
  - The repo already recorded this exact failure in `scripts/tg_helpers.sh:424-427` (447 `tg_cleanup` comments on tele-funtoken-msg-scoring#3928).
  - **Impact:** on any issue with more than 50 comments, `stall_recovery_clarify_answer` picks the newest trusted clarification-questions comment *within the oldest 50* (`max_by(sort_key)`, 26398-26399). It can then re-assert a stale `/answer` or fall back to stale RECOMMENDED letters for questions that have since been superseded. The answer gets posted automatically, so this changes pipeline state.
- **Recommended fix:** read the newest page the same way `_tg_newest_comments_json` does (`scripts/tg_helpers.sh:434-448`: one issue read for `.comments`, then `page=ceil(total/100)`). Alternatively use `--paginate … | jq -s 'add // []'` as `read_standalone_state_json` does (18681), keeping the existing `max_by` selection. Drop the misleading `sort`/`direction` parameters. Keep fail-open semantics: a failed read still yields `none`.

**BUG-002** — `.github/workflows/integration-pr-readiness.yml:25-33`, `56-79`; `scripts/check_integration_pr_readiness.py:190`, `233`, `247` — Severity: **Medium** — Category: `bug`
- **Description:** The readiness status can be decided by a stale label snapshot.
  - The override decision uses PR labels from the event payload (`toJSON(github.event.pull_request.labels)`, line 58), written to a file and read at `check_integration_pr_readiness.py:190`.
  - The workflow fires on `labeled`/`unlabeled`/`synchronize` with no concurrency group. Each run posts a commit status for `HEAD_SHA`, and the last writer wins.
  - In the multi-hour runner backlog the report measured (readiness run 43506153482: 13,183s), runs can finish out of order. A `labeled ai:override-incomplete-merge` run that finishes *after* the later `unlabeled` run posts `success`, so the merge gate opens after the operator removed the override.
  - That a reversal actually happened is an inference; nothing in the code prevents one.
- **Recommended fix:**
  - Re-read live PR labels inside the script before deciding. One `gh pr view <n> --json labels` call; the script already shells out to `gh issue view` at line 74. Fail closed (post `failure`) when that read fails.
  - Add the per-PR `concurrency:` block from Speed item 1 with `cancel-in-progress: true`, so only the newest event's run posts a status.

**SEC-001** — `.github/workflows/integration-pr-readiness.yml:51-79`; `.github/workflows/lint-pr-body-auto-close.yml:49-56`, `100-115` — Severity: **Medium** — Category: `security`
- **Description:** Both required-check workflows run the gate script from the PR's own checkout. `actions/checkout@v5` with no `ref:` checks out the pull-request merge ref on `pull_request`.
  - Readiness runs `scripts/check_integration_pr_readiness.py` with `statuses: write` (line 39).
  - The lint runs `scripts/lint_pr_body_auto_close.py` (line 112).
  - A same-repository PR that edits either script (including an AI implement run on an `orchestrator/project-*` integration PR, which is exactly the PR the readiness gate protects) is judged by its own modified copy. It can post `success` or skip the lint.
  - The rest of the pipeline already avoids running PR-controlled support code (the `.codex-workflow-src` trusted checkout in `cancel_on_pr_close.yml:76-111`; "Runs only the copy in the trusted support checkout, never a PR-head script" at `orchestrate_poll.yml:2196-2198`).
  - [NEEDS VERIFICATION] whether branch rules or CODEOWNERS already block edits to these two scripts.
- **Recommended fix:** add a second checkout of the base commit and run the script from there:
  ```yaml
  - name: Checkout trusted gate script (base commit)
    uses: actions/checkout@v5
    with:
      ref: ${{ github.event.pull_request.base.sha }}
      path: .trusted-gate-src
      fetch-depth: 1
      persist-credentials: false
  ```
  Then call `python3 .trusted-gate-src/scripts/check_integration_pr_readiness.py` (and the same for the lint). Keep the PR checkout only for data the gate reads.

**SEC-002** — `.github/workflows/update_workflows.yml:614-632`, `936-947`, `1849`, `1851`; `scripts/apply_audit_gate_assets.py:255` — Severity: **Medium** — Category: `security`
- **Description:** Free-text step outputs are interpolated straight into double-quoted shell assignments in the summary step: `AUDIT_GATE_MESSAGE="${{ steps.audit_gate.outputs.message }}"` (1849) and `FAILURE_REASON_DETAIL="${{ steps.update.outputs.failure_reason_detail }}"` (1851).
  - `message` comes from `apply_audit_gate_assets.py` and can be `str(exc)` (line 255), an exception text shaped by consumer `package.json` content.
  - `failure_reason_detail` embeds `${SELECTED_PROFILE}`, file paths and `${UPSTREAM_DIR}` (e.g. line 961).
  - A `"`, a backtick or `$(` in either value breaks the script or executes as command substitution.
  - Line 632 also writes `message` to `$GITHUB_OUTPUT` with no newline sanitisation. A multi-line exception text could inject extra `key=value` outputs. `fail_with_reason` sanitises CR/LF at 939-940; the audit-gate step does not.
- **Recommended fix:**
  - Pass these through the step's `env:` block, as the same step already does for `COMMIT_PR_URL` and the like at 1825-1829.
  - At line 632, strip CR/LF the same way as 939-940, or write the value with a random heredoc delimiter (`message<<EOF_<rand>`).

**BUG-003** — `.github/workflows/implement.yml:5757-5758` — Severity: **Medium** — Category: `bug`
- **Description:** In the no-op fall-through of "Handle no-op implementation" (step `set -euo pipefail`, line 5545), `gh_retry gh api ".../issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` has no failure guard.
  - `-f` with no method is treated as a non-idempotent POST and gets one attempt (`_gh_api_args_unsafe_post`, `scripts/gh_helpers.sh:706-778`, `_gh_cmd_is_unsafe_post` 840-851).
  - The 2,500-comment cap is classified as permanent (`gh_helpers.sh:217-218`).
  - Any single failure exits the step before the `ai:implementation-failed` label transition (5763-5774). A plain no-op is then reported as a job failure, and the `failure()`-gated steps (from 6790 onward) take over. [NEEDS VERIFICATION] of the exact labels those steps leave.
  - The sibling "Mark issue done" step guards the same call: `|| echo "::warning::…"` at 6738.
- **Recommended fix:** add `>/dev/null 2>&1 || echo "::warning::Could not post the no-op comment on #${ISSUE_NUMBER}; labelling anyway."` so the label transition always runs.

**BUG-004** — `.github/workflows/implement.yml:5771-5774` (compare `5584-5588`, `5719-5723`, `292-295`) — Severity: **Medium** — Category: `bug`
- **Description:** The no-op fall-through removes only `ai:done` and adds `ai:implementation-failed`. Guard 0 (5585) and Guard 2 (5720) also remove `ai:implementing`.
  - "Validate approval phase label" set `ai:implementing` at 2413-2414.
  - The phase precheck skips every later implement run while `ai:implementing` is present (292-295: "Another implement run is in progress. Skipping.").
  - So a standalone issue that ends here cannot be re-approved until a person removes the label. The poller's re-issue path may clean this up for orchestrator-managed issues [NEEDS VERIFICATION].
- **Recommended fix:** add `--remove-label 'ai:implementing'` to both `gh issue edit` calls at 5771-5774. That matches Guards 0 and 2 and the "Mark issue done" step (6736).

**BUG-005** — `.github/workflows/test-and-mark-stable.yml:986`, `1188`, `1681`, `2709`; `scripts/stable_e2e_step_wait_review.sh:464`; `scripts/comprehensive_test_and_release_gh_api.sh:109-133` — Severity: **Low** — Category: `bug`
- **Description:** The rate-limit guard `if [ "$RATE_LIMIT_BACKOFF" -gt 0 ] && [ -z "$LABELS" ]; then continue; fi` can never fire.
  - `gh_api_safe` updates `RATE_LIMIT_BACKOFF` inside `$(gh_api_safe_print …)` (for example `LABELS=$(gh_api_safe_print …)`, 1184-1185). That is a subshell, so the parent keeps the 0 it set at 914, 1075, 1409 and 2559.
  - A rate-limited empty read therefore flows through as `labels=` and changes `CUR_STATE` (1213-1223), which resets the inactivity timer. That is the "corrupt state" the comment at 1187 says it prevents.
- **Recommended fix:** call `gh_api_safe` in the parent shell (it already stores the body in `GH_API_SAFE_OUTPUT`, lines 97-118), then branch on its return code, for example `if ! gh_api_safe …; then sleep "$POLL_INTERVAL"; continue; fi`.

**BUG-006** — `.github/workflows/test-and-mark-stable.yml:1022-1031`, `1192-1195` — Severity: **Low** — Category: `bug`
- **Description:** The clarify-phase failure check reads `comments?per_page=5&direction=desc` and filters `.user.login == "github-actions[bot]"`.
  - Per BUG-001, `direction` is ignored, so the call returns the **oldest** 5 comments.
  - Pipeline comments are posted with GH_PAT under a human login (documented at `orchestrate_poll_process.sh:26286-26288`), so the login filter also misses them.
  - The "clarification needed" fast-fail therefore never triggers, and the phase relies on the inactivity timeout.
  - The plan-phase activity signal at 1192 has the same `direction` problem for issues with more than 100 comments.
- **Recommended fix:** use the newest-page pattern (`tg_helpers.sh:434-448`). Match on the comment marker (`<!-- ai:clarification-questions -->`) and a trusted `author_association`, as `orchestrate_poll_process.sh:26294-26300` does, instead of the bot login.

**BUG-007** — `scripts/promote_candidate_follow.sh:187-199`, `245-252`; `.github/workflows/promote-candidate-follow.yml:13-25` — Severity: **Low** — Category: `bug`
- **Description:** The "one CRITICAL alert per UTC day" for an unprotected `promote-candidate` branch fires only when a run with `GITHUB_EVENT_NAME == schedule` *starts* in UTC hour `00`. With cron `17 * * * *` that is exactly one candidate run per day, and two things can drop it:
  - **Pending-slot replacement:** the group `promote-candidate-follow-${repo}` with `cancel-in-progress: false` replaces the pending 00:17 run whenever a `push` run queues behind the active one. The report shows 9 of 10 runs cancelled.
  - **Queue delay:** the 00:17 run starts after 01:00 when the queue is long. The report shows 14,079s on run 43509590865.
  - Either way, that day's alert is lost silently.
- **Recommended fix:** gate on a per-UTC-date marker instead of the hour. Reuse the `actions/cache` marker pattern of the Claude pool daily report (`orchestrate_poll.yml:1320-1330`, `2278-2290`) with key `promote-candidate-unprotected-<YYYY-MM-DD>`, and alert on the first run of any event type that misses the marker.

**BUG-008** — `scripts/workflow_failure_heal_pr_reconcile.sh:117-136` — Severity: **Low** — Category: `bug`
- **Description:** `_close_heal` posts "Closing without merging: …" on the heal PR (120-123) *before* `PATCH state=closed` (124). If the PATCH fails, the function returns, leaving an open PR whose newest comment says it was closed.
  - This script runs only on the source PR's `closed` event (`internal-cancel-on-pr-close.yml:19-54`), so nothing retries it.
- **Recommended fix:** PATCH first, comment only on success. On failure, emit `::warning::WORKFLOW_HEAL_PR_RECONCILE heal_pr_close_failed …` (the `log` line already exists) and post no closing text.

**SHELL-001** — `.github/workflows/ci.yml:438-440`, `478-480`, `509-511` — Severity: **Low** — Category: `shellcheck`
- **Description:**
  - The `A && echo "OK" || { echo "FAIL"; exit 1; }` form is SC2015: the failure branch also runs when `echo` fails.
  - Line 439 interpolates the shell variable into Python source (`json.load(open('$f'))`), so a file name containing `'` breaks the check. File names are repo-controlled, so the risk is low.
- **Recommended fix:** `if ! python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "$f"; then echo "FAIL: $f"; exit 1; fi; echo "OK: $f"`. Apply the same `if !` form to the `bash -n` and `shellcheck` loops.

### Section 2: GitHub API Call Redundancy Audit

**API-001** — `.github/workflows/cancel_on_pr_close.yml:398-434` + `.github/workflows/internal-cancel-on-pr-close.yml:6-7`; `.github/workflows/orchestrate_poll.yml:268-305` + `.github/workflows/internal-orchestrate-poll.yml:4-5` — Severity: **Medium** — Category: `api-redundancy`
- **Description:** The global merge-train release scan (`review_merge_train.sh release`) runs twice every 5 minutes in this repository:
  - once per poll tick ("Runs on EVERY tick", `orchestrate_poll.yml:269-274`);
  - once per scheduled cancel-on-close sweep (`cancel_on_pr_close.yml:416-417`, cron `*/5`).
  - Per `review_merge_train.sh:95-99`, one release costs one open-PR list plus `actions/runs?status=<s>` listings for five statuses, twice (≥10 calls, with paging), plus per-PR file reads.
  - **Current:** ≥22 calls per 5 minutes, about ≥264 per hour, before file reads. **Projected:** ≥11 per 5 minutes, about ≥132 per hour.
  - Concurrent scans are safe (label-delete claim, `cancel_on_pr_close.yml:20-21`) but wasteful. The comment at 399-401 says the scheduled path exists for core-profile repos with no poller.
- **Recommended fix:** add `MERGE_TRAIN_SCHEDULED_RELEASE_ENABLED: ${{ vars.MERGE_TRAIN_SCHEDULED_RELEASE_ENABLED || 'true' }}` (default true per §8, so core-profile consumers keep their backstop). In the step, `exit 0` on `EVENT_NAME == schedule` when it is `false`, and set the variable to `false` in repos that run the poller. The event path (PR close) is unchanged. [NEEDS VERIFICATION] that every poller repo also runs this schedule.

**API-002** — `scripts/workflow_failure_heal_pr_reconcile.sh:239-247` — Severity: **Low** — Category: `api-batching`
- **Description:** There is one `GET repos/<repo>/pulls?state=open&head=<owner>:ai/issue-<n>` per matching heal issue, inside the `for heal_issue` loop. Current: N REST calls. Projected: one aliased GraphQL call per 50 issues.
- **Recommended fix:** extend the batching pattern of `_cpr_fetch_pr_state_batches` (`cancel_on_pr_close.yml:198-263`): aliases `i<N>: pullRequests(headRefName:"ai/issue-<N>", states:OPEN, first:10){nodes{number headRefName headRefOid baseRefName}}`, mapped to the same TSV fields used at 274. Fail open: a failed batch logs `heal_pr_lookup_failed` per issue, as today.

**API-003** — `.github/workflows/implement.yml:5685-5703` — Severity: **Low** — Category: `api-batching`
- **Description:** The ancestor no-op walk makes two REST calls per hop (issue body, then the paginated comments of the parent), sequentially, inside `for hop`. Current: 2×threshold, so 4 by default (`IMPL_NOOP_ANCESTRY_THRESHOLD=2`). Projected: 1 GraphQL call per hop (`issue(number:$n){body comments(last:100){nodes{body}}}`), so 2 by default. Hops depend on each other, so they cannot be batched further.
- **Recommended fix:** one GraphQL read per hop with the same `Re-issued from #N` parse and no-op marker test. Keep the existing fail-open `break`.

**API-004** — `scripts/workflow_failure_heal_recovery_scan.sh:162`, `280-286` — Severity: **Low** — Category: `api-redundancy`
- **Description:** The list query already returns each candidate's `comments(last:30)`, but every examined candidate is re-read with a paginated REST comments call (up to `MAX_EXAMINED=20` per tick). Current: up to 20 extra reads per tick. Projected: 0 reads for issues with 30 or fewer comments.
- **Recommended fix:** add `totalCount` to `comments(last:30){…}` and `databaseId`/`author{login}` to its nodes. Build `COMMENTS_FILE` from the list snapshot when `totalCount <= 30`; otherwise keep the REST read. [NEEDS VERIFICATION] that `heal-scope-recovery check` accepts the GraphQL-derived `{id, body, user.login}` shape.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** — "Resolve workflow support ref" step, 20 copies across 13 workflows (e.g. `.github/workflows/orchestrate_poll.yml:147-188`, `.github/workflows/cancel_on_pr_close.yml:32-74`, `.github/workflows/implement.yml:1353-1396`) — Severity: **Medium** — Category: `duplication`
- **Description:** The promote-candidate and consumer-pin resolution is copied verbatim, including the inline `gh_api_retry` fallback and the `jq` protected-branch check. Only the workflow file name in the `workflow_ref` regex differs (for example `cancel_on_pr_close\.yml` vs `orchestrate_poll\.yml`). A security fix to the pin logic has to land 20 times.
- **Recommended fix:** a composite action is awkward here: it would itself have to be pinned to the ref this step resolves [NEEDS VERIFICATION]. Instead, generate the step from one template and enforce it with the existing drift gate (`make generate-check`, `ci.yml:274-277`). Add a contract test that every copy matches the template except the file-name token.

**DUP-002** — support-file staging loop, 56 copies of the `.codex-workflow-src` → `.codex-workflow-src-main` fallback across 6 workflows (`orchestrate_poll.yml` 16, `implement.yml` 12, `plan.yml` 10, `orchestrate.yml` 8, `clarify.yml` 6, `orchestrate_clarify_respond.yml` 4; e.g. `orchestrate_poll.yml:672-967`) — Severity: **Medium** — Category: `duplication`
- **Description:** Each block repeats the same 10-13 lines: resolve src, fall back to main, `::error`/`::notice`, `install -m`, append to `_fetched_scripts`. `scripts/stage_workflow_support.sh:678-735` already has `stage_required_entry`/`stage_optional_preserve_entry`/`stage_optional_copy_entry`, but only `review_autofix.yml` and `validate.yml` call it.
- **Recommended fix:** add a generic helper to `scripts/stage_workflow_support.sh` (sourced from `.codex-workflow-src` after checkout):
  ```bash
  stage_support_files()
  {
  	# stage_support_files <required|optional> <dest_dir> <mode> <missing_msg> <relpath...>
  	local level="$1" dest="$2" mode="$3" msg="$4" rel src
  	shift 4
  	for rel in "$@"
  	do
  		src=".codex-workflow-src/${rel}"
  		[ -f "${src}" ] || src=".codex-workflow-src-main/${rel}"
  		if [ ! -f "${src}" ]
  		then
  			[ "${level}" = "required" ] && { echo "::error::${msg} ${rel}"; return 1; }
  			echo "::notice::${msg} ${rel}"
  			continue
  		fi
  		mkdir -p "${dest}/$(dirname -- "${rel}")"
  		install -m "${mode}" "${src}" "${dest}/${rel}"
  	done
  }
  ```
  Migrate `orchestrate_poll.yml` "Stage workflow support files" first; it also fixes EXPR-001.

**DUP-003** — `scripts/tg_helpers.sh:662-743` (`tg_cleanup_phase_msgs`) and `745-809+` (`tg_cleanup_msgs`) — Severity: **Low** — Category: `duplication`
- **Description:** The delete-then-keep loops at 690-734 and 768-813 are line-for-line identical: id validation, the 3-consecutive-failure breaker, the comment `DELETE`, and the kept/skipped accounting.
- **Recommended fix:** extract `_tg_cleanup_tracking_entries <issue_num> <phase_or_all> <chat_id> <entries>` and call it from both public functions. The public names stay unchanged (§10).

**DUP-004** — `.github/workflows/review_autofix.yml:1258-1285`, `1699-1705`, `4629-4641`; `.github/workflows/implement.yml:5581-5583`, `5629-5631`, `5716-5718`, `5767-5769`, `6732-6734`, `6784-6786`, `6961-6963`, `7002-7004` — Severity: **Low** — Category: `duplication`
- **Description:** There are inline re-implementations of `gh_retry` and `ensure_label_exists`:
  - `review_autofix.yml:1258-1280` defines a 4-attempt exponential `gh_retry` with no rate-limit classification and no non-idempotent-POST guard. It wraps `gh pr merge --auto` (1431, 1468) and retries permanent 4xx failures.
  - `review_autofix.yml:1282-1285` defines a 4-argument `ensure_label_exists`, which conflicts with the canonical 2-argument signature in `scripts/label_helpers.sh:176`.
  - `implement.yml` creates labels inline 8 times with colours and descriptions that diverge from the catalog: `ai:closed` `6b7280` (5582) vs `6a737d` (`label_helpers.sh:62`); `ai:needs-human` `b60205` (5630) vs `e11d48` (`label_helpers.sh:48`).
- **Recommended fix:** source `gh_helpers.sh` and `label_helpers.sh` from the verified support checkout already used in those jobs, and call `ensure_label_exists <label> <repo>`. Delete the inline variants. Keep the inline fallback only behind `type … >/dev/null || …`, as `review_autofix.yml:1699` does.

### Section 4: Expression Size Limit Risk Assessment

Existing guard: `tests/test_workflow_file_size_limit.py:66-98` fails CI when a `run:` body containing `${{` is at least 18,000 characters, so no current workflow body is over 18,000. The findings below cover the 15,000-18,000 band and gaps in that guard.

**EXPR-001** — `.github/workflows/orchestrate_poll.yml:664-969` ("Stage workflow support files") — Severity: **Medium** — Category: `expression-limit`
- **Description:** A body of about 300 lines with three interpolations: `${{ vars.PROMPT_PRELUDE_REFACTOR_ENABLED … }}` (812), `${{ vars.UNATTENDED_IDENTITY_REINJECT_ENABLED … }}` (813) and `${{ github.repository }}` (947). Estimated at 15,000-16,500 characters, leaving about 4,500-6,000 of headroom [NEEDS VERIFICATION: print `len(run)` from the existing test]. It grows with every new support file; line 673 alone is about 1,000 characters.
  - `implement.yml:1451-1452` already moved the same two variables into `STAGE_PROMPT_PRELUDE_REFACTOR_ENABLED` and `STAGE_UNATTENDED_IDENTITY_REINJECT_ENABLED` env values.
- **Recommended fix:** move the two variables into step `env:` (the `STAGE_*` names) and replace line 947 with `"${GITHUB_REPOSITORY}"`. The body then has no `${{ }}` and leaves the expression limit entirely. Longer term, apply DUP-002.

**EXPR-002** — `.github/workflows/test-and-mark-stable.yml:1060-1342` ("Phase 2: Wait for plan to complete") — Severity: **Medium** — Category: `expression-limit`
- **Description:** A body of about 274 lines with three `${{ steps.create-issue.outputs.created_after }}` interpolations (1086, 1098, 1116). Estimated at 15,500-17,500 characters, leaving about 3,500-5,500 of headroom [NEEDS VERIFICATION].
  - Sibling steps already promoted the same value to env with the comment "Passed through env so the run body holds no ${{ }} expression" (1382-1385, 2155-2156, 2255-2259, 3714-3716).
- **Recommended fix:** add `PLAN_CREATED_AFTER_INPUT: ${{ steps.create-issue.outputs.created_after }}` to the step `env:` and use `${PLAN_CREATED_AFTER_INPUT}` at the three sites.

**EXPR-003** — `.github/workflows/implement.yml:5544-5774` ("Handle no-op implementation") — Severity: **Low** — Category: `expression-limit`
- **Description:** A body of about 230 lines with 9 `${{ github.* }}` interpolations (5549, 5551, 5757, 5765, 5767, 5771, 5773). Estimated at 13,500-15,000 characters [NEEDS VERIFICATION]. The step already assigns `REPO` and `RUN_URL` locally but keeps interpolating `${{ github.repository }}` afterwards (5757-5773).
- **Recommended fix:** use the runner's default env: `RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"`, `REPO="${GITHUB_REPOSITORY}"`, and `${REPO}` at 5757-5773.

**EXPR-004** — `tests/test_workflow_file_size_limit.py:66-98` — Severity: **Low** — Category: `expression-limit`
- **Description:** The guard checks only `jobs.*.steps[*].run` in workflow files, at a single 18,000 threshold. It does not cover:
  - composite actions (`.github/actions/*/action.yml`, six files);
  - large `if:` expressions (for example `implement.yml:5537`, `6791`);
  - `with:` values;
  - a 15,000-character early-warning tier.
- **Recommended fix:** extend the test to walk `.github/actions/*/action.yml` `runs.steps`. Add a 15,000 tier that prints `::warning::RUN_EXPRESSION_NEAR_LIMIT file=… step=… length=…` without failing, and assert `if:` strings stay under 4,000 characters.

**EXPR-005** — `.github/workflows/implement.yml` (7,362 lines), `.github/workflows/test-and-mark-stable.yml` (6,963 lines), `.github/workflows/review_autofix.yml` (6,049 lines) — Severity: **Medium** — Category: `expression-limit`
- **Description:** The real limit is GitHub's 512,000 bytes per workflow file, guarded at 480,000 (CLAUDE.md §27; `tests/test_workflow_file_size_limit.py:26-27`), not the 1 MB / 800 KB figures in the audit brief. At a typical 55-65 bytes per line, `implement.yml` is about 405-480 KB, so it may be within about 15% of the guard [NEEDS VERIFICATION with `wc -c`]. No file can be confirmed above 800 KB; the CI guard would fail first.
- **Recommended fix:** run `wc -c .github/workflows/*.yml`. If `implement.yml` is at or above 430,000 bytes, move the largest `run:` bodies to `scripts/implement_step_<slug>.sh` now, following the `review_autofix_step_<slug>.sh` pattern (§27). Candidates: "Run Codex implementation" (2893-4218, about 1,325 lines), "Attempt post-Codex syntax repair" (4753-5284) and "Preflight destructive-commit guard" (4328-4741).

### Section 5: Cross-Cutting Concerns

**DEAD-001** — `scripts/gh_helpers.sh:372-377`; `README.md:3864-3866` — Severity: **Low** — Category: `dead-code`
- **Description:** Nothing in `.github/workflows`, `scripts/` or `.github/actions` calls `gh_rate_limit_breaker_tripped`. It is documented as consumer API.
  - `README.md:3864` also says "The poller's inline retry helper in the 'Find active tracking issues' step also writes this flag". That step has no inline retry helper: it sources `gh_helpers.sh` and falls back to `gh_retry() { "$@"; }` (`orchestrate_poll.yml:315-316`).
- **Recommended fix:** correct README line 3864. Keep the function (it is documented consumer API), but note it as consumer-only in the comment at `gh_helpers.sh:344-358`.

**DEBT-001** — `scripts/orchestrate_poll_process.sh` (over 33,000 lines; matches at line 33076) — Severity: **Medium** — Category: `tech-debt`
- **Description:** A single bash file of this size needs `# shellcheck extended-analysis=false` to keep ShellCheck from crashing the runner (`ci.yml:492-495`, issue #6680). Defects like BUG-001 and CONSIST-001 hide in it, and CI only checks it at `--severity=error` (`ci.yml:510`).
- **Recommended fix:** split by concern into sourced modules (for example `orchestrate_poll_stall_recovery.sh` for about 18,600-18,900 and `orchestrate_poll_clarify_answers.sh` for about 26,258-26,410). Add them to the staging list at `orchestrate_poll.yml:673` and keep function names unchanged (§10). Raise those modules to `--severity=warning`.

**CONSIST-001** — `scripts/orchestrate_poll_process.sh:18672`, `18681`, `32599` — Severity: **Low** — Category: `consistency`
- **Description:** Paginated comment reads pass `sort=created&direction=desc`. These calls are harmless (`--paginate` plus `max_by`), but the copy-paste shape is the root cause of BUG-001.
- **Recommended fix:** drop the ignored parameters, and add a CI grep guard that rejects `issues/[^/]+/comments?[^"]*direction=` outside `tests/`.

**CONSIST-002** — `.github/workflows/orchestrate_poll.yml:2159-2173`; `.github/workflows/forward-merge-stable-to-main.yml:535` — Severity: **Low** — Category: `consistency`
- **Description:** The failure-alert step reads `actions/runs/<id>/attempts/<n>/jobs` with raw `curl -H "Authorization: token …"`: no retry and no rate-limit handling. `implement.yml:6829` reads the same data through `gh_retry _safe_gh_jq`.
- **Recommended fix:** source `scripts/gh_helpers.sh` (already staged) and use `gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/attempts/${GITHUB_RUN_ATTEMPT}/jobs"`. Keep the existing fail-open fallback.

**CONSIST-003** — `.github/workflows/orchestrate_poll.yml:154-155`, `328`, `358`, `947`; `.github/workflows/cancel_on_pr_close.yml:40-41`; `.github/workflows/implement.yml:5757-5773` — Severity: **Low** — Category: `consistency`
- **Description:** `${{ github.repository }}` and `${{ github.sha }}` are interpolated into `run:` bodies where `${GITHUB_REPOSITORY}`/`${GITHUB_SHA}` are always set. This works against the env-promotion convention (test-and-mark-stable comments at 1382, 2156) and adds to the expression size (EXPR-001, EXPR-003).
- **Recommended fix:** replace these with the default runner env variables when the file is next edited.

**CONSIST-004** — `scripts/label_helpers.sh:216` (compare `313`; `scripts/gh_helpers.sh:157-164`) — Severity: **Low** — Category: `consistency`
- **Description:** `ensure_label_exists` puts raw `gh` stderr into `::warning::…: ${_label_err}` with no escaping. Line 313 of the same file neutralises `::`, and `gh_helpers.sh` uses `_gh_actions_escape` for exactly this purpose (issue #8095, SEC-001).
- **Recommended fix:** `echo "::warning::ensure_label_exists: failed to create label '${label_name}' in repo '${repo}': $(_gh_actions_escape "${_label_err:0:200}")" >&2`.

**CONSIST-005** — `.github/workflows/lint-pr-body-auto-close.yml:85-86` — Severity: **Low** — Category: `consistency`
- **Description:** In a required check, the commit read is a bare `gh api --paginate`, so a transient 5xx fails the PR's required lint until someone re-runs it. The repo's classified retry helper `gh_api_retry` (`scripts/gh_helpers.sh:605-652`) keeps the step failing when the read cannot be done ("the check never passes unread", 83-84).
- **Recommended fix:** use `gh_api_retry --paginate …` from the trusted checkout proposed in SEC-001.

**CONSIST-006** — `.github/workflows/cancel_on_pr_close.yml:377-379` — Severity: **Low** — Category: `consistency`
- **Description:** `gh_retry gh api -X POST …/runs/<id>/cancel` is classified as a non-idempotent POST and gets one attempt (`gh_helpers.sh:706-713`). The workflow itself says cancel POSTs are idempotent (20-21), so a transient failure leaves the run alive and logs it as "likely already completed".
- **Recommended fix:** prefix with `GH_RETRY_IDEMPOTENT=true`, as `label_helpers.sh:250` does for its idempotent POSTs.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 12 | BUG-002, BUG-003, BUG-004, SEC-001, SEC-002, API-001, DUP-001, DUP-002, EXPR-001, EXPR-002, EXPR-005, DEBT-001 |
| Low | 19 | BUG-005, BUG-006, BUG-007, BUG-008, SHELL-001, API-002, API-003, API-004, DUP-003, DUP-004, EXPR-003, EXPR-004, DEAD-001, CONSIST-001, CONSIST-002, CONSIST-003, CONSIST-004, CONSIST-005, CONSIST-006 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/orchestrate_poll_process.sh` | Small |
| API call optimization | `.github/workflows/cancel_on_pr_close.yml`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `.github/workflows/implement.yml`, `scripts/workflow_failure_heal_recovery_scan.sh` | Medium |
| Code modularization | 13 workflows with "Resolve workflow support ref", 6 staging workflows, `scripts/stage_workflow_support.sh`, `scripts/tg_helpers.sh`, `.github/workflows/review_autofix.yml`, `.github/workflows/implement.yml`, `scripts/orchestrate_poll_process.sh` | Large |
| Expression size reduction | `.github/workflows/orchestrate_poll.yml`, `.github/workflows/test-and-mark-stable.yml`, `.github/workflows/implement.yml`, `tests/test_workflow_file_size_limit.py` | Small |
| Medium/Low fixes | `.github/workflows/integration-pr-readiness.yml`, `.github/workflows/lint-pr-body-auto-close.yml`, `scripts/check_integration_pr_readiness.py`, `.github/workflows/update_workflows.yml`, `.github/workflows/implement.yml`, `.github/workflows/test-and-mark-stable.yml`, `scripts/stable_e2e_step_wait_review.sh`, `scripts/promote_candidate_follow.sh`, `scripts/workflow_failure_heal_pr_reconcile.sh`, `scripts/label_helpers.sh`, `.github/workflows/ci.yml`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/cancel_on_pr_close.yml`, `README.md` | Medium |