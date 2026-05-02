## Executive Summary

- **The highest-impact fix is to remove or narrowly bypass the new edit-tool discipline for trivial implement tasks.** Two `test_and_mark_stable` runs failed after **4,758s** (`run 25223836137`) and **5,465s** (`run 25237291900`), while the underlying `implement` runs failed in **176–188s** with `Codex bailed: 2 consecutive attempts with no actionable output`. The most direct evidence is the PR summary carried in copilot run `25246015290`, which shows the regression boundary: pre-change smoke implements succeeded in **4–7K tokens**, post-change failed with **4,502 / —** and **24,112 / 13,534** token attempts. **Estimated impact:** saves **~70–85 minutes per failed release test run** and removes the main current failure mode. **Confidence: high.**

- **`review_autofix` is the biggest operational drag after the release test failures.** Family p95 is **1,660s** across 63 runs, with **36 cancelled** runs and slow successful runs at **2,938s** (`25237552686`) and **3,032s` (`25215784558`). Logs show repeated runner waits, heavy GH API usage, and very high Serena tool churn. **Estimated impact:** **20–40% latency reduction** on long review runs by earlier gating, API reuse, and reduced tool churn. **Confidence: medium-high.**

- **Runner allocation and startup overhead are a cross-pipeline bottleneck, especially where jobs later skip or cancel.** CI runs cluster around **614–653s** (`25245179350`, `25245197313`, `25245600802`) with runner wait visible up front; `review_autofix` cancellations such as `25245097952` spent **303s** mostly not doing useful work; `implement` skip/protect runs still consumed **164–167s** (`25245094032`, `25245083016`) largely due to startup/wait. **Estimated impact:** **5–15s per poll/promote/forward-merge job**, **minutes per skipped review/implement job**. **Confidence: high.**

- **Memory retrieval is working well for implement, but weak for review.** Across deep-dive logs, `retrieve` telemetry hit rate was **90.9% (20/22)** with average selected-context size of **43.3 estimated tokens**; however both sampled slow `review_autofix` runs retrieved **0 records** with `keyword_method: "none"`, while implement retrieves consistently returned **1–2 records** at **28–56 tokens**. **Estimated impact:** mostly reliability/quality rather than direct cost; likely reduces reviewer rework and prompt bloat. **Confidence: medium-high.**

- **GH API usage is not rate-limit unhealthy yet, but there is clear redundancy.** Slow `review_autofix` runs showed about **323 GH API log hits** each, `test_and_mark_stable` failures showed **71–94**, and `workflow_log_analysis` runs showed **180–356**. Repeated `/pulls`, `/commits`, `/pulls/files`, GraphQL linked-issue lookups, and `/rate_limit` checks are visible in the same jobs. **Estimated impact:** **20–30% fewer API calls** in review flows and lower cancellation/rate-limit risk. **Confidence: medium.**

- **Workflow log analysis itself is expensive enough to warrant tuning.** `workflow_log_analysis` consumed **137,610–193,188 tokens** in sampled runs while summarizing **74–96** unselected runs (`25223869335`, `25237305050`, `25244066270`, `25245013179`). That is acceptable as an offline diagnostic workflow, but it is now a major cost center. **Estimated impact:** **25–50% token savings** on analysis runs with reduced target count or adaptive summarization. **Confidence: medium.**

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Fail fast in `test_and_mark_stable` when the alt-model implement enters the known no-actionable-output pattern
- **Evidence:** `test_and_mark_stable` failed at `Wait for clarify→plan→implement (alt-model)` in `run 25223836137` after **4,758s** and `run 25237291900` after **5,465s**. The underlying implement failures were much earlier: `25245077011` failed in **176s**, `25245085089` in **188s**, both at `implement / implement / Run Codex implementation`.
- **Root cause:** The release test waits on a long chain even after the alt-model path has hit a deterministic failure signature.
- **Exact change:** In the release test waiter, detect the failure signature `Codex bailed: 2 consecutive attempts with no actionable output` (or failed implement run conclusion on the spawned issue) and terminate the alt-model branch immediately instead of continuing the full wait/analyse chain.
- **Estimated time savings:** **~70–85 minutes per failing release-test run**.
- **Implementation risk:** **Low.** This only short-circuits a known-failed branch; keep the current full path behind a debug flag if needed.
- **Critical-path win:** **Yes.**

### 2. Remove or task-gate the edit-tool-discipline block for fully specified single-file implement tasks
- **Evidence:** The PR summary captured in copilot run `25246015290` shows the sharp regression boundary. Before the block, 7 smoke implements succeeded via direct shell writes in **4–7K tokens**; after the block, 5 consecutive smoke implements failed, including `25224028373` (**4,502 / —**) and `25245077011` (**24,112 / 13,534**).
- **Root cause:** Prompt/tool policy change pushed the model toward Serena/apply-patch behavior that the alt-model handled poorly on trivial tasks.
- **Exact change:** Add a branch in `implement` that detects “single file + exact desired content + no clarification needed” tasks and uses a simplified editor policy, or skips the heavy Serena/edit-discipline block entirely for those cases.
- **Estimated time savings:** **2–4 minutes per failing implement run**, plus the release-test savings above.
- **Implementation risk:** **Medium-low.** Scope it to trivial tasks only; leave current discipline in place for normal code edits.
- **Critical-path win:** **Yes.**

### 3. Move skip/gate decisions ahead of runner-heavy jobs in `review_autofix`
- **Evidence:** `review_autofix` p95 is **1,660s**. Cancelled run `25245097952` lasted **303s** with both `review` paths skipped; cancelled runs `25246004472` and `25246014112` also show hosted-runner waits before the flow ended. Successful docs-only run `25245179393` finished in **29s**, meaning the gating logic itself is cheap once it actually runs.
- **Root cause:** Runner allocation and setup happen before the workflow conclusively knows whether it should run the expensive reviewer/editor path.
- **Exact change:** Hoist the PR/head-ref/doc-only/deterministic-skip logic to a lightweight front gate in the caller workflow, and only fan into reviewer jobs when `should_run=true`.
- **Estimated time savings:** **30s to 5min** on skipped/cancelled review runs; **meaningful p95 reduction** for the family.
- **Implementation risk:** **Low.**
- **Critical-path win:** **Yes.**

### 4. Reduce checkout overhead in `orchestrate_poll`
- **Evidence:** Recent poller run `25245499516` succeeded in **45s**, but the log summary says `Checkout repository` dominated startup and fetched many tags. The workflow also logged `poll_completed` with `has_work:false`, so this was a no-op cycle.
- **Root cause:** Full-ish fetch behavior is too expensive for a frequent poller, especially on no-work cycles.
- **Exact change:** Use the minimum fetch depth and disable tag fetching for the repository checkout in poller paths that do not need tag history.
- **Estimated time savings:** **5–15s per poller run**.
- **Implementation risk:** **Low**, if validated against any code paths that inspect tags.
- **Critical-path win:** **Moderate.**

### 5. Stop artifact enumeration/deletion from dominating copilot-review cleanup
- **Evidence:** Copilot review runs `25246015290` (**135s**) and `25245099232` (**136s**) both call out `Cleanup artifacts` as the runtime hotspot, with GH API calls against `/actions/runs/<id>/artifacts`.
- **Root cause:** Cleanup is doing explicit list/delete work every run.
- **Exact change:** Prefer retention-based artifact expiry, or only delete by exact known artifact name when the artifact count exceeds a small threshold.
- **Estimated time savings:** **30–90s per copilot review run**.
- **Implementation risk:** **Low-medium.** Validate retention expectations first.
- **Critical-path win:** **Local optimization**, but worthwhile.

### 6. Avoid starting full implement jobs when the issue is already guarded by `ai:implementing`
- **Evidence:** Implement runs `25245094032` (**164s**) and `25245083016` (**167s**) eventually skipped because the issue already had `ai:implementing`, but still consumed startup time.
- **Root cause:** The guard exists, but too late in the lifecycle.
- **Exact change:** Move the `ai:implementing` check to an earlier dispatch/gating stage, ideally before the job requests a runner.
- **Estimated time savings:** **1–3 minutes per prevented duplicate implement run**.
- **Implementation risk:** **Low.**
- **Critical-path win:** **Moderate.**

## Cost Optimizations

Ranked by expected token and/or dollar savings.

### 1. Bypass the heavy Serena/edit-discipline prompt path for trivial smoke-canary implements
- **Evidence:** PR `#1948`, surfaced in copilot run `25246015290`, documents a jump from successful **4–7K token** smoke edits to failed attempts of **4,502 / —** and **24,112 / 13,534** after the edit-tool discipline block was introduced.
- **Root cause:** Repeated prompt/context expansion and tool-discipline pressure on a task that only needs a direct file overwrite.
- **Exact change:** For fully specified single-file content replacement tasks, use a minimal prompt template and direct edit mode; do not include the full Serena discipline block.
- **Estimated savings:** **10K–30K tokens per failed smoke issue**, plus avoided reruns.
- **Quality-risk notes:** Low if scoped to deterministic tasks only; do not generalize to normal implementation work.

