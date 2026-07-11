## Executive Summary

- **`orchestrate_poll` is the main systemic latency sink.** It ran 38 times with `p50=188s` and `p95=205.45s`, and sampled runs `29154088259`, `29153165352`, `29152503868`, and `29151751569` repeatedly logged runner-wait messages before doing useful work. Removing even one poll hop from the critical path would save about **3 minutes** per active orchestration path. **Impact: high. Confidence: high.**

- **The worst long-tail comes from review fanout, not from hard failures.** `review_autofix` run `29152499040` took `4994s`; `step-046 Run_reviewer_models` alone took about `4299s`, with six reviewer models, `ENABLE_REVIEWER_TWO_PASS=true`, and a moonshot retry that burned about **24m52s** after an empty output/rate-limit classification. Skipping or shrinking pass 2 on low-risk diffs would cut similar outliers by **~22–47 minutes**. **Impact: high. Confidence: high.**

- **Skipped child-workflow fanout is a systemic noise pattern.** Repo-wide, `63/147` runs ended in `other`, and those counts line up exactly with skipped child families: `clarify=17`, `plan=15`, `implement=15`, `orchestrate_clarify_respond=16`. That is low compute cost per run, but it adds queue churn, API churn, and observability noise. **Impact: medium. Confidence: high.**

- **Reliability risk is currently masked as “successful” runs.** In `review_autofix` run `29152499040`, the consolidator failed open with `Not inside a trusted directory...`, then the parser failed open with `no_issue_markers`, yet the workflow still concluded success. The next reliability win is surfacing degraded-success states, not fixing crash rate (`failure_count=0`). **Impact: medium. Confidence: high.**

- **Cost/cache tuning is blocked by telemetry gaps.** Aggregate telemetry shows `26` OR/OpenRouter calls but `0` OR tokens, `cache_hit_rate=null`, and no `CONTEXT_BUDGET_WARN` or `BREAK_GLASS` events even when one editor prompt reached `285,853` bytes in run `29152499040`. The first safe move is to improve source-side token/cache logging before changing models aggressively. **Impact: high. Confidence: high.**

## Speed Optimizations

1. **Collapse queue-heavy `orchestrate_poll` hops** *(critical-path win)*  
   - **Evidence:** `orchestrate_poll` had `38` runs, `p50=188s`, `p95=205.45s`. Sampled runs `29154088259`, `29153165352`, `29152503868` logged `Waiting for a runner...` / `hosted runner to come online`. In `29154088259`, the poll step itself took about `195s`, and `step-019 Process each tracking issue` took about `60s`.
   - **Root cause:** separate poll jobs appear to repay runner queue/startup overhead on every hop.
   - **Exact change:** first add per-job timing telemetry; if queue time remains dominant for a week, keep one runner alive for one extra poll iteration or suppress redispatch when tracked state is unchanged.
   - **Estimated savings:** about **188s per removed poll hop**.
   - **Implementation risk:** medium; keep fail-open to current one-hop behavior on timeout.
   - **Add logs:** `queue_wait_ms`, `runner_assignment_ms`, `job_setup_ms`, `poll_compute_ms`, `tracking_issue_count`, `unchanged_state_count`, `redispatch_after_ms`.

2. **Short-circuit pass 2 and bound stalled reviewer retries** *(critical-path win)*  
   - **Evidence:** run `29152499040` used six reviewer models across two passes. Pass 2 started at `13:15:09` and the second summarizer finished at `13:37:08` even though the log said the pass-2 diff was `0 LOC < 200`. Moonshot failed with empty output on attempt 1, was classified `retryable (rate_limit)`, and did not succeed until `13:13:41`; a stall marker recorded `idle_secs=600`.
   - **Root cause:** low-risk diffs still take the full two-pass fanout, and per-model retry/stall limits are too loose.
   - **Exact change:** re-enable/log a risk-tier gate; skip pass 2 entirely when pass-1 consensus is strong and the diff is below threshold, or reduce pass 2 to 1–2 models; cap model idle time and continue with a partial quorum.
   - **Estimated savings:** **~22 minutes** by skipping pass 2 on similar runs, **~47 minutes** if the long moonshot retry is also bounded.
   - **Implementation risk:** low-medium; fall back to the current full fanout when reviewers disagree.
   - **Add logs:** `review_risk_tier`, `models_selected`, `pass2_enabled`, `pass2_reason`, `model_attempt`, `retry_reason`, `first_token_ms`, `last_token_ms`, `stall_idle_secs`, `response_bytes`.

