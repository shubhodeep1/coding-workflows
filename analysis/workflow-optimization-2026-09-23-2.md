## Executive Summary

- **Move script-reference validation to the first CI gate.** CI failed 25/36 runs (69.4%); representative runs `35817863136`, `35818026682`, and `35813579769` spent 34–43 minutes before reporting `implement.yml` referencing nonexistent `scripts/helper.sh`. Estimated recoverable waste: **16.8 runner-hours** in this window. **Impact: very high; confidence: high.**
- **Gate live release tests on static preflight.** Stable gate `35802596362` knew static validation had failed by `01:10:39Z` but continued until `02:48:57Z`; parent promotion `35802575310` consequently lasted 8,139s. Estimated saving: **98–135 minutes per deterministic gate failure**. **Impact: very high; confidence: high.**
- **Review/autofix dominates AI cost and tail latency.** It consumed 49,052,286 OpenRouter tokens—**99.66%** of repository OpenRouter usage—and had p95 duration 5,359.8s. Adaptive reviewer selection and health-based slot quarantine should save **15–30% of review tokens** and 15–35 minutes on stalled runs. **Impact: high; confidence: medium.**
- **Minimax is the principal review-cost/stall hotspot.** `minimax/minimax-m3` accounted for 26,368,820 tokens (**53.8% of review usage**), with 9/20 calls lacking usage data; stall/server-error retries materially delayed run `35779370870`. **Impact: high; confidence: high.**
- **Queued and redundant review runs amplify congestion.** Fifteen review runs were cancelled after accumulating 20,559s; selected slow runs had 4.69 hours of runner queue delay, including `35804161376` waiting 7,744s before a 125s PR-closed path. **Impact: high; confidence: medium.**
- **Diagnostics are insufficient at two repeated failure points.** Runs `35814503883` and `35820358995` only logged “Could not publish the linked-issue metadata integrity digest”; security run `35821734999` exposed a missing-path error without the offending path. **Impact: medium; confidence: high.**

## Speed Optimizations

1. **Critical path: create a fast integrity-preflight job.**
   - **Evidence:** CI’s `Script-workflow cross-reference` is currently the final step in `.github/workflows/ci.yml`. Twenty-five failed runs accumulated 60,540s. Representative failures reported missing `scripts/helper.sh`.
   - **Root cause:** A deterministic repository-consistency check runs after the complete test suite.
   - **Exact change:** Split checkout plus `scripts/check_workflow_script_refs.py` into a 1–2 minute `integrity-preflight` job; make the expensive lint/test job depend on it.
   - **Logging:** Emit `WORKFLOW_SCRIPT_REF_SUMMARY scanned_workflows=N references=N missing=N elapsed_ms=N` and one sanitized line per missing reference including workflow and YAML line.
   - **Estimated saving:** 34–43 minutes per affected CI run; approximately 16.8 runner-hours in this window.
   - **Risk:** Low; retain the existing final check temporarily as a redundant assertion.

2. **Critical path: make release E2E jobs depend on static preflight.**
   - **Evidence:** `validate-scripts` failed in run `35802596362`, while the independent E2E smoke test continued and eventually failed with `Editor Bait ... retry_timeout` and `Orch Tests ... not_run`.
   - **Root cause:** Live tests and static checks fan out from `resolve-version` independently.
   - **Exact change:** Add a small preflight job and include it in `needs` for `e2e-smoke-test`, alt-model, orphan-workflow, and other live dispatch jobs.
   - **Estimated saving:** 98 minutes after the observed static failure; up to roughly 135 minutes if the reference check runs first.
   - **Risk:** Low; only deterministic static failures block live testing.

3. **Critical path: suppress redundant pending review dispatches without cancelling active autofix.**
   - **Evidence:** Review had 15 cancellations and 20,559s of accumulated duration. `35815804016` was cancelled before its first step after 3,826s. The reusable workflow correctly serializes by PR with `cancel-in-progress: false`, but different event routes can still create replacement pending runs.
   - **Root cause:** Dispatch deduplication is not uniformly applied before every explicit/fallback trigger.
   - **Exact change:** Reuse the existing active/pending-run helper before all explicit dispatch paths; key deduplication by repository and PR, not event type or SHA. Preserve active PR-backed runs.
   - **Logging:** `REVIEW_DISPATCH_DECISION pr=N source=... active_run=... pending_run=... action=dispatch|suppress`.
   - **Estimated saving:** Reduced queue churn and potentially 20–40 minutes p95 during bursts.
   - **Risk:** Low–medium; fail open when API state is unavailable.

