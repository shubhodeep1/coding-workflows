## Executive Summary

- **CI is the clearest avoidable delay.** In `shubhodeep1/coding-workflows`, 17 of 24 CI runs failed at `lint / Inventory parity`; run `36235957125` reached that check after roughly 19 minutes and reported missing entries in `docs/INVENTORY.md`. Fix the inventory and move its existing check ahead of long tests. **Estimated impact:** roughly 14–18 minutes earlier feedback on similarly failing runs. **Confidence: high.**
- **Reported model cost is materially overstated.** Run `36237495547` contains combined-job and per-step copies of the same usage lines. Across downloaded logs, deduplication changes OpenRouter usage from **96 calls / 61.99M tokens** to **82 calls / 51.83M tokens**. This is a **10.16M-token accounting correction, not a spending reduction**. Fix collector deduplication before using these totals for cost decisions. **Confidence: high.**
- **Review resolution repeatedly fails closed without a diagnostic cause.** Runs `36240110701`, `36240577301`, and `36241767821` end with `Resolver scope check failed closed (ValueError)`; the first two also show a missing support file. Preserve the scope guard, but log the specific sanitized check failure and verify support files before model work. **Estimated impact:** fewer unexplained 8–10-minute failed runs; preventable fraction unknown. **Confidence: medium.**
- **A successful workflow can still represent failed work.** Review run `36237495547` took 2,875 seconds, ended `success`, yet its final summary says `finalize_reason=editor_changes_lost` and `edits_pushed=false`. Surface this as a separate business-outcome signal. **Estimated impact:** earlier detection of ineffective review cycles; time saved is unmeasured. **Confidence: high.**
- **The release gate needs request-level diagnostics.** Run `36224773465` logged 47 HTTP 404 responses while waiting for implementation, then failed its canary after a retry. Log sanitized endpoint, status, and attempt totals before changing polling behavior. **Estimated impact:** potential reduction in repeated failed lookups; the responsible endpoint and safe call reduction remain unverified. **Confidence: medium.**

## Speed Optimizations

1. **Critical path — fail CI on cheap drift checks first.** CI run `36235957125` lasted 1,173 seconds before inventory parity listed the missing Claude issue workflow and scripts; 17 CI failures share that step. Move the existing `Inventory parity` step to the earliest compatible point after checkout, alongside the already-early `Drift check (generated docs)`, while retaining the ordering contract in `tests/test_ci_inventory_parity_order_contract.py`. Emit `CI_CHECK_RESULT` with check name, elapsed time, and mismatch counts. **Savings:** approximately 14–18 minutes to *detect* comparable failures, not a reduction in failure incidence. **Risk:** low; keep the same command and gate.
2. **Critical path — diagnose reviewer stalls before changing retries.** Review run `36237495547` took 2,875 seconds; its `minimax/minimax-m3` slot advanced after `stall_guard`, retried the same model, and advanced again about 11 minutes later before failback. Log per-slot attempt start/end, progress observed, reason, model, and token-usage availability. Only if the same-head retry is demonstrably making no progress should it be short-circuited. **Potential savings:** up to roughly 11 minutes for that repeated stalled attempt; not a demonstrated general saving. **Risk:** medium, because an early stop could discard a recoverable review.
3. **Lower-priority polling work — identify the 404 source.** `test_and_mark_stable` run `36224773465` emitted 47 `gh: Not Found (HTTP 404)` lines during `Phase 3b: Wait for PR creation`; the workflow’s poll loop reads PRs, issue labels, runs, active-job steps, and a branch commit. First add per-endpoint counters. If the missing branch is responsible, trial a short negative-cache TTL while retaining the existing inactivity and run-status checks. **Savings:** request latency is unmeasured; up to about two-thirds of *that lookup’s* repeated misses under a three-tick TTL, conditional on attribution. **Risk:** low for logging, medium for altered polling.
4. **Micro-optimization, not the critical path — Semble latency.** Deduplicated downloaded logs contain 17 `SEMBLE_QUERY` events totaling 7,707 logged milliseconds, versus multi-minute review and CI runs. Do not optimize query milliseconds before the failures and stalls above.

## Cost Optimizations

