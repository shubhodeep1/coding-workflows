## Executive Summary

- **CI is systematically timing out:** 23/32 runs (71.9%) were cancelled near the 30-minute job limit; 22 stopped in `lint / Orchestrate poll process unit tests`. Sharding plus a temporary 45-minute ceiling should eliminate most cancellations and save 8–15 minutes of critical-path latency. **Confidence: high.**
- **Review/autofix dominates operations:** it consumed 75.4% of recorded wall time; CI plus review/autofix account for 93.1%. Seven deep-dived successful reviews averaged 105 minutes of execution. **Confidence: high.**
- **Reviewer configuration is unnecessarily expensive:** each iteration launches six models twice, and pass 1 runs at `xhigh` despite repository documentation specifying `medium`. Changing only pass 1 to `medium` should save roughly 15–30 minutes and 20–35% of reviewer cost while retaining the `xhigh` second pass. **Confidence: high.**
- **The consolidator rollout is broken but masked:** all 7 sampled slow reviews failed immediately with `Not inside a trusted directory and --skip-git-repo-check was not specified`, then continued fail-open. **Impact:** lost synthesis, parser coverage, and lessons-learned generation. **Confidence: high.**
- **Editor retries are systemic:** all 7 sampled reviews succeeded only on attempt 3; 12 attempts failed manifest validation and 2 were stall-killed. Fixing the output contract could save 10–18 minutes per normal review and more than 30 minutes in stall cases. **Confidence: high.**
- **Cost and API observability are incomplete:** OpenRouter reports 143 calls but no token/cache values, `cache_hit_rate` is unavailable, no `GH_API_CALL` telemetry exists, and failed-run folders contain metadata without step logs. **Confidence: high.**

## Speed Optimizations

### 1. Critical path: shard the orchestrator-poll CI tests

- **Evidence:** 23/32 CI runs were cancelled; 22 failed at `Orchestrate poll process unit tests`, generally after 1,817–1,839 seconds. Examples: runs `33070953400` and `33060444929`. Successful runs still took 1,211–1,743 seconds, including `33060427213` and `33059180763`.
- **Root cause:** `.github/workflows/ci.yml` places the large remaining test suite in one 30-minute `lint` job.
- **Exact change:** split the derived test list into 2–3 deterministic matrix shards, retain a final aggregate `CI` check, and temporarily raise the current job ceiling to 45 minutes during rollout.
- **Diagnostic logging:** emit `TEST_TIMING name=... elapsed_ms=... outcome=... shard=...` and a heartbeat with completed/remaining counts.
- **Estimated savings:** 8–15 minutes p95; removes approximately 11.6 hours of cancelled CI wall time in this window.
- **Risk:** medium for sharding; low for the temporary timeout increase.

### 2. Critical path: run reviewer pass 1 at medium reasoning

- **Evidence:** the 7 slow reviews spent an average 4,317 seconds in reviewer passes. `scripts/review_run_reviewers.sh` launches pass 1 with `xhigh`, while `probably_unnecessary_but_read_if_stuck.md` documents pass 1 as `medium`.
- **Root cause:** hard-coded reasoning drift; six reviewers run twice at high cost.
- **Exact change:** introduce `REVIEWER_PASS1_REASONING`, defaulting to `medium`; leave pass 2 at the existing scheduled `xhigh`.
- **Estimated savings:** 15–30 minutes and 20–35% of reviewer spend per full review.
- **Risk:** low-to-medium because the deep second pass remains unchanged.

### 3. Critical path: repair editor manifest retries

- **Evidence:** all 7 sampled runs succeeded on attempt 3. Runs `32978817666`, `32980421457`, `32987735882`, `33004184186`, `33041351113`, and `33056159542` each had two manifest-validation failures. Run `33015375913` had two stall-killed attempts.
- **Root cause:** inferred output-contract mismatch; the same retry pattern across unrelated PRs is unlikely to be PR-specific.
- **Exact change:** inject an exact machine-generated manifest skeleton, report missing entries to the retry prompt, and reuse valid worktree edits while retrying only summary repair.
- **Diagnostic logging:** `EDITOR_VALIDATION_FAILURE attempt=... reason=... missing_count=... output_bytes=... worktree_changed=...`.
- **Estimated savings:** 10–18 minutes normally; approximately 36 minutes for run `33015375913`.
- **Risk:** medium; preserve existing final validation and fail-open behavior.

