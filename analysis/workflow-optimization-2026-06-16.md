## Executive Summary

- `implement` is the clear cost hotspot in `shubhodeep1/coding-workflows`: `5,308,818 / 5,318,953` Codex tokens (99.8%) and `56 / 71` Codex calls came from that family, concentrated in two successful runs: `27593429372` (501s, 2,656,436 tokens, 30 calls) and `27592823653` (476s, 2,652,382 tokens, 26 calls). **Estimated impact:** 20-40% lower implementation token spend (~0.5M-1.0M tokens per successful implement run). **Confidence:** medium.

- Repeated AI-memory / processed-command bookkeeping is a major user-visible latency tax. In sampled successes, non-model bookkeeping consumed ~`133.0s` in `implement` run `27593429372` and ~`133.4s` in `plan` run `27593211841`; `orchestrate_poll` runs `27593718320` and `27593082272` also spent ~`46s` just on `Record poll run start/end`. **Estimated impact:** 80-110s saved per successful `plan`/`implement` run and 35-60s per active poll cycle. **Confidence:** high.

- CI’s critical path is dominated by a single test bucket. Run `27593141149` (`ci`, 1750s) spent `1282.7s` in `lint/Orchestrate_poll_process_unit_tests` alone (73.3% of total CI runtime). **Estimated impact:** 300-600s faster CI with test sharding. **Confidence:** high.

- `validation_refresh` is a separate long pole: run `27592186345` took `1615s`, with `1584.1s` in one `Run validation refresh` step processing `13` repos (`7` green, `3` red, `3` skipped). **Estimated impact:** 400-900s faster nightly refresh by chunking/prefiltering work. **Confidence:** medium.

- Reliability is currently good at the workflow level (`136` runs, `0` failures, `0` retries, `0` cancellations), but there are two latent issues: repeated AI-memory `force-tick-get/put` fail-open errors in both successful `implement` runs, and prompt-cache telemetry is effectively blind (`cache_hit_rate=null`, all `or_*` counters `0`). **Estimated impact:** prevents masked follow-up delays and enables future cache savings. **Confidence:** high.

## Speed Optimizations

_Per-step timings below are timestamp-span estimates from exported step logs._

1. **Shard `ci`’s `Orchestrate_poll_process_unit_tests`** *(critical-path win)*  
   - **Evidence:** Run `27593141149` (`ci`, success, 1750s) spent `1282.7s` in `lint/Orchestrate_poll_process_unit_tests`. The next slowest steps were far smaller: `ShellCheck static analysis` `67.5s`, `Integration-ahead-by gate regression tests` `67.4s`, `Orchestrate_poll implementation-failed regression fast-fail` `58.7s`, `Validation bootstrap and family direct-run tests` `54.2s`.  
   - **Root cause:** one monolithic unit-test bucket dominates CI wall clock.  
   - **Exact change:** split the `Orchestrate_poll_process_unit_tests` suite in `.github/workflows/ci.yml` into 2-4 shards (by file glob or `pytest -k` partitions) and run them in parallel.  
   - **Estimated time savings:** `300-600s` per CI run.  
   - **Implementation risk:** low-medium; test orchestration only, but shard balance should be validated.

2. **Trim fixed overhead in `orchestrate_poll`** *(critical-path win for issue progression)*  
   - **Evidence:** `orchestrate_poll` consumed `3809s` across `19` runs (35.4% of all observed runtime). In run `27593718320` (188s), actual `Process each tracking issue` work was `54.9s`; fixed overhead was about `133.1s`, including `Record poll run start` `23.5s`, `Record poll run end` `22.8s`, `Checkout repository` `19.7s`, and `Install Semble` `11.6s`. Run `27593082272` showed the same pattern (`52.9s` issue processing vs `23.2s` start, `22.7s` end, `20.2s` checkout, `10.2s` Semble).  
   - **Root cause:** control-plane work dominates actual polling work.  
   - **Exact change:** in `.github/workflows/orchestrate_poll.yml` / `scripts/orchestrate_poll_process.sh`, batch `poll_started` + `poll_completed` memory writes, keep the current early `Find active tracking issues` gate, and make Semble installation lazy so it only happens when an issue path actually needs retrieval.  
   - **Estimated time savings:** `35-60s` per active poll cycle; roughly `11-19 minutes` across a 19-run window.  
   - **Implementation risk:** low-medium; preserve `always()` end logging and fail-open behavior.

3. **Batch AI-memory bookkeeping in `plan` and `implement`** *(critical-path win)*  
   - **Evidence:**  
     - `implement` run `27593429372` (501s): `Run Codex implementation` `239.6s`, but bookkeeping also cost `Claim /approved command` `28.4s`, `Record implementation run start event` `21.2s`, `Record implementation candidate` `21.0s`, `Record implementation run success event` `20.9s`, `Complete /approved processed command` `20.8s`, `Finalize implementation task lineage as in_progress` `20.7s` — `133.0s` total.  
     - `plan` run `27593211841` (373s): `Run Codex planning` `187.3s`, but `Check and claim /answer command` `28.8s`, `Record planning run completed` `21.7s`, `Record planning candidate (plan posted)` `21.7s`, `Complete /answer command (plan posted)` `21.6s`, `Record planning run start` `21.5s`, `Post implementation plan` `18.1s` — `133.4s` total.  
   - **Root cause:** repeated AI-memory branch sync / write / push round-trips around each phase.  
   - **Exact change:** in `.github/workflows/implement.yml` and `.github/workflows/plan.yml`, replace multiple standalone `memory_processed_command_*`, `memory_record_run_event`, `memory_record_candidate`, and `memory_finalize_task` calls with one transaction-style helper per phase that preserves idempotency and duplicate-claim protection.  
   - **Estimated time savings:** `80-110s` per successful `implement` run and `80-100s` per successful `plan` run.  
   - **Implementation risk:** low-medium; correctness matters, but scope is localized.

