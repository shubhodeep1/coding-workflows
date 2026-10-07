## Executive Summary

- **Review/autofix is the dominant measured critical path.** In `shubhodeep1/coding-workflows`, 312 runs had a 557.5s median and 1,978s p95. Four slow review runs account for **all 41,737,826 reported OpenRouter tokens** and 76 reported calls. Prioritize per-phase timing and same-head dispatch diagnostics before changing models. **Impact:** potentially substantial latency and token savings, not yet quantifiable across all reviews. **Confidence:** high for measured runs; low for extrapolation.
- **CI and check-failure triage show concentrated failures.** CI failed 13/26 runs; triage failed 11/26, including six at “Post check-failure triage issue” and five at “Collect check-failure context.” Add reason-coded stage outcomes without weakening either gate. **Impact:** faster diagnosis and fewer avoidable repeat investigations. **Confidence:** high for failure locations, low for underlying causes.
- **Checkout and conflict preparation need better failure attribution.** Four review failures stopped at “Checkout PR head branch,” including runs `37647760213` (2,232s) and `37661395500` (1,899s); two stopped at conflict-prompt preparation, `37663738492` and `37665635776`. Log elapsed time and classified Git failure reasons before increasing retries. **Impact:** possible savings approaching the time spent before a diagnosed failure; actual savings unknown. **Confidence:** high for locations, low for causes.
- **Cache and MCP conclusions are coverage-limited.** Eighteen of 76 OpenRouter calls lack usable usage, making aggregate `cache_hit_rate` null. Twelve Semble queries logged 122,833 bytes; 40 Semble fallbacks are classified as **CI contract-test** events, not runtime failures. **Impact:** better attribution before any cache or rollout change. **Confidence:** high.
- **The supplied full-log directory is not mounted in this environment.** Neither its `summary.json` nor `errors/`, `slow/`, or `recent/` could be inspected. Findings below use the assembled context and run-row summaries; exact error text, API counts, and memory events require the archive. **Impact:** restores root-cause confidence. **Confidence:** high.

## Speed Optimizations

1. **Critical path — distinguish review compute from repeat dispatch.** Run `37668894733` reports roughly 406s in `review / codex-agent` within a 428s review; review-family p95 is 1,978s. PR `#6678` appears in four long runs, but head/base revisions are unavailable, so these are **not proven duplicates**. Emit a phase-duration summary and a sanitized head/base/rubric identity for each dispatch; suppress a review only when those identities and the prior successful disposition match. **Estimated saving:** up to one whole confirmed redundant run—for example, 2,354s for `37663225276` *if* it proves redundant; otherwise zero. **Risk:** medium if identity checks are incomplete; logging alone is low-risk.
2. **Failure-path latency — classify checkout early.** Runs `37647760213`, `37653635912`, `37661395500`, and `37663723863` failed at “Checkout PR head branch” after 1,161–2,232s overall. The workflow already retries fetch up to four attempts. Add a `REVIEW_CHECKOUT_DIAG` outcome with pre-step elapsed time, fetch attempts, exit category, and whether the source/workspace/metadata-head guard matched; then move only proven safe, cheap preflight checks ahead of expensive setup. **Estimated saving:** unknown, bounded by time preceding each affected failure. **Risk:** low for logging; medium for step reordering.
3. **Release scan — profile before restructuring.** In cancel-on-PR-close run `37669878803`, merge-train release took about 34s of 48s and reported `examined=37 released=0`. Add counts for queued/active/blocked/unresolved candidates, distinct file fetches, cache hits, pages, and elapsed time. Reuse the existing cycle-local file cache; optimize a measured hotspot rather than skipping the safety scan. **Estimated saving:** 0–34s on a comparable run, unproven. **Risk:** low for logging.
4. **Queueing, not compute — measure contention.** Auto-release run `37669016921` spent approximately 124s of 142s waiting for `release-check`. Emit queued-at, job-started-at, and compute-time summaries by job; correlate them with short, non-actionable dispatches before changing triggers. **Estimated saving:** unquantifiable until contention is linked; no claim that removing skips would eliminate the 124s. **Risk:** low.

## Cost Optimizations