### 4. Critical path: suppress same-head queued review duplicates

- **Evidence:** review runs waited 2,123 seconds (`33004184186`) and 6,186 seconds (`33041351113`) between job evaluation and runner request; runner pickup was effectively immediate. Runs `32980399079`, `33015359985`, and `33056143324` were cancelled before the first codex step after 6,276–7,051 seconds.
- **Root cause:** overlapping wrapper/direct dispatches serialize in `pr-autofix-<PR>` with `cancel-in-progress=false`.
- **Exact change:** keep running jobs protected, but suppress dispatch when the same PR and head SHA already has a running or queued review. Cancel only stale queued duplicates, never active work.
- **Diagnostic logging:** emit concurrency key, head SHA, active owner run ID, queue age, and suppression reason.
- **Estimated savings:** up to 35–103 minutes of queue delay on affected runs.
- **Risk:** low-to-medium.

**Micro-optimization note:** Semble consumed only about 6.8 seconds across 12 deep-dive queries; it is not a meaningful latency target.

## Cost Optimizations

### 1. Investigate the implement token outlier before changing models

- **Evidence:** implement run `33039973788` used 1,326,191 Codex tokens across 13 calls—96.75% of all 1,370,765 recorded Codex tokens.
- **Root cause:** unknown because this token outlier was not archived for deep inspection.
- **Exact change:** archive top-token runs independently of top-duration runs; emit prompt tokens, completion tokens, role, model, reasoning, phase, and stable-prefix hash per call.
- **Estimated savings:** potentially 0.66–1.06 million tokens if repeated context or runaway retries account for 50–80% of the outlier.
- **Quality risk:** do not downgrade the implement model until the per-call breakdown identifies the expensive stage.

### 2. Reduce pass-1 reasoning, not reviewer coverage

- **Evidence:** 12 reviewer invocations per full iteration; sampled reviews logged 13–16 OpenRouter calls after retries.
- **Exact change:** use medium reasoning for pass 1 and retain all six models plus `xhigh` pass 2.
- **Estimated savings:** 20–35% of reviewer token/dollar cost.
- **Quality risk:** lower than reducing roster size because diversity and deep review remain intact.

### 3. Eliminate failed editor attempts

- **Evidence:** 14 of 21 sampled editor attempts failed before attempt 3.
- **Exact change:** repair manifest generation and use a summary-only continuation after valid code edits.
- **Estimated savings:** approximately 40–65% of editor spend in affected runs.
- **Quality risk:** low if existing validation remains authoritative.

### 4. Preserve Semble; instrument Serena before expanding it

- **Semble:** 22 queries returned 196,762 logged bytes, with no runtime fallback. Deep queries averaged under one second. It appears to be selecting bounded context rather than causing prompt expansion.
- **Serena:** 8 query summaries represented 180 tool calls, but reported zero response bytes and zero milliseconds. In run `33056159542`, the visible calls were only `chmod` and `write_text`, so Serena did not replace expensive discovery work there.
- **Exact change:** record per-tool success/error, response bytes, duration, files touched, and whether the call replaced a shell/read operation.
- **Estimated savings:** unknown until instrumentation distinguishes productive edits from noisy round trips.
- **Quality risk:** none for logging-only changes.

### 5. Stop treating unavailable OpenRouter metrics as zeros

- **Evidence:** 143 calls were collected, but every prompt/completion/cache total is zero or `na`; dollar cost cannot be calculated.
- **Exact change:** emit `usage_available=false reason=...` instead of numerical zero and capture provider request IDs plus usage-source status.
- **Estimated savings:** enables reliable prioritization; direct savings not currently measurable.
- **Quality risk:** none.

