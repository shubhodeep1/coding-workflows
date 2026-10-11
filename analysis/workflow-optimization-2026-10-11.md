## Executive Summary

- **Review latency is the largest bottleneck.** In `shubhodeep1/coding-workflows`, review run **38083242662** waited about **97 minutes** between its gate finishing and its reviewer job starting; run **38086341818** waited about **90 minutes**. The job-system logs show a wait for a hosted runner. Record gate-to-runner queue time separately and suppress superseded review work safely; the *upper bound* on a win is roughly 90–97 minutes for runs like these, not a guaranteed saving. **Confidence: high** on the wait, medium on recoverable impact.
- **CI has a repeated infrastructure failure signature.** Five of six failed CI runs, including **38083227102** and **38098079703**, show `static-checks` receiving a runner shutdown signal; the final `lint` job then reports that job as unsuccessful. Add collector-side `runner_shutdown` classification and a bounded same-commit retry of the affected job while retaining the required CI gate. This could avoid a full ~11-minute CI cycle when the retry succeeds. **Confidence: high.**
- **Two reviews of the same PR head failed identically.** Runs **38085885171** and **38088093755** completed an initial reviewer pass, then reported `malformed_output`; the fallback exited 1 and the review failed. Emit a sanitized schema-rejection category and fallback exit category, rather than only `reviewers_failed` with no first error. Earlier diagnosis should reduce repeated failed review work; time savings are unmeasured. **Confidence: high.**
- **One implementation exhausted five costly no-change attempts.** Run **38083270579** spent **7,177 seconds** overall and reported no file changes on every attempt. Its reported cumulative Claude model cost rose from **$5.28 after attempt 2** to **$14.54 after attempt 5**; those figures are model telemetry, not audited billing. An opt-in stop-and-escalate rule for repeated, identical no-change outcomes could have avoided up to **$9.26 reported cost** and roughly **39 minutes** in this case. **Confidence: medium** on transferable savings.
- **The main audit gap is attribution.** The assembled context covers **1,000 runs**, but has log telemetry for **119**; API-budget snapshots in 11 deep-dive reviews all say `used_in_job=unknown`. Add queue, failure-class, attempt, and API-call-count events before changing model or API behavior. Expected impact is better targeting of the failures above, not a directly measurable speedup. **Confidence: high.**

## Speed Optimizations

1. **Critical path — distinguish and reduce reviewer queueing.** Review runs **38083242662** and **38086341818** finished `review / gate` at approximately 20:32:54 and 21:08:27 UTC, but `review / codex-agent` began at 22:10:16 and 22:37:58 UTC, respectively. The gate itself took seconds; the intervening wait dominates both runs. **Change:** have the collector emit `AI_JOB_TIMING_V1` with run ID, job, gate-complete time, runner-start time, `queue_ms`, `compute_ms`, and timestamp source. Then use the existing PR-head checks to stop *verified superseded* work before it occupies a reviewer runner, without canceling a run that may still write valid results. **Estimated saving:** up to the observed 90–97-minute wait on an affected run if capacity is freed; actual reduction must be measured. **Risk:** medium for cancellation logic, low for logging.

2. **Critical path — recheck freshness on reviewer entry.** Four of **11 deep-dive review summaries** ended `stale_base_skip`; run **38083242662** waited roughly 97 minutes for its reviewer job and then recorded **538 seconds** of review budget before that outcome. **Inference:** the base can become stale during queueing. **Change:** perform a current-head/base freshness check immediately after runner assignment and before model work, preserving the existing final merge checks. **Estimated saving:** up to roughly **8–9 minutes of compute** on a run like 38083242662, with no claim that it removes queue time. **Risk:** low if uncertain API responses retain the current safe path.

3. **Critical path — bound repeated implementation no-ops.** In **38083270579**, `Run Codex implementation` logged five attempts with no file changes, from 21:14:40 to the 22:16:25 failure. **Change:** emit `IMPLEMENT_ATTEMPT_V1` with attempt, duration, model, outcome, changed-file count, and a non-content failure fingerprint. Trial stop-and-escalate after two *identical* no-change results on an unchanged head; retain retries for transient errors or changed diagnostics. **Estimated saving:** up to ~39 minutes after attempt 2 in this instance. **Risk:** medium; a later attempt might succeed, so test the rule against historical attempts first.

