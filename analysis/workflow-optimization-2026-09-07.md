## Executive Summary

- **Cost telemetry is overcounted by duplicate aggregate/granular logs.** Run `34078200709` reports 24 OpenRouter calls and 9.07M tokens, but full logs contain 12 unique calls and 4.54M tokens. Corrected window estimate: **61 calls / 28.45M tokens**, versus reported 73 / 32.98M. Impact: 15.9% accounting correction. **Confidence: high.**
- **Reviewer fan-out is the dominant critical path.** Run `34078200709` spent ~59.4 minutes in reviewers; pass 2 ran six models despite a `0 LOC` scoped diff, consuming 2.40M tokens and ~29.7 minutes. Skipping empty-scope pass 2 could halve that run’s AI cost. **Confidence: high.**
- **CI is sequentially bottlenecked.** CI p50 is 1,497s. Run `34082598014` spent 603s in orchestrator tests, 148s in integration-ahead tests, and 113s in collector coverage. Parallel jobs could save approximately 8–11 minutes. **Confidence: high.**
- **Poller overhead is mostly infrastructure rather than orchestration.** Run `34083198228` spent 92s on two AI-memory writes, 42s checking out the repository, and ~25s installing/indexing Semble; processing took 76s. Reusing one memory checkout and lazily initializing Semble could save 45–70s per ordinary poll. **Confidence: high for sampled run; medium fleet-wide.**
- **Reliability is generally strong but reviewer-slot degradation is common.** Four of five fully metered review runs had an empty-output, non-retryable failure, or stall kill; quorum allowed every productive run to finish. **Confidence: high.**
- **The sole hard failure was path-related.** Security Audit run `34021424100` failed during Codex execution with `No such file or directory (os error 2)`. **Confidence: high.**

## Speed Optimizations

1. **Skip pass 2 when its scoped review surface is empty — critical path.**
   - **Evidence:** Run `34078200709`: `diff is 0 LOC`; pass 2 still ran six reviewers from approximately 03:39:10–04:08:51.
   - **Root cause:** `review_run_reviewers.sh` adjusts reasoning by diff size but does not skip a zero-diff pass.
   - **Exact change:** Before pass 2, require non-zero scoped diff or actionable ledger/comment/check-run changes. Emit `REVIEWER_PASS2_SKIPPED reason=empty_scoped_diff`.
   - **Estimated savings:** ~29.7 minutes and 2.40M tokens for the observed run.
   - **Risk:** Low if unresolved comments, check failures, and ledger entries remain explicit override conditions.

2. **Split the CI lint job into parallel heavy-test lanes — critical path.**
   - **Evidence:** Run `34082598014`: total 1,275s; orchestrator tests 603s, integration-ahead tests 148s, collector coverage 113s.
   - **Root cause:** Independent test groups run sequentially in one job.
   - **Exact change:** Create parallel `orchestrator-tests`, `workflow-analysis-tests`, and `static-and-contract-tests` jobs, preserving a single aggregate required check.
   - **Estimated savings:** 8–11 minutes per CI run.
   - **Risk:** Medium; required-check names and shared setup must remain backward-compatible.

3. **Reuse the AI-memory checkout and lazily initialize Semble in pollers — critical path.**
   - **Evidence:** Poll run `34083198228`: memory start 47.9s, memory end 44.3s; Semble installation/indexing ~25s, with no Semble query.
   - **Root cause:** Memory operations clone independently; Semble initializes whenever work exists rather than when a query is required.
   - **Exact change:** Add an `AI_MEMORY_SESSION_DIR` reused by all operations in a job; initialize Semble on first query.
   - **Estimated savings:** 45–70s per no-judge poll; potentially 1–2 runner-hours across 115 cycles.
   - **Risk:** Low with existing fail-open behavior.

