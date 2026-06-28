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

## Deep Audit — Workflows & Scripts (2026-06-28)

### Section 1: Bug & Correctness Sweep

- **ID:** BUG-001  
  **File path(s):** `.github/workflows/review_autofix.yml:5133-5179, 5217-5291`  
  **Severity:** High  
  **Category tag:** `bug`  
  **Description:** The `editor-changes-lost` recovery path is weaker than the earlier post-commit retrigger path. The post-commit block tries `review_autofix.yml`, then the caller workflow, and when the caller resolves back to `review_autofix.yml` it also falls back to `ai-review.yml` and `internal-review.yml` (`5133-5179`). The later `Re-dispatch review on editor-changes-lost` block (`5217-5291`) uses raw `gh workflow run` calls with no retry/registration check and omits that final `ai-review.yml` / `internal-review.yml` fallback branch entirely. If direct dispatch fails while `github.workflow_ref` resolves to `review_autofix.yml`, this path only emits a warning and exits. Because this branch produces no commit, there is no guaranteed `pull_request.synchronize` event to rescue it.  
  **Recommended fix:** Route both retrigger paths through one helper, preferably `scripts/dispatch_and_watch_workflow_run.sh:13-91`, so they share the same candidate workflow list, retry/backoff, and dispatch-registration verification. At minimum, make the `editor-changes-lost` branch mirror the `ai-review.yml` / `internal-review.yml` fallback logic already present in `5133-5179`.

### Section 2: GitHub API Call Redundancy Audit

- **ID:** API-001  
  **File path(s):** `scripts/gh_helpers.sh:562-614, 630-679`  
  **Severity:** High  
  **Category tag:** `api-redundancy`  
  **Description:** `gh_api_json_to_file` and `curl_gh_api` retry every non-success case up to `GH_RETRY_MAX_ATTEMPTS` instead of stopping on permanent failures. In `gh_api_json_to_file`, any non-rate-limit `gh` failure falls into exponential backoff (`596-601`). In `curl_gh_api`, every non-2xx HTTP status except explicit rate limits is retried (`660-670`). That means permanent 401/404/422-class failures burn up to five calls plus backoff sleeps instead of failing fast; this amplifies already-observed missing-log/archive 404 behavior.  
  **Current call count:** Up to `5` attempts per permanent failure.  
  **Proposed call count:** `1` for permanent permission/not-found/validation errors; keep retries only for rate limits, 5xx, network faults, or truncated JSON.  
  **Existing pattern to extend:** `scripts/gh_helpers.sh:_is_gh_rate_limit`, `_parse_reset_header`, `_sleep_until_reset`.  
  **Recommended fix:** Add permanent-error classification for 400/401/404/422 and non-rate-limit 403 responses, then short-circuit instead of backing off. Keep the current wait/reset path only for truly transient failures.

- **ID:** API-002  
  **File path(s):** `.github/workflows/issue_pr_status.yml:358-426, 584-599`  
  **Severity:** Medium  
  **Category tag:** `api-redundancy`  
  **Description:** When the orchestrator GraphQL classification batch fails, the workflow falls back to per-issue REST lookups (`403-426`). Later, if classification is still incomplete, the merged-alert suppression path refetches linked issue metadata one-by-one again (`584-599`) instead of reusing the earlier fallback results. Worst case, one run does one GraphQL request plus `N` REST issue fetches, then another `N` REST fetches over the same issue set.  
  **Current call count:** Worst case `1 + N + N`.  
  **Proposed call count:** `1 + N`.  
  **Existing pattern to extend:** The per-run cache style already used in `scripts/orchestrate_poll_process.sh` (for example `STALL_MANAGED_LINKED_PR_CACHE`) and its GraphQL alias batching helpers such as `_fetch_candidate_issue_details_graphql`.  
  **Recommended fix:** Persist the first fallback’s `{issue_number -> labels/body/classification}` map into `${RUNTIME_DIR}` or a step output, and have the merged-alert suppression branch reuse that cache instead of reissuing `_safe_gh_jq` per issue.

