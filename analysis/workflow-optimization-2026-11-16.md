I've finished gathering evidence. The workflow-log folders were not mounted, so the step logs couldn't be read, and the report below rests on the supplied analysis context plus the repository source.

## Executive Summary

- **Most of the wall-clock time is spent waiting for a runner, not running the job (confidence: high).** Several workflows ran far longer than their own job timeouts allow:
  - `lint-pr-body-auto-close.yml` has a 5-minute timeout (line 47) and no concurrency group, yet runs 43927314324, 43926282177 and 43927304744 took 2250 s, 2233 s and 2227 s. That is at least 32 minutes of queueing each.
  - `integration-pr-readiness.yml` has a 5-minute timeout (line 44), yet run 43926281605 took 2419 s.
  - CI's longest possible compute path is about 30 minutes: a test job (20-minute timeout) → `lint` (5) → `final-ci-failed` (5). Yet CI [pr:9597] (run 43925484798) took 7834 s, meaning at least ~100 minutes was not compute.
  - Inference: the account is at the GitHub-hosted concurrent-job cap (Free plan 20, Pro 40). At 18:07:12–18:07:20, three full CI runs (8 parallel jobs each) started alongside CI quick, review/autofix, the auto-close lint and the readiness check.
  - Estimated impact: removing this queueing would cut p95 for gate workflows from ~37 minutes to under 5 minutes.
- **The token telemetry is blind to Claude-engine spend (confidence: high).**
  - `build_run_cost_telemetry` (`scripts/cost_audit.py:609-613`) keeps only `RUN_COST_TELEMETRY_FIELDS` (lines 209-245).
  - That list leaves out `CLAUDE_COUNT_KEYS` (lines 128-134) and `claude_cost_usd`.
  - As a result, every run reports `codex_calls=0` and `or_calls=0`, including four review/autofix runs of 79–114 minutes. The "0 tokens" total is a collection gap, not zero spend. Impact: no cost optimization can be measured until this is fixed.
- **The deep-dive log selection wastes its "recent" slots on skipped runs (confidence: high).**
  - All 100 rows in `recent_runs` are `skipped` runs, or pollers cancelled before their first step.
  - 15 of them had log downloads attempted, and every one returned `log_download_status=empty_archive`.
  - The cause is that `select_runs_for_log_export_categories` (`scripts/collect_workflow_logs.py:1469`) and the analyzer (`scripts/analyze_workflow_logs.py:432`) do not filter `conclusion=skipped`.
- **The only real failure was a cheap documentation check found after 93 minutes (confidence: medium).** CI run 43925460837 failed at `tests-release-and-log-analysis / Inventory parity` after 5589 s. `tests/inventory_parity.py` is a static check of `docs/INVENTORY.md`, `README.md` and `agents.md`. No implement or autofix pre-push path runs it: the only references are in `ci.yml`.
- **The orchestrator poller effectively ticks every 37–61 minutes, not every 15 (confidence: high).**
  - The cron is `*/15 * * * *` (`.github/workflows/internal-orchestrate-poll.yml:5`).
  - Of 11 poller runs, 9 were `cancelled_before_first_step`: superseded while pending, because of `cancel-in-progress: false` (`orchestrate_poll.yml:101-102`).
  - The 2 that completed took 3666 s (run 43927831760) and 2236 s (run 43938174138).
  - The `poll` job has no `timeout-minutes`, so it falls back to GitHub's 360-minute default.
- **Most runs are skipped no-ops (confidence: high).** 883 of 993 runs (89%) were "other", meaning skipped:
  - clarify: 210/212
  - plan: 207/208
  - implement: 207/209
  - orchestrate_clarify_respond: 209/209
  - In one burst, 44 skipped runs were created in 11 seconds (19:50:05–19:50:16): 11 events, each fanning out to the same 4 workflows.
  - These use little runner time, but they swamp the telemetry (repository-wide p50 is 1 s) and the collector's API budget.

## Speed Optimizations

Ranked by expected latency reduction.

