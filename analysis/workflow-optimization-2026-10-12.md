## Executive Summary

- **Fix deterministic CI failures first.** In `shubhodeep1/coding-workflows`, 22 of 23 CI failures name one of two guard steps: 11 name an unwired test and 11 name the workflow script-size guard. Run `38213131911` first failed its wiring guard 224 seconds after creation but finished 973 seconds later. Repairing the violations and running the guards before expensive jobs could remove repeat failures and shorten failure feedback by minutes. **Confidence: high** for the failures; **medium** for the latency gain.
- **Correct telemetry before using it to budget models or MCP.** Aggregate job logs repeat step events. The collector reports 62 OpenRouter calls and 36.89M tokens; deduplicating mirrored lines *within each run* yields 37 calls and 20.11M tokens. This is a reporting correction, **not** a spending saving. **Confidence: high.**
- **Separate runner wait from computation.** CI run `38214164022` had a `tests-promote-stall-and-review` runner wait of about 1,122 seconds. Review run `38213722665` had about 258 seconds of gate runner wait and 297 seconds of agent runner wait. Timestamp each transition before changing concurrency. **Potential impact: minutes per affected run; confidence: high** for measured waits.
- **Investigate a masked review-stage failure.** All five active review runs among the ten slow deep dives record `consolidator.status=failed` and `failure_class=empty_output`, although their workflows succeeded. Preserve the current fail-open path while determining whether the invocation or its summary is wrong. **Impact: review-quality risk not yet quantifiable; confidence: high** for the recorded pattern, **low** for its root cause.
- **Do not equate skips with failures.** Of 1,000 runs, 739 were skipped; recent clarify, plan, and implement runs have no step logs or skip reasons. Emit a reason at dispatch/collector level so intentional gating can be distinguished from lost work. **Impact: diagnostic clarity; confidence: high.**

## Speed Optimizations

1. **Critical path—fail CI before running the full job set.** The `CI test wiring guard` names `tests/test_clarify_sandbox_base_image_preseed.py` in run `38213131911`; the other guard reports a 19,821-character interpolated `Commit and push updates` body in `.github/workflows/update_workflows.yml` in run `38212635767`, above its 18,000-character limit. First wire the test and move the oversized body into a script or its expressions into `env:`. Then place these existing fast guards in an early preflight job and make expensive CI jobs depend on it, preserving check names and fail-closed behavior. **Estimated saving:** run `38213131911` had 973 seconds between its first guard failure and completion; four deep-dived size-guard runs had a median 407 seconds remaining after failure. These are *opportunities*, not guaranteed savings. **Risk: medium** for dependency changes; low for fixing the violations.

2. **Critical path—measure review eligibility-to-start delay.** Run `38207005403` lasted 5,568 seconds, but its review summary records 1,275 seconds of review-budget elapsed time. The gate ended around 05:15:41 UTC and the agent runner started around 06:21:38 UTC; no “runners busy” line establishes why that intervening period occurred. Add `gate_completed_at`, `agent_eligible_at`, `runner_assigned_at`, and `review_started_at` to the existing run summary. Investigate the resulting dependency/concurrency/queue split before altering scheduling. **Potential saving: up to the observed approximately 66-minute gap if avoidable; currently unproven. Risk: low** for logging.

3. **Lower-impact—avoid inapplicable readiness work before runner assignment.** Readiness run `38216887322` succeeded in 26 seconds but its summary says the head ref was not an orchestrator branch; roughly 16 seconds was runner wait. Move the existing branch-applicability predicate to a pre-run/job gate where its required-check semantics remain intact, and log `skip_reason`. **Estimated saving:** up to 26 seconds per similarly inapplicable run, not a measured fleet-wide total. **Risk: medium** if the check is required.

## Cost Optimizations

1. **Fix accounting first—largest apparent “saving,” zero real saving.** The mirrored OpenRouter lines in runs `38207005403`, `38209571935`, and `38213722665` account for 25 extra reported calls and 16,780,793 extra reported tokens. In `scripts/collect_workflow_logs.py`, structured deduplication keys retain the timestamp, while aggregate and step copies have slightly different timestamps. Match a parent job event to its child step using a stable event ID—or normalized structured payload and job identity—without collapsing genuinely separate calls in one step. **Estimated dollar saving: $0; measurement error corrected: 16.78M reported tokens. Quality risk: none** if distinct-call tests are retained.

2. **Reduce avoidable full-diff review expansion, conditionally.** Runs `38207005403` and `38207023231` say reviewer iteration scoping fell back to `full-diff` because `LEDGER_STATUS_FILE` was missing; their deduplicated OpenRouter totals were approximately 0.94M and 0.90M tokens, respectively. Log ledger presence, iteration number, selected diff bytes, and prompt bytes; restore the ledger before a subsequent scoped pass when valid, retaining full-diff fail-open otherwise. **Savings: unquantifiable without per-pass prompt sizes; quality risk: medium** if context is scoped incorrectly.