4. **Chunk or prefilter `validation_refresh` work** *(critical-path win for nightly maintenance)*  
   - **Evidence:** Run `27592186345` (`validation_refresh`, success) took `1615s`; `Run validation refresh` alone consumed `1584.1s`. The emitted summary processed `13` repos with totals `{green: 7, red: 3, skipped: 3}`, and at least `3` repos were dedup-skipped.  
   - **Root cause:** sequential multi-repo processing in one long runner step.  
   - **Exact change:** in `.github/workflows/validation-refresh.yml` / `scripts/validation_refresh_runner.py`, prefilter dedup-disabled repos before expensive work and split the repo list into 2-4 matrix chunks or resumable batches.  
   - **Estimated time savings:** `400-900s` per refresh run.  
   - **Implementation risk:** medium; cross-repo branch / PR coordination must stay idempotent.

5. **Lazy-install Semble when it is actually used** *(secondary win)*  
   - **Evidence:**  
     - `plan` run `27593211841` spent `13.2s` on `Install Semble` and `8.9s` on `Build Semble index`, while the `plan` family logged `0` Semble query calls.  
     - `implement` run `27593429372` spent `9.5s` + `9.4s`; `orchestrate_poll` run `27593718320` spent `11.6s` on `Install Semble`.  
   - **Root cause:** unconditional setup, even when no retrieval occurs.  
   - **Exact change:** guard Semble install/index behind a cheap “will retrieval be needed?” predicate from prompt assembly / targeted file context.  
   - **Estimated time savings:** `15-22s` per successful `plan` run, `18-19s` per successful `implement` run, and `10-12s` per poll cycle that does not use Semble.  
   - **Implementation risk:** low.

6. **Reduce skipped-wrapper churn only after higher-ROI work** *(micro-optimization; not priority)*  
   - **Evidence:** `77` runs ended as other/skipped, but total skipped runtime was only `239s`: `clarify` `21/52s`, `orchestrate_clarify_respond` `20/67s`, `implement` `18/70s`, `plan` `18/50s`.  
   - **Root cause:** broad `issue_comment` / `issues` triggers with guards inside reusable workflows.  
   - **Exact change:** mirror reusable guards in the wrapper workflows where possible (`internal-clarify.yml`, `internal-orchestrate-clarify-respond.yml`).  
   - **Estimated time savings:** low; `239s` across the whole window.  
   - **Implementation risk:** low.

## Cost Optimizations

1. **Right-size `implement` reasoning effort and enable reuse before changing model families**  
   - **Evidence:** `implement` accounts for `5,308,818 / 5,318,953` total Codex tokens. In both successful `implement` runs (`27593429372`, `27592823653`), the environment logged `MODEL_EDITOR: openai/gpt-5.4`, `MODEL_REASONING_EFFORT: xhigh`, `CODEX_THREAD_REUSE_ENABLED: false`, `WORKSPACE_REUSE_ENABLED: false`, and `OPENROUTER_PROMPT_CACHE_DISABLED: false`. By contrast, `plan` used only `8,108` tokens total and `orchestrate` used `2,027`; they are not the cost problem.  
   - **Root cause:** the most expensive phase uses high reasoning with no thread/workspace reuse.  
   - **Exact change:** keep the model family constant first, but lower `THINKING_LEVEL_IMPLEMENT` one notch for small/medium diffs, and enable `CODEX_THREAD_REUSE_ENABLED` plus `WORKSPACE_REUSE_ENABLED` for the same issue lineage. Run an A/B on this repo before wider rollout.  
   - **Estimated savings:** `20-40%` on successful `implement` runs, or about `0.5M-1.0M` tokens per run.  
   - **Quality-risk notes:** medium. Use a size/complexity gate and preserve current settings on second-attempt/fallback runs.

2. **Tighten Semble retrieval so it reduces prompt expansion instead of adding noisy support context**  
   - **Evidence:** deduped direct step logs from `implement` runs `27593429372` and `27592823653` show `17` unique `SEMBLE_QUERY` events totaling `125,333` bytes, all `target=overflow`. The sampled files were often support-heavy rather than task-local code: `prompts/mode-plan.txt`, `prompts/mode-judge.txt`, `prompts/references/severity-classification.txt`, `tests/test_plan_clarify_blocked_output.py`, `tests/prompt_size_budget.py`, `.github/workflows/ci.yml`, `docs/INVENTORY.md`. At the same time, implementation token burn stayed at ~`2.65M` tokens/run.  
   - **Root cause:** overflow retrieval is broad enough to pull prompt/test/docs context that may not help the edit.  
   - **Exact change:** rank Semble candidates by approved-plan file mentions, touched-path adjacency, and package locality; deprioritize `prompts/`, `tests/`, and `docs/` unless the plan explicitly references them; add a per-turn Semble byte cap and dedupe repeated file fetches.  
   - **Estimated savings:** `62-100KB` fewer retrieved bytes across the two sampled implement runs if 50-80% of current retrieval is trimmed; likely tens of thousands of prompt tokens at most if those bytes are currently inlined (inference).  
   - **Quality-risk notes:** low-medium. Keep an override for test- or prompt-edit tasks.

