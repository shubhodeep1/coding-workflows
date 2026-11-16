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
## Deep Audit — Workflows & Scripts (2026-11-16)

Scope: I read the files below in depth. Other files were checked with targeted searches only.
- Workflows: `orchestrate_poll.yml`, `cancel_on_pr_close.yml`, `lint-pr-body-auto-close.yml`, `integration-pr-readiness.yml`, `ci_pr_quick.yml`, `internal-orchestrate-poll.yml`, plus selected steps of `implement.yml`, `review_autofix.yml` and `validate.yml`.
- Scripts: `scripts/orchestrate_poll_process.sh` (actions-runs loader and its callers), `scripts/lint_pr_body_auto_close.py`, `scripts/check_integration_pr_readiness.py`, `scripts/review_merge_train.sh` (listing loop).
- Tests: `tests/test_workflow_file_size_limit.py`.

Findings already in this report's earlier sections (poller `timeout-minutes`, runner-queue saturation, collector skipped-run filtering, Claude telemetry fields) are not repeated; where they matter they are cross-referenced as "Executive Summary" or "Speed item N".

I checked these and they turned out not to be problems:
- `review_merge_train.sh:1229` lists the run statuses twice on purpose (documented at lines 1141 and 1277-1279).
- The `$(_load_actions_runs_cached)` callers do not re-fetch, because the cache is filled in the parent shell first (`orchestrate_poll_process.sh:28333`, `:28372`, `:34009`).
- Implement's repeated reads of the issue at `implement.yml:2329` and `:2378` are deliberate re-checks before the phase advances (comment at lines 2327-2328).
- The PAT that `git remote set-url` writes into `.git/config` (`implement.yml:1351`, `orchestrate_poll.yml:493`) never reaches the agent, because the isolated workspace drops `.git` (`scripts/codex_isolated_workspace.py:128`, `:1176`).
- Untrusted event fields (issue, PR and comment titles and bodies, head refs) reach shell only through `env:` in every workflow I searched.

### Section 1: Bug & Correctness Sweep

**BUG-001 — A 304 on the in-progress run listing reuses stale queued and completed runs**
- **File:** `scripts/orchestrate_poll_process.sh:18189-18241`, `:18243-18270`
- **Severity:** High | **Category:** `bug`
- **Description:**
  - The conditional request carries `If-None-Match` only on the `status=in_progress` listing (lines 18193-18196).
  - The blob that is cached and stored with that ETag is the *merged* in_progress + queued + completed list (lines 18254-18266).
  - On a 304 (lines 18218-18240), the code returns `cached_runs` as-is, including the old queued and completed lists. It then calls `actions-runs-cache put` again (lines 18225-18229), which renews the cache entry. Inference: this restarts the `ACTIONS_RUNS_CACHE_TTL_SECONDS` window (default 60, line 3004).
  - Result: as long as the in-progress set does not change (most commonly when it is empty), new queued runs are never seen. That is exactly the state this report's Executive Summary describes, with gate workflows queued for 27-37 minutes.
  - `build_active_issue_set` (lines 18736-18749) and the retrigger_review in-flight guards (lines 20839, 26508) then work from a stale queue. The comment at lines 34017-34022 already lists "304-reuse of empty cached_runs" as a known source of empty active sets.
  - Downstream duplicate dispatch or wrongful stall recovery is an inference [NEEDS VERIFICATION against tick logs].
- **Recommended fix:**
  - Keep the in-progress part separately in the cache payload (e.g. `.in_progress_runs` alongside `.runs`).
  - On a 304, reuse only that part and always re-read queued (line 18247) and completed (line 18252) before merging.
  - Call count: 3 on the 200 path (unchanged); 1 → 3 on the 304 path.

**BUG-002 — A failed queued or completed listing is cached as if the snapshot were complete**
- **File:** `scripts/orchestrate_poll_process.sh:18247-18253`, `:18259-18265`, `:18281-18296`
- **Severity:** Medium | **Category:** `bug`
- **Description:**
  - `queued_runs` and `completed_runs` fall back to `'[]'` on failure (`|| echo '[]'`).
  - The partial blob is then marked ready and written to the cross-run cache for the TTL.
  - If the in-progress fetch fails, the loader still returns 0 with an empty or stale blob (lines 18288-18296). The only trace is a `rate_limit_audit_fallback` warning on stderr.
  - Callers cannot tell "no runs in flight" from "the listing failed". This fails open on a safety guard that decides whether stall recovery may act.
- **Recommended fix:**
  - Add a global `_ACTIONS_RUNS_BLOB_COMPLETE` (check the name is unused) and set it to `false` on any of the three failures.
  - Skip the `actions-runs-cache put` call when the blob is partial.
  - Make the destructive stall actions (`close_and_reissue`, the empty-commit `retrigger_review` push) skip the tick when it is `false`.
  - Existing patterns to mirror: `MERGE_TRAIN_RUNS_LISTING outcome=incomplete` in `scripts/review_merge_train.sh:1153-1185`, and `AUTOFIX_SWEEP_SNAPSHOT_INCOMPLETE` in agents.md:5369.

**BUG-003 — One run can take support code from two different promote-candidate commits**
- **Files:**
  - `.github/workflows/orchestrate_poll.yml:147-188` and `:572-614`
  - `.github/workflows/implement.yml:789-794` and `:1368-1373`, plus the duplicate-PR-notice step (read at line 489)
  - `.github/workflows/orchestrate.yml:221` and `:395`
- **Severity:** Medium | **Category:** `bug`
- **Description:**
  - Every internal wrapper pins `@promote-candidate` (`internal-orchestrate-poll.yml:18`, `internal-implement.yml:47`, `internal-orchestrate.yml:37`), so the promote-candidate branch is read on every run in this repo.
  - In the poll job, two separate steps each read `branches/promote-candidate`:
    - The first sets `SCRIPT_REF` for the early checkout. From it, `gh_helpers.sh`, `review_merge_train.sh` and `label_helpers.sh` are staged (lines 237-266) and the merge train runs (lines 268-305).
    - The second sets `SCRIPT_REF` again for the main support checkout (lines 616-697).
  - If the branch moves between the two reads, or one read fails (a failure silently keeps `github.sha`, lines 155 and 581), one tick runs helpers from two different commits.
  - `validate.yml:225-238` already fixes exactly this: it reads once, exposes `promote_candidate_sha` and `promote_candidate_checked`, and reuses them at lines 469-470 and 497-501. Its comment reads "so both checkouts come from one candidate commit even if the branch moves meanwhile".
