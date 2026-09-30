## Executive Summary

- **The cost audit overcounts duplicated job-and-step log lines.** In review run `36710093970`, the collector reports 24 OpenRouter calls and 129.8M tokens; the step logs contain 12 calls and 64.9M tokens. CI run `36710322463` likewise reports eight Semble fallbacks for four test events. Fix collection before using these totals to set a budget. **Impact:** roughly halves the affected *reported* counts, not provider spend. **Confidence: high.**
- **Two review failures share a fail-closed resolver symptom.** Runs `36710787595` and `36711893947` both failed on PR `#5596` at “Run Codex resolver, validate, stage, commit,” consuming 522s and 469s. The guard logs `ValueError`, but not which invariant failed. Add a bounded reason code; retain fail-closed behavior. **Impact:** makes 991s of observed failed-run time diagnosable and may prevent an unchanged-state redispatch. **Confidence: high for the symptom; low for the underlying cause.**
- **Check-run waiting is a repeatable critical-path stall.** Five retained reviews—including `36710093970` and `36711436894`—each spent approximately 300s waiting for one queued/in-progress check-run before proceeding with a snapshot. Log the check-run identity, status changes, poll count, and why it remains eligible. **Impact:** up to five minutes per run *if* the wait proves unnecessary; no timeout reduction is justified yet. **Confidence: high.**
- **Reviewer execution dominates long reviews.** “Run reviewer models” took approximately 3,674s of the 4,420s in `36710093970`, and 2,361s of the 2,762s in `36710190747`. Add per-model attempt and wait timings before changing models or reasoning levels. **Impact:** a hypothetical 10% reduction in the first reviewer step is about six minutes; savings are not demonstrated. **Confidence: high for timing, low for achievable savings.**
- **A passing CI run hid three label-contract errors.** CI run `36710322463` logged three descriptions exceeding GitHub’s 100-character limit during label-sync dry-run, yet succeeded. Validate the contract before API reads and require zero dry-run errors in CI. **Impact:** prevents those three known-invalid create/update attempts from reaching live sync. **Confidence: high.**

## Speed Optimizations

1. **Reviewer compute — critical path.** In `36710093970`, the reviewer step occupied ~83% of the run; the interval between two recorded model completions reached 2,617s. That interval does **not** establish provider latency: tool work and retries within it are not separately timed. Emit `model`, `call`, `attempt`, request/wait/tool milliseconds, retry reason, and reviewer contribution at the existing usage boundary. Then trial fewer passes only where the current review tier and quality checks permit. **Estimated saving:** ~367s in this run under a *hypothetical* 10% step reduction; **risk:** low for logging, high for removing reviewers.
2. **Check-run wait — critical path.** Runs `36710093970`, `36710323223`, `36710976280`, `36711436894`, and `36711437994` each ended the check-run collection step at its 300s timeout. The collector already excludes its own run and backs off between polls; do not simply lower the timeout. Log each remaining check-run’s stable ID, workflow, status, last-change age, and final decision. End early only after establishing that a check is stale or outside the required set. **Estimated saving:** 0–300s per affected run depending on that diagnosis; **risk:** low for logging, medium for changing eligibility.
3. **CI test execution — secondary path.** CI run `36710322463` spent ~892s in “Orchestrate poll process unit tests”; recorded passing cases include 75.3s and 53.9s tests. Inspect those fixtures for real sleeps/backoff and use a fake clock where timing behavior remains asserted. **Estimated saving:** at most ~129s from just those two cases if their time is replaceable; **risk:** medium to timing-test fidelity.
4. **Hosted-runner queue — smaller, external delay.** Review runs `36717475987` and `36717459621` recorded roughly 13s and 16s of runner wait. Record queue versus execution time separately; prioritize the 300s waits and reviewer step rather than changing runner infrastructure. **Estimated saving from logging alone:** 0s; **risk:** low.

## Cost Optimizations

