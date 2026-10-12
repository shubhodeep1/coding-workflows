## Executive Summary

- **Fix telemetry counting first.** The collector reports 56.25M OpenRouter tokens in the deep-dive archive, but whole-job-only recalculation finds 42.90M: **13.35M reported tokens are duplicates**, not additional billed usage. Run 38252166135 alone is counted as 20 calls instead of 10. **Impact:** accurate cost decisions; **confidence: high**.
- **A failed child blocked the release test for 5,292 seconds.** Test & Mark Stable run 38253656098 failed while watching validation-refresh run 38253831133. That child reported 10 red consumer results—which the runner classifies as non-blocking—but also one checkout error following a GraphQL failure. A bounded retry of that read could avoid a full test rerun on this path, potentially **up to 88 minutes**; success is not guaranteed. **Confidence: medium**.
- **Closed PRs still reach failing review steps.** Review runs 38257610792 and 38261607088 marked PRs #7267 and #7266 closed, then failed at “Checkout PR head branch” because the workspace was unavailable. The former also spent 300 seconds waiting for checks after the close warning. **Impact:** prevent two observed failures and avoid up to five minutes of needless wait per affected run; **confidence: high**.
- **Queueing and merge-train work need separate timing.** Readiness run 38271033352 waited about 150 seconds for a runner; CI run 38268305673 waited about 85 seconds. Two scheduled close-cleanup runs spent about 192 and 218 seconds in merge-train release scans. **Impact:** expose which delays are avoidable before changing dispatch behavior; **confidence: medium**.
- **MCP failures do not indicate a broken rollout in this sample.** All 52 reported Semble fallbacks are contract-test events; runtime fallbacks are zero. Serena has no queries or probes, and run 38268306664 reports it disabled. **Impact:** avoid an unnecessary rollback; **confidence: high for observed logs, low for unsampled runs**.

## Speed Optimizations

Ranked by potential end-to-end latency reduction:

| Priority | Evidence and root cause | Exact change and diagnostic logging | Estimated saving; risk |
|---|---|---|---|
| 1 — release critical path | Run 38253656098 took **5,292s** and failed on child 38253831133. The child took **1,288s** and reported `processed=13 green=2 red=10 error=1`; its error was a checkout GraphQL failure. | Retry only transient, read-only checkout/default-branch requests with a small bounded backoff. Log `VALIDATION_STAGE repository=… stage=checkout attempt=… status=… elapsed_ms=…`, then retain the existing error gate if retries fail. Do **not** turn red self-tests green. | Potentially avoids one **5,292s** rerun on this failure path; **low implementation risk**, conditional benefit. |
| 2 — review critical path | Runs 38257610792 and 38261607088 detected closed PRs before an unconditional checkout step. Run 38257610792 subsequently logged a **300s** check-run wait. | Guard checkout, check-wait, and memory retrieval on the already-established `PR_CLOSED` state; retain the current failure for an *open* PR with a missing workspace. Log `REVIEW_PRECHECK pr=… state=… action=skip_closed|continue|fail_missing_workspace elapsed_ms=…`. | Up to **300s of observed wait** on run 38257610792, plus downstream work; **low risk** if unknown PR state never counts as closed. |
| 3 — runner queue, not compute | Run 38271033352 spent about **150s** waiting for `ubuntu-latest`; CI run 38268305673 waited about **85s**. Run-level `run_started_at` does not expose those job waits. | Record job queued-to-runner and execution milliseconds separately using already-fetched job data. Test whether redundant, identical-head events can be coalesced **without changing required-check behavior**. | At most the measured wait for a genuinely redundant job; **medium risk** for dispatch changes, negligible risk for logging. |
| 4 — instrument before optimizing | Cancel-on-close runs 38270862087 and 38268250577 spent **192s** and **218s** in “Release merge-queued PRs”; one examined 29 PRs and released zero. Code already caches per-PR file lists and deliberately scans active-run statuses twice. | Add `MERGE_TRAIN_RELEASE_TIMING` with `listing_ms`, `files_ms`, `blockers_ms`, `comments_ms`, `api_calls_by_category`, and cache hits. Optimize the measured dominant category; preserve the safety double-pass. | **No defensible saving yet**; the observed step is the opportunity ceiling. Logging is low risk. |

