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

## Deep Audit — Workflows & Scripts (2026-08-14)

### Section 1: Bug & Correctness Sweep

#### BUG-001
- **File path & line range:** `scripts/tg_helpers.sh:155-206,227-277`
- **Severity:** Medium
- **Category tag:** `bug`
- **Description:** Both `tg_store_msg_id` and `tg_store_phase_msg_id` do a read-modify-write against a shared GitHub issue comment: fetch recent comments, locate the first marker comment, append a new Telegram message ID with `sed`, then `PATCH` the full body. These helpers are called from multiple workflow paths (for example `.github/workflows/plan.yml:1548,1907`, `.github/workflows/review_autofix.yml:4941,6626`, and `scripts/orchestrate_poll_process.sh:101-122`). Two near-simultaneous sends can therefore read the same old body and race; the last `PATCH` wins and drops the other ID. `tg_cleanup_msgs` only deletes IDs that still exist in the stored markers (`scripts/tg_helpers.sh:383-440`), so dropped IDs become orphaned Telegram messages.
- **Recommended fix:** Replace the shared-marker update with an append-only model: create one hidden tracking comment per Telegram message (or per phase message) and let `tg_cleanup_msgs` delete all matching marker comments. If comment reuse must stay, add a retry/merge loop that re-reads the current body and retries until the new ID is confirmed present.

### Section 2: GitHub API Call Redundancy Audit

#### API-001
- **File path & line range:** `.github/workflows/review_autofix.yml:546-587,610-616`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** The review gate fetches `repos/${REPOSITORY}/pulls/${PR_NUMBER}/files` at line 555 for doc-only detection, then can fetch the exact same paginated endpoint again at line 612 when `candidate_skip=true` and `pr_files_json` is still empty/`[]`. This is the same data, same fields, same execution path.
- **Current call count:** `2` paginated REST reads on the affected path.
- **Proposed call count:** `1`.
- **Batching/helper pattern to extend:** Reuse the step-local cache (`pr_files_json` / `file_count`) the way `scripts/orchestrate_poll_process.sh` reuses cycle-local JSON blobs instead of refetching.
- **Recommended fix:** Treat the first `/files` response as authoritative for both doc-only detection and materiality suppression. If the first fetch fails, carry a single fail-open flag forward instead of reissuing the endpoint.

#### BATCH-001
- **File path & line range:** `scripts/orchestrate_poll_process.sh:14889-14920`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** The feature sweep lists open PRs once, then for every behind `ai/issue-*` PR performs an extra `GET /pulls/{n}` solely to read `.head.sha` before `update-branch`. The per-PR read is inside the loop.
- **Current call count:** `1 + N_behind` metadata reads (`gh pr list` once, then one `GET /pulls/{n}` per behind PR), plus unchanged `update-branch` writes.
- **Proposed call count:** `1` metadata read query, plus unchanged `update-branch` writes.
- **Batching/helper pattern to extend:** Add a GraphQL helper beside `_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql` that returns `number`, `headRefName`, `baseRefName`, `mergeable`, `mergeStateStatus`, and `headRefOid` in one pass.
- **Recommended fix:** Replace the `gh pr list` + per-PR `GET /pulls/{n}` pattern with a single GraphQL open-PR fetch that already includes `headRefOid`, then pass that SHA directly to `update-branch`.