4. **Enforce the check-run collector’s total deadline.**
   - **Evidence:** Run `34078200709` waited the full 300s using sleeps of 20/40/80/80/78s. Cancelled run `34078160722` remained in this step at 481s.
   - **Root cause:** Retry/API time is not fully bounded by the nominal wait timeout.
   - **Exact change:** Calculate a monotonic deadline and pass the remaining budget into every API request; snapshot immediately when exhausted.
   - **Estimated savings:** Up to 181s on the observed cancellation.
   - **Risk:** Low; the collector already fails open.

5. **Treat skipped phase workflows as a micro-optimization.**
   - Clarify, plan, implement, and clarify-response produced 85 skipped runs but consumed only 256s total. Trigger-level filtering may reduce UI noise, but it is not a priority.

## Cost Optimizations

1. **Fix duplicate telemetry before making budget decisions.**
   - Run `34078200709` has both an aggregate job log and granular step logs. `cost_audit.py` counted both.
   - Exclude the aggregate parent when granular children exist; do not deduplicate solely by line text.
   - Add `telemetry_source_mode` and `duplicate_candidate_count`.
   - **Savings:** No runtime savings, but corrects 4.54M tokens and 12 calls in this window.
   - **Quality risk:** None.

2. **Eliminate empty-scope second passes.**
   - Across five metered runs, pass 2 consumed **17.89M tokens—62.9% of corrected OpenRouter usage**.
   - Run `34078200709` alone could have avoided 2.40M tokens.
   - Preserve pass 2 for changed scope, unresolved feedback, failed checks, or sensitive forced-review conditions.
   - **Quality risk:** Low under those guards.

3. **Add a real per-model prompt-budget gate.**
   - Run `34017571910` sent 2.735M prompt tokens to `mistralai/mistral-small-2603`; that reviewer then failed non-retryably.
   - Yet `context_budget_warn_count=0`.
   - Emit `PROMPT_BUDGET_V1 model=… estimated_tokens=… window=… ratio=… action=compact|skip|continue`; compact with Semble before dispatch.
   - **Potential savings:** Up to 2.86M tokens for the anomalous call.
   - **Quality risk:** Low–medium; five other reviewers remain available.

4. **Enable self-trigger suppression.**
   - Set `AUTOFIX_SKIP_SELF_TRIGGERED=true`. Paired wrapper/direct review runs produced long pending cancellations, including `34013621055` (5,627s) and `34023239644` (5,064s).
   - The direct continuation and 30-minute sweep remain safety nets.
   - **Savings:** Primarily queue/control-plane waste; potentially one duplicate model cycle if concurrency visibility fails.
   - **Quality risk:** Low.

5. **Improve cache consistency by model.**
   - Corrected weighted cache-read rate is **59.4%**. Per-model rates ranged from 0% for Mistral to ~75% for Qwen and DeepSeek.
   - Moving from 59.4% to 70% would shift approximately 3.0M input tokens from uncached to cached processing at the same workload.
   - Log a stable-prefix digest and provider cache capability before altering prompt order.
   - **Quality risk:** None for prefix stabilization.

**Semble:** 15 unique production queries returned 242,390 bytes in 8.6s total. This is low overhead and appears to replace unbounded file expansion with bounded chunks. Keep it enabled. One 62-byte result suggests adding `result_chunks` and `source_count` to identify low-value queries.

**Serena:** Zero queries, fallbacks, or probes; `SERENA_ENABLED=false` in review. It incurred no cost but provided no tool-call replacement.

## Reliability Improvements

1. **Repair Security Audit path diagnostics.**
   - **Evidence:** Run `34021424100`, `Run security audit`: Codex exited nonzero with `os error 2`.
   - **Category:** Runtime path resolution.
   - **Fix:** Validate every prompt/config/output path immediately before Codex execution and emit `SECURITY_AUDIT_EXEC_V1` with sanitized path existence and stderr tail.
   - **Impact:** Prevent or immediately localize the current 100% security-audit failure rate.
   - **Rollback/fail-open:** Retain current hard failure for security audits; logging is additive.

