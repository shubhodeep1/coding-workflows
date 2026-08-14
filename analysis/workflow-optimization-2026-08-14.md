## Executive Summary

Primary evidence came from the local workflow-log folder at `/home/runner/work/_temp/workflow-log-output`; prompt-supplied `analysis_context` was useful secondary context, but its telemetry coverage conflicts with the folder summary.

- **Operationally green, observationally weak.** All 105 runs in the local window succeeded, but local `summary.json` shows only **29/105** runs with parsed telemetry and reports **0 tokens, 0 cache activity, 0 Semble/Serena activity, `cache_hit_rate=null`**. The prompt `analysis_context` claims **105** telemetry-bearing runs with very different wall-clock percentiles, so current cost/cache metrics are not trustworthy. **Estimated impact:** high, because prompt-size/cost regressions can ship unnoticed. **Confidence:** high.
- **Idle `orchestrate_poll` is the largest systemic time sink.** `orchestrate_poll` accounted for **50/105 runs** at **124.9s average**. Sample run **31761907002** took **125s**; `Find active tracking issues` found **0 active tracking issues**, and the system log showed runner wait/startup before a graceful exit. **Estimated impact:** high; halving confirmed-idle poll frequency would save about **52 runner-minutes** per similar window. **Confidence:** high for the pattern, medium for the savings estimate.
- **`validation_refresh` hides real downstream failures under a green top-level run.** Run **31663114780** succeeded after **1671s** while logging `processed=12`, `green=8`, `red=2`, `skipped=2`, plus `discovery_budget_exhausted`. The red repos were `shubhodeep1/digital_pa` (container never became healthy within 120s) and `shubhodeep1/binance-blessings` (self-test failure). **Estimated impact:** high on false-green risk and operator response time. **Confidence:** high.
- **`review_autofix` is mostly healthy-idle but under-instrumented.** The family ran **43 times** at **8.2s average**; sampled sweeps **31763338965**, **31760737324**, and **31758456385** all logged `candidates=0` and `dispatched=0`. The risk is not failure; it is inability to distinguish true no-work from empty-snapshot/API-selection bugs. **Estimated impact:** medium. **Confidence:** high.
- **No hard GitHub Actions reliability crisis is visible.** No failed workflows, no sampled 429s, and no retry storms were evident in the inspected logs. The safest next step is **diagnostic logging**—`WORKFLOW_TIMING`, `GH_API_SUMMARY`, `MODEL_USAGE_SUMMARY`, `VALIDATION_REPO_RESULT`, `MCP_STATUS`—before changing behavior. **Estimated impact:** medium. **Confidence:** high.

## Speed Optimizations

Most speed gains here are about **reducing idle control-plane work**, not optimizing model execution.

1. **[Critical-path] Instrument and then reduce confirmed-idle `orchestrate_poll` cadence.**
   - **Evidence:** `orchestrate_poll` ran **50 times**; average **124.94s**. Run **31761907002**: `Find active tracking issues` found **0**, then exited gracefully.
   - **Root cause:** a fixed **5-minute cron** keeps allocating runners even when there is no active orchestrator work.
   - **Exact change:** first emit `WORKFLOW_TIMING workflow=orchestrate_poll queue_ms=... setup_ms=... work_ms=... cleanup_ms=...` and `NOOP_EXIT workflow=orchestrate_poll reason=no_tracking_issues active_issues=0`. If a week of data shows the idle ratio stays high, slow the idle poll cadence from **5m to 10m**.
   - **Estimated savings:** about **25 fewer poll runs** and **~52 runner-minutes** per comparable 105-run window.
   - **Risk:** medium; slower pickup of newly opened tracking issues.

2. **[Critical-path] Add per-repo timing and earlier budget stops to `validation_refresh`.**
   - **Evidence:** run **31663114780** took **1671s** and logged `discovery_budget_exhausted` with remaining budgets of **757s**, **621s**, and **484s** while still producing **2 red repos**.
   - **Root cause:** long-tail per-repo discovery/self-test work is visible only after the run is mostly over.
   - **Exact change:** emit `VALIDATION_REPO_RESULT repo=... stage=discovery|self_test ms=... outcome=... reason=...` and log `budget_before_ms`/`budget_after_ms` before each repo starts. Then enforce the existing budget gate before launching the next expensive repo.
   - **Estimated savings:** **several minutes per long refresh run** once repeated slow red repos are obvious enough to skip or pre-fix. This is an inference from one 27.9-minute run.
   - **Risk:** low; mostly logging and earlier stop conditions.

