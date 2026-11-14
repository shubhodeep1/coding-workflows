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