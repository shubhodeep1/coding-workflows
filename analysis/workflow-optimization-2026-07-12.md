## Executive Summary

- `review_autofix` is the dominant failure and latency hotspot. All 3 failed runs in this window (`29181029369`, `29184631998`, `29187610398`) failed in `review / codex-agent` → `Run reviewer models` after 30-36 OpenRouter calls/run, 6 `CONTEXT_BUDGET_WARN` events/run, prompt sizes around `259,748-265,185` tokens, `All reviewers failed`, and `AUTOFIX_EDITOR_EMPTY_NOOP=true`; estimated impact: recover `22,861s` (`6.35h`) of wasted runtime in this failure class; confidence: **high**.
- Most CI failure volume is deterministic prompt/workflow contract drift, not live Semble instability. 8 CI failures (`29178397430`, `29178474615`, `29180258919`, `29185248708`, `29188717586`, `29192843799`, `29193763798`, `29194789280`) failed `lint` → `Review/judge/conflict strict-render contract test`, while sampled telemetry shows `semble_fallbacks=45`, `semble_contract_test_fallbacks=45`, `semble_runtime_fallbacks=0`; estimated impact: removing this drift would eliminate `8/13` observed failures; confidence: **high**.
- Cost visibility is weakest on the most expensive path. Deep-dive telemetry captured `175` OpenRouter calls but `or_total_tokens=0`, `or_cache_write_tokens=0`, `or_cache_read_tokens=0`, and `cache_hit_rate=null`; failed run `29181029369` logged `30` usage lines with token/cache fields as `na`; estimated impact: unlock safe model/cache tuning on the current cost center; confidence: **high**.
- Stable paths should be left mostly alone for now. `orchestrate_poll` was `46/46` successful (p50 `204s`), and `copilot_pull_request_reviewer` was `24/24` successful (p50 `180s`); estimated impact: targeted fixes in `review_autofix` and CI are safer than broad pipeline redesign; confidence: **high**.
- AI memory retrieval looks healthy where emitted: sampled deep-dive telemetry showed `11/11` `retrieve` ops selecting records, avg `estimated_tokens=1383` against avg budget `1400`, `keyword_method=llm`, zero `fail_open`, zero `enabled=false`; estimated impact: mainly diagnostic confidence, not immediate speed/cost savings; confidence: **medium**.
- GH API and queue observability are too weak to explain several 200-300s “successful but idle-looking” runs. Recent runs `29197623519`, `29197951349`, `29197059068`, and `29197062690` show hosted-runner wait and paginated GitHub queries without structured call/retry counts; estimated impact: modest latency reduction and lower rate-limit risk once instrumented; confidence: **medium**.

## Speed Optimizations

1. **Critical-path win: shrink `review_autofix` input before reviewer execution**
   - **Evidence:** Failed runs `29181029369`, `29184631998`, and `29187610398` each failed in `Run reviewer models` after 6 `CONTEXT_BUDGET_WARN` events and prompts around `260k+` tokens. In `29184631998`, the assembled reviewer context already included the workflow’s prior “no output / will retry” comment from failed run `29181029369`, indicating a self-feeding prompt-growth loop.
   - **Root cause:** Administrative workflow comments and broad PR context are being fed back into later reviewer prompts until they reach or exceed model windows.
   - **Exact change:** In `scripts/assemble_prompt.sh` and `scripts/render_prompt.sh`, exclude workflow-authored retry/no-output comments from future prompt assembly; add a hard preflight budget check that drops low-signal blocks before any reviewer call.
   - **Diagnostic logging to add:** `PROMPT_COMPONENT component=<...> tokens=<...> kept=<true|false> reason=<...>` and `PROMPT_BUDGET total_tokens=<...> window=<...> overflow=<...>`.
   - **Estimated time savings:** Avoid most of the `22,861s` spent in the 3 failed review runs; likely pull `review_autofix` tail latency closer to successful run `29192166948` (`224s`).
   - **Implementation risk:** Low-medium.

2. **Critical-path win: stop replaying the full reviewer matrix after deterministic exhaustion**
   - **Evidence:** The 3 failed `review_autofix` runs consumed `30`, `36`, and `36` OpenRouter calls respectively, then logged `All reviewers failed`, followed by an empty editor/no-op path.
   - **Root cause:** Retry logic appears to treat deterministic context exhaustion like a transient provider failure.
   - **Exact change:** In `scripts/review_run_reviewers.sh`, classify reviewer outcomes (`context_budget_exceeded`, `provider_error`, `timeout`, `empty_editor_noop`) and only retry transient classes; if every reviewer fails deterministically, skip the editor phase.
   - **Diagnostic logging to add:** `REVIEW_ATTEMPT model=<...> attempt=<...> prompt_tokens_est=<...> retryable=<...> status=<...> elapsed_ms=<...>` and `REVIEW_SUMMARY reviewers_failed=<...> deterministic_failures=<...> editor_started=<...>`.
   - **Estimated time savings:** Roughly `4,000-6,500s` per affected failure run.
   - **Implementation risk:** Medium.

