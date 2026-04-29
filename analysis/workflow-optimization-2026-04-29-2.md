## Executive Summary

- **Highest-impact blocker: deterministic MCP/OpenRouter failure is burning full implement runs before failing.** In `shubhodeep1/coding-workflows`, failed implement runs `25076992830` (4,984s), `25057072163` (4,053s), `25055428237` (3,818s), and `25069841009` (3,559s) all fail in `implement / implement` with repeated Azure 400s: `Invalid input: expected "function"... received undefined`, tied in the issue context to a failed MCP handshake. **Estimated impact:** save ~25–80 minutes per affected issue and materially cut reruns. **Confidence:** high.

- **The release-gate path is currently unhealthy and dominated by long-running subtests.** `test_and_mark_stable` has **3 total runs, 0 successes, 2 failures, 1 cancelled**, average duration **4,414s**. Run `25088532565` failed after **5,967s** when `orchestrate-decompose-test` produced only **1 child issue instead of >=2**; `orphan-workflows-test` also failed because `workflow-log-analysis` run `25088541089` was **cancelled** after a long `deep-audit`. **Estimated impact:** recover the stable-release signal and remove ~1–1.5 hours of wasted gate time per failed release run. **Confidence:** high.

- **A large fraction of workflow traffic is no-op fan-out, which is adding queue pressure and orchestration noise.** Overall repo sample: **1,000 runs**, but **732** are `other`; in recent runs, clarify/plan/implement/orchestrate-clarify-respond are frequently dispatched together and all finish as **skipped in 0–2s**. Workflow medians confirm this: `clarify` p50 **1s**, `plan` p50 **1s**, `implement` p50 **1s**, `orchestrate_clarify_respond` p50 **1s**. **Estimated impact:** large reduction in scheduler pressure and incidental GH/API overhead; indirect latency win on real work. **Confidence:** high.

- **Review/autofix is expensive and cancellation-heavy.** `review_autofix` has **75 runs**, **38 cancelled** (50.7%), p95 **1,569s**; cancelled runs include `25084671803` (499s), `25086210798` (459s), and `25089879069` (491s). A recent review log also states the newer `CHECK_RUNS_WAIT_TIMEOUT_SECS=1200` increases worst-case check-run polling from **31 -> 61 API requests** per iteration. **Estimated impact:** save 5–25 minutes and substantial API churn per stale/cancelled review cycle. **Confidence:** medium-high.

- **AI memory telemetry is healthy; prompt-cache telemetry is not visible enough.** Across sampled logs, `AI_MEMORY_TELEMETRY` shows **65 retrieves**, **100% hit rate**, average `estimated_tokens` **46.15**, `keyword_method` split **plain 55 / llm 10**, and **0** zero-hit retrieves, **0** `fail_open: true`, **0** `enabled: false`, **0** push retries >1. By contrast, no concrete prompt-cache create/read counters were present despite `OPENROUTER_PROMPT_CACHE_DISABLED=false` in multiple jobs. **Estimated impact:** memory system is not the current bottleneck; prompt-cache observability is. **Confidence:** high.

- **Serena/tooling instructions and runtime capabilities are out of sync in at least one path.** Implement run `25057072163` repeatedly emitted `ERROR codex_core::tools::router: error=unsupported call: activate_project` before proceeding, indicating the prompt/tool contract is asking for tools the runtime does not expose. **Estimated impact:** reduce tool churn, wasted model turns, and implementation latency. **Confidence:** high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Fail open before Codex starts when any MCP server handshake is broken
- **Evidence:** Implement failures `25076992830` (4,984s), `25057072163` (4,053s), `25055428237` (3,818s), `25069841009` (3,559s) all fail in `Run Codex implementation`. The logs repeatedly surface Azure 400s on `tools[7]`, and run `25076992830` explicitly ties the failure to an MCP initialize failure (`context7`) producing an invalid tool entry.
- **Root cause:** Deterministic preflight defect; failed MCP registrations are still reaching the model payload.
- **Exact change:** In `setup_serena.sh` / Codex config generation, probe each MCP server first and only write `[mcp_servers.<name>]` blocks for servers that complete initialize successfully. If probe fails, log and continue with remaining tools. Keep the current version pinning as defense-in-depth.
- **Estimated time savings:** **25–80 minutes per affected issue** by avoiding full failed implement attempts.
- **Implementation risk:** **Low-medium.** Safe if implemented as fail-open and scoped to non-required MCP servers.
- **Critical-path?** **Yes.** This is directly on the implement critical path.