### 2. Cut avoidable reruns from the known `no actionable output` implement failure mode
- **Evidence:** Multiple implement failures (`25224028373`, `25237704374`, `25245077011`, `25245085089`) hit the same two-attempt failure signature. The release-test failures are downstream consequences of those reruns.
- **Root cause:** The system keeps paying for retries on a prompt/model configuration that is already known-bad for this task shape.
- **Exact change:** Add a fingerprinted fail-fast rule: if task shape is “trivial exact-file overwrite” and attempt 1 exits with the known announce-without-edit or silent Serena stall pattern, switch strategy immediately instead of spending a second full attempt.
- **Estimated savings:** **One full model attempt per affected implement run**; using the observed examples, roughly **4.5K–13.5K tokens** saved per run.
- **Quality-risk notes:** Medium-low; keep the second attempt for non-trivial tasks.

### 3. Tune `workflow_log_analysis` summarization breadth
- **Evidence:** `summarize_unselected_runs` telemetry reported **193,188 tokens** for 95 summaries (`25223869335`), **167,745** for 96 (`25237305050`), **137,610** for 82 (`25244066270`), and **154,202** for 74 (`25245013179`).
- **Root cause:** The analysis workflow is summarizing a large unselected-run set even when the deep-dive set already explains the dominant issues.
- **Exact change:** Reduce the unselected-run target count from 100 to a lower adaptive budget when failure modes are already concentrated, or stop early once the run-family distribution stabilizes.
- **Estimated savings:** **25–50% token reduction** on analysis runs.
- **Quality-risk notes:** Low for routine windows; keep the current wider pass for release/debug modes.

### 4. Reduce repeated review prompt inflation and tool-instruction replay in `review_autofix`
- **Evidence:** Slow review run `25237552686` logs repeated large Serena instruction blocks and very high tool counts; the same run also recorded `Top Serena tools | replace_symbol_body (118), insert_after_symbol (118), get_symbols_overview (94), find_symbol (92), find_referencing_symbols (92) |`.
- **Root cause:** Repeated prompt/context restatement and repeated symbol/tool operations across the same review session.
- **Exact change:** Memoize per-file symbol overviews and references inside the review job, and avoid replaying the full Serena discipline block on every internal pass.
- **Estimated savings:** **Moderate** token reduction on the slowest review runs; likely **double-digit percent**.
- **Quality-risk notes:** Medium; requires care not to stale-cache across changed files in the same run.

### 5. Re-evaluate reasoning level only for deterministic microtasks, not globally
- **Evidence:** Implement runs use `MODEL_EDITOR: openai/gpt-5.3-codex` with `MODEL_REASONING_EFFORT: xhigh`. The regression evidence in PR `#1948` says a temporary change to `low` did not fix the problem, so broad downgrade is not the answer.
- **Root cause:** One-size-fits-all reasoning configuration on tasks that vary widely in complexity.
- **Exact change:** Keep current reasoning for normal implementation/review, but pin deterministic microtasks (smoke-canary, docs-only label sync, no-work poll cycles) to lower reasoning or non-LLM paths.
- **Estimated savings:** Small per run, but material at high volume.
- **Quality-risk notes:** Low if done selectively; do **not** lower reasoning for judge/conflict/review-heavy flows.

