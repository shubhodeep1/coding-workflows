## Executive Summary

- **The dominant speed problem is on the critical path.** All six CI runs succeeded, but CI p50 was 2,672.5 seconds; in run **36499418954**, the orchestrate-poll test step took 1,326 seconds. Running that independent test group alongside the preceding lint work could save **up to roughly 20 minutes per CI run**, subject to preserving the required check (medium confidence; medium implementation risk).
- **One review outlier needs provider-level diagnosis.** Review run **36483245451** took 8,075 seconds. One `x-ai/grok-4.20` usage record reported **661,382,198 cache-read tokens** and 662,879,782 total tokens—**87.8% of the assembled window’s reported total**. Reconcile that record with provider billing before treating it as spend; log per-slot elapsed time and consider a guarded duration cap (high confidence in the log, low confidence in billed cost).
- **Zero failed conclusions do not mean zero operational gaps.** Of 1,000 collected rows, 481 succeeded, 516 skipped, and three were cancelled. Successful run **36501158404** warned that standalone validation could not be dispatched for merged PR **#4842**. Record a durable follow-up state rather than letting the successful conclusion conceal that gap (high confidence).
- **Collector accuracy needs a small fix.** The 24 reported CI `SEMBLE_FALLBACK` lines represent **16 distinct contract-test events**: two CI archives include each event in both aggregate-job and individual-step logs with slightly different timestamps. One skipped implement run, **36457861680**, also appears twice in the 1,000 rows. Deduplicate before aggregation (high confidence).
- **API and queue bottlenecks cannot yet be ranked.** The window has no measured GH API endpoint counts or reliable queue intervals. Add sanitized call, retry, wait, and job-timing summaries before changing polling or batching behavior (high confidence in the collection gap).

## Speed Optimizations

1. **Parallelize CI’s two long test groups — critical-path win.** **Evidence:** CI run **36499418954** took 2,695 seconds; its `lint / Orchestrate poll process unit tests` step took **1,326 seconds**, after approximately 22 minutes of preceding job work. Run **36499440954** showed the same pattern: **1,303 seconds** in that step. **Inferred cause:** independent suites are serialized in one job. Move the poll-test group to a parallel job, regenerate its `/tmp/orchestrate_poll_remaining_tests.txt` input there, and retain a required `lint` result that depends on both groups. **Estimated saving:** up to **20–22 minutes** on comparable CI runs, less duplicate setup; **risk: medium**, so verify test independence and branch-protection behavior first.
2. **Bound exceptionally long reviewer slots — tail win, not a median win.** **Evidence:** review **36483245451** ran **8,075 seconds**, with a `grok-4.20` pass-one usage record appearing at 23:07:19 after other reviewers had reported by 21:13:33. The existing reviewer idle watchdog does not establish an absolute slot-duration limit. **Inferred cause:** a progressing but very long provider attempt can hold the panel open. First emit slot start, end, last-progress age, attempt, and failback reason; then trial a **45-minute absolute attempt cap** using the existing failback path, without bypassing review gates. **Estimated saving:** as much as **about 69 minutes** on an analogous long slot, not on ordinary runs; **risk: medium** because failback quality must be checked.
3. **Profile poll ticks before optimizing them.** The 36 `orchestrate_poll` runs had p50 **388 seconds**; run **36500869895** spent nearly its entire **376 seconds** in `poll / poll`. Emit per-tick time for API work, judge work, intentional waiting, and state writes. **Saving: unquantifiable until those timings exist; risk: low** for logging. This is more consequential than eliminating short skipped dispatches.
4. **Defer skipped-run prefiltering — micro-optimization.** The **516 skipped rows consumed 1,511 seconds in aggregate**; plan and implement each skipped every run in their families. Log eligibility reasons before considering trigger filtering. Even eliminating every skipped run would save at most **1,511 runner-seconds in this window**, not 1,511 seconds on one pipeline’s critical path; **risk: low to medium** if an eligible event could be suppressed.

## Cost Optimizations

