## Executive Summary

- **Restore the static reviewer prompt file before model invocation.** In four `shubhodeep1/coding-workflows` review/autofix runs—36072616228, 36076221005, 36076920721, and 36077126289—`Run reviewer models` ended with `cat: ./pre_assembled_static.txt: No such file or directory`, after a successful Semble query. Together, those failed runs occupied **1,738 runner-seconds**. Preserve the file outside the mutable checkout and check it immediately before use. **Impact:** prevents this observed failure class and its rerun cost; **confidence: high**.
- **Bound repeated memory-substate waits.** The deep-dive logs contain **1,154** `ledger_emit_substate` warnings for 120-second `record-run-event` timeouts across **16** review runs; run 36052022235 alone has 88. Calls can overlap, so multiplying warnings by 120 seconds would overstate savings. Shorten the fail-open timeout and suppress repeated attempts briefly after a timeout. **Impact:** up to 110 seconds less waiting *per affected synchronous attempt* if reduced to 10 seconds; **confidence: high** for the waits, **medium** for end-to-end savings.
- **Fix and front-load the CI prompt contract.** All **11/11 CI runs failed**; 10 failed at `Review/judge/conflict strict-render contract test`. Logs for 36058812894, 36068219202, and 36069685528 confirm an assembled-versus-legacy prompt byte assertion. Report the first mismatch and run this test earlier. **Impact:** removes a demonstrated merge blocker once corrected and exposes regressions earlier; **confidence: high** for the three inspected assertions, **medium** for the other seven same-step failures.
- **Separate reviewer failure classes in diagnostics.** Four inspected runs produced **24 reviewer-slot failures with exit 226** and “no text events”; other runs show a rejected combination of `reasoning.effort` and `reasoning.max_tokens`, or editor-manifest validation failures. Emit a bounded, credential-free failure category per attempt rather than treating these as one provider problem. **Impact:** fewer blind reruns and faster routing to the correct fix; **confidence: high**.
- **Do not read the overall 29.7% success share as task success rate.** Of 1,000 runs, **654 were skipped**. Review/autofix is the expensive tail: 171 runs, 31 failures, p95 **5,232 seconds**. Log skip and terminal reasons with a PR/issue correlation key before changing dispatch policy. **Impact:** more reliable throughput measurement; **confidence: high**.

## Speed Optimizations

1. **Critical path — stop repeated substate timeouts.** Run 36058839944 logged 78 timeout warnings; 36052022235 logged 88. `scripts/ledger_emit_substate.sh` bounds each memory write at 120 seconds and fails open, but a timed-out attempt leaves its dedupe key available for another attempt. Set a shorter tested `LEDGER_EMIT_TIMEOUT_SECONDS` (initially 10–15 seconds), add a per-run cooldown after failure, and log `attempts_suppressed` and cumulative wait milliseconds. **Estimated saving:** up to roughly 105–110 seconds per affected synchronous attempt; overlapping calls make run-level savings unknown. **Risk:** low to medium—substate freshness may fall, so retain final run-event writes and compare coverage.
2. **Critical path — prevent missing-file review attempts.** The four static-file failures lasted **314, 338, 352, and 734 seconds**. The file is assembled before preflight, while `scripts/review_run_reviewers.sh` later reads `./pre_assembled_static.txt`; its disappearance between checks is a **lifecycle/path inference**, not a proven single cleanup command. Put the generated file in the existing runtime directory, pass an absolute path, and recheck presence/size after the merge-topology gate. Log only path class, byte count, and checksum—not contents. **Estimated saving:** avoids up to the **1,738 observed failed runner-seconds** if these attempts need not recur. **Risk:** low with a compatibility fallback to the current path.
3. **Critical path — fail CI earlier.** CI p50 is **1,046 seconds**; run 36069685528 reached the prompt assertion near the end of its 901-second run. Move the strict-render/assembly contract ahead of the longer lint suite, and print the template name, first differing byte offset, lengths, and hashes on failure. Correct the template or golden text only after reviewing the semantic difference. **Estimated saving:** potentially minutes per failing CI run; exact amount requires step-duration collection. **Risk:** low; test semantics stay unchanged.
4. **Smaller opportunity — measure poller waits before tuning cadence.** `orchestrate_poll` ran 44 times with p50 **349.5 seconds**; run 36073795815’s `poll` step occupied its 331-second run. Emit per-tick API wait, model compute, sleep, and state-write milliseconds before reducing polling or parallelizing it. **Estimated saving:** unquantified; **risk:** low for logging, higher for cadence changes. By comparison, 62 observed Semble queries took about **34 seconds total** across deep-dive logs, so optimizing those first would be a micro-optimization.

