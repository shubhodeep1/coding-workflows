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
## Deep Audit — Workflows & Scripts (2026-11-13)

**Scope and method.** This audit read these files in depth: `implement.yml`, `review_autofix_sweep.yml`, `workflow-log-analysis.yml`, `security-audit.yml`, `cancel_on_pr_close.yml`, `lint-pr-body-auto-close.yml`, `integration-pr-readiness.yml`, `test-and-mark-stable.yml`, `scripts/gh_helpers.sh`, `scripts/tg_helpers.sh` and `scripts/label_helpers.sh`, plus targeted loop and API scans of `scripts/orchestrate_poll_process.sh` (35,812 lines). It also read the smaller scripts `nag_reminder.sh`, `mark-stable.sh`, `implement_stale_branch_check.sh`, `worktree_gc.sh` and `emit_event.sh`. Byte and character counts could not be measured in this sandbox, so every size figure below is an estimate from line counts and is marked as one.

**Already covered earlier in this report, not repeated here:**
- `ci.yml` has no `concurrency:` group (Speed Optimizations item 1).
- The `security-audit.yml:59-61` repo-wide group drops PR-scoped dispatches (Reliability item 1).
- Poller cron supersession (Speed item 2).

### Section 1: Bug & Correctness Sweep

**BUG-001** — `.github/workflows/implement.yml:5757-5774` (with `:6939-7014`)
- **Severity:** Medium
- **Category:** `bug`
- **Description:** The step "Handle no-op implementation" runs under `set -euo pipefail` (`:5545`). On its fall-through path it posts the no-op comment first, with no `|| true`:
  - `gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments" -f body="${noop_body}"` (`:5757-5758`)
  - Only after that does it swap labels to `ai:implementation-failed` (`:5763-5774`).
  - A POST create gets exactly one attempt in `gh_retry`, because `_gh_cmd_is_unsafe_post` sets `max_attempts=1` (`gh_helpers.sh:1215-1217`).
  - So one transient 5xx aborts the step before the failure label is applied.
  - The next step, "Comment on issue failure", then runs (`if: (failure() || cancelled()) …`, `:6940`). It sees `ai:implementing` still present and moves the issue back to `ai:awaiting-approval` (`:7001-7006`).
  - Inference: the issue goes back into the stall-recovery `/approved` loop that `:6950-6953` describes, instead of the orchestrator's `ai:implementation-failed` re-issue path.
  - Every other comment post in the same step is guarded (`:5594-5595`, `:5637-5638`, `:5728-5729`).
- **Recommended fix:**
  - Apply the label swap (`:5763-5774`) before posting the comment.
  - Guard the POST with `|| echo "::warning::Could not post no-op comment on #${ISSUE_NUMBER}"`, the same shape as `:5595`.

**BUG-002** — `scripts/tg_helpers.sh:108-134`
- **Severity:** Medium
- **Category:** `bug`
- **Description:** `tg_send_msg` has two defects.
  - Its `curl` has no `--max-time` (`:124`). The sibling `tg_send_report_msg` uses `--max-time 30` (`:156`), and `deleteMessage` uses `--max-time 10` (`:217`).
  - It parses the reply with an unguarded `msg_id=$(printf '%s' "${resp}" | jq -r '.result.message_id // empty' 2>/dev/null)` (`:132`). If the reply is not JSON (for example an HTML 502 page), `jq` exits non-zero. Under the caller's `set -euo pipefail`, that assignment aborts the calling step.
  - Unguarded direct callers under `set -euo pipefail` include:
    - `workflow-log-analysis.yml:2345` (step starts with `set -euo pipefail` at `:2320`)
    - `workflow-log-analysis.yml:808`, `:1720` and `:2960`
    - `update_workflows.yml:1784`
    - `issue_pr_status.yml:1148`
  - Impact: a Telegram outage can hang a notify step until the job timeout, or turn a successful pass's job red.
  - `tg_send_report_msg` already guards the same parse (`:164-166`).
- **Recommended fix:** Add `--max-time 30` at `:124`, and mirror `:164-166`:
  ```bash
  local msg_id
  if ! msg_id=$(printf '%s' "${resp}" | jq -r '.result.message_id // empty' 2>/dev/null)
  then
  	msg_id=""
  fi
  ```

**BUG-003** — `.github/workflows/review_autofix_sweep.yml:220-247` (with `:671-693`)
- **Severity:** Medium
- **Category:** `bug`
- **Description:** The open-PR listing makes a raw `gh api --paginate -X GET "repos/${REPOSITORY}/pulls"` call piped to `jq -s` (`:236-247`), under `set -euo pipefail` (`:221`).
  - This happens even though `gh_api_retry` is sourced a few lines earlier (`:224-228`).
  - The active-run snapshot in the same step deliberately uses `gh_api_retry` (`:366`) under the #6634 contract.
  - One transient error therefore fails the "Enumerate open PRs…" step, so no review is dispatched that tick.
  - Two later steps then skip as well, because they have no status function in their `if:` and so get an implicit `success()`:
    - "Re-run cancelled CI failed jobs once" (`:671-682`)
    - "Re-dispatch stale security holds" (`:684-693`)
- **Recommended fix:** Use `GH_TOKEN="${READ_TOKEN}" gh_api_retry --paginate -X GET "repos/${REPOSITORY}/pulls" -f state=open -f per_page=100`. On a non-zero return, emit `AUTOFIX_SWEEP_SKIP reason=pr_list_unavailable` and `exit 0`, so the independent CI-rerun and security-hold passes still run.

**BUG-004** — `scripts/worktree_gc.sh:40-47, 77-84`
- **Severity:** Low
- **Category:** `bug`
- **Description:** The cached run list is passed on the command line with `jq -cn --argjson runs "${cached_runs}"` (`:46`).
  - Linux limits a single argv string to 128 KiB (MAX_ARG_STRLEN), and a large `actions-runs-cache` can exceed it.
  - When it does, `_worktree_gc_write_cache_snapshot` fails and `worktree_registry.sh gc` runs without `WORKTREE_ACTIVE_RUNS_FILE` (`:81`).
  - Separately, under `set -euo pipefail` (`:2`), a failing `gc` exits before `rm -f "${cleanup_file}"` (`:84`), which leaks the temp file.
  - [NEEDS VERIFICATION] I did not read what `gc` does when it has no active-runs file.
- **Recommended fix:**
  - Build the file from stdin instead: `printf '%s' "${cache_json}" | jq -c '{workflow_runs: (.cache.runs // [])}' > "${out_file}"`.
  - Add `trap 'rm -f "${cleanup_file}"' EXIT` right after the `mktemp` at `:68`.

