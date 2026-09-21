## Executive Summary

- **Implement preflight failures wasted ~100 runner-minutes and 3.97M Codex tokens** across runs `35488007595`, `35494288283`, and `35499660092`; all failed only after model execution on `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=scripts/helper.sh`. A pre-model support-ledger check would save **31–36 minutes and ~1.324M tokens per affected run**. **Impact: very high; confidence: high.**
- **Review/autofix is the dominant bottleneck:** p95 `8,145.6s` (136 minutes), with 55–108 memory-ledger failures per sampled slow run. Eight completed slow runs produced **666 synchronous substate-write failures**, commonly timing out after 120 seconds. **Estimated saving from local batching/circuit breaking: 10–30 minutes per long run; confidence: medium.**
- **Reviewer-provider degradation is systemic:** `mistralai/mistral-small-2603` failed in all eight structured slow-review samples; one request in run `35511425396` attempted ~269,522 tokens against a 262,144-token context. **Impact: 5–25 minutes and failed calls per affected review; confidence: high.**
- **CI regressions fail too late:** five CI runs (`35495539377`, `35498989364`, `35503818432`, `35508264536`, `35510947848`) spent 15–20 minutes before detecting the same stale budget assertion, `300 != 180`. **Impact: 15–20 minutes faster feedback per regression; confidence: high.**
- **Prompt caching is valuable but incompletely measured:** 168.33M cache-read tokens were 84.3% of 199.58M reported OpenRouter tokens, yet `cache_hit_rate` is null and 30.6% of calls lack usage. There were 18 context-budget warnings. **Impact from safer prompt pruning: estimated 10–20% prompt-token reduction; confidence: medium.**
- **GitHub API hygiene exists in code, but runtime auditing is absent.** No real GitHub rate-limit event appeared in selected full logs, but there are no trustworthy endpoint/call/latency counters. **Impact: diagnostic rather than immediate savings; confidence: high.**

## Speed Optimizations

| Rank | Type | Evidence and root cause | Exact change | Estimated saving | Risk |
|---|---|---|---|---|---|
| 1 | Critical path | Three implement failures ran 1,873–2,185s before discovering missing `scripts/helper.sh` support baseline. | Immediately after support staging, validate every ledger entry against `STAGED_SUPPORT_BASE_DIR`. Emit `IMPLEMENT_SUPPORT_PREFLIGHT entries=N missing=N paths=... support_ref=...`; abort before memory retrieval or Codex. | 31–36 min/run; ~100 min observed | Low |
| 2 | Critical path | Eight slow reviews emitted 666 `ledger_emit_substate` failures; 55–108 per run. `review_run_reviewers.sh` invokes the helper synchronously per lane. | Append substates to run-local JSONL, then flush once per phase. On the first auth/bootstrap failure, open a run-local circuit and retain local telemetry only. Emit `AI_MEMORY_SUBSTATE_SUMMARY attempted=N flushed=N dropped=N reason=... ms=...`. | **Inference:** 10–30 min from review p95 | Low–medium |
| 3 | Critical path | All eight structured slow reviews used six reviewers and had the Mistral slot fail. Runs included 17 retryable failures and four stall kills. | Canary `REVIEWER_CIRCUIT_BREAKER_ENABLED=1`; retain threshold `3` and TTL `1800`. Log `REVIEWER_SLOT_HEALTH model=... failures=N state=open|closed reason=...`. | **Inference:** 5–25 min on degraded runs | Low |
| 4 | Feedback path | Five CI runs surfaced the `300 != 180` budget assertion only after 944–1,223s. | Update the stale test to use the same `E2E_JOB_TIMEOUT_MINUTES` source as the workflow, and run this contract before the large CI suite. Emit budget components in `CI_BUDGET_CONTRACT`. | 15–20 min faster failures | Low |
| 5 | Recurring overhead | Poll run `35546180313` took 293s; start and completion memory pushes each consumed about 47–48s. | Reuse one memory checkout and flush buffered start/completion events in one final push; failure trap should flush `phase_started` plus `phase_failed`. | ~45–50s per poll | Medium |
| 6 | Queue/dispatch | Twelve long cancelled `Internal: AI Review & Autofix` runs were paired with successful semantic-agent runs created 14–19s later; paired durations total 65,831s. | Correlate dispatches by PR/head SHA and wait for both wrapper and engine visibility before fallback dispatch. Emit `REVIEW_RUN_LINK parent_run=... child_run=... pr=... head_sha=... source=...`. | Reduced pending-run replacement and queue noise | Low |

