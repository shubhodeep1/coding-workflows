## Executive Summary

- **Review/autofix is the dominant bottleneck.** Its p95 is **8,277.5s**, with **31/151 runs cancelled**. PR #4035 spawned runs `34153553863` and `34153571772` 15 seconds apart; the former waited **8,443s** before executing for another **6,309s**. A shared supersession gate could remove entire duplicate cycles. **Impact: 1–3 hours per duplicate; confidence: high.**
- **CI has a systemic 26.7% failure rate**: 12 failures across 45 runs. Eight repeatedly failed the same two orchestrator-poll tests; four failed prompt byte-equivalence with only a bare `AssertionError`. **Impact: prevent repeated 20–26 minute diagnostic cycles; confidence: high.**
- **GH API pressure is operationally material.** Sweep run `34170258012` hit HTTP 403 rate limiting and then passed the error string into `jq`. Poll run `34173481973` spent an unlogged **614.5s** before processing its first issue in a section containing seven sequential label-list calls. **Impact: up to 10 minutes per poll plus fewer rate-limit failures; confidence: high on timing, medium on exact blocked endpoint.**
- **Reported AI spend is large but measurement is not fully reliable.** The broader context reports **107.0M OpenRouter total tokens over 147 calls** and **2.69M Codex tokens over 18 calls**. Exported-folder totals are lower, and run `34162529930` duplicated 15 OpenRouter records across parent/child log entries. **Impact: cost totals may be overstated by at least 8% in the exported view; confidence: high.**
- **Memory and Semble are mostly healthy.** Logical deep-dive memory retrieval hit **9/9**, averaging **1,410 estimated tokens against a 1,422-token budget**. Semble recorded 28 queries, 273,772 bytes, and only one runtime fallback. Serena emitted no valid query or probe telemetry. **Confidence: high for selected logs, medium globally.**
- **Prompt-size pressure is real:** six `CONTEXT_BUDGET_WARN` events, including 159,196/200,000 tokens in run `34153571772`. No `BREAK_GLASS` events were observed. **Impact: lower timeout and overflow risk; confidence: high.**

## Speed Optimizations

1. **[Critical path] Suppress superseded review runs after concurrency waits**
   - **Evidence:** PR #4035 runs `34153553863` and `34153571772`; PR #4029 runs `34160171011` and `34160185793`; PR #4035 runs `34162514241` and `34162529930`.
   - **Root cause:** Different review entry workflows enqueue equivalent work; serialized runs remain eligible after a newer sibling finishes.
   - **Exact change:** Before dispatch and immediately after codex-agent admission, compare PR/head SHA with the latest completed review-family run. Exit successfully with `AUTOFIX_SUPERSEDED_AFTER_QUEUE` when a newer equivalent run completed.
   - **Estimated savings:** 90–180 minutes and one full reviewer/editor cycle per duplicate.
   - **Risk:** Low–medium; fail open if the comparison API is unavailable.

2. **[Critical path] Bound retries for the two unhealthy reviewer families**
   - **Evidence:** In eight slow review runs, Kimi-k3 had 23 calls with eight usage-unavailable results; Minimax-m3 had 24 calls with eight unavailable results. Run `34162529930` spent **4,365s** in reviewer models versus **574s** in the editor.
   - **Root cause:** Slot-local retries continue despite several other reviewer families succeeding.
   - **Exact change:** Reduce the retryable slot limit from three to two when at least four other primary slots succeeded; open the health circuit after two consecutive retryable failures.
   - **Estimated savings:** 10–40 minutes on affected reviews.
   - **Risk:** Low–medium; retain the third attempt when reviewer quorum is not met.

3. **[Critical-path outlier] Batch standalone stall candidate discovery**
   - **Evidence:** Poll `34173481973` had a silent 614.5-second interval before issue processing. The relevant code issues seven sequential `gh issue list` requests.
   - **Root cause:** Per-label enumeration combined with suppressed retry/backoff diagnostics.
   - **Exact change:** Extend the existing GraphQL candidate helper to return all relevant open labeled issues in one paginated request, with the current REST sequence as fail-open fallback.
   - **Estimated savings:** Six API calls per poll and up to ten minutes during quota pressure.
   - **Risk:** Low with fallback retained.