1. **Measure, then reduce, runner-queue wait (critical path).**
   - **Evidence:** the 5-minute-timeout workflows above ran 27–37 minutes. `cancel_on_pr_close.yml` (5-minute timeout, line 13) run 43942433833 took 1933 s. CI runs took 4408–7834 s against a ~30-minute compute ceiling. The review/autofix p95 is 6230 s.
   - **Root cause (inference):** too many jobs compete for the concurrent-job cap. Each full CI run alone fans out 8 parallel jobs (`static-checks`, `tests-hooks-and-orchestrator`, `tests-heal-plan-and-validation`, `tests-promote-stall-and-review`, `tests-release-and-log-analysis`, `orchestrate-poll`, `tests-system-python`, `tests-sandbox-python`). Three full CI runs plus CI quick started within 8 seconds at 18:07.
   - **Exact change (step 1, diagnostic, zero risk):** in the collector, derive `queue_seconds = first job started_at − run created_at` from the jobs API for runs already selected for deep-dive, which avoids new per-run calls. In the meantime, add a first-step log line to the long workflows (`ci.yml`, `review_autofix.yml`, `orchestrate_poll.yml`) and register the new prefix through an `agents.d/` fragment:
     ```bash
     echo "RUNNER_QUEUE_WAIT: workflow=${GITHUB_WORKFLOW} run_id=${GITHUB_RUN_ID} job=${GITHUB_JOB} job_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
     ```
   - **Exact change (step 2, after measuring):** stop launching full CI and CI quick for the same head SHA at the same time. CI quick uses a per-run concurrency group (`ci.yml:82`, `ci-quick-{run_id}`), so it never deduplicates. CI quick ran 15 times in the window, at 785–2750 s each.
   - **Estimated savings:** 25–35 minutes off p95 for gate workflows, and 60–100 minutes off full CI.
   - **Risk:** step 1 is none; step 2 is low to medium, because it changes when gates run.
2. **Catch inventory-parity drift before pushing (critical path for failed PRs).**
   - **Evidence:** run 43925460837 spent 5589 s before failing on a static docs check.
   - **Change:** run `PYTHONDONTWRITEBYTECODE=1 python3 tests/inventory_parity.py` in the implement and autofix post-edit validation. The step must stay where it is in CI, because `tests/test_ci_inventory_parity_order_contract.py` requires it to share a job with the log collector and analyzer coverage gates.
   - **Savings:** one full CI cycle (~1.5 h) per parity regression.
   - **Risk:** low; it is a read-only check.
3. **Bound the poller's run time (orchestrator cadence).** Add `timeout-minutes` to the `poll` job and log how long each phase takes, so the 61-minute tick (run 43927831760) can be explained. Expected result: tick cadence moves back toward 15 minutes.
4. **Micro-optimization: Semble lazy bootstrap.** Run 43927831760 paid a 49,233 ms bootstrap for 2 queries. That is under 2% of the run, so it is low priority.

## Cost Optimizations

1. **Restore Claude-engine token telemetry (enables every other cost item).**
   - **Evidence:** in `cost_audit.py:609-613` and `:209-245`, the fields `claude_calls`, `claude_input_tokens`, `claude_output_tokens`, `claude_cache_write_tokens`, `claude_cache_read_tokens` and `claude_cost_usd` are absent from every `cost_telemetry` block.
   - **Change:** add these keys to `RUN_COST_TELEMETRY_FIELDS`, and the count keys to `AGGREGATABLE_COST_FIELDS`. The change only adds fields.
   - **Savings:** no direct saving, but it makes model, reasoning-level and cache decisions measurable.
   - **Quality risk:** none.
2. **Model and reasoning level: not assessable.** `orchestrate_poll.yml:70-73` defaults `MODEL_EDITOR=openai/gpt-6-sol` with `MODEL_REASONING_EFFORT_JUDGE=high`. With no token data, I cannot recommend lowering either.
3. **Semble is net-small and not noisy.** The single poll run logged 2 queries, 11,529 bytes and 5 sources, with `static_dup_bytes=0` and `echo_lines_dropped=0`. There is no sign of low-value prompt expansion. There is also not enough volume to show it reduces prompts.
4. **Serena:** no `SERENA_QUERY`, `SERENA_FALLBACK` or `SERENA_PROBE` lines were present, so tool-call replacement cannot be evaluated.
5. **Avoidable reruns:** only 1 failure (CI 43925460837), and every run was attempt 1, so rerun cost is negligible. The real cost is the 93 minutes of runner time behind that failure; see Speed item 2.