Micro-optimization: Copilot review is healthy—49/49 successful, p50 `385s`—so optimize it only after the review/autofix and CI critical paths.

## Cost Optimizations

1. **Prevent invalid implement executions.**  
   The three identical staged-support failures consumed `3,972,495` Codex tokens—**60.0% of all 6,620,824 Codex tokens** in the assembled window. The preflight above is the largest low-risk cost reduction.

2. **Batch AI-memory writes.**  
   Long reviews repeatedly attempted remote substate pushes that timed out or failed authentication. In runs `35511425396` and `35519204369`, final writes retried 16 times before failing with missing Git credentials. Batch to one push and stop retries after an authentication-class error. This saves runner minutes rather than model tokens.

3. **Enforce model-aware prompt limits.**  
   Eighteen `CONTEXT_BUDGET_WARN` events occurred, with sampled prompts at 186K–233K tokens. Run `35511425396` requested approximately 237,522 input plus 32,000 output tokens. Compute:
   `maximum_input = context_window - requested_output - safety_margin`, then prune oldest comments/check history before invocation. Emit component counts via `PROMPT_COMPONENT_BUDGET phase=... static=... diff=... comments=... memory=... semble=... total=... limit=...`.

   **Estimated saving:** 10–20% of 29.61M direct prompt tokens, or roughly 3–6M tokens.  
   **Quality risk:** medium; never trim the current diff, issue requirements, or latest review findings.

4. **Use reviewer tiers for eligible, non-sensitive changes.**  
   `REVIEW_TIER_RESOLVER_ENABLED=false` and `REVIEWER_RISK_TIER_ENABLED=0` caused six-model review panels. Enable the resolver for non-sensitive paths; the existing always-full regex preserves full review for `scripts/`, workflows, prompts, and contracts. Eligible runs can reduce reviewer calls from six to three—up to 50%—without weakening sensitive-path reviews.

5. **Preserve cache-friendly prefixes.**  
   Cache-read tokens represent 84.3% of reported OpenRouter tokens, a strong positive signal. Avoid moving volatile PR metadata into the static prefix. Add a stable-prefix hash and per-call `cache_status=hit|miss|unknown`; do not infer hit rate from token totals.

6. **Semble should remain enabled, with yield measurement.**  
   It made 63 queries returning 469,210 bytes—about 7.45KB/query—with four runtime budget fallbacks. It is likely reducing raw file expansion, but context warnings show it does not control total prompt growth. Add `raw_bytes`, `selected_bytes`, and `prompt_bytes_avoided` to `SEMBLE_QUERY`. Do not enable Serena solely for cost reduction: it was disabled and emitted no runtime telemetry.

## Reliability Improvements

| Rank | Failure evidence | Category | Exact fix and logging | Expected impact / rollback |
|---|---|---|---|---|
| 1 | Three of four implement failures shared `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING`. | Contract/version skew | Fix the staged-support manifest and add pre-model ledger validation. | Could eliminate 75% of observed implement failures. Rollback by retaining the old post-model guard. |
| 2 | Eight slow reviews logged 666 memory substate failures; four runs emitted fail-open memory telemetry. | Dependency/authentication | Authenticate memory remote once, batch events, and circuit-break on auth errors. Include `reason`, `rc`, and attempts in telemetry. | Removes a masked broken write path while retaining fail-open execution. |
| 3 | Run `35546298657` failed after 312s because `LINKED_ISSUE_METADATA_FILE` was unset. | Workflow/helper interface mismatch | Introduce a versioned required-env manifest shared by workflow and script; validate immediately after runtime initialization. | Prevents late setup failures. Fail closed before expensive work. |
| 4 | Mistral failed in 8/8 slow-review summaries; sampled failures included upstream rate limiting and context overflow. | Provider/context | Enable the health breaker and classify `context_length_exceeded` separately from retryable server errors. | Fewer doomed retries; disable through the existing variable if quality regresses. |
| 5 | Five CI runs repeated the same stale `180` expectation against a 300-minute workflow timeout. | Test/config drift | Derive both validation and test expectations from one budget constant and run it first. | Removes deterministic rerun churn. |
| 6 | Security audit run `35499174216` failed with `captured_path_error=No such file or directory`, then only reported `Codex exited nonzero`. | Missing diagnostic context | Validate all generated prompt/input paths before Codex and log the exact missing path and argv role. | Faster root-cause isolation; only one run, so confidence is low. |