**BUG-005** — `.github/workflows/test-and-mark-stable.yml:3731-3734`
- **Severity:** Low
- **Category:** `bug`
- **Description:** `gh api … -X PATCH -f state=closed … 2>/dev/null || echo "Warning: could not close PR #${PR_NUMBER}"` is followed by an unconditional `echo "  Closed PR #${PR_NUMBER}"`. After a failed close, the cleanup log reports both a warning and "Closed", which misleads release-gate triage.
- **Recommended fix:** Use `if gh api …; then echo "  Closed PR …"; else echo "::warning::…"; fi`, the same shape already used at `:3775-3782`.

**SEC-001** — `.github/workflows/security-audit.yml:100-116`
- **Severity:** Low
- **Category:** `security`
- **Description:**
  - The bootstrap builds `ghr_auth="AUTHORIZATION: basic $(… x-access-token:${GH_TOKEN} | base64)"` and passes it as `git -c http.extraHeader="${ghr_auth}"` (`:108`, `:112`, `:115-116`).
  - The base64 token therefore sits in git's argv, readable through `/proc/*/cmdline` by any process on the runner while the clone runs. GitHub log masking does not cover argv.
  - `tests/test_workflow_gh_retry_fallback_contract.py:436-452` keeps this step byte-identical in `review_autofix.yml`, `validate.yml`, `implement.yml`, `orchestrate_clarify_respond.yml` and `security-audit.yml`, so all five share the exposure.
- **Recommended fix:** Pass the header through the environment, following the repo's existing one-shot pattern in `workflow-log-analysis.yml:2239-2243`: `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader GIT_CONFIG_VALUE_0="${ghr_auth}" git …`. Update all five copies and the identity test together.

**SEC-002** — `.github/workflows/implement.yml:5643-5657`; `scripts/tg_helpers.sh:124`, `:156`, `:217`
- **Severity:** Low
- **Category:** `security`
- **Description:** The Telegram bot token is placed in the curl URL (`https://api.telegram.org/bot${TG_BOT_SECRET}/sendMessage`), so it appears in curl's argv for the duration of each call. `implement.yml:5652` repeats this inline instead of calling `tg_helpers.sh`.
- **Recommended fix:** In `tg_helpers.sh`, feed the URL through a config read from stdin, for example `printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "${TG_BOT_SECRET}" | curl -K - …`. Make the inline sender in `implement.yml` call the helper (see DUP-008).

**SEC-003** — `.github/workflows/implement.yml:2006-2019`
- **Severity:** Low
- **Category:** `security`
- **Description:** `${{ steps.baseline_refctx.outputs.branch }}` is interpolated straight into double-quoted shell (`:2012`, `:2017`, `:2019`).
  - It is not exploitable today, because the producer accepts only `ai/reissue-baseline/pr-(\d+)-([0-9a-f]{12})-\d+-\d+` (`:1198`).
  - Git ref names may contain `$(` and backticks, though. Any future loosening of `:1198` would turn this into command injection.
- **Recommended fix:** Pass the output through `env:` (`BASELINE_BRANCH: ${{ steps.baseline_refctx.outputs.branch }}`) and reference `"${BASELINE_BRANCH}"`, as the step already does for `IMPLEMENT_RESOLVED_FALLBACK_REF` (`:2001`).

### Section 2: GitHub API Call Redundancy Audit

**API-001** — `.github/workflows/implement.yml:2276-2299`
- **Severity:** Medium
- **Category:** `api-redundancy`
- **Description:** "Fetch issue comments" uses a hand-rolled retry loop of five iterations.
  - Each iteration pages through the full comment list: `gh api --paginate "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}/comments?per_page=100"` (`:2284`).
  - It re-fetches the whole list until the approval comment ID appears (`:2289`).
  - It uses linear sleeps with no rate-limit classification, and it still sleeps after the final attempt (`:2293`).
  - If `APPROVAL_COMMENT_ID` is empty, all five full listings run and the step then fails (`:2296-2298`).
  - Current cost is up to 5 × P calls (P = comment pages).
- **Recommended fix:**
  - First check presence with one `gh_api_retry "repos/${GITHUB_REPOSITORY}/issues/comments/${APPROVAL_COMMENT_ID}"` call, retrying only on `GH_API_RETRY_LAST_KIND=transient`.
  - Then page through the list once.
  - Projected cost: 1 + P calls, or 1 call in steady state.
  - Fail fast when `APPROVAL_COMMENT_ID` is not `^[1-9][0-9]*$`.

**BATCH-001** — `scripts/orchestrate_poll_process.sh:32526-32541`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** After review-blocked handling, the re-check loop makes one REST call per wave issue to read labels: `gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${inum}/labels"` (`:32533`). It does this for every entry in `${ISSUE_NUMS}`, with no cache check.
  - The batched helper `_fetch_issue_labels_batch_graphql` (`:4757-4770`, 25 issues per query, fail-open with per-issue fallback) already serves the same data at `:30026` and `:29597`.
  - The concatenated `LABELS_JSON` also has no empty-value guard, unlike the fallback loop at `:32551`.
  - Current cost is N REST calls; projected cost is ⌈N/25⌉ GraphQL calls plus REST only for misses.
- **Recommended fix:** Replace `:32526-32541` with `LABELS_JSON="$(_fetch_issue_labels_batch_graphql "$(printf '%s\n' ${ISSUE_NUMS} | jq -Rcs 'split("\n") | map(select(test("^[0-9]+$")) | tonumber)')")"`. Keep the existing miss-fallback loop (`:32545-32553`) unchanged.

**BATCH-002** — `scripts/orchestrate_poll_process.sh:7731-7748` and `:32663-32684`
- **Severity:** Medium
- **Category:** `api-batching`
- **Description:** Blocker issues are resolved one REST call at a time: `gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/issues/${blocker_issue}" --jq '.state'` (`:7734`, `:32670`). This runs inside the per-`if_issue` loop that starts at `:32568`, so the cost per tick is the sum of B over the failed issues.
- **Recommended fix:**
  - Add `state` to the alias fragment in `_fetch_issue_labels_batch_graphql` (`:4800-4803`), or add a sibling helper `_fetch_issue_states_batch_graphql` with the same batching contract.
  - Prefetch every blocker for the wave once, before the `:32568` loop.
  - Projected cost: ⌈ΣB/25⌉ GraphQL calls, with fail-open to the current per-issue read. See also DUP-007.

**BATCH-003** — `scripts/orchestrate_poll_process.sh:189-247`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** `replay_failed_reclarify_commands` makes two REST calls per queued issue: the issue itself (`:202`) and its full paginated comments (`:205`). Current cost is 2Q + pages. `_fetch_blocked_issue_routing_graphql` (`:262-283`) already batches labels and the last 20 comments for 25 issues per call. [NEEDS VERIFICATION] The replay check needs the complete comment history (`:206`), so `comments(last: 20)` may not be enough.
- **Recommended fix:** Batch the issue read (state, `__typename`, labels) through the existing helper and keep the per-issue comment listing only for issues that pass the label/state gate. Projected cost: ⌈Q/25⌉ + Q′ calls, where Q′ ≤ Q.