3. **Preflight child-workflow eligibility in the parent orchestrator** *(micro-optimization, high hygiene value)*  
   - **Evidence:** the repo’s entire `other_count=63` comes from child workflows that skip almost immediately. Visible clusters include `29154634864`/`29154634877`/`29154634858`/`29154634870` at `2026-07-11T13:37:14Z`, all skipped in `1s`.
   - **Root cause:** parent orchestration is dispatching children before checking conditions that the child later rejects.
   - **Exact change:** mirror child eligibility checks in the parent and suppress dispatch when the skip outcome is already knowable; keep manual override support.
   - **Estimated savings:** only **~3–5s** per skip cluster, but fewer queued jobs and cleaner dashboards.
   - **Implementation risk:** low.
   - **Add logs:** `parent_run_id`, `child_workflow`, `eligible`, `skip_reason`, `dispatch_source`, `dispatch_suppressed_count`.

## Cost Optimizations

1. **Cut low-value reviewer fanout on small diffs**  
   - **Evidence:** `review_autofix` recorded `26` OR calls. In run `29152499040`, the review stage effectively did `14` AI review calls before editor/consolidator work: 6 reviewers + summarizer in pass 1, then 6 reviewers + summarizer in pass 2. The pass-2 diff was logged as `0 LOC < 200`.
   - **Root cause:** small/low-risk reviews are paying for the same fanout pattern as larger or disputed reviews.
   - **Exact change:** skip pass 2 when pass-1 consensus is strong, or restrict pass 2 to 1–2 tie-breaker models; lower reasoning effort on tiny diffs instead of using `high`.
   - **Estimated savings:** **4–7 AI calls per low-risk review** and a large share of OR spend on outliers.
   - **Quality-risk note:** low if full fanout remains the fallback on disagreement.
   - **Add logs:** `review_call_count`, `summarizer_call_count`, `reasoning_effort`, `consensus_score`, `fanout_saved_calls`.

2. **Fix OR/cache telemetry before making bigger pricing or model changes**  
   - **Evidence:** repo aggregate shows `26` OR calls, but `or_prompt_tokens=0`, `or_completion_tokens=0`, `or_total_tokens=0`, `or_cache_write_tokens=0`, `or_cache_read_tokens=0`, and `cache_hit_rate=null`.
   - **Root cause:** source logs or parsers are not emitting/normalizing numeric token and cache fields reliably.
   - **Exact change:** emit numeric token/cache values at the call site and add parse-status counters so “missing” is distinct from “zero”.
   - **Estimated savings:** **unknown until visible**; this is the prerequisite to any credible cache or model-cost optimization.
   - **Quality-risk note:** none.
   - **Add logs:** `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_write_tokens`, `cache_read_tokens`, `cache_status`, `parse_status`, `model`.

3. **Trim implement-context assembly**  
   - **Evidence:** `implement` accounts for `1,326,191 / 1,338,349` recorded Codex tokens (~`99.1%`). In sampled implement run `29152267029`, AI memory retrieved `31` records (`1405/1600` estimated tokens) and Semble added two overflow pulls totaling `13,190` bytes.
   - **Root cause:** **inference:** implement prompts are likely near budget and may be carrying duplicated or low-signal context.
   - **Exact change:** dedupe memory records before prompt assembly, cap low-value overflow attachments, and log per-source byte/token contribution.
   - **Estimated savings:** **~5–15%** of implement tokens if duplicate context is being re-inlined.
   - **Quality-risk note:** medium; fail open to the current full context when relevance scores are close.
   - **Add logs:** `memory_tokens`, `memory_record_count`, `memory_deduped_count`, `semble_bytes`, `file_context_bytes`, `prompt_headroom_tokens`.

