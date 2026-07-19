## Executive Summary

- **High impact, high confidence:** `orchestrate_poll` is mostly queue/setup overhead, not issue-processing time. The family ran **50 times** with **p50 235s / p95 367.4s**; sampled run **29678792218** spent only about **32s** actually processing two already-complete tracking issues (`#3627`, `#3601`) inside a **234s** run. Moving work detection ahead of heavy setup and logging `queue_ms/setup_ms/work_ms` should save roughly **30–180s per idle poll cycle**.
- **High impact, high confidence:** `review_autofix` is the dominant long-tail latency and AI-cost driver. The family ran **56 times** with **avg 873.7s / p95 4292.75s**; slow runs **29640164275 (7776s)** and **29645452933 (6947s)** alone account for the retained sample’s entire `review_autofix` Codex usage (**12,156 tokens**, **6 calls**) and show dual runner allocation plus multi-minute bootstrap before first review work. Adding per-phase and per-iteration summaries is the fastest safe way to cut minutes off active PRs.
- **High impact, high confidence:** OpenRouter cost control is currently blind. The repo aggregate shows **99 `or_calls`** but **0** `or_prompt_tokens`, `or_completion_tokens`, `or_total_tokens`, `or_cache_write_tokens`, and `or_cache_read_tokens`, with `cache_hit_rate=null`, even though runs log `OPENROUTER_PROMPT_CACHE_DISABLED=false` (for example, validate run **29639211829**). The collector already expects these fields in `scripts/cost_audit.py`; the workflows are not emitting enough source telemetry.
- **High impact, high confidence:** The only hard failure in the window was a real application-side validation failure, not harness breakage. Run **29639211829** failed in `validate / validate -> Enforce validation outcome` with `VALIDATION_STATUS: fail` and `VALIDATION_RAW_STATUS: needs_fixes`; Serena failed open twice with `SERENA_FALLBACK ... reason=disabled`, and Semble ran once with `bytes=4273`. Add a structured validation failure summary so operators can distinguish “needs fixes” from infra/harness problems immediately.
- **Medium impact, high confidence:** Drift audit is producing false reassurance when source logs are missing. Run **29672861229** emitted **6** `log fetch failed: log not found` warnings, then still ended with `drift-audit: no drift markers found in recent runs.` A coverage summary line and soft-degraded result would reduce false-green audits with almost no risk.
- **Medium impact, medium confidence:** AI memory retrieval is working well but running near its token ceiling. Across the sampled deep-dive logs, **9/9** `AI_MEMORY_TELEMETRY` retrieve ops selected records, averaging **1,383 estimated tokens** against a logged **1,400 token_budget**, all with `keyword_method=llm`. Keep it on, but add latency/retry fields and a compact per-run memory summary.

## Speed Optimizations

1. **Move no-work detection ahead of heavy setup in `orchestrate_poll` and no-op review sweeps**  
   - **Evidence:** `orchestrate_poll` has **p50 235s** across **50** runs. In run **29678792218**, `Find active tracking issues` reported **2 active tracking issues** at `07:51:02`, but actual per-issue processing only ran from `07:53:21` to `07:53:53`, and both issues were already complete. In no-op sweep run **29676978688** (**50s** total), `AUTOFIX_SWEEP_START ... candidates=0` and `AUTOFIX_SWEEP_END ... candidates=0` landed in the same second (`06:47:00`), so the useful work was sub-second.  
   - **Root cause:** work discovery happens after runner allocation and setup steps.  
   - **Exact change:** in `.github/workflows/orchestrate_poll.yml`, move `Find active tracking issues` before Codex/Semble/support-source bootstrap; in `review_autofix_sweep.yml`, keep the existing PR enumeration first and add an immediate exit path before any optional setup.  
   - **Diagnostic logging to add:** `POLL_PREFLIGHT_SUMMARY run_id=<id> active_issues=<n> queue_ms=<q> bootstrap_ms=<b> work_ms=<w> cleanup_ms=<c>` and `AUTOFIX_SWEEP_SUMMARY candidates=<n> queue_ms=<q> api_ms=<a> dispatch_ms=<d>`.  
   - **Estimated savings:** **30–180s per idle run**.  
   - **Risk:** **low**.

