## Executive Summary

- **Planning has a confirmed compatibility failure.** In five archived `plan / plan` runs—including 37385496261 and 37394219899—the staged `codex_stall_guard.sh` rejected `--engine` on all three attempts. A preflight against the *staged* script would prevent the identical retries and fail before further preparation. The five failed runs consumed **2,349 seconds combined**; not all of that time is recoverable. **Confidence: high.**
- **CI regressions are the largest observed failure cluster.** CI failed in **10/32 runs**; six failed at `Orchestrate poll process unit tests`. Runs 37390665575 and 37392114833 repeat judge-safety assertion failures. Fix the implementation or fixtures without relaxing the guards; this addresses up to six observed failing runs, subject to other tests passing. **Confidence: high.**
- **Review latency includes distinct wait problems.** Run 37387443320 waited **679 seconds for a runner**, then logged a **1,423-second gap** between agent eligibility evaluation and job start whose cause is not established. Its check-run collection also took **301 seconds** waiting for an in-progress check. Log these intervals separately before changing concurrency or polling. **Confidence: high for timings; low for the handoff cause.**
- **A late remote error wasted an expensive review.** Run 37387112300 failed after **2,870 seconds** at `Push all pending commits`: the remote rejected the push with an Internal Server Error, and the workflow did not retry it. It had logged **36.27 million usage tokens**. A bounded, ancestry-checked retry for an explicit transient server error could avoid a full rerun without weakening branch protection. **Confidence: high for the failure; medium for prospective savings.**
- **Telemetry is useful but incomplete.** The assembled window reports **199.36 million usage tokens**, **77.37% cache hit rate**, and **zero observed runtime Semble fallbacks**. It has no GH API request counts, dollar prices, or production Serena probes. Add endpoint-level and handoff diagnostics before claiming API savings or changing models. **Confidence: high.**

## Speed Optimizations

| Rank | Evidence and root cause | Exact change; estimated saving | Risk |
|---|---|---|---|
| **1 — critical path: runner and handoff wait** | Review 37387443320: `review / gate` waited **679s** after “Waiting for a runner”; CI 37386924253 has a poll job waiting **749s**. Separately, review 37387443320 has an unexplained **1,423s gate-to-agent eligibility/start gap**—do not count it as confirmed runner queue. | Log `job_eligible_at`, `job_defined_at`, `runner_started_at`, and supersession reason. Audit existing same-PR concurrency so *safely superseded* work can release capacity before model execution, while preserving runs with unpushed edits. **Potential:** up to the observed ~11-minute runner wait on similarly contended runs; handoff savings are unquantifiable until diagnosed. | **Medium:** unsafe cancellation could lose work. |
| **2 — critical path: pending-check wait** | Review 37387443320 spent **301s** in `Collect PR check-run failures`, logging seven sleeps totaling **296s** for one pending check; this was waiting, not demonstrated API slowness. | Emit `CHECK_WAIT` with fetch count, accumulated sleep, pending count, final status, and deadline result. Test whether any reviewer work is independent of final check context; overlap only that work, then refresh the check snapshot before an edit decision. **Potential:** at most **301s** in this run if independence is established. Never omit the final check. | **Medium:** stale CI context could affect fixes. |
| **3 — failed-run waste: planning preflight** | Five archived planning failures retried the same unsupported `--engine` argument three times, with **10s + 20s** backoff per run. The current checkout’s guard accepts the option; the run’s staged copy did not. | Before memory retrieval or model setup, validate the staged guard’s options and log its version/hash and selected engine. Classify option/usage exit 2 as non-retryable. **Saving on this failure mode:** **30s retry sleep per run**, plus later doomed preparation if the check runs early; no claimed improvement to successful planning time. | **Low.** |
| **4 — feedback, not a shortcut around CI** | CI 37392114833’s poll-test shards logged roughly **374–411s** each; several judge tests failed. | Run the relevant judge-safety contract tests as an early, separate signal while retaining full required CI. **Impact:** earlier failure identification; mandatory full-CI wall-time saving is **not established**. | **Low** if required checks remain unchanged. |

Semble calls are a micro-optimization here: observed query timings are around **0.4–0.6s** per marker, far below the waits and model steps above.

## Cost Optimizations

