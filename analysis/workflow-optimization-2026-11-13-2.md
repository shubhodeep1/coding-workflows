## Executive Summary

**Data caveat.** The run-logs directory (`/home/runner/work/_temp/workflow-log-output`) was not mounted in this sandbox, so I could not read `summary.json`, `errors/`, `slow/` or `recent/`. Everything below comes from `analysis_context.json` and from reading the repo's workflow and collector source. `insufficient_data` is `false`, but the 1,000-run window covers only about 2 hours (earliest run created 07:23:50Z for run 43385459655, latest 09:22:53Z for run 43400830121). The window contains no AI token telemetry at all.

- **Runner queue saturation is the main latency driver (high confidence for the symptom, medium for the cause).** Runs whose jobs are capped at 5 minutes take about 33 minutes end to end:
  - `lint_pr_body_auto_close`: p50 2,016s, against `timeout-minutes: 5` in `lint-pr-body-auto-close.yml:47`.
  - `cancel_on_pr_close`: p50 2,015s, against `timeout-minutes: 5` in `cancel_on_pr_close.yml:13`.
  - CI: p50 5,548s (about 92 minutes). Execution can't exceed about 30 minutes: the widest parallel tier has a 20-minute cap and the two tail jobs (`budget-watch`, `lint`) have 5 minutes each.
  - So at least 60 minutes per CI run is time spent not executing. The most likely explanation is waiting for a runner, though run `updated_at` drift is also possible.
  - Inference: 11 CI runs created between 07:24:09 and 07:51:05Z, each starting about 10 parallel jobs, flood the hosted-runner pool. Estimated impact: removing superseded CI runs could cut queue wait for every workflow by tens of minutes.
- **The orchestrator poller effectively runs about hourly, not every 5 minutes (high confidence).**
  - `orchestrate_poll` had 17 runs: 2 succeeded (3,347s and 3,340s) and 15 were cancelled with `cancelled_before_first_step` after 135–1,086s pending.
  - The cron is `*/5` (`internal-orchestrate-poll.yml:5`) and the concurrency group is shared across the repo with `cancel-in-progress: false` (`orchestrate_poll.yml:101-102`). GitHub keeps only one pending run per group, so queued ticks replace each other.
  - Impact: wave progression latency is about 56 minutes per cycle.
- **89.6% of the window is skipped no-op runs, which hides real signal (high confidence).**
  - 896 of 1,000 runs are skipped. Of those, 811 come from four workflows (`clarify`, `plan`, `implement`, `orchestrate_clarify_respond`) that each fire on every `issue_comment.created` and then fail their job-level `if:`.
  - This shrinks the window to about 2 hours and fills the `recent/` deep-dive slots with empty archives (`diagnostic_failure_reason: logs: empty_log_archive`, e.g. run 43400830121).
- **Cost and token analysis is blind this window (high confidence).**
  - `codex_tokens_used`, `or_*` and `claude_*` are all 0, and `cache_hit_rate` is null.
  - None of the runs that actually used AI were selected for log download (`log_download_status: not_selected`): plan 43385899311 and 43385896256, review_autofix 43386251486, validate 43392283525, unblock judge 43386044382 and 43392828270.
  - The slow bucket ranks by `duration_seconds`, which includes queue wait, so CI runs with no AI took those slots.
- **No failures (`failure_count: 0`), but 25% of executed runs were cancelled (26 of 104).** Most are benign supersession of pending runs (poller 15, cancel sweep 6). The 4 `security_audit` cancellations (e.g. 43399816689 after 108s, 43386164506 after 467s) need checking because PR-scoped audits share one repo-wide group (medium confidence).
- **Semble is healthy, and Serena left no trace in this window.** There were 40 Semble fallbacks, all `context=contract-test` (4 per CI run across 10 CI runs) and 0 runtime fallbacks. There was 1 production query (5,091 bytes, 4 sources, 0 `static_dup_bytes`). Serena has zero query, fallback or probe lines.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