## Cost Optimizations

1. **Avoid failed full review/editor passes before changing models.** Review run 36052022235 failed after **5,981 seconds** with **12,326,437 reported OpenRouter total tokens**; run 36065459446 failed in `Apply fixes with editor model` after **4,037 seconds**, following manifest-validation trouble and a workspace-guard rejection. Fix the deterministic file and format failures, and classify empty reviewer output before any same-head retry. **Estimated saving:** the token and runner spend of prevented attempts; not a defensible dollar estimate without prices and complete usage. **Quality risk:** low if failed reviews still never count as approval.
2. **Keep model/reasoning changes evidence-gated.** Review logs show six reviewer slots, a primary `openai/gpt-6-sol` editor and, in run 36063391601, a final-attempt `openai/gpt-5.6-sol` fallback. Run 36076664580’s security audit used `openai/gpt-6-sol` at `xhigh` and took 427 seconds, but has no token breakdown. First log role/model/reasoning, outcome, elapsed time and available usage per attempt; trial lower reasoning only on a defined retry class with quality comparison. **Estimated saving:** unknown until measured. **Quality risk:** material if reasoning is reduced globally.
3. **Measure prompt expansion and cache fragmentation.** Wider telemetry reports **45,511,239 prompt**, **147,276,416 cache-read**, and **2,573,173 completion tokens** across 281 logged OpenRouter calls; 35 calls lack usage. Aggregate `cache_hit_rate` is **null**, not zero. Individual runs range from **0.464443** (36058813471) to **0.776629** (36075609726). Preserve the existing static-prefix-first assembly, log a prefix hash/byte length and dynamic-section sizes, and move run-specific noise after the reusable prefix. **Estimated saving:** unknown; compare measured uncached-input tokens and latency before/after. **Quality risk:** low if ordering alone changes.
4. **Bound Semble overflow context rather than disabling Semble.** Wider telemetry records **89 `SEMBLE_QUERY` calls and 912,298 logged bytes**. In the deep-dive logs, 41 `target=overflow` calls returned **335,641 bytes**; run 36063391601 made five overflow queries after a reviewer-context query. Deduplicate repeated chunks and fetch overflow only for relevant changed files or explicit need, retaining a fail-open path. **Estimated saving:** at most the removed response bytes and associated prompt expansion; bytes are *not* measured billable tokens. Semble may be replacing larger context, but no baseline proves that here. **Quality risk:** medium if useful evidence is elided; compare review findings.
5. **Serena is not an established saving or cost in this window.** There are zero logged Serena queries, response bytes, tool calls, fallbacks, or probes; a recent review summary reports `SERENA_ENABLED: false` (36077107599). Do not claim it replaced downstream work or produced noisy bytes. **Estimated saving:** none evidenced; **quality risk:** none from leaving it unchanged.

## Reliability Improvements

