## Executive Summary

- **Fix CI’s repeated deterministic failures first.** All 8 failures among 24 CI runs occurred in `shubhodeep1/coding-workflows`; six runs show inventory-parity failures, five show missing event-mirror logging callsites, and several have additional failures. Run `37127904831` reported four failed jobs. Fixing the checks could prevent repeated 9–16-minute failed runs. **Impact: high; confidence: high.**
- **Review/autofix is the largest measured compute bottleneck.** In run `37124247419` for PR #6126, reviewer models took about 1,220 seconds and editor fixes 830 seconds—79% of the 2,581-second run. Preserve the review gate, but measure work by head SHA before attempting reuse. **Impact: high; confidence: high for the bottleneck, low for savings from reuse.**
- **Reported cost and warning totals are inflated by mirrored logs.** Run `37124247419` records the same 14 OpenRouter usage events twice: its reported 28 calls and 99.66 million tokens represent 14 distinct events and 49.83 million reported tokens. Its two context warnings are one distinct event. Fix collector deduplication before using totals for budgets. **Impact: high for measurement accuracy; confidence: high.**
- **Merge-train waiting is real, not a reason to bypass safety.** Run `37127631768` examined 16 queued PRs and released none; its logs identify older overlapping PRs as blockers. Prioritize the oldest blocker and log queue age and blocked reason. **Impact: potentially high for flow; confidence: high for the stall, low for its end-to-end duration.**
- **API and queue-time attribution are missing.** No per-endpoint API-call counts or retry totals were supplied, and all 1,000 collector rows have identical created and started timestamps. Instrument existing helpers and distinguish actual runner wait from compute before changing poll cadence or batching. **Impact: high diagnostic value; confidence: high.**

## Speed Optimizations

1. **Critical path—review/autofix.** Run `37124247419`, `review / codex-agent`, spent about 1,220 seconds in *Run reviewer models* and 830 seconds in *Apply fixes with editor model*. Review/autofix p50/p95 duration is 393/1,437 seconds across 217 runs. **Likely cause:** model and editor work, not setup micro-steps. **Change:** log per-pass elapsed time, head SHA, rubric version, finding count, and whether the same-head result was already validated; reuse a result only when those inputs and required safety checks match. **Potential saving:** up to one full review run when identical work is proven; no such opportunity is established by this sample. **Risk:** medium—never reuse across changed heads or rubrics.

2. **Failed-CI critical path.** In CI run `37127904831`, Actionlint failed roughly 15 seconds after creation, but the run lasted 975 seconds while parallel poll-process tests continued; its longest poll shard ran about 884 seconds. **Change:** put quick Actionlint, inventory, and event-mirror checks in a preflight dependency for expensive shards, while retaining a full-suite path for diagnostics. **Potential saving:** approximately 14–15 minutes on a similarly failing run; successful runs gain a preflight dependency. **Risk:** medium because early gating can hide additional failures.

3. **Poll-test imbalance.** Run `37127904831` had poll-process shards of roughly 884, 675, 639, and 636 seconds. **Change:** emit per-test durations and move an independent slow test group from shard 0 only after verifying isolation. **Potential saving:** at most about 209 seconds on a similarly balanced run, not a guaranteed improvement. **Risk:** medium.

4. **Secondary, not critical-path-first:** run `37124247419` spent about 130 seconds in *Free disk space*. Log space before/after and the cleanup action; avoid or narrow it only where a safe-space threshold is already met. **Potential saving:** up to that step’s measured time on qualifying runs. **Risk:** low if low-space runs retain cleanup.

## Cost Optimizations