1. **Avoid expensive late-stage reruns first.** Review 37387112300 logged **24 OpenRouter usage calls** and **36.27M total usage tokens** before its remote-500 push failure. Add the safe push retry described below and log whether a subsequent run actually repeated the review. **Potential saving:** up to one repeated review *if* this failure causes a full rerun; neither rerun occurrence nor dollar cost is established. **Quality risk:** none from preserving the existing review and push ancestry checks.

2. **Measure phase value before reducing model effort.** The assembled review telemetry is **208 calls**, **35.73M prompt**, **1.53M completion**, **9.04M cache-write**, and **153.06M cache-read tokens**. Run 37387112300 alone used 24 calls; archived planning logs configure high reasoning effort, while summarized run 37395114759 names a lighter model for materiality/summarization. Add per-phase `model`, `reasoning_effort`, usage, elapsed time, and accepted-outcome counters; trial a cheaper setting only for a demonstrated low-risk phase. **Savings:** unknown without per-model prices and phase attribution; **quality risk:** high for judge, security, and editing decisions, so do not downgrade those blindly.

3. **Reduce repeated uncached context without hiding evidence.** Overall `cache_hit_rate` is **77.37%**, but slow reviews 37381317564 and 37387443320 show **56.70%** and **62.10%** respectively. Reuse a stable instruction prefix and cycle-local issue/PR facts; place run-specific noise after that prefix and select only relevant memory/context. As a *scenario, not a forecast*, a 10% reduction in the measured **35.73M prompt-token** component equals about **3.57M tokens** in this window. **Quality risk:** medium unless required evidence and rubric text remain intact.

4. **Evaluate context tools on outcomes, not byte counts alone.** The assembled data logs **27 `SEMBLE_QUERY` markers / 317,088 `bytes=`**, with no runtime fallback. Archived `review / codex-agent / Run reviewer models` queries commonly log ~13KB for `target=reviewer-context`; `overflow` and `conflict-resolver-context` also occur. This is targeted context, but no no-Semble prompt baseline proves reduced expansion. Log query purpose, injected bytes, subsequent prompt delta, and useful-result status; cap only demonstrated low-value output. Serena has **zero queries, tool calls, response bytes, and probes**; summarized review 37395114759 says it was disabled. Do not claim Serena replaced downstream work or recommend a rollout on this evidence. **Savings and quality effect:** unknown pending comparison.

5. **Record cancellation cost.** Seven review runs were cancelled; 37386869805 lasted **2,387s**, but has no usage record here. Record cancellation phase and usage accrued before considering an earlier safe supersession gate. **Potential saving:** unquantifiable; preserve in-progress edits.

## Reliability Improvements

| Rank | Failure evidence; category | Smallest safe fix and diagnostic addition | Expected impact; rollback/fail-open |
|---|---|---|---|
| **1** | Five inspected `plan / plan` failures, including 37394219899: `codex_stall_guard.sh: unknown option: --engine`, followed by `no_transcript` and three identical attempts. **Staged-interface mismatch**, not proven model-capacity failure. | Check the staged guard and wrapper together before invocation; emit `PLANNING_GUARD_PRECHECK` with run, staged-script hash/version, engine, supported-option result, and exit category. Do not retry usage exit 2 or switch models for it. | Prevents this confirmed five-run failure mode. Roll back the preflight if it misclassifies an option; retain the existing invocation contract. |
| **2** | CI **10/32 failures**: six poll-test steps, including repeated `test_review_blocked_merged_fix_target_rejects_unrelated_followup` failures reporting `head_repo_mismatch` in 37390665575 and 37392114833. Security tests in 37384706088/37386834838 rejected `python3 -c`; 37386924253’s push-parser tests expected a guarded destination but got none. **Contract/security regressions**; some `##[error]` lines elsewhere are intentional negative-test output. | Preserve the guards. Emit a concise `CI_TEST_FAILURE` containing test ID, assertion category, expected/actual *safe identifiers*, and fixture version; fix code or fixture based on that distinction. | Could clear the observed CI failures once their assertions pass; do not fail-open or remove security tests. |
| **3** | Review 37387112300: pre-push check reported a fresh base; remote then returned an Internal Server Error and the workflow’s non-fast-forward-only retry classifier stopped. **Transient remote-service error**, distinct from merge conflict. | For an explicit server-error rejection, re-fetch the branch, check whether the commit already landed and whether an external tip advanced, then make at most a few backoff retries **without force-push**. Emit `REVIEW_PUSH_ATTEMPT` with attempt, rejection class, ancestry result, and outcome—never credentials or raw remote output. | Could prevent recurrence of this one observed late failure. Keep hard failure for ambiguous results, auth errors, or unsafe ancestry; feature-flag the new retry for rollback. |
| **4** | Assembled telemetry: **60 `SEMBLE_FALLBACK`**, all **contract-test** fallbacks, `target=overflow`; **0 runtime** fallbacks. No Serena query or probe is recorded. | Keep the tested fail-open behavior and separately log runtime availability, probe result, target, and fallback reason. Do **not** treat test fixtures’ deliberately missing Semble binary as a broken production rollout or zero Serena probes as proof of availability. | Better detection of a masked rollout; no failure-rate reduction can yet be estimated. |