1. **Reconcile the anomalous review usage record before changing models.** Run **36483245451** reported **668,537,627 total tokens across 13 calls**, including the single **662,879,782-token** `grok-4.20` record. The assembled review total is **754,584,007**; seven of 134 calls lack complete usage. Log provider request ID, model, attempt, input-byte count, returned usage fields, and elapsed time, then compare with billing. **Potential saving:** preventing a recurrence could remove an outlier of this *reported* magnitude; **dollar saving is unknown**, and automatic model removal risks review quality.
2. **Measure panel yield before reducing reasoning or passes.** The six deep-dive reviews used roughly **12–15 model calls each**; all six ended `clean_review_no_commit`. The configured reviewer panel has six models and defaults to `xhigh` first-pass reasoning, with an existing shallower small-diff second-pass setting. Emit per-pass unique findings, elapsed time, usage, and final disposition; use that evidence to evaluate the existing reasoning settings rather than dropping a reviewer. **Estimated token saving: not supportable yet; quality risk: high** for an unmeasured reduction.
3. **Measure context value, not just context volume.** `review_autofix` recorded **10 `SEMBLE_QUERY` calls**, **138,202 logged bytes**, target `reviewer-context`—about **13,820 bytes per query**. The six inspected calls each returned 12 chunks. There is no before/after prompt-size or downstream-tool comparison, so Semble’s net reduction in prompt expansion is **unproven**; log selected-chunk bytes *and bytes actually inserted* alongside avoided file-context reads. Serena recorded **zero queries and zero response bytes** and was explicitly disabled in recent review **36504129211**; it cannot yet be credited with replacing tool or model work. **Estimated saving: unknown; quality risk: low** for measurement, potentially high for indiscriminate context removal.
4. **Avoid reruns only after identifying their cause.** Recent review runs **36501249655** and **36501056487** both skipped autofix while PR **#4695** awaited a Claude-fixer session; the skips avoided model spend. Record the age of that handoff and the event that will resume it. Do not dispatch repeated reviews merely to investigate a wait. **Measured token saving from the current gate:** none calculable; **quality risk: low** for logging.

## Reliability Improvements

1. **Make successful-but-incomplete validation visible.** **Evidence:** `review_autofix` **36501158404** succeeded while warning that no standalone validation workflow could be dispatched for merged PR **#4842**. **Category:** follow-up dispatch/observability. Emit `validation_dispatch_status` with a bounded reason and PR number, persist pending follow-up for the existing workflow to revisit, and alert if it remains pending. **Impact:** reduces silent validation gaps; **rate unknown**. Fail open for the completed merge, but do not label validation complete until it runs; rollback is removal of the follow-up, not a change to merge rules.
2. **Correct telemetry inflation before alerting on fallbacks.** **Evidence:** CI runs **36499418954** and **36499440954** each have four `SEMBLE_FALLBACK target=overflow context=contract-test` events represented eight times across two archive views. The collector’s existing structured-line deduplicator compares full lines; the two views differ slightly in timestamp. **Category:** collector duplication. Deduplicate using a normalized event body plus run and step identity, retaining genuinely repeated events; also key run rows by repository, run ID, and attempt to remove duplicate **36457861680**. **Impact:** this window’s contract-test fallback count becomes **16 rather than 24**, without changing runtime behavior. Retain raw counts during rollout for rollback comparison.
3. **Watch prompt pressure and slot failback separately.** `CONTEXT_BUDGET_WARN` occurred twice, both in review: **36463458847** logged **169,689/200,000** prompt tokens (ratio **0.8484**), and **36472977218** logged **161,007/200,000** (**0.805**), above their 140,000-token threshold. The latter also logged a Minimax `server_error` failback; the former logged a `stall_guard` failback. **Category:** prompt-size risk plus provider recovery, not demonstrated rubric pressure. Log the prompt component sizes and failback outcome; alert on repeat warnings, not these two successful recoveries alone. **Impact:** earlier detection of truncation or retry risk; **failure-rate reduction unknown**. `BREAK_GLASS` count was **zero** in collected telemetry.
4. **Keep intentional MCP fail-open tests out of runtime incidents.** All **24 counted** `SEMBLE_FALLBACK` lines were in CI contract tests targeting `overflow` with a deliberately missing executable; there were **zero observed runtime fallbacks**. Distinguish `context=contract-test` from runtime in dashboards and alerts. No `SERENA_PROBE` or Serena runtime fallback was recorded; that is **absence of rollout evidence**, not evidence of availability. **Impact:** fewer false incidents; preserve existing fail-open behavior.

## AI Memory Health

In six deep-dive `review / codex-agent` logs—runs **36452313366, 36463458847, 36464455484, 36472967508, 36472977218, 36483245451**—all **6/6 `retrieve` operations selected records**: 23 each, for a **100% sampled hit rate**. Mean `estimated_tokens` was **1,376.5 of a 1,400-token budget (98.3%)**; `keyword_method` was **llm: 6, plain: 0, none: 0**. These excerpts contain **12 successful `record-run-event`** and **six successful `record-candidate`** operations, all with `push_attempts=1`; no zero-record, `fail_open:true`, or `enabled:false` entry was found. A separate unselected-run summary for `issue_pr_status` **36501158379** reports one successful lesson-ingestion write from 22 parsed items for PR **#4842**.

**Recommendation:** emit retrieval relevance/selection reason and a non-sensitive record-ID digest by task. Repeated selection of 23 records near the budget ceiling warrants checking usefulness, but does not justify shrinking the budget without quality evidence. **Expected impact:** identifies removable context while preserving memory recall; token saving is not yet measurable. No `finalize-task`, `promote`, `compact`, or processed-command operation appeared in the inspected deep dives; verify their emission in workflows that execute them.