3. **[Micro] Defer checkout/support staging on no-op control-plane workflows.**
   - **Evidence:** `cancel_on_pr_close` run **31762683643** was a **13s** no-op; `issue_pr_status` run **31762683675** was a **12s** no-op with no linked issues.
   - **Root cause:** helper checkout/staging happens before the workflow knows whether there is any business work to do.
   - **Exact change:** do the GH API/GraphQL no-op detection first; only checkout helper scripts if cancellation/lineage-finalization is actually needed.
   - **Estimated savings:** about **3–5s/run** on these no-op workflows (inference from total durations and trivial business work).
   - **Risk:** low.

4. **[Micro] Back off `review_autofix` sweeps after repeated zero-candidate windows.**
   - **Evidence:** family total **43 runs**, average **8.16s**; sampled sweeps repeatedly logged `candidates=0`.
   - **Root cause:** recurring no-op scans still consume runner startup and PR enumeration work.
   - **Exact change:** emit `SWEEP_SCAN_SUMMARY open_pr_pages=... active_run_pages=... candidates=... oldest_candidate_age_s=...` and, if consecutive zero-candidate sweeps dominate, reduce idle sweep frequency.
   - **Estimated savings:** roughly **~3 runner-minutes** per comparable window if half of zero-candidate sweeps are removed.
   - **Risk:** medium; lower sweep responsiveness.

## Cost Optimizations

Actual token/dollar tuning is blocked by telemetry gaps. The highest-value cost work is to make existing collector fields usable.

1. **Restore collector-grade model/cache telemetry before changing models.**
   - **Evidence:** local window shows **`codex_tokens_used=0`, `or_total_tokens=0`, `or_cache_write_tokens=0`, `or_cache_read_tokens=0`, `cache_hit_rate=null`**. Historical analysis in `analysis/workflow-optimization-2026-07-12.md` already showed **`or_calls=175`** with **`or_total_tokens=0`**, so this blind spot is persistent.
   - **Root cause:** emitters and parser are misaligned; this looks like observability failure, not true zero usage.
   - **Exact change:** emit one canonical `MODEL_USAGE_SUMMARY phase=... model=... calls=... prompt_tokens=... completion_tokens=... total_tokens=... cache_write_tokens=... cache_read_tokens=...` line wherever model work occurs, using the exact fields already aggregated by `scripts/cost_audit.py`.
   - **Estimated savings:** high diagnostic leverage; direct token savings are **unquantified** until telemetry is real.
   - **Quality risk:** none; logging only.

2. **Short-circuit docs-only review paths before high-effort reasoning.**
   - **Evidence:** prompt-supplied summary for run **31762652316** / PR **#3739** reported `skip=true reason=docs_only` and also `EDITOR_REASONING_EFFORT: xhigh`. The local deep-dive logs did not expose token usage, so treat this as **secondary evidence**.
   - **Root cause:** expensive reasoning appears reachable even after a deterministic skip decision.
   - **Exact change:** when the gate decides `skip=true`, bypass editor/reviewer reasoning entirely or force `reasoning_effort=low`.
   - **Estimated savings:** one high-effort model call per docs-only PR.
   - **Quality risk:** low if the skip gate remains deterministic.

3. **Measure prompt growth and cache fragmentation directly.**
   - **Evidence:** `cache_hit_rate=null` everywhere; prompt-supplied summary for **31762652316** reported `CONTEXT_BUDGET_WARN_RATIO: 0.7`, but the local collector saw **0** parsable `CONTEXT_BUDGET_WARN:` lines.
   - **Root cause:** context pressure may exist, but there is no stable cache/payload telemetry.
   - **Exact change:** emit `PROMPT_CACHE_SUMMARY phase=... prefix_hash=... cache_read_tokens=... cache_write_tokens=... hit=...` and `CONTEXT_PAYLOAD_SUMMARY phase=... static_bytes=... dynamic_bytes=... files=... mcp_bytes=...`.
   - **Estimated savings:** medium, once the team can remove unstable prompt prefixes and oversized dynamic context.
   - **Quality risk:** low.