#### BATCH-002
- **File path & line range:** `scripts/orchestrate_poll_process.sh:2811-2825,3019-3060`
- **Severity:** Low
- **Category tag:** `api-batching`
- **Description:** `_subissue_closing_pr_number` does one `gh pr list --head` lookup, then on a tier-1 miss performs one timeline fetch via `_issue_cross_ref_pr_numbers_unique`, then one `GET /pulls/{n}` per cross-referenced PR to inspect `merged_at` and `body`. The file’s own comment already documents the per-PR REST fetch.
- **Current call count:** `2 + N_xref` reads on the tier-1-miss path.
- **Proposed call count:** `3` reads regardless of `N_xref` (`gh pr list --head`, timeline fetch, one batched GraphQL PR-hydration call).
- **Batching/helper pattern to extend:** Reuse the alias-batching style from `_fetch_linked_pr_status_graphql` to hydrate candidate PR `body`/`mergedAt` fields in one GraphQL request.
- **Recommended fix:** Keep the existing timeline narrowing, but batch-hydrate the resulting PR numbers in one GraphQL alias query and scan bodies in-memory instead of calling `GET /pulls/{n}` in the loop.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path & line range:** `scripts/stage_workflow_support.sh:1-972; .github/workflows/implement.yml:850-920; .github/workflows/check_failure_triage.yml:196-221; .github/workflows/plan.yml:278-315; .github/workflows/clarify.yml:278-315; .github/workflows/orchestrate.yml:340-357; .github/workflows/orchestrate_poll.yml:331-350; .github/workflows/orchestrate_clarify_respond.yml:277-316`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** `scripts/stage_workflow_support.sh` already centralizes workflow-support staging and is used by `validate.yml` and `review_autofix.yml`, but at least seven other workflows still carry bespoke `for f in ... install -m ...` staging loops plus their own fallback logic. The duplicated file lists and copy rules are materially similar across these workflows. This creates drift risk (inference): adding or changing a support asset requires synchronized edits in many places.
- **Recommended fix:** Make `scripts/stage_workflow_support.sh` the single owner for support staging across these workflows. Extend its manifest mode so callers use a common entrypoint such as `scripts/stage_workflow_support.sh <target> --manifest <path>`. Update callers in `implement.yml`, `check_failure_triage.yml`, `plan.yml`, `clarify.yml`, `orchestrate.yml`, `orchestrate_poll.yml`, and `orchestrate_clarify_respond.yml` to emit a small manifest and invoke the helper instead of inlining install loops.

#### DUP-002
- **File path & line range:** `.github/workflows/issue_pr_status.yml:41-175,542-575,642-680`
- **Severity:** Low
- **Category tag:** `duplication`
- **Description:** `issue_pr_status.yml` implements support-repo checkout/copy logic three different ways in one workflow: a generic `checkout_support_ref`/`fetch_from_ref_or_local` path for memory helpers, then two separate ad-hoc clone/copy paths just to stage `tg_helpers.sh` for the merged-alert and cleanup steps.
- **Recommended fix:** Move this workflow onto one shared support-fetch path. The smallest reuse point is `scripts/stage_workflow_support.sh` with a helper such as `fetch-file <repo-path> <target-path> [--allow-main-fallback]`, or stage `scripts/tg_helpers.sh` during the first helper-fetch step and reuse the same staged file in both later steps. Callers to update: `Fetch memory helper scripts`, `Send PR merged Telegram alert`, and `Cleanup tracked Telegram messages`.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001
- **File path & line range:** `.github/workflows/plan.yml:998-1286`
- **Severity:** High
- **Category tag:** `expression-limit`
- **Description:** The `Run Codex planning` `run:` block is about **19,527** characters, leaving only **1,473** characters of headroom below GitHub Actions’ 21,000-character template-expression limit. The whole block is expression-compiled because it contains `${{ }}` interpolations at lines 1235 and 1276.
- **Recommended fix:** Move the large inline planning prompt out of the workflow body and into a prompt asset already aligned with repo conventions (`prompts/mode-plan.txt` or `prompts/_templates/...`), then render it via `scripts/render_prompt.sh` / `scripts/assemble_prompt.sh`. Keep the workflow step limited to orchestration and retries.

#### EXPR-002
- **File path & line range:** `.github/workflows/implement.yml:3675-3919`
- **Severity:** High
- **Category tag:** `expression-limit`
- **Description:** The `Destructive-commit guard — label + alert on rejection` `run:` block is about **18,329** characters, leaving **2,671** characters of headroom. It contains many `${{ github.repository }}` / `${{ github.run_id }}` interpolations, so the whole rejection handler sits close to the hard limit.
- **Recommended fix:** Split the scope-block path and the destructive-delete path into separate steps or extract both branches to a dedicated shell helper under `scripts/`. That reduces both expression-size risk and the amount of duplicated label/comment/Telegram handling in the workflow YAML.

#### EXPR-003
- **File path & line range:** `.github/workflows/implement.yml:852-1146`
- **Severity:** Medium
- **Category tag:** `expression-limit`
- **Description:** The `Stage workflow support files` block in `implement.yml` is about **15,950** characters, leaving **5,050** characters of headroom. Only a few `${{ }}` tokens (for example lines 861, 1036, and 1037) force the entire large support-staging script through the expression compiler.
- **Recommended fix:** Replace the inline staging block with a call to `scripts/stage_workflow_support.sh` plus a manifest, or split the env writes into a tiny step and move the rest of the staging logic into a shell helper.
  
No workflow file exceeds the 800 KB audit threshold; the largest scanned workflow is `.github/workflows/review_autofix.yml` at **445,234** bytes.