4. **Critical path: enable reviewer health quarantine.**
   - **Evidence:** Run `35779370870` spent approximately 37 minutes retrying/failing back the Minimax slot; runs `35810093343`, `35813580294`, and `35814486871` also recorded stall kills or retryable failures.
   - **Root cause:** `REVIEWER_CIRCUIT_BREAKER_ENABLED=0` despite existing health-cache controls.
   - **Exact change:** Enable the existing circuit breaker with the current threshold of three failures; initially quarantine only retryable stall/server-error classes.
   - **Estimated saving:** 15–35 minutes on affected review runs.
   - **Risk:** Medium; require at least three successful reviewer slots before proceeding.

5. **Micro-optimization: skip low-yield Semble overflow results.**
   - **Evidence:** Three overflow queries returned only 385, 48, and 15 bytes while each took about 0.57s.
   - **Exact change:** Avoid overflow queries for very small files or suppress responses below a configurable minimum yield.
   - **Estimated saving:** Under 5 seconds per affected run; minimal token reduction.
   - **Risk:** Low.

## Cost Optimizations

1. **Adaptive model fanout and slot quarantine.**
   - **Evidence:** Review used 49.05M tokens. Minimax alone used 26.37M; every slow sampled review configured six reviewer slots at `xhigh` reasoning.
   - **Root cause:** Fixed full fanout and disabled health/risk controls.
   - **Exact change:** Enable the circuit breaker, then use the existing risk-tier framework for low-risk changes while preserving full review for workflows, security, migrations, and contracts.
   - **Estimated saving:** **7–15M tokens per comparable window**.
   - **Quality risk:** Medium; preserve a successful-reviewer floor and force-full-review label.

2. **Reduce prompt growth before cache tuning.**
   - **Evidence:** Five review runs emitted `CONTEXT_BUDGET_WARN`; run `35814486871` reached 197,255/200,000 tokens (98.6%), and `35798913669` reached 91.5%.
   - **Root cause:** Large diffs, comments, memory, and reviewer context are repeatedly assembled into model prompts.
   - **Exact change:** Deduplicate comment context, summarize unchanged review history, move timestamps/run IDs after stable cacheable prefixes, and reference large artifacts by file rather than duplicating them.
   - **Estimated saving:** 10–20% of prompt tokens, approximately **1.1–2.2M tokens**.
   - **Quality risk:** Low–medium; never truncate current diff or unresolved review comments.

3. **Circuit-break repeated same-head editor no-ops.**
   - **Evidence:** Failed run `35779370870` consumed 11.73M tokens before `editor_empty_noop`; its log reported an editor failure streak of 13. Four additional long failures ended at `Apply fixes with editor model`.
   - **Exact change:** Stop automatic redispatch after two identical same-head empty/no-op failures; rearm only after head SHA, editor model, or configuration changes.
   - **Estimated saving:** Up to one full multi-million-token review cycle per suppressed duplicate.
   - **Quality risk:** Low if findings are still posted and manual retry remains available.

4. **Semble appears useful overall; optimize only low-yield overflow calls.**
   - Seven `reviewer-context` queries returned 105,980 bytes in 3.55s, likely replacing much larger untargeted context, although source-bytes-avoided telemetry is missing.
   - Eight overflow queries returned 42,308 bytes; several were near-empty.
   - Add `source_bytes_scanned`, `prompt_bytes_avoided`, and `selected_chunk_scores` before making broader reductions.

5. **Memory trimming is a micro-saving.**
   - Eight retrievals selected 31–33 records and averaged 1,388.75 tokens against a 1,400-token budget.
   - Deduplicate records or lower the initial budget to 1,000–1,100 with fallback expansion.
   - Estimated saving: 300–500 tokens per retrieval; quality risk medium.

## Reliability Improvements