### 2. Fix decompose-test shape validation before the release gate waits ~6,000s to fail
- **Evidence:** `test_and_mark_stable` run `25088532565` ran **5,967s** and failed because `orchestrate-decompose-test` observed only **1 child issue** where **>=2** were expected. The failing step watched run `25088539295` for ~5m45s before erroring.
- **Root cause:** Orchestrator decomposition quality/contract enforcement issue.
- **Exact change:** Add a post-decompose validator immediately after orchestrator output is generated: assert minimum child count and dependency-edge shape before dispatching downstream work. If invalid, retry decomposition once with a stricter schema reminder; otherwise fail immediately.
- **Estimated time savings:** **45–90 minutes** on bad release-gate runs by failing fast instead of after multi-phase orchestration.
- **Implementation risk:** **Medium.** Needs careful fail-open/one-retry behavior to avoid over-rejecting valid decompositions.
- **Critical-path?** **Yes.** This blocks stable-release qualification.

### 3. Reduce `workflow-log-analysis` deep-audit wall time or separate it from blocking watcher logic
- **Evidence:** `orphan-workflows-test` failed because watched `workflow-log-analysis` run `25088541089` concluded `cancelled`; the issue summary in `issue_pr_status` says `deep-audit` ran **30m16s** and hit `timeout-minutes: 30`. `workflow_log_analysis` family averages **1,976.7s** across 3 runs, with 2 cancelled.
- **Root cause:** Deep-audit compute budget too close to actual execution time; blocking watcher waits on the full heavy analysis path.
- **Exact change:** Keep the already-proposed mitigation path visible in logs—lower deep-audit reasoning from `xhigh` to `high`, keep or raise timeout only after reasoning reduction, and move nonessential audit stages off the blocking release-gate watcher if possible.
- **Estimated time savings:** **10–20 minutes** on the release-gate path; larger reliability gain.
- **Implementation risk:** **Low-medium.** Lowering reasoning is low risk; unblocking the watcher is medium risk.
- **Critical-path?** **Yes** for `test_and_mark_stable`.

### 4. Cap plan-stage stall-recovery loops earlier
- **Evidence:** Slow plan runs `25073268072` (**6,038s**, success) and `25068290028` (**7,243s**, cancelled) are far above family p95 (**200s**). In `25073268072`, the plan run carries auto-stall-recovery context (`planning stalled`, `clarification stalled`) before eventually completing.
- **Root cause:** Recovery loops continue too long before escalating.
- **Exact change:** After the first auto-answer/autorecovery cycle, switch from "continue waiting" to "post bounded failure summary + request human or poller action." Add a hard cap per issue for auto-generated clarification/answer recovery.
- **Estimated time savings:** **30–100 minutes** on pathological plan runs.
- **Implementation risk:** **Medium.** Too aggressive a cap could increase manual intervention.
- **Critical-path?** **Yes.** Plan precedes implement.

### 5. Stop dispatching no-op clarify/plan/implement/respond runs
- **Evidence:** Recent timestamps repeatedly show `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` all dispatched together and immediately skipped. Family medians are **1s** for all four. Repo-wide, `other_count` is **732/1000**.
- **Root cause:** Dispatch happens before phase eligibility is decisively known.
- **Exact change:** Move phase gating up to the caller so downstream reusable workflows are invoked only when their guard condition is already true. Preserve existing in-workflow guards as defense-in-depth.
- **Estimated time savings:** **Small per run, large aggregate**; likely meaningful queue-pressure reduction during bursty orchestration windows.
- **Implementation risk:** **Low.**
- **Critical-path?** **Indirect.** This mostly improves system throughput and queue contention.

### 6. Short-circuit stale review/autofix runs earlier
- **Evidence:** `review_autofix` has **38 cancelled / 75 total** and several cancelled runs lasted **340–499s** before dying. This is wasted wall time with no output.
- **Root cause:** Cancellation/staleness is detected too late in some review loops.
- **Exact change:** Insert fast stale-base / superseded-run checks before long waits (check-run polling, reviewer fan-out, consensus summarization).
- **Estimated time savings:** **5–25 minutes** per cancelled review wave.
- **Implementation risk:** **Low-medium.**
- **Critical-path?** **Yes** on PR completion paths.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