3. **Early-fail CI on prompt contract drift**
   - **Evidence:** 8 CI failures all died in `lint` → `Review/judge/conflict strict-render contract test` after `312-358s`, with the failing assertion in `tests/test_assemble_prompt.py` comparing rendered output to the legacy prompt.
   - **Root cause:** A deterministic render/golden mismatch is discovered late in the CI job.
   - **Exact change:** Run the prompt-contract test earlier whenever `scripts/render_prompt.sh`, `scripts/assemble_prompt.sh`, prompt templates, or golden prompt fixtures change.
   - **Diagnostic logging to add:** upload rendered-output hash, legacy-output hash, and a compact diff artifact on failure.
   - **Estimated time savings:** `5-6 minutes` per failing CI run, plus lower runner occupancy.
   - **Implementation risk:** Low.

4. **Micro-optimization: lazy-start MCP servers in Copilot review**
   - **Evidence:** Successful `copilot_pull_request_reviewer` runs `29192846809` and `29197062690` reported MCP session summaries with `github-mcp-server` and `playwright` connected but `invocations=0`.
   - **Root cause:** Tool servers are started even when the review never uses them.
   - **Exact change:** Gate MCP startup behind first tool use, or disable specific MCP servers by default for review-only runs.
   - **Diagnostic logging to add:** `MCP_BOOT server=<...> started=<...> first_use_ms=<...> invocations=<...>`.
   - **Estimated time savings:** `5-15s` per Copilot review run.
   - **Implementation risk:** Low.

## Cost Optimizations

1. **Stop spending on doomed `review_autofix` retries**
   - **Evidence:** The 3 failed `review_autofix` runs spent `102` OpenRouter calls total (`30+36+36`) and still ended in `All reviewers failed` plus an empty editor path. Each also emitted one Semble query (`15,984`, `15,984`, `16,766` query bytes) without preventing prompt overflow.
   - **Root cause:** The system continues to pay for model calls after the input has already become non-viable.
   - **Exact change:** Combine prompt compaction with deterministic retry suppression in `scripts/review_run_reviewers.sh`.
   - **Estimated savings:** High but not dollar-quantifiable from current logs; at minimum it removes most OR calls on this failure class.
   - **Quality-risk note:** Low if only applied to classified deterministic failures.

2. **Fix OpenRouter token/cache metering before changing models**
   - **Evidence:** Deep-dive telemetry shows `or_calls=175` but `or_total_tokens=0`; run `29181029369` logged `30` usage lines with `prompt_tokens=na`, `completion_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`.
   - **Root cause:** Usage fields are not being captured or normalized at the call boundary.
   - **Exact change:** In `scripts/review_run_reviewers.sh`, emit normalized per-call usage with model, request ID, prompt/completion/total tokens, cache write/read tokens, and reasoning effort; extend `scripts/cost_audit.py` / `scripts/collect_workflow_logs.py` to aggregate `usage_missing=true` separately from true zero.
   - **Estimated savings:** Indirect but prerequisite-level; without this, safe model downshifts and cache tuning are guesswork.
   - **Quality-risk note:** None.

3. **Make prompt cache measurable and actually reusable**
   - **Evidence:** Telemetry currently shows `or_cache_write_tokens=0`, `or_cache_read_tokens=0`, and `cache_hit_rate=null`. Inference: because usage lines are `na`, the current zeroes likely mean “unreported” rather than “no cache activity.”
   - **Root cause:** Cache activity is either uninstrumented or defeated by unstable prompt prefixes.
   - **Exact change:** Move volatile fields (run URLs, timestamps, retry comments) out of the cacheable prefix; emit `prompt_prefix_hash`, `cacheable_prefix_tokens`, `cache_write_tokens`, `cache_read_tokens`, and `cache_hit`.
   - **Estimated savings:** **Inference:** once measured, repeated review/retry flows should be able to save `10-30%` of prompt tokens on stable prefixes.
   - **Quality-risk note:** Low.

4. **Prove Semble is reducing prompt expansion before expanding its role**
   - **Evidence:** Sampled `review_autofix` telemetry shows `8` Semble queries and `119,753` query bytes, yet the dominant failing runs still overflowed context. Serena telemetry is absent (`0` query calls, `0` probes, `0` fallbacks), so there is no evidence Serena replaced downstream tool/model work in this window.
   - **Root cause:** The pipeline logs Semble request volume, but not whether Semble reduces downstream prompt size or model-call count.
   - **Exact change:** Add `response_bytes`, `selected_chunks`, `accepted_bytes_into_prompt`, `prompt_tokens_before`, and `prompt_tokens_after` to `SEMBLE_QUERY` logs; defer Serena cost conclusions until it emits real traffic.
   - **Estimated savings:** Currently unquantified; this is a measurement change needed before safe expansion or rollback.
   - **Quality-risk note:** Low.

