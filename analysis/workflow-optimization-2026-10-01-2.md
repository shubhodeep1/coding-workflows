## Executive Summary

- **Repeated resolver failure is the clearest reliability pattern.** Ten of 13 failed runs, all in `shubhodeep1/coding-workflows`, ended at `review / codex-agent` with “Resolver scope check failed closed (ValueError)” (for example, runs 36819986387 and 36826661058). Their elapsed durations total **6,203 run-seconds**, though runs overlapped. The guard correctly refused to commit, but its log conceals which scope invariant failed. Add a safe reason code before changing guard behavior; resolving the underlying cause could address up to **10 failures in this window**. **Confidence: high** for the pattern, low for the prospective reduction.
- **Long reviews, not ordinary skipped triggers, dominate latency and observed token use.** Review/autofix p50 was 11 seconds but p95 was 1,532 seconds. In run 36823417327, `Run reviewer models` occupied about **2,266 of 2,673 seconds**, including retryable reviewer failures. Instrument per-slot elapsed time and trial existing fallback controls without weakening review of workflow-source changes. Potential saving is **minutes on affected outliers**, not a measured fleet-wide gain. **Confidence: high/medium.**
- **GitHub API pressure caused a separate failure.** Run 36823474603 failed in nine seconds at `review / gate / Resolve trusted review support commit` with a GitHub API rate-limit response. Bounded retries on that trusted read, plus call-count logging on existing wrappers, could prevent an isolated transient failure; a sustained hard limit must still fail closed. **Confidence: high** for the failure, medium for retry benefit.
- **Cost totals need a deduplication fix before dollar decisions.** The collector reports **212 OpenRouter calls and 467.08M tokens**; comparing aggregate-job and individual-step logs for the ten token-bearing runs yields **118 distinct usage lines and 260.97M logged tokens**. Deduplicate those representations in `scripts/cost_audit.py` before interpreting savings. **Confidence: high.**
- **Scope is narrow.** The 1,000 runs span approximately **05:26–06:56 UTC on October 1, 2026**, in one repository. Only 30 have downloaded deep-dive logs; GH API call totals and complete clarify→merge journeys are unavailable. Add coverage and phase-transition counters rather than extrapolating this window to a week. **Confidence: high.**

## Speed Optimizations

Ranked by plausible **end-to-end** effect; savings below are conditional, not promises.

1. **Critical path — diagnose and eliminate resolver scope failures.** Runs 36819986387 and 36826661058 typify ten review failures lasting **475–819 seconds each**. `scripts/review_conflict_resolve.sh` catches several distinct scope-invariant exceptions but logs only their class. Emit `RESOLVER_SCOPE_FAILURE action=check reason=<fixed-enum> attempt=<n>` for conditions such as changed merge state, invalid snapshot, or unsafe path—**never raw paths or exception text**. Use the resulting reason distribution to add a pre-model check *only if that invariant can be checked beforehand*; retain fail-closed behavior. **Estimated saving:** potentially several minutes per affected run if a preflight catches the cause; otherwise primarily faster diagnosis. **Risk:** low for logging, medium for a subsequent fix.
2. **Critical path — constrain pathological reviewer-slot time.** In run 36823417327, `Run reviewer models` took approximately **2,266 seconds**; its minimax slot recorded `server_error`, `stall_guard`, and a fallback before terminal failure. Preserve reviewer quorum and protected-file review, but emit per-slot start/end, model, pass, retry class, and elapsed milliseconds; then canary the existing slot-health/fallback controls for retryable failures. **Estimated saving:** minutes on similarly affected runs; the five seconds of recorded backoff in this example is *not* the main delay. **Risk:** medium; compare review findings before widening.
3. **Critical path on selected runs — make expensive preparation conditional.** `Free disk space` took approximately **174 seconds** in run 36823417327 and **182 seconds** in 36824420116; memory start/candidate/completion writes also took roughly **44–57 seconds apiece** in 36823417327. Log free space, bytes removed, operation time, and memory push-attempt time. Only skip cleanup when a measured free-space threshold is satisfied, retaining the existing low-space path. **Estimated saving:** up to the observed **1–3 minutes of cleanup** on qualifying runs; memory-write savings require measured retry causes. **Risk:** medium for conditional cleanup, low for logging.
4. **Do not trade API calls for an apparent polling win.** In run 36824065255, `Resolve PR for head branch` waited **300 seconds**, checking every 60 seconds before proceeding without an open PR. Keep the safety grace unless a trusted PR identifier is already available; log lookup count and wait reason and reuse that identifier for an early decision. **Estimated saving:** up to five minutes when an authoritative identifier arrives early; **zero** on the observed no-PR path without changing safety semantics. **Risk:** low for logging, medium for routing.