1. **Prevent expensive no-result review cycles, without dropping safeguards.** Run `36237495547` used **10.16M deduplicated reported OpenRouter tokens** and three editor attempts; its final summary records `editor_changes_lost` and no pushed edits. Add an early support/input preflight and record substantive-diff and commit results after each editor attempt. Consider stopping further same-head attempts only after a verified deterministic no-change condition. **Savings:** unmeasured; at most subsequent attempts that meet that condition, not the whole run’s tokens. **Quality risk:** medium—transient tool failures must remain retryable.
2. **Reduce repeated prompt expansion where evidence supports it.** Review run `36233724894` logged a 169,617-token review prompt and a **38.68%** run-level cache-hit rate; `36237495547` logged a 163,924-token prompt. Keep stable instructions ahead of run-specific diffs, bound repeated diff/history excerpts, and record prompt-component token counts and prefix hashes—not prompt contents—per call. **Illustrative ceiling:** removing 10% of the **12.87M deduplicated reported prompt tokens** would remove about **1.29M tokens** in this downloaded sample; achievable savings are unknown. **Quality risk:** retain required evidence and compare review findings before rollout.
3. **Measure MCP value rather than treating bytes as savings.** The assembled counters report 28 Semble queries and 245,844 logged bytes; downloaded logs yield **17 distinct queries / 157,536 bytes** after parent/step deduplication. Run `36237495547` alone has six distinct queries / 46,807 bytes. Semble may be supplying targeted context, but no before/after prompt-expansion baseline establishes a net reduction. Log selected-byte consumption and the context it replaced; trim only low-use chunks. Serena has **zero queries and zero response bytes**, so there is no evidence yet that it replaces downstream tool or model work.
4. **Defer model changes until accounting and outcomes agree.** The slow review logs show a `gpt-6-luna` medium-reasoning summarizer and multiple reviewer models; editor configuration in `36237495547` uses high reasoning. Compare a cheaper/lower-reasoning setting first on non-decision summarization, using finding retention and successful-commit outcomes. **Dollar savings cannot be estimated:** prices and a reliable per-model, per-attempt spend baseline are absent. Do not downgrade resolver or validation decisions on this evidence.

## Reliability Improvements

1. **Remove deterministic CI blockers.** The 17 inventory-parity failures include `docs/INVENTORY.md` omissions—for example, run `36235957125`; four further CI runs, including `36242355750`, failed generated-doc drift with `agents.md` out of date. Regenerate and commit the inventory/docs before rerunning CI, and retain both checks as fail-closed gates. **Expected impact:** removes those repeat failures if the same omissions persist. **Rollback:** revert documentation changes, not the gates. Separately, run `36235957125` also emitted a protected tracking-issue auto-close violation; change that PR-body reference from `Fixes` to `Refs`, rather than weakening the guard.
2. **Classify resolver failures without relaxing scope.** Runs `36240110701`, `36240577301`, and `36241767821` logged `ValueError` at the resolver scope check; judge run `36233413666` also failed that check and logged a missing `pr_changed_files.txt` earlier. These establish a recurring *failure point*, not one proven root cause. Add `RESOLVER_SCOPE_CHECK` with action, error class/sanitized reason, snapshot and conflicted-path counts, and whether restore verification ran. Preflight required support paths and record support-source identity. **Expected impact:** faster separation of missing inputs, unsafe edits, and snapshot faults; failure-rate reduction unquantified. **Rollback/fail-open:** logging may fail open, but scope verification and refusal to commit must not.
3. **Separate validation input failure from MCP availability.** Runs `36235618528` and `36239049270` logged `Prompt file not found:` with an empty path, then `VALIDATION_RAW_STATUS: codex_failure`. Both also logged `SERENA_FALLBACK target=validate phase=diagnose reason=disabled`; that disabled path is **not evidence** that Serena caused the prompt failure. Validate and log prompt-path presence and staging source before invocation, without logging prompt contents; preserve the failing validation outcome. **Expected impact:** removes ambiguity and may avoid repeated roughly 303–307-second failures once the input fault is fixed. **Rollback:** retain the current fail-closed validation gate.
4. **Keep release verification strict.** Run `36224773465` failed after two canary checks: the bait marker remained and content did not match the issue’s required three-line specification. Log review-dispatch receipt, review run ID, head revision, canary blob identity, and each verification/dispatch attempt. **Expected impact:** distinguishes an editor that did not act from stale polling or a failed dispatch; no safe retry reduction is established. Do not accept the corrupted canary.
5. **Interpret fallback and pressure counters correctly.** The **34** `SEMBLE_FALLBACK target=overflow` occurrences are marked `context=contract-test`—30 in CI and four in the release test—not observed runtime outages. The **two** Serena fallbacks are `reason=disabled` in the validation runs; there are no Serena probe results. Conversely, assembled telemetry’s three `CONTEXT_BUDGET_WARN` occurrences resolve to **two distinct warnings** in downloaded logs (`36237495547` and `36233724894`, at 81.96% and 84.81% of a 200,000-token window). They indicate prompt-size pressure, not demonstrated policy/rubric pressure. `BREAK_GLASS` is zero in observed telemetry. Emit event origin and a stable event ID so test fixtures and duplicate log copies cannot masquerade as incidents.