Semble’s reported nine bootstraps total 322 seconds and have zero reported unused bootstraps. Even before correcting duplicates, that is a micro-optimization beside multi-thousand-second review runs; retain lazy bootstrap.

## Cost Optimizations

**Dollar savings cannot be estimated:** the input has no comparable prices or verified billed totals. The token figures below are collector-reported unless identified as whole-job-only.

1. **Measure review passes before changing model selection.** Review/autofix accounts for **63,636,796 of 63,683,625 reported OpenRouter tokens**. In run 38252166135, five reviewer models made 10 *distinct* calls: `pass1` used 4,111,263 tokens and `review` used 1,192,309. The configured editor model in its logs is `openai/gpt-6-sol`; available aggregates do not establish per-call reasoning level or quality contribution. Log `model`, `reasoning_effort`, `phase`, `call`, distinct call ID, and accepted findings. Compare a tightly scoped one-pass trial against judge and finding outcomes **before** removing calls. **Opportunity:** the measured `review` calls are 1,192,309 tokens in that run; **quality risk high**, so production savings are presently **zero**.
2. **Bound prompt growth without removing rubric or relevant files.** Review runs 38246098799 and 38252203873 logged prompts of **165,632** and **188,772** tokens against a 200,000-token window; both exceeded the 140,000 warning threshold. Log component token counts and stable-prefix hashes, then trim only demonstrated redundant context while preserving review requirements. **Conditional ceiling to reach the warning threshold:** 25,632 and 48,772 tokens respectively, not a measured achievable saving; **quality risk medium** for content removal, low for byte-equivalent prefix reordering.
3. **Avoid work after a confirmed PR close.** The two failed review runs still retrieved 26 memory records each, estimating **1,391** and **1,400** context tokens; neither started agents. Move retrieval behind the closed-PR guard. **Saving:** two retrievals and 2,791 prepared context tokens in these runs; **billed savings unknown**, quality risk negligible for confirmed-closed PRs.
4. **Prevent avoidable release reruns.** Failed release-test run 38253656098 recorded **46,829** OpenRouter tokens. A successful transient-read retry could avoid that amount on one equivalent rerun; it cannot be counted as saved until the release completes successfully.
5. **Repair cost accounting—not as a cost-cutting claim.** The deep-dive collector counts run 38252166135 twice (**10,607,144 versus 5,303,572** whole-job-only tokens) and run 38258839515 twice (**16,092,770 versus 8,046,385**). `scripts/collect_workflow_logs.py` compares full timestamped lines; parent-job and child-step copies differ by microseconds. Match the same event across ancestor/descendant logs using a producer event ID, or a narrowly bounded timestamp-normalized fallback with regression tests. **Actual token saving: zero; reporting correction: 13.35M tokens in this archive.**

Semble recorded **nine queries / 134,703 bytes / 76 sources**, with **2,502 static-duplicate bytes**; all queries target `reviewer-context`. A 14,948-byte, eight-source query appears in run 38252166135. This is evidence of targeted retrieval, **not proof of reduced model prompt tokens**: log bytes inserted into the prompt and a comparable non-Semble baseline before claiming savings. Serena recorded **zero calls and zero response bytes**; it cannot yet be credited with replacing model or tool work, nor blamed for noisy output.

## Reliability Improvements