### Section 5: Cross-Cutting Concerns

#### CONSIST-001
- **File path & line range:** `scripts/tg_helpers.sh:175-205,246-276,364-439`
- **Severity:** Low
- **Category tag:** `consistency`
- **Description:** `tg_helpers.sh` uses the repo’s rate-limit-aware `curl_gh_api` helper for GitHub reads (`scripts/tg_helpers.sh:169-172,241-244,332-335,401-404`), but switches to raw `curl` for GitHub comment `POST`/`PATCH`/`DELETE` operations and suppresses failures with `|| true`. That bypasses the retry/backoff behavior centralized in `scripts/gh_helpers.sh:646-695` and makes single-writer failures silent, even when BUG-001’s race does not occur.
- **Recommended fix:** Route all GitHub comment writes/deletes through `curl_gh_api -X POST|PATCH|DELETE ...` (or a tiny wrapper built on it that accepts JSON payloads) and log the HTTP failure path before failing open.

#### DEBT-001
- **File path & line range:** `.github/workflows/check_failure_triage.yml:117-135`
- **Severity:** Low
- **Category tag:** `tech-debt`
- **Description:** The fork-PR guard carries a hand-rolled three-attempt `gh api` retry loop even though the repo standard for GitHub reads is `gh_retry` / `gh_api_json_to_file` in `scripts/gh_helpers.sh:398-529,578-620`. Because this guard runs before support staging, its retry semantics can drift from the repo standard on backoff, rate-limit handling, and JSON validation.
- **Recommended fix:** Replace the inline loop with a tiny pre-checkout helper that mirrors `gh_api_json_to_file` semantics, or embed a minimal local wrapper in the workflow so this step stays aligned with the repo’s standard GH API behavior without changing the fork-safety ordering.

#### SHELL-001
- **File path & line range:** `scripts/stage_workflow_support.sh:129-139,206-219`
- **Severity:** Low
- **Category tag:** `shellcheck`
- **Description:** `stage_workflow_support.sh` uses one-item `for` loops (`for f in transcript_archive.sh; do`, `for f in unattended_system_instructions.md; do`) that ShellCheck flags as `SC2043`. They are harmless today, but they obscure that these are scalar operations, not real list-driven staging loops.
- **Recommended fix:** Replace each one-item loop with a direct scalar block, or convert the surrounding code to array-driven helpers so loop structure only appears where a real list exists.

No `TODO` / `FIXME` / `HACK` markers were found under `.github/workflows/` or `scripts/`. No material dead-code candidate was confirmed after targeted usage checks of suspected helpers.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | EXPR-001, EXPR-002 |
| Medium | 4 | BUG-001, BATCH-001, DUP-001, EXPR-003 |
| Low | 6 | API-001, BATCH-002, DUP-002, CONSIST-001, DEBT-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 2 | Medium |
| Code modularization | 9 | Large |
| Expression size reduction | 4 | Medium |
| Medium/Low fixes | 4 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-08-14)

### Safety Tag Legend
`SAFE_TO_MERGE` means the consolidation is provably same-scope/same-semantics from static reading alone; `NEEDS_VERIFICATION` means overlapping data is real but freshness, step-boundary, or failure-semantic checks are still required; `RISKY_SKIP` means the overlap sits in a paginated, retry/race-defense, or control-plane path that must not be auto-implemented without manual review.

### Consolidation Candidates (MERGE-###)

#### MERGE-001
- **Safety tag:** `RISKY_SKIP`
- **File path and line ranges:** `.github/workflows/cancel_on_pr_close.yml:111-120`, `.github/workflows/cancel_on_pr_close.yml:122-131`
- **Current call count / proposed call count:** `2` list calls → `1` list call
- **Endpoint(s):** `GET /repos/{owner}/{repo}/actions/runs`
- **Evidence:**
  ```sh
  queued_runs_json="$(
    gh_retry gh api \
      --method GET \
      "repos/${REPOSITORY}/actions/runs" \
      --paginate \
      -f status=queued \
      -f event=pull_request \
      -f "branch=${PR_HEAD_REF}" \
      -f per_page=100 \
    | jq -s "${RUNS_JQ}"
  )"
  in_progress_runs_json="$(
    gh_retry gh api \
      --method GET \
      "repos/${REPOSITORY}/actions/runs" \
      --paginate \
      -f status=in_progress \
      -f event=pull_request \
      -f "branch=${PR_HEAD_REF}" \
      -f per_page=100 \
    | jq -s "${RUNS_JQ}"
  )"
  ```
  The step already recombines both result sets locally at `.github/workflows/cancel_on_pr_close.yml:134-159`; the only API-level difference is the `status=` filter.
