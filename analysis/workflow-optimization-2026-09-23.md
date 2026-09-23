## Executive Summary

- **CI is failing late and expensively:** 26/31 CI runs failed at `Script-workflow cross-reference`; nine deep dives found the same missing `scripts/helper.sh` only after 37.3–42.9 minutes. Moving this check first saves an estimated **16–18 runner-hours/window**. **Confidence: high.**
- **Review credential-contract regression caused six verified long failures:** runs `35737368348`, `35755050146`, `35768346658`, `35779370870`, `35790039807`, and `35795320252` failed after reviewer work because editor isolation required `OPENROUTER_API_KEY` after `opencode_helpers.sh` had unset it. They consumed **97,123,290 OpenRouter tokens** and 8.2 runner-hours. **Confidence: high.**
- **Review/autofix dominates AI cost:** 143.95M of 144.11M OpenRouter tokens—**99.9%**—came from this family; p95 duration was 5,948 seconds. **Confidence: high.**
- **Prompt caching is valuable but context is near capacity:** derived weighted cache-read rate is **80.55%**, while eight `CONTEXT_BUDGET_WARN` events occurred exclusively in review/autofix, reaching 97.7% of one model’s context window. **Confidence: high.**
- **Metadata artifact creation is another deterministic failure:** three semantic-agent runs failed at `Collect PR metadata`; run `35798932027` fetched a 170,637-byte diff, then failed because `linked_issue_metadata.json` was never produced. **Confidence: high for latest run; medium for earlier two.**
- **Orchestration is reliable but repeatedly processes stalled state:** 83/85 poll runs succeeded, yet issue `#4290` remained `ai:done` for 142–150 minutes. Fresh-push suppression correctly prevented duplicate recovery. **Confidence: high.**

## Speed Optimizations

1. **Critical path — run workflow/script reference validation first**
   - **Evidence:** all 26 CI failures ended at `Script-workflow cross-reference`; nine logs reported `implement.yml: references scripts/helper.sh which does not exist` after a median 42.2 minutes.
   - **Root cause:** a deterministic, sub-second repository-integrity check runs after the full test suite.
   - **Exact change:** move `python3 scripts/check_workflow_script_refs.py` immediately after checkout/setup in `.github/workflows/ci.yml` and equivalent release validation.
   - **Savings:** 37–43 minutes per affected run.
   - **Risk:** low.

2. **Critical path — preflight editor provider credentials before reviewer fan-out**
   - **Evidence:** six long runs reached the editor only to fail immediately at historical `review_apply_fixes.sh:168`.
   - **Root cause:** `opencode_helpers.sh` captured then unset the raw key; historical isolation subsequently required the unset variable instead of the broker token.
   - **Exact change:** validate `provider_key_captured=true`, `broker_ready=true`, and `broker_token_present=true` before reviewers. Never log values. Isolation must consume `MODEL_PROVIDER_BROKER_TOKEN`, not the raw key.
   - **Savings:** 56–99 minutes and millions of tokens per affected run.
   - **Risk:** low. HEAD removed the historical path, but runtime regression coverage is still needed.

3. **Critical path — bound unhealthy reviewer slots**
   - **Evidence:** 14 structured deep runs recorded eight 600-second stall kills. `minimax/minimax-m3` repeatedly stalled and retried/fell back; the circuit breaker was disabled.
   - **Exact change:** open the existing circuit after one stall plus one retryable failure for the same model, with the existing TTL. Emit model, slot, elapsed time, and circuit state.
   - **Savings:** approximately 10–30 minutes on affected review runs.
   - **Risk:** medium; retain minimum successful-reviewer quorum.

4. **Recurring path — lazily initialize Semble in pollers**
   - **Evidence:** orchestrate-poll ran 85 times but recorded only one Semble query; recent runs spent roughly a minute installing/building Semble before no-query poll cycles.
   - **Exact change:** defer Semble setup until a judge or context-expansion path actually requests it.
   - **Savings:** estimated 30–60 seconds on most poll runs.
   - **Risk:** low.