2. **Place reviewer runtime context inside the permitted workspace.**
   - **Evidence:** Runs `34023251321`, `34027140154`, and `34078200709` had external-directory requests for `/tmp/.../runtime_context/*` auto-rejected immediately before empty reviewer output.
   - **Category:** Sandbox/permission mismatch.
   - **Fix:** Copy read-only runtime context beneath the workspace or add a narrowly scoped read permission.
   - **Impact:** Could remove three of five observed reviewer anomalies.
   - **Rollback:** Revert to current quorum-based fail-open behavior.

3. **Make check-run polling strictly deadline-aware.**
   - **Evidence:** Timeout warning in `34078200709`; cancellation in the same step in `34078160722`.
   - **Impact:** Fewer late cancellations and bounded API usage.
   - **Fail-open:** Continue with `collection_status=timeout`.

4. **Enable reviewer health caching after a shadow period.**
   - Run `34017571910` killed a stalled Minimax attempt after 600s and retried successfully; DeepSeek and Minimax produced empty outputs in three other runs.
   - Shadow-log health decisions for one week, then enable `REVIEWER_CIRCUIT_BREAKER_ENABLED=1`.
   - Rollback is the existing variable toggle.

5. **Fix telemetry-source duplication.**
   - Besides tokens, run `34078200709` reports two Semble calls/30,002 bytes where only one call/15,001 bytes exists. CI run `34082598014` similarly doubles five contract-test fallbacks to ten.
   - Add a regression fixture covering aggregate-plus-granular archives.

`BREAK_GLASS=0` and `CONTEXT_BUDGET_WARN=0`; no policy override pressure was observed. The latter is not reassuring until reviewer prompt-budget coverage is fixed.

## AI Memory Health

- **Retrievals:** 6; **hit rate:** 100%.
- **Records selected:** 29–30 per retrieval; no zero-result retrievals.
- **Average estimated tokens:** 1,386 of 1,400 budget—**99.0% utilization**.
- **Keyword method:** 100% `llm`; no `plain` or `none`.
- **Disabled entries:** 0.
- **Fail-open entries:** 1 expected `finalize-task` event in run `34082600066`, reason `no_linked_issues`.
- **Push behavior:** 18 operations succeeded in one attempt; poll-start in `34083198228` required two attempts. No high retry counts.
- **Concern:** Retrieval is consistently saturated at its token ceiling. Add candidate-count, lowest-selected score, retrieval duration, and dropped-record count before reducing the budget.
- **Latency concern:** Memory writes consumed ~92s in poll run `34083198228`. Add `clone_ms`, `commit_ms`, `push_ms`, and `checkout_reused` fields to `AI_MEMORY_TELEMETRY`.

No sampled `promote`, `compact`, or processed-command telemetry appeared; those operations were not exercised in the inspected workflows.

## GH API Call Audit

Exact call counts are unavailable: `RUN_COST_TELEMETRY_FIELDS` in `scripts/cost_audit.py` contains no GitHub API counters.

**Observed patterns:**

- **Healthy reuse:** Review run `34078200709` cached `LINKED_ISSUES_JSON`; the sweep snapshots active runs before per-PR filtering; poll run `34083198228` detected an existing autofix run and avoided duplicate conflict dispatch.
- **High-volume family:** `orchestrate_poll` ran 115 times. Successful calls are silent, so its API footprint cannot be quantified.
- **Check-run loop:** Run `34078200709` performed five wait cycles over 300s. Each snapshot is at least one API request.
- **Sweep fan-out:** When candidates exist, `review_autofix_sweep.yml` performs one PR listing plus three status snapshots for each of two review workflows—at least seven logical GETs before dispatch.
- **Failure signal:** Poll run `34083198228` received a non-retryable HTTP 422 from `pulls/4019/update-branch`; this was a real merge conflict, not a rate-limit event.
- **Rate limits:** No production 403/429 event was found.

**Changes:**