4. **Micro-optimization — avoid unnecessary diagnosis only for a known harness signature.** Validate run **38083210967** identified a missing rootless Docker cgroup path before application tests, yet proceeded through diagnosis and ended `harness_error` after **1,375 seconds**. **Change:** for that exact preflight signature, emit a structured harness failure and skip application-focused diagnosis while preserving the failed validation verdict and human-readable summary. **Estimated saving:** potentially minutes of diagnosis; its exact duration is not isolated in telemetry. **Risk:** low if narrowly keyed, medium if generalized to Docker failures.

## Cost Optimizations

1. **Repeated no-change implementation is the clearest avoidable spend.** Run **38083270579** accounts for **1,322,139 of 1,324,171** assembled Codex tokens and logged five no-change attempts. The reported cumulative `modelUsage.costUSD` difference between attempts 2 and 5 is **$9.26**. Add per-attempt usage and outcome logging, then trial the guarded escalation described above. **Estimated saving:** up to that reported $9.26 for a recurrence with the same outcome; no fleet-wide dollar estimate is defensible. **Quality risk:** stopping a recoverable attempt, mitigated by fingerprint and unchanged-head checks.

2. **Preserve review quality while measuring model and reasoning choices.** The two failed reviews **38085885171/38088093755** show a successful Sonnet initial pass, malformed output on the subsequent pass, and an unsuccessful Haiku fallback. Run **38083270579** configured high reasoning and switched its editor fallback only on the final attempt. **Change:** log model, reasoning level, pass, schema-validity result, and usage per attempt; test cheaper settings only on a bounded pass after confirming schema and finding quality. **Estimated saving:** unknown until pass-level usage is collected. **Quality risk:** high for an unconditional reviewer downgrade; do not make one.

3. **Measure context value before trimming it.** The assembled review family recorded **69 OpenRouter calls**, **244,424,171 total reported tokens**, and **4 calls without available usage**. Semble contributed **11 `reviewer-context` queries**, **152,619 logged bytes**, and **64 sources**; validate contributed one **4,191-byte** query. `static_dup_bytes=0` is encouraging, but there is no unqueried-prompt baseline proving net prompt reduction. **Change:** record selected-source use and prompt bytes before/after context insertion; trim only demonstrably unused chunks. **Estimated saving:** unquantifiable in tokens today; the measured exposure is **156,810 Semble response bytes**. **Quality risk:** removing relevant evidence.

4. **Avoid rerun spend from infrastructure failures.** Five CI failures have the same runner-shutdown signature, while the implementation and review failures above also generated repeat work. **Change:** classify infrastructure failures separately so any bounded retry targets the failed job at the same commit, not another AI pass. **Estimated saving:** up to one avoided full CI rerun per recovered incident; AI-token savings cannot be assigned from these CI logs. **Quality risk:** low if no failed check is waived.

Serena supplied **zero observed queries or downstream tool replacements**. Its two validate fallbacks in **38083210967** explicitly say `reason=disabled`; there are no response bytes or per-tool results from which to claim savings or noisy output. Semble’s 12 queries average about **13.1 KB** of logged response each; contribution is measurable, but value relative to the prompt it replaced is not.

## Reliability Improvements

1. **Classify runner loss outside the dying job.** CI runs **38083227102, 38090075498, 38093326588, 38095072349, and 38098079703** all show a shutdown signal in `static-checks`, followed by an aggregate `lint` failure. **Category:** runner/infrastructure, not a proven static-check assertion. **Fix:** in the collector, associate the original failed job and step with a `CI_FAILURE_V1` class, runner outcome, and cancellation-request status; allow at most one same-commit retry for classified runner loss. **Expected impact:** potentially recover five of six observed CI failures *if* the runner loss is transient; the logs do not establish that it is. **Rollback:** disable automatic retry; never mark the gate successful without a passing check. Run **38082950546** is distinct: its test job failed with `BrokenPipeError` in `tests/test_dependency_registry_proxy.py`; retain that as a test failure for separate investigation.