## Reliability Improvements

### 1. Prevent CI’s deterministic 30-minute cancellation

- **Failure evidence:** 71.9% CI cancellation rate; 22 runs share the same terminal step.
- **Category:** timeout/capacity.
- **Fix:** temporary 45-minute ceiling, then matrix sharding and per-test timeout diagnostics.
- **Expected impact:** likely raises CI completion from 28.1% to above 90% for this workload.
- **Rollback:** revert the matrix while retaining the higher timeout and timing markers.

### 2. Fix the consolidator trust failure

- **Failure evidence:** 7/7 deep reviews produced zero consolidator bytes and the identical trust error.
- **Category:** configuration rollout.
- **Fix:** add `--skip-git-repo-check` to the synthesis-only consolidator command or execute from a verified trusted worktree. Add a preflight `git rev-parse` result marker.
- **Expected impact:** restores consolidation, parser coverage, and lessons generation.
- **Fail-open:** retain current reviewer-bundle passthrough if consolidation still fails.
- **Rollback:** disable `REVIEW_CONSOLIDATOR_ENABLED` explicitly rather than silently running a broken stage.

### 3. Carry reviewer rate-limit state across passes

- **Failure evidence:** every deep review had a retryable reviewer failure. Of 17 advances, 16 involved `minimax/minimax-m3`; two runs ultimately used its fallback model.
- **Category:** provider capacity.
- **Fix:** when pass 1 rate-limits a slot, start pass 2 with the successful cheaper-reasoning or fallback selection rather than retrying the known-saturated primary.
- **Expected impact:** removes repeated 5–10 minute retry episodes.
- **Rollback:** clear the same-run health hint; keep existing retry ladder.

### 4. Make failed workflows diagnosable

- **Failure evidence:** runs `32984390950` and `32985679705` have no job, step, or log excerpts. Startup failures `32984400029` and `32985626080` were not archived under `errors/`.
- **Category:** collector coverage.
- **Fix:** archive startup failures; query the jobs API even when log download fails; record `log_download_status`, API error, workflow validation annotations, and job conclusions.
- **Expected impact:** converts four opaque failures into actionable classifications.
- **Fail-open:** always write metadata, even if every API request fails.

### 5. Add structured MCP availability probes

- **Evidence:** all 35 Semble fallbacks were correctly classified as contract-test fallbacks, not runtime failures. However run `33071397954` reported `SEMBLE_AVAILABLE:false` without a structured probe/fallback, and all probe counters remain zero.
- **Fix:** emit `SEMBLE_PROBE` and `SERENA_PROBE` whenever availability is decided, including `target`, `result`, `reason`, and elapsed time.
- **Impact:** distinguishes unavailable optional tooling from a masked broken deployment.

No `BREAK_GLASS` or `CONTEXT_BUDGET_WARN` events were observed.

## AI Memory Health

- **Retrieval:** 7/7 deep-dive retrieves selected records, for a 100% hit rate.
- **Budget:** average estimated context was 1,383 of 1,400 tokens—98.8% utilization.
- **Selection:** every retrieve used `keyword_method=llm` and selected exactly 30 records, suggesting the result cap is consistently saturated.
- **Push health:** 3/14 `record-run-event` operations required two push attempts; mean was 1.21 attempts.
- **Gaps:** 6 sampled runs emitted `write_lessons_learned count=0`; the broken consolidator is a likely contributor. Run `33070949342` also logged a fail-open `force-tick-put` failure.
- **Recommendation:** after repairing the consolidator, log candidate count, selected-score range, age distribution, duplicate suppression, and push failure reason. Alert only after repeated fail-open pushes.
- No sampled `retrieve` had `enabled=false`, zero records, or `fail_open=true`. No meaningful `promote`, `compact`, or `finalize-task` coverage was present.

## GH API Call Audit

No structured GH API call-count telemetry was found. Model-provider rate limits above must not be confused with GitHub API rate limits.