`BREAK_GLASS` count was zero, so there is no observed policy-override pressure. The 18 context warnings instead indicate prompt-size risk.

Semble’s 24 contract-test fallbacks are expected test behavior. Four runtime fallbacks out of 63 queries (6.3%), all budget-related, look like healthy rare fail-open behavior rather than a broken rollout. Add whether the raw-content fallback was actually injected.

Serena recorded no query, fallback, or probe event. Logs show `SERENA_ENABLED=false`; this is not a rollout failure, but an explicit `SERENA_PROBE result=skipped reason=disabled target=...` would remove ambiguity.

## AI Memory Health

- **Retrieval effectiveness, selected full logs:** 12/12 retrieves returned records—**100% hit rate**.
- Average retrieved size was **1,448 tokens against a 1,467-token average budget**, or 98.7% occupancy.
- Keyword extraction: `llm=8`, `plain=4`, `none=0`.
- No retrieve returned zero records; no `enabled:false` retrieve was observed.
- Four review runs (`35486128483`, `35504713879`, `35511425396`, `35519204369`) emitted eight fail-open record events.
- Successful memory events required at most two push attempts; four events used two attempts.
- The more serious issue is substate emission: 55–108 failures per completed slow review. Inspected failures include both 120-second timeouts and 16-attempt unauthenticated pushes.

**Recommendation:** preserve retrieval, but decouple remote writes from the reviewer critical path. Add `reason`, `failure_class`, `elapsed_ms`, `push_attempts`, and `circuit_state` to failed memory telemetry.

No `promote`, `compact`, or `finalize-task` telemetry appeared in selected logs, so retention/compaction health cannot be assessed. Verify those operations emit telemetry when scheduled.

## GH API Call Audit

Runtime call counts are **not collected**, so any raw count derived from log text would be unreliable because Actions logs echo scripts and documentation.

- Repository policy correctly requires batched GraphQL and cycle-local caches in `agents.md`, `CLAUDE.md §15`, and `unattended_system_instructions.md §14`.
- `scripts/orchestrate_poll_process.sh` uses `_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`, `ACTIVE_WORKFLOW_ISSUES`, and `STALL_MANAGED_LINKED_PR_CACHE`; these are good patterns.
- No actual `GitHub API rate limit hit` event appeared in selected full logs.
- The review sweep statically performs separate queued/in-progress/pending snapshots for both `internal-review.yml` and `review_autofix.yml`. **Inference:** fetching unfiltered recent runs once per workflow and filtering locally could reduce six status calls to two per tick—a 67% reduction, approximately 192 calls/day on the 30-minute schedule. Retain per-status fallback if the unfiltered page is saturated.

Add instrumentation inside `gh_retry`, `gh_retry_to_file`, `_safe_gh_jq`, `gh_api_json_to_file`, and `curl_gh_api`:

- `GH_API_CALL helper=... method=... endpoint_class=... attempt=N result=ok|error|rate_limited ms=N cache=hit|miss`
- `GH_API_SUMMARY calls=N retries=N rate_limits=N errors=N by_endpoint=...`

Normalize paths and omit query values, bodies, tokens, repository secrets, and PII.

## Prompt Cache & Memory System