- **Recommended fix:**
  - Give the first resolve step an `id:`.
  - Emit `promote_candidate_sha` and `promote_candidate_checked` from it, as `validate.yml:228,238` does.
  - Have the later resolve step reuse those outputs instead of reading again.
  - Apply the same change to `implement.yml` and `orchestrate.yml`. API impact is under API-001.

**BUG-004 — The integration readiness status can be overwritten by an out-of-order run**
- **File:** `.github/workflows/integration-pr-readiness.yml:41-76`; `scripts/check_integration_pr_readiness.py:111-137`
- **Severity:** Medium | **Category:** `bug` (race)
- **Description:**
  - The workflow has no `concurrency:` group.
  - PR labels come from the event payload snapshot (`toJSON(github.event.pull_request.labels)`, line 69).
  - Every run POSTs the `orchestrator/integration-pr-not-ready` status to the same head SHA. The status created last wins.
  - Runs queue for up to 2419 s (Executive Summary), so they can finish out of order. A `labeled` run for `ai:override-incomplete-merge` that finishes after the later `unlabeled` run leaves the required status at `success` with the override removed.
- **Recommended fix:**
  - Add a job-level `concurrency: {group: integration-pr-readiness-${{ github.event.pull_request.number }}, cancel-in-progress: true}` on `readiness`.
  - Use job level so label events that the job's `if:` skips do not take the group [NEEDS VERIFICATION of GitHub's ordering of skipped jobs versus concurrency].
  - Also pass `--pr-number`, have the script re-read the live labels and `headRefOid` in one `gh pr view --json labels,headRefOid` call, and skip the POST when `headRefOid != --head-sha`. That is +1 call per run.

**BUG-005 — The auto-close lint has no per-PR concurrency, so a stale body check can finish last**
- **File:** `.github/workflows/lint-pr-body-auto-close.yml:27-47`, `:58-69`
- **Severity:** Medium | **Category:** `bug` (race)
- **Description:**
  - `edited`, `synchronize` and the other event types each start a run (line 34) that lints the body snapshot from its own payload (lines 60-61).
  - There is no concurrency group, and queueing reaches about 37 minutes (Executive Summary). A run for an older body can therefore finish after the run for the current body.
  - Whether branch protection honours the last-completed or the last-created check run with the same name is [NEEDS VERIFICATION].
- **Recommended fix:** add workflow-level `concurrency: {group: lint-pr-body-auto-close-${{ github.event.pull_request.number }}, cancel-in-progress: true}`. There is no job-level `if:` here, so workflow-level cancellation is safe. This also cuts the queued fan-out the Executive Summary describes.

**BUG-006 — In-flight guards in long poll ticks read a snapshot taken at tick start**
- **File:** `scripts/orchestrate_poll_process.sh:18120-18123`, `:20839`, `:26508`
- **Severity:** Medium | **Category:** `bug` [NEEDS VERIFICATION]
- **Description:**
  - `_ACTIONS_RUNS_BLOB_READY` caches the actions-runs blob for the whole process.
  - The retrigger_review guards (the comment at lines 26504-26507 says they exist to avoid "discard[ing] in-flight editor work") read this blob.
  - Poll ticks take 37-61 minutes (Executive Summary), so a review run dispatched or started after the cache was filled is invisible to the guard that decides an empty-commit push. This is an inference from the caching design.
- **Recommended fix:**
  - Record the fetch time in a new global next to the cached blob (e.g. `_ACTIONS_RUNS_BLOB_FETCHED_EPOCH`; check uniqueness).
  - In the two guard call sites only, clear `_ACTIONS_RUNS_BLOB_READY` and reload in the parent shell when the blob is older than a bounded age (e.g. 300 s).
  - That costs at most 3 calls per guarded push, and only when stale.

**SEC-001 — A step output is interpolated straight into shell (validated today, so defence in depth only)**
- **File:** `.github/workflows/implement.yml:2006-2019`
- **Severity:** Low | **Category:** `security`
- **Description:**
  - `${{ steps.baseline_refctx.outputs.branch }}` and `.status` are spliced into `echo` and assignment lines.
  - The branch comes from a trusted-author issue footer and must match `ai/reissue-baseline/pr-(\d+)-([0-9a-f]{12})-\d+-\d+` (line 1198) before `status=accepted`, so it is not exploitable now.
  - The same pattern recurs at `clarify.yml:2273`/`:2290` and `validate.yml:902-905`.
- **Recommended fix:** move these values into the step's `env:` (e.g. `BASELINE_BRANCH`, `BASELINE_STATUS`, `CHECKOUT_BASELINE_OUTCOME`) and reference them as shell variables. This also takes the body out of the expression-size count (Section 4).

**SHELL-001 — Multi-line `run:` bodies without `set -euo pipefail`**
- **Files:**
  - `.github/workflows/review_autofix.yml:5503-5685` ("Telegram review-blocked judge decision")
  - `.github/workflows/orchestrate_poll.yml:135-145`
  - `.github/workflows/orchestrate_poll.yml:1206-1228`
- **Severity:** Low | **Category:** `shellcheck`
- **Description:**
  - In the review_autofix step, a failed `source "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh"` (line 5504) goes unnoticed until `tg_send_tracked` fails with 127 at line 5685, with no useful diagnostic.
  - The two poll steps are fail-open by intent but do not say so.
- **Recommended fix:**
  - Add `set -uo pipefail` plus an explicit `|| { echo "::warning::tg_helpers unavailable"; exit 0; }` around the `source`, matching the fail-open style of `orchestrate_poll.yml:1266-1272`.
  - Add a one-line "intentionally fail-open" comment to the two poll steps.

### Section 2: GitHub API Call Redundancy Audit