1. **Fix and guard the missing workflow script reference.**
   - **Evidence:** CI collector failure point was `Script-workflow cross-reference` for all 25 failures; deep dives repeatedly found `implement.yml` referencing nonexistent `scripts/helper.sh`.
   - **Category:** Deterministic repository inconsistency.
   - **Fix:** Remove the stale reference or commit the required script; run the reference checker in preflight and generation checks.
   - **Expected impact:** Eliminate the 69.4% CI failure cluster.
   - **Rollback/fail-open:** Remain fail-closed; this check protects deployed workflows.

2. **Expose metadata-integrity publication failures.**
   - **Evidence:** Runs `35814503883` and `35820358995` failed identically after collecting PR context and a 163KB diff, but logged no command, destination, return code, or stderr.
   - **Category:** Output-publication/integrity contract.
   - **Fix:** Log sanitized destination type, file existence/bytes, return code, and first stderr line. Emit a success marker too.
   - **Expected impact:** Faster root cause and prevention of repeated opaque failures.
   - **Rollback:** Keep fail-closed before reviewer/model spend.

3. **Treat empty editor output as an explicit model-slot failure.**
   - **Evidence:** `35779370870` completed reviewer, consolidator, parser, and ledger phases but failed with an empty editor summary. Short runs `35814503883` and `35820358995` also finalized as `editor_empty_noop`.
   - **Category:** Model/tool execution and output validation.
   - **Fix:** Retry once using the configured fallback model when output is empty, then findings-only finalize. Include editor attempt duration, exit status, stdout/stderr byte counts, and tool-call count.
   - **Expected impact:** Fewer reruns and fewer false-success/no-output paths.
   - **Rollback:** Never commit or push when editor validation fails.

4. **Improve security-audit missing-path diagnostics.**
   - **Evidence:** Run `35821734999` successfully installed `codex-cli 0.114.0`, then Codex exited with `No such file or directory` without identifying the path.
   - **Category:** Execution environment/input path.
   - **Fix:** Log resolved Codex path/version, prompt path and bytes, config path presence, working directory, and a bounded stderr excerpt with paths sanitized.
   - **Expected impact:** Convert an opaque scheduled-audit failure into an immediately actionable one.
   - **Rollback:** Audit should continue to fail closed.

5. **Do not alert on observed Semble contract-test fallbacks.**
   - All 52 fallbacks were `context=contract-test`, target `overflow`, deliberately using a missing binary. Runtime fallbacks were zero.
   - Keep them in test metrics but exclude them from rollout-health alerts.

## AI Memory Health

- **Retrieve health:** 8/8 retrievals selected records—**100% hit rate**. No zero-record or disabled retrievals were observed.
- **Budget pressure:** Average estimated context was **1,388.75/1,400 tokens (99.2%)**, with 31–33 records selected each time.
- **Keyword method:** `llm` 8/8; `plain` 0; `none` 0.
- **Fail-open writes:** Four entries across runs `35779370870` and `35810093343`: failed `record-candidate` and `record-run-event` operations. Retrieval itself remained healthy.
- **Push contention:** Eight successful memory operations required `push_attempts=2`.
- **Recommendation:** Add operation latency, conflict/rebase reason, selected-versus-eligible count, and truncation count. Alert only when retrieval fails or write fail-open repeats on the same task.

## GH API Call Audit

- **Collector gap:** No aggregate endpoint/call/retry/rate-limit counters were supplied. No rate-limit event was found in selected logs.
- **Review sweep is appropriately batched.** Run `35828453162` fetched three candidates and skipped all due to active runs. Current implementation uses one open-PR snapshot plus six status-filtered workflow-run streams—seven paginated command invocations independent of PR count—rather than per-PR calls.
- **Close cleanup follows repository hygiene rules.** It uses two active-run snapshots and GraphQL batches of at most 50 PRs. Run `35828464590` safely preserved three active runs lacking PR linkage.
- **Review metadata reuses early GraphQL state.** Later stages logged “Linked issues already cached from early fetch,” avoiding repeated common-case calls. Non-default-base fallback still adds one GraphQL call.
- **Dispatch payload reliability:** Log summary for successful run `35827869438` records a prior HTTP 422 caused by exceeding GitHub’s ten top-level `client_payload` fields. Keep a contract test that logs `payload_property_count` before dispatch.
- **Recommended telemetry:** Add a wrapper-level summary:
  `GH_API_SUMMARY calls=N retries=N graphql_batches=N rate_limit_events=N endpoints={...}`.