3. **Fix prompt-cache observability before trying to tune cache hit rate**  
   - **Evidence:** across `114` runs with parsed log telemetry, `cache_hit_rate` is `null`, `or_prompt_tokens=0`, `or_completion_tokens=0`, `or_total_tokens=0`, `or_cache_write_tokens=0`, and `or_cache_read_tokens=0`, even though sampled `plan` and `implement` logs showed `OPENROUTER_PROMPT_CACHE_DISABLED: false`.  
   - **Root cause:** either the cached provider path is bypassed, or cache telemetry is not emitted on the path actually in use.  
   - **Exact change:** instrument the Codex path with explicit cache read/write/hit counters, and if OpenRouter caching is intentionally unused here, remove or rename the env flag so operators stop assuming caching exists.  
   - **Estimated savings:** not quantifiable from the current window; likely `10-20%` token/latency savings only after telemetry is real and reuse is enabled.  
   - **Quality-risk notes:** low.

4. **Lower `validation_refresh` discovery effort only after token telemetry is added**  
   - **Evidence:** `.github/workflows/validation-refresh.yml` runs discovery with `VALIDATION_DISCOVERY_MODEL=openai/gpt-5.4`, `VALIDATION_DISCOVERY_REASONING_EFFORT=xhigh`, and a `VALIDATION_DISCOVERY_BUDGET_SECS` default of `2100`, but the run `27592186345` exported no token telemetry.  
   - **Root cause:** expensive daily discovery defaults without cost visibility.  
   - **Exact change:** keep `xhigh` only for `disagree` / new-bootstrap paths, use `high` or `medium` for already-stable repos, and emit token telemetry from `scripts/validation_refresh_runner.py`.  
   - **Estimated savings:** unquantified until telemetry exists.  
   - **Quality-risk notes:** medium; discovery quality matters for cross-repo bootstrap decisions.

5. **Do not spend time on Serena cost tuning yet**  
   - **Evidence:** aggregate telemetry shows `serena_query_calls=0`, `serena_query_response_bytes=0`, `serena_fallbacks=0`, `serena_probe_ok=0`, `serena_probe_failed=0`, `serena_probe_skipped=0`. Sampled logs also showed `SERENA_ENABLED: false`.  
   - **Conclusion:** Serena is neither replacing downstream tool/model work nor adding response-byte noise in this window.  
   - **Exact change:** none yet; enable it only behind telemetry.  
   - **Estimated savings:** none today.  
   - **Quality-risk notes:** none.

6. **Avoidable reruns are not a current cost driver**  
   - **Evidence:** `0` failures, `0` retries, `0` cancellations in `136` runs.  
   - **Conclusion:** there is no meaningful rerun tax to optimize in this window.

## Reliability Improvements

_No run in this window failed or retried, so these are preventative fixes._

1. **Harden AI-memory `force-tick` writes after implementation PR creation**  
   - **Failure evidence:** both successful `implement` runs (`27593429372`, `27592823653`) emitted repeated `AI_MEMORY_TELEMETRY` entries in step `Force orchestrate poll after implementation PR`: `{"op":"force-tick-get","ok":false,"fail_open":true}` and `{"op":"force-tick-put","ok":false,"fail_open":true}`, twice each per run.  
   - **Root cause category:** fail-open memory state propagation failure.  
   - **Exact fix:** add a bounded retry after `memory_ensure_branch`, surface a dedicated step-summary/metric when `force-tick` still fails, and keep the final behavior fail-open so implementation success is not blocked.  
   - **Expected reliability impact:** reduces the risk of delayed or missed follow-up polling after PR creation.  
   - **Rollback/fail-open:** preserve the current non-blocking path if retries still fail.

2. **Deduplicate line-based telemetry so contract-test fallbacks do not look like runtime regressions**  
   - **Failure evidence:** CI run `27593141149` exported `semble_fallbacks=10` / `semble_contract_test_fallbacks=10`, but the direct step log `lint/Targeted_file_context_contract_tests` contained `5` unique `SEMBLE_FALLBACK` lines, all `context=contract-test` and all caused by an intentionally missing Semble binary.  
   - **Root cause category:** observability / collector overcount, not runtime failure.  
   - **Exact fix:** in `scripts/collect_workflow_logs.py`, dedupe event lines across full-job and per-step exports (or parse only per-step logs for line-based event counters).  
   - **Expected reliability impact:** prevents false fallback-rate alarms and avoids masking a real rollout issue later.  
   - **Rollback/fail-open:** no runtime behavior change.

3. **Surface `validation_refresh` red repos more explicitly**  
   - **Failure evidence:** run `27592186345` succeeded overall, but its emitted summary reported `13` repos processed with totals `{green: 7, red: 3, skipped: 3}`. One logged red example was `shubhodeep1/digital_pa` with `self_test_failed(exit=1): app did not become healthy within 120s (last status=running running=true health=unhealthy)`.  
   - **Root cause category:** downstream repo self-test instability hidden inside an overall-green workflow conclusion.  
   - **Exact fix:** write a dedicated per-repo red summary block to the job summary and group red repos first on the next run; keep the current overall workflow success policy if desired.  
   - **Expected reliability impact:** reduces silent degradation in the maintenance loop.  
   - **Rollback/fail-open:** low risk; observability-first.

4. **Reduce wrapper noise that obscures real state**  
   - **Failure evidence:** `77` runs ended as other/skipped (`56.6%` of all runs), largely from wrapper workflows: `clarify` `21`, `orchestrate_clarify_respond` `20`, `implement` `18`, `plan` `18`.  
   - **Root cause category:** broad event triggers with guards evaluated after workflow start.  
   - **Exact fix:** add wrapper-level `if` guards where possible, especially for `internal-clarify.yml` and `internal-orchestrate-clarify-respond.yml`, to mirror the reusable workflow conditions.  
   - **Expected reliability impact:** lowers operator noise and makes real regressions easier to spot.  
   - **Rollback/fail-open:** low risk.