**API-001 — The same `branches/promote-candidate` read is repeated within one job**
- **Files:** `orchestrate_poll.yml:166`, `:592`; `implement.yml:489`, `:794`, `:1373`; `orchestrate.yml:221`, `:395`
- **Severity:** Medium | **Category:** `api-redundancy`
- **Current → proposed calls:**
  - poll job: 2 → 1 per tick, i.e. 192 → 96 per day at `*/15`
  - implement job: up to 3 → 1 per run
  - orchestrate job: 2 → 1 per run
- **Pattern to extend:** `validate.yml:225-238` emits `promote_candidate_sha`/`promote_candidate_checked`, which `validate.yml:469-470,497-501` reuse.
- **Note:** every one of these reads goes through the no-retry shim (CONSIST-001), so cutting reads also cuts the chance of the split result described in BUG-003.

**API-002 — The auto-close lint looks up each referenced issue with its own call**
- **File:** `scripts/lint_pr_body_auto_close.py:143-178`, `:257-268`
- **Severity:** Low | **Category:** `api-batching`
- **Current → proposed calls:**
  - Now: one `gh issue view --json labels` per unique (repo, issue). With up to 250 commit messages linted (`lint-pr-body-auto-close.yml:85-98`), N can be dozens.
  - Proposed: one aliased GraphQL query per referenced repo per 50 issues (`i<N>: issue(number:N){labels(first:100){nodes{name}}}`), i.e. ⌈N/50⌉ per repo.
- **Pattern to extend:**
  - `_fetch_issue_labels_batch_graphql` (`scripts/orchestrate_poll_process.sh:4817`)
  - `_cpr_fetch_pr_state_batches` (`cancel_on_pr_close.yml:198-263`), which documents its batching contract
- **Constraints:**
  - Keep the per-issue `None` (lookup failed) semantics for issues that come back null or with errors, so the exit-2 path is preserved.
  - Keep the `label_lookup` test injection point.

**API-003 — The readiness check calls `gh` with no retry and can fetch the same issue twice**
- **File:** `scripts/check_integration_pr_readiness.py:73-88`, `:111-137`, `:200-223`
- **Severity:** Low | **Category:** `api-redundancy` (missing backoff)
- **Description:**
  - `_fetch_issue` and `_post_commit_status` call `gh` once with no retry.
  - A transient 5xx on the status POST leaves the required context missing until the next push (the script returns 1).
  - With a non-empty `--tracking-labels-file`, the issue can be fetched twice (lines 201 and 221).
- **Current → proposed calls:** 1-2 reads + 1 POST, unchanged, but with classified retries.
- **Pattern to extend:** `scripts/gh_api_retry.py`, the Python counterpart of `gh_api_retry` (agents.md:5369). Use `--idempotent` for the status POST, since statuses are append-only and the latest one wins.

**API-004 — An inline `gh_retry` in review_autofix ignores the shared retry contract**
- **File:** `.github/workflows/review_autofix.yml:1269-1291`, used at `:1295`, `:1442`, `:1479`, `:1513`, `:1534`, `:1553`
- **Severity:** Low | **Category:** `api-redundancy` (tight or unclassified retry)
- **Description:**
  - This copy is defined unconditionally in the deterministic-skip job.
  - It retries every failure 4 times with a fixed 2/4/8 s backoff, including permanent 4xx errors and primary or secondary rate limits.
  - It has no reset-aware wait and writes nothing to the breaker file.
  - It ignores `GH_RETRY_IDEMPOTENT=true` (lines 1513, 1553).
  - agents.md:5369 requires reset-aware waits and one attempt for non-idempotent creates.
- **Current → proposed calls:** up to 4× per failing call → classified (permanent 4xx: 1 attempt; rate limit: wait for the reset).
- **Pattern to extend:** add the existing "Bootstrap GitHub API retry helper" step (`review_autofix.yml:364-413`, which exports `GH_API_RETRY_HELPERS`) to this job, `source` it, and keep the inline copy only as the fallback, the way `review_autofix.yml:853-883` already does.

**Reviewed, not a finding:** the cancel loop at `cancel_on_pr_close.yml:371-387` makes one POST per run, but GitHub has no bulk-cancel endpoint, so N calls cannot be avoided. Its PR-state reads are already batched (lines 198-263).

### Section 3: Code Duplication & Modularization Opportunities

**DUP-001 — The "Resolve workflow support ref" block is copied 16 times across 9 workflows**
- **Files:** grep counts `promote_candidate_support_ref="${{ github.sha }}"` 16 times. Examples:
  - `orchestrate_poll.yml:147-188`, `:572-614`
  - `cancel_on_pr_close.yml:32-74`
  - `implement.yml:772-813`, `:1353-1396`, `:7291-7333`
  - `clarify.yml` (branch reads at :408, :2525)
  - `plan.yml` (:527, :3917)
  - `orchestrate.yml` (:221, :395)
  - plus `check_failure_triage.yml`, `workflow_failure_heal.yml` and `orchestrate_clarify_respond.yml`
- **Severity:** Medium | **Category:** `duplication`
- **Description:**
  - Each copy differs only in the workflow file name inside the two `workflow_ref` regexes.
  - BUG-003 and API-001 exist because only `validate.yml` received the read-once fix.
  - A `scripts/` helper cannot own this, because the step runs *before* support code is checked out.
- **Recommended fix:**
  - Create a composite action `.github/actions/resolve-support-ref/action.yml` with inputs `workflow_file`, `prior_promote_candidate_sha` (optional) and `gh_token`, and outputs `script_ref`, `promote_candidate_sha`, `promote_candidate_checked`.
  - It must still write `SCRIPT_REF` to `GITHUB_ENV`, so the existing steps keep working.
  - Reference it pinned by commit, the way `orchestrate_poll.yml:511` pins `install-codex@91b30f6…`.
  - Update `tests/test_workflow_support_ref_follows_pin.py` and `tests/test_workflow_gh_retry_fallback_contract.py`, which pin the current inline shape.

**DUP-002 — Two poll steps carry the same ~210-line `env:` block**
- **File:** `.github/workflows/orchestrate_poll.yml:1332-1549` ("Process each tracking issue") and `:1569-…` ("Run standalone stall recovery without active projects")
- **Severity:** Medium | **Category:** `duplication`
- **Description:**
  - The comment at lines 1558-1560 says `tests/test_orchestrate_poll_workflow_contract.py` keeps the two blocks equal, so every new knob must be added twice.
  - It also inflates the file (see EXPR-006).