1. **Correct measurement before tuning spend.** The collector’s whole-line deduplication in `scripts/collect_workflow_logs.py` misses matching aggregate/step events whose microsecond timestamps differ. In `37124247419`, each of 14 usage records appears in both logs. Deduplicating only matched parent/child step events yields approximately **89 rather than 103** OpenRouter calls and **302.39 million rather than 352.22 million** reported OpenRouter tokens for this log-covered cohort. These are corrected *telemetry estimates*, not a billing reconciliation. Add a stable event ID or normalize timestamps within a bounded parent/child match; retain repeated events within one step. **Saving:** no demonstrated billing saving; prevents misdirected optimization. **Quality risk:** none if raw records remain available for audit.

2. **Avoid only proven repeated reviews.** PR #6126 appears in long successful review runs `37118616001`, `37114980790`, and `37124247419`; the supplied evidence does **not** establish identical heads or work. Record head SHA, prompt/rubric version, and verdict so exact-repeat work can be identified without skipping changed-head review. **Potential saving:** one eligible run could avoid tens of minutes and its model usage; current avoidable-run count is unknown. **Quality risk:** high if identity checks are weakened.

3. **Reduce uncached context cautiously.** Log-covered OpenRouter usage includes 25.52 million prompt tokens, 4.14 million cache-write tokens, and 320.27 million cache-read tokens; aggregate `cache_hit_rate` is unavailable because one of 103 reported calls lacks usage. Run `37106320510` reports a 65.2% hit rate versus 94.7% in `37121763028`. Keep static instructions ahead of changing diff/run material, and log prefix hash, dynamic-context bytes, and cache status per call before attributing that gap to fragmentation. **Scenario, not forecast:** trimming 10% of the roughly 25.7 million *deduplicated* prompt-plus-write tokens would remove about 2.6 million such tokens. **Quality risk:** context removal may lose findings.

4. **Test model effort and retrieval value, rather than downgrading globally.** Run `37124247419` configured reviewer reasoning `xhigh` and editor reasoning `high`. A lower-effort trial should be restricted to measured small-diff cohorts and compared on findings and later autofix cycles; dollar savings cannot be estimated without prices and a quality baseline. Semble logged 14 queries/151,241 bytes as collected, or 13 distinct queries/136,542 bytes after the demonstrated mirror correction. Reviewer-context responses are about 14.5 KB each; an implement overflow query in `37105860038` returned just 13 bytes. Log *bytes selected for prompts* and subsequent model usage, and omit near-empty retrievals if tests confirm no loss. The current evidence cannot show that Semble reduces prompt expansion. Serena logged no runtime queries or tool calls and was disabled in sampled reviews, so replacement value and noisy-response risk cannot yet be assessed.

## Reliability Improvements

1. **Deterministic CI regression; highest priority.** Inventory parity reports unexpected `scripts/smoke_review_dispatch.sh` in six failed runs, including `37123734645`; event-mirror tests report no `AUTOFIX_DISPATCH_SKIPPED` echo callsites in five, including `37127904831`. Run `37127904831` also has three tests missing a `tmp_path` argument and Actionlint rejects constant-false conditions in `.github/workflows/orchestrate_poll.yml`. **Category:** code, fixture, and contract drift. **Fix:** update the inventory; restore the intended skip diagnostic or adjust its contract only after verifying equivalent behavior; run fixture-dependent tests under their fixture-aware harness or give them explicit temporary directories; remove disabled Actionlint-flagged steps without inadvertently enabling them. Log every failing job and step, not just the collector’s single `failure_point`. **Expected impact:** addresses recurring CI failures; rerun reduction cannot be measured until fixes land. **Rollback:** keep existing checks mandatory; revert individual fixes rather than suppressing checks.

2. **Preserve integration-intent protection.** CI run `37127904831`, poll shard 1, includes a failed `test_judge_prompt_caps_embedded_pr_diffs_by_bytes`; its logs also exercise fingerprint guards that refuse a merge-resolve commit when sub-issue intent is lost. **Category:** poll-test regression, with safety checks also appearing in test output. Reproduce the actual failed assertion independently and log test name, shard, and elapsed time; do not classify every expected fingerprint-guard error line as a production failure or disable the guard. **Expected impact:** cleaner failure attribution and fewer repeated failed shards. **Rollback:** retain fail-closed merge verification.