### 6. Preserve current model choices for summarization unless/until explicit quality regressions appear
- **Evidence:** Review flows use `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini` with `XPOLL_SUMMARISER_REASONING: medium`; there is no sampled evidence that the summarizer is a major quality or cost problem relative to review/implement.
- **Root cause:** None observed.
- **Exact change:** No immediate model switch recommendation; focus cost work on reruns, prompt shape, and no-op dispatches first.
- **Estimated savings:** N/A.
- **Quality-risk notes:** This is a “don’t churn models prematurely” recommendation.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Fix the alt-model implement regression caused by the edit-tool-discipline prompt block
- **Failure evidence:** `implement` failed 12/184 runs (**6.52%** family failure rate), with clustered failures in `25224028373`, `25237704374`, `25245077011`, and `25245085089`; `test_and_mark_stable` then failed in `25223836137` and `25237291900`.
- **Root cause category:** Prompt/tool-policy regression.
- **Exact fix:** Revert, gate, or task-scope the edit-tool discipline block so trivial tasks do not require Serena/apply-patch-only behavior.
- **Expected reliability impact:** High; likely removes the dominant current release-test failure mode.
- **Rollback/fail-open considerations:** Put behind a feature flag or task-shape guard so you can restore the stricter path for complex edits.

### 2. Repair missing memory-helper staging in review failure paths
- **Failure evidence:** Recent cancelled review runs `25246014112` and `25246004472` both warned: `memory helper script missing; skipping run-end failure event`.
- **Root cause category:** Support-file staging/cleanup race.
- **Exact fix:** Stage `memory_helpers.sh` before any failure path can execute, and make cleanup happen strictly after failure-event emission.
- **Expected reliability impact:** Medium; improves run ledger completeness and reduces blind spots in recovery logic.
- **Rollback/fail-open considerations:** Keep fail-open behavior if helper truly is unavailable, but emit a structured warning metric.

### 3. Convert duplicate-implement suppression from in-job to pre-dispatch
- **Failure evidence:** Runs `25245094032` and `25245083016` both discovered an existing `ai:implementing` label only after startup; duplicate pressure also increases the chance of cancelled/overlapping runs.
- **Root cause category:** Concurrency/idempotency handling too late.
- **Exact fix:** Resolve issue state and label guard before dispatching the reusable implement workflow.
- **Expected reliability impact:** Medium; fewer overlapping runs and fewer accidental race conditions.
- **Rollback/fail-open considerations:** If pre-dispatch state fetch fails, fail open to current behavior.

### 4. Add an explicit smoke-task fallback strategy after a first failed Serena activation/edit attempt
- **Failure evidence:** PR `#1948` states one failure mode was “silent exit after `serena.activate_project`”; another was announce-without-invoke. Direct logs for `25245085089` show Serena was set up successfully, memory retrieved, and then the model still bailed.
- **Root cause category:** Tool-use dead-end on a trivial task.
- **Exact fix:** If the first attempt activates Serena and produces no repo delta for a task constrained to one file, immediately switch to a direct shell-write strategy.
- **Expected reliability impact:** High on smoke/canary tasks; low elsewhere.
- **Rollback/fail-open considerations:** Scope tightly by task constraints.

### 5. Stabilize nightly validation self-test by isolating failing fixtures
- **Failure evidence:** `nightly_validation_selftest` run `25242537588` failed in **89s** with `fixtures=3 passed=1 failed=2`.
- **Root cause category:** Test-fixture instability or stale expectation.
- **Exact fix:** Quarantine the two failing fixtures into separate named cases, emit per-fixture status in the job summary, and avoid treating the entire nightly as opaque failure.
- **Expected reliability impact:** Medium for nightly signal quality.
- **Rollback/fail-open considerations:** Keep nightly red if the fixtures are meant to gate; otherwise downgrade known-bad fixtures to warning until fixed.

### 6. Suppress no-op clarify/plan/respond fan-out when auto-comments are known not to match phase triggers
- **Failure evidence:** The recent window contains many 0–2s skipped runs across `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond`, often immediately after auto-generated `/approved` or `/answer` comments.
- **Root cause category:** Orchestrator trigger fan-out/no-op dispatch.
- **Exact fix:** Add a dispatch-side classifier that only invokes downstream workflows whose trigger predicate can actually pass for the current event body.
- **Expected reliability impact:** Medium; fewer racey no-op runs and less queue churn.
- **Rollback/fail-open considerations:** If classifier is uncertain, keep current fan-out behavior.

## AI Memory Health

- **Observed telemetry coverage:** Memory telemetry **was observed** in deep-dive logs, but not consistently in all sampled recent runs. The sampled deep-dive set yielded **108 structured `AI_MEMORY_TELEMETRY` JSON records**.
- **Retrieve hit rate:** **90.9%** (`20/22` retrieves selected at least one record).
- **Average retrieve size vs budget:** Average `estimated_tokens` was **43.3**, max **56**. That is small and healthy relative to the surrounding prompt sizes.
- **`keyword_method` distribution:** `plain` = **20**, `none` = **2**, `llm` = **0** observed.
- **Zero-record retrieves:** **2** retrieves returned `records_selected: 0`, both in slow `review_autofix` runs (`25215784558`, `25237552686`) with `role: reviewer` and `keyword_method: "none"`.
- **Fail-open / disabled flags:** No sampled JSON telemetry had `fail_open: true`, and none had `enabled: false`.
- **Push retry behavior:** Only **3** telemetry records had `push_attempts > 1`, all `record-run-event` for implement phase-start; max observed was **2**, so push retry pressure is currently low.

### What this means
- **Implement memory retrieval is healthy.** Failing implement runs such as `25245077011` and `25245085089` retrieved **2 records** with **56 estimated tokens**, so memory is not the cause of the no-actionable-output regression.
- **Reviewer memory retrieval is weak.** Slow review runs are spending minutes and lots of tool/API work while retrieving **0 records**, suggesting the reviewer query path is underpowered or mismatched to stored memory categories.
- **Telemetry is only partially observable in the current sample.** Many recent short/skipped runs showed no memory telemetry at all, so dashboarding should distinguish “memory not used” from “memory telemetry missing”.

### Recommended actions
1. **Tune reviewer retrieval keywords** so `review_autofix` does not default to `keyword_method: none`.
2. **Add a per-run memory summary line** to make zero-hit retrieves visible without digging into raw logs.
3. **Keep current fail-open behavior**, but add a metric for “telemetry expected but absent” to improve observability.

## GH API Call Audit

### High-volume patterns

#### 1. `review_autofix` is the biggest API hot spot
- **Evidence:** Slow successful runs `25215784558` and `25237552686` each showed about **323 GH API log hits**.
- **Hot steps:** `review_codex-agent` and `review_gate`.
- **Observed patterns:** repeated `gh api` calls for:
  - `/pulls/{PR_NUMBER}`
  - `/commits/{PR_HEAD_SHA}`
  - paginated `/pulls/{PR_NUMBER}/files`
  - GraphQL linked-issue lookups
  - issue comment posting
  - `/rate_limit`
