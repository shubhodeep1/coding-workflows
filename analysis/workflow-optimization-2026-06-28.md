## Executive Summary

I analyzed `workflow_log_report.json` (1000 runs), 40 deep-dive local log folders, and 50 sampled `log_summary` rows. Counts marked as sampled come from those 50 summaries; the rest use the full 1000-run window.

- **`review_autofix` is the main critical-path problem.** It had 86 runs with `p50=23s` but `avg=980.2s` and `p95=3825.3s`, which means a short-skip/long-tail split. The worst examples were run `28308671786` failing after `3017s` in `Apply fixes with editor model` and run `28310389106` cancelling after `3419s` with `Review phase stalled — no activity for 40 minutes`. **Impact:** very high (40-60 min saved on affected PRs). **Confidence:** high.  
- **Support-bundle drift is causing repeat hard failures.** Five `review_autofix` runs (`28306104297`, `28306494803`, `28307478886`, `28308332469`, `28308338896`) all failed preflight after `211-517s` because `.../scripts/assemble_prompt.sh` was missing from the staged runtime bundle. **Impact:** high (3-9 min/run plus rerun avoidance). **Confidence:** high.  
- **Cost telemetry is structurally incomplete.** The window shows `or_calls=197` but `or_prompt_tokens=0`, `or_completion_tokens=0`, `or_total_tokens=0`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, and `cache_hit_rate=null`; only `119/1000` runs had parsed log telemetry. `scripts/cost_audit.py` is ready to aggregate these fields, so this looks like an emitter gap, not a parser gap. **Impact:** high on cost control; direct model tuning is currently blind. **Confidence:** high.  
- **CI is unstable enough to block throughput.** `ci` had `12` runs with `10` failures (`83.3%`). Concrete failures included historical actionlint errors against `workflow-log-analysis.yml` (`28306114306`, `28308810040`), a Ruff `E731` failure in `scripts/slop_scan_local.py:425` (`28309914657`), and a validation self-test assertion on missing workflow classification for `check_failure_triage.yml` (`28310925389`). **Impact:** high merge friction, medium compute waste. **Confidence:** high.  
- **Observability workflows are themselves degraded.** `workflow_log_analysis` was `10/10` failure in the 1000-run window; the collector recorded two explicit `partial_data:missing_log_archive` 404s (`28306349630`, `28308808748`), and `drift_audit` run `28311356890` repeated 11 log-fetch warnings in one successful run. **Impact:** high MTTR / low diagnostic confidence. **Confidence:** high.  
- **The breakage is localized, not universal.** `orchestrate_poll` was `33/34` success, `issue_pr_status` was `5/5` success, `copilot_pull_request_reviewer` was `11/11` success, and Semble had `0` runtime fallbacks in the window. **Impact:** medium; repairs can stay narrowly scoped to `review_autofix`, CI, and log collection. **Confidence:** medium.  

## Speed Optimizations

1. **Move support-bundle validation to immediately after staging**
   - **Evidence:** `review_autofix` runs `28306104297`, `28306494803`, `28307478886`, `28308332469`, and `28308338896` all spent `211-517s` before preflight failed on `MISSING: .../scripts/assemble_prompt.sh`.
   - **Root cause:** support-file staging/ref drift (inference). The runtime bundle is only proven incomplete late in the job.
   - **Exact change:** right after `stage_workflow_support.sh`, emit a single structured manifest line such as `SUPPORT_STAGE_MANIFEST` with `script_ref`, `source_repo`, `resolved_source_sha`, `required_count`, `staged_count`, `missing_files`, and a manifest artifact path; then validate the required file list there, not minutes later in preflight.
   - **Estimated time savings:** `3-9` minutes on each failure of this class, plus avoided reruns.
   - **Implementation risk:** low.
   - **Note:** current `scripts/stage_workflow_support.sh` in HEAD already lists `assemble_prompt.sh` as required, so the next diagnostic question is whether failing runs resolved an older `SCRIPT_REF` or skipped the copy path.

