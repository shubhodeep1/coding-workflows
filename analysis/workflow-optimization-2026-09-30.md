## Executive Summary

- **Fix measurement before changing model spend.** In `shubhodeep1/coding-workflows`, six slow review runs contain the same OpenRouter and Semble events in aggregate job logs and individual step logs. The collector reports **124.06M tokens and 173 calls** for the sampled runs; counting canonical step events gives **72.90M tokens and 109 calls**. The 51.16M-token difference is *reporting inflation, not realized cost savings*. Impact: reliable prioritization and budgets; **confidence: high**.
- **Review latency has two observable non-model costs.** Five review runs, including `36648823889` and `36647263924`, exhausted a **300-second check-run wait**; six segmented runs spent **96–498 seconds** in “Free disk space.” Logging check identities and disk headroom should precede changes to either safeguard. Potential saving: up to the overlapping wait or unnecessary cleanup time per eligible run; **confidence: high for durations, medium for achievable savings**.
- **One concrete CI failure needs a small fix.** CI run `36649492354` failed “Inventory parity” because `docs/INVENTORY.md` did not document `scripts/guard_differential.py`; it finished after 548 seconds. Update the inventory on the affected branch and run parity before submission. Impact: prevent recurrence of this failure; **confidence: high**.
- **MCP availability is not the failure pattern shown here.** The failed CI log’s four distinct `SEMBLE_FALLBACK target=overflow` events are labelled `context=contract-test`; duplicate log copies make that run’s row report eight. No runtime fallback or Serena probe was observed. Impact: avoid treating a contract test as a broken rollout; **confidence: high for the inspected run**.
- **Coverage limits broader conclusions.** Only **16 of 1,000 runs** have downloaded, parsed logs in the folder summary; ten slow review runs supply all measured OpenRouter usage. The assembled analysis calls 115 rows “with log telemetry,” but 99 of those rows have no downloaded logs. Do not extrapolate sampled costs or infer orchestrator stalls from skips alone. Impact: more trustworthy trend detection; **confidence: high**.

## Speed Optimizations

1. **Critical path — diagnose, then overlap check-run waiting.** The “Collect PR check-run failures CI lint autofix context” step reached its 300-second timeout in `36648823889`, `36646798608`, `36647239481`, and `36647263924`; the aggregate log shows the same timeout in `36641109403`. `scripts/collect_pr_check_runs_context.py` logs the number pending but not their names or origin, so the cause is unknown. Emit a per-poll summary of check name, app, status, elapsed time, self-run exclusion count, and exit reason. If those checks can safely be polled during independent disk cleanup, start polling earlier and join before review **without shortening the timeout or weakening the merge gate**. Estimated saving where overlap is possible: **96–211 seconds** in the four runs with segmented cleanup timings; implementation risk **medium**.

2. **Critical path — make cleanup conditional only after measuring headroom.** “Free disk space” took 498 seconds in `36648413566`, 214 in `36648758366`, and 166 in `36648823889`; it was also the step at cancellation in `36648716510` and `36641494664`, though the cancellation cause is unknown. Log bytes available before and after cleanup and its action duration. Then test skipping it only above a verified safe free-space threshold, retaining the existing cleanup otherwise. Estimated saving: **0–498 seconds per eligible sampled run**; risk **medium** because an incorrect threshold could cause disk failures.

3. **Critical path — profile reviewer slots before changing models.** “Run reviewer models” took **337–1,074 seconds** across six segmented slow reviews; `36648823889` took 765 seconds in that step. Emit per-slot elapsed time, pass, retry class, cache-read tokens, and verdict. Pilot any pass or reasoning-level reduction against review-quality outcomes before rollout; the latency saving is **not estimable from current evidence** and a model change carries **high quality risk**.