1. **Missing static file — deterministic input failure.** The four cited `Run reviewer models` failures reached Semble successfully, then failed reading `./pre_assembled_static.txt`. Revalidate after checkout/merge cleanup and fail with a distinct `review_input_missing` event before retrieval or model work; retain the existing preflight. **Expected impact:** eliminate this confirmed four-run failure class. **Rollback:** restore the relative-path fallback, never synthesize an empty prompt.
2. **Memory-substate timeout storm — telemetry dependency.** The **1,154** warnings are failed substate emissions, not 1,154 failed workflow runs. Add short timeout/cooldown telemetry with `phase`, `elapsed_ms`, `reason=timeout`, and suppression count; investigate memory-store write latency separately. **Expected impact:** fewer repeated waits and warnings. **Fail-open:** keep review execution independent of substate writes.
3. **CI contract drift — validation failure.** Three inspected CI logs fail `tests/test_assemble_prompt.py` at `test_assembled_templates_match_legacy_prompt_bytes`; 10 CI runs point to that step. Surface a safe first-difference diagnostic, then repair the mismatch without weakening byte equality. **Expected impact:** restore this CI gate once the regression is corrected. **Rollback:** revert the template change, not the assertion.
4. **Reviewer/provider and editor failures — distinct categories.** Runs 36055775446, 36058701173, 36064965623, and 36067920051 each have six exit-226 reviewer failures with no text events. Run 36065459446 also records the incompatible pair `reasoning.effort`/`reasoning.max_tokens`, an editor-manifest failure, and rejected generated `tests/__pycache__` files. Ensure model configuration supplies only one reasoning control; prevent bytecode/cache artifacts in the guarded worktree; emit separate `empty_structured_output`, `provider_request_invalid`, `manifest_invalid`, and `workspace_guard_rejected` counters. **Expected impact:** fewer avoidable retry loops; **rollback:** retain strict manifest and workspace guards—never treat missing reviewer output as clean.
5. **Incomplete failure attribution.** Several review failures have no job/step and zero-second duration, including 36050108797; workflow-validation annotations are unavailable in the collector payload. Fetch annotations **only for startup failures**, and log the dispatch/ref validation outcome. A successful run, 36076968467, also warns that no standalone validation workflow could be dispatched for merged PR #4408: log attempted workflow names and a bounded reason, and keep validation status explicitly unknown rather than silently passing. **Expected impact:** faster diagnosis; no change to merge gates.

There were **zero observed `BREAK_GLASS` events** and **one `CONTEXT_BUDGET_WARN`**, in failed review run 36052022235: **202,624 prompt tokens against a 200,000-token window** on a fallback attempt. This points to prompt-size risk, not demonstrated rubric-pressure break-glass use. Emit assembled-size and truncation decisions before each attempt and enforce the existing budget without dropping required instructions.

Semble’s **12 fallbacks** are four apiece in CI contract-test runs 36058812894, 36068219202, and 36069685528 (`target=overflow`, `context=contract-test`); **runtime fallbacks are zero**. Do not diagnose a broken runtime rollout from these fixtures. Separate probe availability from runtime fallback in new events: poller summaries for 36074523151 and 36075193900 report Semble enabled but unavailable, without a counted probe result.

## AI Memory Health

Across **26 downloaded deep-dive logs**, there are **22 `AI_MEMORY_TELEMETRY` retrieves: 22/22 hits (100%)**, averaging **1,397 estimated tokens against a 1,400-token budget**; all 22 use `keyword_method=llm`. No sampled retrieve selected zero records or reported `enabled=false` or `fail_open=true`. The logs also contain 44 `record-run-event` and 13 `record-candidate` telemetry entries; run 36076968424’s unselected-run summary confirms a successful `finalize-task` for issue #4407. No `promote`, `compact`, or processed-command telemetry was established from this sample.

Retrieval is effective but leaves about **3 tokens of average headroom**. Log records considered, selected, truncated, and budget-limited by role; inspect relevance before increasing the budget. Among observed operations with `push_attempts`, **10 took two attempts and two took three**. Record retry cause and latency, and correlate them with the separate substate-timeout storm; successful retrieval does not establish healthy writes.

## GH API Call Audit

**Endpoint request counts, pagination totals, retry counts, and GitHub 403/429 counts are not present.** No high-volume endpoint can therefore be *measured* from this window; OpenRouter rate-limit classifications must not be counted as GitHub rate limits.