2. **Reduce `review_autofix` dual-runner and bootstrap tax on the hot path**  
   - **Evidence:** `review_autofix` ran **56** times with **avg 873.7s / p95 4292.75s**. In run **29640164275**, both `review / gate/system` and `review / codex-agent/system` waited on separate hosted runners, and the first sampled memory retrieve did not occur until `10:05:33`, about **3m22s** after the `codex-agent` job started. Run **29645452933** shows the same split job shape and still took **6947s**.  
   - **Root cause:** the gate and worker jobs each pay runner/setup overhead; heavy installs/builds happen before first model work.  
   - **Exact change:** keep `gate` minimal, but push `free-disk-space`, support checkout, `setup-uv`, `Install Semble`, and `Build Semble index` as late as possible behind `needs.gate.outputs.should_run == 'true'`; only merge the jobs later if phase timing proves it is worth the complexity.  
   - **Diagnostic logging to add:** `AUTOFIX_PHASE_TIMING pr=<n> gate_queue_ms=<...> agent_queue_ms=<...> bootstrap_ms=<...> reviewer_ms=<...> editor_ms=<...> commit_ms=<...>`.  
   - **Estimated savings:** **1–4 minutes per active review run**.  
   - **Risk:** **low** for logging/reordering, **medium** if jobs are merged.

3. **Instrument review iterations so runaway runs can be trimmed safely**  
   - **Evidence:** long-tail review runs dominate the workflow family: **29640164275 (7776s)**, **29645452933 (6947s)**, **29649065639 (4472s)**, **29655837254 (3679s)**, **29651942407 (3666s)**, **29653909141 (3612s)**. The terminal state of **29645452933** is visible (`REVIEWERS_SUCCESSFUL: 6`, `DID_COMMIT: true`, `EDITOR_SUMMARY_POSTED: true`, `MERGE_CONFLICT: false`), but the logs do not emit compact per-iteration timing or reason codes.  
   - **Root cause:** iteration cost is hidden until the end of the run.  
   - **Exact change:** emit an `AUTOFIX_ITERATION_STATE` line at each reviewer/editor loop boundary with iteration number, reviewer count, changed-files count, did-commit flag, and next-action reason.  
   - **Estimated savings:** indirect now, but likely the main prerequisite for cutting the **>1 hour** tail safely.  
   - **Risk:** **low**.

4. **Trim cleanup noise and small post-job overhead**  
   - **Evidence:** run **29639211829** ended with `git-submodule cannot be used without a working tree`; recent poll and drift logs also show repeated post-job `git config` / `git submodule foreach` cleanup.  
   - **Root cause:** generic cleanup runs even when a worktree is absent or irrelevant.  
   - **Exact change:** guard submodule cleanup behind a worktree check.  
   - **Diagnostic logging to add:** `POST_JOB_CLEANUP_SUMMARY cleanup_ms=<...> skipped_submodule_cleanup=<true|false> reason=<...>`.  
   - **Estimated savings:** **1–10s per run**, plus less warning noise.  
   - **Risk:** **low**.

## Cost Optimizations

1. **Fix OpenRouter usage and cache telemetry before changing models**  
   - **Evidence:** repo aggregate: **99 `or_calls`**, but **0** `or_prompt_tokens`, `or_completion_tokens`, `or_total_tokens`, `or_cache_write_tokens`, `or_cache_read_tokens`; `cache_hit_rate=null`. `scripts/cost_audit.py` already expects these fields, but the current inputs stay zero/null.  
   - **Root cause:** source workflows are not emitting provider/cache details that the collector can parse.  
   - **Exact change:** emit `LLM_CALL_SUMMARY provider=openrouter model=<...> phase=<...> prompt_tokens=<...> completion_tokens=<...> total_tokens=<...> cache_write_tokens=<...> cache_read_tokens=<...> latency_ms=<...>`.  
   - **Estimated savings:** not yet quantifiable, but this unlocks control over **99 currently unmetered calls**.  
   - **Quality risk:** **none**; this is observability only.