- **ID:** API-003  
  **File path(s):** `scripts/check_external_branch_advance.sh:175-202`  
  **Severity:** Medium  
  **Category tag:** `api-batching`  
  **Description:** Commit attribution verification is done with one `gh api repos/${REPOSITORY}/commits/${sha}` call per advancing self-like commit inside a loop. The local git scan has already identified the candidate SHA list; the remaining GitHub lookups are an `N`-request fanout for author/committer identity only.  
  **Current call count:** `N` commit API calls for `N` self-like advancing commits.  
  **Proposed call count:** `1` batched GraphQL alias request for the SHA set.  
  **Existing pattern to extend:** The alias-batching style in `scripts/orchestrate_poll_process.sh:_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql`.  
  **Recommended fix:** Add a small batched commit-identity helper that accepts a SHA list and returns normalized author/committer logins in one request, then make `check_external_branch_advance.sh` consume that map instead of calling `gh api` per SHA.

### Section 3: Code Duplication & Modularization Opportunities

- **ID:** DUP-001  
  **File path(s):** `.github/workflows/clarify.yml:215-338`; `.github/workflows/plan.yml:271-367`; `.github/workflows/issue_pr_status.yml:133-170`; `.github/workflows/orchestrate_clarify_respond.yml:277-360`; `.github/workflows/orchestrate_poll.yml:330-365`; `.github/workflows/implement.yml:840-880`; `scripts/stage_workflow_support.sh:826-905`  
  **Severity:** High  
  **Category tag:** `duplication`  
  **Description:** The repo already has a manifest-driven support-staging implementation in `scripts/stage_workflow_support.sh`, and current callers already exist in `.github/workflows/review_autofix.yml:1333-1348` and `.github/workflows/validate.yml:258-416`. But several other workflows still hard-code their own script lists, fallback rules, `.gitignore` generation, schema staging, and prompt-file staging. Inference: this multi-list pattern creates the same drift vector behind the already-reported missing-support-file failures, because a required-file addition must be propagated to many inline copy loops.  
  **Shared module:** `scripts/stage_workflow_support.sh`  
  **Signature/interface:** `bash "${helper_path}" validate --manifest "<manifest.json>"`  
  **Callers:** `clarify.yml`, `plan.yml`, `issue_pr_status.yml`, `orchestrate_clarify_respond.yml`, `orchestrate_poll.yml`, `implement.yml`  
  **Recommended fix:** Move these workflows onto manifest-driven staging, reusing the pattern already present in `validate.yml` and `review_autofix.yml`. Keep workflow-specific manifests small and declarative; keep copy/fallback behavior centralized in `stage_workflow_support.sh`.

- **ID:** DUP-002  
  **File path(s):** `.github/workflows/implement.yml:178-210, 4393-4418`; `scripts/gh_helpers.sh:982-1121`  
  **Severity:** Medium  
  **Category tag:** `duplication`  
  **Description:** `implement.yml` embeds the same issue-timeline cross-reference query twice: once in the early “Safety check for existing PR” step and again in the `gh pr create` failure recovery path. The two blocks share the same REST timeline endpoint and nearly the same `jq` filter, but shape the output differently (`#N URL` list vs first URL only). If the timeline-filter contract changes, both blocks must be updated in lockstep.  
  **Shared module:** `scripts/gh_helpers.sh` (or a small new `scripts/issue_pr_helpers.sh`)  
  **Signature/interface:** `resolve_open_linked_prs <repo> <issue_number> [--first-url]`  
  **Callers:** `Safety check for existing PR`, `Create Pull Request` failure recovery  
  **Recommended fix:** Extract the cross-reference lookup into one helper returning normalized JSON or URLs, then let each step format that result as needed. Reuse `gh_issue_timeline_with_cross_refs` as the base implementation if helper staging can be moved early enough.

- **ID:** DUP-003  
  **File path(s):** `scripts/assemble_prompt.sh:12-41, 72-93`; `scripts/render_prompt.sh:12-41, 138-159`  
  **Severity:** Low  
  **Category tag:** `duplication`  
  **Description:** `resolve_prompt_file` and `resolve_assembly_source_path` appear with effectively identical bodies in both prompt-entry scripts. Any future change to prompt-root resolution or `_templates` lookup has to be made twice.  
  **Shared module:** New `scripts/prompt_path_helpers.sh`  
  **Signature/interface:** `resolve_prompt_file <path>` and `resolve_assembly_source_path <path>`  
  **Callers:** `scripts/assemble_prompt.sh`, `scripts/render_prompt.sh`  
  **Recommended fix:** Move the shared path-resolution helpers into one sourced module, leaving each entrypoint responsible only for its script-specific environment and execution flow.