1. **Cancel superseded pull-request CI runs (critical path).**
   - **Evidence:** CI has 12 runs with p50 5,548s and p95 5,986s. The slowest are 43385506641 (6,237s), 43385956229 (5,781s) and 43387255907 (5,730s). Each CI run starts about 10 parallel jobs: `static-checks`, four `tests-*` jobs, and the `orchestrate-poll` matrix `[0..4]` (`ci.yml:2792`). `ci.yml` has no `concurrency:` block (the grep for `concurrency` matched other workflow files but not `ci.yml`).
   - **Root cause (inference):** Every push to a PR branch runs the full 10-job fan-out, even when a newer push to the same PR is already queued. That fills the runner pool, and the lint, cancel, poller, plan and review runs then wait 28 minutes or more.
   - **Change:**
     ```yaml
     concurrency:
       group: ci-${{ github.repository }}-${{ github.event.pull_request.number || github.ref }}
       cancel-in-progress: ${{ github.event_name == 'pull_request' }}
     ```
     Push CI on `main`, `stable` and `orchestrator/project-*` keeps running in full, because check-failure triage needs a base-branch result (`ci.yml:5-12`).
   - **Estimated savings:** It depends on how many of the 12 runs were superseded runs for the same PR. The context doesn't carry `head_branch` or `event`, so this is unknown. If even 3 of the 11 burst runs were superseded, about 30 fewer concurrent job requests would compete for runners.
   - **Risk:** Low to medium. Before landing, verify that `check_failure_triage` ignores `cancelled` CI conclusions so superseded runs don't open triage issues.
2. **Stop scheduled-cron supersession from inflating poller latency.**
   - **Evidence:** 15 of 17 poller runs were cancelled before their first step (e.g. 43389556058 after 1,086s, 43398471073 after 715s). The successful runs take about 3,340s each.
   - **Root cause:** A `*/5` cron combined with an about 56-minute run time means roughly 10 ticks per cycle get cancelled.
   - **Change:** First find out where the 56 minutes go. The poller already prints `ORCH_POLL_CYCLE_SUMMARY elapsed_ms=… projects_seen=… sleep_ms=…` and `ORCH_POLL_PROJECT_TIMING` (`orchestrate_poll_process.sh:90,142`), but no collector parses them (the grep across `scripts/` finds them only in the emitter). Parse those lines first (see the next item), then decide whether `sleep_ms` or per-project time dominates.
   - **Estimated savings:** Unknown until the time is measured. The ceiling is about 50 minutes of orchestrator latency per cycle.
   - **Risk:** Diagnostics-only change, so none.
3. **Rank the collector's slow bucket by execution time, not wall time (diagnostic).** `duration_seconds = updated_at − run_started_at` (`collect_workflow_logs.py:1147-1151`) counts job queue wait. Add `queue_wait_seconds` (see GH API Call Audit) and sort the slow bucket by execution time. Impact: this window's slow slots showed queue pressure, not slow code.
4. **Micro-optimization: Semble bootstrap.** The single bootstrap took 52,958 ms in poller run 43386313404, about 1.6% of its 3,347s. Not worth acting on yet.

## Cost Optimizations

There is no token data this window, so dollar savings can't be ranked. These items close the gap first.

1. **Give the deep-dive budget to runs that actually used AI.**
   - **Evidence:** All AI-bearing runs were `not_selected`: plan (2 runs, 2,069–2,189s), review_autofix (4 runs, p50 1,200s), validate (1 run, 2,370s), unblock_judge_dispatch (2 runs) and successful security_audit runs.
   - **Root cause:** `select_notable_runs_for_logs` takes the slow runs per repo by duration (`collect_workflow_logs.py:1341-1352`). Queue-inflated CI runs won, and the success sample was only 5 runs (`sampled_success_runs: 5`).
   - **Change:** Cap slow-bucket picks per `workflow_family` (for example 2), so CI can't take all the slow slots.
   - **Savings:** Makes cost visible next window. **Quality risk:** None.
2. **Model, reasoning-level, cache and context-budget review: blocked.** `cache_hit_rate` is null and `break_glass_count` and `context_budget_warn_count` are both 0, but no AI phase run was parsed, so those zeros don't mean healthy. Repeat this section once item 1 lands.
3. **Semble value.** One `SEMBLE_QUERY` in `orchestrate_poll` (run 43386313404) returned 5,091 bytes from 4 sources with 0 `static_dup_bytes`. That is compact, non-duplicated context, so there is no sign Semble is adding noise. One sample is too few to judge whether it reduces prompt expansion.
4. **Serena.** Zero `SERENA_QUERY` lines, so there is no evidence about whether it replaces tool calls or adds noisy response bytes.
5. **Avoidable reruns.** `retries` is 0 on every row, so there is no rerun waste.

## Reliability Improvements