## GH API Call Audit

**No per-endpoint call counts, rate-limit totals, or retry-wait totals were supplied.** No rate-limit incident was identified in inspected logs, but that does not establish a window-wide rate of zero. Instrument `scripts/gh_helpers.sh` (`gh_retry` and `gh_retry_to_file`) with endpoint **templates**, job/step, attempts, result class, and wait milliseconds—never tokens, raw URLs, bodies, or PR text—and aggregate them by run. This follows `CLAUDE.md` §15’s reuse, batching, cycle-local-cache, and fail-open rules.

- **Concrete static redundancy, occurrence unmeasured:** on a snapshot miss, `scripts/orchestrate_poll_process.sh` fetches the same final PR once for `.state` and again for `.merged_at`. Fetch one PR JSON payload and extract both fields. **Estimated reduction:** **one GET per such miss** (two to one); window-wide saving and rate-limit effect require the new counter. Preserve the current safe fallback on fetch failure.
- **Batching opportunity to test, not an observed hotspot:** review sweep **36501039110** inspected **12 candidates** and dispatched **nine**, but reports no lookup count. Measure calls per candidate before changing its loop. For any demonstrated per-candidate reads, reuse a cycle-local snapshot or the repository’s existing aliased GraphQL pattern; an N-call lookup could become approximately `ceil(N/batch_size)` calls, conditional on endpoint and permissions. Keep the existing REST fail-open path. Do not classify `review_collect_pr_metadata.sh`’s distinct PR, issue-comment, and review-comment fetches as redundant without matching endpoint data.

## Prompt Cache & Memory System

The assembled aggregate `cache_hit_rate` is **null** because **7/134** OpenRouter calls have unavailable usage. Reported per-run values vary: **99.4581%** in **36483245451**, **87.833%** in **36501661780**, **71.2367%** in **36452313366**, and **58.615%** in **36501066657**. The very high outlier includes **661.38 million provider-reported cache-read tokens in one call**; it must not be taken as proof that ordinary prompts cache equally well. Aggregate cache-write tokens are reported as zero, which alone does not establish that creation failed.

**Recommendation:** log per-call cache fields, stable-prefix digest/length, dynamic-context length, model, and usage availability. Then test placing run-specific noise after otherwise stable instructions without changing content. This can diagnose fragmentation and improve read reuse, but **token and latency impact are unknown** until comparable calls are measured. The two review `CONTEXT_BUDGET_WARN` events above make prompt-component sizing especially useful. Memory retrieval’s **1,376.5/1,400-token** average leaves little budget headroom; test relevance before trimming. Cache or memory failure must continue to fail open under existing rules.

## Orchestrator Health

All **36 `orchestrate_poll` runs succeeded**, with p50 **388 seconds** and p95 **438.25 seconds**; the summary for **36500869895** attributes nearly all **376 seconds** to `poll / poll`. The supplied rows do **not** connect ticks to projects, waves, judge cycles, deferrals, or terminal states, so success cannot establish wave progression. Plan (**128/128**) and implement (**129/129**) runs were skipped, but eligibility reasons are unavailable; calling them stuck would be an inference without support.

Emit one `ORCH_TICK_SUMMARY` per tick with counts of active projects, wave advances, judge decisions, intentional deferrals, conflict-heal attempts, terminal transitions, and time by API/wait/compute/state-write category. For Claude-fixer waits such as PR **#4695**, add handoff age and resume condition; for close cleanup **36500815798**, which preserved **three active runs without PR linkage**, log safe linkage categories and revisit them rather than cancelling unlinked runs. **Expected impact:** distinguishes healthy waits from stalls without changing the state machine. Track age since last wave advance, repeated same-head handoffs, deferred-project age, and conflict-heal attempts per project.

## Pipeline Flow Bottlenecks

| Flow segment | Observed bottleneck | Type and next diagnostic |
|---|---|---|
| Clarify → plan → implement | Clarify **11 success / 120 skipped**; plan **128 skipped**; implement **129 skipped** | Eligibility/dispatch unknown. Log skip reason and task lineage; do not infer a clarification loop. |
| Review/autofix | **344 runs**, p50 **14 s**, p95 **949.7 s**; **36483245451** took **8,075 s** | Model compute/tail wait. Log per-slot start, progress, completion, and retry/failback duration. |
| Validate / CI | CI **6/6 success**, p50 **2,672.5 s**; poll-test step **1,326 s** in **36499418954** | Serialized test compute. Pilot the parallel-job change above while retaining the required gate. Standalone validation dispatch also needs an explicit follow-up state for PR **#4842**. |
| Orchestrate / merge | Poll p50 **388 s**; close cleanup preserved **three unlinked active runs** in **36500815798** | Poll wait versus API versus merge/conflict overhead is not separated. Add tick-stage and linkage timings before tuning. |
| Queue / retry | Three cancellations; no failed conclusions or run-attempt retries | All 1,000 run `created_at`/`run_started_at` pairs are equal, and job timing is largely uncollected. Obtain job queued/start times; zero measured lag is **not** proof of zero queueing. |