1. **Correct attribution before optimizing spend.** `scripts/collect_workflow_logs.py` has a structured-line de-duplication pass, but its key retains timestamps that differ slightly between composite job logs and child step logs. Across the 12 runs with retained full logs, leaf-only parsing changes reported OpenRouter usage from **260 calls / 466.3M tokens** to **130 calls / 233.1M tokens**. Prefer child-step telemetry, falling back to the parent only where no child exists—as in `36710787595`. Test both mirrored timestamps and parent-only archives. **Estimated provider saving:** zero; this is an accounting correction. **Quality risk:** none if genuine repeats within one step remain countable.
2. **Measure prompt expansion before trimming it.** Review run `36710190747` has two distinct `CONTEXT_BUDGET_WARN` events at 143,813 and 146,037 prompt tokens against a 140,000 threshold; `36709311504` has one at 176,674. Put stable rubric/instructions before variable PR, memory, and retrieved-code context, and log prefix digest, component bytes, inserted bytes, and cache read/write tokens per call. Keep required evidence intact. **Illustrative saving:** 10% of the approximately 45.3M *estimated de-duplicated, non-cache prompt tokens* in this selected window would be ~4.5M input tokens; that reduction is **not established**. **Quality risk:** high for blind truncation, low for measurement.
3. **Evaluate reviewer selection, not a blanket downgrade.** Nine retained slow reviews contain 130 distinct review usage lines; `google/gemini-3.8-flash` accounts for 25 lines and ~143.5M of their ~233.1M distinct recorded tokens. The failed resolver runs configured `openai/gpt-6-sol` with high reasoning, but their scope failure is not evidence that a cheaper model would work. Log model-level duration, tokens, pass outcome, and whether its finding changed the final decision; pilot the existing lite tier only on eligible low-risk changes. **Estimated saving:** unquantifiable until contribution and prices are joined to de-duplicated calls; **quality risk:** material for workflow edits and conflict resolution.
4. **Avoid unchanged-state failures.** The two PR `#5596` failures consumed 991s; their collected OpenRouter usage is zero, which is **not** proof of zero unmetered agent cost. Key redispatch suppression to PR, head, conflict fingerprint, and failure reason—never PR number alone. **Estimated saving:** one 469–522s failed run if the state truly had not changed; token saving unknown. **Quality risk:** low with a state-change escape hatch.
5. **Test MCP value rather than counting bytes as savings.** Retained review steps made nine distinct `SEMBLE_QUERY target=reviewer-context` calls returning 116,659 logged bytes; the failed resolver steps made two each to `overflow` and `conflict-resolver-context`. Semble supplies targeted context, but the prompt warnings mean these data cannot establish that it reduced expansion. Log scanned, selected, inserted, and discarded bytes by target and call ID. Serena has zero observed queries and is marked disabled in recent review summaries, so no Serena replacement or response-byte saving can be assessed. **Estimated saving:** unknown pending those counters; **quality risk:** low for logging.

## Reliability Improvements

1. **Preserve the resolver guard; identify its failing invariant.** Both PR `#5596` runs log “Resolver scope check failed closed (ValueError)” after resolver activity. `scripts/review_conflict_resolve.sh` can raise `ValueError` for several distinct snapshot, path, and merge-state conditions; the logs cannot choose among them. Emit an enumerated `RESOLVER_SCOPE_CHECK` reason, action, attempt, allowed/changed-path *counts*, merge-state-changed flag, and agent exit class—without raw paths or exception text. Keep refusal to retry or commit when verification fails. **Expected impact:** classify both observed failures and safely suppress exact-state repeats; **rollback:** disable the added diagnostic fields, not the guard.
2. **Make label validation visible to CI.** CI `36710322463`, “AI label sync dry-run smoke,” recorded **3 errors among 59 label outcomes** for descriptions of 106, 110, and 128 characters. Fix those contract descriptions, validate lengths locally before issuing reads, and have this CI smoke assert an empty errors array. **Expected impact:** eliminates three known invalid operations; **rollback:** retain the current dry-run output and live-sync error handling.
3. **Repair telemetry reliability.** In `36710190747`, four reported context warnings represent two child-step events; in CI `36710322463`, eight reported Semble fallbacks represent four. Fix composite/leaf attribution in the collector and add a regression test. **Expected impact:** trustworthy warning and cost rates; **rollback:** retain both raw log forms while reverting only the attribution change.
4. **Separate unavailable logs from workflow failure.** Review run `36713743563` is a 102s `startup_failure`, but its archive request returned HTTP 404 and supplied no job or step evidence. On that condition, record archive status and fetch available job/validation diagnostics once; report the cause as unknown if still absent. **Expected impact:** restores diagnostic coverage for this one run without guessing a cause; **rollback:** existing partial-data classification remains.
5. **Keep MCP fail-open signals correctly classified.** CI `36710322463`, “Targeted file context contract tests,” generated four distinct `SEMBLE_FALLBACK target=overflow` events using a deliberately missing executable. There are **no observed runtime Semble fallbacks** or Serena probe failures. Do not label this a broken rollout. Emit separate contract-test, runtime-fallback, and availability-probe summaries at the existing call sites. **Expected impact:** fewer false rollout alarms; **rollback:** retain raw events. The three distinct context warnings merit prompt-size investigation; observed `BREAK_GLASS` events are zero, not evidence of rubric pressure.