### 1. Remove deterministic implement retries on Azure/MCP 400s
- **Evidence:** Implement logs repeat the same Azure 400 payload (`Invalid input: expected "function"...`) across multiple attempts in runs such as `25076992830`. The issue body itself says affected workflows fail after exhausting retries.
- **Root cause:** Retrying a deterministic request-shape failure instead of classifying it as non-retryable.
- **Exact change:** Treat this exact OpenRouter/Azure validation shape as a non-retryable class after first occurrence; abort remaining Codex attempts and surface a specific remediation hint.
- **Estimated savings:** **Largest single token/dollar win** in sampled data; avoids repeated full prompt submissions and model turns on doomed runs. Exact token totals unavailable in sampled telemetry.
- **Quality-risk notes:** **Low risk** if classification is narrow and exact-match based.

### 2. Temporarily quarantine reviewer models that are known-incompatible with current tool shape
- **Evidence:** The `issue_pr_status` summary references claude-branch-review run `25090174095`, noting **3 of 6 reviewer slugs** (`deepseek/deepseek-v4-pro`, `qwen/qwen3.6-plus`, `x-ai/grok-4.1-fast`) had failed every attempt across both passes for several runs until catalog fixes. The same log notes a catalog change to switch `apply_patch_tool_type` for those slugs.
- **Root cause:** Reviewer panel includes models whose tool contract was incompatible, causing wasted parallel reviewer passes.
- **Exact change:** Until the catalog fix is validated in production, gate those 3 models behind a feature flag or health check; run only proven-good reviewer models on the default path.
- **Estimated savings:** Up to **~50% reviewer-panel token spend** on affected review passes, plus reduced latency.
- **Quality-risk notes:** **Medium.** Some cross-model diversity is lost, but safer than paying for known failures.

### 3. Lower default review check-run wait budget or make it adaptive
- **Evidence:** In recent `review_autofix` run `25091754066`, env shows `CHECK_RUNS_WAIT_TIMEOUT_SECS=1200`. The step summary explicitly says this raises worst-case API requests per iteration from **31 -> 61** and adds up to **10 minutes** wall time.
- **Root cause:** Static long wait budget on every iteration, even when little value is gained.
- **Exact change:** Use a shorter default (e.g. revert to 600s) with adaptive extension only when required check-runs are active and close to terminal. Prefer early exit once the specific blocking checks finish.
- **Estimated savings:** **Meaningful GH API + Actions runtime savings** on every review cycle; token savings indirect.
- **Quality-risk notes:** **Low-medium.** Must preserve enough time for normal CI completion.

### 4. Stabilize prompt prefixes and stop re-inlining identical issue context across retries
- **Evidence:** In implement run `25076992830`, the same long issue body is visible repeatedly around retry windows; the same MCP failure explanation is effectively re-fed multiple times. `OPENROUTER_PROMPT_CACHE_DISABLED=false` is set, but no create/read telemetry is visible.
- **Root cause:** Retry prompts appear to rebuild large dynamic context blocks instead of separating invariant prefix from attempt-local deltas.
- **Exact change:** Build prompts as: stable system prefix + stable issue/problem summary file + compact per-attempt delta appendix. Keep timestamps, transient warnings, and attempt counters out of the cacheable prefix.
- **Estimated savings:** Likely **double-digit % token reduction** on retries; exact savings not measurable from available telemetry.
- **Quality-risk notes:** **Low.** This is mostly formatting and assembly hygiene.

### 5. Reduce unnecessary high-reasoning defaults where no complex reasoning is needed
- **Evidence:** `plan` uses `openai/gpt-5.4` with `MODEL_REASONING_EFFORT=xhigh`; `orchestrate_poll` exposes `MODEL_EDITOR=openai/gpt-5.4` and `MODEL_REASONING_EFFORT_JUDGE=xhigh` even in run `25091149961`, which processed **0 tracking issues**.
- **Root cause:** Broad use of high/xhigh reasoning tiers on lightweight or empty-work cycles.
- **Exact change:** Use conditional model/reasoning selection:
  - empty/no-work poll cycles: skip model work entirely;
  - metadata-only judge paths: medium/high;
  - reserve xhigh for plan generation and ambiguous conflict resolution only.