## Reliability Improvements

1. **Inventory-parity failure in CI.**
   - **Evidence:** run 43925460837, `tests-release-and-log-analysis / Inventory parity`, failed.
   - **Category:** docs drift (inference: a PR added a tracked surface under `scripts/`, `prompts/` or `.github/workflows/` without a `docs/INVENTORY.md` entry). The FAIL lines themselves were unavailable because the log directory was not mounted.
   - **Fix:** run the pre-push check described in Speed item 2.
   - **Impact:** removes 100% of this failure class (1 of the 20 CI runs in the window).
   - **Fail-open:** if the check is unavailable it is advisory only, and CI stays the authority.
2. **Superseded poller runs hide real cancellations.**
   - **Evidence:** 9 of 11 orchestrate_poll runs are `cancelled`, all at `poll / poll : cancelled_before_first_step`, including runs 43947406168, 43947626007, 43933013646, 43938565928, 43941174023, 43930702192 and 43936634417. Some waited up to 1243 s before being cancelled.
   - **Root cause:** expected concurrency-group pending replacement (`orchestrate_poll.yml:97-102`).
   - **Fix:** classify `cancelled_before_first_step` together with a concurrency group as a `superseded` conclusion in the collector, so real cancellations stand out.
   - **Impact:** the cancellation rate falls from 12 of 993 runs to the true rate.
3. **BREAK_GLASS and CONTEXT_BUDGET_WARN:** both 0 across all 115 runs with log telemetry. There is no sign of policy or prompt-size pressure, but coverage is limited (see the data gaps).
4. **Semble fallbacks: healthy.**
   - All 16 were contract-test lines (`semble_contract_test_fallbacks=16`, `semble_runtime_fallbacks=0`), 4 in each of CI runs 43925484798, 43925478355, 43925460837 and 43937230500. These are test fixtures matched by `_is_contract_test_semble_line` (`cost_audit.py:381-382`).
   - There were no runtime fallbacks and 0 failed bootstraps. No mitigation is needed.

## AI Memory Health

- No `AI_MEMORY_TELEMETRY:` lines could be inspected. The deep-dive folder (`/home/runner/work/_temp/workflow-log-output`) does not exist in this sandbox, and no row in the analysis context has a `log_summary`.
- The retrieve hit rate, `estimated_tokens` against budget, `keyword_method` mix, `fail_open` and `enabled:false` entries, and push retries are therefore all **unknown**.
- **Recommendation:**
  - Make sure the analysis job mounts the `workflow-log-output` artifact.
  - Add `AI_MEMORY_TELEMETRY` op counts (`retrieve` hits and misses, `fail_open`) to `cost_audit.parse_log` output, so they appear in `cost_telemetry` even when logs are not mounted.

## GH API Call Audit

- **Gap:** the context has no GH API call counts, hotspots or rate-limit events.
- **Observed waste in the collector:**
  - It downloaded log archives for 15 `skipped` runs (for example 43950040720, 43950040580, 43949583894 and 43949584595), and all came back `empty_archive`.
  - The 100-row `recent` export is entirely skipped or pre-start-cancelled runs.
  - **Change:** filter `conclusion in {"skipped"}`, and runs whose `failure_point.step_name == "cancelled_before_first_step"`, out of the `recent` category at `collect_workflow_logs.py:1469`. Filter `recent_runs` the same way at `analyze_workflow_logs.py:432`.
  - **Estimated reduction:** about 15 archive downloads per collection, with every deep-dive slot going to runs that have logs.
- **Event fan-out:** 11 events at 19:50:05–19:50:16 each created 4 runs (clarify, plan, implement, orchestrate_clarify_respond), all skipped at job-level `if:`. Inference: an orchestrator bulk label or comment action.
  - Moving the filter into `on:` types or path filters, where possible, would cut about 880 run objects per window. That shrinks the collector's paginated `runs` listing and the event noise.
  - Each workflow's trigger filters would need auditing first; risk is medium, because an over-tight filter would miss real dispatches.