## AI Memory Health

Nine distinct `AI_MEMORY_TELEMETRY` retrieves in downloaded review/judge logs selected records **9/9 times (100%)**. Mean estimated size was **1,392.7 / 1,400 budgeted tokens (99.5%)**; keyword methods were `llm` **9**, `plain` **0**, `none` **0**. No sampled retrieve had zero records or `enabled:false`. A high hit rate does not establish relevance: log selected-versus-used record counts and truncation so the near-full budget can be assessed.

Across sampled logs there were 25 `record-run-event`, eight `record-candidate`, three `force-tick-put`, and four `force-tick-get` events. Seven force-tick events had `ok:false, fail_open:true`: validation runs `36235618528` and `36239049270` each failed one put and two gets after clone warnings; review run `36235518984` failed a put after a push warning. The validation runs skipped dispatch because claim ownership was unconfirmed, whereas `36235518984` dispatched without a persisted cooldown claim. Log the **claim policy and failure stage** explicitly; preserve each path’s existing safety behavior. Of 33 telemetry entries with `push_attempts`, two recorded two attempts and 31 recorded one; higher retry counts were not observed. No `finalize-task`, `promote`, `compact`, or processed-command operations appeared in these downloaded excerpts; maintenance-run logs are unavailable, so verify their emission rather than inferring non-execution.

## GH API Call Audit

- **Measured failed-response hotspot:** `test_and_mark_stable / e2e-smoke-test / Phase 3b`, run `36224773465`, logged **47 HTTP 404 responses**. The current loop includes a branch-commit lookup before a PR exists, but the log does not identify which endpoint returned each 404. Add a wrapper summary keyed by *endpoint template*, with status, elapsed time, retry number, and cache-hit count. If it confirms repeated missing-branch reads, a three-tick negative cache could avoid approximately **31 of 47** such reads in a comparable interval; verify that branch progress remains visible through other signals.
- **Possible high-volume loop, not a measured call count:** that Phase 3b loop performs PR, issue, runs, conditional jobs, and commit reads on a 10-second poll interval. It already reuses one runs response for several calculations. Under `CLAUDE.md` §15, first reuse that response and existing cycle-local data; do not add a per-item lookup. Instrument before claiming a call-count saving from further batching.
- **Other workflow steps lack request totals.** Review sweep `36242593093` reported three candidates, two dispatches, and one skipped-active candidate; auto-release run `36242678986` reported an internal failed dispatch attempt within a budget of three, despite workflow `retries=0`. Neither supplies endpoint call totals. Add `GH_API_SUMMARY` per job/step: endpoint template, logical requests, attempts, 404/429/5xx counts, rate-limit resets, and response-time p95. Avoid URLs with issue text, tokens, or query contents. No sampled 429/rate-limit event establishes current rate-limit pressure.

## Prompt Cache & Memory System

Run-level `cache_hit_rate` varies sharply: **85.04%** (`36221120963`), **82.74%** (`36225738221`), **38.68%** (`36233724894`), and **0%** in release run `36224773465`. The aggregate field is **null**, and two distinct review context warnings show growing prompts. Inference: changing diff/history and other dynamic material ahead of reusable instructions may fragment cache prefixes; the logs do not prove prefix order. Emit a non-content prefix hash, component token sizes, model, cache read/write tokens, and breakpoint result per call. Move only stable instructions before dynamic material after checking equivalent outputs.

Deduplicated downloaded usage reports **38.13M cache-read tokens** and **155,123 cache-write tokens**. The computed read share of prompt-plus-read tokens is approximately **74.76%**; it is **not** the missing aggregate `cache_hit_rate` or a dollar-savings estimate. The nine memory retrieves were hits but nearly exhausted their fixed budget; log relevance and truncation before increasing it. Keep optional memory failures fail-open while retaining the explicit ownership check on validation dispatch.