5. **Micro-optimization — circuit-break repeated live-log fetches**
   - **Evidence:** stable-release run `35768550507` made **236 failed live-log fetches** for job `106891727076` over roughly 11 minutes.
   - **Exact change:** after three consecutive failures, suppress live-log calls for 60 seconds while retaining status polling.
   - **Savings:** up to 233 API calls; small direct latency reduction.
   - **Risk:** low.

## Cost Optimizations

1. **Eliminate reviewer spend before deterministic editor failure**
   - The six verified provider-contract failures consumed **97.12M tokens**, 67.5% of review/autofix’s total.
   - Add the early provider preflight above and classify failures as `credential_contract`, not `editor_empty_noop`.
   - **Estimated savings:** up to 97M tokens for this observed recurrence.
   - **Quality risk:** none.

2. **Enable risk-tiered reviewer selection**
   - `REVIEW_TIER_RESOLVER_ENABLED=false`, `REVIEWER_RISK_TIER_ENABLED=0`, while reviews routinely invoke six slots at `xhigh`.
   - Enable the existing tier resolver for low-risk diffs, preserving full review for `.github/workflows/`, `scripts/`, prompts, migrations, and contracts.
   - **Estimated savings:** 10–25% of review prompt/completion tokens.
   - **Quality risk:** medium; deploy in observe-only mode first.

3. **Reduce context duplication**
   - Eight warnings occurred at 141K–165K tokens on 200K models; run `35701080313` reached 125,098/128,000.
   - Deduplicate repeated workflow instructions, cap historical comment bodies, and pass unchanged static context only once.
   - **Estimated savings:** 5–15% of uncached prompt tokens, approximately 1.4M–4.2M tokens.
   - **Quality risk:** low if hashes and omission summaries are logged.

4. **Keep Semble enabled**
   - Runtime queries typically returned 12 chunks, 13–15KB, in 348–581ms. There were zero runtime fallbacks; all 40 fallbacks were explicit contract tests.
   - Semble appears to add compact, low-latency context rather than noisy expansion.
   - Add `source_bytes`, `selected_bytes`, `dedup_bytes`, and estimated prompt bytes avoided to prove net benefit.

5. **Serena cannot yet be evaluated**
   - Queries, tool calls, response bytes, fallbacks, and probes were all zero; deep logs show it disabled.
   - Emit a skipped probe when disabled so “intentionally off” is distinguishable from missing instrumentation.

## Reliability Improvements

1. **Fix and regression-test the provider credential contract**
   - **Impact:** prevents the six verified editor failures.
   - Add `EDITOR_PROVIDER_CONTRACT status=ok|failed raw_key_captured=<bool> broker_ready=<bool> isolation_mode=<mode>`.
   - Fail before reviewers when the contract is unsatisfied.
   - **Rollback:** fail closed for editing while preserving reviewer artifacts.

2. **Restore linked-issue metadata artifact ownership**
   - `scripts/review_collect_pr_metadata.sh` neither requires nor writes `LINKED_ISSUE_METADATA_FILE`, while the workflow immediately hashes it.
   - Atomically write the artifact and emit:
     `PR_METADATA_ARTIFACT status=ok bytes=<n> sha256=<digest> linked=<n> fallback=<n>`.
   - **Expected impact:** remove the repeated 265–359 second metadata failures.
   - **Fail-open:** linked context may be empty, but artifact schema and digest must still exist.

3. **Fix the missing workflow reference and fail fast**
   - Remove the stale `scripts/helper.sh` reference or restore the intended helper.
   - Log the workflow path, reference type, source line, and missing path as structured JSON.
   - **Expected impact:** eliminate the CI family’s observed 83.9% failure rate.

4. **Improve AI-memory failure diagnostics**
   - Twelve unique fail-open events contained only `source=shell`: six `record-candidate`, six `record-run-event`.
   - Add operation, helper, exit code, failure class, push attempt, and sanitized stderr category.
   - Continue fail-open, but alert only after repeated failure for the same operation.