| Hotspot | Evidence | Recommendation | Estimated reduction |
|---|---|---|---:|
| Per-reviewer PR-state watchdog | `review_run_reviewers.sh` checks PR state from each parallel reviewer; six reviewers run per pass | One parent monitor writes shared PR state for child watchers | Up to 83% for this endpoint |
| Gate metadata/files lookups | `review_autofix.yml` performs direct PR, commit, GraphQL, and paginated file calls | Wrap all through `gh_helpers.sh`; persist PR/file JSON for reuse | 1+ calls per fallback path |
| Check-run polling | Same check-run snapshot may be fetched repeatedly for up to 300 seconds | Emit snapshot hash/page count and reuse unchanged payloads | 20–60% during stable waits |
| Overlapping dispatch checks | Sweep checks both `internal-review.yml` and `review_autofix.yml` | Build one cached active-run index per sweep | O(PR×workflow) to O(workflow pages) |

This aligns with `agents.md`, which requires cycle-local caches and forbids re-fetching cached orchestrator state per iteration.

**Required logging addition:**

`GH_API_CALL method=GET endpoint_template=repos/:owner/:repo/pulls/:number logical_attempt=1 pages=1 status=200 elapsed_ms=... retry_reason=none rate_remaining=...`

Also emit a job-end `GH_API_SUMMARY`. Endpoint values must be sanitized, and GraphQL bodies must never be logged.

## Prompt Cache & Memory System

- `cache_hit_rate` is unavailable across the entire window.
- OpenRouter cache reads/writes are zero because usage fields were logged as `na`, not because cache misses were proven.
- All 42 reviewer slot summaries across the 7 deep runs reported zero cache-read tokens. Seven retries attempted reuse, but none recorded a read.
- Editor retries deliberately append an epoch/nonce, guaranteeing cache fragmentation. Keep this safety mechanism until manifest compliance is fixed; then limit nonce busting to refusals or repeated invalid cached output.
- Dependency-cache hits such as run `33069893455` are healthy but unrelated to prompt caching. Plan run `33071734461` reported concurrent cache creation, indicating dependency-cache contention.
- Copilot reviews built prompts of 50,334 tokens (`33067152986`) and 65,022 tokens (`33069878980`). No context-budget warning fired, so these were below the configured threshold—not necessarily small.
- **Recommendation:** log stable-prefix hash, dynamic-section token counts, cache eligibility, provider usage availability, and miss reason. Keep static instructions first and move timestamps, run IDs, and retry diagnostics to the tail.

## Orchestrator Health

- `orchestrate_poll` completed 23/23 runs successfully, with p50 246 seconds and p95 374 seconds.
- The recent poll runs `33058970573` and `33071397954` spent almost their full 236–269 seconds in the poll step; this is stable rather than a failure spike.
- Run `33061939791` found queued candidates aged 1,136–1,141 minutes but skipped them as `active_run`. This shows the sweep cannot distinguish healthy active work from stale concurrency backlog.
- Clarify, plan, implement, and clarify-response produced 335 skipped runs overall. These appear mostly to be healthy gating, but skip reasons are not consistently aggregated.
- **Smallest safe mitigation:** add `ORCHESTRATOR_QUEUE_STATE` with active run ID, status, head SHA, queue age, and concurrency key; emit `WORKFLOW_SKIP reason=...` for every gated workflow.
- Track: phase age, same-head queued count, pending-to-running delay, recovery action, judge fingerprint repeats, and merge-deferral count.

## Pipeline Flow Bottlenecks

| Stage | Dominant bottleneck | Evidence | Priority |
|---|---|---|---:|
| Clarify | Occasional model work | Successful run `33071664674` took 118s; most runs skipped | Low |
| Plan | High reasoning and cache contention | Run `33071734461` took 291s; maximum 705s | Medium |
| Implement | Token expansion | Run `33039973788`: 1.326M tokens, 1,005s | High cost |
| Review | Two full six-model passes | Deep sample averaged 72 minutes | Highest |
| Autofix editor | Two failed retries before success | 7/7 succeeded only on attempt 3 | Highest |
| CI | Single 30-minute test job | 23/32 cancelled | Highest reliability |
| Orchestrator poll | Fixed polling work | p50 246s, 23/23 success | Medium |
| Merge/conflict | Additional resolver work | Run `33041351113` resolved a conflict and pushed successfully | Situational |