- **Proposed fix:** If manually approved, replace the dual status-specific reads with one broader `actions/runs` read that keeps the existing `event=pull_request`, `branch=${PR_HEAD_REF}`, and `per_page=100` filters, then preserve the current local `jq`/`awk` filtering and dedupe.
- **Safety rationale:** This is a `--paginate` call inside a PR-close cancellation path, which hits both the paginated-call and control-plane `RISKY_SKIP` triggers.
- **Downstream signal:** Manual review only: prove a single broader `actions/runs` query preserves page-boundary coverage for both queued and in-progress runs and does not change cancellation/no-match behavior before implementing.

#### MERGE-002
- **Safety tag:** `RISKY_SKIP`
- **File path and line ranges:** `.github/workflows/review_autofix_sweep.yml:114-126`, `.github/workflows/review_autofix_sweep.yml:151-153`
- **Current call count / proposed call count:** `4` snapshot calls per sweep (`2` statuses × `2` workflows) → `2` snapshot calls per sweep
- **Endpoint(s):** `GET /repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs`
- **Evidence:**
  ```sh
  snapshot_active_review_runs() {
    local workflow="$1"
    local status snapshot_json='{}' cached_head_ref cached_count

    if snapshot_json="$(
      for status in queued in_progress; do
        gh api --paginate -X GET "repos/${REPOSITORY}/actions/workflows/${workflow}/runs" \
          -f status="${status}" \
          -f per_page=100 \
          2>/dev/null || true
      done | jq -c -s '
  ```
  and the function is invoked for two workflows:
  ```sh
  for wf in internal-review.yml review_autofix.yml; do
    snapshot_active_review_runs "${wf}"
  done
  ```
- **Proposed fix:** If manually approved, change `snapshot_active_review_runs()` to issue one paginated read per workflow and keep the existing local `unique_by(.id)` plus `head_branch` counting logic unchanged.
- **Safety rationale:** This is a paginated active-run snapshot used as a concurrency gate, so it falls under the paginated-call and race-sensitive control-path `RISKY_SKIP` rules.
- **Downstream signal:** Manual review only: capture a busy sweep sample where queued and in-progress runs span multiple pages, then verify that one per-workflow snapshot preserves the current `active_review_runs["${workflow}:${head_ref}"]` counts before changing the loop.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001
- **Safety tag:** `NEEDS_VERIFICATION`
- **File path and line ranges:** `.github/workflows/check_failure_triage.yml:117-137`, `scripts/check_failure_triage.sh:151-178`
- **Current call count / proposed call count:** `2` successful PR-metadata reads on the same-repo path → `1` on that path, with cache-miss fallback retained
- **Endpoint(s):** `GET /repos/{owner}/{repo}/pulls/{pull_number}`
- **Evidence:**
  ```sh
  # .github/workflows/check_failure_triage.yml:124-137
  if gh api "repos/${GITHUB_REPOSITORY}/pulls/${{ inputs.pr_number }}" > "${pr_payload}" 2>/dev/null; then
    break
  fi
  ...
  HEAD_REPO="$(jq -r '.head.repo.full_name // ""' "${pr_payload}" 2>/dev/null || echo '')"
  ```
  ```sh
  # scripts/check_failure_triage.sh:151-178
  PR_JSON_FILE="${RUNTIME_DIR}/pr_payload.json"
  if gh_api_json_to_file "${PR_JSON_FILE}" gh api "repos/${REPO}/pulls/${PR_NUMBER}"; then
    PR_JSON="$(cat "${PR_JSON_FILE}")"
  fi
  ...
  PR_STATE="$(printf '%s' "${PR_JSON}" | jq -r '.state // ""')"
  HEAD_REF="$(printf '%s' "${PR_JSON}" | jq -r '.head.ref // ""')"
  PR_TITLE="$(printf '%s' "${PR_JSON}" | jq -r '.title // ""')"
  PR_URL="$(printf '%s' "${PR_JSON}" | jq -r '.html_url // ""')"
  HEAD_REPO_FULL_NAME="$(printf '%s' "${PR_JSON}" | jq -r '.head.repo.full_name // ""')"
  ```
  The guard already fetched the exact PR payload needed for `HEAD_REPO`; the script re-fetches the same endpoint later because the first payload is discarded. The runtime workspace is only created later at `.github/workflows/check_failure_triage.yml:267-273`, so there is no current cache handoff.