- `cache_hit_rate`: **unavailable**
- OpenRouter cache-read tokens: `168,325,878`
- Cache-write tokens: `0`
- OpenRouter total tokens: `199,582,371`
- Usage available: `136/196` calls (69.4%); unavailable: `60/196` (30.6%)
- Context warnings: `18`

The large cache-read share indicates useful prefix reuse, but it is not equivalent to a hit rate. Zero cache-write tokens may mean creation usage is unreported rather than absent.

Recommended changes:

1. Emit `prefix_hash`, `cache_status`, `cache_read_tokens`, `cache_creation_tokens`, and `usage_unavailable_reason` per call.
2. Keep system instructions, stable rubrics, and tool contracts before volatile PR metadata.
3. Place comments, timestamps, run IDs, and retry diagnostics in a dynamic suffix.
4. Enforce the model-aware hard input limit before requests, not only the existing 70% warning.
5. Record Semble’s `raw_bytes_saved`; retain raw fallback when query output is empty or budget-exhausted.
6. Reduce memory retrieval budgets only as a micro-optimization: 1.4–1.6K memory tokens are small relative to 186K–233K review prompts.

## Orchestrator Health

- Poller reliability is generally healthy: 108 successes, zero failures, and 11 cancellations across 119 runs; p50 `285s`.
- Recent run `35546180313` completed successfully, but two memory-event pushes consumed about 95 seconds combined.
- Stall recovery worked in implement run `35538013085`, which succeeded after logging that implementation stalled and was re-triggered.
- Sweep deduplication worked: recent sweeps repeatedly skipped PRs `#4174` and `#4176` because active runs existed.
- However, 12 long cancelled wrapper runs paired with successful engine runs strongly suggest duplicate-entrypoint/concurrency replacement. This also keeps sweeps in no-op `active_run` cycles.
- Semble availability changed from false during early setup to true after installation in run `35546180313`; final availability should be summarized once to prevent misleading partial snapshots.

Track these indicators:

- `ORCHESTRATOR_CYCLE_SUMMARY issues=N actions=N noops=N api_calls=N memory_ms=N process_ms=N queue_ms=N`
- active-run age and status by PR
- parent/child run correlation
- stall-recovery attempts and outcomes
- final MCP availability, not only early environment snapshots
- merge-train examined/released counts

## Pipeline Flow Bottlenecks

| Stage | Observed state | Dominant bottleneck | Priority fix |
|---|---|---|---|
| Clarify | p50 `1s`; 134/139 other/skipped | Gate-heavy, not compute-bound | No optimization needed |
| Plan | p50 `1s`; 125/130 other/skipped | Gate-heavy | No optimization needed |
| Implement | Active runs reach 879–2,205s; three late identical failures | Post-model staged-support validation | Pre-model support preflight |
| Review/autofix | p95 `8,145.6s`; six-reviewer panels; memory timeouts; provider failures | Compute, retry, prompt growth, memory writes | Memory batching + provider breaker + prompt cap |
| Copilot review | p50 `385s`, 49/49 successful | Secondary model latency | Observe only |
| Validate/CI | CI p50 `2,116s`, failure rate 25.7% | Large serial suite and late contract checks | Fast-fail deterministic contracts |
| Orchestrate | Poll p50 `285s`; frequent no-op/active-run cycles | Setup, memory pushes, dispatch dedup | Stage timing and single memory flush |
| Merge/conflict | Recent merge-train summary examined `0`, released `0` | Insufficient evidence | Add cycle summary before tuning |

End-to-end priority: **implement preflight → memory batching/auth circuit → reviewer provider/context controls → CI fast-fail → dispatch correlation**.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix p95 136 minutes; CI p50 35 minutes; poller p50 4.75 minutes.
- **Top failure modes:** staged-support baseline missing in three implement runs; repeated stale CI budget contract; memory-ledger timeouts/auth failures; one missing review metadata env variable.
- **Highest-cost drivers:** 199.58M OpenRouter tokens in review/autofix and 3.97M Codex tokens wasted by three deterministic implement failures.
- **Top actions:**
  1. Add and enforce `IMPLEMENT_SUPPORT_PREFLIGHT`.
  2. Batch reviewer substates locally and circuit-break memory writes after the first authentication-class failure.
  3. Enable reviewer health breaking and enforce model-aware prompt limits before request dispatch.