1. Add `GH_API_CALL_V1 endpoint_class=… method=… attempt=… status=… elapsed_ms=… cache=hit|miss`.
2. Emit one `GH_API_SUMMARY_V1` per job with calls, retries, rate-limit sleeps, and endpoint-class counts.
3. In the sweep, fetch global queued/in-progress/pending runs once per status and filter both workflow IDs locally: six status requests become three, approximately a **43% reduction** for candidate-bearing sweeps.
4. Preserve the repository rule: extend `_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`, and cycle-local caches before adding calls.

## Prompt Cache & Memory System

- Collector `cache_hit_rate` is `null`; corrected token-weighted cache-read rate is **59.4%**.
- Observed run rates: **45.4%–70.8%**.
- Run `34078200709` had a 232,854-byte static prefix and 339,944-byte editor prompt, showing that cache-friendly static-first assembly is active.
- Model behavior is fragmented:
  - Qwen: 75.3%
  - DeepSeek: 75.1%
  - Kimi: 71.6%
  - Minimax: 65.0%
  - Grok: 42.1%
  - Mistral: 0%
- Add `PROMPT_CACHE_PREFIX_V1 phase=… model=… prefix_sha256=… static_bytes=… dynamic_bytes=… provider_cache_supported=…`.
- Keep timestamps, run IDs, temporary paths, and current issue state after the stable cache boundary.
- Memory retrieval is effective but fills 99% of its budget; reserve 10–15% headroom if relevance-score telemetry shows low-value tail records.
- Cache write tokens were reported as zero while reads were substantial. Record cache-write state as `unsupported|missing|zero|positive` rather than collapsing all cases to zero.

## Orchestrator Health

- **115/115 poll runs succeeded**; p50 246s, p95 268.9s. One 764s outlier occurred in run `34073418128`.
- Full run `34083198228` processed tracking issue `#3965`, encountered a real conflict on PR `#4019`, and correctly skipped duplicate dispatch because review was already active.
- No actual judge task was observed in the inspected poll run; poller telemetry recorded zero OpenRouter calls. Judge activity therefore cannot be distinguished from “judge configured but not invoked.”
- Semble ended available and indexed in `34083198228`; earlier generated summaries incorrectly captured only its initial `false` defaults.
- No clarification-loop, wave-terminalization, or recovery-budget escalation was observed.
- Add `ORCH_CYCLE_SUMMARY_V1 tracking=N issues_scanned=N judge_calls=N stall_actions=N conflict_actions=N gh_calls=N phase_ms={…}`.
- Also emit a finalized `SEMBLE_PROBE` after indexing so summaries do not mistake initialization state for final availability.

## Pipeline Flow Bottlenecks

| Stage | Dominant overhead | Evidence | Priority fix |
|---|---|---|---|
| Clarify | Mostly control-plane skips | 22/23 skipped; p50 1s | No immediate action |
| Plan | One expensive execution | Run `34074071104`, 704s | Add phase timers and prompt composition |
| Implement | No executed implementation in window | 20/20 skipped | Collection gap, not a bottleneck |
| Review/autofix | Reviewer fan-out and repeated iterations | p95 5,333s; PR `#4013` reached iterations 1–5 | Skip empty pass 2; prompt caps |
| Check collection | Fixed 300s wait | Run `34078200709` | Deadline-aware snapshot |
| Validate | Long single-stage execution | `34076893536`, 1,095s | Add subphase timings |
| CI | Sequential heavy tests | p50 1,497s | Parallel job lanes |
| Orchestrate | Memory writes, checkout, recurring setup | Run `34083198228` | Reuse memory clone; lazy Semble |
| Merge/conflict | Manual fallback path | Run `34082598056`, README conflict, PR `#4019` | Preserve fail-open; add conflict telemetry |

End-to-end order: **reviewer pass 2 → CI serialization → poll memory/setup → check-run wait → merge-conflict overhead**.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** review/autofix (51.2% of elapsed time), poller (30.8%), CI (13.9%).

**Top failure modes:** Codex path failure in security audit; reviewer sandbox permission denials; stalled/empty reviewer outputs; queued duplicate review lanes.

**Highest-cost drivers:** six-model two-pass review at high/xhigh reasoning, uncached Mistral prompts, repeated PR `#4013` autofix iterations.