- **Check-run polling is the first candidate to instrument.** `review_autofix`’s `Collect PR check-run failures` uses one logical paginated snapshot per poll iteration, potentially multiple HTTP pages/retries; its documented wait budget is 300 seconds. Log `endpoint=check-runs`, head-key hash, iterations, pages, retries, unchanged snapshots, elapsed milliseconds, and terminal reason. Reuse the last confirmed same-head snapshot where safe; **estimated reduction:** one or more requests per redundant snapshot, conditional on measured duplication. Preserve the current `api_error` sentinel so unknown does not become “passing.”
- **Preserve existing batching.** The repository’s API hygiene rules prefer `_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`, and cycle-local caches; the poller also shares an actions-runs cache. Emit batched item counts, cache hits/misses, fallback reasons, and underlying request counts before proposing per-item rewrites. Logs for 36065459446 and 36067920051 show linked-issue *body-text* fallback when GraphQL closing references were empty; that line alone does **not** prove an extra API call.
- **Measure dispatch and reconciliation, not just requests.** Sweep run 36075594994 examined eight candidates, dispatched five and skipped three active peers. Log endpoint-template request totals and correlation IDs alongside `AUTOFIX_PEER_CHECK`/dispatch outcomes; retain the three skips. Close-cleanup run 36075407860 preserved three active runs without PR linkage: log why linkage was unavailable before considering any more aggressive cancellation. **Estimated call/rate-limit reduction:** unquantifiable until request counters exist; no new service is needed.

## Prompt Cache & Memory System

Cache-read tokens are substantial, while reported cache-write tokens are **0** and overall `cache_hit_rate` is **unavailable**. Zero reported writes does not establish that no provider-side cache was created. The differing per-run hit rates above warrant a prefix-hash comparison, not an assumption of fragmentation. Log cache metadata accepted/rejected, breakpoint fallback, prefix hash, prefix bytes, and uncached dynamic bytes per model call; keep prompts and credentials out of logs. Expected effect is measurable lower uncached-input tokens and latency, with no intended reliability change.

Run 36052022235’s context warning and the **1,397/1,400-token** average memory retrieve show little growth room. Cap optional overflow before the provider window, place dynamic PR text after the stable prefix, and report what was elided. Neither a prompt-cache fail-open event nor a cache-metadata rejection was established in these deep dives; add counters to verify both behaviors.

## Orchestrator Health

`clarify`, `plan`, and `implement` have **152/160**, **145/152**, and **146/152** other/skipped outcomes respectively; recent runs such as 36077469185, 36077469351, and 36077469155 skipped within the same fan-out. This is **not evidence that all three phases are stuck**: their gate reasons are absent. Add `skip_reason`, project/issue correlation, wave, and next eligible phase to each gate outcome.

The poller completed **44/44** runs, but its p50 is **349.5 seconds**. Run 36073914917 found PRs #4354 and #4415 still queued in the merge-train release scan. Track wave entry/exit, deferral reason/count, judge decision, validation-dispatch outcome, and oldest pending PR age; avoid changing merge ordering without that evidence. Review run 36075618027 shows an autofix fingerprint cap for PR #4348 at **3/3** repeated `reviewers_failed` outcomes. The cap contains repetition but does not resolve the PR: log a terminal handoff and alert status, not another automatic identical-head pass. Conflict-heal retry and clarification-loop counts were not supplied.

## Pipeline Flow Bottlenecks

| Segment | Evidence | Bottleneck type and next measurement |
|---|---|---|
| Clarify → plan → implement | Mostly skipped fan-out; successful implement outliers include runs 36045021044 (**1,038 s**) and 36016290312 (**1,037 s**). | Gate versus compute unknown; emit skip reasons and active-step timings. |
| Review/autofix | p50 **337 s**, p95 **5,232 s**; run 36052022235 **5,981 s**. | Dominant compute/retry tail plus substate waits; fix deterministic failures and timeout repetition first. |
| CI and validate | CI **11/11 failed**, p50 **1,046 s**; merged PR #4408 had a validation-dispatch warning in run 36076968467. | Validation blocker and unknown dispatch result; front-load contract test and log dispatch outcome. |
| Orchestrate/merge | Poll p50 **349.5 s**; PRs #4354/#4415 remained queued in run 36073914917. | Poll compute versus sleep and merge deferral unresolved; emit component timings and queue age. |

The collector supplies no usable **job-level queue latency**: its run-level `run_started_at` equals `created_at` across the 1,000 rows. Do not conclude queueing is zero; collect first-job-start and dispatch-to-start timestamps.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottleneck:** review/autofix’s long tail and repeated memory-substate waits. **Top failure modes:** missing reviewer static file; strict-render CI mismatch; empty reviewer output and editor-manifest/workspace-guard failures. **Highest measured cost driver:** review/autofix accounts for all **195,346,505** reported OpenRouter total tokens in the wider assembled telemetry, whose coverage is partial.