3. **Separate MCP tests from availability incidents.** The collector reports 40 `SEMBLE_FALLBACK` lines, all marked `context=contract-test`, target `overflow`, with **zero runtime fallbacks**; mirrored logs account for four of those 40. This is healthy test coverage, not evidence of 40 production outages. Poll run `37127624660` separately reports Semble and its index unavailable, without a runtime query failure. Emit distinct runtime availability and contract-test counters, with target and reason, to reveal a broken rollout without alarming on fixtures. Serena has zero runtime queries, fallbacks, and probes; `SERENA_ENABLED: false` in sampled reviews explains why zero failures do not prove availability. **Expected impact:** fewer false incident signals and earlier detection of real unavailability. **Rollback:** keep current fail-open paths.

4. **Track non-fatal cleanup warnings.** Successful review runs including `37124243456` and `37124221658` report post-job Git submodule cleanup failing without a working tree. Log checkout/cleanup ordering and a cleanup-warning count before changing it. **Expected impact:** clearer signal; no demonstrated run-failure reduction. **Rollback:** do not make cleanup a new blocking gate.

`BREAK_GLASS` count is zero. The two collected `CONTEXT_BUDGET_WARN` records in review run `37124247419` are one mirrored warning: review prompt 155,859 tokens against a 200,000-token window and 140,000-token threshold. It signals **prompt-size pressure**, not observed rubric or break-glass pressure.

## AI Memory Health

Nine distinct `AI_MEMORY_TELEMETRY` retrieves in the slow-run logs—all seven review retrieves and two implement retrieves—selected records: **9/9 hit (100%)**, averaging **1,433 estimated tokens against a 1,444-token average budget**. Keyword methods were `llm` 7, `plain` 2, `none` 0. No observed retrieve returned zero records or reported `enabled: false` or `fail_open: true`. Run `37124247419` selected 25 reviewer records at 1,389/1,400 tokens; the near-full budget warrants relevance measurement before trimming.

Observed operations also include successful candidate records, run events, implement command claims/completions, and task finalization. A `record-run-event` in `37107182581` needed five push attempts; log retry cause and elapsed backoff, and alert on repeated high attempts without blocking review. No `promote` or `compact` event appears in these deep dives; memory-maintenance run `37106035607` has no full telemetry here. Verify those operations’ emission rather than inferring inactivity.

## GH API Call Audit

| Observed workflow/job/step | Evidence | Safe action and expected call impact |
|---|---|---|
| Review Autofix Sweep, run `37124210897` | Summary records 17 candidate dispatches in about 38 seconds. | Log dispatch endpoint category, status, attempt count, and latency. Dispatches are per candidate; **do not claim** they can be batched. Savings unknown. |
| Cancel on PR Close, run `37127631768`, *Release merge-queued PRs* | 16 examined, 0 released; step took about 14 seconds. `scripts/review_merge_train.sh` already lists PRs once and caches each distinct PR file list per process. | Log per-cycle distinct file fetches versus `_MT_FILES_CACHE` hits, pagination, and blocked-reason counts. Preserve the cache and fail-open behavior; no redundant-call reduction is established. |
| Orchestrate Poller, 17 runs | p50 duration 476 seconds; per-endpoint calls, retries, and rate-limit events are not supplied. | Add a sanitized per-job summary to existing `scripts/gh_helpers.sh`: endpoint *template*, method, calls, attempts, cache hits, status class, rate-limit waits, and elapsed milliseconds. Avoid URL parameters, bodies, and tokens. Then extend existing batched GraphQL/cycle-local caches where a repeated lookup is demonstrated, consistent with `codex.md` §15. |