- **Estimated savings:** **Moderate recurring savings** across orchestrator traffic.
- **Quality-risk notes:** **Medium.** Apply only on clearly low-complexity branches.

### 6. Eliminate no-op workflow fan-out
- **Evidence:** Four phase workflows are frequently spawned only to skip in 0–2s.
- **Root cause:** Over-dispatch.
- **Exact change:** Caller-side pre-gating before workflow dispatch.
- **Estimated savings:** Mostly **Actions minutes and scheduler cost**, not model tokens.
- **Quality-risk notes:** **Low.**

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Make failed MCP servers non-fatal to implement/validate/review jobs
- **Failure evidence:** Implement failures `25076992830`, `25057072163`, `25055428237`, `25069841009`; repeated Azure 400 due to invalid tool-list entry after MCP failure.
- **Root cause category:** External tool integration / request assembly defect.
- **Exact fix:** Handshake-probe all MCPs before registration; omit failed tools from config; classify the Azure 400 as deterministic/non-retryable.
- **Expected reliability impact:** Should remove the dominant sampled implement failure mode.
- **Rollback/fail-open considerations:** Fail open by continuing without the broken MCP; log degraded capability.

### 2. Enforce decompose-output schema before downstream release-gate progression
- **Failure evidence:** `test_and_mark_stable` run `25088532565` failed because decomposition produced **1** child issue when the test expected **>=2**.
- **Root cause category:** Orchestrator decomposition correctness.
- **Exact fix:** Validate `child_count`, `files_touched`, and dependency-edge minimums immediately after decompose. If invalid, one retry with hardened instructions; otherwise stop.
- **Expected reliability impact:** High for the release gate; prevents a whole class of late failures.
- **Rollback/fail-open considerations:** One retry then fail-fast; do not silently continue with malformed decomposition.

### 3. Prevent deep-audit timeout from cancelling the whole watched analysis path
- **Failure evidence:** `orphan-workflows-test` failed because watched `workflow-log-analysis` run `25088541089` concluded `cancelled`; summary says `deep-audit` hit its 30-minute cap.
- **Root cause category:** Timeout budgeting / long-tail compute.
- **Exact fix:** Lower reasoning first, then right-size timeout; ensure downstream `api-redundancy` is not hard-blocked by `deep-audit` when partial analysis is acceptable.
- **Expected reliability impact:** High on `test_and_mark_stable`.
- **Rollback/fail-open considerations:** Prefer partial-report success over all-or-nothing cancellation.

### 4. Protect orchestrate poller from runner starvation and repeated rescheduling
- **Failure evidence:** `orchestrate_poll` failures `25058629488` and `25061570578` both lasted **903s** and never really executed; system logs show repeated `Waiting for a runner to pick up this job...`.
- **Root cause category:** Queueing / scheduling.
- **Exact fix:** Reduce background no-op workflow volume, keep poller concurrency serialized, and if possible prioritize the poller trigger path over no-op phase fan-out.
- **Expected reliability impact:** Moderate-high; fewer missed poll cycles and fewer false failures.
- **Rollback/fail-open considerations:** Keep cron safety net; do not reduce poll frequency until queue pressure drops.

### 5. Add richer failure artifacts for nightly validation self-test
- **Failure evidence:** `nightly_validation_selftest` run `25089252262` failed in `Run validation self-test matrix`, but the sampled excerpt only shows the tail and final `exit code 1`.
- **Root cause category:** Observability gap.
- **Exact fix:** Always emit matrix-row summary, failing case name, and last 100 lines of the failing child step into step summary/artifact.
- **Expected reliability impact:** Medium; faster diagnosis and lower rerun thrash.
- **Rollback/fail-open considerations:** Pure observability change; low risk.

### 6. Stop fallback paths from degenerating into per-issue REST loops
- **Failure evidence:** In recent `issue_pr_status` run `25091754059`, logs say `GraphQL batch failed — fall back to per-issue REST detection`.
- **Root cause category:** API fallback inefficiency / partial degradation.
- **Exact fix:** Add a second batched GraphQL fallback before per-issue REST, or cache the first GraphQL result for reuse through the rest of the step.
- **Expected reliability impact:** Moderate; reduces rate-limit exposure and flakiness under degraded conditions.
- **Rollback/fail-open considerations:** Keep REST as final fail-open path.