**Top three actions, in order:** (1) preserve and recheck the static reviewer file after mutable-worktree operations; (2) shorten and circuit-break fail-open substate writes while counting suppressed events; (3) diagnose and fix the prompt-byte contract, then move that test earlier. Keep reviewer and workspace safety gates intact.

## Metrics Appendix

**Window:** September 24, 2026 14:50:43 UTC–September 25, 2026 00:25:31 UTC. One repository; GitHub Actions API source.

| Workflow family | Runs | Success | Failure | Cancelled | Other | Duration p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| All | 1,000 | 297 | 42 (**4.2% of all**) | 6 | 655, including 654 skipped | 2 / 976 s |
| Review/autofix | 171 | 134 | 31 (**18.1% of family**) | 5 | 1 | 337 / 5,232 s |
| CI | 11 | 0 | 11 | 0 | 0 | 1,046 / 1,090.5 s |
| Orchestrate poll | 44 | 44 | 0 | 0 | 0 | 349.5 / 399.25 s |
| Clarify / plan / implement | 160 / 152 / 152 | 8 / 7 / 6 | 0 / 0 / 0 | 0 / 0 / 0 | 152 / 145 / 146 | 1 / 25.15; 1 / 11; 1 / 11 s |

| Cost and diagnostic coverage | Wider assembled telemetry | Downloaded-log collector |
|---|---:|---:|
| Runs with log telemetry | 125 | 26; includes 9 successes, 16 failures, 1 cancellation |
| OpenRouter calls; usage available / unavailable | 281; 246 / 35 | 203; 168 / 35 |
| Prompt / completion / reported total tokens | 45,511,239 / 2,573,173 / **195,346,505** | 33,421,175 / 1,645,355 / 141,619,615 |
| Cache read / write tokens; aggregate `cache_hit_rate` | 147,276,416 / 0; **null** | 106,563,872 / 0; **null** |
| `wall_clock_p50_ms` / `wall_clock_p99_ms` | 9,000 / 6,046,520; **123 samples** | 4,071,000 / 6,482,750; **26 biased deep-dive samples** |
| `break_glass_count` / `context_budget_warn_count` | 0 / 1 | 0 / 1 |
| Codex calls / tokens | 0 / 0 reported; **not** a measure of all model work | 0 / 0 reported |

The wider totals and deep-dive totals have **different coverage**; do not add them together or interpret the deep-dive wall-clock percentile as a population percentile. In particular, 35 OpenRouter calls lack usage in both views.

| MCP and API signal | Wider total | Target/availability detail from deep dives |
|---|---:|---|
| Semble queries / logged bytes | **89 / 912,298** | 62 directly located: `reviewer-context` **21 / 308,704 bytes**; `overflow` **41 / 335,641 bytes**. Remaining wider calls are not assigned a target here. |
| Semble fallbacks | **12** | `overflow` contract-test fixtures **12**; runtime **0**. |
| Semble probe `probe_ok` / `probe_failed` / `probe_skipped` | Not reported | No counted probe events; two poll summaries report unavailable state, not a probe result. |
| Serena queries / response bytes / tool calls / fallbacks | **0 / 0 / 0 / 0** | No per-tool breakdown exists; recent configuration reports disabled. |
| Serena probe `probe_ok` / `probe_failed` / `probe_skipped` | **0 / 0 / 0** | No target-level availability event; absence is not a successful probe. |
| GH endpoint calls / pages / retries / 403–429 | **Not collected** | Sweep 36075594994 reports **5 dispatches and 3 active-peer skips**, not underlying request counts. |
| Other MCP servers observed | **None established** | No other `<NAME>_QUERY`, `_FALLBACK`, or `_PROBE` events found in the inspected logs. |

The next collection should retain per-step durations and job start times; emit bounded, structured failure categories, API request/page/retry counters, and explicit MCP probe outcomes. That would make the savings estimates testable without adding infrastructure or changing review safety behavior.
