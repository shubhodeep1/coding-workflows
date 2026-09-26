## Executive Summary

- **CI is the clearest repeatable failure.** Seven of ten CI runs failed; five spent 558–630 seconds before reaching the same thread-reuse assertion in `lint / Validation bootstrap and family direct-run tests` (for example, runs 36170477132 and 36186609211). Move that contract check ahead of the long test suite. **Estimated impact:** about 50 minutes saved across those five observed failures if caught near startup. **Confidence: high.**
- **Successful review runs can conceal expensive recovery or a blocked PR.** Run 36203196657 lasted 2,038 seconds, used 26 OpenRouter calls, and reported `editor_changes_lost` followed by redispatch. Recent successful review gates for PRs 4443 and 4450 reported dirty merge state and a fingerprint cap of 3/3. Emit an explicit terminal *work outcome* distinct from the GitHub run conclusion. **Impact:** fewer repeated reviews and faster escalation; savings cannot yet be measured. **Confidence: high.**
- **Two review/autofix runs failed safely, but late:** runs 36176625636 and 36196360602 rejected an incomplete security-sensitive support set after 323 and 430 seconds. Preserve that refusal; preflight the verified bundle before expensive setup and report which required component is absent. **Impact:** potentially several minutes sooner per affected run. **Confidence: high.**
- **Synchronous memory work is visible on the poll critical path.** In poll run 36203394070, the start and completion memory-event steps each took about 55 seconds of a 362-second run. Instrument clone, write, and push time separately before changing persistence behavior. **Potential impact:** up to roughly 110 seconds per comparable poll if that overhead proves reusable; **confidence: medium** on savings, high on measured delay.
- **The headline median hides active work.** Of 1,000 runs, 685 were skipped; overall p50 was 2 seconds, versus 343 seconds for the 47 poll runs. Publish active-run and skipped-run metrics separately. **Impact:** better prioritization rather than direct runtime savings. **Confidence: high.**

## Speed Optimizations

1. **Review/autofix critical path — highest potential.** Review runs 36172003252 and 36179301062 took 4,237 and 3,588 seconds. Their `codex-agent` logs contain 86 and 79 distinct ledger-substate timeout warnings, respectively; those counts **must not** be multiplied by the 120-second timeout because calls may overlap. Root cause category: auxiliary event emission repeatedly reaching its timeout during model work. In `scripts/ledger_emit_substate.sh`, emit a once-per-run summary of attempts, timeouts, skipped emissions, and elapsed time; then test a per-run circuit breaker for *auxiliary* substate emissions after the first timeout, retaining required memory writes and existing fail-open behavior. **Estimated savings:** one or more 120-second waits where timeouts are sequential; measure before claiming a run-wide gain. **Risk: medium.**
2. **Fail CI contracts early.** The same `tests/test_codex_thread_reuse_core.py` assertion—expecting the validation workflow’s `stage_workflow_support.sh` bootstrap—ended five CI runs totaling 3,033 seconds. Run this contract and the shared shell-block guard before family direct-run tests; correct either the workflow or the contract to the intended trusted bootstrap, without weakening it. **Estimated savings:** most of each 558–630-second failed run on recurrence. **Risk: low** for ordering, medium for any contract change.
3. **Expose and then shorten poll memory overhead.** Poll run 36203394070 spent approximately 55.8 seconds recording `poll_started` and 54.7 seconds recording `poll_completed`; `Process each tracking issue` occupied about 109 seconds. Add `clone_ms`, `write_ms`, `push_ms`, and `attempts` to memory-operation telemetry. If clone time dominates, reuse a safely refreshed run-local checkout with the existing conflict/retry path. **Conditional savings:** roughly 45–55 seconds on a subsequent write; not yet established. **Risk: low** for logging, medium for checkout reuse.
4. **Instrument a CI blind interval before tuning shards.** Three successful CI runs took 2,326–2,624 seconds and each had an approximately 1,002–1,155-second gap before the next `orchestrate-poll shard 0` output. Emit flushed shard start/end, current test, and periodic elapsed-time events. Four shards were already configured in run 36168535223, so adding shards without this evidence is premature. **Immediate savings: none;** enables a targeted critical-path change. **Risk: low.** Semble queries taking roughly 0.4–0.7 seconds are micro-optimizations by comparison.

## Cost Optimizations