3. **Canary model/reasoning changes, not blanket cuts.** Active slow reviews including `38207005403` and `38213722665` log reviewer reasoning effort `xhigh`; the five active sampled reviews include three `autofix_pushed` outcomes. Measure findings and edits by reviewer slot and pass before testing lower effort on a low-risk repeat pass. Do not drop a reviewer or change the failing consolidator merely to cut calls. **Dollar/token saving: not estimable** without per-model prices and comparable quality outcomes. **Quality risk: high** for an unmeasured blanket change.

Semble’s **six distinct** sampled queries returned 86,241 logged bytes from 32 sources, with `static_dup_bytes=0`; five targeted `reviewer-context` and one `review-blocked-judge-context`. The logs do not measure prompt size *with versus without* retrieval, so they do not prove net prompt savings. Its six lazy bootstraps took 216,987 ms in total, substantially more than query time. Log selected-context bytes actually inserted and prompt-byte deltas before tuning retrieval. No validated Serena query occurred, so Serena’s downstream-work replacement and response-byte value cannot be assessed; log tool, calls, response bytes, and work displaced if it is enabled. Avoidable CI reruns are plausible from the repeated deterministic guards, but no rerun count or dollar cost is measured.

## Reliability Improvements

1. **Configuration/test-wiring failures—repair, do not suppress.** CI has 23 failures in 42 runs. Eleven primary failure points are the unwired-test guard, 11 are the workflow-size guard, and one is the `agents.md direct-edit check` (`38211924014`, without a mounted deep-dive log). Wire the named test and shrink the named workflow step; emit a compact `CI_GUARD_RESULT` with guard, offending path, measured length or unwired count, and result. **Expected impact:** address the two recorded causes behind 22 primary failure points; recurrence after repair is unknown. **Rollback:** retain both fail-closed guards; revert only the wiring/script change if necessary.

2. **Review-stage fail-open—identify what `empty_output` represents.** Runs `38204569645`, `38207005403`, `38207023231`, `38209571935`, and `38213722665` each report a failed consolidator but successful workflow completion. Emit `REVIEW_CONSOLIDATOR_RESULT` with `invocation_started`, exit class, input/output byte counts, elapsed time, and the fallback chosen; reconcile it with `REVIEW_AUTOFIX_RUN_SUMMARY_V1`. **Expected impact:** expose or resolve a five-of-five sampled active-review anomaly; failure-rate reduction cannot yet be estimated. **Rollback/fail-open:** keep the existing reviewer-output fallback while diagnosing.

3. **Memory-write resilience—retain fail-open and pinpoint retries.** Review-blocked judge run `38210066011` emitted one `AI_MEMORY_TELEMETRY` `write_lessons_learned` event with `ok=false, fail_open=true`; run `38207023231` recorded a memory event requiring three push attempts. Add a bounded failure class and retry-delay total, without record content. **Expected impact:** faster diagnosis of persistent write problems; no observed workflow failure to eliminate. **Rollback:** keep memory writes nonblocking.

4. **Distinguish MCP test fixtures from rollout health.** The collector reports 120 `SEMBLE_FALLBACK` events across 15 deep-dived CI runs, but mirrored job/step lines represent **60 distinct** `target=overflow, context=contract-test` events—four per run testing a missing binary. Runtime fallback and bootstrap failure counts are zero in the observed review logs. Preserve the contract tests; report test and runtime denominators separately and add a runtime availability result per attempted target. **Expected impact:** prevent false incident alerts and reveal a genuinely broken rollout; no demonstrated runtime failure to fix. Serena has no validated probe or runtime event, so its availability is **unknown**, not healthy.

No `BREAK_GLASS` or `CONTEXT_BUDGET_WARN` event was counted in the available telemetry; that does not establish absence outside covered logs. If emitted, log workflow, run, target, and a bounded policy-pressure or prompt-size category—not prompt contents.

## AI Memory Health

Across ten slow review deep dives, all **10 `retrieve` operations selected records**: sampled hit rate **100%**, zero empty retrieves, and average `estimated_tokens` **1,384.4 against a 1,400-token budget** (98.9%). Every retrieve used `keyword_method=llm`; none used `plain` or `none`, and none recorded `enabled=false` or `fail_open=true`. The deduplicated sample also contains 20 `record-run-event`, five `record-candidate`, and four `write_lessons_learned` events. Of 25 events reporting `push_attempts`, 19 took one, five took two, and one took three; one write failed open as noted above. No `finalize-task`, `promote`, `compact`, or processed-command event was seen **in these review logs**.