4. **Failure-feedback win, not a routine-run shortcut.** In CI `36649492354`, inventory parity failed about 32 seconds after run creation, but parallel CI work continued to the 548-second conclusion. Add a pre-submission parity check on affected changes; consider an early CI gate only if its startup cost does not slow successful runs. Potential failure-feedback improvement is **up to roughly eight minutes on a similar failure**; implementation risk **low for local preflight, medium for CI dependency changes**. Semble’s observed reviewer queries took roughly 0.4–0.6 seconds each, so optimizing them first would be a micro-optimization.

## Cost Optimizations

1. **Correct attribution first.** `scripts/collect_workflow_logs.py` has a structured-line deduper, but its full-line key includes timestamps: the two copies of `SEMBLE_QUERY` in `36648823889` have the same fields and timestamps differing by microseconds. Normalize the timestamp and pair matching parent-job and child-step events **one-to-one**, with a regression test that preserves genuinely repeated calls. This removes **51.16M falsely attributed tokens, 64 calls, and 84,972 falsely attributed Semble bytes** from this sample; it saves **no actual tokens or dollars**. Risk: **low with paired-event tests**.

2. **Target fresh prompt expansion, not cached reads indiscriminately.** The eight usage-complete slow reviews have **17.62M canonical prompt tokens** and a **73.09% weighted cache-hit rate**. The review warnings in `36641109403` and `36641456604` show prompts of **153,066** and **173,855** tokens against a 140,000-token warning threshold. Preserve the stable prefix, put run-specific material later, and remove demonstrably duplicated context; log static-prefix fingerprint and dynamic-byte counts to validate the cause. A *hypothetical* 10% cut in fresh prompt tokens across those eight runs is **1.76M tokens**; quality risk is **low only for verified duplication**. No price data supports a dollar estimate.

3. **Measure first- versus second-pass value before model changes.** Canonical logs contain **109 OpenRouter usage events** in ten slow reviews, including `pass1` and `review` calls; three inspected runs log reviewer reasoning effort `xhigh`. There are seven “reviewer … failed” warnings across five sampled runs, so automatically dropping a pass or downgrading reasoning could damage coverage. Log pass-specific finding contribution and cost, then run a guarded quality comparison. Savings: **unknown until that comparison**; quality risk **high**.

4. **Assess retrieval value rather than assuming it.** Ten distinct `SEMBLE_QUERY target=reviewer-context` events returned **12 chunks each and 140,861 logged bytes total**. There is no before/after prompt-size or finding-quality measure, so these lines do **not** establish that Semble reduces prompt expansion rather than adding context. Add selected-byte, displaced-byte, and finding-usefulness summaries; cap low-value chunks only after measurement. Serena has **zero queries and zero response bytes** in this window, so it cannot be credited with replacing downstream tool or model work. Savings: **unmeasured**.

5. **Prevent avoidable CI reruns.** Inventory drift caused the sole failed CI run, `36649492354`; the inspected run shows no OpenRouter calls and no retry. Fixing parity avoids approximately **548 seconds of CI run time per similar failure**, not measured token spend. Risk: **low**.

## Reliability Improvements

1. **Observability integrity — highest priority.** The collector’s exact timestamped deduplication misses job/step copies in six reviews and the failed CI run. For `36649492354`, eight reported contract-test fallbacks represent **four distinct events** in its step log. Fix the deduper as above, retain source-step provenance and a `duplicates_suppressed` count, and compare old/new totals during rollout. Expected impact: eliminate this demonstrated counting error; rollback to the old counter if distinct-event tests reveal undercounting, without changing workflow behavior.

2. **Documentation drift — actual CI failure.** `36649492354`, job `tests-release-and-log-analysis`, step “Inventory parity,” identifies the missing inventory entry for `scripts/guard_differential.py`. Add the entry on the affected branch and enforce parity before CI. Expected impact: prevent this failure class; **do not fail open** the parity gate.

3. **Unknown pending checks — latency and merge-safety risk.** Five sampled reviews timed out after 300 seconds and proceeded with a snapshot. The log gives a count, not the pending check identities; **inference:** an unrelated, stale, or legitimately slow check could explain the waits. Emit identities and final collection status, and keep an unknown/incomplete result distinct from passing in downstream decisions. Expected impact: identify whether waits can safely be reduced; retain the current timeout and required-check protections until proven safe.