5. **Interpret current fallback / warning signals as healthy, not broken rollout**  
   - **Evidence:** `break_glass_count=0`, `context_budget_warn_count=0`, `semble_runtime_fallbacks=0`, `serena_fallbacks=0`, `serena_probe_failed=0`. The only observed Semble fallbacks were contract-test-only in CI.  
   - **Conclusion:** there is no evidence of policy-pressure break-glass use, prompt-budget pressure, Semble runtime outage, or Serena probe instability in this window.  
   - **Smallest safe mitigation:** keep fail-open behavior, but separate contract-test fallback metrics from production fallback metrics in dashboards and summaries.

## AI Memory Health

_AI-memory counts below are deduped by unique run/step evidence because the exported artifacts include both full-job and per-step logs._

| Run ID | Workflow | Step | `records_selected` | `estimated_tokens` | `keyword_method` | Result |
|---|---:|---|---:|---:|---|---|
| `27592423103` | `orchestrate` | memory retrieve | 0 | 0 | `llm` | miss |
| `27593211841` | `plan` | `Retrieve memory context` | 0 | 0 | `llm` | miss |
| `27592644066` | `plan` | `Retrieve memory context` | 0 | 0 | `llm` | miss |
| `27593429372` | `implement` | `Retrieve implementation memory context` | 1 | 28 | `plain` | hit |
| `27592823653` | `implement` | `Retrieve implementation memory context` | 1 | 28 | `plain` | hit |

- **Retrieve hit rate:** `40%` (`2/5`).
- **Average `estimated_tokens`:** `11.2`.  
- **Budget comparison:** not possible from current logs; the retrieve telemetry did not emit a retrieval budget field.
- **Keyword-method distribution:** `llm=3`, `plain=2`, `none=0`.
- **Zero-record retrieves:** `3/5`, all in `orchestrate`/`plan`.
- **`fail_open: true` entries:** present on both successful `implement` runs in `Force orchestrate poll after implementation PR` (`force-tick-get` / `force-tick-put` failures, twice each per run).
- **`enabled: false` entries:** none observed.
- **Push retry health:** all sampled write ops that emitted `push_attempts` reported `1`; no high retry counts were observed.

**Assessment:** memory is helping `implement` a little (small, cheap hits) but is ineffective for `plan`/`orchestrate` in the sampled window. The strongest inference is that the planning retrieval path’s `llm` keyword extraction is underperforming relative to the simpler `plain` strategy that hit in both implementation runs.

**Recommendation:** keep the current implementation retrieval path, but test `plain` or hybrid `plain -> llm fallback` retrieval for planning; also emit retrieval budget and miss-reason fields so future analysis can separate “no relevant memory exists” from “retrieval failed to find it.”

## GH API Call Audit

1. **Poller Actions-run lookups are the likeliest high-volume API pattern, but call counts are not directly exported**  
   - **Evidence:** `scripts/orchestrate_poll_process.sh` fetches Actions runs in separate in-progress, queued, and completed paths; `orchestrate_poll` is also the largest runtime family (`3809s`, 19 runs). The repo already uses `gh_retry` and an AI-memory / ETag cache in this path.  
   - **Observed issue:** likely redundant list calls when one cycle could reuse a broader fetched set. This is an inference from code, not a measured API-count export.  
   - **Concrete change:** defer the completed-run fetch until an active tracking issue actually needs completed-history resolution, and reuse one fetched run set across all issues processed in the same cycle.  
   - **Estimated reduction:** up to `1-2` Actions API list calls per active poll cycle on this path (`~33-67%` of that sub-path).  
   - **Rate-limit risk reduction:** medium.  
   - **Keep:** current `gh_retry` and ETag caching; no `429` or secondary-rate-limit warnings were observed.

2. **Copilot PR reviewer is making repeated session-log PUTs with no MCP work performed**  
   - **Evidence:** `log_summary` for reviewer run `27593145262` reported repeated `PUT /agents/sessions/06050656-c6b9-4372-9c2a-49a8f849f5cb/logs` calls and a restricted transcript upload of `56,148` chars; run `27593033457` reported repeated `PUT /agents/sessions/6e3974f4-c8e2-4099-84b2-eb567129fe7f/logs` calls and a `72,468`-char transcript. Both summaries also reported `"github-mcp-server"=connected/invocations=0` and `"playwright"=connected/invocations=0`.  
   - **Observed issue:** chat/log flushing appears chatty relative to actual MCP/tool usage.  
   - **Concrete change:** if the action supports it, reduce session-log flush frequency to step boundaries or end-of-run upload; if not, cap transcript upload verbosity for unchanged/no-comment reviews.  
   - **Estimated reduction:** `30-70%` fewer reviewer-side session-log API calls on those runs.  
   - **Rate-limit risk reduction:** low-medium.

3. **The poller’s issue enumeration is already reasonably batched**  
   - **Evidence:** `.github/workflows/orchestrate_poll.yml` uses one `gh issue list --label "ai:orchestrator-tracking" --state open --limit 20` call in `Find active tracking issues`.  
   - **Assessment:** this is good API hygiene already.  
   - **Concrete change:** none needed now; keep this single batched query.

4. **No direct evidence of GitHub API failure behavior in the sampled window**  
   - **Evidence:** no exported `429` events, no secondary rate-limit warnings, and no retries at the workflow level.  
   - **Assessment:** current API hygiene is adequate; the best next step is better call-count telemetry, not emergency throttling work.

## Prompt Cache & Memory System

