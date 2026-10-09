## Executive Summary

- **Repair cost telemetry before using it to tune models.** In `shubhodeep1/coding-workflows`, echoed script examples were counted as Codex usage, while job-level and step-level copies inflated OpenRouter usage from **53 distinct calls to 85 reported calls**. Correcting collection changes the measured OpenRouter total from **63.77M to 38.57M tokens**; it does not itself save tokens. **Impact: high decision quality; confidence: high.**
- **Closed PRs are failing review instead of exiting cleanly.** Review runs `37871935623` (PR #6830), `37873106675` (PR #6833), and `37874335918` (PR #6566) logged a closed or merged PR, then failed at **Checkout PR head branch** because the workspace was unavailable. Check `PR_CLOSED` before workspace-dependent work. **Impact: prevent three observed failures and roughly two minutes of subsequent work per affected run; confidence: high.**
- **Implementation spends model time before discovering a known authorization boundary.** Runs `37872984794`, `37876411608`, and `37877571029` ended after **1,208–1,629 seconds** with `reason=no_allowlist` at the automation-path guard. Reject *provably* unauthorized protected-path plans before implementation; retain the final staged-file guard. **Impact: potentially many minutes per qualifying issue; confidence: medium** because actual staged paths are not known until later.
- **CI has two concrete, separate regressions.** Three CI runs failed `test_template_copies_are_identical`; run `37877468851` failed the Node scaffold/Hardhat-assets assertion. Restore template parity and fix the scaffold contract rather than weakening either test. **Impact: address four of four observed CI failures; confidence: high.**
- **MCP and prompt pressure need target-specific diagnosis.** Deduplicated logs show **12 runtime Semble fallbacks** at `target=overflow`, all `reason=binary-unavailable` in implementation, versus **16 intentional CI contract-test fallbacks**. Review run `37877468996` also emitted three distinct context-budget warnings, one at **195,041/200,000 tokens**. **Impact: less fallback context and lower prompt-size risk after remediation; confidence: high for occurrence, low for token savings.**

## Speed Optimizations

| Rank | Evidence and root cause | Exact change and expected saving | Risk |
|---|---|---|---|
| **1 — critical path: exit terminal-PR reviews** | In review runs `37871935623`, `37873106675`, and `37874335918`, **Check PR state (defense-in-depth)** set `PR_CLOSED`, but **Checkout PR head branch** still required an absent workspace. In `37874335918`, free-disk work took **216 seconds** before the state warning. | Gate the checkout step and other workspace-dependent setup on `PR_CLOSED != true`; move the existing state check ahead of avoidable setup where dependencies permit. Preserve the existing behavior when PR state is unknown. Expect approximately **2–5 runner minutes** saved on a comparable terminal-PR run, not the entire reported run duration. | Low; test closed, merged, unknown-state, and no-PR review modes. |
| **2 — critical path: preflight protected-path intent** | Implementation runs `37872984794` (issue #6834), `37876411608` (#6838), and `37877571029` (#6841) reached **Preflight destructive-commit guard** only after implementation work; all logged `reason=no_allowlist`. | After issue metadata and grant creation, classify an approved plan that *unambiguously* requires denied automation paths and route it to approval before invoking the model. Keep the staged-file check authoritative. Upper bound in these examples is **1,208–1,629 seconds per run**; realized savings require a measurable early classification rate. | Medium; ambiguous plans must continue, not be prematurely rejected. |
| **3 — smaller, measurable poller win** | Poll run `37875700711`, **Process each tracking issue**, reported **28 merge-queued PRs** skipped by the standalone conflict sweep. The current sweep performs a per-PR REST read *before* checking that label. | Add labels to the existing open-PR snapshot or its batched GraphQL read; skip only when the snapshot is valid for the candidate head, and fall back to the live REST read on missing or stale data. **Up to 28 REST reads per comparable poll**, with a roughly **tens-of-seconds**, not minutes, latency opportunity. | Low–medium; preserve the live-read fallback. |

**Queue time is separate from these compute savings.** Review run `37874335918` waited roughly **27 minutes** between job eligibility and runner request, then logged **126 seconds** of hosted-runner contention. Poll run `37875700711` similarly spent about **14 minutes** before runner request and **75 seconds** in an explicit “runners busy” interval. Add timestamps for *eligible → requested → assigned → first step* before attributing all pre-run delay to runner capacity.

## Cost Optimizations

1. **Fix measurement first.** The six implementation deep dives contain **no Codex token match after removing echoed shell-script lines**, although their raw telemetry reports **110 Codex calls and 13,221,390 tokens**. The assembled total is **118 calls and 13,221,394 tokens**. Separately, split review archives `37874356831` and `37877468996` duplicate usage lines with slightly different timestamps. Deduplicate by semantic event identity—run/attempt, job, phase, call, model and usage fields—not exact timestamped text; parse Codex usage only from runtime output. **Savings: $0 directly; prevents unsound model and budget decisions.** Do not interpret the corrected absence of matches as proof that actual model cost was zero.

2. **Avoid model work on deterministically ineligible issues.** The three `no_allowlist` implementation failures consumed long runs; `37872221909` and `37876295511` instead ended in deliberate `BLOCKED` verdicts and correctly did not retry that verdict. Surface protected-path and hold-scope incompatibilities at intake where provable, before implementation. **Estimated saving: the model portion of each preventable run, presently unquantifiable in dollars** because reliable implementation usage is absent. **Quality risk:** never infer authorization from a model’s proposed edit alone or relax the final guard.

3. **Measure review-panel value before reducing it.** Four selected successful reviews produced **53 distinct OpenRouter usage calls** across several models, including eight `openai/gpt-6-luna` calls; run `37877468996` alone produced **17 calls** in **2,160 seconds**. Record pass, reasoning level, verdict change, and whether a second pass changed the final finding. Pilot fewer passes only for *validated* unchanged-head/base or equivalent low-risk cases; retain full review and judge behavior otherwise. **Potential saving: calls from a demonstrably redundant pass; no defensible token or dollar estimate yet.** **Quality risk: material** if passes are removed without verdict-equivalence evidence.

4. **Reduce repeated context selectively.** Deduplicated Semble queries returned **61,331 logged bytes in five queries**, with **26 sources** and `static_dup_bytes=0`; no before/after prompt measurement proves they displaced more context than they added. Implementation had **12 `target=overflow` binary-unavailable fallbacks** and no successful Semble query in those sampled runs. Log fallback context bytes and prompt bytes with/without a query, then fix binary availability or bound fallback context. Serena recorded **zero queries, tool calls, response bytes, fallbacks, or probes**: there is no evidence it replaced downstream work or added noise. **Savings: unknown until those byte and work-displacement fields are emitted;** do not disable useful context solely to meet a token target.

## Reliability Improvements

| Priority | Failure evidence and category | Smallest safe fix; expected effect | Fail-open / rollback |
|---|---|---|---|
| **1** | Three of four review failures were terminal-PR/workspace sequencing errors: `37871935623`, `37873106675`, `37874335918`. | Make `PR_CLOSED` an explicit successful skip before checkout, with a structured skip reason. Could eliminate **3/4 observed review failures** if the pattern persists. | Retain current unknown-state handling; roll back the new skip condition independently. |
| **2** | Three of five implementation failures ended `IMPLEMENT_AUTOMATION_PATH_GUARD … reason=no_allowlist`; two others (`37872221909`, `37876295511`) returned deliberate `BLOCKED`. This is authorization/plan mismatch, not a transient retry failure. | Log grant reason and approved-path *counts* at plan acceptance; request human approval or re-plan before model execution when incompatibility is certain. Track `blocked_by_policy` separately from infrastructure failure. | **Never** fail open on a denied staged-path guard; ambiguous early checks defer to the existing final guard. |
| **3** | CI runs `37871916424`, `37872030453`, `37874335446` failed template-copy parity; `37877468851` failed `test_node_runtime_scaffold_has_no_hardhat_assets_and_wires_custom_tests`. | Synchronize generated/live hook copies and correct the scaffold or its intended contract; run these focused tests before full CI. Addresses the **four observed CI failures**. | Do not suppress assertions. |
| **4** | Semble: **12 distinct runtime fallbacks**, `target=overflow`, `reason=binary-unavailable`, across implementation runs `37872221909`, `37876295511`, `37876411608`, `37877571029`; **no successful overflow queries** there. The **16 distinct** `target=overflow` fallbacks in four CI runs carry `context=contract-test` and are intentional tests, not rollout incidents. | Emit one availability check per implementation job, including executable-found and bootstrap state; restore binary availability or explicitly use bounded legacy context. This should remove repeated *masked* runtime fallbacks if availability is the cause. | Preserve bounded legacy-context fail-open; alert on runtime, not contract-test, counts. |
| **5** | Judge run `37872427340` refused a host fallback for a host-only conflicted path. | Log `conflict_class`, isolation reason, and required manual owner/action; route to manual merge. Improves diagnosis of this **one observed failure**. | Keep isolation refusal; do not enable host fallback. |

`BREAK_GLASS` **events: 0 observed**. Run `37877468996`, **Run reviewer models**, emitted **three distinct** `CONTEXT_BUDGET_WARN` events (six in raw duplicated telemetry); the highest reached **97.52%** of a model’s window. That indicates prompt-size risk, **not demonstrated policy/rubric break-glass pressure**. Log the contributing context-block sizes and trim low-value blocks; do not merely raise the warning threshold.

## AI Memory Health

- Across **14 distinct `retrieve` events** in selected deep dives, **14/14 selected records**: reviewer **8/8**, averaging **1,389.9 of 1,400 budget tokens** with `keyword_method=llm`; implementation **6/6**, averaging **1,587.2 of 1,600** with `keyword_method=plain`. `none`: **0**. No observed retrieve returned zero records, reported `fail_open: true`, or reported `enabled: false`. This is healthy retrieval availability but leaves little budget headroom.
- **33 observed push-bearing events** had attempt counts: **16 at one**, **13 at two**, and **four at three or more**. Run `37872427340` needed **six** attempts for a `record-run-event`; `37872030807` needed **five** for `record-candidate`. All those observed events eventually reported success. Add `push_retry_reason`, elapsed milliseconds and final outcome, without logging memory contents; investigate if high-attempt events repeat.
- Closed-PR reviews `37871935623`, `37873106675`, and `37874335918` each retrieved reviewer memory before failing. Reorder terminal-state checks to avoid about **4,161 estimated retrieval tokens across these three runs**, while preserving retrieval for active reviews. `finalize-task`, `promote`, `compact`, and `processed-command-complete` were **not observed in the 19 full-log runs**; verify emission and coverage before diagnosing their absence as malfunction.

## GH API Call Audit

- **Measured hotspot, bounded opportunity:** poll `37875700711`, standalone conflict sweep, skipped **28 `ai:merge-queued` PRs after the per-PR REST lookup**. Its initial open-PR list omits labels, while an existing batched clean-state query handles other candidates. Reuse or extend that snapshot to identify queued PRs; require a matching head and use the current REST path on cache miss. This follows this repository’s **`CLAUDE.md` §15** requirements to check existing calls, batch per-item reads, use cycle-local caches and fail open. **Potential reduction: up to 28 REST reads per similar cycle;** verify with counters.
- **Do not duplicate existing batching.** `scripts/orchestrate_poll_process.sh` already batches managed-stall linked-PR lookups at **25 issues per GraphQL call** and clean standalone PR checks in **30-PR batches**. Poll `37875700711` reported **27 merge-train-queued stall skips**. Add cache-hit/miss and batch-size counts before alleging those paths make 27 per-item calls.
- **Call totals and rate-limit incidence are not measurable here.** `GH_PAT_BUDGET` reports `used_in_job=unknown`; poll `37875700711` crossed a reset between readings (**4,913 remaining → 5,000**), so subtraction is invalid. The observed `HTTP 422` on PR #6670 was a merge conflict, **not a rate-limit event**; no production 429 was established. In the existing `gh_retry` wrapper, emit per-run/job/step **endpoint category, call count, response status, retry count, cache hit/miss, and reset-aware rate-limit delta**—never request bodies or credentials. This will quantify both call reduction and rate-limit risk.

## Prompt Cache & Memory System

- **Measured:** four selected successful reviews had **53 distinct** OpenRouter calls, of which **40 reported available usage** and **13 did not**. Of the 40, **24 reported positive cache reads** and **16 reported zero**. Deduplicated logged totals are **30,775,325 cache-read**, **1,796,350 cache-write**, and **5,623,624 prompt tokens**. These are usage fields, **not a bill or a global cache-hit percentage**.
- **`cache_hit_rate` is unavailable at repo level.** The one complete per-run value is **0.840327** for review run `37872030807`; the collector deliberately reports `null` when usage is unavailable. Emit cache availability and a stable-prefix hash per phase/model, keeping variable PR metadata, timestamps and volatile diagnostics *after* reusable instructions. Compare zero-read calls by provider before calling them fragmentation: provider behavior and missing usage remain alternatives. **Token/latency savings cannot be estimated reliably until that comparison;** preserving prompt meaning keeps reliability risk low.
- Reviewer memory is consistently useful by selection count but nearly fills its budget. Log selected-record count, budget, estimated tokens and block-level prompt sizes together. For run `37877468996`’s **three distinct** budget warnings, remove or summarize demonstrably redundant context while retaining security and review requirements. Monitor warning rate and cache-read changes after the edit; do not trade correctness for a nominal cache gain.

## Orchestrator Health

Poll `37875700711` found **one active tracking issue**, #6664; its security-pass fix issue #6841 remained in progress. The same poll logged **27 `merge_train_queued` stall skips**, **18 human-gated-latch skips**, and **two security-dependency-held skips**; examples of `ai:done` age reached **3,637 minutes**. The skips show guards preventing duplicate recovery, **not proof that the merge train progressed**. PR #6670’s update-branch returned a non-retryable merge-conflict `422`, after which conflict review was dispatched. Judge run `37872427340` separately reached a manual-merge isolation boundary.

Keep these safeguards. Add one per-issue transition record with tracking issue, wave, prior/new state, queued-PR count, blocker class, action, retry cap and next eligible time. Alert on *age without transition*, rather than on expected skip volume. All **1,000** collected runs show attempt 1 and zero recorded run retries; that does **not** rule out new-run dispatches or internal conflict-heal attempts.

## Pipeline Flow Bottlenecks

| Flow stage | Observed bottleneck | Diagnostic logging / action |
|---|---|---|
| Clarify → plan | Many intentional skips: clarify **166/174**, plan **150/165**; active plan median **710 seconds**. | Emit eligibility/skip reason and plan model time; do not treat skipped runs as failed throughput. |
| Implement | Active median **1,211 seconds**; three late grant denials and two deliberate `BLOCKED` verdicts among five failures. | Record plan/grant decision before model start and actual model duration/usage; retain final guard. |
| Review/autofix | Active median **500 seconds**, p95 **1,444**; three terminal-PR checkout failures and up to **27-minute** pre-run scheduling delay in a selected failure. | Emit gate-to-job timing, PR-state checkpoint, workspace outcome and panel-pass duration separately. |
| Validate/orchestrate | CI active median **1,035 seconds**, **4/10 failures**; poll active median **708 seconds**, with merge-train and conflict sweeps. | Put focused parity/scaffold checks early; emit wave-transition and API-cache counters. |

**Ordering:** eliminate invalid work and false failures first; then measure scheduler wait versus model compute; then pursue the poller’s smaller API-call win. The available runs do not establish clarification-loop count, review/autofix iteration count, merge throughput, or conflict-heal retry rate.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks:** review scheduling and compute (`37874335918`, `37877468996`); implementation ending at authorization guards (`37876411608`, `37877571029`); CI parity/scaffold failures (`37871916424`, `37877468851`).
- **Top failure modes:** terminal PR reaches workspace checkout (**3 runs**), automation paths lack an allowlist (**3 runs**), deterministic CI assertions (**4 runs**). Highest observed reported cost driver is review OpenRouter usage, but the raw total is duplicated; implementation model cost is not trustworthy.
- **Top three actions:** **(1)** repair telemetry deduplication and echoed-script filtering; **(2)** make terminal-PR review a safe early exit and surface provable grant mismatches before model work; **(3)** fix focused CI assertions, then instrument poller cache hits and API counts. These retain current authorization and merge safeguards.

## Metrics Appendix

**Window:** October 9, 2026, **01:53:08–03:47:54 UTC**; one repository. “Active” excludes skipped runs. The mounted collector has **19 full-log runs: all 13 failures and six successes**; the assembled context reports **119 rows with log telemetry**, many without full deep-dive logs. Thus outcome counts cover 1,000 runs, while detailed diagnostic and cost conclusions have narrower coverage.

| Scope | Runs | Success | Failure | Skipped/other | Active success rate | Active duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| All workflows | 1,000 | 254 | 13 | 733 | **95.1%** (254/267); all-run failure **1.3%** | **474 / 1,298 s** |
| Review/autofix | 148 | 143 | 4 | 1 | 97.3% | 500 / 1,444 s |
| Implement | 160 | 4 | 5 | 151 | 44.4% | 1,211 / 1,676 s |
| CI | 10 | 6 | 4 | 0 | 60.0% | 1,035 / 1,259 s |
| Plan | 165 | 15 | 0 | 150 | 100% | 710 / 896 s |
| Orchestrate poll | 7 | 7 | 0 | 0 | 100% | 708 / 1,411 s |

The **all-run p50/p95 are 1/740 seconds** when skips are included; the 1-second median is not active-pipeline latency.

| Cost/cache metric | Reported telemetry | Deep-dive interpretation |
|---|---:|---|
| OpenRouter calls / total tokens | **85 / 63,767,320** | **53 distinct step-level calls / 38,567,454 tokens** after removing duplicate job-level copies; not a dollar total |
| OpenRouter prompt / completion | 8,970,733 / 637,366 | **5,623,624 / 374,323** distinct step-level |
| OpenRouter cache read / write | 51,354,443 / 2,807,870 | **30,775,325 / 1,796,350** distinct step-level |
| OpenRouter usage available / unavailable | 62 / 23 calls | **40 / 13** distinct calls |
| Codex calls / tokens | **118 / 13,221,394** assembled | **Unreliable**: six implementation deep dives’ matches are echoed script examples; actual implementation model usage is not established |
| `cache_hit_rate` | Repo **null** | **0.840327** in run `37872030807` only; other selected calls lack complete coverage |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | **6,500 / 2,329,650**, 114 assembled samples | Mounted full-log collector: **1,553,000 / 2,479,640**, 19 samples; assembled distribution includes many short skips |
| `break_glass_count` / `context_budget_warn_count` | **0 / 6** | **0 / 3 distinct warnings**, all three in `37877468996` |

| MCP metric | Reported | Distinct emitted events and scope |
|---|---:|---|
| Semble queries / logged bytes | 7 / 90,257 | **5 / 61,331**: `reviewer-context` **4 / 55,652 bytes**, `conflict-resolver-context` **1 / 5,679 bytes** |
| Semble bootstrap / time | 7 / 229,970 ms | **5 ready / 167,874 ms**; none failed or unused in these queried runs |
| Semble sources / static duplicate bytes | 35 / 0 | **26 / 0** distinct |
| Semble fallbacks | 42: runtime 22, contract-test 20 | **28**: `overflow` runtime **12** (`binary-unavailable`), CI contract-test **16** (intentional); fallback bytes not logged |
| Serena queries / response bytes / tool calls / fallbacks | 0 / 0 / 0 / 0 | **No tool breakdown available**; no Serena query or fallback events observed |

| MCP target availability | `probe_ok` | `probe_failed` | `probe_skipped` | Interpretation |
|---|---:|---:|---:|---|
| Semble `reviewer-context` | 0 | 0 | 0 | No probe events emitted; four successful queries observed |
| Semble `conflict-resolver-context` | 0 | 0 | 0 | No probe events emitted; one successful query observed |
| Semble `overflow` | 0 | 0 | 0 | No probe events emitted; **12 runtime**, **16 contract-test** fallbacks |
| Serena — no target observed | 0 | 0 | 0 | Availability **unmeasured**, not proven healthy or broken |

**GH API summary:** exact per-job calls, cache-hit rate, rate-limit-event count and reset-adjusted usage are **not logged**. The actionable observed pattern is **28 queued-PR REST lookups in poll `37875700711` before skip**; its `GH_PAT_BUDGET` readings cannot supply a call delta. **Other MCP servers observed:** none with validated query/fallback/probe events in the selected logs.