## Reliability Improvements

1. **Eliminate deterministic `review_autofix` context-overflow failures**
   - **Failure evidence:** `29181029369`, `29184631998`, and `29187610398` all failed `review / codex-agent` → `Run reviewer models`, each with `6` `CONTEXT_BUDGET_WARN` events, prompt sizes around `260k+`, `All reviewers failed`, and `AUTOFIX_EDITOR_EMPTY_NOOP=true`.
   - **Root cause category:** Prompt management / deterministic retry policy.
   - **Exact fix:** Exclude workflow-authored retry comments from prompt assembly; add a hard budget guard; short-circuit deterministic “all reviewers exhausted” cases before editor launch.
   - **Expected reliability impact:** Addresses `3/13` observed failures directly and should collapse the worst `review_autofix` tail.
   - **Rollback / fail-open:** Keep compaction behind a feature flag initially; if compaction misbehaves, fail with a concise diagnostic rather than silently reverting to huge prompts.

2. **Treat CI strict-render failures as contract drift, not infrastructure noise**
   - **Failure evidence:** 8 runs failed in `lint` → `Review/judge/conflict strict-render contract test` (`29178397430`, `29178474615`, `29180258919`, `29185248708`, `29188717586`, `29192843799`, `29193763798`, `29194789280`).
   - **Root cause category:** Rendered-prompt / golden-fixture drift.
   - **Exact fix:** Make prompt-render changes update or validate their golden output in the same PR; emit rendered/legacy hashes plus a small diff artifact.
   - **Expected reliability impact:** Removes the single biggest repeated failure class (`8/13` total failures).
   - **Rollback / fail-open:** None needed; this is stricter diagnostics, not broader behavior change.

3. **Add workflow-contract diagnostics around optional verifier bootstrap/gating**
   - **Failure evidence:** CI run `29184610745` failed `lint` → `Orchestrate poll process unit tests` with `test_review_autofix_workflow_wires_optional_verifier_bootstrap_and_gate`; the same run also logged repeated integration fingerprint verification failures.
   - **Root cause category:** Workflow contract regression.
   - **Exact fix:** Add a workflow-contract trace from `.github/workflows/review_autofix.yml` / `.github/workflows/review_autofix_sweep.yml` generation or tests: `optional_verifier_enabled`, `bootstrap_job_present`, `gate_dependency_present`, fingerprint hash.
   - **Expected reliability impact:** Small on raw count (`1/13` failures in this window) but high leverage because it guards workflow correctness.
   - **Rollback / fail-open:** None.

4. **Make security-audit path failures self-diagnosing**
   - **Failure evidence:** `security_audit` run `29186428199` failed `security-audit` → `Run security audit` after logging `scope=full`, then `No such file or directory (os error 2)`.
   - **Root cause category:** Missing path / input resolution.
   - **Exact fix:** In `scripts/security_audit.sh`, preflight every required file/dir and log scope, tracker, `HEAD_SHA`, `LAST_AUDITED_SHA`, prompt path, exclusions path, changed-files path, and working dir before use.
   - **Expected reliability impact:** Low on aggregate failure rate, high on mean-time-to-diagnose.
   - **Rollback / fail-open:** Fail closed; only diagnostics should change.

5. **Stop synthetic Semble contract-test fallbacks from masquerading as runtime instability**
   - **Failure evidence:** Sampled telemetry shows `semble_fallbacks=45`, all `semble_contract_test_fallbacks=45`, with `semble_runtime_fallbacks=0`.
   - **Root cause category:** Telemetry classification noise.
   - **Exact fix:** Tag synthetic lines as `synthetic=true test=<name>` and keep them out of runtime-availability dashboards by default.
   - **Expected reliability impact:** Prevents false-positive incident investigation and masked-rollout confusion.
   - **Rollback / fail-open:** None.

## AI Memory Health

- **Observed health is good where emitted.** Sampled deep-dive `AI_MEMORY_TELEMETRY` showed `45` ops total: `record-run-event=26`, `retrieve=11`, `record-candidate=5`, `write_lessons_learned=3`. All `11/11` `retrieve` ops selected records; avg `estimated_tokens=1383` vs avg budget `1400`; `keyword_method=llm` in all sampled retrieves; zero `records_selected=0`, zero `fail_open=true`, zero `enabled=false`.
- **Coverage is the problem.** Recent successful runs such as `copilot_pull_request_reviewer` `29194460614` and `orchestrate_poll` `29196743958` explicitly note that `AI_MEMORY_TELEMETRY` was absent. No sampled logs showed `finalize-task`, `promote`, `compact`, or `processed-command-*` events.
- **Smallest safe logging addition:** emit one final `AI_MEMORY_SUMMARY` line per run with `ops_total`, `retrieves`, `retrieve_hits`, `avg_selected_tokens`, `push_retry_count`, `enabled`, and `fail_open_count`, then extend `scripts/cost_audit.py` to aggregate coverage. That will distinguish “memory disabled/unemitted” from “memory healthy but unused.”