4. **Partial reviewer and validation signals.** Seven reviewer-failure warnings occurred across sampled runs, including two in `36648413566`; their aggregate status does not provide a consistent actionable cause. Emit failure class, attempt, fallback, and quorum result while retaining successful reviewers. Separately, the `log_summary` for successful review run `36651391802` warns that no standalone validation workflow could be dispatched for merged PR **#5321**. Log dispatch decision and subsequent validation evidence; do not equate workflow success with validated merge. Expected failure-rate reduction is **not measurable yet**; preserve existing fail-open behavior for advisory logging, not for required validation.

`BREAK_GLASS` was **0** in collected telemetry. The two `CONTEXT_BUDGET_WARN` events, both in review runs, indicate **prompt-size risk**, not demonstrated rubric-pressure failure. The Semble fallbacks are contract-test events, not probe failures or evidence of a masked runtime rollout.

## AI Memory Health

In canonical deep-dive logs, **8/8 `retrieve` operations hit** (`records_selected` 25–26). They averaged **1,395 estimated tokens against a 1,400-token budget**; `keyword_method` was `llm` in all eight, with **0 `plain` and 0 `none`**. No observed retrieve selected zero records, set `enabled: false`, or reported `fail_open: true`. This is a positive sampled signal, but retrieval is operating close to its budget; log rejected-record counts and truncation reason before enlarging it.

The same logs contain **20 `record-run-event`** and **10 `record-candidate`** operations. **7/30 writes needed more than one push attempt**; the maximum was four on review run `36647263924`, and observed operations reported success. Emit a bounded retry-reason/status field to distinguish contention from API failure; the present attempt count does not prove rate limiting. A non-deep-dive `log_summary` for issue-PR status run `36651391781` reports a successful `ingest_implement_plan_lessons` with 39 parsed and one written. No deep-dive evidence establishes `finalize-task`, `promote`, `compact`, or processed-command coverage; verify those emissions in an appropriate active run.

## GH API Call Audit

| Observed or code path | Evidence and call assessment | Smallest safe action |
|---|---|---|
| Review check polling | Five timed-out reviews log **28 “Waiting” iterations** combined. The loop in `scripts/collect_pr_check_runs_context.py` performs a paginated check-runs GET before each wait and one final GET: **at least 33 GET attempts**, excluding extra pages or retries. Actual request counts and rate-limit headers are not emitted. | Log endpoint *template*, pages, attempts, remaining-limit signal, cache status, pending check identities, and timeout reason. Reuse a completed snapshot within a cycle; do not suppress required-check polling. |
| Orchestrator issue lookups | `scripts/orchestrate_poll_process.sh` contains per-row issue and optional comment GET paths, but the eight poller runs have **no deep-dive logs or measured call counts**. Executed volume is unknown. | Instrument calls per cycle and batch size. If the path is active for multiple rows, reuse the existing candidate-details/linked-PR GraphQL helpers and cycle-local caches instead of adding per-item lookups. Savings are conditional on measured row count. |
| Memory pushes | Seven of 30 sampled memory writes required repeated push attempts; HTTP cause and request count are absent. | Log sanitized retry class and attempt count, without payloads. Do not classify these as rate-limit events without response evidence. |

Repository guidance in `unattended_system_instructions.md` §14 and `agents.md` already requires batching, reuse of `ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`, and `_candidate_details_json`, and fail-open cache misses. Follow those rules rather than add another cache or service. **No rate-limit event or repository-wide GH API total is measurable** from this log set; emit request counts at `gh_retry` and report per workflow/job/step before claiming a call-count reduction.

## Prompt Cache & Memory System

