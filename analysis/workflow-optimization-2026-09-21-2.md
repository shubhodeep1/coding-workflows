## Executive Summary

- **CI contract drift was the dominant failure mode:** 19/37 CI runs failed (51.4%), consuming 6.0 runner-hours. Eleven deep dives showed the identical late assertion `300 != 180` in `test_e2e_smoke_job_has_headroom_for_all_phase_budgets`. Current HEAD passes the 23-test contract. **Impact: eliminate the observed CI failure cluster; confidence: high.**
- **Review/autofix has severe tail latency:** p50 268s versus p95 6,132s; 28 cancellations accumulated 19.8 wall-clock hours, including 20 cancellations before the first step. Run `35565709369` waited about 67 minutes for a hosted runner. **Impact: 15–45 minutes saved on slow reviews through workload reduction; confidence: high.**
- **Review model fan-out is the largest AI cost driver:** 156 calls consumed 138.47M total tokens; slow runs made 12–17 calls across two review passes and six model families. **Estimated saving: 35–55M total tokens with adaptive second-pass execution; confidence: medium.**
- **Six review failures shared one configuration defect:** `Collect PR metadata` lacked `LINKED_ISSUE_METADATA_FILE`; deep-dive runs `35551938072`, `35552937934`, `35553550091`, and `35555067924` show the exact error. Current code initializes the variable. **Impact: remove all six observed failures; confidence: high.**
- **Semble fail-open behavior was healthy:** 37 queries logged 342,482 bytes; 72/73 fallbacks were contract tests and the sole runtime fallback was `reason=budget-exhausted`. Serena was disabled and had no query/probe telemetry. **Confidence: high.**
- **Observability remains incomplete:** aggregate `cache_hit_rate` is null, 39/156 model calls lack usage data, and GH API calls have no executed-call counters. **Impact: materially better capacity and regression diagnosis; confidence: high.**

## Speed Optimizations

1. **Critical path — fail fast on workflow-budget contracts**
   - **Evidence:** all 19 CI failures ended at `Orchestrate poll process unit tests`; the failing test itself ran in about 4.5s after roughly 17–20 minutes of preceding work.
   - **Root cause:** workflow timeout and contract expectation changed independently.
   - **Exact change:** run `tests/test_ci_poll_test_sharding.py` in an early dedicated contract step before the expensive test suite. Emit `CI_CONTRACT_FAILURE contract=e2e_budget actual=300 expected=180`.
   - **Savings:** up to 19 minutes per incompatible commit; 6.0 hours in this window.
   - **Risk:** low.

2. **Critical path — make the second review pass adaptive**
   - **Evidence:** slow runs used 12–17 model calls; `ENABLE_REVIEWER_TWO_PASS=true`. Review p95 was 102 minutes and the maximum was 158 minutes.
   - **Root cause:** full multi-model, two-pass review runs regardless of first-pass confidence or PR risk.
   - **Exact change:** skip pass two when pass one has schema-valid consensus, no high-severity findings, and the diff is below a conservative risk threshold. Preserve two passes for workflow, security, migration, and large changes.
   - **Savings:** estimated 15–45 minutes on qualifying slow runs.
   - **Risk:** medium; initially shadow-log the decision without skipping.

3. **Critical path — reject superseded PR heads before model startup**
   - **Evidence:** 20 review runs were cancelled before their first step; current PR-backed concurrency intentionally does not cancel in-progress work.
   - **Root cause:** commits and dispatches can queue faster than hosted-runner capacity.
   - **Exact change:** immediately compare the requested head SHA with the current PR head and exit before model calls if stale. Log queue age, requested SHA, current SHA, and superseding run ID.
   - **Savings:** avoids an entire 1–2 hour review when stale work eventually starts.
   - **Risk:** low with fail-open behavior on API failure.