4. **Do not tune Semble/Serena spend yet; first distinguish “unused” from “unobserved.”**
   - **Evidence:** no sampled logs contained actual `SEMBLE_QUERY`, `SEMBLE_FALLBACK`, `SERENA_QUERY`, `SERENA_FALLBACK`, or `SERENA_PROBE` lines. Run **31762683746** showed `SEMBLE_ENABLED=true` and `SERENA_ENABLED=false` in env, but still no MCP telemetry.
   - **Root cause:** either the MCP paths are not exercised in this window, or telemetry is missing.
   - **Exact change:** emit `MCP_STATUS server=semble|serena enabled=... target=... query_calls=... request_bytes=... response_bytes=... fallbacks=... probe=ok|failed|skipped`.
   - **Estimated savings:** unquantified; required before deciding whether MCPs are reducing prompt expansion or adding noisy bytes.
   - **Quality risk:** none.

Avoidable reruns were **not** a material cost driver in this window: the local summary showed **0 failures** and sampled runs showed **0 retries**.

## Reliability Improvements

Raw GitHub success rate was already **100%** in this window, so the reliability problem is mostly **false-green masking** and **missing failure signals**.

1. **Promote `validation_refresh` red repos into first-class structured failure events.**
   - **Failure evidence:** run **31663114780** succeeded while logging `red=2`; red repos were `shubhodeep1/digital_pa` and `shubhodeep1/binance-blessings`.
   - **Root cause category:** masked downstream failures under a green aggregate workflow.
   - **Exact fix:** emit `VALIDATION_REPO_RESULT` per repo and a final `VALIDATION_SUMMARY processed=... green=... red=... skipped=... error=... budget_exhausted=...` line in collector-parsable format.
   - **Expected reliability impact:** high reduction in **false-green** outcomes and faster triage; limited effect on GitHub’s raw success percentage.
   - **Rollback / fail-open:** safe; logging only.

2. **Standardize collector-parsable context-pressure and break-glass lines.**
   - **Failure evidence:** local summary reports **`context_budget_warn_count=0`** and **`break_glass_count=0`** across the whole window, but the prompt summary for run **31762652316** reported `CONTEXT_BUDGET_WARN_RATIO: 0.7`.
   - **Root cause category:** inconsistent human-readable warnings that the collector does not count.
   - **Exact fix:** emit canonical lines such as `CONTEXT_BUDGET_WARN: workflow=... phase=... ratio=...` and `BREAK_GLASS: workflow=... phase=... reason=...`.
   - **Expected reliability impact:** medium-high; makes prompt-size regressions and policy/rubric pressure visible before they turn into skipped work or hidden quality loss.
   - **Rollback / fail-open:** safe; logging only.

3. **Make fail-open fallback paths measurable every run.**
   - **Failure evidence:** `issue_pr_status` has a GraphQL-to-REST fallback path in workflow source, `cancel_on_pr_close` loops over cancellation targets, and MCP helper scripts exist, but sampled logs did not emit structured fallback/probe summaries. A secondary summary for **31762683675** suggested a support-ref fallback to `main`, but this was not confirmed in sampled step logs.
   - **Root cause category:** hidden fail-open behavior.
   - **Exact fix:** emit `SUPPORT_REF_FALLBACK requested_ref=... resolved_ref=... reason=...`, plus `GH_API_SUMMARY` and `MCP_STATUS` at the end of each control-plane workflow.
   - **Expected reliability impact:** medium; turns silent degraded behavior into visible, countable events.
   - **Rollback / fail-open:** safe; logging only.

No collector-grade `BREAK_GLASS:` lines, no collector-grade `CONTEXT_BUDGET_WARN:` lines, and no `SEMBLE_*`/`SERENA_*` runtime fallback lines were found in the sampled deep-dive logs.

## AI Memory Health

- **Observed telemetry was minimal.** Sampled deep-dive logs only showed `AI_MEMORY_TELEMETRY` `record-run-event` emissions in `orchestrate_poll` run **31761907002** (`poll_started` and `poll_completed`).
- **Retrieve hit rate is not measurable.** I found **0 sampled `retrieve` operations**, so:
  - `% with records_selected > 0`: **not measurable**
  - average `estimated_tokens` vs budget: **not measurable**
  - `keyword_method` distribution: **not measurable**
- **No sampled memory-health negatives were visible** because the relevant ops were absent:
  - `records_selected=0` on `retrieve`: **not observed**
  - `fail_open: true`: **not observed**
  - `enabled: false`: **not observed**
  - high push retry counts: **not observed**; sampled `record-run-event` used `push_attempts=1`
- **No sampled lifecycle coverage** for `record-candidate`, `finalize-task`, `promote`, `compact`, or `processed-command-*`.
- **Evidence of missing no-op telemetry:** `issue_pr_status` run **31762683675** logged “No linked issues found; skipping lineage finalization,” but that no-op did not surface a corresponding `AI_MEMORY_TELEMETRY` event.