4. **Batch poll start/end memory writes**
   - **Evidence:** Poll runs spent 43–47s recording start and another 42–44s recording completion.
   - **Exact change:** Spool the start event locally; flush start and terminal events in one end-of-run push, including an `always()` failure path.
   - **Estimated savings:** 40–47s per poll, roughly 15–18% of a normal 255–263s execution.
   - **Risk:** Medium; preserve crash visibility through the failure flush.

5. **Move applicability checks before runner allocation**
   - **Evidence:** Integration-readiness runs `34168625416` and `34162513955` queued approximately 120s and 390s, then completed the non-applicable check in about three seconds.
   - **Exact change:** Add a job-level branch-pattern `if` for workflows applicable only to `orchestrator/project-*`.
   - **Estimated savings:** 2–6.5 minutes per irrelevant invocation.
   - **Risk:** Low.

**Micro-optimizations:** Semble installation consumed only 8–9s and Codex installation 3–4s in recent pollers; these are not current priorities.

## Cost Optimizations

1. **Eliminate superseded full reviews**
   - Individual slow review runs consumed **5.2M–17.1M reported OpenRouter tokens**. Skipping only proven-superseded runs removes the full cost without reducing review coverage.
   - **Estimated savings:** One complete panel/editor bill per duplicate.
   - **Quality risk:** Low when keyed by PR, immutable head SHA, and completed successor.

2. **Stop repeated unavailable-model attempts**
   - All 16 usage-unavailable calls in the logical slow-run sample came from Kimi-k3 or Minimax-m3.
   - Apply the conditional two-attempt limit described above and retain four healthy model families.
   - **Estimated savings:** Up to 16 failed attempts in the sampled slow runs; token usage for unavailable calls was not reported.
   - **Quality risk:** Low–medium.

3. **Compact warned prompts before fan-out**
   - `CONTEXT_BUDGET_WARN` examples:
     - `34143516477`: 140,523/200,000, 70.26%.
     - `34153571772`: 159,196/200,000, 79.6%.
     - `34160185793`: four warnings at 186,218–187,462 tokens.
   - Trim oldest comments, prior-run prose and duplicate context while preserving current diff, unresolved findings and output contract.
   - **Estimated savings:** 10–25% prompt input on warned runs.
   - **Quality risk:** Medium; emit per-section byte/token contributions before enforcing.

4. **Improve prompt-cache prefix stability**
   - Broader telemetry shows roughly **75% cache-read share**, but aggregate `cache_hit_rate` is null because 19 calls lacked usage data.
   - Put invariant persona/checklist/output-contract blocks before memory, PR text, diff and Semble content behind a rollback flag.
   - **Estimated savings:** 5–10% of non-cached input on low-hit calls.
   - **Quality risk:** Medium; instrument prefix hashes first.

5. **Keep Semble enabled, but measure usefulness**
   - Semble averaged about **9.8KB/query**, with one runtime budget fallback. Selected overflow queries replaced large-file expansion with approximately 105KB of targeted content; one response was only 29 bytes.
   - Add `chunks_returned`, `source_bytes`, `selected_bytes`, `truncated` and query hash fields.
   - **Estimated savings:** Currently unquantifiable; telemetry indicates Semble is more likely reducing context than adding material cost.
   - **Quality risk:** None for logging changes.

6. **Cap oversized Copilot review prompts**
   - Run `34168628941` built a **103,808-token** prompt and fired its cost-JIT signal at **$0.5129**.
   - Apply deterministic file/diff prioritization before prompt construction.
   - **Estimated savings:** Potentially 20–40% on similarly large PRs.
   - **Quality risk:** Medium; evidence is one sampled run.

No evidence supports lowering orchestrator judge reasoning from `xhigh`: recent pollers configured it but did not emit judge-call token telemetry.

## Reliability Improvements

1. **Make sweep API failures safe and diagnosable**
   - **Failure:** `34170258012` received HTTP 403, then failed with `Cannot index string with string "draft"`.
   - **Root cause category:** Rate-limit handling and response validation.
   - **Fix:** Use `gh_retry_to_file`; validate that paginated output contains JSON arrays before `jq`. If active-run snapshots are unavailable, skip dispatch for that tick rather than treating the active map as empty.
   - **Expected impact:** Eliminates this failure class and avoids duplicate dispatch during quota outages.
   - **Rollback:** Environment flag can restore legacy fail-open dispatch; scheduled retry remains the safe default.