2. **Make reviewer failures actionable.** Runs **38085885171/38088093755** failed at `Run reviewer models` with the same malformed-output-then-fallback-exit sequence; both logged `first_error_present=0`. **Category:** output contract followed by an insufficiently classified fallback failure; the fallback’s underlying cause is unknown. **Fix:** extend `REVIEWER_POOL`/`REVIEWER_SLOT_STATE` with sanitized parser category, expected schema version, fallback exit class, and whether a usable earlier pass exists—without logging raw model output. **Expected impact:** faster separation of parser, model, and provider incidents; failure-rate reduction requires a subsequently verified fix. **Rollback:** keep existing failure and fail-closed review behavior.

3. **Surface validation harness failure before diagnosing code.** In **38083210967**, Docker could not start the validation container because the rootless cgroup path was missing; `Enforce validation outcome` correctly failed with `raw_status=harness_error`. The same run also logged **four** fail-open memory force-tick get/put entries after clone failures, including a warning that dispatch proceeded without a persisted cooldown claim. **Category:** harness provisioning, with a separate memory-persistence risk. **Fix:** emit a concise preflight signature and runner environment class, and log whether the force-tick claim persisted plus the resulting dispatch decision. **Expected impact:** avoid misrouting a harness failure as an application fix and expose possible duplicate dispatch; frequency is unknown from one validate run. **Rollback:** preserve the failed validation verdict and existing fail-open memory behavior.

The assembled telemetry records **0 `BREAK_GLASS`** and **0 `CONTEXT_BUDGET_WARN`** events across its covered runs: no observed policy/rubric-pressure or prompt-size warning, not proof of absence in unobserved logs. All **68 Semble fallbacks** are classified as **CI contract-test** events (`target=overflow`, simulated missing binary); **0 runtime fallbacks** were recorded. This is healthy test coverage, not evidence of a broken production rollout. Serena’s two `target=validate phase=diagnose reason=disabled` fallbacks are neither probe failures nor evidence of runtime unavailability. Add an explicit disabled/probe-skipped availability event if rollout availability needs auditing.

## AI Memory Health

Across **12 distinct deep-dive retrieves**, **12 selected records**: observed hit rate **100%**. Average `estimated_tokens` was **1,406** against an average **1,417-token** budget, or **99.3%** of the combined budgets. Keyword methods were **11 `llm`**, **1 `plain`**, **0 `none`**. Reviewer retrieves, for example **38085885171**, selected about 24–25 records close to a 1,400-token cap; implementation run **38083270579** selected 28 within 1,600. No zero-record or `enabled:false` retrieve appeared in those deep dives.

The narrow concern is headroom, not hit rate. Record retrieved-token-to-budget ratios and whether selected lessons were cited or used; trial a small, relevance-ranked reserve rather than deleting lessons wholesale. This may reduce repeated context, but token and quality impact require measurement. `record-run-event` pushes used more than one attempt in **10 of 26** observed events, reaching **3 attempts** in review run **38083242662**; log retry class and elapsed time without logging record contents. Validate run **38083210967** had **four** `fail_open:true` force-tick get/put entries. A recent summary for **38098313886** also shows `finalize-task` with `fail_open:true` and `reason=no_linked_issues`; that is a scoped no-op, not a demonstrated memory outage. Finalize, promote, and compact success rates cannot be calculated from the provided deep dives; verify consistent emission for those operations.

## GH API Call Audit

- **Measured probes, not measured workload calls:** 11 deep-dive review runs emitted **44 `GH_PAT_BUDGET` snapshots**—start and end for both `gate` and `codex-agent`. Every `used_in_job` was `unknown`, including **38086341818**. The repository’s `gh_pat_budget` helper notes that `/rate_limit` does **not** debit the primary REST allowance; these snapshots must not be presented as 44 primary-quota units or as total API usage. **Change:** count endpoint families, attempts, cache hits, response classes, and retry sleep in existing GitHub API helpers, using sanitized endpoint templates and no request bodies. **Expected impact:** identify real hotspots and rate-limit exposure; no call reduction yet.
- **Do not invent a per-item hotspot.** Review run **38086341818** explicitly reused an early linked-issue fetch (`count=1`); these logs do not establish an unbatched issue-lookup loop. For any measured N-item poller hotspot, first extend its existing fetch or use the repository’s `_fetch_candidate_issue_details_graphql` / `_fetch_linked_pr_status_graphql` and cycle-local caches. **Conditional reduction:** from N lookups to `ceil(N / validated_batch_size)` calls; absolute savings and rate-limit reduction require endpoint counts. Cache misses must retain the smallest safe legacy read.
- **Investigate cancellation latency with call attribution.** The unselected-run summary for **38097716231** attributes about **280 of 289 seconds** to `cancel-active-runs`, but supplies no endpoint counts or retry timing. Add operation-level call/retry and wait durations there before proposing batching. **Expected impact:** isolate API wait from local processing; savings are presently unknown.