2. **Focus `review_autofix` first; it consumes nearly all visible AI spend**  
   - **Evidence:** `review_autofix` accounts for **12,156 / 14,182 Codex tokens (86%)**, **6 / 7 Codex calls**, **10 / 11 Semble queries**, **124,878 / 129,151 Semble request bytes**, and all **99 OpenRouter calls** in the retained run bundle.  
   - **Root cause:** repeated reviewer/editor passes on the same PR lineage, plus expensive setup before useful work.  
   - **Exact change:** add per-iteration token/context summaries, then use them to down-tier unchanged follow-up passes (for example, fewer reviewers when the diff is tiny and prior iteration already converged).  
   - **Diagnostic logging to add:** `AUTOFIX_COST_SUMMARY pr=<n> iteration=<i> codex_tokens=<...> or_calls=<...> semble_bytes=<...> files_changed=<...> reviewer_count=<...>`.  
   - **Estimated savings:** likely **the largest cost lever in the repo**; even a 25–50% reduction in repeated review passes would dominate visible savings.  
   - **Quality risk:** **medium** if reviewer fan-out is reduced before telemetry exists; **low** for logging.

3. **Measure prompt-cache effectiveness explicitly**  
   - **Evidence:** `cache_hit_rate` is null across the aggregate, and `or_cache_*` totals are zero, while sampled runs log `OPENROUTER_PROMPT_CACHE_DISABLED=false`.  
   - **Root cause:** cache use may be working, missing, or fragmented by unstable prompt prefixes; current telemetry cannot distinguish them.  
   - **Exact change:** emit `PROMPT_CACHE_RESULT provider=openrouter phase=<...> prefix_hash=<...> dynamic_hash=<...> hit=<true|false> write_tokens=<...> read_tokens=<...>`.  
   - **Estimated savings:** potentially large on repeated review/orchestrator prompts; currently impossible to prove.  
   - **Quality risk:** **none**.

4. **Keep Semble, but prove whether it is actually shrinking downstream context**  
   - **Evidence:** Semble usage is modest: **11 queries / 129,151 request bytes** aggregate; validate run **29639211829** logged `SEMBLE_QUERY target=validate-diagnose-context chunks=3 bytes=4273 ms=697`; slow review run **29645452933** logged one Semble-backed query and **11,302** bytes via cost telemetry. No live runtime Semble fallback was observed in inspected logs; the aggregate’s **5** fallbacks are flagged as contract-test only.  
   - **Root cause:** only request bytes are visible; there is no response-byte or “prompt bytes avoided” accounting.  
   - **Exact change:** emit `SEMBLE_QUERY_RESULT target=<...> request_bytes=<...> response_bytes=<...> selected_chunks=<...> selected_bytes=<...> latency_ms=<...>`.  
   - **Estimated savings:** probably **modest but real**; enough to justify keeping it if it suppresses larger prompt expansion.  
   - **Quality risk:** **low**.

5. **Do not count on Serena for savings until it actually serves queries**  
   - **Evidence:** aggregate `serena_query_calls=0`, `serena_fallbacks=2`; run **29639211829** logged `SERENA_FALLBACK target=validate phase=diagnose reason=disabled` twice.  
   - **Root cause:** Serena is disabled, so it is not replacing any downstream tool/model work.  
   - **Exact change:** emit one `SERENA_STATUS enabled=<bool> available=<bool> reason=<...>` per run, and when enabled add `SERENA_QUERY target=<...> response_bytes=<...> tool_calls=<...> ms=<...>`.  
   - **Estimated savings:** **0 today**; this is a rollout-readiness fix.  
   - **Quality risk:** **none**.