**Recommendation:** extend the existing `AI_MEMORY_TELEMETRY:` emitter in `scripts/memory_helpers.sh` to cover no-op and disabled paths too, e.g.:
- `{"op":"retrieve","enabled":true,"records_selected":0,...}`
- `{"op":"finalize-task","skipped_reason":"no_linked_issues"}`
- `{"op":"retrieve","enabled":false,"fail_open":true,...}`

That is the smallest safe change to make memory usefulness measurable without altering behavior.

## GH API Call Audit

No sampled 429s or secondary rate-limit warnings were observed, so this is mostly an **efficiency and visibility** audit.

| Workflow / step | Evidence | Observed call pattern | Recommendation | Estimated reduction |
|---|---|---|---|---:|
| `orchestrate_poll` / `Find active tracking issues` | 50 runs total; run **31761907002** found `0 active tracking issue(s)` | One issue-list scan per cron tick, even when idle | Add `GH_API_SUMMARY endpoint=issues.list pages=... items=... retries=... ms=...`; if idle ratio stays high, slow idle cadence | ~25 poll scans/window if cadence halves |
| `review_autofix` / `sweep` | 43 runs total; sampled sweeps **31763338965**, **31760737324**, **31758456385** had `candidates=0` | Workflow source enumerates PRs and active review runs, but logs expose only start/end totals | Add `SWEEP_SCAN_SUMMARY` with page/item counts and oldest-candidate age; back off after repeated zero-candidate sweeps | Potentially ~50% of sweep API work if idle sweeps are halved |
| `cancel_on_pr_close` | run **31762683643**, 13s, no matching queued/in-progress runs | Workflow source does two paginated `actions/runs` list calls (`queued`, `in_progress`) before any cancel POSTs | Log list-page counts and, if repo volume stays modest, replace dual status queries with one broader list filtered locally | 50% fewer list calls on no-op path (2→1) |
| `issue_pr_status` | run **31762683675**, 12s, no linked issues | Good GraphQL batching with REST fallback path, but fallback use is invisible in logs | Keep GraphQL batching; add `graphql_nodes=... rest_fallback_issues=... ms=...` | Mostly visibility, not call-count reduction |

Additional audit notes:
- No repository-specific GH API hygiene document was provided in the prompt/context.
- The main current gap is **not** rate limiting; it is **missing per-step API counts, pages, retries, and item totals**.

## Prompt Cache & Memory System

- **Current prompt-cache health is unknown.** Local telemetry shows `cache_hit_rate=null`, `or_cache_write_tokens=0`, and `or_cache_read_tokens=0` across the window.
- **This is a persistent blind spot, not a one-off.** Historical analysis from `analysis/workflow-optimization-2026-07-12.md` already showed `or_calls=175` with `or_total_tokens=0` and `cache_hit_rate=null`.
- **Context pressure may already exist.** Secondary evidence from review/autofix run **31762652316** reported `CONTEXT_BUDGET_WARN_RATIO: 0.7`, but the local collector saw **no** parsable `CONTEXT_BUDGET_WARN:` lines.
- **Semble/Serena effectiveness cannot be evaluated in this window.**
  - No sampled `SEMBLE_QUERY` lines: no evidence Semble reduced prompt expansion.
  - No sampled `SERENA_QUERY` lines: no evidence Serena replaced downstream tool/model work.
  - Run **31762683746** had `SEMBLE_ENABLED=true` and `SERENA_ENABLED=false`, so “enabled but idle/unobserved” is more likely than “actively costly.”
- **Likely cache-fragmentation causes are still only inference.** Possible causes include timestamps, PR candidate lists, support-ref diagnostics, and variable log excerpts appearing before otherwise-stable prompt prefixes. This is **unverified** until prefix hashes and static/dynamic byte counts are logged.

**Concrete logging additions:**
- `MODEL_USAGE_SUMMARY ...`
- `PROMPT_CACHE_SUMMARY phase=... prefix_hash=... cache_read_tokens=... cache_write_tokens=... hit=...`
- `CONTEXT_PAYLOAD_SUMMARY phase=... static_bytes=... dynamic_bytes=... files=... mcp_bytes=...`
- `MCP_CONTEXT_SUMMARY server=... request_bytes=... response_bytes=... target=...`
- `AI_MEMORY_TELEMETRY` on no-op retrieve/finalize paths

Use the field names already present in `scripts/cost_audit.py` wherever possible; that is lower risk than creating a new schema.