- **Redundancy issue:** Gate logic and codex-agent both query overlapping PR metadata in the same run.

#### 2. `test_and_mark_stable` repeatedly fans out API work across phase tests
- **Evidence:** Failing release tests showed **71** GH API hits in `25223836137` and **94** in `25237291900`; cancelled runs in the same family still showed **50–100**.
- **Hot steps:** `orchestrate-decompose-test`, `e2e-smoke-test`, `e2e-alt-model-test`, `validate-standalone-test`, `orphan-workflows-test`.
- **Observed patterns:** issue creation, issue/comments polling, timeline checks, workflow dispatches, repeated workflow-run discovery.

#### 3. `workflow_log_analysis` is API-heavy by design
- **Evidence:** Sampled runs showed **180–356** GH API log hits.
- **Observed patterns:** deep fetch/summarization of many runs and logs.
- **Audit view:** This is acceptable for an offline analysis job, but it should be budgeted as such.

#### 4. Artifact cleanup in copilot review is a local API hotspot
- **Evidence:** Copilot runs `25246015290` and `25245099232` call `/actions/runs/<id>/artifacts` and spend most time in cleanup.
- **Redundancy issue:** List/delete per run rather than using retention to absorb most cleanup.

### Concrete batching/reuse changes

1. **Create a cycle-local PR metadata cache in `review_autofix`.**
   - Fetch `/pulls`, `/commits`, `/pulls/files`, and linked issues once.
   - Write them to temp JSON and share across `review_gate` and `review_codex-agent`.
   - **Estimated reduction:** **20–30% fewer API calls** in slow review runs.

2. **Make `/rate_limit` checks lazy, not eager.**
   - Current retry wrappers visibly call `/rate_limit` even in healthy runs (`25245499516`, `25246031701`, slow `review_autofix`).
   - Only query `/rate_limit` after a retryable 403/429 or after N consecutive API failures.
   - **Estimated reduction:** **5–10% fewer calls** in API-heavy jobs.

3. **Batch linked-issue and file-list usage.**
   - `review_gate` uses both PR metadata and file enumeration; serialize once and pass to subsequent steps.
   - **Estimated reduction:** several calls per run for small PRs; much more on larger paginated PRs.

4. **Collapse release-test polling lookups.**
   - In `test_and_mark_stable`, store the spawned issue/workflow IDs once instead of repeatedly rediscovering “latest run after PRE”.
   - **Estimated reduction:** dozens of calls over multi-hour failing release tests.

5. **Use retention-first artifact hygiene.**
   - For copilot review, avoid explicit artifact list/delete unless artifact count is above a threshold.
   - **Estimated reduction:** small in call count, meaningful in elapsed time.

### Alignment with repo API hygiene
- **Batching:** Not consistently applied today in `review_autofix` and release-test polling.
- **Cycle-local caches:** Clear opportunity in review and test workflows.
- **Fail-open behavior:** Generally good; wrappers already tolerate API failures and avoid hard failure in many noncritical paths.

## MCP & Serena Efficiency

### What the logs show

- **Good:** The workflows are already following the “activate once” rule. In slow `review_autofix` run `25237552686`, `serena.activate_project` was called once and succeeded quickly.
- **Good:** Targeted Serena tools are being used rather than raw file dumps. The same run logged strong use of symbol-level tools and reported:
  - `replace_symbol_body` **118**
  - `insert_after_symbol` **118**
  - `get_symbols_overview` **94**
  - `find_symbol` **92**
  - `find_referencing_symbols` **92**
- **Bad:** That volume is also evidence of **tool churn**. The same files/symbols are likely being revisited repeatedly within one review session.
- **Bad:** The failing smoke-task implement path shows Serena/tool discipline may be counterproductive on trivial edits. In `25245085089`, Serena setup succeeded, memory retrieval succeeded, and the job still bailed with no actionable output.
- **Mixed:** No strong evidence of broad raw-file reads in the sampled deep dives; the bigger issue is repeated targeted calls, not broad reads.

### Recommendations

1. **Add run-local memoization for Serena read results.**
   - Cache `get_symbols_overview` and `find_referencing_symbols` responses per file/symbol during a single run.
   - Best target: slow `review_autofix`.
   - **Expected impact:** Lower tool count, lower prompt/tool latency, fewer repeated lookups.

2. **Add a trivial-task Serena bypass.**
   - For exact-content single-file tasks, do not require Serena-based exploration.
   - **Expected impact:** Major reliability improvement for smoke tasks; token/time savings too.

3. **Do not replay the full Serena instruction block on every internal pass.**
   - Logs show repeated discipline/instruction blocks in long review runs.
   - Emit once into stable context, then append only incremental task state.
   - **Expected impact:** Lower token usage and less chance of instruction-induced stalls.

4. **Parallelize safe metadata reads before tool-heavy review logic.**
   - PR metadata, changed-file list, linked-issue graph, and cached symbol overviews can be prepared in parallel.
   - **Expected impact:** Moderate latency reduction in review runs.

5. **Keep Serena for complex edits, not for deterministic file rewrites.**
   - Current policy is too uniform for very different task classes.

## Prompt Cache & Memory System

### Prompt cache behavior
- **Observed:** Sampled AI workflows consistently logged `OPENROUTER_PROMPT_CACHE_DISABLED: false`, so prompt-cache instrumentation is enabled.
- **Observed:** Explicit prompt-cache read/write/hit counters were **not** surfaced in the sampled run logs, so actual hit rate is not measurable from this window.
- **Observed:** Non-LLM caches are working:
  - `setup-uv` cache hits in implement/plan runs.
  - `review-ledger` cache hit in slow `review_autofix` run `25237552686`.

### Likely cache-fragmentation causes
1. **Large dynamic prefixes.**
   - The same jobs repeatedly print large env/config blocks and instruction payloads.
2. **Repeated replay of long stable instruction blocks.**
   - Especially in `review_autofix`.
3. **Task-inappropriate policy blocks.**
   - The edit-tool discipline block appears to have increased both prompt size and failure likelihood for smoke tasks.
4. **Per-pass noise placement.**
   - Repeated runner/env/support-script chatter ahead of actual task payload likely reduces stable shared prefix.

### Memory system effectiveness
- **Implement:** healthy retrieves, small context footprint, high hit rate.
- **Review:** retrieves often empty, so the memory system is contributing little to the longest review runs.