- **Expected reduction:** No large safe reduction is proven. Combining closing-reference and title/body issue hydration could save one GraphQL call on affected review runs.

## Prompt Cache & Memory System

- Repository-level `cache_hit_rate` was unavailable; only three run-level values were collected: **61.4%** (`35804156977`), **41.7%** (`35803994060`), and **0%** (`35802596362`).
- Reported OpenRouter counters contained 37.61M cache-read tokens and 11.16M prompt tokens, a **derived cache-read share of 77.1%**. This is not equivalent to complete-window hit rate because 13/91 calls lacked usage.
- Cache write tokens were 160,692, entirely associated with the stable test run.
- Five context warnings show prompt growth is eroding cache value and risking context overflow.
- **Improvements:**
  - Keep system instructions, rubric, and stable repository context first.
  - Move run IDs, timestamps, current comments, and mutable state after the stable prefix.
  - Emit cache hit rate per model and phase even when some calls lack usage.
  - Add prompt-prefix fingerprinting to distinguish provider misses from genuine prefix variation.
- Expected impact: 5–15% lower uncached prompt processing and lower latency; low implementation risk.

## Orchestrator Health

- `orchestrate_poll` was operationally healthy: **49/49 successes**, p50 302s, p95 1,019.8s.
- Clarification successfully auto-answered issue `4314` in run `35827405453`.
- Trigger fan-out is noisy: clarify, plan, implement, and clarify-response produced **539 of 627 nonterminal outcomes (86%)**, overwhelmingly short skips. Bursts at `06:34–06:35Z` created 38 runs with only a small number doing useful work.
- Scheduled cleanup repeatedly preserved three active runs without PR linkage. This is safe fail-open behavior, but missing linkage weakens cancellation and stall recovery.
- **Smallest safe mitigations:**
  - Gate dispatch upstream using canonical labels/state rather than starting every phase workflow and skipping internally.
  - Emit `ORCHESTRATOR_TRANSITION issue=N from=... to=... reason=... wave=N age_s=N`.
  - Emit a correlation marker containing issue, PR, branch, originating run, and intended phase on every dispatch.
- No explicit wave-progression, deferral-count, conflict-heal retry, or terminal-state aggregates were present; add these before changing state-machine policy.

## Pipeline Flow Bottlenecks

| Stage | Dominant evidence | Bottleneck type | Priority action |
|---|---|---|---|
| Clarify | 151 runs; 141 skipped; p50 1s | Dispatch/skip overhead | Upstream event routing |
| Plan | 140 runs; 131 skipped; successful run `35827484805` took 644s | Dispatch plus model compute | Route before dispatch; track queue/model time separately |
| Implement | 140 runs; 128 skipped; p95 744s; outlier `35818890247` 2,561s | Compute | Emit phase-level model/tool timing |
| Review/autofix | p95 5,359.8s; 49.05M tokens; queue delays up to 7,744s | Queue, model compute, retries | Deduplicate pending runs and quarantine unhealthy reviewer slots |
| Validate/CI | CI p50 2,511.5s; failure only discovered at final step | Compute ordered before deterministic gate | Run reference preflight first |
| Release | `35802596362` 8,108s and parent `35802575310` 8,139s | Parallel failure propagation | Make live jobs depend on static preflight |
| Merge/conflict | Forward merge run `35827322363` completed successfully in 37s | Minor | No current optimization needed |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** late CI integrity validation; review fanout/model stalls; hosted-runner queue congestion.
- **Top failure modes:** missing `scripts/helper.sh` reference; editor empty/no-op; opaque metadata digest publication; security-audit missing-path error.
- **Highest cost driver:** review/autofix, with 99.66% of OpenRouter tokens; Minimax generated 53.8% of review usage.
- **Top three actions:**
  1. Add and enforce the fast script-reference preflight.
  2. Enable reviewer circuit breaking and same-PR pending-run suppression.
  3. Add structured diagnostics for metadata publication, editor attempts, queue delay, and security-audit paths.

## Metrics Appendix

### Overall

| Metric | Value |
|---|---:|
| Window | 2026-09-22 20:16Z – 2026-09-23 06:50Z |
| Total runs | 1,000 |
| Success | 318 (31.8%) |
| Failure | 36 (3.6%) |
| Cancelled | 19 (1.9%) |
| Other/skipped | 627 (62.7%) |
| p50 / p95 duration | 6s / 2,519s |
| Average duration | 314.1s |
| Log-telemetry coverage | 125 runs |
| Wall-clock samples | 121 |
| Wall-clock p50 / p99 | 2,000ms / 8,075,200ms |