1. **Security-audit runs dispatched per PR can be dropped.**
   - **Evidence:** 4 of 8 `security_audit` runs were cancelled: 43399816689 (108s), 43386164506 (467s), 43399121942 (303s) and one more. Their `failure_point` is null, so they are not confirmed as pre-start cancellations.
   - **Root cause (inference):** Dispatches with `ref`/`pr_number` (`security-audit.yml:11-22`) share the group `security-audit-reusable-${{ github.repository }}` (`:60`). A second pending dispatch replaces the first, which is the same failure mode as #8327 that `cancel_on_pr_close.yml:15-19` documents. Another explanation is that `cancel_on_pr_close` cancelled audits for PRs that had closed.
   - **Fix:** Add `event`, `head_branch` and `display_title` to the run rows so the cause can be classified. If it is supersession, key the group on `inputs.ref` for dispatches and keep the repo group for schedules, following the `cancel_on_pr_close` pattern. Do not reuse the caller-wrapper group, because of the deadlock noted at `:51-58`.
   - **Impact and rollback:** Prevents lost single-issue audit passes. The comment at `:431-434` says stale pending markers trigger a re-dispatch, so lost passes are probably delayed rather than lost (uncertain).
2. **Classify `cancelled_before_first_step` under a concurrency group as `superseded`, not cancelled.**
   - **Evidence:** 21 of 26 cancellations: 15 poller runs and 6 `cancel / cancel-active-runs` sweeps (e.g. 43391950663 after 687s, 43399656968 after 613s).
   - **Fix:** In `collect_workflow_logs.py`, add a `superseded_count` alongside `cancelled_count`, keeping `cancelled_count` unchanged so nothing breaks.
   - **Impact:** The real cancel rate drops from 25% to about 5% (5 of 104), so real drops like item 1 stand out.
3. **Cancel-sweep lag may feed queue saturation (inference).** The 5-minute `cancel_on_pr_close` job takes 2,015s at p50. Runs for closed PRs therefore keep holding runners for about 30 minutes before the sweep cancels them. Item 1 under Speed (CI concurrency) reduces this. Do not add `cancel-in-progress` to the sweep group, because overlapping runs are designed to be safe.
4. **Semble, Serena and break-glass.** All 40 `SEMBLE_FALLBACK` lines are `contract-test` (classified by `_is_contract_test_semble_line`, `cost_audit.py:381-382`) at exactly 4 per CI run, with 0 runtime fallbacks and 0 failed bootstraps. That is healthy, by-design fail-open behaviour. There were no `SERENA_PROBE` lines (ok, failed and skipped are all 0), so I can't tell "disabled" apart from "never reached". Mitigation: emit `SERENA_PROBE result=skipped reason=disabled` when Serena is off. There were no `BREAK_GLASS` or `CONTEXT_BUDGET_WARN` lines.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` lines were available. The log directory isn't mounted, and none of the clarify, plan, implement, review or validate runs that could have retrieved memory had logs downloaded. That means I can't report a retrieve hit rate, `estimated_tokens` against budget, `keyword_method` distribution, `fail_open` or `enabled:false` entries, or push retries.

**Recommendation:** Once Cost item 1 is in place, check that at least one plan or review_autofix run per window is deep-dived. Also have the collector count `AI_MEMORY_TELEMETRY` lines by `op` into the cost-telemetry block, so the zeros become measurements instead of gaps.

## GH API Call Audit

- **No API telemetry in the context.** The collector parses `GH_PAT_BUDGET phase=end` lines (`collect_workflow_logs.py:114,1827`, emitted by `gh_helpers.sh:74`). However, no hotspots, rate-limit events or remaining-budget values appear in this context. The likely reason is that the only parsed runs were CI and skipped runs, which don't emit them.
- **The collector spends most of its run listing on skipped runs.** 896 of 1,000 listed runs (about 9 of 10 pages of 100) are skipped no-ops.
  - **Change:** Paginate until N non-skipped runs are collected (with a page cap), report `skipped_count` separately, and exclude `conclusion == "skipped"` from the `recent` bucket. That bucket currently sorts every eligible run by creation time (`collect_workflow_logs.py:1469`) and downloaded 16 empty archives in this window.
  - **Impact:** Roughly 16 fewer wasted log-download calls per collection, and a time window about 5–9× longer for the same cap.
- **Queue-wait diagnostics without new API calls.** For runs whose jobs were already fetched (`jobs_fetch_status: success` on the cancelled rows), compute `job_queue_wait_max_seconds = max(job.started_at − job.created_at)` from that same response. Extend the same fetch only to runs already selected for the slow bucket (at most `max_log_runs`). Per §14, this reuses an existing call and stays fail-open: if the field is missing, the row is left as it is.
- **Event fan-out.** Each `issue_comment.created` starts 4 workflow runs. There were about 203 such events in about 2 hours, mostly bot or status comments by inference. Skipped runs use no runners but add run-history noise. A possible later step is to merge the four `internal-*.yml` dispatchers into one workflow with four jobs, which would create 4× fewer run records. That is a structural change at medium risk, and it is not recommended this cycle.
- **Hygiene.** The poller already uses batched GraphQL helpers (`_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`). No unbatched loops are evidenced in this window.