## AI Memory Health

Observed from `AI_MEMORY_TELEMETRY:` lines in sampled logs.

- **Telemetry observed:** yes.
- **Total telemetry events:** **360**
- **Operation distribution:**
  - `record-run-event`: **150**
  - `processed-command-check`: **65**
  - `processed-command-claim`: **65**
  - `retrieve`: **65**
  - `processed-command-complete`: **10**
  - `record-candidate`: **5**

### Retrieval effectiveness
- **Retrieve hit rate:** **100%** (`65/65` retrieves had `records_selected > 0`)
- **Average `estimated_tokens`:** **46.15**
- **`keyword_method` distribution:**
  - `plain`: **55**
  - `llm`: **10**
  - `none`: **0**

### Flags
- **Retrieves returning 0 records:** **0**
- **`fail_open: true` entries:** **0**
- **`enabled: false` entries:** **0**
- **Push retry counts >1:** **0**

### Interpretation
- The memory system is behaving well operationally: retrievals are consistently returning useful context, token size is small, and writes are succeeding on first push.
- The mix is heavily weighted toward **ledgering** (`record-run-event`) rather than **learning/promotion**. Only **5** `record-candidate` events were observed, and no `promote`, `finalize-task`, or `compact` events appeared in the sampled logs.
- That suggests memory is currently stronger as an execution ledger than as a long-horizon learning store.

### Recommendation
- Keep the current retrieval settings.
- Add explicit telemetry for why `record-candidate` was or was not emitted on plan/implement/review completion so learning throughput can be improved without guessing.

## GH API Call Audit

### 1. Release-gate watcher loops are high-volume and low-value
- **Evidence:** `orphan-workflows-test` in run `25088532565` polled watched run `25088541089` every ~15s from `2026-04-29T02:53:05Z` to `2026-04-29T03:30:38Z` before failing on `cancelled`—roughly **150 status polls**.
- **Pattern:** repeated status lookups in a long-running loop.
- **Concrete change:** Use staged polling: 15s for first 5 minutes, then 60s thereafter; or switch to `gh run watch`/single watcher helper with backoff.
- **Estimated call-count reduction:** roughly **70–80%** on long watcher paths.
- **Rate-limit risk reduction:** high.

### 2. Review check-run polling budget is too expensive by default
- **Evidence:** Recent `review_autofix` log (`25091754066`) states `CHECK_RUNS_WAIT_TIMEOUT_SECS=1200` raises worst-case requests from **31 -> 61** per iteration.
- **Pattern:** repeated check-run polling for each review cycle.
- **Concrete change:** shorten default timeout, poll only required check names, and stop once the blocking subset reaches terminal state.
- **Estimated call-count reduction:** up to **30 calls per review iteration**.
- **Rate-limit risk reduction:** medium-high.

### 3. Post-merge validate dispatch still has a per-issue REST fallback path
- **Evidence:** In `review_post-merge-validate-dispatch` (`25091754066`), the job first runs a batched GraphQL query for `closingIssuesReferences`, then falls back to `gh api pulls/{n}` body parsing and `gh issue view` when labels are unknown.
- **Pattern:** one good batched query followed by N-item REST fallback.
- **Concrete change:** expand the initial GraphQL query to always include the exact label predicate needed for validation gating, and carry that JSON through the whole step.
- **Estimated call-count reduction:** **N-1** calls for PRs with multiple linked issues.
- **Rate-limit risk reduction:** medium.

### 4. Issue-PR status sync degrades from batch to per-issue REST too quickly
- **Evidence:** `issue_pr_status` run `25091754059` logs `GraphQL batch failed — fall back to per-issue REST detection`.
- **Pattern:** failover from batch to loop instead of second-level batch retry.
- **Concrete change:** add a second batched GraphQL retry with a narrower query or cached payload before dropping to per-item REST.
- **Estimated call-count reduction:** depends on linked issue count; likely material in bulk-close events.
- **Rate-limit risk reduction:** medium.

### 5. Implement logs show API-heavy orchestration around a deterministic model failure
- **Evidence:** Even the doomed implement path still performs issue metadata fetch, comments fetch, label edits, workflow dispatches, and retries before surfacing the same deterministic Azure 400.
- **Pattern:** expensive API choreography before/after a non-retryable failure.
- **Concrete change:** once the exact deterministic failure signature is detected, skip remaining retries and minimize cleanup/status mutations to one final state write.
- **Estimated call-count reduction:** meaningful on every affected failure run.
- **Rate-limit risk reduction:** medium.