## Reliability Improvements

1. **Surface validation failures as structured outcomes, not just exit codes**  
   - **Failure evidence:** run **29639211829** failed at `validate / validate -> Enforce validation outcome`; terminal log shows `VALIDATION_STATUS: fail`, `VALIDATION_RAW_STATUS: needs_fixes`, then `Validation did not pass.` and exit code `1`.  
   - **Root cause category:** application-side failure with weak triage summary.  
   - **Exact fix:** in `validate.yml` / `scripts/self_heal_validation.sh`, emit `VALIDATION_FAILURE_SUMMARY tracking_issue=<...> pr=<...> raw_status=needs_fixes failing_check=<...> self_heal_attempts=<...> semble_bytes=4273 serena_mode=disabled`.  
   - **Expected impact:** faster diagnosis, fewer blind reruns.  
   - **Rollback / fail-open:** logging only; no behavior change.

2. **Make drift-audit coverage loss explicit**  
   - **Failure evidence:** run **29672861229** emitted **6** `drift-audit: run #... log fetch failed: log not found ...` warnings for runs `29633797626`, `29637383098`, `29643850391`, `29645460699`, `29653901976`, and `29655844764`, then ended with `drift-audit: no drift markers found in recent runs.`  
   - **Root cause category:** observability gap masked as success.  
   - **Exact fix:** emit `DRIFT_AUDIT_COVERAGE scanned=<n> fetched=<n> missing=<n> missing_run_ids=<csv>` and downgrade the conclusion to “partial coverage” when missing logs > 0.  
   - **Expected impact:** lower false-green rate for drift audits.  
   - **Rollback / fail-open:** keep the job green if desired; only the summary semantics change.

3. **Cancel superseded CI work earlier and log why**  
   - **Failure evidence:** the `ci` family had **5 total runs**, **4 cancelled**, and **p50 duration 1816s**. A sampled `log_summary` for run **29658602527** indicates the run was cancelled after about **1807s**, with `lint` dominating while tests had already passed.  
   - **Root cause category:** superseded-run waste / missing cancellation observability.  
   - **Exact fix:** add workflow-level concurrency with `cancel-in-progress`, and emit `CI_CANCEL_REASON superseded_by=<run_id> ref=<branch> elapsed_ms=<...> current_step=<...>`.  
   - **Expected impact:** lower rerun waste and less runner starvation.  
   - **Rollback / fail-open:** concurrency can be disabled if it suppresses legitimate parallelism.

4. **Keep Serena fail-open behavior, but collapse repeated “disabled” noise into one status line**  
   - **Failure evidence:** current window shows **0 Serena queries**, **2 fallbacks**, **0 probes**; validate run **29639211829** also logged `SERENA_AVAILABLE: false`.  
   - **Root cause category:** deliberate disabled rollout, not service failure.  
   - **Exact fix:** emit one `SERENA_STATUS enabled=false available=false reason=disabled target_scope=validate` per run; reserve `SERENA_FALLBACK` for true runtime failure after an attempted query.  
   - **Expected impact:** cleaner logs and clearer rollout health.  
   - **Rollback / fail-open:** preserves current fail-open semantics.

5. **Suppress cleanup warnings that are not actionable**  
   - **Failure evidence:** validate run **29639211829** logged `git-submodule cannot be used without a working tree` during post-job cleanup.  
   - **Root cause category:** cleanup guard mismatch.  
   - **Exact fix:** worktree presence check before submodule cleanup; log `CLEANUP_NOTE reason=no_worktree`.  
   - **Expected impact:** fewer false warnings masking real ones.  
   - **Rollback / fail-open:** low-risk guard.

**Current-window pressure signals:** no `BREAK_GLASS` events and no parsed `CONTEXT_BUDGET_WARN` events were observed, so the reliability issues here are primarily observability and run-control gaps, not rubric/policy pressure.