**BATCH-004** — `scripts/orchestrate_poll_process.sh:5166-5178`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** In tier 2 merged-PR resolution, there is one REST `pulls/${pr}` read per cross-referenced PR (`:5169`). Current cost is up to X calls per issue.
- **Recommended fix:** Use one aliased GraphQL call, `pr<N>: pullRequest(number:N){ mergedAt body }`, for all `xref_prs`, following the batching contract of `_fetch_linked_pr_status_graphql` (`:22564`). Projected cost is ⌈X/25⌉ calls.

**BATCH-005** — `scripts/orchestrate_poll_process.sh:8796-8801`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** `security_pass_unblock_filed_advisory_followups` makes one REST `issues/${row_issue}` read per follow-up row. Current cost is R calls per merged-state tick.
- **Recommended fix:** Prefetch state and labels for every row with the extended helper from BATCH-002. Projected cost is ⌈R/25⌉ calls, keeping the existing warning on misses (`:8803`).

**BATCH-006** — `.github/workflows/implement.yml:5690-5703`; `scripts/orchestrate_poll_process.sh:18586-18587`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** The "Re-issued from #N" ancestry walk makes two REST calls per hop: the issue body (`:5691`) and the full paginated comments (`:5695-5696`). The poller walk at `:18586-18587` follows the same per-hop pattern. Current cost is 2 × threshold calls plus pages.
- **Recommended fix:** Use one GraphQL call per hop, `issue(number:N){ body comments(last:100){ nodes{ body } } }`. Projected cost is one call per hop, and the fail-open `break` stays as it is.

**BATCH-007** — `.github/workflows/review_autofix_sweep.yml:500-526`
- **Severity:** Low
- **Category:** `api-batching`
- **Description:** In the per-PR loop, every pipeline-authored `e2e-smoke-test` PR triggers one REST call for its issue: `gh api "repos/${REPOSITORY}/issues/${smoke_issue_number}"` (`:517`). Current cost is S calls per tick.
- **Recommended fix:** Collect the smoke candidates' issue numbers in a first pass over `rows`, then read their state with one aliased GraphQL query (25 per call). Projected cost is ⌈S/25⌉ calls.

**API-002** — `.github/workflows/review_autofix_sweep.yml:310`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** Every tick that has candidates reads `repos/${REPOSITORY}` just for `.default_branch`. The same job already uses `github.event.repository.default_branch` in an expression (`:152`). [NEEDS VERIFICATION] I did not confirm whether `schedule` event payloads carry `repository.default_branch`.
- **Recommended fix:** Add `DEFAULT_BRANCH_HINT: ${{ github.event.repository.default_branch }}` to the step's `env:`. Use the hint when it matches `^[A-Za-z0-9._/-]+$`, otherwise fall back to the current API read. That cuts one call to zero when the payload carries the field.

**API-003** — `.github/workflows/test-and-mark-stable.yml:3752-3768`
- **Severity:** Low
- **Category:** `api-redundancy`
- **Description:** The late-Implement cancel pages through up to 10 pages of the repo-wide `actions/runs` list (`:3753-3760`) and filters client-side for `status != completed`. That costs up to 10 calls, mostly spent on completed runs.
- **Recommended fix:** Query the same endpoint with server-side status filters (`status=queued`, `in_progress` and `pending`; the API accepts one value per call), keeping the existing `created>=` filter. Projected cost is 3 calls. [NEEDS VERIFICATION] Check this against the `display_title` match at `:3757`.

Note on the cancel POSTs (`cancel_on_pr_close.yml:371-387`, `orchestrate_poll_process.sh:18397-18399`): one call per run is unavoidable, because GitHub has no bulk-cancel endpoint. No action is needed.

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001** — `.github/workflows/workflow-log-analysis.yml:1094-1438`, `:1968-2233` and `:2584-2849`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** The three AI pass steps ("Run workflow log analysis", "Run deep audit pass" and "Run API redundancy pass") each re-implement the same machinery:
  - the `_wla_label_retry` and `_wla_label_create` one-liners (`:1115-1116`, `:1987-1988`, `:2604-2605`)
  - the `TRACKING_ISSUE` validation (`:1118-1126`, `:1990-1998`)
  - a ~90-line `AI_PHASE_FAILURE_V1` emitter (`:1137-1230` and `:2000-2076`, plus the API-redundancy copy around `:2628-2683`)
  - the `wla_engine_ready` and `claude_run_selected` engine switch with codex fallback (`:1373-1402`, `:2154-2169`)
  - the capacity-wait exit 80 and the exponential retry loop
- **Recommended fix:** Create `scripts/workflow_log_analysis_pass.sh` that defines the following, then reduce each step to setting env plus calling `wla_run_pass`. Callers: the three steps above. This also removes most of the step-level expressions (EXPR-001, EXPR-002).
  - `wla_emit_phase_failure <failed_step_name> <failure_mode> <attempt_count> <summary> <heading>`
  - `wla_label_create`
  - `wla_run_pass <role> <prompt_file> <out_file> <failed_step_name>`

**DUP-002** — `.github/workflows/workflow-log-analysis.yml` (four job preambles)
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** Four jobs repeat the same 10–12 setup steps almost line for line:
  - "Resolve AI engine" (`:251`, `:865`, `:1759`, `:2383`)
  - Claude install and credential (`:290-304`, `:904-918`, `:1798-1812`, `:2422-2436`)
  - the Codex cache, install, persist and restore blocks (`:310-347`, `:924-970`, `:1818-1864`, `:2442-2488`)
  - Semble setup, install and index (`:357-384`, `:985-1032`, `:1920-1967`, `:2536-2583`)
- **Recommended fix:** Add a local composite action, `.github/actions/setup-log-analysis-engine/action.yml`, with inputs `role` and `semble_enabled`, following the existing `./.github/actions/install-codex` usage (`security-audit.yml:308`). Replace the four preambles with it.

**DUP-003** — `scripts/gh_helpers.sh:1208-1301`, `:1312-1385` and `:1434-1522`
- **Severity:** Medium
- **Category:** `duplication`
- **Description:** `gh_retry`, `gh_retry_to_file` and `gh_api_json_to_file` contain near-identical copies of the same retry loop:
  - mktemp error handling
  - the unsafe-POST single-attempt rule
  - permanent-failure detection, then the rate-limit branch with `_gh_ratelimit_tg_alert`, `_gh_rate_limit_trip_breaker` and `_gh_rate_limit_wait_for`
  - `2 ** (attempt-1)` backoff
  - the final error lines

  They differ only in where successful output goes (replay to stdout, write to a file, or validate JSON then write). Fixes have already had to be applied three times, for example the stderr excerpt from SEC-001 of #8095.
- **Recommended fix:** Extract `_gh_retry_core <sink: stdout|file:<path>|json:<path>> <command…>` and turn the three public functions into thin wrappers that keep their signatures (§10).