### Cross-check against repo API hygiene rules
The logs themselves repeatedly instruct maintainers to prefer batched GraphQL and cycle-local caches. The biggest gaps in sampled runs are:
- watcher loops with fixed-interval polling,
- per-issue fallback lookups after partial GraphQL success,
- retries continuing after deterministic failures.

## MCP & Serena Efficiency

### 1. Serena prompt contract and runtime capability are mismatched
- **Evidence:** Implement run `25057072163` repeatedly logged `ERROR codex_core::tools::router: error=unsupported call: activate_project` between `13:57:04Z` and `13:59:32Z`.
- **Impact:** The agent wastes turns trying a tool path the runtime cannot execute.
- **Concrete change:** Generate Serena instructions from runtime capability discovery. If `activate_project` is unavailable, do not instruct the model to call it.
- **Benefit:** Lower tool churn, fewer wasted model/tool turns, faster completion.

### 2. Context7 MCP failure is poisoning the whole tool list
- **Evidence:** Run `25076992830` shows `mcp: context7 failed ... timed out after 10 seconds`, while `git` and `serena` both reached ready state; shortly after, Azure rejects the malformed tool list.
- **Impact:** One bad optional MCP collapses the entire model call.
- **Concrete change:** Register optional MCPs independently and omit failed ones from the request payload.
- **Benefit:** Preserves Serena/Git value even when Context7 is unhealthy.

### 3. Serena efficiency reporting exists but is not surfaced
- **Evidence:** Implement runs export `SERENA_REPORT_FILE` (e.g. `/tmp/codex-implement-25076992830/serena_efficiency_report.md`), but the report content is not visible in sampled step summaries/artifacts.
- **Impact:** Hard to audit broad reads vs targeted symbol usage from production runs.
- **Concrete change:** Upload the Serena report as an artifact or append a short summary to the step summary.
- **Benefit:** Makes MCP/tool efficiency measurable instead of anecdotal.

### 4. Increase safe parallelization in pre-model preparation
- **Evidence:** Implement and status workflows fetch/support-stage multiple independent assets: issue metadata, comments, memory context, support scripts.
- **Concrete change:** Parallelize non-dependent fetch/stage operations before model invocation, then join before prompt assembly.
- **Benefit:** Small but safe latency win without affecting correctness.

### 5. Prune unsupported or low-value tool instructions from prompts
- **Evidence:** Logs contain extensive embedded policy/tooling guidance, and unsupported Serena calls appeared in execution.
- **Concrete change:** Build tool guidance from actual available tools at runtime; exclude unavailable Serena/MCP actions from the prompt block.
- **Benefit:** Better token efficiency and fewer invalid tool attempts.

## Prompt Cache & Memory System