Queueing, reviewer compute, editor retries, and CI timeouts—not Semble or runner pickup—dominate end-to-end latency.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review/autofix p95 6,750 seconds; CI p50 1,818 seconds; same-PR concurrency waits up to 6,186 seconds.
- **Top failure modes:** 23 CI cancellations, 21 review cancellations, broken consolidator, systematic editor retries, and opaque failed/startup runs.
- **Highest-cost drivers:** implement run `33039973788`; twelve-model-call two-pass reviews; repeated editor attempts.
- **Top actions:**
  1. Shard orchestrator-poll tests and temporarily raise CI timeout.
  2. Fix consolidator trust handling and editor manifest compliance.
  3. Set reviewer pass 1 to medium and add same-head dispatch suppression.

## Metrics Appendix

### Run outcomes

| Outcome | Count | Rate |
|---|---:|---:|
| Total | 609 | 100% |
| Success | 226 | 37.11% |
| Failure | 2 | 0.33% |
| Startup failure | 2 | 0.33% |
| Cancelled | 44 | 7.22% |
| Skipped | 335 | 55.01% |

Overall duration: p50 **8s**, p95 **4,719s**. Cancelled runs represent **33.76%** of recorded wall time.

### Key workflow families

| Family | Runs | Success | Failure/startup | Cancelled | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|
| CI | 32 | 9 | 0 | 23 | 1,818s | 1,827s |
| Review/autofix | 104 | 80 | 3 | 21 | 35s | 6,750s |
| Copilot reviewer | 32 | 31 | 1 | 0 | 206s | 321s |
| Orchestrate poll | 23 | 23 | 0 | 0 | 246s | 374s |
| Clarify | 87 | 2 | 0 | 0 | 1s | 10s |
| Plan | 84 | 2 | 0 | 0 | 1s | 11s |
| Implement | 85 | 1 | 0 | 0 | 1s | 11s |

### Cost and review telemetry

| Metric | Value |
|---|---:|
| Runs with log telemetry | 115/609 (18.88%) |
| Codex calls | 39 |
| Codex tokens | 1,370,765 |
| OpenRouter calls | 143 |
| OpenRouter tokens | Unavailable (`na`) |
| Cache read/write tokens | Unavailable |
| `cache_hit_rate` | Unavailable |
| `wall_clock_p50_ms` | 9,000 |
| `wall_clock_p99_ms` | 7,790,240 |
| `break_glass_count` | 0 |
| `context_budget_warn_count` | 0 |

### MCP telemetry

| System/target | Queries | Tool calls | Bytes | Fallbacks | Probe OK/failed/skipped |
|---|---:|---:|---:|---:|---:|
| Semble, all | 22 | — | 196,762 | 35 contract-test; 0 runtime | 0 / 0 / 0 |
| Semble reviewer-context, deep sample | 7 | — | 79,922 | 0 | 0 / 0 / 0 |
| Semble overflow, deep sample | 4 | — | 32,391 | 0 | 0 / 0 / 0 |
| Semble conflict-resolver-context | 1 | — | 9,286 | 0 | 0 / 0 / 0 |
| Serena review/autofix | 6 | 36 | 0 | 0 | 0 / 0 / 0 |
| Serena implement | 2 | 144 | 0 | 0 | 0 / 0 / 0 |

Other MCP servers observed: **none**.

### GH API telemetry

| Metric | Value |
|---|---|
| Structured call count | Not emitted |
| Per-endpoint counts | Not emitted |
| Retry totals | Not emitted |
| Rate-limit events | None visible in sampled logs; global coverage incomplete |
| Required next step | Instrument `gh_helpers.sh` and direct gate calls |