- **ID:** DUP-004  
  **File path(s):** `scripts/review_run_reviewers.sh:35-73`; `scripts/review_apply_fixes.sh:73-111`; `scripts/review_rb_judge.sh:203-241`  
  **Severity:** Low  
  **Category tag:** `duplication`  
  **Description:** `emit_context_budget_warn_for_prompt` is copied three times with the same shell/Python bridge into `cost_audit.py`. That makes context-budget telemetry changes easy to miss in one caller and complicates future OR/cache instrumentation cleanup.  
  **Shared module:** `scripts/codex_helpers.sh`  
  **Signature/interface:** `emit_context_budget_warn_for_prompt <phase> <prompt_path> <model>`  
  **Callers:** `review_run_reviewers.sh`, `review_apply_fixes.sh`, `review_rb_judge.sh`  
  **Recommended fix:** Promote the helper into `scripts/codex_helpers.sh` (or another shared Codex/runtime module) and source it from the three review-phase scripts.

### Section 4: Expression Size Limit Risk Assessment

- **ID:** EXPR-001  
  **File path:** `.github/workflows/plan.yml:979-1255`  
  **Severity:** High  
  **Category tag:** `expression-limit`  
  **Description:** The `Run Codex planning` step embeds a very large inline prompt heredoc plus prompt-build/orchestration logic in one `run:` block that contains `${{ }}` interpolations. Estimated current expression size is **~18,256 chars**, leaving only **~2,744 chars** before GitHub’s 21,000-char template-expression hard limit.  
  **Recommended fix:** Move the inline planning instructions into a checked-in prompt file under `prompts/` and keep the workflow step as a thin wrapper that renders/assembles the prompt. If needed, split prompt construction and model invocation into separate steps.

- **ID:** EXPR-002  
  **File path:** `.github/workflows/memory_maintenance.yml:45-391`  
  **Severity:** Medium  
  **Category tag:** `expression-limit`  
  **Description:** The `Extract repository learnings (fail-open)` step combines two large inline Python heredocs, OpenRouter request wiring, and `${{ github.* }}` interpolations in one `run:` block. Estimated current expression size is **~15,168 chars**, leaving **~5,832 chars** of headroom.  
  **Recommended fix:** Extract the two Python bodies into `scripts/*.py` entrypoints and keep the workflow step to environment setup plus script invocation only.

- **ID:** EXPR-003  
  **File path:** `.github/workflows/implement.yml:3606-3825`  
  **Severity:** Medium  
  **Category tag:** `expression-limit`  
  **Description:** The `Destructive-commit guard — label + alert on rejection` step inlines large markdown/comment/Telegram templates and many `${{ github.* }}` interpolations in a single `run:` block. Estimated current expression size is **~15,096 chars**, leaving **~5,904 chars** of headroom.  
  **Recommended fix:** Extract the scope-block/destructive-block handler into a script such as `scripts/handle_destructive_or_scope_block.sh`, or split scope-block and destructive-block handling into separate smaller workflow steps.

Current workflow file size check: I did **not** find any workflow above the 800 KB warning threshold. The largest current files are `review_autofix.yml` (**351,064** chars), `implement.yml` (**274,861**), and `plan.yml` (**108,705**).

### Section 5: Cross-Cutting Concerns

No `TODO`, `FIXME`, or `HACK` markers matched under `.github/workflows`, `scripts/*.sh`, or `scripts/*.py`. I also did not find a dead-code candidate I could prove safely without risking a false positive, and I did not find a standalone ShellCheck-class issue stronger than the consistency item below.