No live rate-limit event count or complete API call total is supplied. The `octo/broken` `gh api` timeout visible in CI test logs is a test fixture, not a production rate-limit incident. Follow this repo’s API-hygiene rule: reuse a call before adding one, batch measured N-item reads, and retain fail-open cache-miss behavior.

## Prompt Cache & Memory System

The assembled review telemetry reports **227,726,784 OpenRouter cache-read tokens**, **2,342,398 cache-write tokens**, and **13,791,519 prompt tokens**. Cache reads are about **93.2% of reported `or_total_tokens`**, but that share is **not** a provider cache-hit rate or a dollar discount. The aggregate `cache_hit_rate` is **null**; available run values are **96.8%** (**38083227253**), **75.7%** (**38086358838**), and **67.7%** (**38097305729**). Four of 69 calls lack usage.

**Change:** emit a non-reversible stable-prefix hash, model, prompt-section byte counts, and per-call cache creation/read counters; keep static rules and retrieved memory ahead of run-specific noise only where the provider’s prefix behavior and safety checks permit it. Compare the same review tier and model before changing layout. **Expected impact:** measurable cache and latency differences on subsequent runs; the three observed rates do not justify a numeric savings forecast. The absence of `CONTEXT_BUDGET_WARN` in covered telemetry lowers the observed prompt-growth alarm, but memory retrieves at **99.3%** of budget merit headroom monitoring. Semble `static_dup_bytes=0` does not establish that its **156,810 logged bytes** reduced net prompt expansion.

## Orchestrator Health

The `orchestrate_poll` family had **19 runs: 14 successes, 5 cancellations**, with **1,081-second p50** and **1,622-second p95** durations; it has **no log telemetry** in this window. Thus wave progression, deferrals, terminal stalls, and poll-time API retries are not attributable. Add `AI_ORCHESTRATOR_TICK_V1` at tick end with tracking issue, wave before/after, active/blocked/deferred counts, conflict-retry count, pending-check wait, and terminal reason. Expected impact is detection of repeated no-progress ticks; no speed estimate is supportable yet.

Review evidence supplies two concrete flow signals: **38088136232** deferred auto-merge with `reason=base_moved_overlap`, and **38083227253** pushed an autofix and issued a continuation dispatch. Four of 11 deep-dive reviews ended `stale_base_skip`. Track stale skips and continuation outcomes by PR head so safe freshness checks can be moved earlier. CI’s “integration fingerprint verification FAILED” lines occur inside orchestrator **unit tests**; they demonstrate a protective conflict check, not live conflict-heal failures in these 19 poll runs.

## Pipeline Flow Bottlenecks

| Segment | Observed bottleneck | Smallest next step |
|---|---|---|
| Clarify → plan | Both families have **1-second p50** largely amid skipped runs; active examples include clarify **38097942409: 321s** and plan **38098417600: 698s**. | Emit active-versus-skipped phase time; do not optimize from the skipped-run median. |
| Implement | **38083270579: 7,177s**, including about **50 minutes before its first job log** and five no-change attempts. | Split queue and per-attempt compute; trial guarded no-op escalation. |
| Review/autofix | **79 runs**, **1,997s p50**, **6,327s p95**; two deep dives have 90–97-minute gate-to-reviewer waits. | Prioritize queue attribution and safe superseded-work checks before micro-optimizing Semble’s ~33-second average bootstrap. |
| Validate | The sole validate run, **38083210967: 1,375s**, failed at harness preflight rather than application tests. | Classify the exact harness signature early; retain failure. |
| CI and merge | CI **695s p50**, **6/50 failures**, five with runner shutdown; review **38088136232** deferred on a moved base. | Classify and retry infra-only failures; log base-move deferrals and their next-run resolution. |