The folder summary’s `cache_hit_rate` is **null** because five OpenRouter usage events lack complete usage data, despite **8/10** slow review runs having individual rates (**55.55%–84.13%**). Recomputed from canonical, usage-complete events, the weighted rate is **73.09%**. Canonical logs record **51.17M cache-read tokens** across the ten reviews and **0 reported cache-write tokens**; zero reported writes does not establish that no cache creation occurred.

Add a per-call coverage line containing phase, model, usage availability, cache-enabled/breakpoint status, read/write counts, and a fingerprint of the *non-sensitive static prefix*. Compare that fingerprint across runs before attributing misses to unstable prefixes or dynamic noise. Keep review memory’s 1,400-token retrieval budget, but log selection/truncation so its near-full utilization does not silently grow prompts. The two context-budget warnings merit targeted context trimming; estimate latency and reliability improvement **only after** recording prompt size and cache behavior before and after. No observed cache fail-open event justifies changing its current behavior.

## Orchestrator Health

There are **645 skipped runs**, including 154 each in plan, implement, and orchestrate-clarify-respond, and 145 in clarify. Recent examples `36652090406` (plan) and `36652090345` (implement) have empty archives. A skipped workflow with no job log is **not evidence of a stuck wave**. Emit a sanitized eligibility/skip decision at the dispatching gate or collect its job-condition outcome, with issue/PR correlation, phase, prior state, and decision reason.

All eight `orchestrate_poll` runs succeeded but have a **447.5-second p50** and no deep-dive step evidence. Add one cycle summary covering wave transition, clarification count, deferral reason, conflict-heal attempt, terminal outcome, API calls, and time spent waiting versus computing. Track repeated identical state transitions and age since last productive transition. The validation-dispatch warning for PR #5321 warrants a separate observable `dispatched | already_covered | unavailable` outcome; whether that PR lacked validation cannot be determined here.

## Pipeline Flow Bottlenecks

- **Clarify → plan → implement:** Clarify had 18 successes among 163 runs; plan had two among 156; implement had zero among 154, all skipped. This window cannot establish conversion or clarification-loop rates without correlated issue/PR and skip-reason events. Add phase-transition timestamps and reason codes.
- **Review/autofix:** The dominant measured compute is reviewer models (**337–1,074 seconds** in six segmented slow runs). Separate from it the **300-second check wait** in five runs and **96–498-second cleanup** in six; log these as model compute, external wait, and runner preparation respectively.
- **Validate/CI:** CI p50 is **537.5 seconds** over 14 runs; `36649492354` failed a fast parity check but concluded after 548 seconds of parallel work. Capture first-failure time and remaining-job time for early-feedback decisions.
- **Queue, retry, merge/conflict:** Queue time and merge/conflict overhead are **not measured**. The inspected failed CI run has zero retries, while sampled memory pushes do retry internally; these are different measures. Record job queued/start timestamps, request attempts, and conflict-heal outcomes before ranking those overheads against observed review waits.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** long reviewer-model steps, check-run waits, disk cleanup, then CI’s roughly nine-minute runs. **Top failure modes:** one inventory-parity CI failure; incomplete check-run snapshots; duplicated telemetry that obscures costs. **Highest-cost driver:** sampled slow review OpenRouter usage—**72.90M canonical total tokens across ten runs**, not the collector’s 124.06M.

**Top three actions:** (1) fix paired-event deduplication and log provenance; (2) add pending-check identity/API-attempt and disk-headroom diagnostics before altering review timing; (3) repair affected-branch inventory parity and require pre-submission checking. These preserve review and merge safeguards while making subsequent optimization decisions testable.

## Metrics Appendix

All-run durations include skipped runs. Cost figures below cover **ten selected slow review runs**, not all 1,000 runs; “canonical” means aggregate-job copies were excluded when individual step logs were present.