`break_glass_count=0` and `context_budget_warn_count=0` in the covered logs: there is **no observed policy/rubric-pressure or model-window warning** to quantify. CI tests of prompt character/byte caps are test evidence, not production `CONTEXT_BUDGET_WARN` events. Continue emitting both markers and report their coverage denominator.

## AI Memory Health

- Across archived logs, **21 distinct retrieve payloads** (deduplicated by run and JSON payload) all selected records: **21/21 positive**, none with zero records. Mean `estimated_tokens` was **1,172** against mean budget **1,305**; keyword methods were **`llm` 21, `plain` 0, `none` 0**. Planning averaged **931/1,200** tokens; reviewer retrieval averaged **1,390/1,400**, leaving little *memory-allocation* headroom. Run 37387112300’s reviewer retrieve selected 24 records at 1,379/1,400. Preserve relevant selections but log which categories fill the budget; this should reduce low-value context without asserting model-window pressure.
- No archived retrieve reports `enabled:false`, `fail_open:true`, or a zero-record miss. A **separate summarized** `issue_pr_status` run, 37395114695, reports `finalize-task` with `fail_open:true`, `reason=no_linked_issues`; that is not evidence of a failed retrieval. Summarized run 37395836573 reports a successful merged-task finalization. Keep operation-specific `reason` and `ok` fields so these outcomes remain distinguishable.
- **Sixteen distinct archived memory events** report more than one push attempt while ultimately reporting success. In review 37387443320, `Record review run start in memory` took approximately **115s** and reported **10 push attempts**. Add `push_duration_ms`, retry reason category, and contention outcome to `AI_MEMORY_TELEMETRY`; investigate that run before changing fail-open behavior. No `promote` or `compact` event was found in the archived subset—verify emission during those operations rather than inferring they never occur.

## GH API Call Audit

**Request counts, status distributions, rate-limit remaining, and retry totals were not collected.** No selected runtime log establishes rate-limit exhaustion. Instrument existing wrappers locally with `GH_API_CALL` fields for workflow/job/step, **endpoint template** (not full URL or token), method, pages, attempts, HTTP class, elapsed milliseconds, and remaining limit; aggregate without adding a diagnostic API request.

| Observed path | What can be said now | Safe call-count action and rate-limit effect |
|---|---|---|
| Review 37387443320, `Collect PR check-run failures` | Seven logged sleeps imply **at least eight fetch invocations** if the loop reached another snapshot; actual HTTP requests can exceed invocations through pagination/retries. `scripts/collect_pr_check_runs_context.py` already uses `gh api --paginate --slurp`, 100 per page, and backs off. | Keep polling while status changes matter; log fetch/page/retry counts and reuse its written snapshot downstream. **Measured redundant calls: none established; current reduction estimate: unknown.** Avoid an unmeasured tighter loop that increases shared-limit risk. |
| Non-orchestrator readiness run 37396067386 | Its **11s** success reported readiness not applicable. The script uses the event head ref and must still **POST a neutral required-check status**; it does not need a tracking-issue lookup on this path. | Retain the status POST. Skipping this apparently no-op workflow would break its required-check contract; **safe demonstrated call saving: zero**. |
| Production `orchestrate_poll` | **9/9 runs succeeded**, but none has collected log telemetry here; per-item call counts cannot be audited. | Add batch size, cycle-cache hits/misses, fallback calls, and endpoint-template counters. Follow this repo’s `CLAUDE.md §15`: reuse cycle-local facts and aliased GraphQL batches where supported, fail open to the smallest safe legacy read. If a measured \(N\)-item loop is found, batching to size \(B\) saves \(N-\lceil N/B\rceil\) calls; **\(N\) and current savings are unknown**. |