5. **Separate contract-test MCP fallbacks from operational availability**
   - All 40 Semble fallbacks were `context=contract-test`, not production failures.
   - Exclude them from availability SLOs while retaining separate test coverage totals.

## AI Memory Health

- **Retrieval:** 12/12 retrieves selected records: **100% hit rate**.
- **Budget:** average estimated tokens **1,387.2 / 1,400**—99.1% utilization.
- **Keyword method:** `llm` 12, `plain` 0, `none` 0.
- **Zero-record retrieves:** 0.
- **Disabled retrieves:** 0.
- **Fail-open events:** 12 unique—six candidate writes and six run-event writes.
- **Push retries above one:** two events: run `35717770409` phase start and run `35772092715` phase failure.
- **Gap:** no `promote`, `compact`, or `finalize-task` telemetry appeared in the deep sample. Verify those operations emit telemetry, and log why 17 `write_lessons_learned` operations all wrote zero records.

## GH API Call Audit

- **Material data gap:** no dynamic per-endpoint call totals were collected. Static code contains many API call sites, but that is not evidence of runtime volume.
- **Confirmed hotspot:** release run `35768550507` repeated the same failed job-log lookup 236 times.
- **Existing positive hygiene:** `gh_helpers.sh` provides rate-aware retries, permanent-failure classification, and several GraphQL/batched helpers. No rate-limit events were observed.
- **Required instrumentation:** emit one sanitized line per wrapper invocation:
  `GH_API_CALL helper=<name> method=<method> endpoint=<normalized> attempt=<n> result=<result> status=<code> duration_ms=<n> cache=<state> rate_remaining=<n>`.
- **Batch/reuse recommendation:** cache poll-tick PR/issue metadata by number and head SHA, then reuse it across stall, conflict, and noop-suspicious sweeps.
- **Expected reduction:** over 95% for the E2E failed-log hot loop; broader poller savings require the missing runtime call telemetry.

## Prompt Cache & Memory System

- **Reported aggregate `cache_hit_rate`:** unavailable (`null`).
- **Derived weighted cache-read rate:** **80.55%**, using cache-read divided by prompt-plus-cache-read tokens.
- Individual review runs ranged from 62.0% to 88.2%; stable-release run `35768550507` reported 0%.
- Cache reads were **114.98M tokens**, versus only 158,932 cache-write tokens. Review/autofix reported no cache writes, suggesting incomplete provider creation telemetry.
- Twenty-five of 166 OpenRouter calls—15.1%—had unavailable usage.
- Preserve stable instructions and repository context at the prompt prefix; place timestamps, run IDs, comments, and volatile diffs afterward.
- Emit `prompt_prefix_sha256`, static/dynamic byte counts, model, and cache status per call to identify fragmentation.
- Memory retrieval is effective, but the near-full 1,400-token budget leaves little room for ranking variance; log candidate count before and after truncation.

## Orchestrator Health

- Orchestrate-poll: **85 runs, 83 successes, 0 failures, 2 cancellations; p50 305s, p95 608.4s**.
- Issue `#4290` was repeatedly identified as stalled in `ai:done`, but runs `35799020783` and `35799657783` safely skipped retriggering because PR `#4291` had a fresh push.
- Autofix sweeps correctly suppressed duplicate active runs for PRs `#4291` and `#4259`.
- Cancel-on-close runs warned that three active runs lacked PR linkage. Add one structured event containing run IDs, workflow paths, head branches, and attempted lineage sources.
- Track:
  - `ORCH_TICK_SUMMARY issues_scanned transitions dispatches skips api_calls`
  - `STALL_DECISION issue phase age action outcome`
  - `WAVE_PROGRESS tracking_issue wave active terminal deferred`
  - `CONFLICT_SWEEP prs_scanned fixed skipped duration_ms`

## Pipeline Flow Bottlenecks