1. **Prevent avoidable editor recovery first.** Run 36203196657 reported `editor_changes_lost` and redispatch after a 2,038-second review using 26 calls and 44.21 million OpenRouter usage tokens, including cache reads. Validate the on-disk diff and claimed changes immediately after editor execution, before downstream work; log the loss point and preserve the existing redispatch as fallback. **Savings:** potentially a comparable repeated attempt, *not* a claim that all 44.21 million tokens in this run were wasted. **Quality risk: low** if this only detects loss.
2. **Investigate low cache reuse without changing review content.** Across the assembled 88 OpenRouter calls, cache-read tokens were 73.79 million and noncached prompt tokens 14.73 million; calculated `cache_hit_rate` was 83.36%. Review run 36179301062 was lower at 47.73% (12 calls). Log stable-prefix hash, model, phase, and dynamic-context placement per call; keep invariant instructions before run-specific material. **Conditional savings:** shifting roughly 0.81 million tokens from noncached prompt to cached reads in a run with 36179301062’s input volume if its hit rate reached 75%; dollar savings require provider prices and billing data. **Quality risk: low** for ordering-only changes.
3. **Measure Semble’s net context value.** The assembled window records 32 `SEMBLE_QUERY` calls and 224,162 logged bytes: 25 `target=overflow` calls/121,690 bytes in implement and seven `target=reviewer-context` calls/102,472 bytes in review/autofix. Run 36191760929 alone made eight overflow queries/30,366 bytes. Log source bytes that would otherwise enter the prompt alongside selected chunks and bytes actually injected. **Savings:** unquantifiable until paired measurements show whether Semble replaces expansion or adds low-value context. **Quality risk: medium** for changing selection; logging is low-risk.
4. **Do not downgrade models or reasoning on configuration alone.** Review logs show `REVIEW_CONSOLIDATOR_REASONING: xhigh`, and run 36173134609 switched its final editor attempt from capacity-limited `openai/gpt-6-sol` to `openai/gpt-5.6-sol`. Record actual model, reasoning level, calls, tokens, latency, and accepted outcome by phase; trial cheaper settings only on a bounded, quality-checked cohort. **Dollar estimate: unavailable. Quality risk: high** without outcome comparison. Serena recorded zero real queries and zero response bytes, so there is no evidence yet that it replaces downstream tool/model work—or adds noisy responses.

## Reliability Improvements

1. **Resolve CI’s repeated contract failure.** Five of seven failed CI runs ended at the identical thread-reuse assertion; run 36164629176 instead failed the shared-helper guard, and run 36200909816 failed Ruff with four reported errors. Add a fast, named preflight result (`check`, expected contract, observed contract, source revision) and run Ruff before lengthy tests. **Expected impact:** removal of a five-run repeat failure class once the underlying contract is fixed. **Rollback:** retain the original full checks until preflight parity is demonstrated.
2. **Keep support staging fail-closed, diagnose it earlier.** Runs 36176625636 and 36196360602 refused to mix incomplete security-sensitive support snapshots. Log the verified support identity, missing required filenames, and time spent before staging; guard downstream steps when staging did not establish their variables—the same logs subsequently show `EDITOR_SUMMARY_FILE: unbound variable`. **Expected impact:** fewer secondary errors and faster repair, not permission to use an unverified bundle. **Rollback:** keep the current refusal intact.
3. **Surface blocked reviews as blocked work.** Review-gate runs 36203700569 and 36203694112 succeeded as workflows while PRs 4443 and 4450 were dirty and had reached `workflow_failure` fingerprint cap 3/3. Emit one deduplicated PR/head-scoped terminal event with merge state, cap reason, and handoff owner; suppress redundant sweeps on that unchanged head while preserving a fresh-head recheck. **Expected impact:** fewer no-progress dispatches and shorter time to intervention. **Rollback:** revert suppression, not the cap.
4. **Classify validation failures at their origin.** Validate run 36165111080 failed `Enforce validation outcome` after template renderer exit 14 produced `harness_error`; its summary listed possible causes rather than a confirmed cause. Capture a bounded, redacted renderer stderr excerpt and dependency/config check result at the renderer step. **Expected impact:** fewer diagnostic reruns; one observed run, so rate impact is unknown. **Rollback:** retain the current harness-error status.
5. **Do not mistake synthetic MCP fallbacks for an outage.** CI runs 36168535223, 36179284365, and 36185244321 emitted four `SEMBLE_FALLBACK target=overflow` events each for a deliberately missing executable with `context=contract-test`: **12 test fallbacks, zero recorded runtime fallbacks**. Keep test and runtime counts separate. Serena has zero query, fallback, and probe events; that is an availability-coverage gap, not proof of a broken rollout. Observed `BREAK_GLASS` and `CONTEXT_BUDGET_WARN` counts are both zero, with no measured policy-pressure or prompt-window incident in covered logs.

## AI Memory Health