## Prompt Cache & Memory System

The assembled review sample has **153.06M cache-read** and **9.04M cache-write tokens**, with a **77.37%** aggregate hit rate under the collector’s read/(prompt + write + read) definition. Run 37387112300 reached **85.39%** and still failed at push: cache performance does not cure late reliability failures. Runs 37381317564 (**56.70%**) and 37387443320 (**62.10%**) warrant prefix inspection, but their misses do not by themselves prove fragmentation.

Emit a privacy-safe stable-prefix hash, static/dynamic prefix byte counts, per-phase cache read/write/uncached tokens, and cache-disable/fail-open reason. Then move timestamps, run IDs, and other dynamic material *after* reusable instructions where semantics permit; retain the full safety rubric. Compare cache hit rate and accepted-review outcomes before rollout. **Token/latency gain is unmeasured** until prefix variance is captured; bounded prompt-reduction scenario is in Cost Optimizations. The memory retrieval results above are positive, but near-full reviewer budgets justify measuring selected-record usefulness rather than indiscriminately increasing memory injection.

## Orchestrator Health

- **Clarify-to-plan progressed, then failed safely:** plan 37394219899 claimed an `answer` command, later recorded `processed-command-complete` with `status=failed` and a `phase_failed` event after the guard error. This is evidence of failure bookkeeping, not a demonstrated stuck claim. Add `command_claim_age_ms` and terminal transition reason so abandoned claims can be distinguished; impact is faster diagnosis, with no state-machine change.
- **Many runs are intentionally gated, not failed:** **646/1,000** conclusions are `skipped`; `clarify` has 151/156 other/skipped and `implement` 149/152. Recent run 37396038221 is a one-second skipped clarify. Record skip reason from workflow event/job metadata at collection time—there is no running step in which to print a log. This would distinguish healthy filtering from stalled progression without launching extra jobs.
- **Wave, deferral, and judge-cycle health remains unmeasured.** All nine `orchestrate_poll` runs succeeded, but have zero parsed logs in this selection. Review 37390688134 did spend **81s** in a conflict-resolver/validate/commit step; that alone does not establish recurring conflict-heal retries. Add `ORCHESTRATOR_TRANSITION` with cycle/wave, prior/next state, deferral or judge reason, retry count, and time in state; monitor age of pending waves and terminal-state transitions. Preserve existing fail-closed merge and judge checks.

## Pipeline Flow Bottlenecks

| Stage | Observed bottleneck type | End-to-end priority |
|---|---|---|
| Clarify → plan | Most clarify runs skip quickly; **8/154 plan runs failed**, with five archived failures proving the staged-guard mismatch. **Retry/preparation overhead**, not established model latency. | **First:** preflight staged interfaces and stop non-retryable retries. |
| Implement → review/autofix | Implement is mostly gated; active review has **p50 564s, p95 3,163.6s**. Review 37387443320 combines **679s runner queue**, unexplained handoff, **301s check wait**, and **794s reviewer-model step**. | **Next:** separate queue/handoff/check/model timing; safely reduce superseded work, then test overlap only for independent tasks. |
| Validate/CI → orchestrate | CI **p50 869.5s, p95 1,441.75s**, with **10/32 failures**; selected poll shards take minutes. Production orchestrator poll **p50 557s** but lacks step evidence. **Compute plus runner contention and failing validations** are visible; wave retries are not. | Fix CI assertions and collect production poll transitions before changing cadence. |
| Merge/push | Review 37390688134’s resolver step took **81s**; review 37387112300 failed after **2,870s** on a remote push 500, not a recorded merge conflict. | Add bounded safe push recovery before optimizing the comparatively small observed resolver step. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** contended review/CI runners, the unclassified review gate-to-agent handoff, pending-check waits, and long review model steps. **Top failure modes:** staged planning-guard incompatibility (five confirmed runs), judge/security CI regressions (10 CI failures overall), and one late remote push rejection. **Highest-cost driver:** review/autofix’s **199.36M logged usage tokens** in the assembled telemetry; dollar cost and cancelled-run usage are unavailable.