1. **Prevent only verified redundant review work.** The four token-bearing runs are `37649595095` (9,445,823), `37657163863` (11,876,236), `37654281070` (3,738,177), and `37663225276` (16,677,590)—all review/autofix. Capture dispatch reason and revision identity, then deduplicate only an identical, already-completed review. **Estimated saving:** up to that run’s *reported total tokens* per proven duplicate; dollars and billable-token savings cannot be calculated here. **Quality risk:** high if a changed head, base, or rubric is mistaken for the same review.
2. **Measure prompt expansion before trimming it.** Runs `37649595095` and `37657163863` emitted two and four `CONTEXT_BUDGET_WARN` events respectively; together they account for 21,322,059 reported total tokens. Add per-prompt-section byte/token estimates, model, reasoning level, and cache usage to review/editor summaries. Deduplicate repeated context only after identifying the growing section. **Estimated saving:** unknown; six warnings identify candidates, not removable-token counts. **Quality risk:** retain material evidence and the existing review rubric.
3. **Keep model changes scoped.** The workflow configures multiple reviewer models, reviewer reasoning `xhigh`, and editor reasoning `high`, but the supplied run aggregates do not separate token cost or accepted findings by model/slot. Log model, reasoning level, attempts, outcome, and usage per slot; test a lighter setting on a narrowly defined, already-supported profile before changing production defaults. **Estimated dollar saving:** unavailable without per-model prices and usage. **Quality risk:** potential missed findings; do not globally downgrade.
4. **Assess MCP value from contribution, not query count.** The four slow reviews made 12 Semble queries totaling 122,833 logged bytes—about 10,236 bytes/query. Target names, `sources`, and `static_dup_bytes` contributions are not available in this context, so reduced prompt expansion is **not established**. Log target, contributed-source count, duplicated-static bytes, and bytes actually inserted into prompts. Serena has zero recorded queries/tool calls and is explicitly disabled in summaries for runs `37668880820` and `37668876036`; no replacement benefit can be claimed. **Estimated saving:** unknown. **Quality risk:** do not prune retrieval or enable Serena based on zeros alone.
5. **Avoid post-diagnosis waste where safe.** Failed triage posting runs `37655564710` and `37658984994` each recorded one Codex call and 2,026 tokens. Classify whether posting failed on marker validation, token scope, transient API error, or another condition; check locally verifiable prerequisites before diagnosis without adding an API probe. **Estimated saving:** up to 4,052 recorded Codex tokens across these two cases *only if* the failure was predictable. **Quality risk:** preserve diagnosis when posting may recover.

## Reliability Improvements

1. **CI test failures — regression or environment category, cause unverified.** CI failed 13/26 runs. “Validation self-test unit tests” failed in `37647983427`, `37649159395`, and `37651766699`; merged-PR commit-guard tests failed in `37650128310`, `37664544454`, `37666875390`, and `37668038984`. Emit failing test identifier, sanitized assertion/error category, test-suite duration, and first-failure timestamp in a compact CI summary. Fix the recurring assertion once identified; retain the gates. **Expected impact:** fewer repeated CI failures and faster repair, not a defensible percentage yet. **Rollback:** remove added diagnostics if noisy; do not fail open past tests.
2. **Triage stage failures — collection versus posting.** Six of 11 triage failures occurred at posting (for example `37655564710`); five occurred at collection (for example `37668269982`). Existing code has several distinct collection gates and a separately token-scoped posting step. Emit `stage`, `reason_code`, API status category, retry count, and whether the issue-body prerequisite passed—without body text or credentials. **Expected impact:** isolate the dominant fix and reduce unsuccessful diagnosis-to-post cycles. **Rollback:** retain current fail-closed issue validation and self-loop guard.
3. **Review checkout/conflict failures — Git or state category, cause unverified.** Record fetch exit classification and attempt durations at checkout, and merge exit, unmerged-path count, blob-backfill attempt, and classified terminal reason in `scripts/review_conflict_prepare.sh`. The script already distinguishes failed merge replay from ordinary content conflicts; do not turn either into a successful resolution. **Expected impact:** shorter time to repair the four checkout and two preparation failures. **Rollback:** diagnostics can be removed without changing Git behavior.
4. **Separate test fallbacks from rollout health.** The 40 Semble fallbacks occur in CI telemetry and are all classified `contract-test`; recorded runtime fallbacks are zero. No Serena probe failure or runtime fallback was recorded. Keep contract-test and runtime counters separate in alerts, and emit target/reason for real runtime fallbacks. **Expected impact:** fewer false rollout alarms without masking future failures. **Rollback/fail-open:** retain existing fallback behavior.
5. **Treat cleanup warnings as warnings, not failed reviews.** Successful reviews `37668894733` and `37668931133` summarize post-job `git-submodule` “without a working tree” warnings. Log checkout/post-job workspace presence and cleanup exit category; investigate the lifecycle before changing cleanup. **Expected impact:** clearer alerting and prevention of a possible future hard failure; current failure-rate benefit is unproven. **Rollback:** keep successful-run disposition unchanged.

Recorded `BREAK_GLASS` count is **0** in covered telemetry; there is no observed policy-bypass pattern. The six context-budget warnings instead warrant prompt-size investigation, not a policy-pressure conclusion.

## AI Memory Health