- **Proposed fix:** Persist the successful guard payload to a stable temp path (for example under `$RUNNER_TEMP`) and export that path; then update the PR-resolution block in `scripts/check_failure_triage.sh` to seed `PR_JSON_FILE` from the pre-fetched payload when present and valid JSON, falling back to `gh_api_json_to_file` only on cache miss.
- **Safety rationale:** This crosses workflow-step/script boundaries, and the later script also uses `.state` to short-circuit closed PRs, so the SAFE_TO_MERGE same-step/freshness preconditions are not proven statically.
- **Downstream signal:** Verify three cases before removing the second GET: same-repo open PR, fork PR short-circuit, and PR closed between the guard step and script start; if the third case changes behavior, keep a narrow live `.state` refresh but reuse cached title/body/head-repo data.

#### REUSE-002
- **Safety tag:** `NEEDS_VERIFICATION`
- **File path and line ranges:** `scripts/review_collect_pr_metadata.sh:258-263`, `.github/workflows/review_autofix.yml:4872-4916`, `scripts/review_rb_judge.sh:834-842`
- **Current call count / proposed call count:** `2` successful linked-issue reads on the common cache-populated path → `1` on that path
- **Endpoint(s):** GitHub GraphQL `repository.pullRequest.closingIssuesReferences(first: 50)`
- **Evidence:**
  ```sh
  # scripts/review_collect_pr_metadata.sh:258-263,277-278
  if gh_retry "${_linked_tmp}" api graphql \
    -f owner="${REPOSITORY_OWNER}" \
    -f name="${REPOSITORY_NAME}" \
    -F number="${PR_NUMBER}" \
    -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){closingIssuesReferences(first:50){nodes{number title body}}}}}' \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes // []'; then
  ...
    printf 'LINKED_ISSUES_JSON=%s\n' "${_linked_numbers}" >> "${GITHUB_ENV}"
  ```
  ```sh
  # .github/workflows/review_autofix.yml:4883-4887
  # Reuse LINKED_ISSUES_JSON from the early "Collect PR metadata" step
  # which already fetched closingIssuesReferences via GraphQL ...
  if [ -n "${LINKED_ISSUES_JSON+x}" ]; then
  ```
  ```sh
  # scripts/review_rb_judge.sh:834-842
  ISSUE_NUMBERS="$(gh_retry gh api graphql \
    -f owner="${REPOSITORY%/*}" \
    -f name="${REPOSITORY#*/}" \
    -F number="${PR_NUMBER}" \
    -f query='query($owner:String!, $name:String!, $number:Int!) { repository(owner:$owner, name:$name) { pullRequest(number:$number) { closingIssuesReferences(first: 50) { nodes { number } } } } }' \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes[].number' || true)"

  if [ -z "${ISSUE_NUMBERS}" ]; then
    ISSUE_NUMBERS="$(printf '%s' "${LINKED_ISSUE_FALLBACK_NUMBERS_JSON:-[]}" | jq -r '.[]' 2>/dev/null || true)"
  fi
  ```
  By judge time, the workflow has already materialized `LINKED_ISSUES_JSON`; the judge ignores that cache and re-queries GraphQL.
- **Proposed fix:** Update `scripts/review_rb_judge.sh` to read `ISSUE_NUMBERS` from `LINKED_ISSUES_JSON` first, keep `LINKED_ISSUE_FALLBACK_NUMBERS_JSON` as the next fallback, and only call GraphQL when the cache is truly unset/invalid. If any pre-judge PR-body edit can change closing refs, refresh `LINKED_ISSUES_JSON` immediately after that edit instead of inside the judge.
- **Safety rationale:** The reuse crosses workflow-step/script boundaries, and closing references are body-derived state, so freshness must be verified before eliminating the judge’s live GraphQL read.
- **Downstream signal:** Verify that every path into `review_rb_judge.sh` first executes the cache-population step, and audit earlier PR-body mutations—at minimum `scripts/review_conflict_resolve.sh:1503-1508`—to prove they cannot change closing references without also refreshing `LINKED_ISSUES_JSON`.