## AI Memory Health

- **Coverage observed:** sampled deep-dive logs contained `AI_MEMORY_TELEMETRY` for `review_autofix` and `validate`; scanned counts from the sampled bundle were `record-run-event: 32`, `record-candidate: 9`, `retrieve: 9`, `write_lessons_learned: 5`, `force-tick-get: 2`, `force-tick-put: 2`.
- **Retrieve hit rate:** **9/9 retrieves selected records** (**100%** hit rate).
- **Budget use:** sampled retrieve lines averaged **1,383 estimated tokens** against a logged **1,400 token_budget**, so retrieval is effective but consistently close to the cap.
- **Keyword method:** **100% `llm`** in sampled retrieves; no `plain` or `none` retrieves were observed.
- **Fail-open / disabled:** no sampled retrieve had `fail_open: true` or `enabled: false`. The only fail-open memory events were shell-side `force-tick-get/put` failures in validate run **29639211829** (`ok=false`, `fail_open=true`), which did not break the run.
- **Notable no-op:** in `review_autofix` run **29645452933**, `write_lessons_learned` logged `count: 0`, suggesting some finalize paths are instrumented but produce no persistent lessons.
- **Recommendation:** keep memory enabled, but add `latency_ms`, `push_attempts`, `selected_record_count`, and `budget_source` to retrieve/finalize telemetry, plus one end-of-run `AI_MEMORY_SUMMARY hits=<...> misses=<...> est_tokens_total=<...> push_fail_open_count=<...>`.

## GH API Call Audit

1. **`scripts/orchestrate_poll_process.sh` is the main GH API hotspot, but exact call volume is invisible**  
   - **Evidence:** the script contains repeated `gh issue list`, `gh pr list`, `gh api pulls/...`, paginated comments/timeline fetches, `gh run list/view`, and `gh workflow run` calls, plus GraphQL batch helpers like `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql`. In run **29678792218**, only two tracking issues were processed, yet the process step still ran long enough to justify per-endpoint timing.  
   - **Redundancy pattern:** per-issue lookups mixed with list/sweep paths; exact duplicates cannot be quantified because calls are not logged structurally.  
   - **Concrete change:** wrap `gh_retry` / `gh_retry_to_file` in `scripts/gh_helpers.sh` to emit `GH_API_CALL workflow=orchestrate_poll endpoint=<...> method=<...> attempt=<...> ms=<...> paginated=<bool>`.  
   - **Estimated reduction:** likely **20–40%** once duplicate call paths are visible and batched consistently.  
   - **Rate-limit risk reduction:** high.

2. **`review_autofix` gate/worker paths re-fetch PR metadata and file lists**  
   - **Evidence:** `.github/workflows/review_autofix.yml` contains multiple direct `gh api` calls for PR metadata, paginated `/pulls/<n>/files` fetches, GraphQL issue linkage, comment/label mutations, and workflow dispatches. The same PR’s title/body/files appear to be fetched in multiple branches of the workflow.  
   - **Redundancy pattern:** repeated lookups on one PR inside both gate and codex-agent logic.  
   - **Concrete change:** materialize `PR_CONTEXT_JSON` and `PR_FILES_JSON` once in the gate path, persist them to the runtime dir or outputs, and log reuse via `GH_API_CACHE_HIT key=pr:<n>:files`.  
   - **Estimated reduction:** about **5–15 GH calls per active review run**.  
   - **Rate-limit risk reduction:** medium.

3. **Drift audit retries missing logs without a coverage summary**  
   - **Evidence:** run **29672861229** burned six log-fetch attempts before concluding no drift markers were found.  
   - **Concrete change:** emit `GH_LOG_FETCH run_id=<...> result=<ok|not_found|auth|parse> ms=<...>` and an aggregate `DRIFT_AUDIT_COVERAGE`.  
   - **Estimated reduction:** small in raw call count, high in operator clarity.