2. **Make stall detection step-aware instead of log-size-aware**
   - **Evidence:** run `28310389106` cancelled after `3419s`; its summary says `Run reviewer models` stayed active for about `41 min`, the poller kept seeing identical log size `52333b`, and the healthy review completed about `6 min` after the poller gave up. Run `28311508326` references the same stall pattern in its PR body summary.
   - **Root cause:** the watchdog treats unchanged job logs as inactivity when step metadata is transient or incomplete.
   - **Exact change:** emit `REVIEW_LIVENESS` on each poll with `job_id`, `current_step`, `step_started_at`, `last_log_size_bytes`, `unchanged_log_polls`, `jobs_api_state`, `decision=continue|defer|stall`, and `reason`; when the run is still `in_progress`, back off log polling instead of stalling purely on unchanged bytes.
   - **Estimated time savings:** `40-60` minutes on affected false cancellations.
   - **Implementation risk:** medium.

3. **Collapse short PR-dedupe GH API work**
   - **Evidence:** sampled `review_autofix` runs `28310924438`, `28311507512`, `28309225484`, `28309913750`, `28310388196`, and `28308809077` spent most of their `5-9s` total runtime in `resolve-claude-branch-pr` calling `gh api` for open PR lookup and repo default branch lookup.
   - **Root cause:** repeated metadata fetches in a path that often exits immediately.
   - **Exact change:** skip the repo default-branch call when `github.base_ref` is already known; cache the open-PR lookup result in job outputs; emit `GH_API_CALL_SUMMARY phase=resolve_pr calls=<n> cache_hit=<bool>`.
   - **Estimated time savings:** about `2-3s` per short dedupe run.
   - **Implementation risk:** low.
   - **Type:** micro-optimization.

4. **Separate queue wait from useful poll time**
   - **Evidence:** `orchestrate_poll` had `p50=202s`, `p95=429.2s`; sampled run `28308527097` explicitly waited for a hosted runner before a normal poll run, and runner-wait warnings appeared in 12 sampled summaries across families.
   - **Root cause:** queueing and active work are blended into one duration.
   - **Exact change:** emit `queue_wait_ms`, `active_poll_ms`, and `sleep_ms` per poller run; only optimize compute time after queueing is isolated.
   - **Estimated time savings:** likely `10-60s` on idle/no-op poller runs.
   - **Implementation risk:** medium.

## Cost Optimizations

1. **Eliminate failed and aborted `review_autofix` attempts before tuning models**
   - **Evidence:** the preflight failure cluster burned `211-517s` each with no useful output; run `28310389106` cancelled after `3419s`; long successful `review_autofix` runs such as `28310378407`, `28310393975`, and `28295529547` consumed `26`, `24`, and `14` `or_calls` respectively.
   - **Root cause:** reliability defects are creating the largest avoidable spend.
   - **Exact change:** fix the support-manifest and stall-detection issues first, and emit `RUN_COST_ABORTED phase=<phase> or_calls=<n> codex_tokens=<n> reason=<...>` at every fail/cancel boundary.
   - **Estimated savings:** one avoided long rerun saves roughly `12-26` OR calls and `15-60` minutes of runner/model time on an affected PR.
   - **Quality-risk notes:** none; this removes waste without changing model behavior.