- **Recommended fix:**
  - Move every entry that does not reference `steps.*` into the `poll` job's `env:` (lines 109-113).
  - Keep step-scoped entries where they are: `CLAUDE_POOL_REASON` (line 1346) and the standalone sweep flag.
  - Change the contract test to assert that both steps inherit from job env and carry only the allowed step-local deltas.

**DUP-003 — Inline retry wrapper and label catalog duplicated in review_autofix**
- **File:** `.github/workflows/review_autofix.yml:861-883`, `:1269-1291`, `:1293-1296`, `:1506-1512`
- **Severity:** Low | **Category:** `duplication`
- **Description:**
  - Two copies of the buffered-retry loop.
  - An inline `ensure_label_exists` with hard-coded color and description strings. The comment at lines 1506-1511 admits these "must stay in lockstep" with `scripts/label_helpers.sh`.
- **Recommended fix:**
  - The owner should be `scripts/gh_helpers.sh` (`gh_retry`, via `GH_API_RETRY_HELPERS`) and `scripts/label_helpers.sh` (`ensure_label_exists <label> <repo>`), both loaded from the bootstrap clone (API-004).
  - Callers to update: the deterministic-skip step (1241-1564) and the post-merge validate dispatch (849-1005).

**DUP-004 — The "Stage gh retry helper" loop is copied in two workflows**
- **File:** `.github/workflows/cancel_on_pr_close.yml:124-148`; `.github/workflows/orchestrate_poll.yml:237-266`
- **Severity:** Low | **Category:** `duplication`
- **Description:** the same "prefer `.codex-workflow-src`, fall back to `.codex-workflow-src-main`, `install -m 0755`" loop over `gh_helpers.sh review_merge_train.sh label_helpers.sh` appears in both, and `orchestrate_poll.yml:672-697` repeats it for 50+ files.
- **Recommended fix:**
  - Add `scripts/stage_support_files.sh --required <list> --optional <list> --dest scripts`, run from the verified `.codex-workflow-src` checkout.
  - Register it under `REQUIRED_BOOTSTRAP_SCRIPTS` the way `review_autofix_step_*.sh` scripts are (§24).
  - Callers: the two steps above plus `orchestrate_poll.yml` "Stage workflow support files".

### Section 5 is below; first the expression-size checks.

### Section 4: Expression Size Limit Risk Assessment