1. **Keep validation mechanism errors distinct from consumer failures.** Run 38253831133 had **10 red consumer results** with self-test failures, including compose startup/build and health failures, plus **one checkout error**. Its parent, 38253656098, failed. Retry the transient checkout read, emit a per-stage failure category and final retry disposition, and continue to gate on genuine mechanism errors. **Expected impact:** may remove this one observed release-blocking error; **rollback:** restore the previous attempt limit, never suppress the final error.
2. **Honor confirmed closed-PR state end to end.** Both observed review/autofix failures, 38257610792 and 38261607088, reached “Review workspace is unavailable” after logging closed-PR state. Add the guarded skip and a terminal `REVIEW_DISPOSITION` containing PR state, skip reason, and failed step. **Expected impact:** eliminates this specific two-failure pattern; **fail-open boundary:** ambiguous state must not be treated as closed.
3. **Catch CI contract violations earlier while retaining CI.** CI failed **7/49** runs. Runs 38246118726 and 38268027882 rejected direct additions to `agents.md`; run 38245816108 found an unwired test; 38245692461 failed the disabled-publishing contract; 38246083359 deferred plan approval because its helper was unavailable. Run the same guards in pre-submit validation and emit `CI_FAILURE_SUMMARY job=… test=… category=…` rather than only an exit code. Put new documentation in `agents.d/`, per the repository rule. **Expected impact:** earlier diagnosis, not a guaranteed failure-rate reduction; final CI remains authoritative.
4. **Retry the artifact download, not the lint decision.** CI run 38250387138 failed installing actionlint when `curl` returned **504**. Add a small bounded download retry with status/attempt logging, then verify the pinned artifact before executing; still fail if unavailable. **Expected impact:** addresses one observed transient-install failure; rollback is the prior single attempt.
5. **Preserve unknown outcomes as unknown.** Poll run 38246066105 has **no job or step failure point** because its log archive returned **404**. Log archive-fetch attempts, HTTP status, age and jobs-fallback outcome; do not classify it as an orchestrator-code defect from the missing archive alone.

**Policy and MCP signals:** `BREAK_GLASS` is **0**. The **two** `CONTEXT_BUDGET_WARN` events are review prompt-size risk, not evidence of a policy exception. Semble’s **52 reported fallbacks** are all `target=overflow`, `context=contract-test`; **zero runtime fallbacks** were reported. CI run 38268027882 illustrates duplicate job/step copies: eight reported fallback lines represent four whole-job events. No Serena availability probe or runtime fallback was observed, so the safe mitigation is coverage logging—not rollout rollback.

## AI Memory Health

In **11 unique deep-dive review runs**, all **11 retrieves selected records** (**100% observed hit rate**), selecting 26 records each. Estimated context averaged **1,394/1,400 budget tokens** (99.6%); `keyword_method` was **llm: 11, plain: 0, none: 0**. No observed retrieve selected zero records, had `enabled=false`, or logged `fail_open=true`. The sample also has 22 `record-run-event`, five `record-candidate`, and three `write_lessons_learned` events. Of 27 entries reporting push attempts, **five took two attempts** and none took more; log retry *reason and elapsed time* to detect worsening contention.

Run 38261607088 retrieved memory after PR #7266 had been marked closed. Guard that operation as above. No `finalize-task`, `promote`, `compact`, or processed-command telemetry appears in these selected review logs; verify emission in the workflows that perform those operations rather than treating their absence here as a failure.

## GH API Call Audit

**Call totals, per-endpoint frequencies, and rate-limit-event counts are not supplied.** Review deep dives emit `GH_PAT_BUDGET used_in_job=unknown` (for example, run 38252166135); no measured `GH_API_RETRY` or 429 event was found in the inspected job logs. A **404** occurred fetching run 38246066105’s archive, and validation-refresh run 38253831133 logged a GraphQL checkout error. Neither establishes rate-limit saturation.

| Workflow / step | Supported pattern | Safe action and expected call reduction |
|---|---|---|
| Cancel on PR Close / merge-train release | Runs 38270862087 and 38268250577 spent 192s and 218s scanning; one examined 29 PRs. `scripts/review_merge_train.sh` already lists open PRs once, caches file lists per distinct PR, and normally reads five active-run statuses twice for safety. | Log calls/pages, latency, cache hits, and outcome by endpoint category. **No verified redundant call to remove yet; estimated reduction: 0 until measured.** If a repeated metadata lookup is found across 29 PRs, reuse the existing PR-list row—**up to 28 calls per repeated lookup**, conditional, without weakening the two-pass check. |
| Cancel on PR Close / scheduled cleanup | Run summaries 38270862087 and 38268250577 each say **three unlinked active runs were preserved**. Code fetches queued and in-progress runs separately and batches PR states in groups of 50. | Keep both bounded status filters and fail-open preservation. Log pages, batch count, unresolved PRs, and preserved-run reason; **no supported reduction** from combining calls. |
| Validation Refresh / checkout | Run 38253831133 has one observed GraphQL error after processing 13 repos. | Log read endpoint *category*, attempts and final status; retry only transient reads. Call count may rise by at most the configured retries on this path, while avoiding a full workflow rerun if successful. |