### Recommendations

1. **Keep the stable prompt prefix truly stable.**
   - Move volatile issue/run metadata toward the prompt suffix.
   - Keep tool-discipline text in one canonical block only.
   - **Impact:** Better prompt-cache reuse; lower token resend cost; lower latency.

2. **Emit explicit prompt-cache read/write telemetry.**
   - Add `cache_read`, `cache_write`, `cache_hit`, `cache_miss`, and approximate token savings to logs.
   - **Impact:** Better optimization feedback loop; improved reliability diagnostics.

3. **Use task-shape prompt templates.**
   - Separate templates for:
     - deterministic one-file edits
     - normal implementation
     - review/autofix
     - poller/judge
   - **Impact:** Lower fragmentation and less over-instruction.

4. **Improve reviewer memory retrieval.**
   - Since reviewer retrieves were the only observed zero-hit retrieves, tune candidate categories/keywords for review paths.
   - **Impact:** Better reviewer relevance with negligible token overhead.

## Orchestrator Health

### Observed health signals
- **Good:** `orchestrate_poll` recent run `25245499516` completed cleanly in **45s**, logged `poll_completed`, `job_status:"success"`, `has_work:"false"`, and `push_attempts:1`.
- **Good:** Memory ledger writes for poll start/end are succeeding.
- **Bad:** The system generates many no-op workflow invocations. The recent window shows a heavy burst of 0–2s skipped `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` runs.
- **Bad:** `review_autofix` has very high cancellation volume (**36 cancelled of 63 total**).
- **Bad:** Duplicate/overlapping implement dispatches still happen, later suppressed by the `ai:implementing` guard.
- **Mixed:** Auto-approval/auto-answer flows are functioning, but they also create downstream event churn when predicates are known-false.

### Smallest safe mitigations

1. **Add dispatch-side trigger classification.**
   - Prevent phase workflows from firing when the event body cannot satisfy that phase’s `if`.
2. **Promote duplicate-implement suppression earlier.**
   - Prevent runner allocation for runs that will only discover `ai:implementing`.
3. **Surface cancellation cause metrics.**
   - Split “cancelled due duplicate”, “cancelled due gate false”, and “cancelled due upstream close”.
4. **Track no-op fan-out ratio.**
   - This is the clearest current orchestrator hygiene indicator.

### Observable indicators to track
- Skipped workflow count per orchestrator action
- `review_autofix` cancellation rate
- Mean and p95 time-to-gate in `review_autofix`
- Duplicate-implement suppressions before runner allocation
- Stall-recovery dispatch count vs successful completion count

## Pipeline Flow Bottlenecks

### 1. Queueing / runner allocation
- **Evidence:** Visible across CI (`25245179350`, `25245197313`, `25245600802`), review cancellations (`25245097952`, `25246004472`, `25246014112`), poller (`25245499516`), nightly validation (`25242537588`), and many small jobs.
- **Impact:** Minutes lost on jobs that later skip or cancel.
- **Fix priority:** High.

### 2. Retry and stalled-attempt overhead in implement
- **Evidence:** Failing implement runs burn two attempts before surfacing the known stuck pattern; release tests then wait on those outcomes.
- **Impact:** Direct token waste and indirect multi-hour release delays.
- **Fix priority:** Highest.

### 3. Review/autofix compute + tool churn
- **Evidence:** `review_autofix` slow runs at **2,938–3,032s**, p95 **1,660s**, huge Serena counts, heavy GH API usage, and repeated polling/check-run logic.
- **Impact:** Large p95 drag, high cancellation waste.
- **Fix priority:** High.

### 4. CI compute baseline
- **Evidence:** CI family p50 **616s**, p95 **650.35s**, consistent across many runs.
- **Impact:** This is the dominant steady-state compute cost, but it is stable and reliable.
- **Fix priority:** Medium; optimize after the failure/review bottlenecks.

### 5. Artifact cleanup overhead
- **Evidence:** Copilot review runs around **135–136s** dominated by artifact cleanup.
- **Impact:** Local but recurring.
- **Fix priority:** Medium-low.