## GH API Call Audit

> Exact GH call counts, retry counts, and rate-limit sleeps are not emitted today, so this audit is pattern-based rather than count-based.

1. **`orchestrate_poll` repeatedly lists tracking issues**
   - **Evidence:** Recent poller runs `29196743958` and `29197623519` logged `poll/Find active tracking issues` and found `2 active tracking issue(s)`.
   - **High-redundancy pattern:** The same repo-level tracking-issue lookup appears in every successful poller run.
   - **Concrete change:** In `scripts/orchestrate_poll_process.sh`, emit `GH_API_CALL logical_op=find_tracking_issues endpoint=issues.list items=<...> retries=<...> duration_ms=<...>` and `GH_API_SUMMARY` once per run.
   - **Expected reduction:** Likely modest per run, but it gives immediate visibility into whether the `204-298s` poller runtime is GH work, queue wait, or sleeping.
   - **Rate-limit risk reduction:** Medium.

2. **Review gate is doing paginated PR-file enumeration**
   - **Evidence:** Recent review/autofix gate run `29197059068` logged paginated PR-file fetches and `AUTOFIX_GATE_DET_SKIP_EVAL pr=3643 files=20 additions=532 deletions=24`.
   - **High-redundancy pattern:** PR file manifests are expensive enough to paginate and are natural candidates for reuse across gate/review/autofix phases.
   - **Concrete change:** Persist the PR file manifest once per run and reuse it across later steps; log `logical_op=pr_file_manifest page_count=<...> file_count=<...> additions=<...> deletions=<...>`.
   - **Expected reduction:** Remove at least one paginated file walk per reviewed PR.
   - **Rate-limit risk reduction:** Medium.

3. **Sweep logic is querying PRs and workflow runs in a way that looks batchable**
   - **Evidence:** Recent sweep run `29197951349` logged paginated `/pulls` and `/actions/workflows/.../runs` queries, then `AUTOFIX_SWEEP_SKIP pr=#3643 reason=active_run ... count=1`.
   - **High-redundancy pattern:** **Inference:** active-run detection is likely re-checking workflow state per PR when one precomputed map per workflow family would suffice.
   - **Concrete change:** Fetch active workflow runs once per sweep, build an in-memory PR/head-SHA index, and reuse it for skip decisions.
   - **Expected reduction:** O(PRs) workflow-run lookups down toward O(workflow pages).
   - **Rate-limit risk reduction:** High if PR volume rises.

4. **Add shared GH API telemetry in the retry wrapper**
   - **Evidence:** The current telemetry aggregator tracks Semble/Serena/cache/context metrics but not GH API volume or retry cost.
   - **Concrete change:** Add `gh_api_calls`, `gh_api_retries`, `gh_api_rate_limit_sleep_ms`, and `gh_api_error_count` to the collector/aggregator, sourced from a shared `GH_API_CALL` log line.
   - **Expected reduction:** Diagnostic first; this is the minimum needed before more aggressive batching.
   - **Rate-limit risk reduction:** High.

## Prompt Cache & Memory System

- **Prompt-cache telemetry is effectively unavailable today.** Deep-dive telemetry reports `or_cache_write_tokens=0`, `or_cache_read_tokens=0`, `cache_hit_rate=null`, and OpenRouter usage lines often show `na` for cache fields. First fix the logging before assuming cache is unused.
- **Cache fragmentation is likely high on `review_autofix` (inference).** The prompt appears to absorb volatile content such as prior retry comments and workflow-run references; run `29184631998` already contained the bot-authored retry comment from `29181029369`. That kind of early-prefix churn destroys cache reuse.
- **Context pressure is already eroding cache value.** The deep-dive sample recorded `24` `CONTEXT_BUDGET_WARN` events, concentrated in `review_autofix`; once prompts are near or above the model window, retries mutate too much of the prompt to benefit from caching.
- **Recommended additions:**  
  - `PROMPT_PREFIX_HASH`, `CACHEABLE_PREFIX_TOKENS`, `VOLATILE_SUFFIX_TOKENS`  
  - `CACHE_EVENT action=<read|write> tokens=<...> hit=<true|false>`  
  - `PROMPT_COMPONENT component=<memory|semble|comments|files|system> tokens=<...>`  
  - `MEMORY_PROMPT_DELTA retrieved_records=<...> accepted_tokens=<...> post_memory_prompt_tokens=<...>`  
  This will show whether memory and Semble are replacing prompt expansion or merely adding more bytes.
- **Expected impact:** **Inference:** once the prefix is stabilized and measured, repeated review flows should see lower tokens, lower latency, and less overflow risk without changing model behavior.