4. **Micro-optimization — short-circuit idle poll cycles**
   - **Evidence:** `orchestrate_poll` had 89 runs with p50 291s; several recent runs lasted 289–310s with no recorded transition.
   - **Exact change:** exit early only when there are zero active projects, merge-train entries, deferred checks, and deadline-bound waits.
   - **Savings:** estimated 2–4 minutes per truly idle poll.
   - **Risk:** medium; requires an explicit cycle-state summary before enabling.

## Cost Optimizations

1. **Adaptive model fan-out**
   - **Evidence:** 138.47M total tokens, 156 calls, and six model families in slow reviews.
   - **Change:** use a smaller first-pass quorum for low-risk PRs and launch additional reviewers only on disagreement, parse failure, or high-risk files.
   - **Estimated savings:** 25–40%, or approximately 35–55M total tokens. Dollar savings will be smaller because much of the total is discounted cache-read traffic.
   - **Quality risk:** preserve the full ensemble for sensitive changes and audit skipped calls.

2. **Enable conservative uninteresting-file filtering**
   - **Evidence:** `REVIEWER_FILTER_UNINTERESTING_ENABLED=false`; nine prompts reached 186k–233k tokens.
   - **Change:** enable filtering with existing migration/database exemptions, and always retain changed executable files plus dependency manifests.
   - **Estimated savings:** 10–20% of the 19.89M non-cache prompt tokens, roughly 2–4M tokens.
   - **Quality risk:** low-to-medium; compare findings in shadow mode first.

3. **Retain Semble, but instrument usefulness**
   - **Evidence:** 37 queries averaged about 9.3KB each—small relative to review prompts. Only one runtime fallback occurred.
   - **Assessment:** Semble is likely reducing raw file expansion, but context warnings show it is not sufficient alone. One query returned only 156 bytes, indicating occasional low-value retrieval.
   - **Change:** log selected chunk count, deduplicated bytes, prompt bytes replaced, and whether retrieved text was included.
   - **Estimated savings:** enables removal of low-value queries; direct current overhead is minor.
   - **Quality risk:** low.

4. **Improve usage completeness before model-price tuning**
   - **Evidence:** 39/156 calls (25%) lacked usage data.
   - **Change:** emit provider, model, retry count, latency, and an explicit `usage_missing_reason`.
   - **Risk:** none; model-routing savings cannot be measured reliably until this gap closes.

## Reliability Improvements

1. **Guard required review environment as a manifest**
   - **Evidence:** six failures at `Collect PR metadata`; exact error: required `LINKED_ISSUE_METADATA_FILE` unset.
   - **Fix:** validate all required paths immediately after runtime initialization and emit one structured list of missing variables.
   - **Impact:** would have prevented all six review failures.
   - **Rollback/fail-open:** metadata required for correctness should fail fast; optional memory/Semble paths may fail open.

2. **Prevent late CI contract regressions**
   - **Evidence:** 19 identical failures and likely propagation into `test_and_mark_stable` run `35548345586`. The parent promotion run `35548334532` then failed after 5,975s.
   - **Fix:** early budget-contract test plus a release log linking child run ID, conclusion, failure job, and failure step.
   - **Impact:** avoids approximately 3.3 hours across the failed promotion/gate pair.
   - **Confidence:** release propagation is an inference because those full logs were not selected.

3. **Improve cancellation diagnostics**
   - **Evidence:** 28 review cancellations; 20 before the first step.
   - **Fix:** record `concurrency_group`, queue age, cancelling/superseding run ID, PR, head SHA, and whether any model call began.
   - **Impact:** distinguishes healthy pending-run replacement from runner starvation or stuck concurrency.
   - **Rollback:** logging-only.

4. **Repair opaque AI-memory fail-open events**
   - **Evidence:** eight `ok=false, fail_open=true` events across runs `35511425396`, `35519204369`, `35538971404`, and `35548430644`.
   - **Fix:** add `reason`, `exit_code`, operation duration, repository/ref, retry count, and stderr category.
   - **Impact:** memory degradation becomes actionable instead of silently masked.
   - **Rollback:** retain fail-open behavior.