### 6. Merge/conflict overhead
- **Evidence:** Limited direct deep-dive evidence in this sample, but config and review-blocked flows indicate the orchestrator spends effort on conflict-heal/judge paths. The current sample did not show a dominant live conflict loop.
- **Impact:** Not a top bottleneck in this window.
- **Fix priority:** Lower until stronger evidence appears.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` alt-model branch waiting on a known failing implement path
- `review_autofix` long-tail duration and high cancellation rate
- CI runner wait + 10-minute baseline lint/test runtime
- No-op workflow fan-out across clarify/plan/implement/respond

**Top failure modes**
- `implement` no-actionable-output loop on trivial smoke tasks
- `test_and_mark_stable` downstream failure from that implement regression
- nightly validation self-test failing 2/3 fixtures
- missing memory-helper script on some review failure paths

**Highest-cost drivers**
- Repeated failed implement attempts on smoke issues
- `workflow_log_analysis` summarization tokens (**137K–193K** per sampled run)
- Slow `review_autofix` sessions with heavy Serena/API usage
- Stable CI baseline across many runs

**Top 3 prioritized actions**
1. **Gate or remove the edit-tool discipline block for trivial single-file implement tasks.**
2. **Pre-gate `review_autofix` and duplicate `implement` runs before runner allocation.**
3. **Cache/reuse PR metadata and Serena read results within `review_autofix` runs.**

## Metrics Appendix

### Repository-level summary

| Repository | Total Runs | Success | Failure | Cancelled | Other/Skipped | Failure Rate | Avg Duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 251 | 15 | 47 | 687 | 1.5% | 109.2 | 1.0 | 613.0 |

### Key workflow-family metrics

| Workflow Family | Total Runs | Success | Failure | Cancelled | Other | Failure Rate | Avg Duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| implement | 184 | 13 | 12 | 5 | 154 | 6.5% | 27.1 | 1.0 | 201.4 |
| review_autofix | 63 | 26 | 0 | 36 | 1 | 0.0% | 305.7 | 41.0 | 1660.0 |
| test_and_mark_stable | 5 | 0 | 2 | 3 | 0 | 40.0% | 3828.2 | 3629.0 | 5323.6 |
| ci | 54 | 54 | 0 | 0 | 0 | 0.0% | 612.6 | 616.0 | 650.4 |
| clarify | 217 | 27 | 0 | 0 | 190 | 0.0% | 16.6 | 1.0 | 118.4 |
| plan | 184 | 24 | 0 | 0 | 160 | 0.0% | 15.0 | 1.0 | 148.9 |
| orchestrate_poll | 30 | 30 | 0 | 0 | 0 | 0.0% | 59.0 | 45.0 | 56.2 |
| workflow_log_analysis | 5 | 2 | 0 | 3 | 0 | 0.0% | 3632.6 | 3576.0 | 4923.4 |

### Notable deep-dive runs

| Run ID | Workflow Family | Conclusion | Duration (s) | Primary Signal |
|---|---|---|---:|---|
| 25237291900 | test_and_mark_stable | failure | 5465 | Alt-model wait failure on clarify→plan→implement |
| 25223836137 | test_and_mark_stable | failure | 4758 | Same failure mode as above |
| 25245085089 | implement | failure | 188 | Codex no-actionable-output on smoke task |
| 25245077011 | implement | failure | 176 | Same failure mode; downstream evidence shows 24,112 / 13,534 tokens |
| 25237552686 | review_autofix | success | 2938 | Heavy Serena/API churn, zero reviewer memory hit |
| 25215784558 | review_autofix | success | 3032 | Same pattern as above |
| 25245499516 | orchestrate_poll | success | 45 | No-work poll cycle; checkout/tag fetch dominates |
| 25246015290 | copilot_pull_request_reviewer | success | 135 | Artifact cleanup hotspot; carries PR #1948 regression evidence |
| 25242537588 | nightly_validation_selftest | failure | 89 | 2 of 3 fixtures failed |

### Observed token evidence

| Source Run | Context | Observed Tokens |
|---|---|---|
| 25246015290 / PR #1948 summary | Pre-regression smoke implements | 4K–7K each |
| 25246015290 / PR #1948 summary | Post-regression run 25224028373 | 4,502 / — |
| 25246015290 / PR #1948 summary | Post-regression run 25245077011 | 24,112 / 13,534 |
| 25223869335 | `workflow_log_analysis` summarize_unselected_runs | 193,188 |
| 25237305050 | `workflow_log_analysis` summarize_unselected_runs | 167,745 |
| 25244066270 | `workflow_log_analysis` summarize_unselected_runs | 137,610 |
| 25245013179 | `workflow_log_analysis` summarize_unselected_runs | 154,202 |

### Memory telemetry summary

| Metric | Value |
|---|---:|
| Structured telemetry records observed | 108 |
| `retrieve` operations | 22 |
| Retrieve hit rate | 90.9% |
| Zero-record retrieves | 2 |
| Avg `estimated_tokens` on retrieve | 43.3 |
| Max `estimated_tokens` on retrieve | 56 |
| `keyword_method=plain` | 20 |
| `keyword_method=none` | 2 |
| `fail_open: true` observed | 0 |
| `enabled: false` observed | 0 |
| Telemetry events with `push_attempts > 1` | 3 |
| Max `push_attempts` observed | 2 |

### GH API hotspot summary from sampled deep dives

| Workflow / Run | Approx. GH API Log Hits | Main Patterns |
|---|---:|---|
| review_autofix / 25215784558 | 323 | `/pulls`, `/commits`, `/pulls/files`, GraphQL, issue comments, `/rate_limit` |
| review_autofix / 25237552686 | 323 | Same as above |
| test_and_mark_stable / 25237291900 | 94 | workflow dispatches, issue/comments polling, run discovery |
| test_and_mark_stable / 25223836137 | 71 | Same family pattern |
| workflow_log_analysis / 25245013179 | 356 | bulk run/log collection and summarization |
| workflow_log_analysis / 25223869335 | 180 | same |
| copilot review / 25246015290 | 10 | artifact enumeration/deletion |
| orchestrate_poll / 25245499516 | 4 | `/rate_limit`, tracking issue lookup |

### Cache observations

| Cache / Signal | Observed State | Evidence |
|---|---|---|
| OpenRouter prompt cache instrumentation | Enabled | `OPENROUTER_PROMPT_CACHE_DISABLED: false` in implement/review/poll runs |
| Prompt cache hit/miss counters | Not observed | No explicit cache read/write telemetry in sampled logs |
| `setup-uv` cache | Hit | Implement/plan runs logged `Cache hit for: setup-uv...` |
| Review ledger cache | Hit | Slow `review_autofix` run `25237552686` logged `Cache hit for restore-key: review-ledger...` |

If you want, I can turn this into a shorter exec-ready action list with owners/severity, or a CSV-style remediation tracker.

## Deep Audit — Workflows & Scripts (2026-05-02)

### Section 1: Bug & Correctness Sweep

- **ID** — BUG-001  
  **File path** — `.github/workflows/review_autofix.yml:617-623`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The deterministic-skip path adds `ai:ready-to-merge` to each linked issue with a raw `POST /issues/{n}/labels`, but it never removes the issue’s previous phase label. That breaks the exclusivity implied by `.github/ai/label_contract.v1.json`’s `issue_phase` group and can leave contradictory states such as `ai:done` + `ai:ready-to-merge` or `ai:review-blocked` + `ai:ready-to-merge`. Other codepaths in this repo already avoid this by doing a phase-swap rather than an append-only label add.  
  **Recommended fix** — Replace the append-only call with the existing contract-aware helper in `scripts/label_helpers.sh`, specifically `set_issue_phase_label_resilient <issue_number> ai:ready-to-merge <repo>`. If this lightweight job must stay self-contained, inline the same GET→compute→PUT logic used by `scripts/label_helpers.sh:160-197` instead of POST-only mutation.

- **ID** — SEC-001  
  **File path** — `scripts/review_commit_changes.sh:455-459; scripts/review_conflict_resolve.sh:852-855`  
  **Severity** — Medium  
  **Category tag** — `security`  
  **Description** — Both scripts rewrite `origin` to `https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}`, which persists the PAT in `.git/config` for the remainder of the job. That increases the secret’s exposure window and makes later debug output, accidental `git remote -v`, workspace inspection, or future artifact capture materially riskier than a per-command credential injection.  
  **Recommended fix** — Stop mutating the remote URL. Push with ephemeral auth instead, e.g. a per-command `git -c http.https://github.com/.extraheader=... push ...`, or a temporary credential helper that is unset immediately after push. Keep the existing repo-wide auth conventions centralized the same way `scripts/gh_helpers.sh` centralizes API auth behavior.