## Orchestrator Health

`orchestrate_poll` completed **34/34 runs successfully**, p50 **348 seconds**; this measures workflow outcomes, not completed waves. `clarify`, `plan`, and `implement` had respectively **141/151**, **137/141**, and **133/140** skipped runs, often with empty log archives. Do not classify skips as clarification loops or stalls without a skip reason. Emit one transition record with tracking issue, wave, prior/new state, deferral reason, active-child count, and age.

Two cancel-cleanup summaries (`36240473742`, `36241977748`) each say three active runs lacked PR linkage and were preserved; log stable linkage-miss categories and ages, not identifiers alone. The fingerprint dedup in heal intake `36239974115` matched source issue `4510` to existing `4487`, a useful anti-duplication signal; track its hit count. Review run `36237495547` shows why GitHub `success` must be paired with the existing `REVIEW_AUTOFIX_RUN_SUMMARY_V1` fields (`finalize_reason`, slot result, `edits_pushed`). Track repeated same-head resolver failures, conflict-backfill attempts, claim deferrals, and time since last wave transition as observable stuck-state indicators.

## Pipeline Flow Bottlenecks

| Stage | Observed bottleneck | Next diagnostic or fix |
|---|---|---|
| Clarify → plan → implement | Mostly skipped workflow invocations; active-work duration and transition reasons are not reconstructable from empty archives. | Emit skip/transition reasons before interpreting cadence. |
| Review/autofix | p95 **1,436 seconds** across 173 runs; `36237495547` took 2,875 seconds and pushed no edits. | Preflight inputs, record per-slot latency/progress, and distinguish GitHub success from effective completion. |
| Validate → CI | Validation failed **3/4** runs; CI failed **21/24**, with 17 late inventory failures. | Diagnose the blank validation prompt; move inventory parity earlier and fix generated artifacts. |
| Orchestrate/release | Poller p50 **348 seconds**; release smoke `36224773465` took **2,486 seconds** and failed after canary retry. | Add poll/dispatch receipt and attempt timing; preserve canary gate. |
| Queue and merge/conflict overhead | No job queue timestamps or merge-attempt totals in the assembled metrics. Three sampled resolver failures also logged partial-clone backfill warnings. | Record queued/start timestamps and conflict attempt/result separately; do not attribute the whole run duration to either. |

Order end-to-end work by **deterministic CI blockers**, **review/validation failures**, then **poll-call hygiene**. Semble’s measured query time is a micro-optimization by comparison.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** late CI inventory checks (for example `36235957125`, 1,173 seconds), review/autofix tail latency (`36237495547`, 2,875 seconds), and the 2,486-second release smoke run `36224773465`. **Top failure modes:** inventory/generated-doc drift; sampled resolver scope checks that cannot be verified; blank validation prompt paths; and an unrestored release canary. **Highest-cost driver:** review usage, with 14 distinct model calls / 10.16M reported tokens in `36237495547` alone; raw collector totals double-count that run.

**Top three actions:** (1) correct generated inventory/docs and move inventory parity to the CI fast-fail lane; (2) fix timestamp-tolerant parent/step telemetry deduplication and add endpoint/attempt summaries; (3) add support, prompt, and resolver-scope preflight diagnostics while keeping scope, validation, and canary gates fail-closed.

## Metrics Appendix

**Scope and coverage.** Collector snapshot: **September 26, 2026, 12:45 UTC**. One repository, 1,000 runs. The assembled context reports some log telemetry for **125** runs; the supplied folder has **31 downloaded full logs**, **nine empty selected archives**, and 960 unselected log archives. Run summaries widen coverage, but job-level queue time, API call totals, merge iterations, and dollar costs are unavailable.

| Population | Runs | Success | Failure | Cancelled | Skipped/other | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---|
| All, `shubhodeep1/coding-workflows` | 1,000 | 341 (34.1%) | 41 (4.1%) | 10 | 608 | 7 / 691 s; includes skips |
| CI | 24 | 2 | 21 (87.5% of runs) | 1 | 0 | 1,112.5 / 2,562.1 s |
| Review/autofix family | 173 | 149 | 16 (9.25%) | 6 | 2 | 15 / 1,436 s |
| Validate | 4 | 1 | 3 | 0 | 0 | 316.5 / 494.3 s |
| Orchestrate poll | 34 | 34 | 0 | 0 | 0 | 348 / 421.35 s |
| Test and mark stable | 1 | 0 | 1 | 0 | 0 | 2,486 s; single run |