No `AI_MEMORY_TELEMETRY:` event is present in the supplied run rows or summaries, and the deep-dive logs are unavailable. Consequently, retrieve hit rate, average `estimated_tokens` versus `token_budget`, `keyword_method` distribution (`llm`/`plain`/`none`), zero-record retrieves, `fail_open: true`, `enabled: false`, and push retries are **not measurable**, not zero. `scripts/ai_memory.py` contains retrieve-event emission; verify that the relevant workflow steps emit it and that the collector preserves an aggregate by operation and role. Record counts and bounded numeric fields only, not retrieved records. **Expected impact:** makes memory effectiveness and failed-open operation diagnosable without changing retrieval behavior.

## GH API Call Audit

- **Observed scan, not observed call count:** cancel-on-PR-close `37669878803` examined 37 merge-train candidates in roughly 34s. `scripts/review_merge_train.sh` already caches file lists per distinct PR and documents a paginated active-run listing; 37 candidates must **not** be reported as 37 API calls. Add endpoint-class call/page counts, cache hits, retries, HTTP status categories, and elapsed time to its release summary. **Expected reduction:** zero justified from current evidence; each *newly demonstrated* duplicate file-list lookup avoided would save its REST page call(s). This follows `CLAUDE.md` §15’s reuse-before-new-call rule.
- **Existing batching should be preserved:** scheduled close cleanup fetches queued and in-progress runs with two bounded status-filtered requests and batches PR-state GraphQL lookups in groups of 50. Do not collapse the status filters into an unbounded completed-run scan. Instrument page counts, unresolved PRs, and batch failures before proposing a call-count change. **Expected reduction:** none safely established; rate-limit risk is currently unquantified.
- **Triage hotspot to measure:** `scripts/check_failure_triage.sh` fetches PR metadata, conditionally fetches a parent issue, and pages open triage issues for deduplication. Log whether each lookup was needed and reused; batch future per-item additions under §15 rather than placing lookups inside a candidate loop. **Potential reduction:** one call per confirmed redundant lookup, but the supplied runs provide no count to multiply by.

`GH_PAT_BUDGET` start/end logging exists, but no budget or endpoint totals were supplied. Attach its existing report and bounded per-step call summaries to the next analysis. Shared-PAT budget deltas are not exact per-job call counts under concurrency; report 403/429 and backoff separately. No rate-limit event is evidenced in this input.

## Prompt Cache & Memory System

Across the four token-bearing reviews, telemetry reports 30,317,000 cache-read tokens and 2,707,037 cache-write tokens, but **aggregate `cache_hit_rate` is null** because 18/76 OpenRouter calls lack usable usage. Fully reported run `37654281070` has a measured hit rate of **72.165%**; it is not a fleet rate. Add usage-availability reason and per-model, per-phase cache totals so missing calls cannot silently appear as misses.

**Inference to test:** dynamic PR/run metadata placed before reusable instructions, varying retrieved context, or growing reviewer history could fragment prompt prefixes. Log section-order identifiers and sizes—not prompt text—and put stable instructions before volatile content where behavior permits. Correlate cache results with the six context warnings in `37649595095` and `37657163863`. **Estimated token, latency, and reliability impact:** unknown until complete usage and section-size measurements are available. Memory-retrieval effectiveness remains unmeasured as described above.

## Orchestrator Health

Plan run `37668600898` reported “planning stalled. Re-triggering plan generation,” then skipped because issue `#40` already had an open PR. **Inference:** stall recovery may have acted on state that the existing-PR gate subsequently superseded; this does not prove a stuck issue. Emit tracking-issue state age, recovery trigger, open-PR lookup outcome, and final gate disposition; reuse that lookup before re-dispatch where safe. **Expected impact:** fewer unnecessary recovery dispatches, count unknown.

Clarify had 120 “other” outcomes in 124 runs, plan 113/119, implement 116/119, and orchestrate-clarify-respond 119/119. Recent rows show skipped examples, but the aggregate “other” category is not a measured clarification-loop or deferral count. Add explicit gate-outcome counters, wave transitions, deferral reason/age, conflict-heal attempts, and terminal/stuck-state age. Track *actionable runs per dispatch* and *recovery dispatches ending in an existing-PR skip*; do not loosen guards to improve these numbers.

## Pipeline Flow Bottlenecks

| Segment | Evidence | Bottleneck type and next safe action |
|---|---|---|
| Clarify → plan → implement | Recent runs `37669973120`, `37669972985`, and `37669973093` skipped; plan `37668600898` recovered then found an existing PR. | **Dispatch/gating:** record skip and recovery reasons before reducing fan-out; end-to-end saving unknown. |
| Review/autofix | 312 runs; p50 **557.5s**, p95 **1,978s**; `37668894733` spent ~406s in `codex-agent`. | **Compute:** phase and model-attempt timing first; conditionally deduplicate identical completed work. |
| Validate/CI | 13/26 failures; CI p50 **1,165s**. | **Failure/retry overhead:** surface first failing test early, without omitting the rest of required validation. |
| Orchestrate/release | Poller p50 **674.5s**; release-check waited ~124s in `37669016921`. | **Queueing:** separate scheduled wait from poll compute before tuning cadence or concurrency. |
| Merge/conflict | Preparation failures `37663738492`, `37665635776`; merge-train `37669878803` examined 37, released 0. | **Conflict/scan overhead:** count terminal reasons, blockers, and API pages; preserve conservative release checks. |