- **ID** — SHELL-001  
  **File path** — `scripts/check_external_branch_advance.sh:180-182`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — `for sha in ${self_subject_shas}; do` relies on shell word-splitting over a multi-line value. Today the loop usually sees plain hex SHAs, but this is still an SC2086-style pattern in a pre-push guard, and it will silently misparse if the producer ever changes formatting or emits an empty record.  
  **Recommended fix** — Consume the list with a line-safe reader: `while IFS= read -r sha; do ...; done <<< "${self_subject_shas}"`, or `mapfile -t shas` followed by `for sha in "${shas[@]}"`.

### Section 2: GitHub API Call Redundancy Audit

Logical call counts below are per execution of the highlighted path; pagination and retries can increase the underlying HTTP request count further.

- **ID** — API-001  
  **File path** — `.github/workflows/review_autofix.yml:207-210,1087-1091,1371-1385,4575-4578`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The normal `review_autofix` path fetches the same PR resource four separate times: once in `gate` for state/head/labels/size, once before expensive work to re-check PR state, once in `Collect PR metadata` for the full payload, and once again in the failure tail to re-check state. The later steps already have `PR_PAYLOAD_FILE`/`PR_META_FILE`, but they still re-hit `/pulls/{PR_NUMBER}`.  
  **Current call count** — 4 logical `GET /repos/{repo}/pulls/{PR_NUMBER}` calls per run.  
  **Proposed call count after fix** — 2 logical calls per run: one in `gate`, one in `Collect PR metadata`, with downstream steps reading cached state from job outputs or `PR_PAYLOAD_FILE`.  
  **Batch/cache pattern to extend** — Reuse the cycle-local cache pattern exemplified by `scripts/orchestrate_poll_process.sh:_fetch_candidate_issue_details_graphql`, plus the existing `PR_PAYLOAD_FILE` artifact already created in this workflow.  
  **Recommended fix** — Emit `pr_state`, `pr_merged`, and `pr_head_ref` from `gate`, and have the codex-agent/failure-tail checks read either those outputs or `jq -r '.state' "${PR_PAYLOAD_FILE}"` instead of calling `gh api` again.

- **ID** — BATCH-001  
  **File path** — `.github/workflows/internal-review.yml:98-101`  
  **Severity** — Low  
  **Category tag** — `api-batching`  
  **Description** — The claude-branch push resolver makes two independent REST calls: one to discover an open PR for the branch and one to fetch the repository default branch. Both are needed only to decide whether to dispatch the no-PR review path.  
  **Current call count** — 2 logical calls per push (`GET /pulls?...head=...`, `GET /repos/{repo}`).  
  **Proposed call count after fix** — 1 GraphQL call returning both `defaultBranchRef.name` and the matching open pull request number.  
  **Batch/cache pattern to extend** — Follow the aliased GraphQL pattern used by `scripts/orchestrate_poll_process.sh:_fetch_linked_pr_status_graphql`.  
  **Recommended fix** — Replace the two REST calls with a single GraphQL query keyed by repository and branch head, and keep the current fail-open behavior explicit in one place.

- **ID** — API-002  
  **File path** — `scripts/orchestrate_poll_process.sh:4525-4529,4584-4603,5217-5220,6946-6947`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — The stall-recovery close/reissue path discovers linked PRs twice for the same issue in the same control flow: `surface_reissue_closed_without_pr` calls `_find_all_linked_prs`, then `close_linked_pr` immediately calls `_find_all_linked_prs` again. `_find_all_linked_prs` itself can issue up to three lookups (timeline, `gh pr list --head`, `gh pr list --search`), so the second call doubles discovery traffic before any actual close action runs.  
  **Current call count** — Up to 6 linked-PR discovery calls per stalled issue in this path.  
  **Proposed call count after fix** — Up to 3 discovery calls per stalled issue by resolving once and passing the result into both helpers.  
  **Batch/cache pattern to extend** — Extend the cycle-local cache approach already used in `STALL_MANAGED_LINKED_PR_CACHE` and other orchestrator caches in `scripts/orchestrate_poll_process.sh`.  
  **Recommended fix** — Have `_find_all_linked_prs` run once per issue/cycle and pass the resolved PR list into both `surface_reissue_closed_without_pr` and `close_linked_pr`, or cache it in an issue-keyed map for the current poll cycle.

- **ID** — BATCH-002  
  **File path** — `scripts/review_rb_judge.sh:146-167`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The review-blocked judge first fetches linked issue numbers via GraphQL, then loops over every linked issue with `GET /issues/{n}`. Only `FIRST_ISSUE_BODY` from the first issue is retained, so every body fetch after the first is wasted API traffic.  
  **Current call count** — 1 GraphQL call + N REST issue fetches for N linked issues.  
  **Proposed call count after fix** — 1 GraphQL call total by returning `number`, `title`, and `body` for linked issues and reading the first node directly.  
  **Batch/cache pattern to extend** — Use the same “fetch the needed fields in the first GraphQL query” pattern as `scripts/orchestrate_poll_process.sh:_fetch_candidate_issue_details_graphql`.  
  **Recommended fix** — Extend the existing `closingIssuesReferences` query to request `nodes { number title body }`, set `FIRST_ISSUE`/`FIRST_ISSUE_BODY` from the first returned node, and drop the per-issue REST loop.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — DUP-001  
  **File path** — `scripts/label_helpers.sh:160-197; scripts/orchestrate_poll_process.sh:1149-1225; scripts/validate_process.sh:532-605; scripts/review_rb_judge.sh:85-110`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Phase-label mutation is implemented four times with near-identical GET→compute→PUT/POST-fallback logic. The copies have already drifted: `review_rb_judge.sh` hardcodes the phase set, while orchestrate/validate use the contract file, and `label_helpers.sh` exposes a more reusable API than either consumer uses. This is high-churn operational logic that should not have four maintenance surfaces.  
  **Recommended fix** — Make `scripts/label_helpers.sh` the single owner. Keep/extend `set_issue_phase_label_resilient <issue_number> <target_label> <repo>` and, if needed, add `resolve_phase_label_plan <target_label> <contract_file>` for callers that need the add/remove set. Update callers in `orchestrate_poll_process.sh`, `validate_process.sh`, `review_rb_judge.sh`, and `review_autofix.yml` to source the shared helper instead of carrying local copies.