**DUP-004** — `_safe_gh_jq` inline redefinitions
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Besides the canonical definition (`gh_helpers.sh:1404-1418`), the function is redefined in six places:
  - `plan.yml:3052`
  - `orchestrate.yml:1456`
  - `implement.yml:6799`
  - `scripts/implement_diagnose_post_codex_failure.sh:54`
  - `scripts/check_failure_triage.sh:379`
  - `review_autofix.yml:1311`, which diverges from the contract (see CONSIST-001)
- **Recommended fix:** Keep the `type _safe_gh_jq >/dev/null 2>&1 ||` guards, but make every fallback body byte-identical to `gh_helpers.sh:1404-1418`. Add a contract test similar to `test_gh_api_retry_bootstrap_step_is_identical` (`tests/test_workflow_gh_retry_fallback_contract.py:436-452`).

**DUP-005** — `.github/workflows/implement.yml` inline `gh label create`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Eight inline `gh_retry gh label create` calls hard-code their own colours and descriptions: `:5581`, `:5629`, `:5716`, `:5767`, `:6732`, `:6784`, `:6961`, `:7002`. `ensure_label_exists` (`label_helpers.sh:176-200`) already owns colour and description, plus a per-process cache. `implement.yml:5763-5766` already prefers it, with a fallback. The remove/add label swap with a fallback edit is also repeated (`:5584-5590`, `:5719-5725`, `:5771-5774`).
- **Recommended fix:** Source `label_helpers.sh` once per step, using the guard at `:5763`. Replace the inline creates with `ensure_label_exists "<label>" "${GITHUB_REPOSITORY}"`. For the swaps, add `swap_issue_labels <issue> <add> <remove…>` to `label_helpers.sh`.

**DUP-006** — "Resolve workflow support ref" promote-candidate block
- **Severity:** Low
- **Category:** `duplication`
- **Description:** The `promote_candidate_workflow_ref` resolution appears 18 times across 11 workflows (for example `cancel_on_pr_close.yml:37-74`, `implement.yml:474-511`, `:778-812`, `:1358-1396` and `:7295-7333`). The copies are already drifting: `implement.yml:782` hard-codes `"shubhodeep1/coding-workflows"`, while the others compare against `${wf_source}`.
  - Extraction is constrained, because the step runs before the support checkout exists.
  - `tests/test_workflow_support_ref_follows_pin.py` tests behaviour, but not byte identity.
- **Recommended fix:** Add a normalized byte-identity test, modelled on `tests/test_workflow_gh_retry_fallback_contract.py:436-452`, that substitutes only the workflow filename. Normalize `implement.yml:782` to the `${wf_source}` form.

**DUP-007** — `scripts/orchestrate_poll_process.sh:7731-7748` vs `:32668-32684`
- **Severity:** Low
- **Category:** `duplication`
- **Description:** The two blocker-state classification loops are identical apart from variable-name casing.
- **Recommended fix:** Add `_classify_blocker_states <blockers_json>`, which sets `*_OPEN_COUNT`, `*_UNKNOWN_COUNT` and `*_STATUS_SUMMARY` from the batched lookup in BATCH-002. Update both callers.

**DUP-008** — inline Telegram senders
- **Severity:** Low
- **Category:** `duplication`
- **Description:** Seven inline `api.telegram.org/bot` curls exist outside `tg_helpers.sh`:
  - `check_failure_triage.yml`
  - `update_workflows.yml` (×2)
  - `issue_pr_status.yml`
  - `implement.yml:5652`
  - `validation-refresh.yml`
  - `test-and-mark-stable.yml`

  `implement.yml:5641-5642` justifies its copy by saying `tg_helpers.sh` "may have been cleaned up".
- **Recommended fix:** Stage `tg_helpers.sh` into `${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}` (already used at `:7025-7029`) and call `tg_send_msg`. This also centralizes the SEC-002 hardening.

### Section 4: Expression Size Limit Risk Assessment

Baseline: `tests/test_workflow_file_size_limit.py:66-98` already fails CI for any `steps[].run` body that contains `${{` and is 18,000 characters or longer. While CI is green, no step can be in the High band (>18,000). The findings below are Medium (estimated 15,000–18,000) or Low watch-list entries. All sizes are estimates from line counts, because the sandbox could not measure characters.

**EXPR-001** — `.github/workflows/workflow-log-analysis.yml:1105-1438` ("Run workflow log analysis")
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:**
  - The body is about 333 lines and contains 15 inline expressions (`:1150-1152`, `:1165`, `:1171-1173`, `:1210-1212`).
  - Estimated size is about 15,500–17,900 characters, leaving roughly 3,100–5,500 characters of headroom to the 21,000 limit [NEEDS VERIFICATION].
  - Every interpolated value is a default runner environment variable: `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_WORKFLOW`, `GITHUB_SERVER_URL` and `GITHUB_REPOSITORY`.
- **Recommended fix:** Replace those 15 expressions with the environment variables. The body then has zero expressions and is exempt from the limit. Longer term, apply DUP-001.

**EXPR-002** — `.github/workflows/workflow-log-analysis.yml:1979-2233` and `:2596-2849`
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** These are the deep-audit and API-redundancy pass bodies, about 255 and 254 lines, with 10 inline expressions each (`:2011-2066`, `:2628-2683`). Estimated size is about 12,000–15,000 characters each, leaving roughly 6,000–9,000 characters of headroom [NEEDS VERIFICATION]. Both embed fixed heading lists and prompt plumbing that will grow with future sections.
- **Recommended fix:** Same as EXPR-001: switch to default environment variables, then move the bodies into the DUP-001 script.

**EXPR-003** — `.github/workflows/implement.yml:5544-5774` ("Handle no-op implementation")
- **Severity:** Medium
- **Category:** `expression-limit`
- **Description:** The body is about 231 lines, holds several multi-line issue-comment templates (`:5571-5580`, `:5614-5628`, `:5708-5715`, `:5737-5755`) and contains 7 inline expressions (`:5549`, `:5551`, `:5757`, `:5765`, `:5767`, `:5771`, `:5773`). Estimated size is about 15,000–17,000 characters, leaving roughly 4,000–6,000 characters of headroom [NEEDS VERIFICATION]. Each new guard block adds about 2–3 KB of template text.
- **Recommended fix:** Use `RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"` and `REPO="${GITHUB_REPOSITORY}"` to remove every expression. Preferably also move the body to `scripts/implement_step_handle_noop.sh`, following the `review_autofix_step_<slug>.sh` pattern (§24), and move the comment templates to `prompts/`.

**EXPR-004** — `.github/workflows/implement.yml:6213-6384` ("Create Pull Request") and `:604-771` ("Resolve integration ref")
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - "Create Pull Request" is about 171 lines with expressions at `:6241`, `:6267`, `:6303`, `:6351` and `:6356`.
  - "Resolve integration ref" is about 167 lines with expressions at `:623`, `:624`, `:708` and `:733`.
  - Each is estimated at about 10,000–13,000 characters, leaving roughly 8,000–11,000 characters of headroom [NEEDS VERIFICATION].