2. **Restore OR/cache telemetry before making any model-selection recommendation**
   - **Evidence:** full-window summary: `or_calls=197` but all OR token totals are zero; `cache_hit_rate=null`; cache read/write tokens are zero; `runs_with_log_telemetry=119/1000`. `scripts/cost_audit.py` already expects `or_prompt_tokens`, `or_completion_tokens`, `or_total_tokens`, `or_cache_*`, `cache_hit_rate`, and `serena_*`.
   - **Root cause:** emitter-side observability gap.
   - **Exact change:** log per-call `provider`, `model`, `phase`, `reasoning_effort`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_write_tokens`, `cache_read_tokens`, `latency_ms`, and `telemetry_version`.
   - **Estimated savings:** direct dollar savings are unquantifiable today; this is the prerequisite for safe savings work.
   - **Quality-risk notes:** low.

3. **Treat prompt caching as unmeasured, not healthy**
   - **Evidence:** `cache_hit_rate=null`, `or_cache_write_tokens=0`, `or_cache_read_tokens=0`; meanwhile `review_autofix` produced `10` `CONTEXT_BUDGET_WARN`s, all in long runs.
   - **Root cause:** cache is either disabled, fragmented, or simply silent.
   - **Exact change:** emit `PROMPT_CACHE_METRICS` with `phase`, `model`, `prefix_hash`, `static_prefix_bytes`, `dynamic_suffix_bytes`, `cache_attempted`, `cache_result`, and `cache_miss_reason`.
   - **Estimated savings:** potentially high on repeated review/editor loops, but currently impossible to quantify.
   - **Quality-risk notes:** low if kept fail-open.

4. **Measure whether Semble is actually shrinking context**
   - **Evidence:** runtime Semble usage was modest and real: `25` queries / `295,198` bytes total (~`11.8 KB/query`), all in `review_autofix`; run `28308671786` logged `SEMBLE_QUERY target=reviewer-context chunks=12 bytes=15127 ms=477`. There were `15` fallbacks total, but all were `semble_contract_test_fallbacks`; runtime fallbacks were `0`.
   - **Root cause:** no `before/after` prompt-size telemetry.
   - **Exact change:** emit `context_bytes_before_semble`, `context_bytes_after_semble`, `selected_chunks`, `response_bytes`, and `bytes_saved_or_added`.
   - **Estimated savings:** probably modest; likely smaller than rerun elimination.
   - **Quality-risk notes:** keep current fail-open behavior; no need to reduce Semble usage blindly.

5. **Do not downshift models yet**
   - **Evidence:** only `20,260` Codex tokens were recorded across `10` Codex calls, while the far larger `197` OR calls have zero token visibility.
   - **Recommendation:** hold model-choice changes until OR token accounting is fixed.
   - **Estimated savings:** unknown.
   - **Quality-risk notes:** high if changed blindly.

## Reliability Improvements

1. **Make support-source drift diagnosable across workflows**
   - **Failure evidence:** `review_autofix` preflight failures all missed `assemble_prompt.sh`; `issue_pr_status` run `28311876417` warned `Support checkout ref ... is unavailable; using main.` and repeatedly fell back to main/local support files.
   - **Root cause category:** reusable-workflow support ref / staged asset drift.
   - **Exact fix:** emit `SUPPORT_SOURCE_RESOLUTION` once per run with `workflow_family`, `script_ref`, `resolved_ref`, `resolved_sha`, `fallback_to_main=<bool>`, `local_fallback_count`, and `required_file_missing_count`; upload the staged manifest as an artifact.
   - **Expected reliability impact:** high; this is the smallest safe fix for a repeated hard-fail class.
   - **Rollback/fail-open:** keep optional helpers as warnings; hard-fail only required assets.

2. **Harden false-stall handling in review watchdogs**
   - **Failure evidence:** run `28310389106` cancelled on `Review phase stalled`; sampled PR metadata in `28311508326` describes the same symptom and the same frozen `52333b` log size.
   - **Root cause category:** watchdog state classification / liveness ambiguity.
   - **Exact fix:** persist the last good `started_at` and emit structured `REVIEW_LIVENESS` and `REVIEW_STALL_VERDICT` records; if `jobs` still say `in_progress` but logs are unchanged, defer with warning instead of cancelling.
   - **Expected reliability impact:** very high on long-running reviews.
   - **Rollback/fail-open:** bias toward continued polling when state is ambiguous.

3. **Turn CI failures into machine-readable signatures**
   - **Failure evidence:** historical actionlint failures `28306114306` and `28308810040` flagged invalid `env` context usage in `.github/workflows/workflow-log-analysis.yml`; run `28309914657` failed Ruff with `E731` at `scripts/slop_scan_local.py:425`; run `28310925389` failed validation self-test with `Unclassified checkout@v5 workflows: ['check_failure_triage.yml']`.
   - **Root cause category:** static contract drift.
   - **Exact fix:** emit one structured `CI_FAILURE_SIGNATURE` per failing subgate with `gate`, `file`, `line`, `rule`, `changed_files_overlap`, and `owner_hint`; for the classification test, emit `CLASSIFICATION_AUDIT missing=[...] scanned_count=<n>`.
   - **Expected reliability impact:** medium on failure-rate, high on MTTR.
   - **Rollback/fail-open:** none; this is logging only.
   - **Note:** current HEAD now shows `vars`, not `env`, in the `workflow-log-analysis.yml` `if:` guards, so that specific actionlint failure may already be fixed; the next CI pass should verify it.

4. **Fix log-archive blind spots in the collector path**
   - **Failure evidence:** explicit collector errors for runs `28306349630` and `28308808748`: `partial_data:missing_log_archive ... /actions/runs/<id>/logs ... HTTP 404`; `drift_audit` run `28311356890` repeated 11 `log fetch failed` warnings.
   - **Root cause category:** log-archive endpoint availability / retention ambiguity.
   - **Exact fix:** on every archive miss emit `LOG_ARCHIVE_FETCH` with `repo`, `run_id`, `workflow_family`, `run_status`, `created_at`, `updated_at`, `age_seconds`, `http_status`, `attempt`, `cached_result`, and `fallback_used`; collapse repeated misses for the same run into one summary entry.
   - **Expected reliability impact:** high on observability reliability.
   - **Rollback/fail-open:** keep partial-data behavior; do not fail silently.

5. **Keep contract-test Semble fallbacks out of production alarms**
   - **Failure evidence:** the full window had `15` Semble fallbacks, all counted as `semble_contract_test_fallbacks`; runtime fallbacks were `0`. Run `28309226371` alone emitted 5 `SEMBLE_FALLBACK ... context=contract-test` lines.
   - **Root cause category:** telemetry mixing expected test fail-open behavior with runtime health signals.
   - **Exact fix:** add `expected=true` or keep contract-test fallback totals in a separate top-level bucket only.
   - **Expected reliability impact:** medium on signal quality; prevents false rollout alarms.
   - **Rollback/fail-open:** none.

`BREAK_GLASS` was `0` in the full window, so I do **not** see evidence of policy/rubric pressure causing failures. Prompt-size pressure is real: all `10` `CONTEXT_BUDGET_WARN`s occurred in five long `review_autofix` runs (`28289469014`, `28290634570`, `28292769056`, `28294810094`, `28295529547`).

## AI Memory Health

- **Telemetry exists and the retrieve path looks healthy.** In the deep-dive bundle I found `67` lines containing `AI_MEMORY_TELEMETRY`, of which `61` were parseable JSON payloads: `record-run-event=32`, `retrieve=13`, `record-candidate=11`, `write_lessons_learned=5`.
- **Retrieve quality is strong.** `13/13` retrieve operations returned records (`records_selected > 0`), average `estimated_tokens=868` against `token_budget=1400`, and `keyword_method=llm` in all 13 cases. I saw `0` zero-hit retrieves, `0` `fail_open:true`, and `0` `enabled:false`.
- **Write-side instrumentation is brittle.** Run `28308671786` logged `memory helper script missing; skipping run-start event` and `...writing fallback reviewer memory context`; run `28310393975` logged `...skipping consensus candidate record` and `...skipping run-end completion event`. So retrieval works when called, but helper absence silently drops some run-event coverage.
- **Coverage gap:** I did **not** see sampled `promote`, `compact`, `finalize-task`, or `processed-command-*` events in the deep-dive bundle. That may mean those paths were inactive, or it may mean they are uninstrumented.
- **Diagnostic-logging addition:** standardize every AI-memory event, including fail-open/helper-missing paths, to include `repo`, `workflow`, `run_id`, `run_attempt`, `pr_number`, `phase`, `source_step`, `helper_present`, `push_attempts`, `estimated_tokens`, and a consistent budget field name (`token_budget` or `budget_tokens`, but not both).

## GH API Call Audit

Counts in this section are directional: they come from the 50 runs with `log_summary` coverage plus the deep-dive logs, not from all 1000 runs. I saw **no** sampled `429` or secondary rate-limit signals; the problem is redundancy and low-value polling.

1. **Repeated PR-dedupe lookups in short `review_autofix` runs**
   - **Evidence:** sampled runs `28310924438`, `28311507512`, `28309225484`, `28309913750`, `28310388196`, and `28308809077` all call `gh api` to find open PRs by head ref; some also call `gh api repos/${REPOSITORY} --jq '.default_branch'`.
   - **Redundancy pattern:** 1-2 metadata calls dominate a path that usually exits in under 10s.
   - **Change:** reuse `github.base_ref` when available, cache the PR lookup result in job outputs, and emit `GH_API_CALL phase=resolve_pr endpoint=<class> ms=<n> cache_hit=<bool>`.
   - **Estimated reduction:** `1-2` GH API calls per short run, roughly `33-100%` of API traffic in that path.
   - **Rate-limit impact:** low absolute count, but easy win.

2. **Poller hammering on frozen logs**
   - **Evidence:** run `28310389106` kept polling `/actions/jobs/{id}/logs` while the visible state stayed `step: Run reviewer models ... log: 52333b`.
   - **Redundancy pattern:** repeated log fetches with no byte delta.
   - **Change:** after 3 unchanged polls, switch to `jobs` metadata polling plus exponential backoff; emit `log_bytes_delta` and `unchanged_log_polls`.
   - **Estimated reduction:** potentially dozens of redundant log fetches during a 40-minute stall window.
   - **Rate-limit impact:** meaningful risk reduction on long reviews.

3. **`issue_pr_status` can devolve into GraphQL + per-issue REST**
   - **Evidence:** deep-dive run `28311876417` warned `Orchestrator-issue batch detection failed; falling back to per-issue REST.`
   - **Redundancy pattern:** one batch GraphQL miss can fan out to per-issue REST calls.
   - **Change:** emit `GH_API_FALLBACK path=issue_pr_status reason=<graphql_error_class> issue_count=<n> rest_calls=<n>` and memoize fetched issue bodies/labels within the run.
   - **Estimated reduction:** low in the sampled run (no linked issues), but can become N+1 on busier PRs.
   - **Rate-limit impact:** medium under larger linked-issue sets.

4. **Repeated missing-log fetches in drift/collector paths**
   - **Evidence:** run `28311356890` logged 11 missing-log warnings; collector errors show two explicit 404 archive misses.
   - **Redundancy pattern:** re-fetching runs already known to have no archive.
   - **Change:** cache negative archive results for the window and emit one aggregate summary instead of N individual warnings.
   - **Estimated reduction:** one avoided GH log fetch per repeated-miss run ID, plus much lower warning noise.
   - **Rate-limit impact:** low-to-medium.

## Prompt Cache & Memory System

- **Cache is unobservable right now.** The parser supports `cache_hit_rate`, `or_cache_write_tokens`, and `or_cache_read_tokens`, but the 1000-run summary shows `cache_hit_rate=null` and both token counters at `0`. That is not evidence of zero cache hits; it is evidence of missing or disabled measurement.
- **Context pressure is localized and real.** All `10` `CONTEXT_BUDGET_WARN`s occurred in long `review_autofix` runs (`28289469014`, `28290634570`, `28292769056`, `28294810094`, `28295529547`). `BREAK_GLASS` stayed at `0`, so prompt growth—not policy override—is the signal to watch.
- **Semble looks healthy but not yet provably valuable.** Runtime Semble emitted `25` queries with `295,198` bytes total and `0` runtime fallbacks. CI fallbacks were contract-test only. I cannot tell whether Semble is reducing prompt expansion or just adding context because the logs do not include pre/post prompt sizes.
- **Serena is effectively dark in this window.** Full-window Serena totals were all zero, and long review runs such as `28308671786` showed `SERENA_AVAILABLE=false`. That should be logged as disabled/unconfigured, not silently interpreted as “healthy.”
- **Concrete logging additions:**
  - `PROMPT_CACHE_METRICS phase=<...> model=<...> prefix_hash=<...> static_prefix_bytes=<...> dynamic_suffix_bytes=<...> attempted=<bool> result=<hit|miss|disabled>`
  - `CONTEXT_BUDGET_WARN phase=<...> estimated_tokens=<...> budget=<...> top_contributors=<diff,memory,history,semble>`
  - `SEMBLE_QUERY_SUMMARY target=<...> chunks=<n> bytes=<n> context_before_bytes=<n> context_after_bytes=<n>`
  - `SERENA_DISABLED reason=<not_configured|bootstrap_failed|opted_out>` once per run when Serena is off

## Orchestrator Health

- **Mostly healthy control plane:** `orchestrate_poll` was `33/34` success, `issue_pr_status` was `5/5` success, and `forward_merge_stable_to_main` fail-opened safely in run `28311876313` by opening fallback PR `#3557` when a direct merge conflicted.
- **Primary pain point is delayed or ambiguous handoff, not total outage.** In `28311876313`, dispatching `internal-review.yml` failed and the workflow explicitly relied on the poller to pick the PR up next cycle. In `28311876417`, support-ref fallback and incomplete orchestrator classification both degraded gracefully, but only via warnings.
- **Top observability gap:** all `800` `other` outcomes in the 1000-run window belong to `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`, each with `p50≈1s`. **Inference:** these are control-plane handoff/update workflows, not failed work, but they distort top-line success-rate reporting.
- **Smallest safe mitigations:**
  - add `handoff_kind` / `terminal_reason` to control-plane workflows so `other` does not mean “unknown”
  - emit `dispatch_result`, `next_poll_eta`, `support_ref_used`, `orchestrator_batch_mode`, and `rest_fallback_issue_count`
  - track `queue_wait_ms` separately from active orchestration time