2. **Emit tracebacks for custom CI test failures**
   - **Failure:** Eight CI runs reported blank failures for:
     - `test_review_blocked_merged_followup_retargets_to_integration_branch`
     - `test_review_blocked_merged_followup_keeps_default_base_when_no_integration_context`
   - **Root cause category:** Behavior/fixture call-order regression, inferred from hard-coded API response sequences.
   - **Fix:** Print exception type, `repr(exception)`, traceback, and assertion source line. Add `failure_type` and `failure_message` to `TEST_CASE_EVENT`.
   - **Expected impact:** Substantially lower MTTR and repeated reruns.
   - **Rollback:** Additive logging only.

3. **Add prompt mismatch diagnostics**
   - Four runs (`34135334159`, `34135394224`, `34135687309`, `34136074449`) failed byte equality with no prompt name or diff.
   - Emit prompt path, byte lengths, SHA-256 values, first differing offset and a bounded unified diff.
   - The September 8 local comparison currently passes, but historical failures remain unauditable.
   - **Rollback:** None required.

4. **Reduce cancellation churn**
   - Review/autofix cancellation rate was **20.5%**. Several cancelled runs waited hours before their first codex step.
   - Apply pre-dispatch and post-queue supersession checks rather than relying solely on eventual cancellation.
   - **Expected impact:** Fewer terminal cancellations and less queue congestion.
   - **Rollback:** Fail open on lookup uncertainty.

5. **Instrument MCP availability consistently**
   - Semble’s one runtime fallback among 28 successful queries is healthy rare fail-open behavior; 78 other fallbacks were explicit contract tests.
   - Serena had zero valid query, fallback or probe events despite logs showing unavailable/disabled states.
   - Emit `SERENA_PROBE target=... result=skipped|failed reason=...` on every bootstrap path.
   - **Expected impact:** Distinguishes disabled rollout from broken availability.
   - **Rollback:** Additive telemetry.

6. **Improve memory write fail-open records**
   - Run `34160185793` lost both a `record-candidate` and terminal `record-run-event`, while correctly allowing the workflow to succeed.
   - Add `reason`, `error_class`, branch/head and deferred-retry status.
   - **Expected impact:** Preserves fail-open reliability while exposing continuity gaps.

No `BREAK_GLASS` events occurred. The six context warnings indicate prompt-size pressure, not policy/rubric override pressure.

## AI Memory Health

- **Logical selected retrievals:** 9.
- **Hit rate:** 100% (`records_selected > 0` in 9/9).
- **Average estimated tokens:** 1,410.
- **Average budget:** 1,422.2; approximately 99.1% utilized.
- **Keyword method:** `llm` 8, `plain` 1, `none` 0.
- **Zero-record retrieves:** 0.
- **`enabled:false`:** 0.
- **Failed fail-open operations:** two in run `34160185793`.
- **Push retries:** 8/30 push-bearing logical events needed two attempts; maximum was two.
- **Observed operations:** 24 `record-run-event`, 9 `retrieve`, 8 `record-candidate`, 14 `write_lessons_learned`, one command check/claim, and one `finalize-task`.
- **Coverage gap:** No selected `promote` or `compact` telemetry. Verify that memory-maintenance runs emit these operations.
- All observed `write_lessons_learned` events recorded `count=0`; this is not a failure, but logging the number of candidates evaluated would distinguish “nothing learned” from “nothing eligible.”

## GH API Call Audit

| Hotspot | Current structure | Exact change | Estimated reduction |
|---|---|---|---:|
| Reviewer PR-state watchdog | One PR GET per reviewer approximately every 90s; six parallel reviewers | One shared pass-level poller touching the existing close sentinel | ~83% |
| Standalone stall labels | Seven sequential `gh issue list` calls per poll | One paginated GraphQL inventory plus REST fallback | ~86% for this phase |
| Autofix sweep | Minimum one PR list plus six workflow/status lists per tick | Wrap all calls in `gh_retry`; optionally use three repo-wide status snapshots filtered locally | Up to 43% |
| Active-run cache miss | Cached scan missed run `34170778456`; direct lookup recovered it in poll `34173481973` | Record miss reason and merge successful fallback into the cycle cache | Avoid repeated fallbacks |