Seven deep-dive `AI_MEMORY_TELEMETRY` retrieves, from two implement and five review runs, all selected records: **100% observed hit rate**, zero empty retrieves. Average estimated retrieval was **1,451 tokens versus a 1,457-token average budget** (about 99.6%); methods were `llm` five, `plain` two, `none` zero. That near-full budget warrants relevance sampling before increasing it, not an automatic cut.

Other observed operations include 22 `record-run-event`, eight `record-candidate`, two `finalize-task`, and two each of `processed-command-claim` and `processed-command-complete`; `promote` and `compact` were not observed. No retrieval was marked disabled or fail-open. **Eleven force-tick operations were fail-open failures**—six gets and five puts—across validate run 36165111080 and implement runs 36161573118/36191760929; validate logged a memory-clone failure. Two of 22 recorded run events needed two push attempts; none observed needed more. Add operation-scoped timing and a short reason category to these events, keeping failure detail redacted and fail-open behavior unchanged.

## GH API Call Audit

- **Observed write hotspot:** poll run 36203394070, `poll / Process each tracking issue`, attempted `pulls/4450/update-branch` and `pulls/4443/update-branch`; both returned non-retryable HTTP 422 merge conflicts. These are **two observed calls**, not a measured total API budget. Use freshly prefetched merge state to avoid an update known to be dirty, while preserving the conflict-heal path and rechecking unknown state. **Conditional reduction:** up to two futile writes in a comparable poll; no retry or rate-limit reduction is demonstrated for this run.
- **Read-path hygiene:** `.github/workflows/orchestrate_poll.yml` uses one configured active-issue list operation; `scripts/orchestrate_poll_process.sh` already contains batched GraphQL candidate/linked-PR helpers and cycle-local caches. This follows `CLAUDE.md` §15’s prohibition on per-item loop lookups. **No unbatched read pattern is confirmed by these logs.** Do not replace the two distinct PR writes with a purported batch-read optimization.
- **Diagnostic addition:** emit one redacted `GH_API` aggregate per job/step: endpoint *template*, read/write, attempts, status class, elapsed milliseconds, cache hit/miss, batch item count, and rate-limit event count. Instrument `scripts/gh_helpers.sh` and the existing batch helpers without logging request bodies or credentials. This will expose missed reuse and quantify any later call-count reduction. No rate-limit event was found in inspected deep dives; full-window rate-limit incidence is **unknown**.

## Prompt Cache & Memory System

The assembled review/autofix telemetry reports **83.36% `cache_hit_rate`**, 73.79 million cache-read tokens, zero reported cache-write tokens, and usage on all 88 OpenRouter calls. Zero *reported* writes should not be interpreted as zero provider-side cache creation. Hit rates vary markedly—from 47.73% in run 36179301062 to 91.22% in run 36203196657. Stable-prefix hashes and a per-call prompt composition breakdown would distinguish fragmentation from intentionally different work; dynamic PR state should follow invariant instructions when semantics permit.

Retrieval is effective in the seven measured deep dives but almost fills its 1,400–1,600-token caps. Log selected-record IDs as non-sensitive hashes, estimated tokens, budget, and whether each selection was used; keep the current caps pending relevance evidence. No `CONTEXT_BUDGET_WARN` was collected, so prompt-growth pressure is **not observed**, not ruled out across uncollected runs. Preserve memory fail-open on force-tick clone failures and count it separately from successful retrieval.

## Orchestrator Health

All 47 `orchestrate_poll` runs succeeded as workflows, yet their p50/p95 durations were 343/839 seconds. Run 36203394070 processed tracking issues, encountered two dirty-PR update conflicts, and finished successfully; review gates later recorded capped, dirty PRs. Track `issue`, `wave`, previous/next state, deferral reason, conflict-heal attempt, unchanged-head age, and terminal blocked reason in one transition event. This should reveal whether success means progression or merely another poll.

Clarify, plan, implement, and clarify-respond families have 158–159 *other/skipped* outcomes each; these counts do **not** establish clarification loops or stuck waves because skipped runs have no step logs. Add an upstream dispatch/skip-reason event keyed to tracking issue and desired phase. Compare wave dwell time and unchanged-head polls before changing schedules or retry limits.

## Pipeline Flow Bottlenecks