### Key Workflow Families

| Family | Runs | Success | Failure | Cancelled/other | p50 / p95 |
|---|---:|---:|---:|---:|---:|
| CI | 36 | 11 | 25 | 0 | 2,511.5s / 2,587.8s |
| Review/autofix | 105 | 79 | 8 | 15 cancelled, 3 skipped | 102s / 5,359.8s |
| Clarify | 151 | 10 | 0 | 141 skipped | 1s / 157s |
| Plan | 140 | 9 | 0 | 131 skipped | 1s / 385.5s |
| Implement | 140 | 8 | 0 | 4 cancelled, 128 skipped | 1s / 744s |
| Clarify respond | 140 | 1 | 0 | 139 skipped | 1s / 9.1s |
| Orchestrate poll | 49 | 49 | 0 | 0 | 302s / 1,019.8s |
| Test/mark stable | 1 | 0 | 1 | 0 | 8,108s |
| Promote main/stable | 1 | 0 | 1 | 0 | 8,139s |

### AI Cost and Cache

| Metric | Value |
|---|---:|
| OpenRouter calls | 91 |
| Prompt tokens | 11,158,343 |
| Completion tokens | 451,799 |
| Cache-read tokens | 37,609,689 |
| Cache-write tokens | 160,692 |
| Total tokens | 49,218,038 |
| Usage unavailable | 13 calls |
| Derived cache-read share | 77.1% |
| Collector aggregate `cache_hit_rate` | unavailable |
| Context warnings | 5 |
| Break-glass events | 0 |
| Codex calls/tokens | 6 / 4,054 |

### Review Model Usage

| Model | Calls | Total tokens | Share |
|---|---:|---:|---:|
| minimax/minimax-m3 | 20 | 26,368,820 | 53.8% |
| deepseek/deepseek-v4-pro | 15 | 7,901,311 | 16.1% |
| qwen/qwen3.7-plus | 14 | 5,485,930 | 11.2% |
| x-ai/grok-4.20 | 14 | 4,801,329 | 9.8% |
| z-ai/glm-5.2 | 6 | 2,833,829 | 5.8% |
| google/gemini-3.1-flash-lite | 6 | 845,939 | 1.7% |

### MCP Telemetry

| System/target | Queries | Logged bytes | Fallbacks | Notes |
|---|---:|---:|---:|---|
| Semble / reviewer-context | 7 | 105,980 | 0 | Average 507ms/query |
| Semble / overflow | 8 | 42,308 | 0 runtime | Average 575ms/query |
| Semble / overflow contract tests | 0 | 0 | 52 | Expected missing-binary tests |
| Serena / no targets observed | 0 | 0 | 0 | Disabled in sampled review logs |
| Other MCP servers observed | 0 | 0 | 0 | None |

| MCP availability target | Probe OK | Probe failed | Probe skipped |
|---|---:|---:|---:|
| Serena / no target emitted | 0 | 0 | 0 |

### AI Memory

| Metric | Value |
|---|---:|
| Retrieve operations | 8 |
| Retrieval hit rate | 100% |
| Average estimated tokens / budget | 1,388.75 / 1,400 |
| Keyword methods | 8 LLM; 0 plain; 0 none |
| Zero-record retrieves | 0 |
| Fail-open entries | 4 |
| Disabled entries | 0 |
| Successful operations requiring two pushes | 8 |

### GH API Evidence

| Workflow/run | Observed pattern | Risk |
|---|---|---|
| Autofix sweep `35828453162` | One PR snapshot plus six workflow/status streams; three active-run skips | Low; batched correctly |
| Cancel cleanup `35828464590` | Two active-run snapshots plus GraphQL batches ≤50 | Low; fail-open preservation |
| Semantic agent `35814503883`, `35820358995` | Cached early linked-issue fetch; one fallback GraphQL for non-default base | Low call volume; diagnostics gap |
| Review `35827869438` | Previous dispatch HTTP 422 from >10 payload properties | Contract regression risk |
| Rate limits | No events observed; collector counts unavailable | Data gap |