**Token and cache accounting.** “Deduplicated” below means matching parent-job/per-step events in the **downloaded** logs, not a corrected collector release. In `scripts/collect_workflow_logs.py`, the existing structured-line deduper compares full timestamped lines; copied events in run `36237495547` differ by microseconds. Extend its parent/descendant-only matching to normalize the timestamp or use a stable event ID, **without collapsing identical events from sibling steps**, and add a regression fixture with skewed timestamps. `scripts/cost_audit.py` can then aggregate the corrected lines.

| Metric | Assembled reported | Distinct downloaded-log events | Interpretation |
|---|---:|---:|---|
| OpenRouter calls; usage available/unavailable | 96; 92 / 4 | **82; 80 / 2** | 14 duplicated calls in `36237495547` |
| OpenRouter total tokens | 61,987,930 | **51,830,063** | 10,157,867 duplicated, not spend |
| Prompt / completion tokens | 15,361,188 / 956,394 | **12,874,600 / 827,591** | Usage-line sums |
| Cache read / creation tokens | 45,676,475 / 155,123 | **38,133,999 / 155,123** | Creation may overlap prompt accounting |
| Codex tokens / calls | 4,052 / 2 | Not independently reconciled | Separate counter; run `36241065396`’s summary quotes another token figure |
| Aggregate `cache_hit_rate` | null | null | Examples: 85.04%, 82.74%, 38.68%, 0% above |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 7,000 / 2,681,020 (122 assembled samples) | 541,000 / 2,825,500 (31 full-log samples) | Different sampling populations |
| `BREAK_GLASS` / `CONTEXT_BUDGET_WARN` | 0 / 3 | **0 / 2 distinct warnings** | One warning copied across two log forms |

| GH API evidence by workflow/job/step | Observed requests or responses | Retries/rate-limit evidence | Collection gap |
|---|---:|---|---|
| Release smoke `36224773465`, `e2e-smoke-test / Phase 3b` | **47 HTTP 404 responses** | No attributable 429 total | Endpoint templates and total attempts absent |
| Review sweep `36242593093`, `sweep / Enumerate open PRs and dispatch` | Calls unknown; **2 dispatched, 1 skipped-active** | Unknown | Per-endpoint counts absent |
| Auto-release `36242678986`, `release-check / Dispatch the release gate` | Calls unknown | Summary reports **one internal failed attempt**, budget three; workflow retries zero | Separate request and workflow retry counters |

| MCP telemetry | Assembled reported | Distinct downloaded-log evidence |
|---|---:|---:|
| Semble queries / logged bytes | **28 / 245,844** | **17 / 157,536**; downloaded raw copies were 26 / 233,806 |
| Semble fallbacks | **34** | 34 `target=overflow`, all `context=contract-test`; **0 observed runtime** |
| Serena queries / tool calls / response bytes / query ms | **0 / 0 / 0 / 0** | No per-tool query breakdown possible |
| Serena fallbacks / probe ok / failed / skipped | **2 / 0 / 0 / 0** | Both `target=validate`, `reason=disabled` |

| MCP target in downloaded logs | Distinct queries / logged bytes | Fallbacks | Probe ok / failed / skipped | Availability interpretation |
|---|---:|---:|---:|---|
| Semble `reviewer-context` | 6 / 80,955 | 0 | 0 / 0 / 0 | No target probe emitted |
| Semble `conflict-resolver-context` | 4 / 37,431 | 0 | 0 / 0 / 0 | No target probe emitted |
| Semble `overflow` | 7 / 39,150 | 34 test-only | 0 / 0 / 0 | Contract tests do not establish runtime availability |
| Serena `validate` | 0 / 0 | 2 disabled-path | 0 / 0 / 0 | Disabled is distinct from a failed probe |

The assembled context adds two Semble query occurrences / 12,038 bytes beyond downloaded-log raw counts; their distinct target/events cannot be independently verified here. Recent orchestrate-poll summaries (`36239872879`, `36240296879`, `36241535360`) say Semble was enabled but unavailable; log availability **reason and target** once per run to distinguish an unavailable rollout from the test-only fallbacks. **Other MCP servers observed:** none in validated sampled runtime events.