Queue time, model compute, retries, and merge/conflict overhead must remain separate in the collector. The 1-second all-run p50 is not an end-to-end delivery-time measure because **693 of 1,000 runs were skipped**.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review queue and compute (**38083242662**, **38086341818**); implementation queue and repeated no-change work (**38083270579**); CI’s ~11-minute median gate.
- **Top failure modes:** five `static-checks` runner shutdowns among six CI failures; two same-head reviewer failures (**38085885171/38088093755**); one Docker harness failure (**38083210967**). **Highest-cost drivers:** review’s **244.4 million reported OpenRouter total tokens** in the assembled telemetry and the failed implementation’s **1.322 million Codex tokens**.
- **Top three actions, in order:** **(1)** add collector-side job queue and original-failure classification, then bounded infra-only CI retry; **(2)** log review pass/schema/fallback causes and check freshness on reviewer entry; **(3)** log implementation attempts and trial unchanged-head no-op escalation. Each retains existing validation and merge gates.

## Metrics Appendix

*Scope:* assembled context for **1,000** runs in one repository. `summary.json` alone reports telemetry on **21** selected runs; the assembled context reports **119** after wider run-summary coverage. Costs, cache, and MCP totals below use the assembled context; detailed causes use deep-dive logs. Sampling and missing usage limit fleet-wide cost conclusions.

| Runs | Success | Failure | Cancelled | Skipped/other | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|
| All | 285 (28.5%) | 10 (1.0%) | 12 | 693 | 1s / 1,768s |
| CI | 44/50 | 6/50 (12%) | 0 | 0 | 695s / 995s |
| Review/autofix | 76/79 | 2/79 (2.5%) | 1 | 0 | 1,997s / 6,327s |
| Implement | 3/140 | 1/140 | 0 | 136 | 1s / 10s |
| Orchestrate poll | 14/19 | 0 | 5 | 0 | 1,081s / 1,622s |
| Validate | 0/1 | 1/1 | 0 | 0 | 1,375s / 1,375s |

Excluding cancelled and skipped runs, the observed failure fraction is **10/295 (3.4%)**. Implement’s family percentiles mostly describe skipped invocations, not its active-run latency.

| Assembled telemetry metric | Value |
|---|---:|
| Runs with log telemetry; wall-clock samples | 119; 117 |
| Telemetry `wall_clock_p50_ms` / `wall_clock_p99_ms` | 8,000 / 7,381,120; sampled distribution, not step latency |
| Codex calls / tokens used | 26 / 1,324,171 |
| OpenRouter calls; usage available / unavailable | 69; 65 / 4 |
| OpenRouter prompt / completion / total tokens | 13,791,519 / 567,745 / 244,424,171 |
| OpenRouter cache write / read tokens | 2,342,398 / 227,726,784 |
| Aggregate `cache_hit_rate` | Unavailable; three run values: 96.8%, 75.7%, 67.7% |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 in covered telemetry |
| GH API calls by endpoint; live rate-limit events | Not collected / not established |
| Deep-dive `GH_PAT_BUDGET` snapshots | 44 across 11 review runs; 44 `used_in_job=unknown` |

| MCP telemetry, assembled context | Queries | Logged response bytes | Bootstrap | Fallbacks | Availability |
|---|---:|---:|---:|---:|---|
| Semble `reviewer-context` | 11 | 152,619; 64 sources | 11; 371,376ms | 0 runtime | No probe series |
| Semble `validate-diagnose-context` | 1 | 4,191; 1 source | 1; 27,034ms | 0 runtime | No probe series |
| Semble CI `overflow` contract tests | 0 | 0 | 0 | **68 simulated** | Not a runtime availability test |
| **Semble total** | **12** | **156,810; 65 sources** | **12; 398,410ms** | **68 test / 0 runtime** | 0 failed or unused bootstraps |
| Serena `validate` | 0 | 0 | — | 2, both `reason=disabled` | Probe ok / failed / skipped: **0 / 0 / 0** |
| Serena other targets | 0 | 0 | — | 0 | Probe ok / failed / skipped: **0 / 0 / 0** |

Semble reported **0 `static_dup_bytes`** and **0 dropped echo lines**. Serena reported **0 query tool calls**, so a per-tool breakdown is not available. **Other MCP servers observed:** none in the inspected deep-dive events; continue generic-prefix collection so a new server is not silently omitted.