No genuine rate-limit event or HTTP-retry count can be quantified from these selected logs; test-fixture errors and the run-level `retries: 0` field are not API-retry measurements. Instrument the existing wrapper rather than adding an API call to measure API usage. A proven same-cycle duplicate lookup could save its repeat calls; the present data does not justify a numeric call-reduction claim.

## Prompt Cache & Memory System

Cache reads dominate recorded OpenRouter input, but the official aggregate `cache_hit_rate` is **null** because one usage record is unavailable. Individual measured rates range from **65.2%** (`37106320510`) to **94.7%** (`37121763028`); `37124247419` reports **92.1%**, unchanged by removing its mirrored records. These differences alone do not prove unstable prefixes. Emit a privacy-safe static-prefix hash, rubric version, dynamic section sizes, and per-call creation/read counts; place run-specific noise after a stable prefix where behavior permits. Track the 155,859-token review warning by distinct event ID so prompt growth is not double-reported.

Memory retrieval is effective by *hit count* (9/9) but nearly fills its budget; record selection reason and downstream usefulness, not record text. Keep retrieval fail-open. Semble’s logged `bytes` measure returned context, not demonstrated model-token savings; Serena was disabled, so compare tool response bytes and downstream tool/model calls in a controlled enabled cohort before promoting it.

## Orchestrator Health

The observed clarify, plan, and implement families are predominantly skipped (150/151, 146/149, and 144/152 respectively). That explains the overall two-second run median; it does **not** establish a clarification loop or stuck state. Log each skip’s reason and tracking-issue/state transition to distinguish expected no-ops from stalled waves.

The merge train supplies stronger stall evidence: `37127631768` left 16 PRs queued, with #6127 blocked by #6126 and later PRs listing progressively more blockers; `37121385157` also reported 16 examined and none released. Investigate and advance the oldest safe blocker first, without bypassing overlap checks. Emit oldest-blocker age, queued age, `released`, and reason counts (`overlap`, active review, incomplete listing, API failure) each tick. Run `37126656841` spent about 34 of 47 seconds in heal intake and identified duplicate issue #6055; keep deduplication and log duplicate-hit latency. Conflict-heal retry and terminal-state rates cannot be determined from the supplied rows; add transition and retry counters keyed by state, not issue content.

## Pipeline Flow Bottlenecks

| Flow segment | Measured signal | Bottleneck type and next diagnostic |
|---|---|---|
| Clarify → plan → implement | Most runs skipped; implement outlier `37105902515` took 3,040 seconds and logged 13 Codex calls. | **Compute or intentional no-op:** log active-phase start/end and skip reason; avoid interpreting family medians of one second as work latency. |
| Review/autofix | p50/p95 393/1,437 seconds; `37124247419` spent about 2,050 seconds in reviewer/editor steps. | **Compute:** per-pass and editor timing, same-head identity, and findings count. |
| Validate/CI | 8/24 failed; `37127904831` failed quick checks early but ran 975 seconds. | **Parallel-test tail/repeat failures:** fix deterministic checks, then evaluate preflight gating. |
| Orchestrate/merge | Poller p50 476 seconds; release scan `37127631768` left 16 overlapping PRs queued. | **Merge wait:** blocker-age and per-cycle reason logging; do not equate safe queueing with API failure. |
| Queue/retry overhead | All 1,000 rows show `run_started_at == created_at`; API retry counts absent. | **Unattributed:** collect actual job queued/start timestamps and wrapper backoff before estimating queue or retry savings. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Bottlenecks:** review/editor compute, long CI poll-test shards, and an overlapping-PR merge train. **Failure modes:** recurring inventory, event-mirror, fixture, and Actionlint CI regressions; misleading single-point failure attribution and mirrored cost events. **Highest-cost drivers:** OpenRouter review usage and long implement runs; pricing and avoidable same-head work are unmeasured.