1. **Prompt-cache telemetry is effectively absent**  
   - **Evidence:** overall `cache_hit_rate=null`, `or_total_tokens=0`, `or_cache_write_tokens=0`, `or_cache_read_tokens=0` across `114` telemetry-bearing runs, despite `OPENROUTER_PROMPT_CACHE_DISABLED=false` appearing in sampled `plan` and `implement` logs.  
   - **Implication:** you cannot currently tell whether caching is off, bypassed, or just uninstrumented.  
   - **Concrete improvement:** emit cache write/read/hit counters from the actual Codex execution path; if the path never uses OpenRouter caching, rename or remove the flag to avoid false confidence.  
   - **Estimated impact:** observability first; likely prerequisite for any real 10-20% prompt-token savings.

2. **Cache-fragmentation risk is high if caching becomes active without prompt normalization**  
   - **Evidence:** `implement` logs showed `CODEX_THREAD_REUSE_ENABLED: false` and `WORKSPACE_REUSE_ENABLED: false`, while the workflow also assembles large dynamic issue/comment context and may pull support-heavy Semble overflow files.  
   - **Inference:** even if provider-side caching is enabled later, volatile issue/thread content and semantically noisy retrieval will fragment cache keys unless a stable static prefix is preserved.  
   - **Concrete improvement:** keep `Pre-assemble static context (cacheable across runs)` at the front of the prompt, move volatile issue/comment expansions after the stable preamble, and avoid Semble support-file inflation unless explicitly needed.  
   - **Estimated impact:** medium future upside on both latency and tokens; low immediate measurable impact until telemetry exists.

3. **Memory retrieval is currently better than cache, but only for implementation**  
   - **Evidence:** deduped AI-memory retrieve hit rate was `40%`, with both hits in `implement` and all misses in `plan`/`orchestrate`.  
   - **Concrete improvement:** keep implementation retrieval as-is, but tune planning retrieval separately and add miss-reason telemetry.  
   - **Estimated impact:** modest reliability/context quality gain; small token impact.

4. **No prompt-budget pressure is visible in the current sample**  
   - **Evidence:** `CONTEXT_BUDGET_WARN` count is `0`, and no `CONTEXT_BUDGET_WARN:` lines were found in deep-dive logs.  
   - **Assessment:** current prompt growth is not yet tripping the explicit warning threshold, but absent cache telemetry means silent inefficiency is still plausible.

## Orchestrator Health

- **Overall health:** functionally healthy. `orchestrate` run `27592423103` succeeded in `379s`; all `19` `orchestrate_poll` runs succeeded; there were `0` workflow failures and `0` retries in the window.
- **Main operational pain point:** the poller is expensive relative to the work it performs. It runs every `5` minutes via `.github/workflows/internal-orchestrate-poll.yml`, so end-to-end responsiveness is bounded by both the cron interval and the `188-362s` active poll runtimes seen in recent/slow samples.
- **Queueing signal:** recent poller run `27593718320` logged “Job is waiting for a hosted runner to come online,” so there is some hosted-runner queue contribution on top of compute time.
- **Control-plane noise:** `orchestrate_clarify_respond` triggered `20` times and skipped every run (`67s` total). That is cheap, but it obscures true health if teams look only at success/skipped counts.
- **Missing visibility:** no exported counters for wave count, deferrals, conflict-heal retries, or stuck terminal states. I saw no deep-dive evidence of conflict-heal loops or terminal-state deadlocks, but the current telemetry is not rich enough to prove absence.
- **Smallest safe mitigations:**  
  1. reduce poll fixed overhead before considering any higher poll frequency,  
  2. batch AI-memory writes,  
  3. add wave/deferral/conflict-heal counters to existing logs.
- **Observable indicators to track next:**  
  - active tracking issues per poll cycle,  
  - poll fixed-overhead ratio (`start/end + checkout + setup + Semble` / total poll runtime),  
  - time from `/answer` or `/approved` comment to next successful phase run,  
  - `force-tick` fail-open count,  
  - skipped-wrapper ratio,  
  - AI-memory retrieve hit rate.

## Pipeline Flow Bottlenecks

| Stage | Evidence | Dominant overhead | Recommendation |
|---|---|---|---|
| Clarify | `23` runs, `21` skipped; successful runs `27592610213` `109s` and `27593166989` `104s` | mostly gating / runner startup | Low priority; trim wrapper noise later |
| Plan | successful runs `27593211841` `373s`, `27592644066` `340s`; model `154.8-187.3s`, bookkeeping ~`133s` | mixed compute + control plane | Batch memory/processed-command steps first |
| Implement | successful runs `27593429372` `501s`, `27592823653` `476s`; model `214.5-239.6s`, bookkeeping ~`133s`, Semble setup ~`19s` | mixed model cost + control plane | Lower reasoning/reuse cost and batch memory steps |
| Review / Autofix | `18` successes, p50 `13s`; several zero-candidate sweeps (`27591105065`, `27588078015`, `27584795094`) | low-value idle work | Keep as-is unless trigger volume rises |
| Validate / CI | `ci` run `27593141149` `1750s`; `validation_refresh` run `27592186345` `1615s` | compute-bound test/validation work | Shard CI unit tests and chunk validation refresh |
| Orchestrate / Poll | `orchestrate_poll` `3809s` total; fixed overhead ~`133s` in active cycles | control plane + some runner queue | Reduce per-cycle overhead before changing cadence |
| Queueing | hosted-runner wait in `27593718320` | external runner availability | monitor; not primary repo-level fix |
| Retry / rerun | `0` retries, `0` failures | none observed | no action needed |
| Merge / conflict overhead | no direct evidence of conflict-heal retries in sampled logs | none observed | add telemetry before optimizing |