Run-row `retries=0` does not exclude in-step Git or API retry loops; instrument those separately.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** review compute and long-tail review runs; CI validation; observed release queue wait. **Top failure modes:** recurring CI test failures, triage collection/post failures, review checkout and conflict-preparation failures. **Highest measured cost:** 41,737,826 OpenRouter total tokens across four slow review runs; dollar cost and the other reviews’ token spend are not established.

**Top three actions, in order:**
1. Add reason-coded checkout, triage, and CI first-failure summaries; use them to fix the recurring cause without relaxing gates.
2. Add review phase/model/cache and revision-identity summaries; deduplicate only proven identical completed reviews.
3. Attach endpoint/page/retry and queue-time summaries to merge-train, poller, and release runs before changing API or scheduling behavior.

## Metrics Appendix

**Window and outcomes** (`other` is the collector category, not assumed to be a failure or success):

| Scope | Runs | Success | Failure | Cancelled | Other | Success / runs | Failure / runs | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `shubhodeep1/coding-workflows` | 1,000 | 415 | 33 | 2 | 550 | 41.5% | 3.3% | 9s / 1,280s |
| Review/autofix | 312 | 302 | 9 | 1 | 0 | 96.8% | 2.9% | 557.5s / 1,978.45s |
| CI | 26 | 12 | 13 | 1 | 0 | 46.2% | 50.0% | 1,165s / 1,623s |
| Check-failure triage | 26 | 0 | 11 | 0 | 15 | 0% | 42.3% | 6.5s / 346s |
| Orchestrate poll | 14 | 14 | 0 | 0 | 0 | 100% | 0% | 674.5s / 1,181.1s |

**Measured cost and cache coverage:** 125/1,000 runs have parsed log telemetry; the supplied full-log archive is unavailable. All nonzero OpenRouter and Semble query aggregates below come from the four identified slow review runs.

| Metric | Supplied value | Interpretation |
|---|---:|---|
| OpenRouter calls; usage available / unavailable | 76; 58 / 18 | 23.7% of calls lack usable usage |
| Prompt / completion / total tokens | 8,421,596 / 292,249 / 41,737,826 | Reported fields; total includes substantial cache activity |
| Cache write / read tokens | 2,707,037 / 30,317,000 | Not a fleet hit-rate calculation |
| `cache_hit_rate` | **null** aggregate; 72.165% in `37654281070` | Aggregate invalidated by unavailable usage |
| Codex calls / tokens | 2 / 4,052 | Both calls appear in failed triage-post runs |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 11,000 / 3,096,040; 125 samples | Collector uses **run duration fallback**; these are not model-call latencies |
| `break_glass_count` / `context_budget_warn_count` | 0 / 6 | Warnings: 2 in `37649595095`, 4 in `37657163863` |

| API/MCP signal | Supplied count and size | Availability or attribution |
|---|---|---|
| GH API calls / 403–429 / retries | **Not supplied** | `37669878803`: 37 merge-train candidates examined, **not** a call count; GH_PAT budget values absent |
| Semble queries | 12; **122,833 logged bytes** | Review/autofix; target and inserted-prompt-byte breakdown unavailable |
| Semble fallbacks | 40 contract-test; **0 classified runtime** | CI; target/reason breakdown unavailable |
| Semble bootstraps / failures / unused / measured time | 0 / 0 / 0 / 0ms recorded | Absence of bootstrap telemetry does not establish bootstrap cost or availability |
| Semble sources / static duplicated bytes | 0 / 0 recorded | Contribution fields unavailable here; do not interpret as proof of no contribution |
| Serena queries / response bytes / tool calls / query time | 0 / 0 / 0 / 0ms recorded | Disabled in cited review summaries; no per-tool breakdown observed |
| Serena fallbacks; probes ok / failed / skipped | 0; 0 / 0 / 0 recorded | No observed probe, so availability cannot be assessed |

**Per-target MCP availability:** no target-level rows were supplied. Serena target **not observed**: `probe_ok=0`, `probe_failed=0`, `probe_skipped=0` recorded; this is **not** a successful availability probe. Semble query and fallback totals cannot safely be allocated to targets. **Other MCP servers observed:** none in the supplied aggregates; unmounted full logs prevent a complete unknown-server audit.