- **Recommended fix:** Replace `${{ github.repository }}` and `${{ github.sha }}` with `${GITHUB_REPOSITORY}` and `${GITHUB_SHA}`.

**EXPR-005** — `.github/workflows/test-and-mark-stable.yml:3541-3706` ("Verify all phases passed") and `:3717-~3846` ("Cleanup test artifacts")
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:**
  - "Verify all phases passed" interpolates 15 step outputs inline (`:3551-3677`).
  - "Cleanup test artifacts" says it keeps its body free of `${{ }}` ("env only, so the body holds no extra ${{ }} expression", `:3713-3714`), but it still interpolates two expressions at `:3797` and `:3799`.
  - Both are estimated under 12,000 characters [NEEDS VERIFICATION].
- **Recommended fix:** Move these values into `env:`, as `:3710-3716` already does for the other values, so both bodies become exempt.

**EXPR-006** — `tests/test_workflow_file_size_limit.py:66-98` (guard coverage gap)
- **Severity:** Low
- **Category:** `expression-limit`
- **Description:** The guard measures only `jobs.*.steps[].run`. It does not measure:
  - step and job `if:` conditions, for example the 7-clause `if:` at `implement.yml:6940` and `:7017`
  - `with:` and `env:` values or `run-name`
  - composite actions under `.github/actions/*/action.yml`

  None of these is near the limit today. The only protection, though, is one test that skips these surfaces.
- **Recommended fix:** Extend the walk to step and job `if`, `with` and `env` values that contain `${{`, and to composite action `runs.steps[].run`, using the same 18,000 threshold.

File-size note:
- The 800 KB and 1 MB thresholds in the audit brief don't apply to this repo. The measured GitHub limit is 512,000 bytes (`tests/test_workflow_file_size_limit.py:26`), and the repo guard is 480,000 bytes (`:27`), so no workflow can be near 800 KB while CI is green.
- The largest workflows by line count are `implement.yml` (7,362), `test-and-mark-stable.yml` (6,963) and `review_autofix.yml` (6,012). Their byte sizes could not be measured here [NEEDS VERIFICATION]; `budget-watch` (`ci.yml:3493-3508`) tracks them.

### Section 5: Cross-Cutting Concerns

**DEAD-001** — `scripts/gh_helpers.sh:372-377`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `gh_rate_limit_breaker_tripped()` has no callers in `.github/workflows/`, `scripts/` or the repo's tests. The only mentions outside `gh_helpers.sh` are in `README.md` and old `analysis/*.md` reports. The comment at `:349-354` says the only consumer (`orchestrate_poll.yml`) "has been removed". Callers use `gh_rate_limit_breaker_active` (`:1034-1042`) instead.
- **Recommended fix:** Don't delete it in this repo yet. Consumers may source `gh_helpers.sh`, so removal is a §10 change. Mark it deprecated in the header comment, and remove it after a consumer-repo grep confirms there are no callers.

**DEAD-002** — `.github/workflows/workflow-log-analysis.yml:1357-1361`
- **Severity:** Low
- **Category:** `dead-code`
- **Description:** `max_attempts` is validated again with the same rule and default that `:1128-1131` already enforced on `MAX_CODEX_ATTEMPTS`, so the second branch can never fire.
- **Recommended fix:** Delete `:1358-1361` and keep `max_attempts="${MAX_CODEX_ATTEMPTS}"`.

**CONSIST-001** — `.github/workflows/review_autofix.yml:1311`
- **Severity:** Medium
- **Category:** `consistency`
- **Description:** `_safe_gh_jq() { gh api "$@"; }` is defined unconditionally, without a `type _safe_gh_jq ||` guard. It overrides the canonical helper, whose whole contract is "suppress stdout on failure" (`gh_helpers.sh:1388-1418`).
  - It is then used by `pr_checks_lib.sh`: `:166`, `:256` and `:283` through `gh_retry`, and `:1216` directly.
  - The `gh_retry` buffer hides the problem, but only when the real `gh_retry` is loaded. With a stub `gh_retry`, the error JSON reaches the jq parsing. For example, the branch-protection read at `pr_checks_lib.sh:166-169` would see an error object and no `required_status_checks`.
  - [NEEDS VERIFICATION] I did not confirm whether that path loosens the required-checks set for auto-merge.
- **Recommended fix:** Use `type _safe_gh_jq >/dev/null 2>&1 || _safe_gh_jq() { …canonical body… }` (see DUP-004).

**CONSIST-002** — inline `${{ github.* }}` in run bodies
- **Severity:** Low
- **Category:** `consistency`
- **Description:** `implement.yml` has 88 inline run-body expressions, mostly `${{ github.repository }}`, `run_id`, `run_attempt`, `actor`, `server_url` and `job.status` (for example `:2101`, `:2284`, `:2412`, `:5549`, `:6720-6752`). `security-audit.yml` and `cancel_on_pr_close.yml:174-193` use `${GITHUB_REPOSITORY}` for the same values. The inline form inflates expression size and is inconsistent across files.
- **Recommended fix:** When editing these blocks, mechanically replace them with `GITHUB_REPOSITORY`, `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_ACTOR` and `GITHUB_SERVER_URL`. Keep `job.status` via `env:`.

**CONSIST-003** — `.github/workflows/security-audit.yml:498`, `:518`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** "Report synced-file security findings upstream" and "Report refused AI engine fallbacks" set `GH_TOKEN: ${{ secrets.GH_PAT }}` with no fallback. Sibling steps use `${{ secrets.GH_PAT || github.token }}` (`:80`, `:464`). In consumers without `GH_PAT`, these steps silently do nothing (`continue-on-error: true`). [NEEDS VERIFICATION] A PAT may be required here on purpose for the cross-repo heal dispatch.
- **Recommended fix:** If the PAT is required, log `reason=gh_pat_missing` explicitly. Otherwise add the `|| github.token` fallback.

**CONSIST-004** — `.github/workflows/sync_ai_labels.yml:125`
- **Severity:** Low
- **Category:** `consistency`
- **Description:** `if [ "${{ inputs.dry_run }}" = "true" ]` is interpolated inline, while later steps read `DRY_RUN: ${{ inputs.dry_run }}` from env (`:170`, `:259`). It is safe today because the input is `type: boolean` (`:11`, `:23`).
- **Recommended fix:** Use `env: DRY_RUN` here too.

**SHELL-001** — `.github/workflows/review_autofix_sweep.yml:490`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** `printf '%s' "${head_ref}" | grep -qF "${HEAD_REF_FILTER}"` has no `--`. A dispatch input such as `-v` or `-e` is parsed as a grep option and silently changes or inverts the filter.
- **Recommended fix:** Use `[[ "${head_ref}" == *"${HEAD_REF_FILTER}"* ]]`, or `grep -qF -- "${HEAD_REF_FILTER}"`.