**Ordered by end-to-end impact:**
1. Reduce `orchestrate_poll` fixed overhead.  
2. Batch `plan`/`implement` AI-memory bookkeeping.  
3. Shard CI’s `Orchestrate_poll_process_unit_tests`.  
4. Chunk/prefilter `validation_refresh`.  
5. Treat skipped-wrapper cleanup as cosmetic until the above land.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `orchestrate_poll`: `3809s` total runtime (35.4% share)
  - `ci`: `1750s` (16.2%)
  - `validation_refresh`: `1615s` (15.0%)
  - `implement`: `1047s` (9.7%)
  - `plan`: `763s` (7.1%)

- **Top failure modes**
  - No hard workflow failures in the window.
  - Latent AI-memory `force-tick` fail-open failures after implementation PR creation.
  - `validation_refresh` can hide red downstream repos behind an overall success conclusion.

- **Highest-cost drivers**
  - `implement`: `5,308,818` Codex tokens, `56` calls, `34` Semble query events / `250,666` bytes at collector level.
  - `plan` and `orchestrate` are negligible token consumers by comparison (`8,108` and `2,027` tokens).

- **Top 3 prioritized actions**
  1. Batch AI-memory bookkeeping in `implement`, `plan`, and `orchestrate_poll`.
  2. Lower `implement` reasoning effort selectively and enable thread/workspace reuse for the same issue lineage.
  3. Split the CI `Orchestrate_poll_process_unit_tests` bucket and chunk `validation_refresh`.

## Metrics Appendix

| Repo | Runs | Success | Failure | Cancelled | Other/skipped | Avg s | p50 s | p95 s | Total runtime s | Runs with parsed telemetry | Codex tokens | Codex calls | OR total tokens | OR cache write/read | `cache_hit_rate` | `wall_clock_p50_ms` | `wall_clock_p99_ms` | `break_glass_count` | `context_budget_warn_count` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 136 | 59 | 0 | 0 | 77 | 79.2 | 8.0 | 373.0 | 10,775 | 114 | 5,318,953 | 71 | 0 | `0 / 0` | `null` | 2,000 | 1,503,070 | 0 | 0 |

| Workflow family | Runs | Success | Other/skipped | Total s (% of window) | p50 / p95 s | Codex tokens | Codex calls | Semble queries / bytes | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| `orchestrate_poll` | 19 | 19 | 0 | 3,809 (35.4%) | 153 / 401.2 | 0 | 0 | `0 / 0` | fixed overhead dominates active cycles |
| `ci` | 1 | 1 | 0 | 1,750 (16.2%) | 1750 / 1750 | 0 | 0 | `0 / 0` | one 1,282.7s test bucket dominates |
| `validation_refresh` | 1 | 1 | 0 | 1,615 (15.0%) | 1615 / 1615 | 0* | 0* | `0 / 0` | single 1,584.1s runner step processed 13 repos |
| `implement` | 20 | 2 | 18 | 1,047 (9.7%) | 1 / 477.3 | 5,308,818 | 56 | `34 / 250,666` | 99.8% of total tokens |
| `plan` | 20 | 2 | 18 | 763 (7.1%) | 1 / 341.7 | 8,108 | 12 | `0 / 0` | bookkeeping heavy on successful runs |
| `copilot_pull_request_reviewer` | 3 | 3 | 0 | 463 (4.3%) | 151 / 187 | 0 | 0 | `0 / 0` | repeated session-log PUTs in summaries |
| `orchestrate` | 1 | 1 | 0 | 379 (3.5%) | 379 / 379 | 2,027 | 3 | `0 / 0` | memory retrieve miss in sampled run |
| `review_autofix` | 18 | 18 | 0 | 276 (2.6%) | 13 / 39.6 | 0 | 0 | `0 / 0` | several zero-candidate sweeps |
| `clarify` | 23 | 2 | 21 | 265 (2.5%) | 1 / 94.7 | 0 | 0 | `0 / 0` | mostly guarded skips |

\* `validation_refresh` exported no token telemetry in the sampled run, so `0` here means “not observed,” not “no model work occurred.”

| Telemetry scope | `or_prompt_tokens` | `or_completion_tokens` | `or_total_tokens` | `or_cache_write_tokens` | `or_cache_read_tokens` | `cache_hit_rate` | Note |
|---|---:|---:|---:|---:|---:|---|---|
| Overall exported telemetry | 0 | 0 | 0 | 0 | 0 | `null` | cache path is unobservable or unused in this window |

| MCP / retrieval scope | Semble query calls | Semble bytes | Semble fallbacks | Contract-test fallbacks | Runtime fallbacks | Serena query calls | Serena response bytes | Serena fallbacks | Serena probe ok / failed / skipped | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| Collector aggregate | 34 | 250,666 | 10 | 10 | 0 | 0 | 0 | 0 | `0 / 0 / 0` | likely overcounted by duplicate full-job + per-step log parsing |
| Deduped deep-dive `implement` evidence (`27593429372`, `27592823653`) | 17 unique | 125,333 unique | 0 | 0 | 0 | 0 | 0 | 0 | `0 / 0 / 0` | all `target=overflow` |
| Deduped deep-dive `ci` evidence (`27593141149`) | 0 | 0 | 5 unique | 5 unique | 0 | 0 | 0 | 0 | `0 / 0 / 0` | all `context=contract-test` |

| MCP target | `probe_ok` | `probe_failed` | `probe_skipped` | Query calls | Fallbacks | Per-tool breakdown |
|---|---:|---:|---:|---:|---:|---|
| Serena (aggregate) | 0 | 0 | 0 | 0 | 0 | none emitted |
| Other MCP servers observed via `<NAME>_QUERY/_FALLBACK/_PROBE` | 0 | 0 | 0 | 0 | 0 | none emitted |