4. **Measure Semble’s savings explicitly; treat Serena as disabled/uninstrumented until proven active**  
   - **Evidence:** repo-wide Semble volume is low (`4` calls, `42,070` bytes, `0` fallbacks). Sampled calls were targeted: `reviewer-context` `14,440` bytes in `29152499040`; `overflow` `6,595` bytes twice in `29152267029`. Serena recorded `0` queries, `0` fallbacks, and `0` probes.
   - **Interpretation:** Semble does **not** look like a runaway cost source; it appears targeted. But because the downstream editor prompt still reached `285,853` bytes in `29152499040`, it is not yet proving that it replaces enough inline context. Serena currently provides no measured benefit.
   - **Exact change:** log “bytes kept out of prompt because of Semble” and emit a one-line Serena enabled/disabled status per run.
   - **Estimated savings:** low direct savings immediately; high attribution value.
   - **Quality-risk note:** none.
   - **Add logs:** `semble_bytes_saved_vs_inline`, `semble_target`, `serena_enabled`, `serena_disabled_reason`.

*Avoidable reruns are not a current cost driver: repo `failure_count=0`, and sampled recent runs were `attempt=1`, `retries=0`.*

## Reliability Improvements

1. **Fix and surface fail-open review stages as degraded-success events**  
   - **Failure evidence:** run `29152499040` logged `stage=consolidator ... failopen=1` with `Not inside a trusted directory...`, followed by `stage=parser event=no_issue_markers failopen=1`.
   - **Root cause category:** environment/config + silent fail-open handling.
   - **Exact fix:** set Git safe-directory before the consolidator/parser step, or pass the safe equivalent only for the ephemeral runner; add a run-level `degraded_run=true` rollup when any stage fails open.
   - **Expected reliability impact:** fewer silently weakened reviews; better operator visibility without making the workflow brittle.
   - **Rollback/fail-open:** keep current fail-open behavior initially, but count it and alert on sustained rate.
   - **Add logs:** `stage_name`, `fail_open`, `exit_code`, `fail_open_reason`, `degraded_run`.

2. **Differentiate model empty-output, rate-limit, and stall paths**  
   - **Failure evidence:** in `29152499040`, moonshot produced empty output, was classified `retryable (rate_limit)`, then hit a long heartbeat-only stall before succeeding on attempt 2.
   - **Root cause category:** external model variability + coarse retry taxonomy.
   - **Exact fix:** track empty output separately from explicit rate-limit responses; cap wall-clock retries; continue with remaining reviewers after timeout.
   - **Expected reliability impact:** lower long-tail stall rate and clearer root-cause data.
   - **Rollback/fail-open:** fail open to the remaining successful reviewers rather than failing the whole run.
   - **Add logs:** `empty_output_count`, `rate_limit_retry_count`, `stall_count`, `timeout_exit`, `partial_quorum_used`.

3. **Turn skip-heavy orchestration into an explicit health signal**  
   - **Failure evidence:** `63` child runs were skipped/no-op; recent clusters at `13:37:14Z` and `13:20:51Z` show four child workflows created only to skip.
   - **Root cause category:** orchestration fanout without upstream eligibility gating.
   - **Exact fix:** emit structured skip reasons in both parent and child; suppress dispatch when the skip is already knowable.
   - **Expected reliability impact:** easier to distinguish healthy no-op behavior from a stuck pipeline.
   - **Rollback/fail-open:** allow manual child dispatch overrides.
   - **Add logs:** `skip_reason`, `parent_run_id`, `dispatch_decision`, `dispatch_suppressed`.