**Top three actions:** (1) fix and preflight the deterministic CI checks without suppressing them; (2) correct parent/step telemetry deduplication and emit per-stage timing; (3) instrument merge-train blocker age and existing GH API wrapper counts before changing API strategy. Expected outcomes are fewer failed CI cycles, trustworthy cost baselines, and an attributable queue stall.

## Metrics Appendix

**Scope and coverage.** Collector generated October 3, 2026, 14:18 UTC. Its `summary.json` has **23** runs with full log telemetry; the supplied assembled context reports **117** runs with log telemetry or added summaries. The configured success sample rate is 7%, but the collector reports **0 randomly sampled success runs**; successful deep dives are targeted. Do not combine the two cohorts’ wall-clock percentiles.

| Family | Runs | Success / failure / cancelled / skipped | Duration p50 / p95 |
|---|---:|---:|---:|
| All, `shubhodeep1/coding-workflows` | 1,000 | 340 / 8 / 5 / 647 | 2 / 554 s |
| CI | 24 | 15 / 8 / 1 / 0 | 786 / 1,216 s |
| Review/autofix | 217 | 211 / 0 / 4 / 2 | 393 / 1,437 s |
| Orchestrate poll | 17 | 17 / 0 / 0 / 0 | 476 / 565 s |
| Implement | 152 | 8 / 0 / 0 / 144 | 1 / 506 s |

Overall failure rate is **0.8% of all runs**; CI failure rate is **33.3% of its 24 runs**. Skipped runs make the overall success rate unsuitable as an active-work success measure.

| Log-covered metric | Collected value | Qualification |
|---|---:|---|
| Codex usage | 26 calls; 2,652,382 tokens | Implement family |
| OpenRouter usage | 103 calls; 352,215,361 total tokens | Includes cached tokens and mirrored records; approximately 89 calls / 302,387,231 tokens after the demonstrated correction |
| OpenRouter prompt / completion | 25,516,795 / 2,285,244 tokens | Collected, before mirror correction |
| Cache creation / read | 4,141,981 / 320,274,506 tokens | Collected, before mirror correction; 102 usage-available calls, 1 unavailable |
| `cache_hit_rate` | null aggregate | Individual observed range 65.2%–94.7%; do not substitute a weighted rate for the unavailable official metric |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 12,000 / 3,022,220 | Assembled context, 115 samples; full-log collector instead reports 940,000 / 3,585,220 on 23 samples |
| `break_glass_count` / `context_budget_warn_count` | 0 / 2 | Warnings are **1 distinct** mirrored event in `37124247419` |

| MCP target / source | Queries; logged bytes | Fallbacks | `probe_ok` / `probe_failed` / `probe_skipped` |
|---|---:|---:|---:|
| Semble `reviewer-context`, review/autofix | 8; 116,940 | 0 | 0 / 0 / 0; no probe signal |
| Semble `overflow`, implement and CI contract tests | 6; 34,301 | 40 collected, all CI contract-test; 0 runtime | 0 / 0 / 0; no probe signal |
| Serena `serena` | 0; 0 response bytes, 0 tool calls, 0 query ms | 0 | 0 / 0 / 0; no runtime probe observed |

After confirmed mirrored-line correction, Semble has **13 distinct queries/136,542 bytes** and **36 distinct contract-test fallbacks**: reviewer-context 7/102,241 bytes and overflow 6/34,301 bytes. Query-to-fallback rates are **not meaningful** here because the fallbacks are deliberate tests, not failed runtime queries. Serena per-tool breakdown: **none observed**. **Other MCP servers observed:** none in the supplied runtime telemetry.

| GH API activity proxy | Observed value | Missing measurement |
|---|---:|---|
| Sweep `37124210897` | 17 candidate dispatches, about 38 s | Actual API attempts/statuses |
| Merge release `37127631768` | 16 PRs examined, 0 released, about 14 s in step | Distinct API calls, cache-hit count, pagination |
| Rate-limit events / HTTP retries | Not quantified | Sanitized wrapper counters and backoff duration |