| Workflow / run | GH API signal | Evidence | Recommended change |
|---|---|---|---|
| `orchestrate_poll` code path | separate Actions-run fetches for in-progress / queued / completed | `scripts/orchestrate_poll_process.sh` fetch path; no direct exported call counts | defer completed-run fetch until needed; reuse fetched sets across issues |
| `copilot_pull_request_reviewer` `27593145262` | repeated `PUT /agents/sessions/.../logs` | `log_summary`; transcript `56,148` chars; MCP invocations `0` | reduce flush cadence if supported |
| `copilot_pull_request_reviewer` `27593033457` | repeated `PUT /agents/sessions/.../logs` | `log_summary`; transcript `72,468` chars; MCP invocations `0` | reduce flush cadence if supported |
| Overall | rate-limit health | no `429` / secondary-rate-limit warnings observed | keep current `gh_retry` + ETag cache behavior |

**Metric caveats**
- The prompt-supplied analysis context covered `114` runs with parsed telemetry; the local deep-dive export contained full logs for a smaller sampled subset. I used prompt aggregates for totals and deep-dive logs / `log_summary` for run-specific evidence.
- Exported line-based telemetry appears to double-count when both full-job and per-step logs are parsed together. Concrete examples:
  - `implement` run `27593429372`: metadata `22` Semble calls / `176,030` bytes vs direct `Run Codex implementation` step evidence `11` unique calls / `88,015` bytes.
  - `implement` run `27592823653`: metadata `12` / `74,636` vs direct step evidence `6` / `37,318`.
  - `ci` run `27593141149`: metadata `10` Semble fallbacks vs `5` unique contract-test fallback lines in the step log.
- Where step durations are cited, they are derived from exported log timestamps rather than native GitHub step-duration metadata.

## Deep Audit — Workflows & Scripts (2026-06-16)

### Section 1: Bug & Correctness Sweep

#### BUG-001
- **File path:** `.github/workflows/orchestrate.yml:995-1008`
- **Severity:** Medium
- **Category tag:** `bug`
- **Description:** The `Create integration branch` step defines a fallback `_safe_gh_jq` as raw `gh api "$@"` when `scripts/gh_helpers.sh` is unavailable. That loses the repo’s normal stdout-suppression behavior on non-2xx responses. In this step, the fallback output is consumed immediately as `DEFAULT_BRANCH` and `BASE_SHA`, so an error payload can be treated as data and flow into ref encoding / branch creation. The canonical helper in `scripts/gh_helpers.sh:516-545` and the local fallbacks in `.github/workflows/plan.yml:1746-1758` and `.github/workflows/implement.yml:4725-4735` do not have this problem.
- **Recommended fix:** Replace the inline fallback with the temp-file wrapper from `scripts/gh_helpers.sh:532-545`, or fail fast if `scripts/gh_helpers.sh` cannot be sourced instead of redefining an unsafe fallback.

### Section 2: GitHub API Call Redundancy Audit

#### API-001
- **File path:** `.github/workflows/review_autofix.yml:487-499,553-558`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** The deterministic skip gate can fetch `repos/${REPOSITORY}/pulls/${PR_NUMBER}/files` twice in one candidate evaluation: once for doc-only detection and again for the materiality-suppression recheck. **Current call count:** up to 2 calls to the same endpoint in the same execution path. **Proposed call count:** 1. This is a same-step cache-reuse miss because `pr_files_json` already exists and only needs a distinct “not fetched / fetch failed / fetched empty” state.
- **Recommended fix:** Fetch PR files once, persist the result or failure sentinel, and reuse `pr_files_json` for both checks. If you want a shared helper, model it on `scripts/gh_helpers.sh:549-615` (`gh_api_json_to_file`) so the fetch result can be reused safely across both branches.

#### BATCH-001
- **File path:** `.github/workflows/review_autofix.yml:823-831,851-866`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** On the PR-body fallback path, the workflow already has all candidate issue numbers in `issue_nodes_json`, but it then serially calls `gh issue view ... --json labels` once per issue to recover labels. **Current call count (label-hydration subpath):** `N` REST calls for `N` issues. **Proposed call count:** 1 GraphQL batch query for all issue numbers. This is exactly the kind of per-iteration API fanout CLAUDE.md §15 warns about.
- **Recommended fix:** Batch the fallback label lookup with one GraphQL alias query keyed by issue number, following the pattern in `scripts/orchestrate_poll_process.sh:10585-10844` (`_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql`). Keep the PR-body extraction path, but replace the per-issue REST loop with one batched label fetch.

#### API-002
- **File path:** `.github/workflows/test-and-mark-stable.yml:2918-2928`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** The cancel-on-close smoke-test poll loop fetches the same Actions run twice every iteration: once for `.status` and once for `.conclusion`. **Current call count:** 2 calls per poll iteration, or up to 240 calls over the 600-second wait budget. **Proposed call count:** 1 call per iteration, or up to 120 over the same budget.
- **Recommended fix:** Fetch the run once as a small JSON object (for example `{status, conclusion}`) and parse both fields locally. If you want a reusable helper instead of inline parsing, `scripts/gh_helpers.sh:549-615` already provides a safe single-fetch pattern to extend.