Semble queries in the failing reviews took hundreds of milliseconds each—for example, run 36826598683 logged **330 ms** for `overflow` and **324 ms** for `conflict-resolver-context`. Optimizing those first would be a micro-optimization beside the failed resolver and reviewer steps.

## Cost Optimizations

1. **Fix measurement before tuning models.** The **467,078,316 reported OpenRouter tokens**, **212 calls**, **396,669,597 cache-read tokens**, and **7,631,285 cache-write tokens** include aggregate-job/step-log duplication. In the ten runs with usage, distinct aggregate-job lines contain **260,969,341 total tokens across 118 calls**. Have `scripts/cost_audit.py` choose one representation per job or deduplicate usage events by stable call identity; log which representation was selected. **Estimated saving:** no actual tokens—this removes approximately **206.11M duplicated reported tokens** and prevents misallocation of optimization work. **Quality risk:** none.
2. **Prioritize failed and retried expensive work, not blanket model downgrades.** The ten resolver failures have **zero captured OpenRouter usage** despite model activity in their logs, so their billable cost is unknown. Run 36823417327 separately shows reviewer-slot retries within a successful run. Emit usage on terminal failure and per-slot retry outcomes, then reduce demonstrably avoidable repeats. **Estimated saving:** unquantifiable tokens until failure usage is captured; potential avoided work is bounded by the affected attempts. **Quality risk:** low for logging; preserve quorum when changing retries.
3. **Measure model and reasoning trade-offs on eligible changes only.** The ten token-bearing runs used **260.97M distinct logged tokens**; `google/gemini-3.8-flash` accounts for **168.33M** of those logged total-token fields. Sampled review configuration uses `REVIEWER_REASONING_EFFORT=xhigh`, two passes, and disabled risk-tier selection; run 36823417327 reviewed workflow-source material. Record tokens, latency, findings, and disposition by **model × pass × file-risk tier**. Canary fewer passes or lower reasoning only for eligible non-protected changes; retain full review for `scripts/` and `.github/workflows/`. The existing deterministic docs-only skip succeeded for PR #5923 in run 36827180545. **Estimated saving:** one measured pass’s usage per *eligible* review; no defensible window-wide token or dollar estimate yet. **Quality risk:** high if applied to protected code, hence the narrow canary.
4. **Treat MCP bytes as context cost, not proven savings.** The collector reports **50 Semble queries / 486,078 logged bytes**; unique aggregate-job events show **30 / 285,378 bytes**, including ten `reviewer-context`, ten `overflow`, and ten `conflict-resolver-context` queries. Runs 36819986387 and 36826598683 returned resolver context but still failed scope validation. Log returned versus *forwarded-to-model* bytes and downstream prompt-token deltas before trimming chunks: Semble may avoid broader file expansion, but this window has no counterfactual proving it. **Estimated saving:** unknown; avoid reducing resolver evidence preemptively. **Quality risk:** medium if relevant conflict context is lost. Serena is disabled in sampled review runs, with **no queries or response bytes**; it cannot presently be credited with replacing tool/model work, nor blamed for noisy responses.

No workflow rerun attempts were recorded (`run_attempt=1` in the inspected failures); **within-run** reviewer and memory retries are the demonstrated avoidable-repeat candidates. Dollar estimates require model prices and deduplicated, failure-inclusive usage.

## Reliability Improvements