Positive hygiene: `scripts/orchestrate_poll_process.sh` already uses the canonical `_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`, `ACTIVE_WORKFLOW_ISSUES` and `STALL_MANAGED_LINKED_PR_CACHE` patterns required by `agents.md`.

**Telemetry gap:** Exact API call counts, retries, reset sleeps and endpoint latency are not emitted. Add a shared marker from `gh_retry`:

`GH_API_CALL operation=<normalized> attempt=<n> rc=<n> http_status=<n> latency_ms=<n> remaining=<n> reset_epoch=<n> cache=<hit|miss|na>`

Emit one step-end aggregate to avoid excessive log volume.

## Prompt Cache & Memory System

- Broader telemetry: **26.46M prompt**, **79.49M cache-read**, **1.05M completion** tokens; observed cache-read share is approximately **75%**.
- `cache_hit_rate` is globally null because 19/147 OpenRouter calls lacked complete usage.
- Available run-level rates ranged from **48.53%** (`34154707019`) to **85.82%** (`34153553863` logical view).
- Cache creation/write tokens were always zero; determine whether this means provider non-reporting or no explicit creation.
- Dynamic memory, PR body, diff and Semble blocks currently precede some invariant checklist material, which may fragment prefixes. Instrument `stable_prefix_hash`, `dynamic_context_hash` and section token counts before reordering.
- Memory retrieval is reliable but nearly saturates its budget every time. Add selected-record score distribution and duplicate-record counts before lowering the 1,400–1,600 token budgets.
- Semble appears beneficial: low query volume, modest returned bytes and one healthy runtime budget fallback.
- Serena provided no measurable tool replacement; probe telemetry is required before evaluating its efficiency.
- The six context warnings show prompt growth is eroding context headroom even where cache reuse is strong.

## Orchestrator Health

- **103/103 poll runs succeeded**, with p50 **258s** and p95 **421.6s**.
- Guards worked correctly:
  - Polls `34171118389` and `34171556850` skipped retriggering issue #4017 because of a fresh PR push.
  - Poll `34173481973` found active review run `34170778456` through a direct check after the cache missed it, preventing an empty commit from invalidating active work.
- Recurring pain point: issue #4017 remained in `ai:done` for 1,124 minutes and required branch-name freshness fallback on consecutive polls. Cache successful branch fallback results for the remainder of the cycle and emit a counter.
- Poll `34173481973` had a 614.5-second silent pre-processing interval. Add `ORCH_SECTION_TIMING` markers around candidate discovery, action-run loading, state reconstruction, stall recovery and sweeps.
- No sampled clarification loop, conflict-heal retry, judge execution or terminal-state failure was observed. Confidence about those paths is therefore limited.
- Track:
  - section elapsed time;
  - actions-cache miss confirmed by direct lookup;
  - stall decisions by reason;
  - wave age/current wave;
  - judge invocations and repeat fingerprints;
  - dispatches deferred due quota or active work.

## Pipeline Flow Bottlenecks

| Stage | Evidence | Bottleneck type | Priority fix |
|---|---|---|---|
| Clarify | 99/105 skipped; p50 1s | Trigger/gating noise | Preserve job-level skips |
| Plan | 92/97 skipped; p50 1s | Trigger/gating noise | Preserve job-level skips |
| Implement | One cancelled 10,882s run with 12 Codex calls and 1.32M tokens | Compute/cancellation | Add attempt wall-clock and supersession logging |
| Review/autofix | p95 8,277.5s; 31 cancellations | Queue, model retry, duplicate work | Supersession gate and conditional retry cap |
| Validate | One 347s run | Insufficient sample | No change |
| Orchestrate poll | p50 258s; one 614s silent interval | API and memory-write overhead | Batch discovery and memory writes |
| CI | p50 1,541s; 12/45 failures | Compute and opaque regression failures | Tracebacks and focused early contracts |
| Merge/conflict | Recent sweeps scanned 2–3 PRs and fixed zero | No sampled dominant overhead | Continue existing guards |