- **ID** — DUP-002  
  **File path** — `scripts/orchestrate_poll_process.sh:1014-1030; scripts/validate_process.sh:483-494`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — `post_tracking_comment` exists in two scripts and performs the same best-effort “post a comment to the tracking issue” behavior with slightly different payload encoding. This is exactly the kind of low-level API glue that drifts in edge handling over time.  
  **Recommended fix** — Move this into a shared helper, e.g. `scripts/gh_helpers.sh: post_issue_comment <repo> <issue_number> <body>`, or a small `scripts/issue_comment_helpers.sh`. Update `orchestrate_poll_process.sh` and `validate_process.sh` to call the shared function.

- **ID** — DUP-003  
  **File path** — `.github/workflows/review_autofix.yml:563-575,1292-1327; scripts/gh_helpers.sh:390-602`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — `review_autofix.yml` carries two inline `gh_retry` implementations even though `scripts/gh_helpers.sh` already provides `gh_retry`, `gh_retry_to_file`, and `gh_api_json_to_file`. The three versions differ in rate-limit handling, permanent-failure detection, temp-file handling, and logging, so operational behavior is now inconsistent inside a single workflow.  
  **Recommended fix** — Source `scripts/gh_helpers.sh` earlier in the lightweight review jobs, or extract a tiny bootstrap step/composite action that stages just that helper. Callers to update: the deterministic-skip job and the `Collect PR metadata` step.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — EXPR-001  
  **File path** — `.github/workflows/test-and-mark-stable.yml:1118-1449`  
  **Severity** — High  
  **Category tag** — `expression-limit`  
  **Description** — This interpolated `run:` block is approximately **19,696 characters**, leaving only about **1,304 characters** of headroom before GitHub’s 21,000-character template-expression hard limit. It already embeds retry helpers, polling logic, and branch-specific review selection logic in one block, so even a modest future edit can make the entire workflow unparsable.  
  **Recommended fix** — Extract the review wait/poll logic into an external script such as `scripts/test_and_mark_stable_wait_review.sh`. Second-best option: split the block into separate “discover run”, “poll status”, and “decide outcome” steps.

- **ID** — EXPR-002  
  **File path** — `.github/workflows/review_autofix.yml:1286-1608`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Collect PR metadata` block is approximately **16,437 characters**, leaving about **4,563 characters** of headroom. It combines retry helpers, multiple API fetches, linked-issue GraphQL, and Python-based context assembly in one interpolated step.  
  **Recommended fix** — Extract the whole metadata/context build into `scripts/review_collect_pr_context.sh`. The repo already follows this pattern for other large review steps via `scripts/review_commit_changes.sh` and `scripts/review_conflict_resolve.sh`.

- **ID** — EXPR-003  
  **File path** — `.github/workflows/validate.yml:188-481`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The support-file bootstrap block is approximately **16,529 characters**, leaving about **4,471 characters** of headroom. It is a monolithic staging step with many `${{ }}` interpolations and a long embedded file-selection policy.  
  **Recommended fix** — Move the bootstrap/staging logic into an external script, e.g. `scripts/validate_stage_support_files.sh`, or split the block into checkout, stage-scripts, and stage-prompts steps.

- **ID** — EXPR-004  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:845-1128`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The “Parse and post answer” block is approximately **15,140 characters**, leaving about **5,860 characters** of headroom. It packs memory gating, loop-guard handling, escalation comment generation, Telegram notification, and ledger completion into one interpolated step.  
  **Recommended fix** — Extract it to a script such as `scripts/orchestrate_clarify_post_answer.sh`, keeping the workflow step limited to env wiring and a single script invocation.

No workflow file currently exceeds the 800 KB early-warning threshold; the largest audited workflow is `review_autofix.yml` at roughly **268,926 bytes**.

### Section 5: Cross-Cutting Concerns

- **ID** — CONSIST-001  
  **File path** — `.github/workflows/review_autofix.yml:577-588`  
  **Severity** — Low  
  **Category tag** — `consistency`  
  **Description** — The deterministic-skip job hardcodes the `ai:review-skipped` label color/description and explicitly notes that it must “stay in lockstep” with `scripts/label_helpers.sh`. That creates a second source of truth separate from `.github/ai/label_contract.v1.json`, so contract changes can silently drift from actual runtime behavior.  
  **Recommended fix** — Source `scripts/label_helpers.sh` and/or load label metadata from `.github/ai/label_contract.v1.json` in this job instead of hardcoding the label definition.

- **ID** — SHELL-002  
  **File path** — `scripts/tg_helpers.sh:335-343,405-413`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — Both cleanup paths parse comma-separated Telegram IDs with `for tg_id in ${id_list}` after mutating `IFS`. The current regex limits the data to digits/commas, so this is not an immediate exploit path, but it is duplicated SC2086-prone parsing that will become fragile if the marker format ever expands.  
  **Recommended fix** — Replace both loops with array-safe parsing, e.g. `IFS=',' read -r -a tg_ids <<< "${id_list}"` followed by `for tg_id in "${tg_ids[@]}"; do ...; done`.

- **ID** — DEBT-001  
  **File path** — `.github/workflows/review_autofix.yml:3763-3770,3884-3891,4618-4625`  
  **Severity** — Low  
  **Category tag** — `tech-debt`  
  **Description** — The same long fallback regex for extracting linked issue numbers from PR title/body is duplicated in three late-stage review_autofix paths. Any future change to supported closing-keyword syntax or false-positive handling now requires keeping three copies aligned.  
  **Recommended fix** — Extract a shared helper, e.g. `scripts/extract_linked_issue_numbers.sh <repo> <pr_meta_file> <pr_number>`, and reuse it from the “ready-to-merge”, “review-blocked”, and workflow-failure labeling paths.

- **Dead code / marker scan** — No dead-code finding reached evidence threshold, and no `TODO` / `FIXME` / `HACK` markers were found in the audited `.github/workflows/*.yml`, `scripts/*.sh`, or `scripts/*.py` files.
- **Python-script sweep** — The audited `scripts/*.py` files did not surface a repository-specific finding stronger than the workflow/shell issues above.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | EXPR-001 |
| Medium | 9 | BUG-001, SEC-001, API-001, API-002, BATCH-002, DUP-001, EXPR-002, EXPR-003, EXPR-004 |
| Low | 7 | SHELL-001, BATCH-001, DUP-002, DUP-003, CONSIST-001, SHELL-002, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 4 | Medium |
| API call optimization | 4 | Medium |
| Code modularization | 6 | Large |
| Expression size reduction | 4 | Large |
| Medium/Low fixes | 7 | Small |