#### REUSE-003
- **Safety tag:** `NEEDS_VERIFICATION`
- **File path and line ranges:** `.github/workflows/review_autofix.yml:1630-1631`, `.github/workflows/review_autofix.yml:5367-5368`, `scripts/review_collect_pr_metadata.sh:209-234`, `scripts/review_enable_auto_merge.sh:127-136`, `scripts/review_enable_auto_merge.sh:192-224`, `scripts/review_conflict_resolve.sh:1425-1438`, `scripts/review_conflict_resolve.sh:1503-1508`
- **Current call count / proposed call count:** `2` successful PR-metadata reads on the normal review path → `1` on that path, with live GET retained as cache-miss fallback
- **Endpoint(s):** `GET /repos/{owner}/{repo}/pulls/{pull_number}`
- **Evidence:**
  ```sh
  # scripts/review_collect_pr_metadata.sh:209-234
  gh_retry "${PR_PAYLOAD_FILE}" api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"
  ...
  jq '{
    title: (.title // ""),
    body: (.body // ""),
    baseRefName: (.base.ref // ""),
    headRefName: (.head.ref // ""),
    headRepoFullName: (.head.repo.full_name // "")
  }' "${PR_PAYLOAD_FILE}" > "${PR_META_FILE}"
  ```
  ```sh
  # .github/workflows/review_autofix.yml:1630-1631,5367-5368
  echo "PR_PAYLOAD_FILE=${RUNTIME_DIR}/pr_payload.json"
  echo "PR_META_FILE=${RUNTIME_DIR}/pr_meta.json"
  ...
  bash "${SUPPORT_SCRIPTS_DIR}/review_enable_auto_merge.sh"
  ```
  ```sh
  # scripts/review_enable_auto_merge.sh:127-136,192-214
  if ! _ORCH_PR_META_JSON="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}" 2>"${_orch_pr_meta_err_file}")"; then
  ...
  _orch_pr_head_ref="$(printf '%s' "${_ORCH_PR_META_JSON}" | jq -r '.head.ref // ""' 2>/dev/null || echo "")"
  ...
  _orch_pr_body="$(printf '%s' "${_ORCH_PR_META_JSON}" | jq -r '.body // ""' 2>/dev/null || echo "")"
  ```
  ```sh
  # scripts/review_conflict_resolve.sh:1425-1438,1503-1508
  jq --rawfile body "${_body_file}" '.body = $body' "${PR_META_FILE}" > "${_tmp}" \
    && mv "${_tmp}" "${PR_META_FILE}" || true
  ...
  if ! gh_retry gh pr edit "${PR_NUMBER}" --repo "${GITHUB_REPOSITORY}" --body-file "${_body_file}" >/dev/null; then
  ...
  _sync_local_pr_body_from_file "${_body_file}"
  ```
  The review job already maintains job-local PR metadata files, and the only observed PR-body edit path synchronizes them; the auto-merge helper still re-fetches the full PR.
- **Proposed fix:** Teach `scripts/review_enable_auto_merge.sh` to prefer `PR_META_FILE` for `.head.ref` and `.body` (or `PR_PAYLOAD_FILE` as a secondary file fallback) when the cache files exist and contain valid JSON, and only fall back to the live `gh api` GET on cache miss/invalid cache.
- **Safety rationale:** This crosses step/script boundaries, and static reading does not fully prove there are no other pre-auto-merge metadata mutations or deleted-head-branch cases that rely on the live fetch.
- **Downstream signal:** Audit all pre-auto-merge PR metadata mutations—at minimum `scripts/review_conflict_resolve.sh:1503-1508`—and confirm the cache files remain authoritative for `.head.ref` and `.body`; if not, add a targeted refresh immediately after the mutating step before removing the live GET.

### Dead Calls (DEAD-API-###)
No findings.

### Cross-References to Deep Audit Section
- API-001: `RISKY_SKIP` — same-endpoint duplication is real, but `/pulls/{pr}/files` is paginated, so consolidation is not auto-safe under this prompt’s rules.
- BATCH-001: `RISKY_SKIP` — the proposed batching lives in `scripts/orchestrate_poll_process.sh`, which this prompt explicitly treats as a manual-review path.
- BATCH-002: `RISKY_SKIP` — also inside `scripts/orchestrate_poll_process.sh`; changing the tiered lookup/batching path would alter orchestrator race/pagination behavior and must stay manual.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 3 | REUSE-001, REUSE-002, REUSE-003 |
| RISKY_SKIP | 2 | MERGE-001, MERGE-002 |

### Implement-Stage Handoff
- No SAFE_TO_MERGE findings in this pass.