5. **Treat MCP fallback types separately**
   - **Evidence:** 73 Semble fallbacks: 72 contract-test and one runtime. No break-glass events; nine context-budget warnings.
   - **Fix:** alert only on runtime fallback rate, not contract-test events. Suggested threshold: more than 5% runtime fallback over a rolling window.
   - **Impact:** avoids false rollout alarms while detecting real degradation.

## AI Memory Health

- **Retrieve hit rate:** 7/7 selected deep-dive retrieves returned records, or **100%**.
- **Context use:** average `estimated_tokens` was **1,372 of 1,400**, or 98% of budget.
- **Keyword method:** `llm=7`, `plain=0`, `none=0`.
- **Zero-record retrieves:** none observed.
- **Disabled retrieves:** none emitted in selected logs.
- **Fail-open failures:** eight events across four slow review runs, split between `record-candidate` and `record-run-event`.
- **Push retries:** most reported one attempt; maximum observed was two.

Recommendation: preserve retrieval, but target 80–90% of the token budget through deduplication so unusual tasks retain headroom. Add actionable error fields to failed write operations.

## GH API Call Audit

- **Review sweep is well batched:** current implementation performs one paginated open-PR snapshot plus three status snapshots for each of two workflows—seven logical reads per tick, independent of PR count. Run `35570805532` evaluated four PRs and skipped all four as active without duplicate dispatch.
- **Post-merge validation repeats metadata reads:** run `35570329063` refreshed linked issues via GraphQL, supported REST fallback, then inspected issue labels; it ended with no validation dispatch for PR `#4191`.
  - **Recommendation:** reuse the gate’s linked-issue snapshot when non-empty and refresh only when absent or stale.
  - **Estimated reduction:** 1–2 calls per post-merge run.
- **Exact executed-call counts are unavailable:** logs contain scripts and endpoints, but no authoritative call-completion telemetry. No actual GH rate-limit event was found in selected deep dives.
- **Required logging addition:** emit one line per completed request:
  - `GH_API_CALL endpoint_class=pulls method=GET attempt=1 status=200 ms=143 items=4 cache=miss`
  - Aggregate calls, retries, 4xx/5xx, and rate-limit sleeps by workflow step.

## Prompt Cache & Memory System

- Aggregate `cache_hit_rate` is unavailable; one run reported **69.37%**.
- Cache-read tokens were **117.38M of 138.47M total tokens (84.8%)**, indicating substantial prefix reuse.
- Cache-write tokens were zero; this may be provider reporting behavior rather than proof no cache entries were created.
- Nine context warnings reached ratios of **0.71–0.89**, with one model-specific ratio of **1.07** against a 200k context window.
- **Inference:** stable prefixes are generally effective, but large dynamic PR/reviewer context is eroding headroom.

Recommendations:

1. Keep system instructions, tools, rubric, and output schema in a stable prefix; append PR-specific data last.
2. Emit `prefix_hash`, static/dynamic byte counts, model context window, cache-read/write tokens, and miss reason per call.
3. Enable conservative file filtering and deduplicate memory/Semble text before prompt assembly.
4. Trigger an automatic context reduction path at 70%, not merely a warning.

## Orchestrator Health

- Polling was reliable: **88/89 successful**, p50 **291s**, p95 **368s**.
- Merge-train state progressed correctly: run `35569978809` reported PR `#4192` blocked by `#4191`; run `35570329086` later released and dispatched `#4192`.
- Sweep deduplication worked: recent sweeps skipped four or five active PRs without failures.
- Most phase workflows were intentional no-ops: clarify 125/133 skipped, plan 117/124, implement 112/123, clarify-response 122/123.
- Failure-heal ran 63 times and skipped every run; this is cheap but obscures whether it is healthy or simply never eligible.