| Flow segment | Evidence | Bottleneck and next action |
|---|---|---|
| Queue | Collector `run_started_at` equals `created_at` for all 1,000 rows, while the summary for cancel-on-close run 36202565954 reports about 21 seconds waiting for a runner. | Queue time is not reliably separated. Collect job queued/start timestamps in the existing run fetch; no queue optimization is justified yet. |
| Clarify → plan | Family p50s are 1 second because most runs skip; active plan run 36201582805 took 537 seconds. | Emit dispatch/skip reason and active-only phase timings before tuning models. |
| Implement | Runs 36161573118 and 36191760929 took 2,117/2,118 seconds. | Separate model, memory-write, and validation time; reuse Semble context only when selected bytes prove useful. |
| Review/autofix | Runs 36172003252/36179301062 took 4,237/3,588 seconds; run 36203196657 reported lost editor changes. | Prioritize ledger-timeout and change-preservation diagnostics on the model critical path. |
| Validate and CI | Validate run 36165111080 had renderer exit 14; five CI runs reached the same assertion after 558–630 seconds. | Move cheap contracts forward and log the renderer’s concrete failure category. |
| Poll/merge | Poll p50 343 seconds; PRs 4443/4450 were dirty and capped. | Track state progression separately from successful execution; avoid known-futile update attempts. |

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks:** long review/autofix model paths (up to 4,237 seconds), successful CI runs over 2,300 seconds, and poll memory/event work. **Top failures:** repeated CI thread-reuse contract assertion (five runs), incomplete trusted support staging (two), and one validation renderer harness error. **Highest measured cost drivers:** review/autofix’s 89.36 million OpenRouter usage tokens across 88 calls, plus implement’s 5.31 million separately reported Codex tokens.

**Prioritized actions:** (1) put the failing CI contract and Ruff ahead of long suites and repair the contract; (2) add review outcome, ledger-timeout, and editor-change-preservation diagnostics without weakening safety gates; (3) time memory and GH API operations in poll, then optimize only measured repeated work.

## Metrics Appendix

**Window and coverage.** GitHub Actions collector snapshot generated September 26, 2026, 00:38 UTC; one repository, 1,000 runs. The assembled analysis context includes cost telemetry for **120 runs**, whereas the supplied full-log collector directory has **23 parsed deep-dive runs**. Totals below use the assembled context unless labeled *deep dive*; do not combine these denominators. Twelve recent skipped runs have empty log archives, and 965 runs were not selected for full-log download.

| Runs | Success | Failure | Skipped | Cancelled | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|
| All families | 304/1,000 (30.4%) | 10/1,000 (1.0%) | 685 | 1 | 2 / 421 seconds |
| CI | 3/10 | 7/10 (70%) | 0 | 0 | 620 / 2,614.6 seconds |
| Review/autofix | 163/167 | 2/167 (1.2%) | 1 other | 1 | 13 / 1,910.1 seconds |
| Orchestrate poll | 47/47 | 0 | 0 | 0 | 343 / 839.2 seconds |
| Validate | 0/1 | 1/1 | 0 | 0 | 298 / 298 seconds |

| Cost and timing measure | Assembled value |
|---|---:|
| Codex calls / tokens used | 60 / 5,310,845 |
| OpenRouter calls; usage available / unavailable | 88; 88 / 0 |
| OpenRouter prompt / completion / reported total tokens | 14,730,425 / 845,116 / 89,357,733 |
| OpenRouter cache read / reported cache write tokens | 73,786,507 / 0 |
| `cache_hit_rate` | 83.36% |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 8,000 / 3,433,760; 117 samples |
| `break_glass_count` / `context_budget_warn_count` | 0 / 0 |

| MCP target or server | Queries | Logged bytes | Fallbacks | Probe ok / failed / skipped | Interpretation |
|---|---:|---:|---:|---:|---|
| Semble `overflow` | 25 | 121,690 query bytes | 12 **CI contract-test only** | Not emitted | Runtime fallbacks: 0; test fallbacks occurred in three CI runs, four each. |
| Semble `reviewer-context` | 7 | 102,472 query bytes | 0 | Not emitted | Net prompt reduction unmeasured. |
| **Semble total** | **32** | **224,162** | **12 synthetic / 0 runtime** | Not emitted | Query bytes are logged output, not token savings. |
| Serena, target unobserved | 0 | 0 response bytes | 0 | **0 / 0 / 0** | No query, tool-call, or availability-probe evidence; per-tool breakdown empty. |
| Other MCP servers observed | 0 | 0 | 0 | None observed | No unknown server prefix found in inspected deep dives. |

**GH API call summary:** two visible `update-branch` PUT failures (HTTP 422) in poll run 36203394070; total calls, cache-miss calls, and full-window rate-limit counts were not supplied. **Next collection step:** add per-step API aggregates and job queue timestamps to the existing telemetry path, and reconcile the 120-run assembled versus 23-run full-log coverage in future reports.