Queueing is independently significant: all nine recent runs with system timing reported busy hosted runners; median maximum queue delay was **42.9s**, maximum **178.8s**. Slow review jobs experienced multi-hour concurrency waits.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix p95 8,277.5s; CI p50 1,541s; poll p50 258s.
- **Top failure modes:** repeated orchestrator test regressions, opaque prompt-byte mismatch, and unhandled sweep rate limiting.
- **Highest-cost drivers:** repeated six-model reviewer passes, Kimi/Minimax retries, duplicate review variants, and 140K–187K-token prompts.
- **Top actions:**
  1. Add PR/head-SHA supersession checks across both review entry workflows.
  2. Centralize reviewer PR-state polling and batch standalone stall discovery.
  3. Add traceback, prompt-diff, GH API, and section-timing telemetry.

## Metrics Appendix

### Outcomes

| Runs | Success | Failure | Cancelled | Other/skipped | p50 | p95 |
|---:|---:|---:|---:|---:|---:|---:|
| 863 | 437 (50.6%) | 13 (1.51%) | 34 (3.94%) | 379 (43.9%) | 9s | 4,470s |

### Core workflow families

| Family | Runs | Success | Failure | Cancelled | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|
| CI | 45 | 33 | 12 | 0 | 1,541s | 1,703.6s |
| Review/autofix | 151 | 117 | 1 | 31 | 89s | 8,277.5s |
| Orchestrate poll | 103 | 103 | 0 | 0 | 258s | 421.6s |
| Implement | 97 | 5 | 0 | 3 | 1s | 684.8s |
| Copilot review | 47 | 47 | 0 | 0 | 199s | 351.8s |

### Telemetry coverage discrepancy

| View | Runs with telemetry | OR calls | OR total tokens | Semble queries/bytes | Wall p50 / p99 |
|---|---:|---:|---:|---:|---:|
| Broader assembled context | 121 | 147 | 107,002,159 | 28 / 273,772 | 249s / 11,755s |
| Exported folder summary | 33 | 135 | 99,408,016 | 26 / 251,734 | 1,502s / 13,911s |

Run `34162529930` alone contains a known duplicate lower bound of 15 OpenRouter calls, 8.35M total tokens and three Semble queries because parent/child log timestamps differ slightly. Add event IDs or normalize timestamp prefixes before deduplication.

### Cost and cache

| Metric | Broader value |
|---|---:|
| Codex calls / tokens | 18 / 2,691,523 |
| OpenRouter prompt | 26,464,911 |
| OpenRouter completion | 1,051,584 |
| OpenRouter cache read | 79,489,549 |
| OpenRouter cache write | 0 |
| OpenRouter calls unavailable | 19/147 |
| Aggregate `cache_hit_rate` | N/A |
| `CONTEXT_BUDGET_WARN` | 6 |
| `BREAK_GLASS` | 0 |

### MCP

| Server/target | Queries | Bytes | Runtime fallbacks | Contract fallbacks | Probe OK/failed/skipped |
|---|---:|---:|---:|---:|---:|
| Semble, all | 28 | 273,772 | 1 | 78 | N/A |
| Semble `reviewer-context`, selected logical | 8 | 115,834 | 0 | 0 | N/A |
| Semble `overflow`, selected logical | 15 | 104,699 | 1 | — | N/A |
| Serena, no target emitted | 0 | 0 | 0 | 0 | 0 / 0 / 0 |

**Other MCP servers observed:** none.

### AI memory, logical selected events

| Retrievals | Hit rate | Avg estimated/budget | Keyword methods | Failed fail-open | Pushes requiring retry |
|---:|---:|---:|---|---:|---:|
| 9 | 100% | 1,410 / 1,422.2 | `llm` 8, `plain` 1 | 2 | 8/30 |

### GH API structural audit

| Pattern | Current minimum | Proposed minimum |
|---|---:|---:|
| Reviewer close-state checks per interval | 6 | 1 |
| Standalone label discovery per poll | 7 | 1 |
| Autofix sweep inventory/status calls | 7 | 4, potentially |
| Runtime call-count telemetry | unavailable | one aggregate per step |