#### API-003
- **File path:** `scripts/orchestrate_poll_process.sh:7198-7204,9895-9897,11966-11974`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** The poller repeatedly issues separate `_safe_gh_jq` calls to the same resource just to read adjacent fields: final PR `.state` and `.merged_at`, stalled issue `.title` and `.body`, and reissued issue `.title` and `.body`. **Current call count:** 2 REST calls per resource read at each site. **Proposed call count:** 1 call per site by fetching a small object once and unpacking locally. Because this lives in `orchestrate_poll_process.sh`, the redundant round-trips recur on the repo’s hottest control-plane path.
- **Recommended fix:** Add small single-fetch helpers such as `get_pr_state_json <pr_number>` and `get_issue_text_json <issue_number>`, or reuse the same “structured object once, parse many fields” approach already used by `scripts/orchestrate_poll_process.sh:10585-10708`.

### Section 3: Code Duplication & Modularization Opportunities

#### CONSIST-001
- **File path:** `scripts/label_helpers.sh:120-206; scripts/validate_process.sh:947-1055; scripts/orchestrate_poll_process.sh:2303-2425; scripts/review_rb_judge.sh:566-582,615-647`
- **Severity:** Medium
- **Category tag:** `consistency`
- **Description:** AI phase-label mutation logic is implemented four different ways. `scripts/label_helpers.sh` owns `ensure_label_exists` plus a resilient GET/PUT phase swap; `validate_process.sh` and `orchestrate_poll_process.sh` each reimplement contract-driven label resolution; `review_rb_judge.sh` stages and sources `label_helpers.sh` and then still keeps its own `_resilient_phase_swap`. The copies have already drifted: return semantics differ (`label_helpers.sh` returns `1` on create failure, while `validate_process.sh` / `orchestrate_poll_process.sh` warn and return `0`), and the judge maintains its own phase-label list.
- **Recommended fix:** Move the contract-driven phase application into `scripts/label_helpers.sh` as a shared API, e.g. `set_issue_phase_label <issue_number> <phase_label> [repo] [contract_file]`, plus `resolve_phase_changes <phase_label> [contract_file]`. Then update callers in `validate_process.sh`, `orchestrate_poll_process.sh`, and `review_rb_judge.sh` to source that module instead of carrying local copies.

#### DUP-001
- **File path:** `.github/workflows/mark-stable.yml:1-545; .github/workflows/test-and-mark-stable.yml:1-5254`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** `test-and-mark-stable.yml` embeds almost the entire operational body of `mark-stable.yml`. A local diff shows 494 of `mark-stable.yml`’s 545 lines match the larger workflow, including representative blocks at `mark-stable.yml:57-90` / `test-and-mark-stable.yml:136-169`, `163-227` / `3282-3346`, and `460-505` / `4724-4769`. That means release-path behavior must be maintained in two places. There is also already a manual helper script, `scripts/mark-stable.sh:1-111`, that encapsulates the tag/pointer update path but is not used by either workflow.
- **Recommended fix:** Extract the shared release path into one owner. The smallest change is a new shared module such as `scripts/mark_stable_release.sh <version> <notes_file> <source_branch> [dispatch_consumers=true|false]`, with both workflows calling it. Alternatively, wrap the common path in a reusable workflow and keep `test-and-mark-stable.yml` only for the extra test gates.

#### DEBT-001
- **File path:** `.github/workflows/mark-stable.yml:315-340; .github/workflows/test-and-mark-stable.yml:3430-3455; .github/workflows/ci.yml:592-600; scripts/check_workflow_script_refs.py:1-194`
- **Severity:** Low
- **Category tag:** `tech-debt`
- **Description:** Both stable-release workflows inline the same grep-based “workflow scripts exist” check even though CI already uses the canonical `scripts/check_workflow_script_refs.py`. The inline shell copies only cover explicit `scripts/...` tokens and a local `OPTIONAL_SCRIPTS` list; the Python checker also handles `${SUPPORT_SCRIPTS_DIR}/...`, bare names inside bootstrap loops, optional refs, and extra reference-holder files. This is duplicated logic with weaker coverage than the canonical implementation.
- **Recommended fix:** Replace both inline shell blocks with `PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_workflow_script_refs.py`. If those workflows need custom exclusions, add them to the Python checker so CI and release-time validation stay in lockstep.

### Section 4: Expression Size Limit Risk Assessment

No formal expression-limit findings.

- I did not find any interpolated `run:` or `if:` block over the 15,000-character audit threshold.
- The closest block is `.github/workflows/implement.yml:3767-3994`, estimated at ~14,728 characters, leaving ~6,272 characters before the 21,000-character hard cap and ~272 characters before the repo’s 15,000-character watch threshold.
- The largest workflow file is `.github/workflows/review_autofix.yml` at ~342,411 characters, well below the 800 KB / 1 MB workflow-size thresholds.

### Section 5: Cross-Cutting Concerns

#### SHELL-001
- **File path:** `scripts/review_collect_pr_metadata.sh:27-29; scripts/review_enable_auto_merge.sh:19-21; scripts/setup_serena.sh:11-16; scripts/review_floor_rules.sh:9-16`
- **Severity:** Low
- **Category tag:** `shellcheck`
- **Description:** These scripts all use the `CDPATH= cd -- ...` form that ShellCheck flags as SC1007. It works as an environment assignment to `cd`, but it reads like a typo, triggers the same warning in multiple files, and keeps the repo’s shell baseline noisier than necessary.
- **Recommended fix:** Normalize all four sites to the explicit ShellCheck-compliant form `CDPATH='' cd -- ...`, or wrap the pattern once in a shared helper such as `script_dir()` and reuse it.

No additional evidence-based dead-code or TODO/FIXME/HACK finding stood out beyond the duplication items above.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 5 | BUG-001, BATCH-001, API-003, CONSIST-001, DUP-001 |
| Low | 4 | API-001, API-002, DEBT-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 3 | Medium |
| Code modularization | 7 | Large |
| Expression size reduction | 0 | Small |
| Medium/Low fixes | 5 | Small |