4. **No rate-limit events were observed in the inspected logs**  
   - **Evidence:** sampled recent runs and the failed validate run showed no explicit GitHub API rate-limit or backoff incidents.  
   - **Implication:** API hygiene problems are more about redundant calls and opaque retries than about current hard throttling.

## Prompt Cache & Memory System

- **Prompt cache is effectively unobservable right now.** Aggregate telemetry shows `cache_hit_rate=null`, `or_cache_write_tokens=0`, and `or_cache_read_tokens=0`, while sampled runs still log `OPENROUTER_PROMPT_CACHE_DISABLED=false`.  
- **Collector support already exists.** `scripts/cost_audit.py` already aggregates `or_*`, `cache_hit_rate`, and wall-clock fields, so the missing piece is source emission, not report plumbing.
- **Workspace cache is also under-explained.** Validate run **29639211829** logged `WORKSPACE_CACHE_RESTORE_STATE: disabled`, `WORKSPACE_REUSE_ENABLED: false`, and empty workspace cache keys; there is no compact summary line that explains whether that was expected.  
- **Memory retrieval itself looks healthy.** The sampled memory retrieve hit rate was **100%**, with consistent near-budget usage.  
- **No current context-pressure signal:** this window had **0** parsed `CONTEXT_BUDGET_WARN` and **0** `BREAK_GLASS` events, so cache value is being eroded more by missing instrumentation than by visible prompt blow-ups.

**Recommended additions**
- `PROMPT_CACHE_RESULT provider=<...> phase=<...> prefix_hash=<...> dynamic_hash=<...> hit=<bool> write_tokens=<...> read_tokens=<...>`
- `WORKSPACE_CACHE_RESULT phase=<...> restore_state=<disabled|miss|hit> matched_key=<...> created_now=<bool>`
- `MEMORY_SUMMARY role=<...> records_selected=<...> est_tokens=<...> token_budget=<...> latency_ms=<...>`

## Orchestrator Health