The repo already enforces this in CI. `tests/test_workflow_file_size_limit.py:31-32,66-98` fails any `jobs.*.steps[].run` that contains `${{` and is 18,000 characters or longer. Every block below is therefore under 18,000 if CI is green. I estimated sizes as line count × observed line widths, minus the 10-character YAML indent; I did not count characters exactly [NEEDS VERIFICATION with `len(run)` from the test's own loader].

In every case below, the only interpolations are `github.server_url`, `github.repository` and `github.run_id`. GitHub exposes these as `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY` and `GITHUB_RUN_ID` in every step, so swapping them takes the body out of the limit entirely.

**EXPR-001 — "Telegram review-blocked judge decision" (review_autofix)**
- **File:** `.github/workflows/review_autofix.yml:5503-5686`
- **Severity:** Medium | **Category:** `expression-limit`
- **Size:** about 183 lines, ≈14,000-16,500 characters; headroom to 21,000 ≈4,500-7,000.
- **Description:**
  - The only `${{ }}` in the body are at lines 5507, 5508 and 5512.
  - The body is a growing `case` over judge actions and skip reasons; each new reason adds 2-3 lines.
- **Recommended fix:** use `${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/pull/${PR_NUMBER}` and `${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}`. The body then has no expressions. If it later passes 20 KB, move it to `scripts/review_autofix_step_tg_rb_judge_decision.sh` per §24.

**EXPR-002 — "Handle no-op implementation" (implement)**
- **File:** `.github/workflows/implement.yml:5544-5775`
- **Severity:** Medium | **Category:** `expression-limit`
- **Size:** about 231 lines, ≈14,000-17,500 characters; headroom ≈3,500-7,000 [NEEDS VERIFICATION].
- **Description:** interpolations at lines 5549 and 5551, and at the comment and label calls at lines 5757-5773.
- **Recommended fix:** same swap (`RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"`, `REPO="${GITHUB_REPOSITORY}"`, `--repo "${GITHUB_REPOSITORY}"`).

**EXPR-003 — "Create Pull Request" (implement)**
- **File:** `.github/workflows/implement.yml:6213-6384`
- **Severity:** Low | **Category:** `expression-limit`
- **Size:** about 170 lines, ≈11,000-14,000 characters.
- **Description:** interpolations at lines 6241, 6267, 6303, 6351 and 6356 (all `github.repository`).
- **Recommended fix:** swap to `${GITHUB_REPOSITORY}`.

**EXPR-004 — "Find active tracking issues" (poll)**
- **File:** `.github/workflows/orchestrate_poll.yml:313-476`
- **Severity:** Low | **Category:** `expression-limit`
- **Size:** about 163 lines with several 200+ character jq lines, ≈12,000-14,500 characters.
- **Description:** the only interpolations are at lines 328 and 358 (`github.repository`). Lines 406-409 of the same step already use `${GITHUB_REPOSITORY}`.
- **Recommended fix:** swap the two interpolations to `${GITHUB_REPOSITORY}`.

**EXPR-005 — The CI guard misses composite actions and large `if:` expressions**
- **File:** `tests/test_workflow_file_size_limit.py:66-98`
- **Severity:** Low | **Category:** `expression-limit`
- **Description:**
  - The guard does not look at `.github/actions/*/action.yml` composite `runs.steps[].run` bodies (6 actions; `install-claude` and `claude-pool-token` run inside the poll job, `orchestrate_poll.yml:1233,1241`).
  - It does not look at step or job `if:` strings either. The largest seen is `review_autofix.yml:4855` at about 480 characters, which is low risk today.
  - It gives no early warning between 15,000 and 18,000 characters.
- **Recommended fix:**
  - Extend `_workflow_files()`-style discovery to `.github/actions/*/action.yml`.
  - Check `if:` strings against the same limit.
  - In `main()`, print a non-failing `RUN_EXPRESSION_WARN chars>=15000` line per block.
  - Optionally fail on new `${{ github.(repository|server_url|run_id|run_attempt|sha|actor) }}` inside `run:` bodies, since the equivalent env vars always exist.

**EXPR-006 — File-size headroom on the two largest workflows**
- **File:** `.github/workflows/implement.yml` (≥7,346 lines) and `.github/workflows/review_autofix.yml` (≥6,068 lines)
- **Severity:** Low | **Category:** `expression-limit` [NEEDS VERIFICATION]
- **Description:**
  - The binding limit here is 512,000 bytes, not 1 MB (§24; test lines 26-27, guard at 480,000). No workflow can exceed 800 KB without failing that guard first.
  - At the ~60-70 bytes per line seen in these files, both are inferred to sit at roughly 400-500 KB. I could not measure exact byte counts in this sandbox.
- **Recommended fix:** check `wc -c` against the guard. If either file is within 50,000 bytes of it, land DUP-001 and DUP-002, which remove the largest repeated blocks, before adding new inline steps.

### Section 5: Cross-Cutting Concerns

**DEAD-001 — An `else` branch the loader can never reach**
- **File:** `scripts/orchestrate_poll_process.sh:18399-18407`
- **Severity:** Low | **Category:** `dead-code`
- **Description:** `_load_actions_runs_cached` returns 0 on every path (lines 18122, 18130, 18165, 18186, 18239, 18270, 18296), so the `else` branch that sets `actions_runs_fetch_rc` and `actions_runs_fetch_error` never runs. Fetch failures are reported only on stderr.
- **Recommended fix:** once BUG-002 lands, return a distinct non-zero code (e.g. 75, matching `gh_api_retry`'s rate-limit code) for an unconfirmed fetch, so this branch becomes live. Otherwise delete it.

**DEAD-002 — A deprecated workflow input with no removal trigger**
- **File:** `.github/workflows/orchestrate_poll.yml:7-20`
- **Severity:** Low | **Category:** `dead-code`
- **Description:** the `caller_workflow` input is a documented no-op kept for compatibility (§10), with no removal trigger recorded.
- **Recommended fix:** no code change. Record a removal trigger (e.g. "no consumer wrapper in `.github/ai/consumer_repos.json` passes `caller_workflow` for 90 days") next to the input description.

**CONSIST-001 — The `GH_API_RETRY_HELPERS` shim never finds the helper in most workflows**
- **Files:**
  - `orchestrate_poll.yml:164-165`, `:590-591`
  - `cancel_on_pr_close.yml:50-51`
  - the same shim in clarify, plan, orchestrate, check_failure_triage, workflow_failure_heal and review_autofix_sweep
- **Severity:** Low | **Category:** `consistency`
- **Description:**
  - Only `validate.yml:188`, `review_autofix.yml:409`, `security-audit.yml:122`, `orchestrate_clarify_respond.yml:136` and `implement.yml:209` export the variable.
  - Everywhere else the shim always defines the plain `gh api` fallback, so the promote-candidate read has no retry.
  - A failed read is logged as `SUPPORT_REF source=promote_candidate_unprotected` (e.g. `orchestrate_poll.yml:172`), which looks the same as a genuinely unprotected branch.
- **Recommended fix:**
  - Log `SUPPORT_REF source=promote_candidate_read_failed` when the response is empty.
  - Either add the "Bootstrap GitHub API retry helper" step (`implement.yml:164-213`) to these jobs, or fold the read into the DUP-001 composite action.

**CONSIST-002 — Mixed `${{ github.* }}` and `${GITHUB_*}` in run bodies**
- **Files:** `implement.yml` (about 70 `${{ github.repository }}`-style interpolations, e.g. 232, 2101, 5757, 6241); `orchestrate_poll.yml:154-155,328,358,580-581`; `cancel_on_pr_close.yml:40-41`
- **Severity:** Low | **Category:** `consistency`
- **Description:** the same files also use `${GITHUB_REPOSITORY}` (e.g. `orchestrate_poll.yml:406-409,493`). The interpolated form is not injectable, but it puts the whole body under the 21,000-character expression limit (Section 4).
- **Recommended fix:** when a step is next edited, convert it to the env form. Enforce this with the lint proposed in EXPR-005.

**CONSIST-003 — Correction to this report's Speed item 1, step 2**
- **File:** `.github/workflows/ci_pr_quick.yml:22-25`; `.github/workflows/ci.yml:82`
- **Severity:** Low | **Category:** `consistency` (documentation accuracy)
- **Description:**
  - The report says CI quick "never deduplicates" because of `ci-quick-{run_id}`.
  - The caller `ci_pr_quick.yml` already sets `group: ci-quick-pr-${{ github.event.pull_request.number }}` with `cancel-in-progress: true`, so CI quick runs on one PR do cancel each other.
  - `ci.yml:82`'s group is unique per run on purpose, so it never collides with the caller's group.
  - The real overlap is CI quick against the full `ci.yml` dispatch on the same head (`ci_pr_quick.yml:4-9`), not quick against quick.
- **Recommended fix:** re-scope Speed item 1, step 2 to "full CI vs CI quick for the same head SHA". No workflow change is needed for quick-vs-quick deduplication.

**DEBT-001 — Label catalog values hand-copied into a workflow step**
- **File:** `.github/workflows/review_autofix.yml:1506-1512`
- **Severity:** Low | **Category:** `tech-debt`
- **Description:** the label color and description for `ai:review-skipped` are hard-coded to match `scripts/label_helpers.sh`, and nothing tests that they still match.
- **Recommended fix:** either source the catalog (DUP-003), or add a test that parses both and asserts equality, like the existing workflow contract tests under `tests/`.

**TODO/FIXME/HACK:** none found in `scripts/*.sh`, `scripts/*.py` or `.github/workflows/*.yml`.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 10 | BUG-002, BUG-003, BUG-004, BUG-005, BUG-006, API-001, DUP-001, DUP-002, EXPR-001, EXPR-002 |
| Low | 18 | SEC-001, SHELL-001, API-002, API-003, API-004, DUP-003, DUP-004, EXPR-003, EXPR-004, EXPR-005, EXPR-006, DEAD-001, DEAD-002, CONSIST-001, CONSIST-002, CONSIST-003, DEBT-001, (none further) |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---|---|
| Critical/High bug fixes | `scripts/orchestrate_poll_process.sh` (actions-runs loader), `scripts/ai_memory.py` (cache payload field), loader unit tests under `tests/` | Medium |
| API call optimization | `.github/workflows/orchestrate_poll.yml`, `.github/workflows/implement.yml`, `.github/workflows/orchestrate.yml`, `.github/workflows/review_autofix.yml`, `scripts/lint_pr_body_auto_close.py`, `scripts/check_integration_pr_readiness.py` | Medium |
| Code modularization | new `.github/actions/resolve-support-ref/action.yml`, 9 workflows using the support-ref block, `.github/workflows/orchestrate_poll.yml` (job env), new `scripts/stage_support_files.sh`, `tests/test_workflow_support_ref_follows_pin.py`, `tests/test_workflow_gh_retry_fallback_contract.py`, `tests/test_orchestrate_poll_workflow_contract.py` | Large |
| Expression size reduction | `.github/workflows/review_autofix.yml`, `.github/workflows/implement.yml`, `.github/workflows/orchestrate_poll.yml`, `tests/test_workflow_file_size_limit.py` | Small |
| Medium/Low fixes | `.github/workflows/integration-pr-readiness.yml`, `.github/workflows/lint-pr-body-auto-close.yml`, `.github/workflows/cancel_on_pr_close.yml`, `scripts/orchestrate_poll_process.sh` (tick staleness, dead branch), `scripts/check_integration_pr_readiness.py` | Medium |
## API Call Consolidation & Dead-Call Analysis (2026-11-16)

### Safety Tag Legend
- **`SAFE_TO_MERGE`**: every precondition was proven by reading the code. That means the same endpoint (or a strict field subset), the same filters, pagination and token, the same function or step with no write in between, the same error and retry behaviour, no job or concurrency boundary crossed, and any cache contract kept. The implement stage may act on these without further review.
- **`NEEDS_VERIFICATION`**: the change looks correct, but at least one precondition can't be proven from the code alone. A person or a later analysis must complete the listed checks first.
- **`RISKY_SKIP`**: at least one hard trigger applies: a retry or backoff loop, an auth or identity flow, pagination, a rate-limit-sensitive path, `orchestrate_poll_process.sh`, stall-recovery or review-blocked judges, a documented race defence, or log keys that other code greps for. These findings are listed for visibility only and must not be implemented automatically.

### Consolidation Candidates (MERGE-###)

**MERGE-001 — The auto-merge guard reads the PR up to twice, through two endpoints**
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - `scripts/review_enable_auto_merge.sh:117`: head-only read inside `reviewed_head_is_current_for_labels`
  - `scripts/review_enable_auto_merge.sh:173`: paginated label read
  - `scripts/review_enable_auto_merge.sh:240`: full PR read
- **Call count, current:** auto-merge disabled path: 1 (`:117`). e2e path: 2 (`:173` + `:117`). Normal path: 2 (`:173` + `:240`).
- **Call count, proposed:** 1 on every path. One `GET pulls/{n}` would be hoisted above line 128. Its `.labels[].name`, `.head.sha`, `.head.ref` and `.base.ref` would serve all three uses.
- **Endpoints:** `GET /repos/{o}/{r}/pulls/{n}` (twice, with different `--jq`) and `GET /repos/{o}/{r}/issues/{n}/labels?per_page=100` (`--paginate`).
- **Evidence:**
  ```bash
  if ! ready_label_current_head_sha="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null)"; then
  if PR_LABELS_RAW="$(gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels?per_page=100" --jq '.[].name' 2>"${_label_err_file}")"; then
  if ! _ORCH_PR_META_JSON="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" 2>"${_orch_pr_meta_err_file}")"; then
  ```
  - The comment at `:115-116` already explains the separate head read: "The full PR-metadata call below is unreachable on these intentional early-exit paths".
  - The same label is already detected from the PR payload snapshot in `scripts/review_autofix_step_detect_smoke_test.sh:50` (`[.labels[]?.name] | index("e2e-smoke-test")` on `PR_PAYLOAD_FILE`).
- **Proposed fix:** fetch `_ORCH_PR_META_JSON` once before the `ENABLE_AUTO_MERGE` branch. Derive `PR_LABELS_RAW` from `.labels[].name`, and have `reviewed_head_is_current_for_labels` compare against the cached `.head.sha`.
- **Safety rationale:** the label read uses `--paginate` (pagination trigger). The guard was made paginated and fail-closed on purpose (comment `:147-171`, Copilot review #3 on PR #1823) to stop an e2e PR racing the e2e gate. Merging would change both page-boundary behaviour and the order of the reads.
- **Downstream signal:** do not auto-implement. Manual review must first confirm against GitHub's REST documentation that `pulls/{n}.labels` is never truncated for PRs with more than 30 labels. It must then confirm that hoisting the full read above line 128 keeps the fail-closed exits at `:184-189` and `:240-244` identical.

### Redundant Re-Fetch (REUSE-###)

**REUSE-001 — The readiness check can fetch the tracking issue twice in one `main()` (extends API-003)**
- **Safety tag:** `NEEDS_VERIFICATION`
- **Files / lines:**
  - `scripts/check_integration_pr_readiness.py:200-214`: first `_fetch_issue`
  - `scripts/check_integration_pr_readiness.py:216-223`: second `_fetch_issue`
  - `scripts/check_integration_pr_readiness.py:73-88`: `_fetch_issue` itself
- **Call count:** current 2, proposed 1. This only happens when `--tracking-labels-file` is non-empty, the body file is missing or empty, and the file labels lack `ai:orchestrator-tracking`.
- **Endpoint:** `gh issue view N --json labels,body`, which is the GraphQL `repository.issue` query.
- **Evidence:**
  - When `labels_from_file` is true and the body is empty, line 201 fetches the issue. Only `fetched_issue["body"]` is merged in (`:213-214`); `fetched_issue["labels"]` is thrown away because `issue["labels"]` is already non-empty (`:211-212`).
  - Line 221 then calls `_fetch_issue` again for the same issue and fields:
  ```python
  fresh_issue = _fetch_issue(args.repo, tracking_num)
  if fresh_issue is not None and fresh_issue["labels"]:
  	issue["labels"] = fresh_issue["labels"]
  ```
  - Nothing is written between `:201` and `:221`, and both calls are in the same function.
- **Proposed fix:** keep `fetched_issue` in scope. At `:216`, use it as `fresh_issue` when it was fetched in this run, and call `_fetch_issue` only when it wasn't. Keep the `fresh_issue["labels"]` truthiness check unchanged.
- **Safety rationale:**
  - Same endpoint and fields, same function, no write in between, no retry loop or pagination.
  - The only caller that passes `--tracking-labels-file` is the poller (`scripts/orchestrate_poll_process.sh:4774-4780`), and the `:217-220` comment frames the re-read as a defence against a stale poller snapshot.
  - The reused data is itself a live read from the same process, so the defence stays intact. Because of that race framing and the poller caller, this can't be `SAFE_TO_MERGE`.
- **Downstream signal (verification steps):**
  1. Confirm the poller can produce an empty `desired_body_file` at `orchestrate_poll_process.sh:4779`; if it cannot, the saving is zero and the change should be dropped.
  2. Add a test in which the body file is empty and the labels file lacks the tracking label, asserting exactly one `_fetch_issue` call. The existing tests at `tests/test_check_integration_pr_readiness.py:361-434` both pass a non-empty body file, so neither covers this path.

**REUSE-002 — `GET /user` is called twice in the implement job**
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - `.github/workflows/implement.yml:2134`: "Fetch issue metadata" step, which exports `IMPLEMENT_SCOPE_PIPELINE_LOGIN` at `:2139-2141`
  - `.github/workflows/implement.yml:2480`: "Unblock judge bulk-delete override" step
- **Call count:** current 2 per run when an `override=bulk_delete` marker is present, otherwise 1. Proposed: 1.
- **Endpoint:** `GET /user`. Both steps use `GH_TOKEN: ${{ secrets.GH_PAT }}` (`:2082`, `:2474`) and the same `if: env.SKIP_IMPLEMENT != 'true'`.
- **Evidence:**
  ```bash
  pipeline_login="$(gh api user --jq .login 2>/dev/null || true)"     # :2134
  pipeline_login="$(gh api user --jq '.login' 2>/dev/null || true)"   # :2480
  ```
  - The second value decides which comments count as authentic pipeline markers (`:2485-2489`), and that lifts the bulk-delete threshold.
- **Proposed fix:** at `:2480`, use `${IMPLEMENT_SCOPE_PIPELINE_LOGIN:-}` when it is non-empty and fall back to the live call otherwise. The export is shape-validated at `:2139`.
- **Safety rationale:** this is an identity lookup that authenticates trust markers (auth-flow trigger), and the decision it gates loosens a destructive-change guard.
- **Downstream signal:** do not auto-implement. A security reviewer must confirm that no step between `:2079` and `:2463` changes `GH_TOKEN` or the identity. They must also confirm that the shape filter at `:2139` cannot turn a valid login into an empty value, which would change the `login_unavailable` skip at `:2481-2483`.

**REUSE-003 — The review-blocked judge re-reads the PR state after its race guard has just read it**
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - `scripts/review_rb_judge.sh:2082-2095`: guard read, then `unset _guard_pr_meta`
  - `scripts/review_rb_judge.sh:2666`: `fix` with `IS_FINAL=true`
- **Call count:** current 2 on the final-`fix` path, proposed 1 (keep `.state` from the guard response).
- **Endpoint:** `GET /repos/{o}/{r}/pulls/{n}`.
- **Evidence:**
  ```bash
  _guard_pr_meta="$(gh_retry _safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}" 2>/dev/null || echo '{}')"
  PR_STATE="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '.state' 2>/dev/null | grep -xE 'open|closed' || echo "")"
  ```
- **Proposed fix:** keep a new `_guard_pr_state` variable (check the name is unused) next to `RB_LIVE_HEAD_SHA` at `:2091`, and read it at `:2666`.
- **Safety rationale:**
  - The guard's comment (`:2072-2078`) says it exists to defend against an asynchronous merge race (race-defence trigger).
  - About 570 lines of action handling run between the two reads.
  - The script is a review-blocked recovery judge, and `review_autofix.yml:4612-4617` re-runs it once on exit 2 (retry trigger).
- **Downstream signal:** do not auto-implement. Manual review must list every write between `:2095` and `:2666` (label swaps, pushes, closes) and decide whether a pre-merge `.state` up to about 570 lines old is acceptable next to `--match-head-commit`.

**REUSE-004 — The judge re-queries `closingIssuesReferences` that metadata collection already fetched in the same job**
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - `scripts/review_rb_judge.sh:1192-1205`
  - `scripts/review_collect_pr_metadata.sh:273-278` (fetch), `:345-357` (persisted to `LINKED_ISSUES_RAW_FILE` and its `.status` file)
  - Both run in the `codex-agent` job (`review_autofix.yml:2909`, `:4612`).
- **Call count:** current 2 GraphQL calls per run that reaches the judge, proposed 1.
- **Endpoint:** GraphQL `repository.pullRequest.closingIssuesReferences(first:50)`.
- **Evidence:** the two queries are not field-identical:
  - The judge asks for `baseRefName` and `labels(first: 100) { nodes { name } pageInfo { hasNextPage } }`.
  - Metadata collection asks for `title` and `labels(first:50){nodes{name}}`, with no `pageInfo`.
  - The persisted file can also hold body-text fallback issues rather than closing references (`:314-340`, `_linked_context_raw`).
- **Proposed fix:** extend the `:277` query with `baseRefName`, `labels(first:100)` and `pageInfo{hasNextPage}`, and persist the raw nodes separately from the fallback. Then have the judge reuse them when `${LINKED_ISSUES_RAW_FILE}.status == ok`.
- **Safety rationale:**
  - The fields differ, so this is not a strict subset.
  - Reviewers, the editor and the conflict resolver all run in between, and `scripts/review_conflict_resolve.sh:3251-3254` rewrites the PR body, which can change the closing references.
  - The judge is a review-blocked recovery path that is re-run on exit 2.
- **Downstream signal:** do not auto-implement. Manual review must confirm that no step between metadata collection and the judge can change the PR body or base, and must define a freshness check (for example, compare the live `head.sha`) before reuse.

**REUSE-005 — The judge re-downloads the PR diff that metadata collection already wrote**
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - `scripts/review_rb_judge.sh:1332-1336`
  - `scripts/review_collect_pr_metadata.sh:514-527`: `gh pr diff` into `PR_DIFF_FILE`, which also exports `PR_DIFF_SOURCE=gh_pr_diff`
- **Call count:** current 2 diff downloads per judged run, proposed 1 when the head did not move.
- **Endpoint:** `GET /repos/{o}/{r}/pulls/{n}` with `Accept: application/vnd.github.diff` (also what `gh pr diff` calls).
- **Evidence:**
  ```bash
  if ! gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" \
    -H 'Accept: application/vnd.github.diff' > "${RB_JUDGE_PR_DIFF_FILE}" 2>/dev/null; then
  ```
  - The judge already reads `PR_PAYLOAD_FILE` from the same runtime directory (`review_rb_judge.sh:1536`, `:1558`).
- **Proposed fix:** copy `ORIGINAL_PR_DIFF_FILE` into `RB_JUDGE_PR_DIFF_FILE` only when all three hold:
  - `PR_DIFF_SOURCE == gh_pr_diff`
  - `steps.commit_changes.outputs.did_commit != 'true'`
  - the live `.head.sha` from `_pr_meta` (`:1149`, kept before the `unset` at `:1225`) equals `jq .head.sha "${PR_PAYLOAD_FILE}"`

  In every other case, keep the live download.
- **Safety rationale:** the judge must judge the head it evaluates (BUG-001 note at `:2087-2090`). The editor may have pushed, and the script runs inside the exit-2 retry wrapper of a recovery judge.
- **Downstream signal:** do not auto-implement. Manual review must confirm that `scripts/review_autofix_step_generate_diff_context.sh:61-89` never leaves a git-fallback or placeholder diff in `ORIGINAL_PR_DIFF_FILE` while `PR_DIFF_SOURCE` still says `gh_pr_diff`.

### Dead Calls (DEAD-API-###)

**DEAD-API-001 — The collector's jobs listing always makes a trailing empty-page request**
- **Safety tag:** `RISKY_SKIP`
- **Files / lines:**
  - `scripts/collect_workflow_logs.py:734-752` (`list_jobs_for_run`)
  - Called at `scripts/collect_workflow_logs.py:2215-2223` for every `failure`/`cancelled` run
- **Call count:**
  - Current: k+1 per run, where k is the number of non-empty pages. Typically 2, since `--per-page` defaults to 100 (`:429-431`) and most runs have far fewer jobs.
  - Proposed: k, typically 1.
  - The 2026-11-16 window had 1 failed and 12 cancelled runs, so about 13 calls are saved per collection.
- **Endpoint:** `GET /repos/{o}/{r}/actions/runs/{id}/jobs?filter=all&per_page=100&page=N`.
- **Evidence:**
  ```python
  page_jobs = payload.get("jobs") or []
  if not page_jobs:
      break
  jobs.extend(page_jobs)
  ```
  - The last response is used only to stop the loop; its data is never used.
  - If that final call raises, the jobs already collected are thrown away (`jobs_fetch_status = "failure"` at `:2224-2226`), so the dead call also adds a failure mode.
- **Proposed fix:** also `break` when `len(page_jobs) < per_page`, or when `len(jobs) >= payload.get("total_count", ...)`. The jobs API returns `total_count`.
- **Safety rationale:** the loop is manual `per_page`/`page` pagination, and each call goes through the `gh_api_json` retry loop (`:498-538`). Both are hard triggers, even though the change looks low-risk.
- **Downstream signal:** do not auto-implement. Manual review must confirm that `filter=all` pages are always full except the last, including runs with re-run attempts. It must also check that the collector's coverage gates in `ci.yml` (Inventory-parity job) and the `analyze_workflow_logs.py` tests don't assert a call count.

### Cross-References to Deep Audit Section
- API-001: NEEDS_VERIFICATION — I agree; the per-job counts check out (`orchestrate_poll.yml:166`/`:592`, `implement.yml:489`/`:794`/`:1373`, `orchestrate.yml:221`/`:395`). New detail:
  - There are two more reads in separate `heal-report` jobs: `implement.yml:7310` (job starts at `:7262`) and `validate.yml:1947` (job starts at `:1884`). Reusing those would need `needs.<job>.outputs` across a job boundary, so they must not be folded into the same-job change.
  - The read feeds the support-ref trust decision, so the fallback must stay a live read whenever the earlier step's output is empty.
- API-002: NEEDS_VERIFICATION — I agree with batching. New detail: `lint_pr_body_auto_close.py:266-268` caches only successful lookups. A repeated reference to an inaccessible issue therefore costs one call per occurrence, which is an implicit retry. The batched query must still emit one `lookup_errors` line per occurrence, and must decide explicitly whether a null alias is cached.
- API-003: NEEDS_VERIFICATION — the double-fetch half is made concrete as REUSE-001. The retry half adds attempts and is outside this removal-only pass.
- API-004: RISKY_SKIP — I agree on substance. The change replaces a retry wrapper, so every call site sits inside the retry loop. It also has to keep the `GH_RETRY_IDEMPOTENT` and fallback contract pinned by `tests/test_workflow_gh_retry_fallback_contract.py`.

### Summary Counts
| Tag | Count | IDs |
|---|---|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 1 | REUSE-001 |
| RISKY_SKIP | 6 | MERGE-001, REUSE-002, REUSE-003, REUSE-004, REUSE-005, DEAD-API-001 |

Counts cover only the new IDs from this pass. The Deep Audit cross-reference tags are listed in the section above.

### Implement-Stage Handoff
No SAFE_TO_MERGE findings in this pass.