These are run-level segments, not a linked end-to-end task trace; their medians must not be added together.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** CI’s serial poll tests (**1,303–1,326 seconds** in runs **36499440954/36499418954**), review’s **8,075-second** tail (**36483245451**), and poll ticks at **388-second p50**. **Top operational risks:** the successful-but-undispatched validation warning (**36501158404**), misleading duplicated fallback telemetry, and unlinked active-run cleanup (**36500815798**). **Highest reported cost:** review’s **754,584,007** OpenRouter total tokens, dominated by one anomalous provider record; billed dollars are unknown.

**Top three actions, in order:** (1) correct collector deduplication and add sanitized per-slot/API/tick diagnostics; (2) parallelize independent CI test work while preserving its required result; (3) reconcile the anomalous reviewer usage and trial an absolute slot cap through existing failback, without removing review coverage.

## Metrics Appendix

**Window:** September 28, 2026, 16:13 UTC–September 29, 2026, 00:39 UTC. Counts below use the assembled context unless marked *raw folder*. The folder has **18 runs with parsed log telemetry** (15 rows with excerpts); the assembled context reports **115**. The folder also records **971 `not_selected`** and **11 `empty_archive`** log statuses. The 1,000 rows contain **999 unique repository/run-ID/attempt keys**.

| Scope / family | Runs | Success / failed / cancelled / skipped | Success rate, all runs | Duration p50 / p95 |
|---|---:|---:|---:|---:|
| Repository, reported rows | 1,000 | 481 / 0 / 3 / 516 | 48.1% | 10 / 383 s |
| CI | 6 | 6 / 0 / 0 / 0 | 100% | 2,672.5 / 2,782 s |
| Review/autofix | 344 | 343 / 0 / 1 / 0 | 99.7% | 14 / 949.7 s |
| Orchestrate poll | 36 | 36 / 0 / 0 / 0 | 100% | 388 / 438.25 s |
| Clarify | 131 | 11 / 0 / 0 / 120 | 8.4% | 1 / 156 s |
| Plan; implement | 128; 129 | 0 / 0 / 0 / 128; 0 / 0 / 0 / 129 | 0% each; all skipped | 1 / 9.65 s; 1 / 10 s |

| Assembled cost/review metric | Value | Qualification |
|---|---:|---|
| OpenRouter calls; usage available / unavailable | 134; 127 / 7 | All reported usage is in review/autofix. |
| Prompt / completion / total tokens | 25,588,048 / 993,582 / 754,584,007 | **Total includes reported cache reads; do not add these columns.** Not a billed-cost figure. |
| Cache read / cache write tokens | 728,005,794 / 0 | Outlier run **36483245451** reports 664,654,388 reads. |
| `cache_hit_rate` | null aggregate | Incomplete usage; selected run values: **99.4581%, 87.833%, 71.2367%, 58.615%**. |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 9,000 / 3,782,720 | **115** assembled telemetry samples; not the all-run duration percentiles. |
| `break_glass_count` / `context_budget_warn_count` | 0 / 2 | Warnings both in review runs identified above. |
| Codex calls / tokens | 0 / 0 | No Codex usage lines counted in this window; not proof that all other stages cost nothing. |
| GH API calls / retries / rate-limit events | **Not collected / not collected / not collected** | Inspected deep dives show no identified rate-limit incident; no window-wide call-rate claim is possible. |

| MCP target and context | Queries; logged bytes | Fallbacks | `probe_ok` / `probe_failed` / `probe_skipped` |
|---|---:|---:|---:|
| Semble `reviewer-context`, review/autofix | **10; 138,202 bytes** | 0 observed runtime | 0 / 0 / 0 recorded |
| Semble `overflow`, CI targeted-file-context contract test | 0 | **24 reported lines; 16 distinct event bodies** | 0 / 0 / 0 recorded |
| Serena, no target observed; disabled in recent review **36504129211** | **0; 0 response bytes** | 0 | **0 / 0 / 0 recorded** |

Serena tool-call count and per-tool breakdown are **0 / none observed**; there is no basis for an availability or replacement-efficiency rate. Semble’s contract-test fallbacks have **no meaningful runtime fallback rate** denominator. **Other MCP servers observed:** none in inspected deep-dive logs; unknown servers outside available logs remain a collection gap.