**Prioritized actions:** (1) preflight the staged planning guard and classify usage errors as non-retryable; (2) fix the named CI assertions without weakening safeguards; (3) add ancestry-checked transient push recovery plus queue, API, and phase-timing diagnostics. These address demonstrated failure waste before riskier model or concurrency changes.

## Metrics Appendix

**Scope and coverage.** The archive’s 1,000 run rows span **October 5, 2026 22:15:13 UTC–October 6, 2026 00:50:15 UTC**. “Other” means skipped here; it is **not** failure. The assembled context reports telemetry for **125 runs**, whereas the archive’s `summary.json` reports **28** parsed runs; archive downloads are 28 successful, 12 empty, and 960 not selected. The assembled totals below include its wider supplied coverage; **do not add** archive totals to them.

| Workflow family | Runs | Success | Failure | Cancelled | Skipped | Failure/all runs | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| **All** | **1,000** | **328** | **19** | **7** | **646** | **1.9%** | **2s / 1,430s** |
| CI | 32 | 22 | 10 | 0 | 0 | 31.25% | 869.5s / 1,441.75s |
| Plan | 154 | 3 | 8 | 0 | 143 | 5.19% | 1s / 484.15s |
| Review/autofix | 187 | 179 | 1 | 7 | 0 | 0.53% | 564s / 3,163.6s |
| Clarify | 156 | 5 | 0 | 0 | 151 | 0% | 1s / 11s |
| Implement | 152 | 3 | 0 | 0 | 149 | 0% | 1s / 11s |
| Orchestrate poll | 9 | 9 | 0 | 0 | 0 | 0% | 557s / 685.2s |

| Assembled cost/review telemetry | Value | Coverage qualification |
|---|---:|---|
| OpenRouter calls; usage available/unavailable | 208; 208/0 | Logged calls, not all 1,000 runs |
| Prompt / completion / total usage tokens | 35,731,785 / 1,528,024 / 199,361,289 | Total includes cache-token accounting; components should not be re-added |
| Cache-write / cache-read tokens; `cache_hit_rate` | 9,044,590 / 153,059,453; **77.37%** | Aggregate over covered usage |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 7,000 / 3,680,700 | **123** sampled run clocks; many short skipped runs |
| Review-only `wall_clock_p50_ms` / `wall_clock_p99_ms` | 3,208,000 / 3,963,720 | **17** sampled review clocks |
| `break_glass_count` / `context_budget_warn_count` | **0 / 0** | Covered logs only |
| Codex calls / reported tokens | 12 / 6 | Not a substitute for missing per-model billing |
| GH API requests / retries / rate-limit events | **Not collected / not collected / not collected** | No zero-rate-limit claim |
| Archived job runner-wait proxy | p50 **18.5s**, p95 approximately **663s** | **120 selected job system logs**, not population-wide |

| MCP server/target | Assembled markers and logged bytes | Archived target detail and availability |
|---|---|---|
| Semble, all query targets | **27 `SEMBLE_QUERY`; 317,088 `bytes=`** (~11,744 bytes/marker) | Archived subset: reviewer-context **16 / 218,576 bytes**; overflow **5 / 40,326**; conflict-resolver-context **2 / 16,030**. Combined/per-step logs can repeat markers, so these are **logged occurrences, not proven distinct service requests**. |
| Semble fallback, `overflow` | **60 total; 60 `context=contract-test`; 0 runtime** | Deliberately missing-binary tests, not a production failure rate. |
| Serena, all targets/tools | **0 queries, 0 fallbacks, 0 response bytes, 0 tool calls, 0 query ms** | No per-tool breakdown exists because no tool was observed; availability untested. |

| MCP availability target observed in query/fallback evidence | `probe_ok` | `probe_failed` | `probe_skipped` |
|---|---:|---:|---:|
| Semble `reviewer-context` | 0 | 0 | 0 |
| Semble `overflow` | 0 | 0 | 0 |
| Semble `conflict-resolver-context` | 0 | 0 | 0 |
| Serena — no target observed | 0 | 0 | 0 |

**Other MCP servers observed:** none with an emitted runtime `<NAME>_QUERY`, `<NAME>_FALLBACK`, or `<NAME>_PROBE` event in the inspected logs. Zero probe rows mean **no probe evidence**, not confirmed availability.