- **Healthy but under-instrumented poll loop:** `orchestrate_poll` had **50/50 successful runs**, and sampled run **29678792218** correctly skipped already-complete projects, ran a close-merged sweep, a conflict sweep, and a noop-suspicious recovery sweep. What is missing is a compact per-run summary of processed/skipped issues and sweep outcomes.
- **Dispatch-style phases are fast, not the bottleneck:** `clarify` had **p95 11s**, `implement` had **p95 10s**. The main delay is later in the lifecycle: review/autofix, validate, and poll orchestration.
- **Status accounting is hard to interpret for dispatch families:** `clarify` shows **100 total runs** but only **1 success** and **99 other**; `implement` shows **99 total**, **1 success**, **98 other**. **Inference:** these are likely dispatch/bridge runs whose semantics are fine, but current status reporting obscures real progression.
- **No rollout panic signals:** no `BREAK_GLASS`, no runtime Semble fallback, and Serena appears deliberately disabled rather than broken.
- **Recommended additions:**  
  - `ORCH_PHASE_TRANSITION issue=<n> from=<state> to=<state> reason=<...>`  
  - `POLL_RUN_SUMMARY active_issues=<...> processed=<...> skipped_complete=<...> stall_recoveries=<...> conflicts_fixed=<...> noop_recoveries=<...>`  
  - `ORCH_STATUS_ACCOUNTING workflow_family=<...> terminal_semantics=<dispatch|worker|poll|finalizer>`

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Fix ordered by impact |
|---|---|---|---|
| Clarify / Implement | Not currently the bottleneck | `clarify` p95 **11s**, `implement` p95 **10s** | No behavior change needed; improve status semantics only |
| Review / Autofix | Long-tail compute + setup | `review_autofix` avg **873.7s**, p95 **4292.75s**; runs **29640164275** and **29645452933** took **7776s** and **6947s** | Add per-phase/per-iteration timing, then trim bootstrap and down-tier repeated passes |
| Validate | Failure diagnosis opacity | run **29639211829** failed after **906s** with `needs_fixes`, but no compact failing-check summary | Emit structured validation-failure summaries |
| Orchestrate Poll | Queue/setup overhead | `orchestrate_poll` p50 **235s** across **50** runs; sampled issue work in **29678792218** was short | Move preflight earlier and log queue/setup/work split |
| Drift Audit | Retry on missing source logs | **29672861229** had **6** missing-log warnings then a “clean” result | Emit coverage summary and partial-result status |
| Validation Refresh | Budget exhaustion late in run | **29671811752** took **1572s**; 3 repos skipped once remaining budget fell below threshold | Log per-repo elapsed/budget and reorder work by runtime/value |
| CI | Cancelled compute waste | `ci` had **4/5 cancelled**, p50 **1816s** | Add supersession telemetry and cancel earlier |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` long tail: **56 runs**, **avg 873.7s**, **p95 4292.75s**.
  - `orchestrate_poll` idle overhead: **50 runs**, **p50 235s** despite light sampled work.
  - `validation_refresh` is infrequent but very long: run **29671811752** took **1572s**.

- **Top failure modes**
  - Real validation failure with weak summary: **29639211829** (`needs_fixes`).
  - Partial-observability drift audit: **29672861229** missing **6** logs.
  - Expensive cancellations in `ci`: **4/5** runs cancelled; sampled summary indicates late cancellation after useful work.

- **Highest-cost drivers**
  - `review_autofix`: **12,156 Codex tokens**, **6 Codex calls**, **99 OpenRouter calls**, **10 Semble queries / 124,878 bytes**.
  - `validate`: **2,026 Codex tokens**, **1 Semble query / 4,273 bytes**, **2 Serena fallbacks**.
  - OpenRouter/cache metrics remain zero/null, so actual dollar distribution is under-measured.

- **Top 3 prioritized actions**
  1. Add structured `queue/bootstrap/work/cleanup` timing and `AUTOFIX_ITERATION_STATE` logs across review/poll/sweep paths.
  2. Emit `LLM_CALL_SUMMARY`, `PROMPT_CACHE_RESULT`, and `GH_API_CALL` so cost and API hotspots become measurable.
  3. Add `VALIDATION_FAILURE_SUMMARY` and `DRIFT_AUDIT_COVERAGE` so operators can distinguish real code failures from missing evidence.

## Metrics Appendix

### Run coverage

| Scope | Total runs | Success | Failure | Cancelled | Other | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 542 | 138 | 1 | 9 | 394 | 144.85 | 2.0 | 304.0 |

### Key workflow families

| Workflow family | Runs | Success | Failure | Cancelled | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| `review_autofix` | 56 | 51 | 0 | 5 | 873.73 | 9.0 | 4292.75 |
| `orchestrate_poll` | 50 | 50 | 0 | 0 | 251.16 | 235.0 | 367.4 |
| `validate` | 2 | 1 | 1 | 0 | 632.5 | 632.5 | 878.65 |
| `ci` | 5 | 1 | 0 | 4 | 1758.0 | 1816.0 | 1818.6 |
| `validation_refresh` | 1 | 1 | 0 | 0 | 1572.0 | 1572.0 | 1572.0 |
| `clarify` | 100 | 1 | 0 | 0 | 4.59 | 1.0 | 11.0 |
| `implement` | 99 | 1 | 0 | 0 | 11.52 | 1.0 | 10.0 |

### Cost / telemetry aggregates

| Metric source | Runs with parsed log telemetry | Wall-clock samples | Codex tokens | Codex calls | OR calls | OR tokens | OR cache write/read | Cache hit rate | Semble queries / bytes | Semble fallbacks | Serena queries | Serena fallbacks | Break glass | Context warns |
|---|---:|---:|---:|---:|---:|---:|---|---|---|---|---:|---:|---:|---:|
| `analysis_context` aggregate | 115 | 109 | 14182 | 7 | 99 | 0 | `0 / 0` | `null` | `11 / 129151` | `5` (contract-test only) | 0 | 2 | 0 | 0 |
| retained `summary.json` bundle | 30 | 30 | 14182 | 7 | 99 | 0 | `0 / 0` | `null` | `11 / 129151` | 0 in retained logs | 0 | 2 | 0 | 0 |

**Data gap:** the collector can aggregate `or_*`, `cache_hit_rate`, `semble_targets`, `serena_targets`, and `serena_tools`, but the current workflow emissions do not populate most OpenRouter/cache fields, and the retained run bundle only covers a subset of full-window telemetry.

### Family-level visible AI usage from retained bundle

| Workflow family | Codex tokens | Codex calls | OR calls | Semble queries / bytes | Serena fallbacks |
|---|---:|---:|---:|---|---:|
| `review_autofix` | 12156 | 6 | 99 | `10 / 124878` | 0 |
| `validate` | 2026 | 1 | 0 | `1 / 4273` | 2 |
| `orchestrate_poll` | 0 | 0 | 0 | `0 / 0` | 0 |

### AI memory health

| Metric | Value |
|---|---:|
| Sampled `retrieve` ops | 9 |
| Retrieve hit rate (`records_selected > 0`) | 100% |
| Avg `estimated_tokens` | 1383 |
| Logged `token_budget` | 1400 |
| `keyword_method=llm` | 9 |
| `keyword_method=plain` | 0 |
| `keyword_method=none` | 0 |
| Retrieve `fail_open=true` | 0 |
| Retrieve `enabled=false` | 0 |
| Other observed fail-open memory ops | `force-tick-get`: 2, `force-tick-put`: 2 |

### Top stall / waste signals

| Signal | Evidence |
|---|---|
| Runner/setup dominates idle poll cycles | `orchestrate_poll` p50 **235s**; run **29678792218** did short issue work inside **234s** total |
| No-op review sweep still pays job overhead | run **29676978688** took **50s**; `AUTOFIX_SWEEP_START` and `END` both showed `candidates=0` in the same second |
| Review/autofix long-tail loops | `review_autofix` p95 **4292.75s**; slow runs **29640164275**, **29645452933**, **29649065639** |
| CI supersession waste | `ci` family **4/5 cancelled**, p50 **1816s** |
| Validation refresh budget exhaustion | run **29671811752** skipped `bitsafe.io`, `hylifegroup.com`, `radateeree-resort.com` due remaining time |

### GH API summary

| Workflow / script | Observed hotspot | Exact count available? | Recommended log |
|---|---|---|---|
| `scripts/orchestrate_poll_process.sh` | repeated `gh issue/pr/run/api` + GraphQL batch helpers | No | `GH_API_CALL`, `GH_API_SUMMARY` |
| `.github/workflows/review_autofix.yml` | repeated PR metadata, PR files, issue comment/label, workflow dispatch calls | No | `GH_API_CALL`, `GH_API_CACHE_HIT` |
| `scripts/drift_audit.sh` | repeated log fetch failures on missing runs | Partial only | `GH_LOG_FETCH`, `DRIFT_AUDIT_COVERAGE` |

### MCP availability / usage

| Server | Target | Query calls | Logged bytes | Fallbacks | Probe ok | Probe failed | Probe skipped | Evidence |
|---|---|---:|---:|---:|---:|---:|---:|---|
| Semble | aggregate | 11 | 129151 request bytes | 5 contract-test only (aggregate) | n/a | n/a | n/a | aggregate telemetry |
| Semble | `validate-diagnose-context` | 1 | 4273 request bytes | 0 runtime | n/a | n/a | n/a | run **29639211829** |
| Serena | aggregate | 0 | 0 | 2 | 0 | 0 | 0 | aggregate telemetry |
| Serena | `validate` | 0 | 0 | 2 (`reason=disabled`) | 0 | 0 | 0 | run **29639211829** |

**Other MCP servers observed:** none in the supplied window.