## Orchestrator Health

- **Stable, but mostly idle.** The repo had **105/105 successful** workflows, and `orchestrate_poll` had **50/50 successful** runs.
- **Sampled active-work evidence was sparse.**
  - `orchestrate_poll` run **31761907002**: `0 active tracking issue(s)`, graceful exit.
  - post-merge review/autofix run **31762683746**: `No linked issues found for merged PR #3739.`
- **No evidence of active orchestrator failure loops**—no sampled clarification loops, wave stalls, deferral storms, conflict-heal retries, or stuck terminal states.
- **Confidence on deep orchestrator flow is only medium** because the sampled window mostly exercised idle/no-op paths.

**Smallest safe mitigation:** add a single end-of-run state line, e.g. `ORCH_STATE_SUMMARY active_tracking_issues=... linked_issues=... waves_started=... waves_completed=... deferrals=... conflict_heal_retries=... terminal_reason=...`.

**Observable indicators to track weekly:**
- idle poll ratio
- oldest active tracking issue age
- post-merge short-circuit rate (`no_linked_issues`)
- validation red-repo count
- conflict-heal retry count
- stuck-state age

## Pipeline Flow Bottlenecks

| Phase | Bottleneck type | Evidence | Best next fix |
|---|---|---|---|
| Clarify / plan / implement | **Not observable in this window** | Sampled runs were mostly control-plane/no-op; no deep-dive active implementation loop was visible | Add `PHASE_SUMMARY phase=clarify|plan|implement ms=... model_calls=... noop_reason=...` |
| Review / autofix | Queue/startup + eligibility scan | **43 runs**, avg **8.2s**; many 6–7s sweeps had `candidates=0` | Add `SWEEP_SCAN_SUMMARY`; reduce idle sweep cadence only after confirming repeated no-op scans |
| Validate | True compute / self-test | run **31663114780** took **1671s** with red repos and budget exhaustion | Add per-repo stage timings and earlier budget-stop logging |
| Orchestrate | Fixed-cadence idle polling | **50 runs**, avg **124.9s**; run **31761907002** found no work | Add `WORKFLOW_TIMING` + `NOOP_EXIT`; then consider slower idle cadence |
| Merge / lineage | No-op short-circuit | run **31762683746** short-circuited with no linked issues | Emit explicit `SHORT_CIRCUIT reason=no_linked_issues pr=3739` |
| Retry / conflict overhead | Low priority in this window | No sampled retry storms, merge conflicts, or conflict-heal loops | Keep logging counts, but do not optimize first |

Net: this window’s pipeline is **control-plane heavy, not model heavy**.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - Idle `orchestrate_poll`: **50 runs**, **124.9s avg**
  - `validation_refresh`: single **1671s** compute outlier
  - `review_autofix`: **43 runs**, mostly zero-candidate sweeps

- **Top failure modes**
  - Telemetry blind spot: local summary reports **0 model/cache/MCP usage** and `cache_hit_rate=null`
  - False-green validation: run **31663114780** succeeded with **2 red repos**
  - Hidden no-op/fallback paths: no structured per-step API, support-ref fallback, or MCP status summaries

- **Highest-cost drivers**
  - Runner minutes consumed by idle `orchestrate_poll`
  - Long validation refresh compute
  - AI token cost is **currently unknowable** because telemetry is missing

- **Top 3 prioritized actions**
  1. **Add canonical logging first:** `WORKFLOW_TIMING`, `GH_API_SUMMARY`, `MODEL_USAGE_SUMMARY`, `VALIDATION_REPO_RESULT`, `MCP_STATUS`, and no-op `AI_MEMORY_TELEMETRY`.
  2. **Use the new no-op data to retune idle control-plane cadence:** start with `orchestrate_poll`, then `review_autofix` sweep.
  3. **Remove unnecessary work from no-op control-plane paths:** defer checkout in `cancel_on_pr_close` / `issue_pr_status`, and short-circuit docs-only review flows before high-effort reasoning.

## Metrics Appendix

### Overall window

| Scope | Total runs | Success | Failure | Cancelled | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Local workflow-log summary | 105 | 105 | 0 | 0 | 83.06 | 106 | 137 |

### Workflow-family distribution