- **ID:** CONSIST-001  
  **File path(s):** `scripts/watchdog_helpers.sh:8-32`; `scripts/review_run_reviewers.sh:94-108`; `scripts/review_rb_judge.sh:115-129`; `scripts/self_heal_validation.sh:143-157`; `scripts/review_conflict_resolve.sh:173-187`  
  **Severity:** Medium  
  **Category tag:** `consistency`  
  **Description:** `read_codex_stall_guard_state` already exists in `scripts/watchdog_helpers.sh`, but the same parser is redefined in four other scripts. Any future stall-guard state change must now land in five places, which is exactly the kind of contract drift that will make step-aware stall/liveness work brittle.  
  **Recommended fix:** Make `scripts/watchdog_helpers.sh` the single owner of stall-guard state parsing. Source it from the four callers, and if a warning-emitting wrapper is needed, add `read_codex_stall_guard_state_with_warning <status_file> <context>` there instead of keeping local copies.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 4 | BUG-001, API-001, DUP-001, EXPR-001 |
| Medium | 6 | API-002, API-003, DUP-002, EXPR-002, EXPR-003, CONSIST-001 |
| Low | 2 | DUP-003, DUP-004 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | ~8 | Large |
| API call optimization | 3 | Medium |
| Code modularization | ~6 | Medium |
| Expression size reduction | ~6 | Medium |
| Medium/Low fixes | ~4 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-06-28)

### Safety Tag Legend
`SAFE_TO_MERGE` means the repository already proves the calls are equivalent enough to consolidate without changing behavior. `NEEDS_VERIFICATION` means the overlap is real but static reading does not fully prove freshness/error-semantic parity. `RISKY_SKIP` means the call sits in a retry/poll/race-defensive path, uses pagination, or lives in `scripts/orchestrate_poll_process.sh`, so it should not be auto-implemented even if it looks redundant.

### Consolidation Candidates (MERGE-###)

- **ID:** `MERGE-001`  
  **Safety tag:** `RISKY_SKIP`  
  **File path and line ranges:** `.github/workflows/test-and-mark-stable.yml:3036-3045`  
  **Current call count:** `2` per poll iteration  
  **Proposed call count:** `1` per poll iteration  
  **Endpoint(s):** `GET /repos/{repo}/actions/runs/{run_id}`  
  **Evidence:** the polling loop reads the same run resource twice back-to-back, once for `.status` and once for `.conclusion`:
  ```bash
  EXISTING_STATUS=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" \
    --jq '.status // ""' 2>/dev/null || echo "")
  EXISTING_CONCLUSION=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" \
    --jq '.conclusion // ""' 2>/dev/null || echo "")
  ```
  Both fields come from the same JSON document, and there is no intervening mutation inside the loop body.  
  **Proposed fix:** replace the pair with one fetch into a temp JSON string/object in the same loop iteration, then parse both `.status` and `.conclusion` locally.  
  **Safety rationale:** `RISKY_SKIP` because the duplicate calls are inside a polling loop; consolidating them changes how transient failures surface and could alter the timeout/empty-string behavior.  
  **Downstream signal:** Do not auto-implement. Manual review must preserve the current 5s poll cadence, 600s timeout, and fail-open handling when the run lookup intermittently returns empty data.

### Redundant Re-Fetch (REUSE-###)