These recommendations follow `CLAUDE.md` §15: extend existing fetches, use cycle-local caches, and fail open safely on cache misses. Do not introduce a per-PR API call merely to improve logging; count calls at the existing helper. Log rate-limit resource, remaining budget and retry wait **without request bodies or credentials**.

## Prompt Cache & Memory System

The assembled sample reports **47,625,161 cache-read**, **3,987,540 cache-write**, and **11,383,691 prompt tokens** across 108 reported OpenRouter calls. These totals need the deduplication repair. Aggregate `cache_hit_rate` is **null**, including where usage is unavailable for four calls; do not convert null to zero. Four review runs have reported rates **61.9%, 62.2%, 75.7%, and 84.6%**; their simple mean is **71.1%**, not a population estimate. Release-test run 38253656098 is **0%** on 10 calls.

Unstable prefixes or dynamic noise *could* fragment cache entries, but this archive contains no prefix hashes or miss reasons to establish either cause. Emit `CACHE_PREFIX` with a non-content hash, static-prefix version, model, breakpoint status, prompt-component token counts, and hit/miss reason. Keep invariant instructions before variable run data where output remains equivalent; compare cache-read tokens and latency on matched workloads. That is a **low-risk measurement and ordering change**, with token and latency gains unquantified until replay. The two review budget warnings warrant measuring which component expanded, not silently truncating the rubric. Memory’s 11/11 observed hits are positive, but the near-full 1,400-token allocation leaves little headroom; measure whether selected records contribute to outcomes before raising it.

## Orchestrator Health

- **Progression is not inferable from dispatch counts alone.** Clarify had **146 skipped/153 runs**, plan **138/143**, implement **129 other plus eight cancelled/143**, and clarify-respond **140 skipped/142**. Recent run IDs 38271378709 (clarify), 38271378699 (plan), and 38271378781 (implement) all skipped. Log one eligibility/disposition record per dispatch with issue, wave, guard reason and terminal state; do not call skips successful phase completions.
- **Poll and merge state deserve correlation IDs.** Orchestrate-poll had **9 successes, 5 cancellations and 1 failure/15 runs**; the failure, 38246066105, lacks logs. Review runs for PR #7044—38246151160, 38252233393 and 38258869453—each succeeded but took **4,307s, 4,217s and 4,385s**. Log PR head, iteration, triggering change, deferral reason, conflict-heal attempt and judge disposition to separate intentional new cycles from churn.
- **Do not mistake negative-test output for live conflict regressions.** CI orchestrate-poll test logs intentionally emit fingerprint-verification errors; the named CI failing tests and checkout failures are the operational evidence. Track live `conflict_heal_attempts`, `judge_dispatches`, `stuck_age_ms` and terminal reasons separately. A workflow-heal intake summary for run 38268901859 reports `scope_guard_disabled`; record that as a guarded disposition, not a successful heal.

## Pipeline Flow Bottlenecks

| Stage | Observed bottleneck | Type | Next action, ordered by end-to-end impact |
|---|---|---|---|
| Clarify → plan → implement | Predominantly skipped dispatches; successful implement runs can still take **2,227s** (38260483740). | Eligibility versus compute not separated | Emit guard reason and active phase timers before altering dispatch frequency. |
| Review/autofix | **70 runs**, p50 **987s**, p95 **4,266.5s**; two failures after PR close and high multi-model token use. | Compute plus avoidable terminal-path work | Fix closed-PR gating first; then measure distinct model passes and review iterations. |
| Validate → release test | Child validation-refresh failed after **1,288s**; parent release test failed after **5,292s**. | Child failure / potential rerun | Retry transient checkout reads and report child outcome categories at the parent. |
| Orchestrate / merge | Poll p50 **968s**; merge-train scans took **192–218s**, with no per-category timing. | Polling and merge/conflict overhead unknown | Add scan-stage and live conflict/judge disposition timers; retain existing safety checks. |
| Runner acquisition | Readiness run 38271033352 waited about **150s**; CI run 38268305673 about **85s**. | Queueing | Publish job-level queue p50/p95 separately from execution; test only safe duplicate-dispatch coalescing. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** review/autofix p95 **4,266.5s**, CI p50 **904s**, and queue waits up to **150s** in the cited sample. **Top failure modes:** seven CI contract/install failures, two closed-PR review failures, and a validation child error that blocked one release test; the poll failure has no archive. **Highest-cost driver:** review/autofix contributes **63.64M/63.68M reported** OpenRouter tokens, but duplicate logs inflate that figure.