## AI Memory Health

Retained review logs contain **11 `retrieve` operations, all selecting records**: 100% hit rate in this selected sample. They average **1,387 estimated tokens against a 1,400-token budget** (~99.1% utilization); all 11 use `keyword_method=llm`, with none using `plain` or `none`. For example, `36710787595` selected 26 records at 1,398 tokens. No retained retrieve selected zero records, reported `enabled:false`, or contained `fail_open:true`.

The same logs contain 22 `record-run-event` and nine `record-candidate` operations. Of these 31 writes, **six report two push attempts and one reports three** (`36710787595`); the logged operations succeeded. Add per-write push duration, retry reason, and final status to `AI_MEMORY_TELEMETRY`, and compare those with the ~43–69s memory-write steps in slow reviews before changing retry policy. This is sampled review evidence, not a repository-wide memory hit rate; `finalize-task`, `promote`, `compact`, and processed-command operations were not observed here.

## GH API Call Audit

| Workflow / step | Evidence and call estimate | Smallest safe change |
|---|---|---|
| CI `36710322463` / “AI label sync dry-run smoke” | 59 per-label outcomes in ~13s. `scripts/ai_labels.py` performs a label GET for each valid contract entry; **at least 59 GET attempts**, barring retries, are inferred from code and outcomes—not an API counter. | Fetch paginated repository labels once, compare locally, retain per-label GET fallback if prefetch fails. If all labels fit one page, **59 → 1 read**; writes still occur only for changes. |
| Review / “Collect PR check-run failures CI lint autofix context” | Five named runs each logged five waits before timeout. The collector issues one paginated check-run fetch per loop: **at least 30 fetch invocations** across those runs, subject to extra pages/retries. | Log fetch/page/retry counts and check IDs; reuse a current same-head snapshot where valid. An eligibility fix could remove repeat polls, but call reduction cannot be promised yet. |
| Review sweep `36717359432` / `sweep` | `AUTOFIX_SWEEP_END` reports 45 candidates, 20 dispatched, 25 skipped active, zero failures; no API-call counter is supplied. | Emit list/detail/dispatch request counts and cache hits once per sweep before proposing batching savings. |
| Collector / run `36713743563` | One observed archive HTTP 404; no runtime rate-limit event or comprehensive API totals in retained logs. | Log endpoint *template*, status class, retry count, and duration; never credentials or response bodies. |

These changes follow `CLAUDE.md`/`codex.md` API hygiene: check for existing reads, prefer batched fetches and cycle-local caches, and fall back to the smallest legacy request on a cache miss. There is insufficient evidence to claim a window-wide rate-limit problem.

## Prompt Cache & Memory System