## Metrics Appendix

### Run outcomes

| Scope | Runs | Success | Failure | Cancelled | Other | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| All workflows | 1,000 | 402 (40.2%) | 15 (1.5%) | 45 (4.5%) | 538 | 8s | 4,081s |
| CI | 35 | 26 | 9 (25.7%) | 0 | 0 | 2,116s | 2,441.9s |
| Implement | 130 | 5 | 4 (3.1%) | 0 | 121 | 1s | 1,193.4s |
| Review/autofix | 153 | 117 | 1 (0.7%) | 34 (22.2%) | 1 | 34s | 8,145.6s |
| Orchestrate poll | 119 | 108 | 0 | 11 (9.2%) | 0 | 285s | 514.5s |
| Copilot reviewer | 49 | 49 | 0 | 0 | 0 | 385s | 579s |
| Validation refresh | 1 | 1 | 0 | 0 | 0 | 1,250s | 1,250s |
| Security audit | 1 | 0 | 1 | 0 | 0 | 87s | 87s |

Terminal-outcome success rate excluding skipped/other runs: **87.0%**.

### AI cost and latency

| Metric | Value |
|---|---:|
| Codex calls / tokens | 58 / 6,620,824 |
| OpenRouter calls | 196 |
| OR prompt / completion / total | 29,613,344 / 1,651,208 / 199,582,371 |
| OR cache read / write | 168,325,878 / 0 |
| OR usage available / unavailable | 136 / 60 |
| Cache hit rate | Not collected |
| Wall-clock samples | 122 |
| Wall-clock p50 / p99 | 267,500ms / 8,957,700ms |
| Break-glass count | 0 |
| Context-budget warnings | 18 |

### MCP telemetry

| System / target | Queries | Bytes | Fallbacks | Notes |
|---|---:|---:|---:|---|
| Semble, all assembled runs | 63 | 469,210 | 28 | 24 contract-test; 4 runtime |
| Semble `overflow`, selected deep logs | 35 | Not split by target | 27 | 24 contract; 3 runtime |
| Semble `reviewer-context`, selected deep logs | 8 | Not split by target | 0 | No observed fallback |
| Serena | 0 | 0 | 0 | Disabled; no runtime evaluation possible |
| Other MCP servers observed | 0 | 0 | 0 | None |

| MCP target availability | probe_ok | probe_failed | probe_skipped | Gap |
|---|---:|---:|---:|---|
| Serena, target not emitted | 0 | 0 | 0 | Emit explicit skipped probe when disabled |
| Semble | N/A | N/A | N/A | Current schema has queries/fallbacks but no probe counters |

### AI memory, selected full logs

| Metric | Value |
|---|---:|
| Retrieve operations | 12 |
| Retrieves with records | 12 (100%) |
| Average estimated tokens / budget | 1,448 / 1,467 |
| Keyword method | `llm=8`, `plain=4`, `none=0` |
| Zero-record retrieves | 0 |
| Fail-open record events | 8 across 4 runs |
| Maximum successful push attempts | 2 |
| Slow-review substate failures | 666 across 8 runs |

### GH API telemetry

| Metric | Value |
|---|---|
| Runtime call count | Not collected |
| Endpoint latency | Not collected |
| Cache hit/miss | Not collected |
| Actual GitHub rate-limit events in selected logs | 0 |
| Confirmed batching controls | GraphQL candidate/linked-PR helpers and cycle-local caches |
| Highest-priority collection step | Add `GH_API_CALL` and `GH_API_SUMMARY` markers |

**Coverage gap:** the assembled context has telemetry or summaries for 124 runs, while `summary.json` contains full deep-log telemetry for 29 selected runs. Successful-run sampling is 7%, so latency/provider conclusions based on slow-run logs are high-confidence for outliers but not population-wide distributions.