| Family | Runs | Success | Failure | Cancelled | Skipped | Success/run | Failure/run | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| All | 1,000 | 349 | 1 | 5 | 645 | 34.9% | 0.1% | 2 / 423 s |
| Review/autofix | 217 | 213 | 0 | 4 | 0 | 98.2% | 0% | 13 / 1,282.4 s |
| CI | 14 | 13 | 1 | 0 | 0 | 92.9% | 7.1% | 537.5 / 549.4 s |
| Orchestrate poll | 8 | 8 | 0 | 0 | 0 | 100% | 0% | 447.5 / 484.6 s |
| Clarify | 163 | 18 | 0 | 0 | 145 | 11.0% | 0% | 1 / 162 s |
| Plan | 156 | 2 | 0 | 0 | 154 | 1.3% | 0% | 1 / 10 s |
| Implement | 154 | 0 | 0 | 0 | 154 | 0% | 0% | 1 / 9.3 s |
| Issue-PR status | 18 | 18 | 0 | 0 | 0 | 100% | 0% | 75.5 / 171.2 s |

Among the **355 non-skipped runs**, 349 succeeded (**98.3%**); this denominator includes five cancellations. Success is not equivalent to an AI model call.

| Sampled cost/review measure | Collector-reported | Canonical-log baseline or coverage |
|---|---:|---:|
| OpenRouter calls; total tokens | 173; 124,060,643 | **109; 72,902,998** |
| Prompt; completion tokens | 33,528,549; 1,748,116 | **20,664,460; 1,065,412** |
| Cache-read; cache-write tokens | 88,786,316; 0 | **51,174,807; 0 reported** |
| Usage available; unavailable | 168; 5 calls | **104; 5 calls** |
| `cache_hit_rate` | null aggregate | **73.09%**, eight complete runs only |
| `wall_clock_p50_ms`; `wall_clock_p99_ms` | 1,456,000; 2,005,300 | 16 downloaded-log run samples; not model-call latency |
| `break_glass_count`; `context_budget_warn_count` | 0; 2 | 0; **2 distinct review warnings** |
| Codex calls; tokens | 0; 0 recorded | No Codex usage line in this sample |

| MCP / API measure | Recorded result | Interpretation and gap |
|---|---:|---|
| Semble reviewer-context queries / logged bytes | Collector **16 / 225,833**; canonical **10 / 140,861** | One distinct query per inspected slow review; mean **14,086 bytes**. Prompt displacement unmeasured. |
| Semble fallbacks | Folder summary **8**; assembled run rows **16**; failed CI `36649492354` **4 distinct / 8 reported** | All classified **contract-test**; **0 runtime** reported. The other CI run’s distinct count lacks a deep-dive log. A fallback/query rate across these different workloads would be misleading. |
| Serena queries / response bytes / tool calls / query time | **0 / 0 / 0 / 0 ms** | No per-tool breakdown exists; `SERENA_ENABLED: false` is reported in review run `36651713955`. |
| Serena fallbacks; probes ok / failed / skipped | **0; 0 / 0 / 0** | No availability conclusion without probes. |
| GH API calls; rate-limit events | **At least 33 check-run GET attempts** in five timed-out reviews; no total or rate-limit counter | Pagination, retries, other endpoints, and rate-limit incidence are unmeasured. |
| Other MCP servers observed | **None validated** | No other-server query, fallback, or probe totals to report. |

| MCP target | Distinct query / bytes confirmed | Fallback evidence | `probe_ok` | `probe_failed` | `probe_skipped` |
|---|---:|---|---:|---:|---:|
| Semble `reviewer-context` | 10 / 140,861 | 0 runtime observed | 0 | 0 | 0 |
| Semble `overflow` | 0 / 0 | 4 distinct contract-test events in failed CI; second CI not independently verified | 0 | 0 | 0 |
| Serena — no target observed | 0 / 0 | 0 observed | 0 | 0 | 0 |

**Collection gap:** folder `summary.json` counts **16 downloaded parsed logs**; the assembled analysis counts **115 `log_parsed` rows**, including **85 `not_selected`** and **14 `empty_archive`** rows. Its 113-sample wall-clock percentile therefore answers a different question from the folder’s 16-sample percentile. Report downloaded-log coverage, summarized-only coverage, and zero-filled rows separately before publishing another aggregate.