| Stage | Observed behavior | Dominant overhead |
|---|---|---|
| Clarify | p50 1s; most runs skipped/no-op | Event filtering |
| Plan | p50 1s; occasional 701–1,398s runs | Compute outliers |
| Implement | p95 835.5s; active run `35782284543` took 2,377s | Model work |
| Review/autofix | p95 5,948s; six verified editor-contract failures | Reviewer compute, stalls, late failure |
| CI | p50 2,356s; 26/31 failures | Late deterministic gate |
| Validate | One 512s run | Sparse evidence |
| Orchestrate poll | p50 305s | Setup plus repeated state/API sweeps |
| Stable release | 6,093s | E2E waits and failed live-log polling |

Queueing is also material: several review runs were cancelled before their first step after 3,161–4,613 seconds. Add `RUN_QUEUE_TELEMETRY queued_ms runner_wait_ms concurrency_wait_ms cancelled_before_first_step superseded_by_run_id`.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- Review/autofix reviewer fan-out and 600-second stall detection.
- CI’s cross-reference check runs after approximately 40 minutes.
- Pollers pay repeated setup costs despite rarely using Semble.

**Top failure modes**
- Historical editor provider-key/isolation contract failure.
- Missing `linked_issue_metadata.json`.
- Missing `scripts/helper.sh` workflow reference.

**Highest-cost drivers**
- Review/autofix: 143.95M OpenRouter tokens.
- Six verified failed editor cycles: 97.12M tokens.
- Full six-reviewer `xhigh` configuration with disabled tiering.

**Top three actions**
1. Add and enforce the provider/broker credential contract before reviewer invocation.
2. Fix metadata artifact generation and move script-reference validation to the start of CI.
3. Enable bounded reviewer circuit breaking and lazy Semble setup.

## Metrics Appendix

### Outcomes

| Scope | Runs | Success | Failure | Cancelled | Other | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Repository total | 1,000 | 345 | 43 | 16 | 596 | 7s | 2,386s |
| CI | 31 | 5 | 26 | 0 | 0 | 2,356s | 2,571s |
| Review/autofix | 105 | 74 | 16 | 11 | 4 | 287s | 5,948.4s |
| Orchestrate poll | 85 | 83 | 0 | 2 | 0 | 305s | 608.4s |
| Implement | 132 | 7 | 1 | 3 | 121 | 1s | 835.5s |
| Test and mark stable | 1 | 1 | 0 | 0 | 0 | 6,093s | 6,093s |

### AI Cost and Cache

| Metric | Value |
|---|---:|
| Codex tokens / calls | 1,326,191 / 13 |
| OpenRouter prompt tokens | 27,756,326 |
| OpenRouter completion tokens | 1,386,822 |
| OpenRouter cache-read tokens | 114,976,240 |
| OpenRouter cache-write tokens | 158,932 |
| OpenRouter total tokens | 144,112,594 |
| OpenRouter calls | 166 |
| Usage available / unavailable | 141 / 25 |
| Reported aggregate cache hit rate | unavailable |
| Derived weighted cache-read rate | 80.55% |
| Wall-clock p50 / p99 | 14,000ms / 6,668,400ms |
| Wall-clock samples | 116 |
| `BREAK_GLASS` | 0 |
| `CONTEXT_BUDGET_WARN` | 8 |

### MCP

| System | Queries | Bytes | Fallbacks | Runtime fallback | Contract-test fallback |
|---|---:|---:|---:|---:|---:|
| Semble | 28 | 280,975 | 40 | 0 | 40 |
| Serena | 0 | 0 | 0 | 0 | 0 |

| Target/system | Probe OK | Probe failed | Probe skipped | Note |
|---|---:|---:|---:|---|
| Serena, all targets | 0 | 0 | 0 | Disabled in deep logs; skipped probes not emitted |
| Semble, reviewer-context/overflow | N/A | N/A | N/A | Semble telemetry has query/fallback events, not probes |

**Other MCP servers observed:** none.

### GH API

| Signal | Value |
|---|---:|
| Dynamic total call count | unavailable |
| Observed rate-limit events | 0 |
| Repeated failed live-log calls, run `35768550507` | 236 |
| Runtime endpoint breakdown | unavailable |

**Coverage gaps:** one repository only; success sampling was 7%; deep folders covered 38 unique runs; dynamic GH API totals and full Serena availability telemetry were absent.