## Orchestrator Health

- **Live orchestrator behavior looks stable.** `orchestrate_poll` was `46/46` successful in the analyzed window; recent runs `29192755351`, `29196743958`, and `29197623519` completed successfully while repeatedly finding `2 active tracking issue(s)`. No `BREAK_GLASS` events were observed anywhere in the sampled telemetry.
- **External enrichment is failing open cleanly in the poller.** Runs `29196743958` and `29197623519` logged `SEMBLE_ENABLED=true` but `SEMBLE_AVAILABLE=false` / `SEMBLE_INDEX_AVAILABLE=false`; sampled aggregate telemetry still shows `semble_runtime_fallbacks=0`. That reads as healthy fail-open behavior, not a live outage.
- **The main orchestrator problem is observability, not correctness.** The current logs do not expose state transitions, wave counts, deferrals, no-op polls, or runner-wait time, so a `207-298s` successful poller run is hard to decompose.
- **Smallest safe additions:** emit `ORCH_STATE_TRANSITION from=<...> to=<...>`, `ORCH_POLL_DECISION action=<...> reason=<...> active_tracking_issues=<...>`, `ORCH_LOOP_SUMMARY gh_api_calls=<...> runner_wait_ms=<...> actions_triggered=<...>`, and `ORCH_NOOP=true` when a poll only observes state.
- **Observable indicators to track:** active tracking issue count, no-op poll count, runner wait ms, actions triggered per poll, GH API calls per poll, and Semble-availability mode.

## Pipeline Flow Bottlenecks