**Top three actions:**
1. Repair parent/step event deduplication and emit explicit cost-coverage and call IDs; this removes **13.35M falsely reported deep-dive tokens**, with no claimed bill reduction.
2. Guard review work after confirmed PR close; address the **two observed failures** and the **300s** needless wait.
3. Add bounded retry and per-stage diagnostics to validation checkout; preserve consumer-red versus mechanism-error distinctions to protect release gating.

## Metrics Appendix

**Scope:** one repository, 1,000 runs; archive generated **October 12, 2026, 15:40 UTC**. Assembled context reports telemetry on **120 runs**; mounted `summary.json` reports **22 deep-dive runs**. They are different coverage sets. The archive has one missing failed-run log (38246066105). Percentiles describe runs, not individual steps.

| Population | Runs | Success | Failure | Cancelled | Skipped | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| All | 1,000 | 264 (26.4%) | 12 (1.2%) | 14 | 710 | 2s / 1,252s |
| Success or failure only, derived | 276 | 264 (95.7%) | 12 (4.3%) | — | — | ≈231s / ≈2,499s |
| CI | 49 | 42 | 7 (14.3%) | 0 | 0 | 904s / 1,359.8s |
| Review/autofix | 70 | 65 | 2 (2.9% of all review runs) | 1 | 2 | 987s / 4,266.5s |
| Orchestrate-poll | 15 | 9 | 1 | 5 | 0 | 968s / 1,800.7s |

| Assembled-context telemetry, **reported, not dedup-corrected** | Value |
|---|---:|
| OpenRouter calls; usage available / unavailable | 108; 104 / 4 |
| OpenRouter total; prompt; completion tokens | 63,683,625; 11,383,691; 730,059 |
| Cache read / write tokens | 47,625,161 / 3,987,540 |
| `cache_hit_rate` aggregate | null; four sampled review values 61.9%–84.6%, one release value 0% |
| Codex calls / tokens | 2 / 4,052 |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 2,000 / 5,272,740; 119 samples—includes skips |
| `break_glass_count` / `context_budget_warn_count` | 0 / 2; warnings in review runs 38246098799 and 38252203873 |
| Deep-dive duplicate correction, whole-job-only | 88 → 66 OpenRouter calls; 56,248,627 → **42,898,670** tokens |

| MCP server and target | Reported queries / bytes | Fallbacks | Bootstrap / contribution | Probe OK / failed / skipped |
|---|---:|---:|---|---:|
| Semble `reviewer-context` | 9 / 134,703 | 0 | 9 bootstraps, 321,997ms; 76 sources, 2,502 static-duplicate bytes | 0 / 0 / 0; no probe evidence |
| Semble `overflow` | 0 / 0 | 52, all contract-test; **0 runtime** | No runtime contribution measured | 0 / 0 / 0; no probe evidence |
| Serena, no target observed | 0 / 0 response bytes; 0 tool calls | 0 | Per-tool breakdown: none observed | 0 / 0 / 0; availability unknown |

At least **two** reported Semble queries, **29,404 bytes**, two bootstraps and four CI fallback counts are verified parent/step duplicates in the mounted archive; do not use reported MCP counts as independent availability trials. **Other MCP servers observed:** none in inspected logs.

| GH API signal | Measured value / gap |
|---|---|
| Per-workflow calls, retries and rate-limit events | **Not collected**; PAT `used_in_job=unknown` in reviewed deep dives |
| Missing archive | One **HTTP 404**: collector fetch for run 38246066105 |
| Validation checkout | One observed GraphQL error: run 38253831133 |
| Merge-train scan | 29 PRs examined, zero released in run 38270862087; **actual API calls unmeasured** |
| Existing batching | Cancel-on-close uses two status-filtered run listings and PR-state GraphQL batches of up to 50; merge-train caches file lists within a scan |

The next collection should retain job queue timestamps, per-stage timers, API call-category counters, cost-event IDs and log-source coverage alongside the existing run ID and job/step identifiers.