Add `ORCHESTRATOR_CYCLE_SUMMARY` with tracked issues, transitions, deferrals, merge blockers, active runs, no-op reason, API calls, sleep time, and next wake reason.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Priority fix |
|---|---|---|---|
| Clarify/plan | Trigger fan-out/no-op runs | Most runs skipped in 1–9s | Add structured skip reasons; optimization is low priority |
| Implement | Sparse evidence | p95 687.5s; one unselected failure | Ensure full logs for every failure |
| Review/autofix | Model compute and queueing | p95 6,132s; 12–17 calls; 28 cancellations | Adaptive ensemble and stale-head gate |
| CI | Long serial test gate and late contract check | p50 1,231s; 19/37 failures | Move budget contract to the start |
| Validate/release | Child-workflow waiting and failure propagation | validation refresh ~1,111s; release pair ~5,900s each | Emit child linkage and terminate promptly |
| Merge train | Blocker wait | PR `#4192` blocked by `#4191` | Existing release behavior worked; add blocker-age metric |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** CI’s 20–42 minute lint job; review p95 above 100 minutes; hosted-runner waits.
- **Top failures:** 19 CI budget-contract failures; six missing review metadata environment failures; release-chain propagation.
- **Highest-cost driver:** review/autofix, with 138.47M total tokens and multi-model two-pass execution.
- **Top three actions:**
  1. Keep the now-fixed budget contract as an early fail-fast gate.
  2. Introduce shadow-mode adaptive second-pass/model quorum selection.
  3. Add structured queue, GH API, memory-failure, and child-workflow telemetry.

## Metrics Appendix

### Run Outcomes

| Scope | Runs | Success | Failure | Cancelled | Skipped/other | Failure rate | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Overall | 1,000 | 392 | 28 | 33 | 547 | 2.8% | 8s | 2,509s |
| CI | 37 | 18 | 19 | 0 | 0 | 51.4% | 1,231s | 2,512s |
| Review/autofix | 149 | 113 | 6 | 28 | 2 | 4.0% | 268s | 6,132s |
| Orchestrate poll | 89 | 88 | 0 | 1 | 0 | 0% | 291s | 368s |
| Copilot reviewer | 49 | 49 | 0 | 0 | 0 | 0% | 366s | 553s |

### AI Cost and Cache

| Metric | Value |
|---|---:|
| OpenRouter calls | 156 |
| Usage available / unavailable | 117 / 39 |
| Prompt tokens | 19,886,142 |
| Completion tokens | 1,206,747 |
| Total tokens | 138,470,943 |
| Cache-read tokens | 117,382,916 |
| Cache-write tokens | 0 |
| Aggregate `cache_hit_rate` | unavailable |
| Observed run cache hit rate | 69.37% |
| Context-budget warnings | 9 |
| Break-glass events | 0 |
| Wall-clock p50 / p99 | 13,000ms / 8,534,910ms |

### MCP Telemetry

| Server/target | Queries | Bytes | Fallbacks | Probe OK / failed / skipped | Notes |
|---|---:|---:|---:|---:|---|
| Semble, aggregate | 37 | 342,482 | 73 | n/a | 72 contract-test; 1 runtime |
| Semble, reviewer-context sample | 7 | 102,280 | 0 | n/a | Deep-dive logs only |
| Semble, overflow sample | 22 | 155,772 | 45 | n/a | 44 contract-test; 1 runtime |
| Serena, target not emitted | 0 | 0 response bytes | 0 | 0 / 0 / 0 | Disabled in observed workflows |
| Other MCP servers | 0 | 0 | 0 | — | None observed |

### GH API Summary

| Workflow/step | Observed pattern | Call-count status |
|---|---|---|
| Review autofix sweep | Batched PR inventory plus workflow/status snapshots | Seven baseline logical reads; pages not logged |
| Post-merge validation | GraphQL, optional REST fallback, per-issue label lookup | Exact executed count unavailable |
| Cancel on PR close | Run discovery, cancellation loop, merge-train release | Exact executed count unavailable |
| Rate-limit events | None in selected deep dives | Broader coverage unavailable |

**Material data gaps:** assembled telemetry covers 123 log-parsed runs, while the full-log folder contains 28 downloaded telemetry runs. Cache-hit coverage, exact GH API counts, Serena probes, and full logs for the failed promotion, stable gate, and implement run are missing.