4. **Close “perfect health vs not instrumented” blind spots**  
   - **Failure evidence:** `break_glass_count=0`, `context_budget_warn_count=0`, Semble fallbacks `0`, Serena queries/probes `0`. Semble’s zero fallbacks look healthy; Serena’s all-zero footprint is more likely “disabled or uninstrumented” than “confirmed healthy.”
   - **Root cause category:** missing status emission.
   - **Exact fix:** emit one capability/status line per run for Semble, Serena, context-guarding, and memory subsystems.
   - **Expected reliability impact:** better rollout detection, especially for silent disables.
   - **Rollback/fail-open:** none needed.
   - **Add logs:** `feature_name`, `enabled`, `disabled_reason`, `status_source`.

## AI Memory Health

Sampled AI memory telemetry looks **effective but close to budget ceilings**.

- Across **5 sampled `retrieve` operations** (`29151853575`, `29152057415` twice, `29152267029`, `29152499040`), the hit rate was **100%** (`records_selected > 0` every time). Average `estimated_tokens` was about **1127**, versus an average budget of **1320**. `keyword_method` distribution was **`llm` 4**, **`plain` 1**, **`none` 0**.

- Headroom is thin in most sampled retrieves: `1192/1200`, `1195/1200`, `1405/1600`, and `1377/1400` show that retrieval is often very near its cap. That is good recall, but it leaves little space for downstream context growth.

- No sampled `retrieve` miss, `enabled:false`, or high push-retry count was observed. However, implement run `29152267029` did log `AI_MEMORY_TELEMETRY` fail-open operations later: `force-tick-get` and `force-tick-put` both reported `ok=false, fail_open=true`.

- Sampled deep dives did **not** show `promote`, `compact`, or `finalize-task` telemetry, so emission appears partial.

**Recommended logging additions**
- `records_considered`, `records_selected`, `deduped_count`
- `estimated_tokens`, `token_budget`, `budget_headroom_tokens`
- `keyword_method`
- `push_retry_count`
- `fail_open`, `fail_open_reason`
- `memory_enabled`, `telemetry_version`

## GH API Call Audit

No repository-specific API hygiene rules were supplied in the analysis window, so this audit is based on workflow evidence and proxies.

1. **Autofix sweep appears to scan often with no dispatch**  
   - **Evidence:** recent `review_autofix` sweep runs `29150787082`, `29149423109`, `29145658569`, `29144102005`, `29142513604`, and `29139020408` all finished in `4–6s` with `candidates=0` and `dispatched=0`. Run `29154346210` saw `candidates=1` but skipped due to an active run.
   - **Likely pattern:** repeated PR listing / active-run checks even when there is nothing to dispatch.
   - **Concrete change:** add per-sweep API counters first; if `0-candidate` sweeps dominate for a week, reduce schedule frequency or add an upstream delta check.
   - **Estimated API reduction:** potentially most sweep-related calls during quiet periods.
   - **Add logs:** `prs_scanned`, `active_runs_checked`, `dispatch_calls`, `gh_api_calls_total`, `gh_api_calls_by_endpoint`.

2. **`orchestrate_poll` likely re-reads issue/check state in loops, but it is not measurable today**  
   - **Evidence:** `orchestrate_poll` ran `38` times; sampled run `29154088259` spent about `60s` in `Process each tracking issue`, but emitted no endpoint/page counters.
   - **Likely pattern:** repeated per-issue or per-check polling without aggregated counters.
   - **Concrete change:** wrap GH calls in counters and page metrics, then batch or cache if per-item loops are confirmed.
   - **Estimated API reduction:** unknown until instrumented; likely material if per-issue loops are present.
   - **Add logs:** `gh_api_calls_total`, `gh_api_pages`, `gh_api_calls_by_endpoint`, `retry_count`, `tracked_items_processed`.

3. **Rate-limit posture looks fine, but confidence is limited by missing counters**  
   - **Evidence:** sampled recent logs reported no deprecation warnings, no secondary rate-limit warnings, and no HTTP 429 signals.
   - **Conclusion:** current rate-limit risk looks low, but this is based on absence of warnings rather than positive call telemetry.
   - **Concrete change:** emit `gh_api_rate_limit_remaining_min`, `secondary_rate_limit_events`, and `retry_backoff_ms`.
   - **Add logs:** `rate_limit_remaining`, `secondary_rate_limit_count`, `retry_backoff_ms`.