1. **Resolver invariant failures — 10 review runs.** Examples 36822260792 and 36824444267 end with `ValueError` followed by refusal to retry or commit. **Root-cause category:** unknown scope-invariant violation, not demonstrated model refusal; the current catch block deliberately suppresses the precise reason. **Fix:** add a fixed, non-sensitive failure-reason enum and action/attempt fields, investigate its distribution, then test the smallest invariant-specific correction. **Expected impact:** diagnostic logging reduces time to root cause; an actual correction could remove up to ten observed failures, but that is unproven. **Rollback/fail-open:** roll back the new diagnosis if noisy; **do not** fail open the commit guard.
2. **CI regressions — two distinct failures.** Run 36819953817 failed `static-checks / YAML lint` on comment spacing in `.github/workflows/ci.yml`; run 36823790103 failed `tests-hooks-and-orchestrator / Inline edit guard hook tests` with **11 failed, 164 passed**, including ten `env -S` denial cases and template parity. **Root-cause category:** source/test-policy regressions, not Semble outages. **Fix:** run the exact lint and hook-parity tests before dispatching heavyweight CI; correct the `env -S` parsing and synchronize its template, retaining denial tests. Emit a compact test-family/failure-count summary. **Expected impact:** addresses these two observed CI failure modes and reduces late discovery; not a measured future failure-rate reduction. **Rollback:** revert code changes, never disable the guard tests.
3. **API limit at trust gate — one review run.** Run 36823474603 received an API rate-limit response in `Resolve trusted review support commit`. **Root-cause category:** shared GitHub quota. **Fix:** put bounded, jittered retry and response-class logging around the existing trusted read, reusing cached support identity where already verified; expose attempts and retry delay without credentials or request IDs. **Expected impact:** may avoid transient gate reruns; cannot overcome a sustained quota exhaustion. **Rollback/fail-open:** retain the current fail-closed trust decision after the retry budget.
4. **Successful outcomes can conceal follow-up gaps.** Run 36827208911 succeeded but warned that standalone validation could not be dispatched for merged PR #5923. **Fix:** emit `VALIDATION_DISPATCH result=unavailable reason=<enum> pr=<n>` and retain an observable pending follow-up until existing checks establish coverage. **Expected impact:** fewer silently untracked validation gaps, not a faster merge. **Rollback:** keep the current warning if follow-up tracking misbehaves.