The reported selected-run totals include **372.8M cache-read**, **7.3M cache-write**, and **90.5M prompt tokens**, but mirrored usage lines inflate those totals. The aggregate `cache_hit_rate` is **null** because 10 of 272 reported usage lines are unavailable. Eight runs have individual rates: from **33.847%** (`36714526156`) to **83.508%** (`36710093970`); `36710190747`, which also warned on prompt size, reports **62.562%**. Cache-read tokens are 78.6% of reported total tokens, **not** an aggregate hit-rate substitute.

**Inference:** moving run-specific text or variable Semble/memory output ahead of a shared prefix could fragment caching, but prefix placement is not measured. Log a hash and token length of the stable prefix, dynamic-component lengths, cache-breakpoint result/retry reason, and usage availability by model and call; do not log prompt text. Keep memory retrieval’s useful 11/11 sampled hits while testing whether its near-full 1,400-token allocation contributes to the three distinct review prompt warnings. No observed cache fail-open event supports changing cache fallback behavior. **Expected impact:** establishes attributable cache and latency savings without risking evidence loss.

## Orchestrator Health

The window has **176/176 skipped** `orchestrate_clarify_respond` runs, **175/175 skipped** `implement` runs, and six successful `orchestrate_poll` runs (p50 438s). Those statuses do **not** establish stuck waves: skipped jobs have little or no step-log evidence. Review run `36717406750` supplies one explicit gate reason, `claude_fixer_awaiting_session`; sweep `36717359432` skipped 25 active candidates while dispatching 20.

Emit a compact decision event in the **existing dispatcher or gate**, rather than creating jobs solely to log skipped workflows: workflow/run, phase, candidate count, skip or deferral reason, prior/current wave state, conflict-heal attempt, judge-cycle decision, and terminal-state age. Track time from `claude_fixer_awaiting_session` to clearance and poll ticks with no state transition. **Expected impact:** distinguish legitimate deferral from a stalled loop; no safe cycle-time saving is measurable yet. The two review cancellations remain distinct from failures.

## Pipeline Flow Bottlenecks

| Flow segment | Observed bottleneck | Priority and diagnostic change |
|---|---|---|
| Clarify → plan → implement | Most runs skip; clarify has 170 skips in 177 runs, while plan and implement each have 175 skips. Their overall medians are 1s. | **Measurement first:** log upstream dispatch and skip decisions, not faster compute. |
| Review/autofix | Review p50 is 14s but p95 is **1,461s**; `36710093970` spent ~3,674s in reviewer execution. Recent gate runs also show ~13–16s runner queueing. | **Highest compute impact:** per-model and per-attempt timing; keep queue time separate. |
| Review check collection | Five retained reviews waited ~300s each for one incomplete check. | **Highest diagnosed wait impact:** identify the check and its eligibility before an early-exit change. |
| Validate / CI | Validate: two runs, p50 588s. CI: seven runs, p50 540s; `36710322463` reached 2,148s, including ~892s of orchestrator-process tests. | Profile test-case clocks; preserve tests and validation gates. |
| Orchestrate / merge-conflict | Poller p50 438s; the two PR `#5596` resolver failures stopped safely. Merge overhead versus other poll work is not separated. | Log state transitions and enumerated resolver reasons; do not weaken conflict checks. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** long reviewer-model steps (`36710093970`), five 300s check-run waits, and the CI test outlier (`36710322463`). **Top failure mode:** two fail-closed resolver scope checks on PR `#5596`; a separate startup failure has no archive. **Highest recorded cost driver:** review-model usage, subject to the confirmed composite-log duplication.

**Top three actions:**
1. Fix `scripts/collect_workflow_logs.py` cost-event attribution and regression-test parent-only versus parent-plus-child archives; this corrects the baseline before model decisions.
2. Add bounded resolver scope reason and state-fingerprint logging, then suppress only exact-state redispatches; retain fail-closed commit protection.
3. Instrument check-run eligibility, poll counts, and per-model review timing; use that evidence to select a safe early-exit or tier trial. Separately, batch the 59-read label-sync pattern and correct its three invalid descriptions.

## Metrics Appendix

**Window:** September 30, 2026, 11:27:37–12:57:50 UTC; one repository. The supplied context sets `insufficient_data=false`, but detailed telemetry is selected rather than comprehensive.