## Prompt Cache & Memory System

1. **Prompt-cache behavior is currently opaque**  
   - **Evidence:** repo aggregate shows `cache_hit_rate=null`, `or_cache_read_tokens=0`, `or_cache_write_tokens=0`, and OR token totals all zero despite `26` OR calls.
   - **Interpretation:** prompt cache may be unused, unsupported on this path, or simply unmeasured.
   - **Recommendation:** emit source-side cache metrics and a parse status; do not infer cache performance from the current data.

2. **Large prompts create clear cache-fragmentation risk**  
   - **Evidence:** in `review_autofix` run `29152499040`, `pre_assembled_static.txt` was `189,741` bytes, `editor_prompt_body.txt` was `95,718` bytes, and the final editor prompt was `285,853` bytes. In `copilot_pull_request_reviewer` run `29154836424`, the prompt was `17,498` tokens.
   - **Inference:** unstable prompt prefixes or noisy early dynamic content are likely eroding cache reuse.
   - **Recommendation:** keep stable instructions/rubrics first, move volatile issue/run data later, and log a stable prefix hash.
   - **Add logs:** `prompt_static_bytes`, `prompt_dynamic_bytes`, `prompt_prefix_hash`, `cacheable_prefix_bytes`, `cache_status`.

3. **Context-budget guards are probably under-instrumented**  
   - **Evidence:** no `CONTEXT_BUDGET_WARN` events were recorded repo-wide, yet memory retrieves were near budget ceilings and at least one review prompt was extremely large.
   - **Interpretation:** absence of warnings is not strong evidence of low prompt pressure.
   - **Recommendation:** warn on low remaining headroom during assembly, not only after the fact.
   - **Add logs:** `prompt_budget_tokens`, `prompt_estimated_tokens`, `prompt_headroom_tokens`, `context_budget_warn`.

Semble looks **targeted, not noisy**: `4` total calls, `42,070` bytes, `0` fallbacks. It is not the current cache problem. Serena remains **non-observable**: `0` queries and `0` probes.

## Orchestrator Health

- **Hard-failure health is good.** Repo `failure_count=0`, and sampled recent runs were `attempt=1`, `retries=0`.

- **Concurrency protection is working.** In sampled sweep run `29154346210`, the workflow skipped PR `#3629` because an `active_run` already existed. Keep that guard.

- **Fanout health is noisy.** `63/147` runs were skipped child workflows. That is the clearest orchestrator-level inefficiency in the current window.

- **Poll health is queue-bound.** `orchestrate_poll` outcomes are successful, but the family is paying a consistent ~`188s` median latency tax.

- **State-machine visibility is thin.** No sampled wave/deferral/conflict-heal counters were available, so it is hard to separate “healthy wait,” “healthy skip,” and “stuck.”

**Track these indicators next**
- `skipped_child_rate`
- `poll_queue_ratio = queue_wait_ms / total_ms`
- `active_run_suppression_rate`
- `degraded_run_rate`
- `reviewer_retry_rate`
- `terminal_state_age_ms`
- `conflict_heal_retry_count`

## Pipeline Flow Bottlenecks

| Phase | Bottleneck type | Evidence | Smallest safe fix |
|---|---:|---|---|
| Clarify / Plan / Implement dispatch | Flow/no-op overhead | `63` skipped child runs across `clarify`, `plan`, `implement`, `orchestrate_clarify_respond` | Parent-side eligibility preflight + structured skip reasons |
| Implement (active runs) | Context/token pressure | `implement` holds `1,326,191` recorded Codex tokens; sampled run `29152267029` was near memory budget and used Semble overflow | Per-source prompt sizing logs, then dedupe memory/overflow context |
| Review / Autofix | Compute | Run `29152499040`: reviewer step `4299s`, editor step `373s` | Risk-tiered reviewer fanout, skip/shrink pass 2 on small diffs |
| Review / Autofix | Retry overhead | Moonshot empty-output → retryable/rate-limit → long stall in `29152499040` | Per-model retry cap + partial quorum fallback |
| Orchestrate / Poll | Queueing | `orchestrate_poll` `p50=188s`; repeated runner-wait logs | Queue vs compute timing, then batch one extra poll iteration per runner if confirmed |
| Validation refresh | Opaque single-run latency | `validation_refresh` had one `1090s` run with no sampled stage breakdown | Add stage timers before changing behavior |
| Merge / conflict handling | Not observable | No sampled conflict-heal / merge-retry telemetry | Add `merge_conflict_detected`, `conflict_heal_retry_count`, `rebase_attempt_count` |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `orchestrate_poll` median latency (`188s`) is the clearest systemic time sink.
  - `review_autofix` has an extreme long-tail mode (`29152499040`, `4994s`) driven by six-model/two-pass review and a long retry.
  - Child workflow fanout produces heavy skip noise (`63` skipped/no-op runs).