- **Clarify / plan:** Not a dominant failure source in this window. The sampled `plan` run `29178507886` succeeded in `603s`, and no repeated clarify-loop failures were surfaced. Optimization priority: **low**, but add phase timers if you need to compare against later weeks.
- **Implement / review-autofix:** This is the main compute + retry bottleneck. Successful `review_autofix` can finish in `224s` (`29192166948`), but failed runs stretched to `6490-8712s` because reviewer calls continued after context exhaustion. Optimization priority: **highest**.
- **Validate / CI:** This is the main deterministic rework bottleneck. 8 strict-render contract failures burned ~`46m` of CI time, and run `29184610745` added another `1798s` workflow-contract failure. Optimization priority: **high**, with earlier gating.
- **Orchestrate / poll:** This path is reliable but queue/chatty. Successful runs still spend `207-298s`, and recent logs show hosted-runner waits plus repeated issue-list work. Optimization priority: **medium**, mostly observability first.
- **Queueing vs compute vs retry vs merge/conflict overhead:**  
  - **Queueing:** hosted-runner wait appears in recent `orchestrate_poll`, sweep, review gate, and Copilot review runs.  
  - **Compute:** reviewer-model execution dominates the worst tail.  
  - **Retry/redo:** deterministic reviewer retries and repeated CI reruns on the same contract drift are the biggest avoidable waste.  
  - **Merge/conflict:** no major live conflict-heal loop was evident; the observed `conflict` signal came from a contract-test name, so conflict handling is not the first place to optimize.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` tail latency: family p50 `19s`, p95 `6490s`; 3 failures consumed `22,861s`.
  - Late CI prompt/workflow contract checks: 9 CI failures total, 8 of them the same strict-render test.
  - Reliable but long poller no-ops: `orchestrate_poll` 46/46 success, p50 `204s`, with runner-wait evidence in recent runs.

- **Top failure modes**
  - Context overflow + reviewer exhaustion in `review / codex-agent` → `Run reviewer models` (`29181029369`, `29184631998`, `29187610398`).
  - Prompt render/golden drift in `lint` → `Review/judge/conflict strict-render contract test` (8 runs).
  - One workflow-contract regression (`29184610745`) and one security-audit missing-path failure (`29186428199`).

- **Highest-cost drivers**
  - OpenRouter reviewer traffic on `review_autofix` (`175` OR calls in sampled deep-dive telemetry; `102` of them in the 3 failed runs alone).
  - Semble query volume without proof of downstream prompt reduction (`8` queries / `119,753` bytes in sampled deep-dive telemetry).
  - Missing cache and OR token telemetry, which blocks safe cost tuning.

- **Top 3 prioritized actions**
  1. Filter self-generated retry/no-output comments from prompt assembly and add hard budget compaction before reviewer calls.
  2. Add structured reviewer/OpenRouter/GH API logging (`REVIEW_ATTEMPT`, `PROMPT_COMPONENT`, `GH_API_CALL`, `SECURITY_AUDIT_INPUT`) and aggregate them in `scripts/cost_audit.py`.
  3. Move prompt/workflow contract tests earlier and attach rendered-vs-legacy diff artifacts so deterministic CI regressions fail fast.

## Metrics Appendix

**Metric layers:** run metadata covers the full `676`-run window; parsed deep-dive log telemetry covers only `35/676` runs. Token/cache/MCP metrics below use the parsed deep-dive layer unless noted.

| Overall window metric | Value |
|---|---:|
| Total runs | 676 |
| Success | 213 |
| Failure | 13 |
| Cancelled | 30 |
| `other_count` | 420 |
| Failure rate | 1.9% |
| p50 duration | 1s |
| p95 duration | 1818s |
| Parsed deep-dive telemetry coverage | 35 / 676 |

| Workflow family | Total | Success | Failure | Cancelled | p50 (s) | p95 (s) | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| `review_autofix` | 103 | 80 | 3 | 20 | 19 | 6490 | `or_calls=175`, `context_budget_warn_count=24`, `semble_query_calls=8` |
| `ci` | 22 | 3 | 9 | 10 | 1796 | 1820 | `semble_fallbacks=45`, all contract-test synthetic |
| `orchestrate_poll` | 46 | 46 | 0 | 0 | 204 | 238 | Recent runs repeatedly found 2 active tracking issues |
| `copilot_pull_request_reviewer` | 24 | 24 | 0 | 0 | 180 | 408 | MCP servers connected with `invocations=0` in sampled runs |
| `security_audit` | 1 | 0 | 1 | 0 | 73 | 73 | Failed on missing path / `os error 2` |

| Failure bucket | Failed runs | Runtime burned (s) | Runtime burned (h) |
|---|---:|---:|---:|
| `review_autofix` | 3 | 22861 | 6.35 |
| `ci` | 9 | 4561 | 1.27 |
| `security_audit` | 1 | 73 | 0.02 |

| Parsed deep-dive telemetry metric | Value | Notes |
|---|---:|---|
| Runs with parsed log telemetry | 35 | Partial coverage only |
| `codex_calls` | 11 | |
| `codex_tokens_used` | 22286 | |
| `or_calls` | 175 | Dominant unmetered model path |
| `or_total_tokens` | 0 | Usage blind spot |
| `or_cache_write_tokens` | 0 | Cache blind spot |
| `or_cache_read_tokens` | 0 | Cache blind spot |
| `cache_hit_rate` | null | Not usable yet |
| `semble_query_calls` | 8 | |
| `semble_query_bytes` | 119753 | Request bytes only; no response-byte accounting |
| `semble_fallbacks` | 45 | |
| `semble_contract_test_fallbacks` | 45 | All sampled fallbacks were synthetic contract-test events |
| `semble_runtime_fallbacks` | 0 | No live runtime Semble fallback observed |
| `serena_query_calls` | 0 | No Serena traffic observed |
| `serena_query_response_bytes` | 0 | |
| `serena_query_tool_calls` | 0 | |
| `serena_fallbacks` | 0 | |
| `serena_probe_ok` | 0 | |
| `serena_probe_failed` | 0 | |
| `serena_probe_skipped` | 0 | |
| `break_glass_count` | 0 | |
| `context_budget_warn_count` | 24 | Concentrated in `review_autofix` |
| `wall_clock_p50_ms` | 340000 | Sampled deep-dive wall-clock metric |
| `wall_clock_p99_ms` | 13102980 | Interpret cautiously due sparse coverage |

| MCP / probe target | Query calls | Response bytes | Tool calls | Fallbacks | Probe OK | Probe failed | Probe skipped | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Semble | 8 | n/a | n/a | 45 | n/a | n/a | n/a | All sampled fallbacks were contract-test synthetic |
| Serena (no targets emitted) | 0 | 0 | 0 | 0 | 0 | 0 | 0 | No `SERENA_*` activity in this window |
| Other MCP servers observed | 0 | 0 | 0 | 0 | 0 | 0 | 0 | No standardized `<NAME>_QUERY/_FALLBACK/_PROBE` lines observed |

| GH API hotspot evidence | Observed pattern | Missing metric | Safest next change |
|---|---|---|---|
| `orchestrate_poll` runs `29196743958`, `29197623519` | Repeated tracking-issue listing | Call count, retries, duration, rate-limit sleep | Add `GH_API_CALL` + `GH_API_SUMMARY` in poller |
| Review gate run `29197059068` | Paginated PR-file enumeration | Pages/items fetched | Reuse a single PR file manifest across later steps |
| Sweep run `29197951349` | Paginated `/pulls` and workflow-run lookups | Per-PR vs batched call shape | Precompute active-run map once per sweep |
| Multiple recent runs | Hosted-runner wait | `runner_wait_ms` | Emit queue/wait metric from run start to first execution |

- **Top stall signals:** `Run reviewer models` context overflow in `review_autofix`; late strict-render CI failures; hosted-runner wait in recent poll/sweep/review runs; `poll` dominating otherwise successful orchestrator runs.
- **Material data gaps:** only `35/676` runs with parsed deep-dive telemetry; `cache_hit_rate` unavailable; OpenRouter token/cache usage missing; no Serena traffic to evaluate; GH API call counts/retries/rate-limit sleeps not instrumented.

## Deep Audit — Workflows & Scripts (2026-07-12)

### Section 1: Bug & Correctness Sweep

#### CONSIST-001
- **File path:** `.github/workflows/review_autofix.yml:1029-1038,4515-4533,4704-4724,5585-5594`
- **Severity:** Medium
- **Category tag:** `consistency`
- **Description:** `review_autofix.yml` has four phase-transition paths that add issue labels with raw `POST /labels` writes only. The deterministic-skip path adds `ai:ready-to-merge` at `1029-1038`, and three fallback `set_issue_phase_label_resilient` copies at `4515-4533`, `4704-4724`, and `5585-5594` do the same for `ai:ready-to-merge`, `ai:review-blocked`, and `ai:closed`. The canonical helper in `scripts/label_helpers.sh:166-217` instead reads current labels, removes all other AI phase labels, and writes the full replacement set. Because `scripts/orchestrate_lib.py:1315-1336,2073-2082` resolves phase by first-match priority, stale phase labels can remain hidden on the issue instead of being cleaned up, leaving raw label state inconsistent for any caller that checks label presence/absence directly.
- **Recommended fix:** Route all issue phase transitions in `review_autofix.yml` through `scripts/label_helpers.sh:set_issue_phase_label_resilient`. If late-stage cleanup makes sourcing fragile, stage/copy `label_helpers.sh` once and keep only a thin bootstrap in YAML.

### Section 2: GitHub API Call Redundancy Audit

#### API-001
- **File path:** `scripts/review_collect_pr_metadata.sh:209-226; scripts/gh_helpers.sh:735-900`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** On the normal PR path, `review_collect_pr_metadata.sh` fetches PR payload, PR issue comments, and PR review comments as separate logical reads, and adds a fourth logical read for top-level reviews when `REVIEW_BREAK_GLASS_ENABLED` is enabled. The repo already has a GraphQL-first batching helper, `gh_pr_with_all_comments()`, that consolidates PR metadata, issue comments, and review comments in one call and is already used in `scripts/review_rb_judge.sh:973-985` and `scripts/orchestrate_poll_process.sh:14776-14788`.
- **Current call count:** `3` logical fetches on the common path; `4` when top-level reviews are enabled. `--paginate` can increase underlying HTTP call count further.
- **Proposed call count:** `1` logical GraphQL fetch after extending the helper to also emit top-level reviews.
- **Batching pattern to extend:** `scripts/gh_helpers.sh:761-900` — `gh_pr_with_all_comments <owner> <repo> <pr_number> [preloaded_meta_json]`
- **Recommended fix:** Extend `gh_pr_with_all_comments()` to return a `reviews` array, then have `review_collect_pr_metadata.sh` persist `.meta`, `.comments`, `.review_comments`, and `.reviews` from that single payload instead of refetching each resource separately.

#### API-002
- **File path:** `scripts/orchestrate_poll_process.sh:11212-11223; scripts/orchestrate_poll_process.sh:10489-10513`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** The standalone stall-recovery discovery stage still loops over seven phase labels and runs `gh issue list --label ...` once per label before making a separate GraphQL marker fetch. This creates an 8-call discovery fan-out before candidate hydration, even though the same file already contains GraphQL batching helpers for related discovery work.
- **Current call count:** `8` logical discovery calls in this path (`7` label scans + `1` marker GraphQL call).
- **Proposed call count:** `1` logical GraphQL discovery call in the common case, with the current multi-call path kept only as a pagination fallback when any alias reports `hasNextPage=true`.
- **Batching pattern to extend:** `scripts/orchestrate_poll_process.sh:10489-10513` — `_fetch_standalone_marker_issues_graphql`
- **Recommended fix:** Extend `_fetch_standalone_marker_issues_graphql` (or replace it with a sibling helper) to return the seven phase buckets plus the existing marker buckets in one aliased GraphQL query, then union the issue numbers locally before `_fetch_candidate_issue_details_graphql`.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path:** `scripts/gh_helpers.sh:532-545; .github/workflows/plan.yml:1991-2004; .github/workflows/implement.yml:4837-4847; .github/workflows/orchestrate.yml:1013-1026; scripts/check_failure_triage.sh:66-79; scripts/implement_diagnose_post_codex_failure.sh:52-62`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** `_safe_gh_jq` is duplicated in five non-canonical locations. The copies have already drifted: the canonical helper logs a `mktemp` failure before returning, while the `implement.yml` and `implement_diagnose_post_codex_failure.sh` copies silently return `1`.
- **Recommended fix:** Keep `scripts/gh_helpers.sh::_safe_gh_jq` as the only implementation, with the existing “same args as `gh api`” signature. Update the workflow/script callers to source or stage `gh_helpers.sh` instead of embedding local copies.

#### DUP-002
- **File path:** `scripts/label_helpers.sh:130-217; scripts/validate_process.sh:1039-1147; scripts/orchestrate_poll_process.sh:2234-2352; scripts/review_rb_judge.sh:729-761; .github/workflows/review_autofix.yml:1029-1038,4495-4533,4691-4724,5576-5594`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** Label-creation and phase-swap logic is reimplemented across multiple scripts and workflows. The copies are no longer behaviorally identical: `label_helpers.sh` performs a full phase replacement, while several `review_autofix.yml` copies are add-only, which is the direct source of `CONSIST-001`.
- **Recommended fix:** Make `scripts/label_helpers.sh` the only owner of `ensure_label_exists <label> <repo>` and `set_issue_phase_label_resilient <issue_number> <target_label> <repo>`. Update the listed callers to source/stage that helper instead of carrying inline variants.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001
- **File path:** `.github/workflows/plan.yml:986-1274`
- **Severity:** High
- **Category tag:** `expression-limit`
- **Estimated expression size:** `19,165` chars
- **Headroom remaining:** `1,835` chars
- **Description:** This interpolated `run:` block is already above the 85% risk threshold. It embeds a large planning prompt heredoc plus multiple `${{ }}` interpolations, so normal instruction growth can push it over GitHub’s 21,000-character limit and invalidate the workflow at parse time.
- **Recommended fix:** Move the planning prompt body to a file under `prompts/` (for example `prompts/mode-plan.txt`) and keep the step as a thin wrapper that renders it with `scripts/render_prompt.sh`.

#### EXPR-002
- **File path:** `.github/workflows/implement.yml:3624-3869`
- **Severity:** High
- **Category tag:** `expression-limit`
- **Estimated expression size:** `18,329` chars
- **Headroom remaining:** `2,671` chars
- **Description:** This interpolated `run:` block bundles the scope-block handler, destructive-commit handler, long inline comments, and Telegram payload assembly into one YAML scalar. It is already over the 18,000-character high-risk threshold.
- **Recommended fix:** Extract this handler to a dedicated script (for example `scripts/implement_handle_blocked_commit.sh`) or split the scope-block and destructive-block branches into separate steps. The repo already uses this pattern in `scripts/implement_diagnose_post_codex_failure.sh`.

#### EXPR-003
- **File path:** `.github/workflows/memory_maintenance.yml:45-391`
- **Severity:** Medium
- **Category tag:** `expression-limit`
- **Estimated expression size:** `15,152` chars
- **Headroom remaining:** `5,848` chars
- **Description:** This `run:` block includes two inline Python heredocs, OpenRouter request construction, and several `${{ }}` interpolations. It has crossed the 15,000-character medium-risk threshold, so incremental feature growth can push it into the high-risk band quickly.
- **Recommended fix:** Extract the shell flow to `scripts/` and move the inline Python programs into dedicated Python modules or separate helper scripts.

- No inline `if:` expression crossed the 15,000-character threshold in the audited workflows.
- No workflow exceeded the 800 KB file-size risk threshold; the largest audited workflow was `.github/workflows/review_autofix.yml` at `354,374` characters.

### Section 5: Cross-Cutting Concerns

#### DEAD-001
- **File path:** `scripts/orchestrate_poll_process.sh:9253-9260,11299-11300`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `read_standalone_state_json()` is definition-only in repo-local search and is not used by the active standalone recovery loop, which parses state directly from cached `comments_json` at `11299-11300`. That leaves an unexercised paginated-comments helper in a hot file.
- **Recommended fix:** Remove `read_standalone_state_json()` if it is truly obsolete, or switch the live path to use it and add coverage if the paginated fetch path is still intended.

#### SHELL-001
- **File path:** `scripts/implement_diagnose_post_codex_failure.sh:269-273`
- **Severity:** Low
- **Category tag:** `shellcheck`
- **Description:** ShellCheck reports SC2015 on `[ "${_attempt}" -lt 3 ] && sleep 4 || true`. Under `set -euo pipefail`, the trailing `|| true` masks a failed or interrupted `sleep` and makes the retry control flow harder to reason about.
- **Recommended fix:** Rewrite the tail as `if [ "${_attempt}" -lt 3 ]; then sleep 4; fi`.

- No new `TODO`/`FIXME`/`HACK` markers were found in `.github/workflows/*.yml`, `scripts/*.sh`, or `scripts/*.py` during this pass.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | EXPR-001, EXPR-002 |
| Medium | 6 | CONSIST-001, API-001, API-002, DUP-001, DUP-002, EXPR-003 |
| Low | 2 | DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 workflows | Medium |
| API call optimization | 3 scripts | Medium |
| Code modularization | 10 files | Large |
| Expression size reduction | 3 workflows | Medium |
| Medium/Low fixes | 2 scripts | Small |