**Recommendation:** retain retrieval and fail-open behavior, but log selected-record usefulness or subsequent citation/use counts alongside the existing token estimate. Its near-full budget and 100% selection rate demonstrate retrieval activity, not relevance. Verify emission for other memory operations in workflows that actually invoke them; do not infer their failure from this review-only sample. **Expected impact:** distinguish useful recall from budget-filling context, with no immediate safe token saving quantified.

## GH API Call Audit

**A per-endpoint call audit is not possible from this window.** Ten review deep dives contain 40 distinct `GH_PAT_BUDGET` start/end snapshots, all reporting `used_in_job=unknown`; they do not provide endpoint, method, retry, or cache-hit counts. A reviewer-slot `rate_limit` retry in run `38209571935` was a *model-slot* signal, not evidence of a GitHub API limit. No measured per-item API hotspot or GitHub rate-limit event can be claimed.

The concrete audit targets are review steps **Collect PR metadata**, **Collect PR check-run failures**, **Cache linked issues references**, and **Mark linked issues ready to merge**, plus orchestrator polling. Add counters to the **existing** `gh_retry`/API helpers: workflow/job/step, endpoint *class* rather than IDs, method, calls, retries, status class, elapsed time, prefetch/cache hit, and rate-limit response. Do not log URLs containing identifiers or response bodies. Follow `CLAUDE.md §15`: extend existing reads, use its batched GraphQL helpers and cycle-local caches, and fail open to the smallest safe legacy read on cache miss. **Conditional call reduction:** if instrumentation finds an N-item REST loop, batching changes N reads to approximately `ceil(N/batch_size)`; no baseline N was observed, so no numeric reduction or rate-limit improvement is asserted. Keep a fresh state check before merge.

## Prompt Cache & Memory System

The supplied aggregate `cache_hit_rate` is **76.82%**, but the same mirrored events inflate its inputs; deduplicated sampled OpenRouter usage gives approximately **74.59%**. Run `38207005403` recorded only **15.83%** and `38207023231` **32.60%**, versus **84.99%** in `38209571935`. Missing-ledger full-diff scoping is an observed source of prompt expansion in the first two runs; unstable prefixes or dynamic noise are *possible*, not established by these logs.

Log a non-sensitive static-prefix hash, dynamic-context byte count, ledger status, per-call cache read/write, and provider cache-support status. Put stable instructions before run-specific data where the existing prompt contract permits it; compare the same review pass before and after. Keep memory retrieval within its 1,400-token cap and record whether selected material was used. **Expected impact:** potentially fewer uncached input tokens and shorter model calls; magnitude and reliability effects require comparable runs. No sampled `CONTEXT_BUDGET_WARN` establishes current pressure, but record prompt-budget headroom so growth is visible before warnings appear. Null cache metrics outside review are missing coverage, not cache misses.

## Orchestrator Health

Seven `orchestrate_poll` runs succeeded, with p50 **842 seconds** and p95 **1,595 seconds**, but no poller deep-dive logs are mounted. Clarify, plan, implement, and orchestrate-clarify-respond have respectively **154/165**, **139/149**, **144/149**, and **149/153** skipped runs; the recent skipped runs have no job logs. Those counts cannot distinguish correct gating from stuck waves or deferrals. The sampled reviews show four `stale_base_skip` finalizations (`38205152531`, `38207564378`, `38211301533`, `38212750769`); their successful workflow conclusions do not mean review work completed.

**Smallest safe addition:** emit bounded transition records at the existing orchestrator and dispatch points: project/wave correlation key, prior/new state, skip or deferral reason, clarification round, candidate count, conflict-heal attempt, last-progress age, and terminal reason. For pollers, add cycle start/end and wait-versus-work timing. Track skipped-by-reason, wave age, repeated unchanged-state cycles, conflict retries, and terminal-without-merge counts before changing polling cadence. **Expected impact:** make stalls diagnosable without new services or altered orchestration behavior.

## Pipeline Flow Bottlenecks

| Flow segment | Evidence | Bottleneck and next safe action |
|---|---|---|
| Clarify → plan → implement | Most runs in each family are skipped; implement also has a 1,745-second successful outlier (`38214636216`). | **Dispatch/coverage gap:** log skip reasons and active-phase timings before interpreting the 1-second overall p50 as throughput. |
| Review/autofix | Family p50 **851 seconds**, p95 **2,885.4 seconds**; `38207005403` lasted **5,568 seconds** with an unexplained approximately 66-minute gate-to-agent gap. | **Queue/dependency plus compute:** timestamp eligibility, assignment, model passes, and completion separately. |
| Validate/CI | **23/42** CI runs failed; run `38214164022` had an approximately **1,122-second** runner wait for one test job. | **Queue then deterministic failure:** fix guards and move them to early preflight; log first-failure-to-terminal delay. |
| Orchestrate/merge | Poller p50 **842 seconds**; four sampled review runs finalized `stale_base_skip`. | **State/freshness overhead:** measure poll cycle decisions and stale-base detection time; keep the final fresh-state safety check. |