| Workflow family | Runs | Success | Avg duration (s) | p50 (s) | Notes |
|---|---:|---:|---:|---:|---|
| `orchestrate_poll` | 50 | 50 | 124.94 | 123.5 | Dominant control-plane volume |
| `review_autofix` | 43 | 43 | 8.16 | 7.0 | Mostly zero-candidate sweeps |
| `validation_refresh` | 1 | 1 | 1671.0 | 1671.0 | Dominant compute outlier |
| `copilot_pull_request_reviewer` | 1 | 1 | 152.0 | 152.0 | Single run only |
| `nightly_validation_selftest` | 1 | 1 | 143.0 | 143.0 | Single run only |
| `promote_main_to_stable` | 1 | 1 | 43.0 | 43.0 | Single run only |
| `forward_merge_stable_to_main` | 1 | 1 | 36.0 | 36.0 | Single run only |
| `drift_audit` | 1 | 1 | 16.0 | 16.0 | Single run only |
| `cancel_on_pr_close` | 1 | 1 | 13.0 | 13.0 | Single run only |
| `issue_pr_status` | 1 | 1 | 12.0 | 12.0 | Single run only |
| `workspace_cache_maintenance` | 1 | 1 | 11.0 | 11.0 | Single run only |
| `integration_pr_readiness` | 2 | 2 | 10.0 | 10.0 | Small sample |
| `lint_pr_body_auto_close` | 1 | 1 | 6.0 | 6.0 | Single run only |

### Telemetry coverage and blind spots

| Metric | Local workflow-log `summary.json` | Prompt `analysis_context` | Comment |
|---|---:|---:|---|
| `runs_with_log_telemetry` | 29 | 105 | Coverage conflict; local folder treated as primary evidence |
| `wall_clock_sample_count` | 29 | 105 | Same conflict |
| `wall_clock_p50_ms` | 118000 | 106000 | Different enough to lower confidence |
| `wall_clock_p99_ms` | 1245680 | 151640 | Material mismatch |
| `codex_tokens_used` | 0 | 0 | Blind spot likely persists |
| `or_total_tokens` | 0 | 0 | Blind spot likely persists |
| `or_cache_write_tokens` | 0 | 0 | Blind spot |
| `or_cache_read_tokens` | 0 | 0 | Blind spot |
| `cache_hit_rate` | null | null | Not usable |
| `break_glass_count` | 0 | 0 | No collector-grade events |
| `context_budget_warn_count` | 0 | 0 | No collector-grade events |
| `semble_query_calls` | 0 | 0 | No sampled runtime telemetry |
| `serena_query_calls` | 0 | 0 | No sampled runtime telemetry |

### Sampled log signals

| Signal | Observed in sampled deep-dive logs | Evidence |
|---|---:|---|
| `AI_MEMORY_TELEMETRY` | Minimal | `record-run-event` only in run **31761907002** |
| `retrieve` memory ops | 0 | No sampled retrieval coverage |
| `BREAK_GLASS:` | 0 | None found |
| `CONTEXT_BUDGET_WARN:` | 0 | None found; only secondary summary evidence on **31762652316** |
| `SEMBLE_QUERY` / `SEMBLE_FALLBACK` | 0 | None found |
| `SERENA_QUERY` / `SERENA_FALLBACK` / `SERENA_PROBE` | 0 | None found |
| GH 429 / secondary rate limit | 0 | None found |

### GH API summary

| Workflow family | Sample run | API pattern | Observed issue |
|---|---:|---|---|
| `orchestrate_poll` | 31761907002 | Issue-list scan for tracking issues | No per-call counts/pages/latency |
| `review_autofix` | 31763338965 / 31760737324 / 31758456385 | PR scan + active-run snapshot | Zero-candidate scans not explainable |
| `cancel_on_pr_close` | 31762683643 | Two `actions/runs` list queries + optional cancel loop | No-op path still pays dual list calls |
| `issue_pr_status` | 31762683675 | GraphQL batched lookup + REST fallback path | Fallback counts not logged |

### MCP availability

| Server | Target | Probe ok | Probe failed | Probe skipped | Query calls | Fallbacks | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| Semble | none observed | n/a | n/a | n/a | 0 | 0 | `SEMBLE_ENABLED=true` seen in run **31762683746**, but no runtime telemetry lines |
| Serena | none observed | 0 | 0 | 0 | 0 | 0 | `SERENA_ENABLED=false` seen in run **31762683746** |
| Other MCP servers observed | none | 0 | 0 | 0 | 0 | 0 | No unknown `<NAME>_*` telemetry found |

**Data-quality note:** the secondary prompt context included at least one inconsistent run row—**31684044551** was labeled `review_autofix` while its summary text described a forward-merge step—so local deep-dive logs and the local folder `summary.json` should be treated as canonical for this window.