**SHELL-002** — `scripts/orchestrate_poll_process.sh:32528`, `:32545`, `:32983`, `:29606`
- **Severity:** Low
- **Category:** `shellcheck`
- **Description:** `for inum in ${ISSUE_NUMS}` and `for rnum in ${REISSUED_NUMS}` rely on unquoted word-splitting and globbing (SC2086). The values are numeric today. `LABELS_JSON` is built by string concatenation (`:32526-32541`) with no empty-value guard, unlike `:32551`.
- **Recommended fix:** Use `mapfile -t` from `jq -r '.[]'` output. With BATCH-001 in place, the concatenation goes away.

**DEBT-001** — `scripts/orchestrate_poll_process.sh` (whole file)
- **Severity:** Low
- **Category:** `tech-debt`
- **Description:** One sourced script is 35,812 lines and has 373 `gh` call sites. The redundant per-issue reads above (BATCH-001, BATCH-002, BATCH-004, BATCH-005) are hard to find in it. No `TODO`, `FIXME` or `HACK` markers exist in `.github/workflows/*.yml` or `scripts/*.{sh,py}`; a word-boundary search found none.
- **Recommended fix:** Split it incrementally into sourced modules by subsystem, for example `scripts/orchestrate_poll_wave.sh` and `scripts/orchestrate_poll_reclarify.sh`. Keep the function names unchanged (§10), and centralize all batched readers in one module with their batching contracts.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 11 | BUG-001, BUG-002, BUG-003, API-001, BATCH-001, BATCH-002, DUP-001, DUP-002, DUP-003, EXPR-001, EXPR-003, CONSIST-001 |
| Low | 30 | BUG-004, BUG-005, SEC-001, SEC-002, SEC-003, API-002, API-003, BATCH-003, BATCH-004, BATCH-005, BATCH-006, BATCH-007, DUP-004, DUP-005, DUP-006, DUP-007, DUP-008, EXPR-002, EXPR-004, EXPR-005, EXPR-006, DEAD-001, DEAD-002, CONSIST-002, CONSIST-003, CONSIST-004, SHELL-001, SHELL-002, DEBT-001 |

Count correction: the Medium row lists 12 IDs and the Low row lists 29, for 41 findings in total.

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | None at Critical/High. The Medium bugs touch `.github/workflows/implement.yml`, `scripts/tg_helpers.sh`, `.github/workflows/review_autofix_sweep.yml` and `.github/workflows/review_autofix.yml` | Small |
| API call optimization | `scripts/orchestrate_poll_process.sh`, `.github/workflows/implement.yml`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/test-and-mark-stable.yml` | Medium |
| Code modularization | `.github/workflows/workflow-log-analysis.yml`, new `scripts/workflow_log_analysis_pass.sh`, new `.github/actions/setup-log-analysis-engine/action.yml`, `scripts/gh_helpers.sh`, `scripts/label_helpers.sh`, `tests/test_workflow_gh_retry_fallback_contract.py`, `tests/test_workflow_support_ref_follows_pin.py` | Large |
| Expression size reduction | `.github/workflows/workflow-log-analysis.yml`, `.github/workflows/implement.yml`, `.github/workflows/test-and-mark-stable.yml`, `tests/test_workflow_file_size_limit.py` | Small |
| Medium/Low fixes | `scripts/worktree_gc.sh`, `.github/workflows/security-audit.yml` (+4 bootstrap copies), `scripts/tg_helpers.sh`, `scripts/gh_helpers.sh`, `.github/workflows/sync_ai_labels.yml`, `.github/workflows/review_autofix_sweep.yml`, `scripts/orchestrate_poll_process.sh` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-11-13)

### Safety Tag Legend
`SAFE_TO_MERGE` means every precondition was proven by reading the code. The calls hit the same endpoint (or a strict subset of its fields) with the same pagination, filters and token, from the same function or step, with no write in between, the same error and retry behaviour, and no concurrency or cache contract crossed. The implement stage may act on these directly. `NEEDS_VERIFICATION` means the merge looks plausible, but at least one precondition can't be proven by reading the code alone. Run the listed verification steps before changing anything. `RISKY_SKIP` means the call is in a retry, backoff or stall-recovery path, an identity, auth or permission probe, a paginated listing, a rate-limit-sensitive path, `orchestrate_poll_process.sh`, or a deliberate race or time-of-check-to-time-of-use (TOCTOU) recheck. These are listed so they are visible, and must not be auto-implemented.

### Consolidation Candidates (MERGE-###)

**MERGE-001** — `RISKY_SKIP`
- **Files and lines:** `scripts/validate_process.sh:4122-4126` (tracking issue body) and `scripts/validate_process.sh:5544-5554` (tracking issue state).
- **Call count:** 2 per tracking run with fixable failures. Proposed: 1.
- **Endpoint:** `GET /repos/{owner}/{repo}/issues/{n}`, twice.
- **Evidence:** The same issue is read twice in one script run, keeping a different field each time:
  ```bash
  gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_NUM}" --jq '.body // ""' > "${PROJECT_SPEC_FILE}"
  ```
  ```bash
  # Tracker-open guard: never open a fix-up against a closed tracker.
  TRACKER_STATE="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_RAW}" --jq '.state' ...)"
  ```
  The two reads are separated by the whole validation run: context assembly, AI attempts and test execution.
- **Proposed fix (not for auto-implementation):** Read `{body, state}` once at `:4123` and keep `state` in a variable. This only saves the call if the guard may use a state that is as old as the validation run. Check first that `TRACKING_ISSUE_NUM` and `TRACKING_ISSUE_RAW` always resolve to the same number.
- **Safety rationale:** The second read is an explicit tracker-open guard against a race. A tracker can close during a validation run that takes many minutes. Merging would make the guard check a stale state, which hits the "defends against upstream races" trigger.
- **Downstream signal:** Do not auto-implement. Keep both calls unless a human accepts that the guard may act on a state as old as the validation run, which would allow fix-ups to be opened against a tracker that closed mid-run.

**MERGE-002** — `RISKY_SKIP`
- **Files and lines:** `.github/workflows/implement.yml:232` ("Precheck approval phase label"), `:999-1013` ("Resolve checkout ref"), `:2100-2102` ("Fetch issue metadata"), `:2329-2335` ("Validate approval phase label") and `:2771-2775` ("Recheck security dependency before edits"). All are in job `implement` (`:109`).
- **Call count:** 3 in the normal case. `:1007` and `:2101` already skip when `ISSUE_META_FILE` passes `issue_meta_matches_issue`, so the reads that actually run are `:232`, `:2329` and `:2771`. Proposed: 1, but not recommended.
- **Endpoint:** `GET /repos/{owner}/{repo}/issues/{n}`.
- **Evidence:** The cache-reuse pattern is already in place (`:999-1004`, `:2100-2102`). The two reads that still run are documented rechecks: `# Recheck just before advancing the phase. /approved must not bypass a still-open security prerequisite or a failed API read.` (`:2327-2328`), and the step name at `:2764` says "before edits".
  - Net-new detail: the fallback at `:2377-2379` (`gh_retry gh api ".../issues/${ISSUE_NUMBER}" --jq '[.labels[].name]'`) is reachable only when `jq '[.labels[].name]'` fails on the payload that `:2335` just wrote. That only happens with a payload that has no `labels` array, so it is effectively never called. Leave it alone; it costs nothing in steady state.