- **Top failure modes**
  - Silent degraded success in review stages (`consolidator` and `parser` fail-open in `29152499040`).
  - Missing OR/cache/GH API telemetry prevents accurate diagnosis of cost and cache misses.
  - AI memory force-tick fail-open events in implement (`29152267029`) are not surfaced at run-summary level.

- **Highest-cost drivers**
  - Recorded Codex spend is almost entirely in `implement` (`1,326,191` tokens, ~`99.1%` of recorded Codex usage).
  - OR/OpenRouter activity is concentrated in `review_autofix` (`26` calls), but token attribution is missing.
  - Semble usage is small and targeted (`4` calls, `42,070` bytes); it is not the dominant cost source.

- **Top 3 prioritized actions**
  1. **Add first-class diagnostics** for queue wait, child-skip reasons, degraded-success states, OR token/cache fields, and GH API counts; aggregate them next to the existing `cost_audit.py` metrics.
  2. **Gate review fanout** by diff size/risk and add per-model timeout/quorum logic to prevent another `29152499040`-style outlier.
  3. **Fix the consolidator trusted-directory issue** and roll up all fail-open stages into a visible `degraded_run` metric.

## Metrics Appendix

### Repo overview and telemetry coverage

| Source | Total runs | Success | Failure | Other/skipped | p50 s | p95 s | Avg s | Log telemetry runs | Wall-clock samples |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `analysis_context` | 147 | 84 | 0 | 63 | 6.0 | 205.0 | 107.9 | 115 | 110 |
| `summary.json` | 147 | 84 | 0 | 63 | 6.0 | 205.0 | 107.9 | 29 | 28 |

*Coverage gap:* the two sources disagree materially on telemetry coverage (`115` vs `29` parsed runs). Use the broader `analysis_context` for aggregates, but treat the mismatch itself as an observability defect.

### Key workflow-family metrics

| Workflow family | Runs | Success | Other/skipped | p50 s | p95 s | Notable signal |
|---|---:|---:|---:|---:|---:|---|
| `orchestrate_poll` | 38 | 38 | 0 | 188.0 | 205.45 | repeated runner waits; likely queue-bound |
| `review_autofix` | 34 | 34 | 0 | 6.0 | 10.0 | `26` OR calls; wall p99 `4,146,720 ms`; outlier run `29152499040` |
| `implement` | 16 | 1 | 15 | 1.0 | 162.75 | `1,326,191` recorded Codex tokens; Semble overflow sampled |
| `plan` | 16 | 1 | 15 | 1.0 | 128.0 | mostly skipped |
| `clarify` | 18 | 1 | 17 | 1.0 | 25.65 | mostly skipped |
| `orchestrate_clarify_respond` | 16 | 0 | 16 | 1.0 | 1.0 | entirely skipped/no-op |
| `copilot_pull_request_reviewer` | 2 | 2 | 0 | 173.5 | 192.85 | `17,498`-token prompt in run `29154836424` |
| `validation_refresh` | 1 | 1 | 0 | 1090.0 | 1090.0 | single long opaque run |

### LLM, cache, and MCP telemetry