These are separate run-family observations, **not** a measured end-to-end critical-path trace across one PR.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** CI runner contention and late terminal completion after guard failures; long review runs with mixed queue/dependency and model time. **Top failure modes:** the unwired test and oversized workflow step (22 primary CI failure points), plus the sampled consolidator `empty_output` pattern. **Highest measured cost driver:** review/autofix OpenRouter usage—**20.11M deduplicated tokens in 37 sampled calls**; fleet-wide dollars are unavailable.

**Prioritized actions:** (1) fix both CI guard violations and preflight their checks; (2) correct parent/child telemetry deduplication and add phase/queue timestamps; (3) reconcile consolidator results while retaining fail-open. Measure GH API calls and skip reasons through existing helpers as part of action 2.

## Metrics Appendix

**Scope:** one repository, 1,000 API-listed runs; collector generated **October 12, 2026, 07:27:52 UTC**. The mounted collector summary has **26 runs with parsed deep-dive telemetry**; the assembled context reports **125 runs with log telemetry/summaries**. Deep dives favor failures and slow runs, so their costs and wall-clock percentiles are not representative of all runs.

| Family | Runs | Success | Failure | Skipped | p50 / p95 duration |
|---|---:|---:|---:|---:|---:|
| All | 1,000 | 238 (23.8%) | 23 (2.3%) | 739 (73.9%) | 1 / 1,059 s |
| CI | 42 | 19 (45.2%) | 23 (54.8%) | 0 | 959.5 / 1,617.9 s |
| Review/autofix | 57 | 57 (100%) | 0 | 0 | 851 / 2,885.4 s |
| Orchestrate poll | 7 | 7 (100%) | 0 | 0 | 842 / 1,595.2 s |
| Clarify / plan / implement | 165 / 149 / 149 | 11 / 10 / 5 | 0 / 0 / 0 | 154 / 139 / 144 | 1 / 465.6; 1 / 591.4; 1 / 11 s |

Among the **261 non-skipped** runs, 238 succeeded and 23 failed; this is a different denominator from the table’s all-run rates.

| Sampled telemetry | Supplied aggregate | Deduplicated mirrored lines |
|---|---:|---:|
| OpenRouter calls; prompt / completion / total tokens | 62; 7,070,434 / 537,225 / 36,887,616 | **37; 4,351,264 / 316,217 / 20,106,823** |
| Cache read / write tokens; `cache_hit_rate` | 27,925,193 / 1,355,000; 76.82% | **14,761,960 / 677,500; 74.59%** |
| Codex calls / tokens in assembled context | 2 / 4,052 | Not independently reconciled; judge token reporting has different coverage |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 | No validated events observed |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 1,000 / 3,844,600, **121** assembled-context samples | 1,486,000 / 5,195,750, **26** collector deep-dive samples; different population, not a deduplication adjustment |

| API/MCP signal | Supplied aggregate | Distinct observed evidence |
|---|---:|---|
| GH API | No endpoint/call total | 40 review `GH_PAT_BUDGET` snapshots; **40 `used_in_job=unknown`**; no measured retry or endpoint breakdown |
| Semble queries / logged bytes / sources | 10 / 139,964 / 51 | **6 / 86,241 / 32**: `reviewer-context` 5 / 80,441 bytes / 30 sources; `review-blocked-judge-context` 1 / 5,800 bytes / 2 sources |
| Semble bootstrap / total time | 10 / 357,372 ms | **6 / 216,987 ms**; 0 observed failed or unused; distinct-query mean about 544.5 ms |
| Semble fallback | 120 contract-test; 0 runtime | **60 distinct** `target=overflow, context=contract-test` across 15 CI deep dives; **0 runtime** |
| Serena query / fallback / probe; response bytes | 0 / 0 / 0; 0 | No validated events; **no per-tool breakdown or replacement rate measurable** |
| Other MCP servers observed | — | **None** with validated query/fallback/probe telemetry |

**Per-target MCP availability:** no `probe_ok`, `probe_failed`, or `probe_skipped` event was emitted for observed Semble targets (`reviewer-context`, `review-blocked-judge-context`, `overflow`): **0 / 0 / 0 for each**. No Serena target was observed: **0 / 0 / 0 emitted probes**, not an availability result. The most material remaining collection gaps are skipped-run reasons, end-to-end phase timestamps, actual GH API call counts, and representative successful-run usage.