Observed `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts are **zero** in collected telemetry: no demonstrated policy/rubric pressure or threshold breach, **not** proof of absence across unlogged runs. Semble’s **12 collector-counted fallbacks** are controlled `target=overflow`, `context=contract-test` missing-binary cases in CI runs 36819953817 and 36823790103 (**eight distinct events**); runtime fallbacks are **zero**. Preserve that healthy test fail-open behavior and label it separately from runtime availability. Serena has **zero probe, query, and fallback events** while disabled; absence of probe failures is not a successful availability test.

## AI Memory Health

Across **19 distinct `retrieve` events** in deep-dive review job logs, **19/19 selected records**: a **100% observed hit rate**, averaging **1,393 estimated tokens against a 1,400-token budget** (about **99.5%** utilized). All 19 used `keyword_method=llm`; none used `plain` or `none`. For example, failed run 36819986387 selected 26 records at 1,399 tokens, while successful run 36820040724 selected 24 at 1,382. No observed retrieve selected zero records, set `enabled=false`, or reported `fail_open=true`.

The same selected-log set contains **40 `record-run-event`**, **10 `record-candidate`**, and one `finalize-task` event. That `finalize-task` in issue-status run 36827312914 reports `fail_open=true` with `reason=no_linked_issues`; it is a benign no-work path, not evidence of a memory outage. Run 36823417327 needed **three push attempts** for a completion event. No `promote`, `compact`, or processed-command event was observed in these logs; their fleet-wide rates are unknown. Add per-operation elapsed time, retry class, and a non-sensitive selected-record-set fingerprint. This will distinguish useful reuse from repeated near-full-budget context; **expected impact** is a basis for safe token trimming and diagnosing the observed minute-scale write steps, with no immediate quality change.

## GH API Call Audit

**Actual per-endpoint call counts and remaining quota were not collected.** The strongest direct API evidence is the rate-limit failure in run 36823474603; recommendations below distinguish code-path counts from measured requests.

| Workflow/job evidence | Call pattern and smallest safe change | Estimated calls/risk |
|---|---|---|
| `review_autofix_sweep.yml`; run **36827159870**: 55 candidates, 38 dispatched, 16 skipped as active, 93 seconds | The workflow already snapshots active runs once rather than twice per PR; **keep that cache**. Log snapshot pages/calls, dispatch calls, and failures by endpoint class in the existing wrapper. | At least **38 dispatch operations** are supported by the run summary; total API calls are unknown. Better counters locate quota pressure without adding requests. |
| `scripts/claude_pr_sweep.py`; run **36826677118**: 14 repos, 62 candidates, three due/queued, 105 seconds | Its documented contract performs a PR read **per candidate** after listing PRs. Reuse fields already present in the list snapshot for preliminary decisions, then perform a fresh authoritative check before each of the three due writes. Log reused versus refetched PRs and snapshot age. | **Conditional estimate:** up to roughly **59 fewer PR reads** for this 62-candidate shape if all preliminary reads can be replaced; preserve freshness checks and per-repo fail-open behavior. |
| `internal-review.yml`; run **36824065255**: five logged 60-second waits before a 300-second no-PR decision | Retain the grace period, but record `lookup_calls`, `lookup_failures`, and whether an existing trusted PR identifier avoided polling. Do not shorten the poll interval: that would increase API use. | Up to the avoided lookups when a PR identifier is available; **no demonstrated reduction** for this no-PR run. |
| `review_autofix.yml`; run **36823474603**, trusted support resolution | Reuse an already verified support result if present; otherwise bounded retry on the existing read and log status class, elapsed time, and retry count. | Normal-path call count unchanged; reduced transient-limit rerun risk, with bounded extra calls on failure. |

These changes follow **`CLAUDE.md` §15**: check existing reads first, batch eligible per-item lookups, use cycle-local caches, and fail open on a cache miss to the smallest safe legacy read. Its GraphQL batching preference must be applied only where the execution environment permits it; the repo also documents a `gh` proxy that rejects unsupported GraphQL operations. Instrument existing calls rather than adding a `/rate_limit` probe.

## Prompt Cache & Memory System

Reported review telemetry has **85.4631% `cache_hit_rate`**, with **396.67M cache-read** and **7.63M cache-write tokens**; deduplicated job-log usage yields **85.7307%**, **222.33M read**, and **4.23M write** across the ten usage-bearing runs. The spread matters: run 36820040724 shows **76.51%**, versus **91.24%** in 36824420116. That difference alone does **not** establish unstable prompt prefixes or dollar savings.

Log a stable prefix fingerprint, the byte position where PR-specific material begins, model/pass, cache support status, creation/read tokens, and any cache retry—without logging prompt contents. Compare low-hit runs before moving dynamic metadata after stable instructions or deduplicating repeated context. **Expected impact:** measurable cache-token and possibly latency improvement if fragmentation is confirmed; otherwise no prompt change. The 19 memory retrieves’ near-full 1,400-token budget warrants logging forwarded tokens and record-set reuse before trialing a smaller cap. Zero captured `CONTEXT_BUDGET_WARN` events provide no basis to declare prompt-size risk solved, given limited coverage.

## Orchestrator Health

The **709 skipped runs** are mostly short phase triggers, not 709 demonstrated stalls: `clarify`, `plan`, and `orchestrate_clarify_respond` each have **171 skipped**, and `implement` has **169**. Review gate skips can be intentional—for example, run 36827289311 waited for a Claude-fixer session for PR #4590. The sweep in run 36827159870 skipped **16 active** reviews while dispatching 38; preserve that duplicate-dispatch guard.

The four `orchestrate_poll` runs succeeded but took **p50 500.5 seconds / p95 1,034 seconds**. `workflow_failure_heal` had **26 skipped** runs; three `claude_twin_sync` runs ended `action_required` without diagnostic logs in this set. Clarification loops, wave progression, deferrals, conflict-heal retries, and terminal-state aging therefore **cannot be reconstructed**. Emit one bounded state-transition record per issue/PR with phase, prior/new state, skip/defer reason, attempt, wait duration, and terminal outcome; track age in state and repeated identical transitions. This should expose stuck work without changing orchestration behavior.

## Pipeline Flow Bottlenecks

| Flow segment | Observed bottleneck | Next diagnostic/action |
|---|---|---|
| Clarify → plan → implement | Most triggers skipped; `clarify` has seven successes, `plan` one success, and `implement` none among 169 runs. These are **not** a measured cohort funnel. | Correlate phase transitions by task and record skip reasons before changing dispatch frequency. |
| Review/autofix compute and retry | Review p95 **1,532 seconds**; run 36823417327 spent about **2,266 seconds** in reviewer models with retryable slot failures. | Add per-slot latency/usage and canary bounded fallback; preserve protected-code review. |
| Validation and CI | CI p50 **549 seconds**; runs 36819953817 and 36823790103 failed cheap lint/hook checks while their full runs lasted **785/538 seconds**. | Run those checks before costly downstream work where dependencies permit; log failed-test family and whether parallel work continued. |
| Orchestrator/API wait | Poller p95 approximately **1,034 seconds**; run 36824065255 spent **300 seconds** awaiting a PR, and 36823474603 hit an API limit. | Separate deliberate safety waits from failed lookups and API retry time; retain grace and trust checks. |
| Merge/conflict overhead | Ten review runs ended in resolver scope failure after **475–819 seconds**; run 36827208911 reported a post-merge validation-dispatch warning. | Classify scope invariant failures and track validation follow-up to closure. |

The available `run_started_at` values do not provide an independent job-queue measurement; **queueing versus execution cannot be apportioned reliably**. Add job queued/start timestamps alongside the proposed compute, retry, and wait counters.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** long reviewer-model steps (run 36823417327: approximately 2,266 seconds), poller/wait time (run 36823045584: 1,127 seconds), and selected disk/memory preparation. **Top failure modes:** ten resolver scope failures, one trusted-gate API limit, and two CI regressions. **Highest observed cost driver:** review-model usage in ten slow successful runs; reported totals are duplicated, and failed-run model usage is missing.

**Prioritized actions:** (1) add safe resolver failure-reason logging and fix the identified invariant without relaxing the guard; (2) deduplicate usage collection and log model-slot time/usage on failures; (3) instrument existing GH API reads and reuse sweep candidate metadata with fresh checks before writes. These first improve diagnosis and measurement, then target the largest observed failure and cost paths.

## Metrics Appendix

**Window and outcomes** — `shubhodeep1/coding-workflows`, October 1, 2026, approximately 05:26–06:56 UTC. Rates below use **all runs**, including skips, unless stated otherwise.

| Family | Runs | Success | Failure | Cancelled / other | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|
| All | 1,000 | 271 (**27.1%**) | 13 (**1.3%**) | 4 / 712 | 1 / 208 s |
| Review/autofix | 191 | 176 | 11 (**5.76%**) | 4 / 0 | 11 / 1,532 s |
| CI | 6 | 4 | 2 (**33.3%**) | 0 / 0 | 549 / 744.75 s |
| Orchestrate poll | 4 | 4 | 0 | 0 / 0 | 500.5 / 1,034 s |
| Copilot PR reviewer | 36 | 36 | 0 | 0 / 0 | 191.5 / 441.5 s |

“All” other comprises **709 skipped and three `action_required`**; its one-second median is not the latency of an active pipeline.

| Usage/coverage measure | Collector or assembled context | Distinct aggregate-job log check / limitation |
|---|---:|---:|
| OpenRouter calls; total tokens | 212; 467,078,316 | **118; 260,969,341**, across ten usage-bearing slow reviews |
| Prompt; completion tokens | 59,840,588; 2,939,751 | 32,776,663; 1,640,372 |
| Cache write; read tokens | 7,631,285; 396,669,597 | 4,228,108; 222,325,774 |
| `cache_hit_rate` | **85.4631%** | **85.7307%** after job/step deduplication |
| `wall_clock_p50_ms`; `wall_clock_p99_ms` | **10,000; 2,010,200**, assembled 123-run sample | Raw 30-log sample: **604,500; 2,483,340**; different sample, not a regression |
| `break_glass_count`; `context_budget_warn_count` | **0; 0** | No observed events in collected logs |

**MCP telemetry.** “Collector” is the supplied aggregate; “distinct” counts an event once from aggregate-job logs (and, for CI fallbacks, the sole available job log). `bytes` are logged query-output bytes, **not** measured prompt-token savings.

| Server / target | Collector query events / bytes | Distinct query events / bytes | Collector / distinct fallbacks | Probe ok / failed / skipped |
|---|---:|---:|---:|---:|
| Semble `reviewer-context` | 18 / 245,758 | 10 / 135,178 | 0 / 0 | N/A — no Semble probe events |
| Semble `overflow` | 16 / 89,312 | 10 / 55,820 | **12 / 8**, contract tests only | N/A |
| Semble `conflict-resolver-context` | 16 / 151,008 | 10 / 94,380 | 0 / 0 | N/A |
| **Semble total** | **50 / 486,078** | **30 / 285,378** | **12 / 8**; runtime **0** | N/A |
| Serena **target unreported; disabled** | **0 / 0 response bytes** | 0 / 0 | **0 / 0** | **0 / 0 / 0** — not probed |

Serena tool calls and per-tool response-byte breakdown: **zero; no tools observed**. **Other MCP servers observed:** none with a validated `<NAME>_QUERY`, `<NAME>_FALLBACK`, or `<NAME>_PROBE` event; `ORCH_QUERY` in an echoed issue-status script is a shell variable, not an MCP event.

**GH API summary:** observed quota failures: **one** trusted-gate run (36823474603); observed successful sweep dispatches: **38** in run 36827159870; documented per-candidate PR reads: **62 candidates** in run 36826677118. **Actual API calls by endpoint, pagination, retries, and rate-limit remaining: not collected.** Collector coverage is **30 downloaded full-log runs** versus **123 context rows with log telemetry**; **962 runs were not selected for log download**, and 919 of those have no `log_summary`. Collect unique usage-event IDs, endpoint-class counters, queue timestamps, and terminal-failure usage before projecting fleet-wide savings.