| Metric | Value | Notes |
|---|---:|---|
| Codex calls | 23 | repo aggregate |
| Codex tokens | 1,338,349 | `implement` is ~`99.1%` of recorded Codex spend |
| OR/OpenRouter calls | 26 | activity concentrated in `review_autofix` |
| OR prompt/completion/total tokens | 0 / 0 / 0 | visibility gap, not believable zero usage |
| OR cache write/read tokens | 0 / 0 | cache behavior unmeasured |
| `cache_hit_rate` | `null` | repo aggregate |
| `break_glass_count` | 0 | no sampled `BREAK_GLASS` lines |
| `context_budget_warn_count` | 0 | no sampled `CONTEXT_BUDGET_WARN` lines |
| Semble | 4 calls, 42,070 bytes, 0 fallbacks | low-volume, targeted usage |
| Serena | 0 queries, 0 fallbacks, 0 probes | likely disabled or uninstrumented |
| AI-call wall clock | p50 `1,000 ms`, p99 `1,049,950 ms` | `110` samples in `analysis_context` |

### Sampled AI memory telemetry

| Run ID | Workflow | Records selected | Estimated tokens / budget | `keyword_method` | Notes |
|---|---|---:|---:|---|---|
| `29151853575` | `orchestrate` | 26 | `1192 / 1200` | `llm` | near budget ceiling |
| `29152057415` | `plan` | 26 | `1195 / 1200` | `llm` | near budget ceiling |
| `29152057415` | `plan` | 10 | `467 / 1200` | `llm` | second retrieve, comfortable headroom |
| `29152267029` | `implement` | 31 | `1405 / 1600` | `plain` | later `force-tick-get/put` fail-open |
| `29152499040` | `review_autofix` | 30 | `1377 / 1400` | `llm` | near budget ceiling |

*Sample summary:* `5/5` sampled retrieves returned records; average estimated tokens ≈ `1127`; average budget ≈ `1320`; `llm` used in `4/5` retrieves.

### GH API summary (proxy-based)

| Workflow / evidence | Direct API counts emitted? | Proxy signal | Rate-limit / retry signal |
|---|---|---|---|
| `review_autofix` recent sweeps (`29150787082`, `29149423109`, `29145658569`) | No | `candidates=0`, `dispatched=0` | none observed |
| `review_autofix` sweep `29154346210` | No | candidate skipped because an active run already existed | healthy concurrency suppression |
| `orchestrate_poll` `29154088259` | No | `Process each tracking issue` took ~`60s` | no 429 data |
| recent sampled runs overall | No repo-wide counters | no secondary rate-limit warnings in summaries | none observed |

### MCP availability rows

| Target | `probe_ok` | `probe_failed` | `probe_skipped` | Queries | Fallbacks | Notes |
|---|---:|---:|---:|---:|---:|---|
| Serena (all targets) | 0 | 0 | 0 | 0 | 0 | no per-target `SERENA_*` telemetry emitted |

*Other MCP servers observed via `*_QUERY` / `*_FALLBACK` / `*_PROBE`: none in sampled telemetry.*

### Highest-value new aggregate fields to add

Add these to the same aggregation path that already carries `codex_*`, `or_*`, Semble/Serena, `break_glass_count`, `context_budget_warn_count`, and wall-clock metrics:

- `runner_queue_ms`, `runner_assignment_ms`, `job_setup_ms`
- `gh_api_calls_total`, `gh_api_calls_by_endpoint`, `gh_api_pages`, `gh_api_retry_count`, `gh_api_secondary_rate_limit_events`
- `reviewer_retry_count`, `reviewer_empty_output_count`, `reviewer_stall_count`, `partial_quorum_count`
- `stage_failopen_count`, `degraded_run_count`, `consolidator_failopen_count`, `parser_no_issue_markers_count`
- `child_dispatch_count`, `child_skip_count`, `child_skip_reason`
- `prompt_static_bytes`, `prompt_dynamic_bytes`, `prompt_prefix_hash`, `prompt_headroom_tokens`
- `memory_deduped_count`, `memory_budget_headroom_tokens`, `memory_fail_open_count`