- **Rule cross-check:** the poller's cycle caches (`_candidate_details_json`, `STALL_MANAGED_LINKED_PR_CACHE`) cannot be checked against these logs. Add `GH_API_CALLS: phase=… count=…` summary lines at the end of the poll run to make this auditable.

## Prompt Cache & Memory System

- `cache_hit_rate` is `null` for every family. `or_cache_read_tokens` and `or_cache_write_tokens` are 0, and the Claude cache fields are dropped by the collector (`cost_audit.py:609-613`). Prompt-cache behavior **cannot be evaluated** for this window.
- `OPENROUTER_PROMPT_CACHE_DISABLED` defaults to `'false'` (`orchestrate_poll.yml:80`). Inference: caching is meant to be on but is not observed.
- There were no CONTEXT_BUDGET_WARN lines, so there is no evidence of prompt growth eroding the cache.
- **Next steps:**
  - Land Cost item 1.
  - Then compute `claude_cache_read_tokens / (claude_input_tokens + claude_cache_read_tokens)` for each phase.
  - Flag any phase below 50% to investigate unstable prefixes, such as timestamps or run IDs placed early in the prompt.

## Orchestrator Health

- **Tick cadence:** a 15-minute cron against poll runs of 37–61 minutes means about 3 of every 4 ticks are superseded. Effective cadence is roughly 40–60 minutes, which slows how fast waves progress.
- **Downstream activity in the window:**
  - orchestrate: 1 success (run 43943103574, 2276 s)
  - validate: 2 successes (runs 43943042913 and 43936704617)
  - unblock_judge_dispatch: 2 successes (#9342 and #9238)
  - promote_candidate_follow: 2 successes
  - There were no failures and no retries.
- **Anomaly:** validate run 43943042913 is named `Internal: AI Validate [tracking:0]`. Tracking ID 0 suggests a missing or defaulted input; verify which caller dispatched it.
- **Indicators to track:**
  - superseded-poll ratio (currently 9/11)
  - poll p50/p95 duration (currently 2951 s p95)
  - queue wait per poll tick (after the diagnostic in Speed item 1)
  - clarify-respond runs that actually executed (0 of 209 this window)
- **Smallest mitigations:** add `timeout-minutes` to `poll`, and add per-phase timing lines in the poll script.

## Pipeline Flow Bottlenecks

1. **Queueing (dominant).** It affects every stage. clarify 43942270681 (2132 s), plan 43926034627 (2870 s) and implement 43934267383 (2239 s) are all in the 18:00–20:00 contention window. Compute cannot be separated from queue time until the diagnostic in Speed item 1 lands.
2. **Review/autofix compute plus queue.** p50 is 2412 s and p95 is 6230 s. Runs: 43926744749 (pr:9571, 6839 s), 43932494565 (pr:9124, 5487 s), 43932476878 (pr:9489, 5426 s).
3. **The CI gate.** p50 is 1989 s and p95 is 5826 s. This includes 15 CI quick runs plus the full CI runs started for the same window.
4. **Orchestrate tick latency.** About 40–60 minutes between effective ticks (see Orchestrator Health).
5. **Retry and merge-conflict overhead:** none observed (every run was attempt 1, and no conflict-heal runs appear).

Fix order by end-to-end impact: queue measurement and reduction → pre-push inventory parity → poll timeout and timing → collector filters.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:**
  - runner-queue saturation (5-minute jobs taking 27–37 minutes; CI at least ~100 minutes over its compute ceiling)
  - review/autofix p95 of 6230 s
  - poller ticks of 37–61 minutes
- **Top failure modes:**
  - one Inventory parity failure (CI 43925460837)
  - 9 superseded poller runs that are recorded as `cancelled`
- **Highest-cost drivers:** unknown; the Claude telemetry is dropped. By wall-clock, the drivers are review/autofix and full CI.
- **Top 3 actions:**
  1. Add the queue-wait diagnostic, then deduplicate CI quick and full CI for the same SHA.
  2. Add the Claude count keys to `RUN_COST_TELEMETRY_FIELDS` and `AGGREGATABLE_COST_FIELDS`.
  3. Exclude skipped and pre-start-cancelled runs from the collector's `recent` deep-dive selection.

## Metrics Appendix

Window: 2026-11-16, about 17:58–20:28 UTC. `success_sample_rate=0.07`; 5 sampled success runs. `insufficient_data=false`.

| Scope | Runs | Success | Failure | Cancelled | Other (skipped) | p50 s | p95 s |
|---|---|---|---|---|---|---|---|
| repository total | 993 | 97 | 1 (0.10%) | 12 | 883 | 1.0 | 1660.8 |
| ci | 20 | 19 | 1 (5%) | 0 | 0 | 1989.5 | 5825.7 |
| review_autofix | 10 | 8 | 0 | 0 | 2 | 2412.5 | 6230.6 |
| orchestrate_poll | 11 | 2 | 0 | 9 | 0 | 796 | 2951 |
| integration_pr_readiness | 15 | 15 | 0 | 0 | 0 | 877 | 2307.7 |
| lint_pr_body_auto_close | 15 | 15 | 0 | 0 | 0 | 950 | 2238.1 |
| cancel_on_pr_close | 6 | 6 | 0 | 0 | 0 | 1318 | 1865.25 |
| security_audit | 6 | 3 | 0 | 3 | 0 | 985 | 1704 |
| clarify / plan / implement | 212 / 208 / 209 | 2 / 1 / 2 | 0 | 0 | 210 / 207 / 207 | 1.0 | 10.0 |
| orchestrate_clarify_respond | 209 | 0 | 0 | 0 | 209 | 1.0 | 10.0 |
| workflow_failure_heal / heal_intake | 29 / 18 | 2 / 7 | 0 | 0 | 27 / 11 | 1.0 / 10.0 | 686.8 / 2380.45 |

Queue-wait lower bounds (run duration minus the maximum compute the timeouts allow):

| Run | Workflow | Timeout cap | Duration | Min. non-compute time |
|---|---|---|---|---|
| 43926281605 | integration-pr-readiness | 5 min | 2419 s | ≥2119 s |
| 43927314324 | lint-pr-body-auto-close | 5 min | 2250 s | ≥1950 s |
| 43942433833 | cancel_on_pr_close | 5 min | 1933 s | ≥1633 s (part may be wait on the per-PR concurrency group) |
| 43928112617 | promote-candidate-follow | 10 min | 2107 s | ≥1507 s (repo-wide concurrency group) |
| 43925484798 | CI [pr:9597] | ~30 min critical path | 7834 s | ≥6034 s |
| 43925460837 | CI (failed) | ~30 min critical path | 5589 s | ≥3789 s |

Telemetry totals (115 runs with log telemetry):

| Metric | Value |
|---|---|
| codex_tokens_used / codex_calls | 0 / 0 |
| or_total_tokens / or_calls | 0 / 0 |
| claude_* | not collected (gap) |
| cache_hit_rate | null |
| wall_clock_p50_ms / wall_clock_p99_ms (repository) | 2000 / 6,738,290 (n=110) |
| break_glass_count | 0 |
| context_budget_warn_count | 0 |
| GH API calls / rate-limit events | not present in context (gap) |

Semble (the only non-fallback activity is in orchestrate_poll run 43927831760):

| Target scope | query_calls | logged bytes | sources | static_dup_bytes | bootstraps (failed / unused) | bootstrap ms | fallbacks (contract-test / runtime) |
|---|---|---|---|---|---|---|---|
| orchestrate_poll | 2 | 11,529 | 5 | 0 | 1 (0 / 0) | 49,233 | 0 / 0 |
| ci (4 runs) | 0 | 0 | 0 | 0 | 0 | 0 | 16 / 0 |

Serena and MCP availability:

| Server | query_calls | response_bytes | tool_calls | fallbacks | probe_ok | probe_failed | probe_skipped |
|---|---|---|---|---|---|---|---|
| Serena | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

- **Other MCP servers observed:** none.
- **Material data gaps:**
  - the log directory was not mounted, so no step logs, `AI_MEMORY_TELEMETRY` or GH API counts could be read
  - Claude token fields are dropped by `cost_audit.py:611`
  - queue time is not separated from compute time
  - the success-run sample is 5 runs

Sources:
- [Actions limits - GitHub Docs](https://docs.github.com/en/actions/reference/limits)