- **ID:** `REUSE-001`  
  **Safety tag:** `NEEDS_VERIFICATION`  
  **File path and line ranges:** `scripts/review_collect_pr_metadata.sh:251-278`, `scripts/review_rb_judge.sh:834-885`  
  **Current call count:** `1` early GraphQL fetch, then `1` additional GraphQL fetch plus `up to N` `GET /issues/{issue_number}` calls in the judge path  
  **Proposed call count:** keep the early GraphQL fetch, then `0` additional GraphQL fetches and `at most 1` live issue fetch only if current labels still need refreshing  
  **Endpoint(s):** GraphQL `repository.pullRequest(number).closingIssuesReferences(first:50)`, `GET /repos/{repo}/issues/{issue_number}`  
  **Evidence:** the metadata collector explicitly says it fetches linked issues early and exports a cache:
  ```bash
  # Fetch linked issue title+body via GraphQL — single call that also
  # populates LINKED_ISSUES_JSON early so the late-stage cache step can
  # skip its own fetch.
  if gh_retry "${_linked_tmp}" api graphql ... nodes{number title body} ...
  ...
  printf 'LINKED_ISSUES_JSON=%s\n' "${_linked_numbers}" >> "${GITHUB_ENV}"
  ```
  But the judge ignores that early cache and re-queries linked issues, then walks issues one by one:
  ```bash
  ISSUE_NUMBERS="$(gh_retry gh api graphql ... nodes { number } ...)"
  ...
  ISSUE_META_JSON="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" || echo '{}')"
  BODY="$(printf '%s' "${ISSUE_META_JSON}" | jq -r '.body // ""' ...)"
  ```
  **Proposed fix:** make `scripts/review_rb_judge.sh` consume the already-exported linked-issue artifact first; if the existing `LINKED_ISSUES_JSON` payload is too small, extend the existing `review_collect_pr_metadata.sh` GraphQL query to include the fields the judge needs and export them in a machine-readable file/env var. Keep a single live `GET /issues/{FIRST_ISSUE}` fallback only if judge-time label freshness is required.  
  **Safety rationale:** `NEEDS_VERIFICATION` because `review_rb_judge.sh` currently reads live first-issue labels/body, while the exported cache today only guarantees linked issue numbers.  
  **Downstream signal:** Verify before changing: (1) `review_collect_pr_metadata.sh` runs on every path that can reach `review_rb_judge.sh`, including `force_rb_judge=true` dispatches; (2) no earlier step depends on judge-time freshness of first-issue labels/body; (3) all judge branches still behave correctly if the cache is missing and the live fallback remains single-call.

### Dead Calls (DEAD-API-###)

- **ID:** `DEAD-API-001`  
  **Safety tag:** `RISKY_SKIP`  
  **File path and line ranges:** `scripts/orchestrate_poll_process.sh:17601-17618`, `scripts/orchestrate_poll_process.sh:12549-12599`  
  **Current call count:** `1`  
  **Proposed call count:** `0`  
  **Endpoint(s):** `GET /repos/{repo}`  
  **Evidence:** the standalone conflict sweep fetches the repo default branch into `DEFAULT_BRANCH`, but the sweep never reads it:
  ```bash
  CONFLICT_SWEEP_FIXED=0
  DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"

  for (( sidx=0; sidx<STANDALONE_COUNT; sidx++ )); do
  ```
  The only helper it dispatches in this block is `_dispatch_review_for_conflicts`, whose signature is just `<pr_number> <head_ref>` and whose body (`scripts/orchestrate_poll_process.sh:12551-12599`) does not read `DEFAULT_BRANCH`. The remainder of the sweep uses `S_PR`, `S_HEAD`, `S_BASE`, `S_PR_JSON`, and `S_HEAD_SHA`, not `DEFAULT_BRANCH`.  
  **Proposed fix:** remove only the standalone-conflict-sweep lookup at `scripts/orchestrate_poll_process.sh:17614`; leave the other `DEFAULT_BRANCH` fetches elsewhere in the file unchanged.  
  **Safety rationale:** `RISKY_SKIP` because the dead-looking call is inside `scripts/orchestrate_poll_process.sh`, which the repo treats as an upstream-race-defensive path requiring manual review.  
  **Downstream signal:** Do not auto-implement. Manual review must confirm no helper reached from the standalone conflict sweep relies on the ambient `DEFAULT_BRANCH` shell variable before removing just this lookup.

### Cross-References to Deep Audit Section

- `API-001`: `RISKY_SKIP` — shared retry/backoff helpers are themselves the rate-limit/recovery path, so permanent-error fast-fail logic should be reviewed manually rather than auto-applied.
- `API-002`: `NEEDS_VERIFICATION` — caching the first fallback issue metadata is sound, but the later merged-alert suppression path may depend on fresher labels/body than the first fallback captured.
- `API-003`: `NEEDS_VERIFICATION` — batching commit attribution matches §15, but the replacement must prove GraphQL returns the same author/committer-login semantics and null-handling as the current per-SHA REST loop.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | REUSE-001 |
| RISKY_SKIP | 2 | MERGE-001, DEAD-API-001 |

### Implement-Stage Handoff

- No SAFE_TO_MERGE findings in this pass.