- **Proposed fix (not for auto-implementation):** None. Folding `:2329` or `:2771` into the `:232` snapshot would remove the security-dependency and PR-dependency TOCTOU checks.
- **Safety rationale:** These are deliberate live rechecks of a security gate (issues #7159 and #7228) taken at different points in the run. Merging them changes what the gate sees, which hits the race-defence and permission triggers.
- **Downstream signal:** Do not auto-implement. Any change needs a security reviewer to confirm that the security-dependency and PR-dependency holds may be evaluated on a snapshot taken before the approval claim and before the editor runs.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001** — `RISKY_SKIP`
- **Files and lines:**
  - `.github/workflows/implement.yml:2134-2141` (identity read, then exported to `GITHUB_ENV`)
  - `scripts/implement_heal_preflight.sh:129-130` (run by "Heal route preflight", `implement.yml:2308-2316`)
  - `.github/workflows/implement.yml:2477-2484` ("Unblock judge bulk-delete override")
- **Call count:** 1 always, plus 1 on the heal route, plus 1 when a bulk-delete override marker is present, so up to 3 per run. Proposed: 1.
- **Endpoint:** `GET /user`.
- **Evidence:** The first read stores the login in the job environment:
  ```bash
  pipeline_login="$(gh api user --jq .login 2>/dev/null || true)"
  ...
  echo "IMPLEMENT_SCOPE_PIPELINE_LOGIN=${pipeline_login}" >> "$GITHUB_ENV"
  ```
  Later steps in the same job read it again with the same `GH_TOKEN: ${{ secrets.GH_PAT }}` (`:2082`, `:2474`):
  ```bash
  pipeline_login="$(gh api user --jq '.login' 2>/dev/null || true)"   # :2480
  identity="$(gh api user --jq .login 2>/dev/null)" || refuse unavailable identity_unavailable   # heal_preflight :129
  ```
  The comment at `:2131-2133` covers only the opposite direction (the later read can't feed the earlier grant). It does not explain why `:2480` doesn't reuse the exported value. `IMPLEMENT_SCOPE_PIPELINE_LOGIN` is already reused without a new call at `:4685`, `scripts/implement_step_prepush_test_gate.sh:566` and `scripts/implement_commit_changes.sh:604`.
- **Proposed fix (not for auto-implementation):** At `:2480`, use `pipeline_login="${IMPLEMENT_SCOPE_PIPELINE_LOGIN:-}"` and keep the live `gh api user` read as the fallback when it is empty.
- **Safety rationale:**
  - Both re-reads are identity probes that authorize something: lifting the bulk-delete guard, and refusing or allowing a heal edit (`implement_heal_preflight.sh:32`: "Never allow another editor path to run after a failed authorization read"). That hits the auth/permission-probe trigger.
  - The value would come from `GITHUB_ENV`, which any step in between can write. One such step is "Heal route preflight", which runs `bash scripts/implement_heal_preflight.sh` from the workspace. The comment at `:2336-2339` describes the checkout's `scripts/` as branch-controlled.
- **Downstream signal:** Do not auto-implement. A security reviewer must confirm two things:
  - (a) Whether the `scripts/` used at `:2316` is the trusted staged copy or the branch checkout.
  - (b) That no step between `:2141` and `:2463` can write `IMPLEMENT_SCOPE_PIPELINE_LOGIN` to `GITHUB_ENV`.

  If either is unproven, reusing the value widens the trust surface of the bulk-delete override.

**REUSE-002** — `RISKY_SKIP`
- **Files and lines:** All in job `sweep` of `.github/workflows/review_autofix_sweep.yml`:
  - `:163-204` → `scripts/provider_outage.py:349-353` (lazy, cached per process)
  - `:505` (enumerate step; lazy, only when a pipeline smoke PR exists)
  - `:641-656` → `scripts/workflow_failure_heal_non_retryable_backfill.sh:150-154` (every tick when enabled)
  - `:684-693` → `scripts/review_security_hold_sweep.py:228-234` in legacy mode, or `:203-219` in `actions_token` mode
- **Call count:** 1–4 per tick. Proposed: 1.
- **Endpoint:** `GET /user`.
- **Evidence:**
  ```bash
  smoke_pipeline_login="$(gh api user --jq .login 2>/dev/null || true)"   # sweep.yml:505, GH_TOKEN=secrets.GH_PAT (:208)
  LOGIN="$(gh api user --jq .login 2>/dev/null | head -1 || true)"        # backfill.sh:150, GH_TOKEN=secrets.GH_PAT (:647)
  ```
  ```python
  identity_result = _run_gh(["api", "user", "--jq", ".login // \"\""])   # security_hold_sweep.py:229
  ```
  Each read is a separate process, and no step exports the login.
- **Proposed fix (not for auto-implementation):**
  - Add one early step that resolves the GH_PAT login and writes it to `$GITHUB_OUTPUT`.
  - Pass it to each consumer through an optional env var. A new name is needed, unique per §10.
  - Each consumer keeps its own `gh api user` fallback when the value is empty.
- **Safety rationale:**
  - Every one of these reads chooses which comment author is trusted. That makes them identity probes on a trust boundary (auth/permission-probe trigger).
  - The tokens are not identical: the security-hold step uses `secrets.GH_PAT || github.token` (`:687`), and `actions_token` mode probes a separate `SECURITY_HOLD_FINDING_AUTHOR_TOKEN`.
  - The backfill script is also run by `workflow-templates/ai-review.yml` in consumer repos (header `:8`), so any new input must have a default (§8).
- **Downstream signal:** Do not auto-implement. This needs manual review of each consumer's trust model and token, and confirmation that a value from an earlier step cannot come from a different token than the consumer's own `GH_TOKEN`. The saving is 1–3 calls per 30-minute tick, which does not justify the risk.

**REUSE-003** — `RISKY_SKIP`
- **Files and lines:** `scripts/orchestrate_poll_process.sh:196-199` (`replay_failed_reclarify_commands`) and `:20709`, `:20732-20735` (`PERSIST_JOURNAL_LOGIN_CACHE`, also filled at `:20857`, `:21093`, `:21470`, `:21539`).
- **Call count:** Up to 2 per poll tick. Proposed: 1.
- **Endpoint:** `GET /user`.
- **Evidence:**
  ```bash
  trusted_login="$(gh_retry gh api user --jq .login 2>/dev/null)" || trusted_login=""      # :198, function-local
  PERSIST_JOURNAL_LOGIN_CACHE="$(gh_retry gh api user --jq .login 2>/dev/null)" || ...    # :20733, process cache
  ```
  The two sites use different shape checks: `:199` rejects a `[bot]` suffix, while `:20734` accepts it.
- **Proposed fix (not for auto-implementation):** In `replay_failed_reclarify_commands`, read `PERSIST_JOURNAL_LOGIN_CACHE` first and fill it after a successful live read. Keep the stricter `:199` regex for the replay decision.
- **Safety rationale:** The code is in `orchestrate_poll_process.sh` and is an identity probe that authorizes a command replay (`:186`: "The marker is data, not authorization"), so it hits two mandatory RISKY_SKIP triggers.
- **Downstream signal:** Do not auto-implement. The poller owner must confirm that line `:20709` (`PERSIST_JOURNAL_LOGIN_CACHE="${PERSIST_JOURNAL_LOGIN_CACHE:-}"`) cannot reset a value set earlier during sourcing, and that the two regexes are reconciled without loosening the replay check.

**REUSE-004** — `RISKY_SKIP`
- **Files and lines:** `.github/workflows/review_autofix_sweep.yml:236-249` (open-PR snapshot written to `ci-cancelled-pr-snapshot.json`) and `scripts/ci_cancelled_rerun.py:246-274` (`current_pr_heads`, called at `:499`).
- **Call count:** 2 × P pages per tick. Proposed: P pages.
- **Endpoint:** `GET /repos/{owner}/{repo}/pulls?state=open&per_page=100` with `--paginate`, twice.
- **Evidence:** The snapshot already holds `head_sha` for every non-draft open PR (`:241`). The rerun helper lists the PRs again:
  ```python
  # The sweep's earlier /pulls listing predates review dispatch; its
  # review-run cache has no live PR heads. Refresh once for all PRs,
  # not per PR; a failed/partial page cannot authorize a rerun.
  ```
- **Proposed fix:** None recommended. The re-listing is a deliberate freshness check: the module docstring at `:9` says "The sweep's earlier PR snapshot cannot prove a head is still current".
- **Safety rationale:** Both calls are paginated, and the second one deliberately defends against a head moving between the snapshot and the rerun POST. That hits the pagination and race-defence triggers.
- **Downstream signal:** Do not auto-implement. It is listed only so a later pass does not "fix" it. Removing it would let a cancelled CI run be re-run against a head that has since been replaced.

### Dead Calls (DEAD-API-###)
No findings. Every call I traced feeds a downstream decision, log line, file or guard:
- `review_autofix.yml:909`, `:1342`, `:1744`, `:2341`, `:5623`
- `implement.yml:6518`
- `review_autofix_sweep.yml:310`, `:505`, `:517`
- the `implement.yml` issue reads in MERGE-002

The `implement.yml:2377-2379` fallback is nearly unreachable rather than dead, as noted in MERGE-002.

### Cross-References to Deep Audit Section
- API-001: RISKY_SKIP — It is a hand-rolled retry loop around `--paginate` (`implement.yml:2283-2294`). Net-new detail for implementers:
  - The full paginated listing is still required downstream: `attempt_journal.py render` (`:2303-2306`) and the bulk-delete override parsing (`:2477-2510`) both read `ISSUE_COMMENTS_FILE`. Only the repeat listings can go.
  - The proposed `GET /repos/{owner}/{repo}/issues/comments/{id}` probe is scoped to the repository, so it must also check that `issue_url` ends in `/issues/${ISSUE_NUMBER}`.
- BATCH-001: RISKY_SKIP — It is in `orchestrate_poll_process.sh`. The loop is a deliberate re-read after labels were changed (`:32532`, "Re-read labels since we may have changed them"). Before any swap, verify that `_fetch_issue_labels_batch_graphql` does not answer from a cycle-local cache filled before the review-blocked writes.
- BATCH-002: RISKY_SKIP — It is in `orchestrate_poll_process.sh`, on the blocker-resolution path.
- BATCH-003: RISKY_SKIP — It is in `orchestrate_poll_process.sh`. This resolves the Deep Audit's open question: `:206` ("Incomplete or malformed comment history never authorizes replay") means `comments(last: 20)` is not enough, so the paginated comment listing at `:205` must stay. The live issue read at `:202-204` also checks `.pull_request? == null`, which a GraphQL `issue(number:)` batch expresses differently (it returns null for a PR number), so the batch must map nulls to skip.
- BATCH-004: RISKY_SKIP — It is in `orchestrate_poll_process.sh`, on the tier-2 merged-PR resolution path.
- BATCH-005: RISKY_SKIP — It is in `orchestrate_poll_process.sh`, on the security-pass unblock path.
- BATCH-006: RISKY_SKIP — The poller half is in `orchestrate_poll_process.sh`, and the `implement.yml:5695-5696` half uses `--paginate`. `comments(last:100)` is not equivalent to the full paginated listing for the no-op ancestry check.
- BATCH-007: NEEDS_VERIFICATION — The read at `review_autofix_sweep.yml:517` runs only for PRs that have the `e2e-smoke-test` label, an `ai/issue-<N>` head and the GH_PAT login as author (`:501-516`), so S is normally 0–1. Verify the S distribution from `AUTOFIX_SWEEP_SMOKE_CLOSE*` log lines before adding a pre-pass. The batched query must keep `READ_TOKEN` (`github.token`) and the "any API error skips the close" semantics (`:498-499`).
- API-002: NEEDS_VERIFICATION — The value is a provenance input (issue #6629). By design it has no fallback, and a failed read skips every dispatch (`:303-309`). The checkout at `:152` already hedges with `github.event.repository.default_branch || github.ref_name`, which suggests the field may be missing on some events.
  - Confirm that `schedule` payloads carry `repository.default_branch`.
  - Make sure a malformed or absent hint falls back to the live read, and never decides provenance alone.
  - `provider_outage.py:444` also fetches `defaultBranchRef`, but only on its PR-recovery path, so it is not a reliable source to reuse.
- API-003: RISKY_SKIP — The repo-wide `actions/runs` listing is paginated (`test-and-mark-stable.yml:3753-3760`). Splitting it into per-status calls changes page boundaries in a release-gate cleanup path.

### Summary Counts
| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 2 | BATCH-007, API-002 (cross-references) |
| RISKY_SKIP | 15 | MERGE-001, MERGE-002, REUSE-001, REUSE-002, REUSE-003, REUSE-004; cross-references API-001, BATCH-001, BATCH-002, BATCH-003, BATCH-004, BATCH-005, BATCH-006, API-003 |

The 15 RISKY_SKIP items are 6 new findings from this pass plus 9 Deep Audit findings retagged as cross-references.

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.
- Every candidate I found is one of these:
  - an identity probe that authorizes something (REUSE-001 to REUSE-003)
  - a deliberate freshness or TOCTOU recheck (MERGE-001, MERGE-002, REUSE-004)
  - paginated or retry-wrapped
  - inside `orchestrate_poll_process.sh`
- The two NEEDS_VERIFICATION cross-references (API-002 and BATCH-007) need the verification steps listed above before they are handed off.