| Scope | Runs | Success | Failure | Startup failure | Cancelled | Skipped | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| All workflows | 1,000 | 270 (27.0%) | 2 (0.2%) | 1 (0.1%) | 2 (0.2%) | 725 (72.5%) | 2s / 296s |
| Review/autofix | 200 | 195 | 2 | 1 | 2 | 0 | 14s / 1,461s |
| CI | 7 | 7 | 0 | 0 | 0 | 0 | 540s / 1,672.2s |
| Orchestrate poll | 6 | 6 | 0 | 0 | 0 | 0 | 438s / 482.75s |
| Validate | 2 | 2 | 0 | 0 | 0 | 0 | 588s / 597s |

| Selected cost metric | Collector report | Coverage / interpretation |
|---|---:|---|
| Log-telemetry runs; wall-clock samples | 14; 14 | Collector summary; its wall-clock p50/p99 are **2,027,000 / 4,305,730 ms**. The supplied analysis context’s 114 telemetry rows and 112 wall-clock samples use a wider population and must not be mixed with these percentiles. |
| OpenRouter prompt / completion / total tokens | 90,534,130 / 3,840,594 / 474,510,870 | Reported, **inflated by mirrored lines** where child logs exist. |
| Cache write / read tokens | 7,302,670 / 372,837,388 | Same attribution caveat. Aggregate `cache_hit_rate=null`; eight individual rates available. |
| OpenRouter calls; usage available / unavailable | 272; 262 / 10 | Retained full-log subset: **130 reported calls → 130 distinct?** No: **260 reported → 130 distinct**. Two additional sampled runs report 12 calls but have no retained full-step files here. |
| `break_glass_count`; `context_budget_warn_count` | 0; 6 | Three distinct warning events verified in retained child logs: one in `36709311504`, two in `36710190747`. |
| Semble queries / logged bytes | 28 / 328,220 | Retained full logs verify **13 distinct queries / 146,699 bytes**; two sampled runs account for a further 4 reported queries / 49,842 reported bytes without retained full steps. |
| Semble fallbacks | 8 reported | **4 distinct**, all `target=overflow`, `context=contract-test`, CI `36710322463`; runtime fallbacks **0 observed**. |
| Serena queries / response bytes / tool calls / query ms / fallbacks | 0 / 0 / 0 / 0 / 0 | No per-tool breakdown exists because no Serena query was observed. |
| Serena probes OK / failed / skipped | 0 / 0 / 0 | **No instrumented probe observed**, not proof of availability. |

The **260 → 130** retained-call comparison is directly reproducible from full step logs. If the same mirroring holds for both additional sampled runs, the selected-run totals would be approximately **136 distinct calls and 237.3M distinct recorded tokens**; treat that extrapolation as provisional, **not** a spend reduction.

| MCP target / availability evidence | Distinct retained queries | Logged bytes | Distinct fallbacks | Probe OK / failed / skipped |
|---|---:|---:|---:|---:|
| Semble `reviewer-context` | 9 | 116,659 | 0 | 0 / 0 / 0 |
| Semble `overflow` | 2 | 11,164 | 4 contract-test events in CI | 0 / 0 / 0 |
| Semble `conflict-resolver-context` | 2 | 18,876 | 0 | 0 / 0 / 0 |
| Serena: no target observed | 0 | 0 | 0 | 0 / 0 / 0 |

The two sampled Semble runs lack retained target-level steps. Poller `36717447884` reports `SEMBLE_AVAILABLE=false` and `SEMBLE_INDEX_AVAILABLE=false`, but emits no target probe or runtime fallback; availability cannot be generalized from it. **Other MCP servers observed:** none in retained logs or excerpts.

**GH API coverage:** ≥59 inferred label GET attempts in CI `36710322463`; ≥30 inferred check-run fetch invocations across the five timed-out retained reviews; one collector archive HTTP 404 in `36713743563`. Exact window-wide endpoint counts, page counts, retry totals, and rate-limit rates were not supplied.