## Prompt Cache & Memory System

- `cache_hit_rate` is null for every family. `or_cache_read_tokens` and `or_cache_write_tokens` are 0, as are `claude_cache_*` reads and writes. There is no prompt-cache evidence either way, so I can't assess fragmentation causes such as unstable prefixes.
- `context_budget_warn_count` is 0, but that comes from 0 parsed AI phases, so it says nothing about prompt-growth pressure.
- Telemetry coverage is inflated. `runs_with_log_telemetry: 115` and `wall_clock_sample_count: 111` include skipped runs with empty archives (`log_parsed: true`, wall clock 1–12s). Wall-clock p50 is 2,000 ms and p99 is 5,775,900 ms. That p50 reflects skipped stubs, not real phases. Recommendation: exclude `skipped` from the wall-clock samples, or report them as a separate series.
- **Next collection step:** Force at least one sampled run each from `plan`, `review_autofix` and `validate`, so the next window has non-null cache metrics.

## Orchestrator Health

- **Poll cadence:** about 1 successful cycle per 56 minutes: 43386313404 (created 07:31, 3,347s) and 43393219806 (created 08:26, 3,340s). 15 of 17 runs (88%) were cancelled while pending.
- **Wave progression:** 1 `validate` run (43392283525, `[tracking:9116]`, 2,370s), 2 unblock-judge runs (#9235 and #9242) and 16 `integration_pr_readiness` runs, all successful. There were no conflict-heal failures and no stuck or terminal states visible.
- **Clarify loops:** The 203 clarify and 203 orchestrate-clarify-respond runs are all skipped (no `/reclarify` trigger matched), so there is no evidence of a clarification loop.
- **Smallest mitigation:** Harvest the existing `ORCH_POLL_CYCLE_SUMMARY` and `ORCH_POLL_PROJECT_TIMING` lines. Add a `ORCH_POLL_CYCLE_SUMMARY_RE` in `collect_workflow_logs.py` and expose `poll_cycle_elapsed_ms`, `poll_sleep_ms`, `projects_processed` and `terminal_reason` per run. No new emitter is needed.
- **Indicators to track:**
  - successful poll cycles per hour (now about 1)
  - superseded-pending poll runs per hour (now about 7.5)
  - `sleep_ms` / `elapsed_ms` ratio
  - poll run queue wait (once queue-wait fields exist)

## Pipeline Flow Bottlenecks

| Stage | Evidence | Overhead type |
|---|---|---|
| Runner acquisition (all stages) | 5-minute-cap jobs taking about 2,000s; CI about 92 minutes against a ≤30-minute execution bound | **Queueing** (dominant) |
| Orchestrate (poll) | 3,340s cycles; 15 of 17 ticks superseded | Compute or sleep (unmeasured) plus supersession |
| Plan | 2 successful runs, 2,069s and 2,189s | Mostly queue (inference) |
| Review/autofix | 4 runs, p50 1,200s, max 2,249s (43386251486) | Unknown split |
| Validate / judge | 2,370s; 2,175–2,190s | Unknown split |
| Merge/conflict | `forward_merge_stable_to_main` 1,946s; `promote_candidate_follow` p50 2,049s | Likely queue |
| Retries | 0 retries in the window | None |

Fixes in order of end-to-end impact:

1. Add CI concurrency for PR events, which frees runners for every stage.
2. Add queue-wait and execution fields to the collector, so this table can show real splits.
3. Parse the poller cycle summary.
4. Exclude skipped runs from the collector's run cap and deep-dive selection.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - Hosted-runner queue saturation during the CI burst (11 CI runs in 27 minutes).
  - The poller's effective cadence is about 56 minutes.
- **Top failure modes:** No hard failures. 21 cancellations are supersessions of pending runs. 4 `security_audit` cancellations are unexplained and could be dropped PR-scoped audits.
- **Highest-cost drivers:** Unknown, since no token telemetry was collected. By runner minutes, CI is the largest because of its 10-job fan-out on every PR push (inference).
- **Top 3 actions:**
  1. Add a PR-scoped `concurrency` group with `cancel-in-progress` for `pull_request` events to `ci.yml`.
  2. Collector changes: exclude skipped runs from the cap and the `recent` bucket; add `queue_wait_seconds`, `superseded_count`, `event` and `head_branch`; cap slow picks per family; parse `ORCH_POLL_CYCLE_SUMMARY`.
  3. Classify the `security_audit` cancellations and key per-PR dispatches on `inputs.ref` if they turn out to be supersession.

## Metrics Appendix

**Run outcomes (window about 07:23–09:22Z, 2026-11-13)**

| Scope | Runs | Success | Cancelled | Skipped/other | Failure | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| Repo total | 1,000 | 78 | 26 | 896 | 0 | 1.0 | 2,042 |
| ci | 12 | 12 | 0 | 0 | 0 | 5,548.5 | 5,986.2 |
| orchestrate_poll | 17 | 2 | 15 | 0 | 0 | 492 | 3,341.4 |
| cancel_on_pr_close | 13 | 7 | 6 | 0 | 0 | 2,015 | 2,411.8 |
| security_audit | 8 | 4 | 4 | 0 | 0 | 459.5 | 2,026.2 |
| integration_pr_readiness | 16 | 16 | 0 | 0 | 0 | 2,030.5 | 2,470 |
| lint_pr_body_auto_close | 14 | 14 | 0 | 0 | 0 | 2,016 | 2,475.2 |
| review_autofix | 4 | 4 | 0 | 0 | 0 | 1,200 | 2,106.95 |
| issue_pr_status | 4 | 4 | 0 | 0 | 0 | 3,244 | 4,162.2 |
| plan | 204 | 2 | 0 | 202 | 0 | 1.0 | 10 |
| clarify / implement / orch_clarify_respond | 203 each | 0 | 0 | 203 each | 0 | 1.0 | 9–10 |
| check_failure_triage | 37 | 2 | 0 | 35 | 0 | 1.0 | 634 |
| workflow_failure_heal_intake | 41 | 2 | 0 | 39 | 0 | 2.0 | 11 |
| workflow_failure_heal | 11 | 0 | 0 | 11 | 0 | 1.0 | 8 |
| validate / unblock_judge / promote_follow / forward_merge / audit_drift / triage_historical | 1 / 2 / 3 / 1 / 1 / 2 | all success except promote_follow (1 cancelled) | | | 0 | 2,370 / 2,182.5 / 2,049 / 1,946 / 1,665 / 2,892 | — |

**Cost and review telemetry**

| Metric | Value |
|---|---|
| codex_tokens_used / codex_calls | 0 / 0 |
| or_prompt / completion / total tokens | 0 / 0 / 0 |
| or_cache_write / read tokens | 0 / 0 |
| cache_hit_rate | null (all families) |
| wall_clock_p50_ms / p99_ms (repo) | 2,000 / 5,775,900 (111 samples, includes skipped stubs) |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |
| runs_with_log_telemetry | 115 (inflated by skipped/empty archives) |
| sampled_success_runs | 5 (success_sample_rate 0.07) |

**GH API**

| Metric | Value |
|---|---|
| GH_PAT_BUDGET lines | none surfaced in context |
| Rate-limit events | none surfaced |
| Skipped share of listed runs | 896 / 1,000 (89.6%) |
| Empty-archive log downloads (recent bucket) | 16 runs with `empty_log_archive` |

**Semble**

| Workflow/target | query_calls | logged_bytes | sources | static_dup_bytes | fallbacks | contract_test | runtime | bootstraps (ms) | failed / unused |
|---|---|---|---|---|---|---|---|---|---|
| orchestrate_poll (43386313404) | 1 | 5,091 | 4 | 0 | 0 | 0 | 0 | 1 (52,958) | 0 / 0 |
| ci (10 parsed runs) | 0 | 0 | 0 | 0 | 40 | 40 | 0 | 0 | 0 / 0 |
| **Total** | 1 | 5,091 | 4 | 0 | 40 | 40 | 0 | 1 (52,958) | 0 / 0 |

**Serena**

| Metric | Value |
|---|---|
| query_calls / tool_calls / response_bytes / query_ms | 0 / 0 / 0 / 0 |
| fallbacks | 0 |

**MCP availability by target**

| Server | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|
| serena (all targets) | 0 | 0 | 0 |

Other MCP servers observed: none.

**Data gaps and next collection steps**

- The log directory wasn't mounted. Mount it, or confirm the path, for the next run.
- No AI-phase logs were collected. Use per-family slow caps and force at least one sampled run per AI family.
- Run rows have no `event`, `head_branch` or per-job queue-wait fields. Add them from the existing jobs fetch.
- `ORCH_POLL_*` timing lines are emitted but never parsed. Add a parser in the collector.