**Top three actions:**

1. Skip zero-scope reviewer pass 2.
2. Parallelize CI’s heavy test groups.
3. Reuse AI-memory checkouts and add strict prompt/API/phase summary telemetry.

## Metrics Appendix

### Run outcomes

| Runs | Success | Failure | Cancelled | Skipped/other | p50 | p95 |
|---:|---:|---:|---:|---:|---:|---:|
| 314 | 222 (70.7%) | 1 (0.32%) | 6 (1.91%) | 85 (27.1%) | 12s | 1,466s |

Non-skipped success rate: **96.94%**.

### Primary workflow families

| Family | Runs | Success | Fail | Cancel | Skipped | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 68 | 62 | 0 | 6 | 0 | 8s | 5,333s |
| orchestrate_poll | 115 | 115 | 0 | 0 | 0 | 246s | 269s |
| ci | 9 | 9 | 0 | 0 | 0 | 1,497s | 1,559s |
| copilot reviewer | 8 | 8 | 0 | 0 | 0 | 126.5s | 176.6s |
| validation_refresh | 1 | 1 | 0 | 0 | 0 | 1,095s | 1,095s |
| security_audit | 1 | 0 | 1 | 0 | 0 | 84s | 84s |
| clarify | 23 | 1 | 0 | 0 | 22 | 1s | 10.9s |
| plan | 22 | 1 | 0 | 0 | 21 | 1s | 11s |
| implement | 20 | 0 | 0 | 0 | 20 | 1s | 10.1s |

### Cost telemetry

| Metric | Collector-reported | Deduplicated |
|---|---:|---:|
| OpenRouter calls | 73 | 61 |
| Prompt tokens | 12,878,027 | 11,398,380 |
| Completion tokens | 423,896 | 395,051 |
| Cache-read tokens | 19,686,213 | 16,659,243 |
| Total tokens | 32,983,855 | 28,448,428 |
| Usage unavailable | 1 | 1 |
| Codex calls/tokens | 3 / 2,027 | unchanged |
| Weighted cache-read rate | 60.5% | 59.4% |

### Additive telemetry

| cache_hit_rate | wall p50 | wall p99 | Samples | BREAK_GLASS | CONTEXT_BUDGET_WARN |
|---:|---:|---:|---:|---:|---:|
| null | 9,000ms | 5,646,800ms | 111 | 0 | 0 |

### Semble and Serena

| System/target | Calls | Bytes | Time | Fallbacks |
|---|---:|---:|---:|---:|
| Semble `reviewer-context` | 5 | 91,031 | 2,721ms | 0 |
| Semble `overflow` | 10 | 151,359 | 5,844ms | 0 |
| Semble total, unique | 15 | 242,390 | 8,565ms | 19 contract-test; 0 runtime |
| Serena | 0 | 0 | 0 | 0 |

| MCP target | probe_ok | probe_failed | probe_skipped | Note |
|---|---:|---:|---:|---|
| Semble/orchestrate_poll | not emitted | not emitted | not emitted | Final availability was healthy in run `34083198228` |
| Serena | 0 | 0 | 0 | Disabled/no production probe |
| github-mcp-server | — | — | — | Connected, 0 invocations in sampled Copilot runs |
| playwright | — | — | — | Connected, 0 invocations in sampled Copilot runs |

### AI memory

| Retrieves | Hit rate | Avg selected | Avg tokens/budget | Keyword method | Fail-open | Push retries |
|---:|---:|---:|---:|---|---:|---:|
| 6 | 100% | 29.7 | 1,386 / 1,400 | 100% LLM | 1 expected | One operation needed attempt 2 |

### Material data gaps

- No GH API call-count fields or endpoint rollups.
- No prompt-prefix fingerprints or model context-window ratios.
- No aggregate judge-cycle counter.
- Collector summaries may capture initialization values instead of final MCP availability.
- Cost parsing currently double-counts archives containing both aggregate and granular logs.