## Pipeline Flow Bottlenecks

| Flow phase | Dominant bottleneck | Evidence | Best next fix |
|---|---|---|---|
| Clarify → Plan → Implement | Metrics dilution, not runtime cost | All `800` `other` outcomes are in these control-plane families; each has `p50≈1s` | Add explicit `terminal_reason` / `handoff_kind` |
| Review/Autofix | Long compute + watchdog + bootstrap failures | `review_autofix` `avg=980.2s`, `p95=3825.3s`; missing `assemble_prompt.sh`; 3017s editor failure; 3419s stall cancel | Support-stage manifest + structured liveness logging |
| Validate / CI | Frequent fast failures | `ci` `10/12` failed; actionlint, Ruff, and validation-selftest signatures are concrete | Structured subgate failure signatures |
| Orchestrate / Poll | Fixed tax + queue wait | `orchestrate_poll` `p50=202s`; sampled runner-wait warnings | Split queue vs active work; dynamic backoff |
| Merge / Conflict handling | Safe fallback adds a poll cycle | `forward_merge_stable_to_main` run `28311876313` opened PR `#3557` and deferred review to poller | Emit `dispatch_failed_reason` and `next_poll_eta` |
| Observability | Collector blind spots | `workflow_log_analysis` `10/10` failure; archive 404s; drift log-fetch noise | Structured archive-fetch diagnostics |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` long tail (`86` runs, `p95=3825.3s`, `avg=980.2s`)
  - `orchestrate_poll` fixed baseline (`p50=202s`)
  - CI merge friction (`10/12` failures)

- **Top failure modes**
  - missing staged support asset `assemble_prompt.sh` in `review_autofix` preflight
  - false review-stall detection on long reviewer steps
  - observability gaps: log-archive 404s and historical `workflow-log-analysis` actionlint breakage
  - contract drift in CI (`slop_scan_local.py` Ruff failure; missing workflow classification for `check_failure_triage.yml`)

- **Highest-cost drivers**
  - `197` OR calls with no usable OR token totals
  - long `review_autofix` runs with `12-26` OR calls each
  - no usable prompt-cache telemetry
  - rerun/cancel waste is likely larger than direct recorded Codex token spend (`20,260` tokens across `10` calls)

- **Top 3 prioritized actions**
  1. Emit and validate a staged support manifest immediately after `stage_workflow_support.sh`.
  2. Emit structured review-liveness logs and stop treating frozen log byte counts as hard inactivity.
  3. Restore OR/cache/Serena telemetry emission for the fields already expected by `scripts/cost_audit.py`.

## Metrics Appendix

| Overall window | Value |
|---|---:|
| Total runs | 1000 |
| Success | 149 (14.9%) |
| Failure | 32 (3.2%) |
| Cancelled | 19 (1.9%) |
| Other | 800 (80.0%) |
| Avg duration | 105.5s |
| p50 duration | 1s |
| p95 duration | 225s |
| Runs with parsed log telemetry | 119 (11.9%) |
| Runs with `log_summary` coverage | 50 (5.0%) |

**Note:** all `800` `other` outcomes are concentrated in `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`, so the raw top-line success rate is not a good end-to-end health KPI by itself.

| Workflow family | Runs | Success / Failure / Cancelled / Other | p50 | p95 | Avg |
|---|---:|---:|---:|---:|---:|
| review_autofix | 86 | 57 / 12 / 17 / 0 | 23s | 3825.3s | 980.2s |
| orchestrate_poll | 34 | 33 / 0 / 1 / 0 | 202s | 429.2s | 225.1s |
| ci | 12 | 1 / 10 / 1 / 0 | 76s | 1767.3s | 395.2s |
| workflow_log_analysis | 10 | 0 / 10 / 0 / 0 | 0s | 0s | 0s |
| copilot_pull_request_reviewer | 11 | 11 / 0 / 0 / 0 | 201s | 407.5s | 234.3s |
| issue_pr_status | 5 | 5 / 0 / 0 / 0 | 16s | 78.8s | 39.0s |

| Cost / cache / MCP metric | Overall | Notes |
|---|---:|---|
| `codex_tokens_used` | 20,260 | 10 Codex calls total |
| `codex_calls` | 10 | concentrated in `review_autofix` |
| `or_calls` | 197 | all in `review_autofix` |
| `or_prompt_tokens / or_completion_tokens / or_total_tokens` | 0 / 0 / 0 | telemetry gap, not believable as true cost |
| `or_cache_write_tokens / or_cache_read_tokens` | 0 / 0 | cache usage not measurable |
| `cache_hit_rate` | null | unmeasured |
| `break_glass_count` | 0 | no policy overrides observed |
| `context_budget_warn_count` | 10 | all in `review_autofix` |
| `wall_clock_p50_ms / wall_clock_p99_ms` | 9,000 / 6,180,120 | 119 telemetry samples |
| `semble_query_calls / bytes` | 25 / 295,198 | ~11.8 KB/query; runtime use only in `review_autofix` |
| `semble_fallbacks` | 15 | all contract-test fallbacks |
| `semble_runtime_fallbacks` | 0 | no runtime rollout break seen |
| `serena_query_calls / response_bytes / tool_calls` | 0 / 0 / 0 | effectively dark |
| `serena_fallbacks` | 0 | no sampled runtime use |
| `serena_probe_ok / failed / skipped` | 0 / 0 / 0 | no probe evidence |

| Sampled GH API / queue signal | Count | Evidence |
|---|---:|---|
| Runner-wait warnings | 12 sampled runs | spread across `review_autofix`, `issue_pr_status`, `orchestrate_poll`, and short control workflows |
| `resolve-claude-branch-pr` PR-dedupe hotspot | 6 sampled runs | `28310924438`, `28311507512`, `28309225484`, `28309913750`, `28310388196`, `28308809077` |
| False-stall review polling | 2 sampled runs | `28310389106`, `28311508326` |
| Drift log-fetch warnings | 1 run / 11 warnings | `28311356890` |
| Issue-status GraphQL → REST fallback | 1 deep dive | `28311876417` |
| Observed 429 / secondary rate-limit events | 0 sampled | none seen in summaries/deep dives |

| AI memory deep-dive metric | Value |
|---|---:|
| Parseable `AI_MEMORY_TELEMETRY` JSON events | 61 |
| Marker lines seen | 67 |
| `retrieve` ops | 13 |
| Retrieve hit rate | 100% |
| Avg `estimated_tokens` / `token_budget` | 868 / 1400 |
| `keyword_method=llm` | 13 / 13 retrieves |
| Zero-hit retrieves | 0 |
| `fail_open:true` retrieves | 0 |
| `enabled:false` retrieves | 0 |
| Helper-missing warning observed | yes (`28308671786`, `28310393975`) |

| MCP probe availability | `probe_ok` | `probe_failed` | `probe_skipped` | Note |
|---|---:|---:|---:|---|
| Serena (all observed targets) | 0 | 0 | 0 | No `SERENA_PROBE` lines in the 1000-run window; treat as disabled or uninstrumented, not verified healthy |

Other `<NAME>_QUERY` / `<NAME>_FALLBACK` / `<NAME>_PROBE` telemetry was not observed in this window. Copilot review summaries did mention `github-mcp-server` and `playwright` as connected with `0` invocations, but that was outside the collector’s aggregated MCP counters.