### Prompt cache behavior
- **Observed:** `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears in `plan`, `implement`, `orchestrate_poll`, and `review_autofix`.
- **Not observed:** no concrete prompt-cache `creation` / `read` counters, hit rates, fail-open events, or token attribution were present in sampled logs.
- **Conclusion:** prompt caching may be enabled, but it is **not observable enough** to optimize confidently.

### Memory retrieval effectiveness
- Memory retrieval is strong in the sample:
  - **100% retrieve hit rate**
  - small average context size (**46.15 tokens**)
  - no fail-open or disabled entries
- This suggests memory is helping without adding much token overhead.

### Likely cache-fragmentation causes
- Repeated long issue bodies and invariant instructions appear to be re-inlined across retries, especially in failed implement runs like `25076992830`.
- Dynamic noise appears close to the prefix: timestamps, retry counts, runner/setup chatter, and changing failure snippets.

### Concrete improvements
1. **Split prompts into stable prefix + dynamic suffix**
   - Put static system/process instructions and invariant issue summary in a cacheable prefix.
   - Keep attempt-specific diagnostics in a short suffix.
   - **Impact:** lower input-token churn and better cache reuse.

2. **Emit cache telemetry per model call**
   - Record cache create/read token counts and cache key family in job logs.
   - **Impact:** makes cache ROI measurable.

3. **Avoid feeding full repeated issue bodies on deterministic retries**
   - Reuse a condensed issue summary plus the last failure signature.
   - **Impact:** lower token and latency cost on retry-heavy runs.

4. **Correlate memory retrieval with prompt-cache results**
   - If memory retrieval always adds small, stable blocks, it should sit inside the cacheable prefix where possible.
   - **Impact:** combines memory usefulness with cacheability.

## Orchestrator Health

### Observed health signals
- **Heavy skip fan-out:** recent bursts dispatch clarify/plan/implement/respond together, then all skip.
- **Poller instability under queue pressure:** failures `25058629488` and `25061570578` spent their budget waiting for runners; later run `25091149961` completed in **34s** when no work existed.
- **Long self-heal loops:** plan run `25073268072` completed only after auto-stall-recovery context was injected.
- **Terminal-state drift in release gate:** `test_and_mark_stable` has **0 successful runs** in sample.

### Pain points
1. **Too many workflows are created before eligibility is known.**
2. **The release gate is coupling too many expensive/fragile checks into one terminal signal.**
3. **Self-heal loops can continue long after value drops off.**
4. **Scheduler contention is likely amplified by background no-op workflow traffic.**

### Smallest safe mitigations
- Pre-gate dispatches.
- Fail fast on invalid orchestration output.
- Shorten or back off watcher loops.
- Cap auto-recovery cycles earlier and escalate.

### Indicators to track after changes
- `% skipped/no-op runs` by workflow family
- `orchestrate_poll` queue wait and failure rate
- `test_and_mark_stable` success rate
- `median child_count` in decompose smoke tests
- `review_autofix` cancelled-runtime minutes
- deterministic MCP-failure count per day

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

### 1. Implement-stage deterministic failure loop
- **Stage:** implement
- **Type:** compute + retry overhead
- **Evidence:** 1,571–4,984s failed implement runs with repeated identical Azure 400s.
- **Fix:** non-retryable classification + MCP preflight.

### 2. Release-gate orchestration and deep-audit tail
- **Stage:** review/autofix -> validate/orchestrate -> release gate
- **Type:** compute + watcher overhead
- **Evidence:** `test_and_mark_stable` average **4,414s**, failures at **5,967s** and **3,542s**; `workflow_log_analysis` cancellations.
- **Fix:** fail-fast decompose validation, optimize deep-audit, reduce blocking waits.

### 3. Plan-stage stall recovery
- **Stage:** clarify -> plan
- **Type:** retry/deferral overhead
- **Evidence:** plan runs at **6,038s** and **7,243s** versus family p95 **200s**.
- **Fix:** tighter cap on auto-answer/autorecovery loops.

### 4. Review/autofix cancellation waste
- **Stage:** review/autofix
- **Type:** compute wasted by superseded runs
- **Evidence:** **38 cancelled / 75** with several 5–8 minute cancellations.
- **Fix:** earlier stale-base and superseded-run exits.

### 5. Queueing on orchestrate poller
- **Stage:** orchestrate/poll
- **Type:** queue overhead
- **Evidence:** two poller failures at **903s** with repeated runner wait messages.
- **Fix:** reduce no-op traffic and preserve poller capacity.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- Deterministic implement failures from MCP/OpenRouter/Azure tool-list shape.
- Release-gate failures driven by decompose correctness and workflow-log-analysis timeout.
- Large volume of skipped/no-op phase workflows.
- Review/autofix cancellation waste and long check-run waits.

**Top failure modes**
- `implement / implement` failing after long retries (`25076992830`, `25057072163`, `25055428237`, `25069841009`).
- `test_and_mark_stable` failing on decompose child-count assertion (`25088532565`) and orphan workflow watch cancellation (`25074100587`, `25088541089` descendant).
- `orchestrate_poll` queue starvation (`25058629488`, `25061570578`).
- `nightly_validation_selftest` exit-code failure with insufficient surfaced diagnostics (`25089252262`).

**Highest-cost drivers**
- Repeated failed implement retries.
- Review/autofix reviewer/check-run wait overhead.
- Deep-audit long-tail runtime.
- No-op workflow fan-out that still consumes scheduler capacity.

**Top 3 prioritized actions**
1. **Preflight MCP registration and classify Azure tool-list 400 as non-retryable.**
2. **Add strict post-decompose schema validation and optimize/deblock workflow-log-analysis deep-audit.**
3. **Move phase eligibility checks before clarify/plan/implement/respond dispatches; add earlier stale/cancel exits in review_autofix.**

## Metrics Appendix

### Repository-level summary

| Repository | Total Runs | Success | Failure | Cancelled | Other | Failure Rate | p50 Duration (s) | p95 Duration (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 211 | 12 | 45 | 732 | 1.2% | 1.0 | 633.0 |

### Key workflow-family metrics

| Workflow Family | Total Runs | Success | Failure | Cancelled | Other | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| implement | 192 | 17 | 7 | 3 | 165 | 192.5 | 1.0 | 1162.3 |
| plan | 193 | 15 | 0 | 1 | 177 | 91.2 | 1.0 | 200.0 |
| review_autofix | 75 | 35 | 0 | 38 | 2 | 400.6 | 31.0 | 1569.2 |
| orchestrate_poll | 25 | 23 | 2 | 0 | 0 | 180.6 | 83.0 | 888.2 |
| test_and_mark_stable | 3 | 0 | 2 | 1 | 0 | 4414.3 | 3734.0 | 5743.7 |
| workflow_log_analysis | 3 | 1 | 0 | 2 | 0 | 1976.7 | 2246.0 | 2334.2 |
| ci | 46 | 46 | 0 | 0 | 0 | 607.3 | 611.0 | 652.5 |
| clarify | 213 | 16 | 0 | 0 | 197 | 10.6 | 1.0 | 76.4 |
| orchestrate_clarify_respond | 193 | 3 | 0 | 0 | 190 | 1.5 | 1.0 | 2.4 |

### Notable failed runs

| Run ID | Workflow Family | Duration (s) | Failure Point |
|---|---|---:|---|
| 25088532565 | test_and_mark_stable | 5967 | `orchestrate-decompose-test` / `Dispatch internal-orchestrate.yml with multi-issue project` |
| 25076992830 | implement | 4984 | `implement / implement` / `Run Codex implementation` |
| 25057072163 | implement | 4053 | `implement / implement` / `Run Codex implementation` |
| 25055428237 | implement | 3818 | `implement / implement` / `Run Codex implementation` |
| 25074100587 | test_and_mark_stable | 3542 | `orphan-workflows-test` / `Dispatch & watch — workflow-log-analysis` |
| 25069841009 | implement | 3559 | `implement / implement` / `Run Codex implementation` |
| 25058629488 | orchestrate_poll | 903 | queue/runner pickup failure |
| 25061570578 | orchestrate_poll | 903 | queue/runner pickup failure |
| 25089252262 | nightly_validation_selftest | 95 | `validation-selftest` / `Run validation self-test matrix` |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total AI memory telemetry events | 360 |
| Retrieve operations | 65 |
| Retrieve hit rate | 100.0% |
| Avg retrieve estimated tokens | 46.15 |
| Keyword method: plain | 55 |
| Keyword method: llm | 10 |
| Zero-record retrieves | 0 |
| `fail_open: true` entries | 0 |
| `enabled: false` entries | 0 |
| Push attempts >1 | 0 |

### Cache / token observability

| Metric | Status |
|---|---|
| Prompt cache enabled flag | Observed (`OPENROUTER_PROMPT_CACHE_DISABLED=false`) |
| Prompt cache create/read counters | **Not observed in sampled logs** |
| Exact token totals by run/model | **Not present in supplied telemetry** |
| Actions cache hits | Observed qualitatively (`codex-v0.125.0` hit, `setup-uv` hit in slow plan run `25073268072`) |

### GH API-heavy patterns observed

| Workflow / Step | Pattern | Observed Cost Signal |
|---|---|---|
| `test_and_mark_stable` / `orphan-workflows-test` | fixed-interval watcher polling | ~37 minutes of polling before cancellation |
| `review_autofix` / check-run collection | polling check-runs | worst-case requests documented as 31 -> 61 |
| `review_post-merge-validate-dispatch` | GraphQL + per-issue REST fallback | extra calls per linked issue when labels unknown |
| `issue_pr_status` | GraphQL batch fallback to per-issue REST | degraded batching behavior |
| `implement` failure path | API cleanup/state updates after deterministic model failure | avoidable post-failure churn |

If you want, I can turn this into a shorter operator-facing memo or a prioritized engineering backlog with owners and acceptance criteria.
