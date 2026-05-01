## Executive Summary

- **Make `workflow_log_analysis` publishing conflict-proof and stop blocking `test_and_mark_stable` on that child workflow.** Run `25208727402` spent **4,834s** and then failed on `analyze-commit-notify / Commit and push report` with `CONFLICT (add/add)` on `analysis/workflow-optimization-2026-05-01-4.md`; `test_and_mark_stable` run `25208710605` separately failed after **4,912s** while watching `workflow-log-analysis`. **Estimated impact:** remove a major false-red source and cut **45–90 minutes** from affected stable-release critical paths. **Confidence:** high.

- **Slim the heavy `review_autofix` path for small, comment-only, and Claude-branch runs.** `review_autofix` has a **p95 of 2,020s**, a slow run at **3,032s** (`25215784558`), and another at **1,552s** (`25221451024`) while running a 6-model reviewer panel, `xhigh` reviewer/editor reasoning, and reviewer two-pass. **Estimated impact:** **10–30 minutes** saved on tail runs plus meaningful token reduction. **Confidence:** medium-high.

- **Fail fast in CI by moving the new recovery tests earlier.** CI has a **34.6% failure rate** (18/52), and many failures cluster on `lint / Implement post-Codex recovery unit tests`, yet still consume **537–594s** before failing (for example `25208681700`). **Estimated impact:** **8–10 minutes** saved per failing CI run during red periods. **Confidence:** high.

- **Reduce wasted token spend in `workflow_log_analysis` and `implement`.** Observed `summarize_unselected_runs` calls consumed **1,955,104 tokens across 10 sampled ops** (range **160,232–304,169** each). Failed implement run `25215763575` spent **93,486 tokens** across two empty-output attempts, with the second attempt alone using **87,826 tokens**. **Estimated impact:** large recurring token savings with low behavior risk. **Confidence:** high.

- **Short-circuit no-work poll cycles before full checkout.** Recent `orchestrate_poll` runs with `has_work=false` still did `actions/checkout@v5` with `fetch-depth: 0` and tag/history fetches, taking **39–58s** (`25223781498`, `25221729088`, and similar summaries). **Estimated impact:** save **8–15s per poll**, reduce git/API noise, and cut runner occupancy. **Confidence:** high.

- **AI memory is on, but retrieval effectiveness is weak in the sampled window.** Across sampled deep-dive logs, only **1 of 5 parseable `retrieve` ops** selected any records (**20% hit rate**), and most used `keyword_method: none`. **Estimated impact:** modest direct speed gain, but better memory quality and telemetry would improve reliability and reduce repeated context expansion. **Confidence:** medium.

## Speed Optimizations

### Critical-path wins

1. **Decouple `test_and_mark_stable` from synchronous child workflow watching**
   - **Evidence:** `test_and_mark_stable` has **0 successes in 5 runs**, **3 failures**, **2 cancellations**, **avg 4,272.6s**, **p50 4,912s**, **p95 5,485.6s**. Run `25208710605` failed at `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` after **4,912s**. Runs `25215477856` and `25212177682` also failed after **5,609s** and **4,992s**.
   - **Root cause:** The stable-release workflow is carrying long-running, non-release-critical downstream verification/publishing work on its own critical path.
   - **Exact change:** Dispatch `workflow_log_analysis` asynchronously and mark it non-blocking for stable promotion; collect/report its status separately instead of failing the parent stable test.
   - **Estimated time savings:** **45–90 minutes** on affected stable-release runs.
   - **Implementation risk:** **Medium.** Safe if release gating remains on release-critical checks only.

2. **Conflict-proof the report publish step in `workflow_log_analysis`**
   - **Evidence:** Run `25208727402` failed after **4,834s**. The log shows:
     - `REPORT_FILE="analysis/workflow-optimization-2026-05-01-4.md"`
     - `Auto-merging analysis/workflow-optimization-2026-05-01-4.md`
     - `CONFLICT (add/add): Merge conflict in analysis/workflow-optimization-2026-05-01-4.md`
     - `error: could not apply ... chore: add workflow log analysis report`
   - **Root cause:** Filename allocation is not race-safe across concurrent report writers.
   - **Exact change:** Generate report filenames with a collision-proof suffix such as `${run_id}` or `${timestamp}-${run_id}`; alternatively, fetch/rebase first, recompute the next free filename after sync, then commit.
   - **Estimated time savings:** Avoids wasting **~80 minutes** of compute on each failed analysis run and prevents downstream stable-release blockage.
   - **Implementation risk:** **Low.** Backward-compatible path naming change.

3. **Gate the expensive `review_autofix` stack by path type**
   - **Evidence:** `review_autofix` has **avg 387.3s**, **p50 40s**, **p95 2,020.2s**, and **22 cancellations** out of 67 runs. Slow run `25215784558` lasted **3,032s** with:
     - `REVIEWER_MODELS: minimax/minimax-m2.5 ... x-ai/grok-4.1-fast`
     - `REVIEWER_REASONING_EFFORT: xhigh`
     - `EDITOR_REASONING_EFFORT: xhigh`
     - `ENABLE_REVIEWER_TWO_PASS: true`
     - `XPOLL_SUMMARISER_MAX_INPUT_LINES: 3000`
   - **Root cause:** Full reviewer breadth and high reasoning are being applied too broadly, including paths that often end up comment-only, deterministic-skip, or non-merge flows.
   - **Exact change:** For docs-only, small-diff, comment-only, and Claude-branch review paths, reduce reviewer panel width, disable reviewer two-pass, and drop reasoning from `xhigh` to `medium` or `high`.
   - **Estimated time savings:** **10–30 minutes** on slow-tail review runs; a few minutes on median active runs.
   - **Implementation risk:** **Medium.** Use feature flags and only scope down on clearly low-risk paths.

4. **Move the known-failing recovery tests to the front of CI**
   - **Evidence:** CI family: **52 runs**, **18 failures**, **34.6% failure rate**, **avg 602.5s**, **p50 609.5s**. Failures repeatedly occur at `lint / Implement post-Codex recovery unit tests`, and the failing run cluster still costs **537–594s** each.
   - **Root cause:** New/high-risk tests are executed late inside a long lint/test job.
   - **Exact change:** Split `Implement post-Codex recovery unit tests` into an early fast-fail stage before the long lint/test suite, or run those targeted tests first inside the job.
   - **Estimated time savings:** **8–10 minutes per failing CI run** while the suite is red.
   - **Implementation risk:** **Low.** Reordering only.

### Local micro-optimizations

5. **Skip full-history checkout in no-work poll cycles**
   - **Evidence:** Recent no-work poll run `25223781498` logged `has_work=false` and `No active orchestrator projects. Exiting gracefully`, yet still ran `actions/checkout@v5` with `fetch-depth: 0`; similar recent poll runs took **39s**, **46s**, **58s**.
   - **Root cause:** The workflow checks out the full repo before it knows whether there is work.
   - **Exact change:** Determine `has_work` first; if false, skip checkout entirely. If checkout is still needed, use shallow/no-tag checkout for no-op cycles.
   - **Estimated time savings:** **8–15s per poll cycle**.
   - **Implementation risk:** **Low.**

6. **Reduce full-history checkout in lightweight support workflows**
   - **Evidence:** Recent `orchestrate_poll` summaries explicitly note `fetch-depth: 0` and many fetched tags; short workflows like `issue_pr_status` and `review_autofix` post-merge dispatch frequently spend noticeable time waiting/starting runners for small amounts of work.
   - **Root cause:** Checkout defaults are heavier than the task requires.
   - **Exact change:** Audit workflows that only need current refs/status and switch to `fetch-depth: 1` plus targeted fetches when history is not required.
   - **Estimated time savings:** **2–10s** on short workflows; small but cumulative.
   - **Implementation risk:** **Low**, provided history-dependent steps are excluded.

## Cost Optimizations

1. **Trim `workflow_log_analysis` summarization input before model call**
   - **Evidence:** Sampled `AI_MEMORY_TELEMETRY` for `summarize_unselected_runs` shows token use of **160,232**, **171,203**, **186,487**, **206,255**, **207,065**, and one **304,169**-token outlier. Total observed spend was **1,955,104 tokens across 10 sampled ops**.
   - **Root cause:** The summarizer is processing too many unselected runs and too much low-value log text, including skipped/no-op runs.
   - **Exact change:** Exclude skipped/no-op runs earlier, lower the `targeted` count when the sample is dominated by clean runs, cap excerpt lines more aggressively, and summarize by workflow family before per-run expansion.
   - **Estimated savings:** **25–50%** of analysis tokens per run, or roughly **40k–120k+ tokens** on many executions.
   - **Quality-risk notes:** Low if failure/outlier runs remain prioritized.

2. **Stop escalating `implement` retries after the first empty-output attempt**
   - **Evidence:** Failed implement run `25215763575` logged:
     - attempt 1: `tokens used` **5,660**
     - attempt 2: `tokens used` **87,826**
     - then `Codex produced no actionable output 2 attempts in a row ... Aborting retry loop.`
   - **Root cause:** A second attempt is allowed to consume dramatically more context/tokens even after the first attempt already indicates an exploration/no-action pattern.
   - **Exact change:** After one empty/no-action attempt, switch to a reduced fallback prompt, lower reasoning, or require a concrete diff plan before re-running the editor. If not available, fail early with diagnostics.
   - **Estimated savings:** Up to **~88k tokens per pathological failed implement run**.
   - **Quality-risk notes:** Low-to-medium; keep one lightweight retry for transient failures, but do not allow full-context re-expansion.

3. **Scope down model selection and reasoning in `review_autofix`**
   - **Evidence:** Review runs use:
     - `MODEL_EDITOR: openai/gpt-5.3-codex`
     - reviewer panel of **6 models**
     - `REVIEWER_REASONING_EFFORT: xhigh`
     - `EDITOR_REASONING_EFFORT: xhigh`
     - `ENABLE_REVIEWER_TWO_PASS: true`
   - **Root cause:** The highest-cost review configuration is applied too often, even when later steps are skipped or the path is comment-only/deterministic-skip.
   - **Exact change:** Use a tiered policy:
     - full panel only for risky code changes,
     - 1–2 reviewers for docs/small diffs,
     - disable two-pass unless the first pass finds disagreement or high-severity issues,
     - reduce editor reasoning on low-complexity diffs.
   - **Estimated savings:** Likely **30–60% review token savings** on low-risk runs and meaningful tail-latency reduction.
   - **Quality-risk notes:** Medium; keep the full path for large or safety-sensitive diffs.

4. **Stabilize prompt prefixes to improve prompt-cache reuse**
   - **Evidence:** `OPENROUTER_PROMPT_CACHE_DISABLED: false` appears in sampled `workflow_log_analysis`, `review_autofix`, and other runs, but no explicit cache create/read counters are logged. Review logs repeatedly print volatile paths, diff file locations, conflict-resolver files, and dynamic metadata.
   - **Root cause:** Prompt prefixes appear to vary run-to-run due to dynamic filenames, run IDs, and repeated injected metadata, which likely fragments cache keys.
   - **Exact change:** Keep system/developer instructions and workflow policy blocks stable; move volatile items (run IDs, temp paths, timestamps) to the end or into referenced files.
   - **Estimated savings:** Not directly measurable from this sample, but likely **meaningful** for repeated review/analysis flows.
   - **Quality-risk notes:** Low.

5. **Remove avoidable reruns caused by pipeline mechanics**
   - **Evidence:** `workflow_log_analysis` publish conflicts and stable-release child-watch failures both burn long runs without producing a better result.
   - **Root cause:** Mechanical rerun triggers, not model quality, are consuming compute and tokens.
   - **Exact change:** Fix publish race, make ancillary child workflows fail-open for release gating, and add earlier fast-fail checks.
   - **Estimated savings:** High compute and token savings indirectly, especially on long-lived release workflows.
   - **Quality-risk notes:** Low.

## Reliability Improvements

1. **Fix `workflow_log_analysis` add/add publish conflicts**
   - **Failure evidence:** Run `25208727402` failed at `analyze-commit-notify / Commit and push report` with `CONFLICT (add/add)` and `error: could not apply`.
   - **Root cause category:** Git publish race / non-unique artifact naming.
   - **Exact fix:** Use unique report filenames or recompute filename after remote sync; if publish still conflicts, fall back to uploading the report as an artifact and marking the run soft-failed instead of hard-failing the workflow.
   - **Expected reliability impact:** Should remove the observed **20% failure** in `workflow_log_analysis` for this window and prevent at least one downstream stable false red.
   - **Rollback/fail-open considerations:** Safe to fail-open on report publication while still surfacing the artifact.

2. **Make ancillary child workflow failures non-blocking in stable-release tests**
   - **Failure evidence:** `test_and_mark_stable` run `25208710605` failed at `orphan-workflows-test / Dispatch & watch — workflow-log-analysis`; the family had **0 successful runs** in the sampled window.
   - **Root cause category:** Over-coupled orchestration / dependency propagation.
   - **Exact fix:** Separate release gating from telemetry/reporting workflows; continue to dispatch child workflows, but record their status without failing the parent unless a release-critical invariant is violated.
   - **Expected reliability impact:** Significant reduction in false-red stable-release failures.
   - **Rollback/fail-open considerations:** Keep explicit alerting if the child workflow fails repeatedly so visibility is not lost.

3. **Catch the “editor bait remained” failure earlier and closer to the editor step**
   - **Failure evidence:** `test_and_mark_stable` runs `25215477856` and `25212177682` failed at `e2e-smoke-test / Phase 4b: Verify editor removed bait line`. Run `25215477856` ended with:
     - `✗ Editor Bait. FAILED (bait_remained)`
     - `✗ Orch Tests . FAILED (not_run)`
   - **Root cause category:** Cross-workflow contract failure between edit/review behavior and downstream E2E verification.
   - **Exact fix:** Add an earlier, deterministic post-edit assertion in the editor/review path that verifies bait removal before the long stable test continues; if unmet, fail immediately and emit the offending diff/head SHA.
   - **Expected reliability impact:** Reduces long false-red E2E failures and shortens diagnosis.
   - **Rollback/fail-open considerations:** Keep the current end-to-end assertion initially as a backstop.

4. **Harden the empty-output retry path in `implement`**
   - **Failure evidence:** Run `25215763575` aborted after `Codex produced no actionable output 2 attempts in a row ...`.
   - **Root cause category:** Retry policy amplifying a bad state instead of escaping it.
   - **Exact fix:** After first empty/no-op attempt, switch to a compact diagnostic mode and require explicit actionable output before a second full attempt; preserve partial file changes if present.
   - **Expected reliability impact:** Fewer stuck implement runs and fewer noisy failures.
   - **Rollback/fail-open considerations:** Keep one retry path, but make it lighter and diagnostic-first.

5. **Introduce a fast precheck for the new CI recovery tests**
   - **Failure evidence:** Multiple CI failures hit the same test step after ~9–10 minutes.
   - **Root cause category:** Failure discovery too late in the pipeline.
   - **Exact fix:** Isolate the new recovery tests into an early job or early stage.
   - **Expected reliability impact:** Does not change underlying code correctness, but materially reduces rerun latency and makes failures less expensive.
   - **Rollback/fail-open considerations:** None; order-only change.

## AI Memory Health

- In the sampled deep-dive logs, I found **52 parseable `AI_MEMORY_TELEMETRY` objects**.
- **Observed op counts:**
  - `record-run-event`: **26**
  - `summarize_unselected_runs`: **10**
  - `retrieve`: **5**
  - `processed-command-check`: **3**
  - `processed-command-claim`: **3**
  - `record-candidate`: **3**
  - `finalize-task`: **2**

### Retrieval effectiveness
- **Retrieve hit rate:** **20.0%** (**1/5** retrieves had `records_selected > 0`)
- **Average `estimated_tokens`:** **5.6** across all retrieves
- **Average `estimated_tokens` on hits only:** **28**
- **`keyword_method` distribution:**
  - `none`: **4**
  - `plain`: **1**
  - `llm`: **0**

### Flags and issues
- **Zero-record retrieves:** Common. Example: slow review run `25215784558` logged `{"estimated_tokens": 0, "keyword_method": "none", "records_selected": 0, "role": "reviewer"}`.
- **`fail_open: true`:** **Not observed** in parseable retrieve telemetry.
- **`enabled: false`:** **Not observed** in parseable retrieve telemetry.
- **High push retry counts:** Generally healthy; I saw **one** telemetry event with `push_attempts: 2` on a `record-run-event` for an `implement` phase-start event. Most others were `push_attempts: 1`.

### Assessment
- Memory is active and ledgering is working, but retrieval quality is weak in the sampled runs.
- The low hit rate suggests the current retrieval strategy is not materially reducing repeated context construction for reviewer/editor flows.

### Recommended next changes
1. Improve retrieval query construction for reviewer/editor roles before widening memory scope.
2. Log retrieve budget values explicitly so `estimated_tokens vs budget` can be audited directly; the sampled logs did not expose the budget.
3. Emit a small end-of-run memory summary (`retrieves`, `hits`, `selected_tokens`) in every AI workflow for easier trend tracking.

## GH API Call Audit

### Highest-value issues

1. **Implement workflow is API-heavy in failure paths**
   - **Evidence:** The deep-dive audit embedded in sampled logs reported for failed implement run `25208345846`:
     - `gh_api: 127`
     - `gh_paginate: 7`
     - `gh_pr_merge: 18`
     - `checkout_fetch0: 3`
   - **Issue:** Repeated metadata/status lookups and merge-related probes appear to be reissued inside one workflow cycle.
   - **Concrete change:** Build a cycle-local cache file for PR metadata, issue state, branch refs, and mergeability; reuse it across steps/attempts instead of re-calling `gh`.
   - **Estimated reduction:** Potentially **dozens of API calls per active implement run**.
   - **Rate-limit risk reduction:** Medium-to-high.

2. **Poller spends GH API budget even on no-work cycles**
   - **Evidence:** The same derived audit reported `orchestrate_poll` runs like `25209369082` and `25210404247` with `gh_api: 2` and `checkout_fetch0: 2`, while recent no-work run `25223781498` still ended with `has_work=false`.
   - **Issue:** API and git activity happen even when there is no active orchestrator work.
   - **Concrete change:** Detect no-work before repo checkout; call `/rate_limit` only after rate-limit-specific stderr rather than as a generic retry helper.
   - **Estimated reduction:** **1–2 API calls per no-work poll** plus git savings.
   - **Rate-limit risk reduction:** Low-to-medium, but cumulative.

3. **Copilot PR review path paginates full file lists and does artifact cleanup lookups**
   - **Evidence:** Recent run summaries cite:
     - `github.paginate(github.rest.pulls.listFiles, ... per_page: 100)`
     - `gh api /repos/.../actions/runs/.../artifacts`
     - artifact delete calls
   - **Issue:** Full pagination and repeated artifact discovery are expensive on larger PRs and redundant if multiple steps need the same data.
   - **Concrete change:** Fetch PR file list once in the prepare phase, persist it to a workflow artifact or output, and reuse artifact IDs instead of re-listing artifacts during cleanup.
   - **Estimated reduction:** **1 full pagination pass + 1 artifact listing call per review run**, more on large PRs.
   - **Rate-limit risk reduction:** Medium.

4. **Post-merge validate dispatch does serial lookup/dispatch/edit work**
   - **Evidence:** Recent `review_autofix` run `25223804387` summary shows:
     - `gh api graphql` to fetch linked issues
     - `gh workflow run ... || gh workflow run "internal-validate.yml"`
     - `gh issue edit ... --remove-label 'ai:orchestrator-validate-required'`
   - **Issue:** Workflow existence checks and fallback dispatches are serialized and appear repeated per event.
   - **Concrete change:** Query workflow availability once, cache it for the step, then dispatch exactly one workflow; batch GraphQL fields so linked-issue data does not need a second call.
   - **Estimated reduction:** **2–3 API calls per post-merge event**.
   - **Rate-limit risk reduction:** Low.

### Hygiene assessment against repo rules
- **Batching:** Inconsistent. Some steps still do per-phase or fallback-driven repeated calls.
- **Cycle-local caches:** Underused, especially in implement/review flows.
- **Fail-open behavior:** Generally reasonable for some helpers, but not for report publishing and some child-workflow watchers.

## MCP & Serena Efficiency

### What was observable
- Direct Serena MCP call counters were **not surfaced** in the sampled logs, so I could not verify symbol-first usage vs broad reads.
- Serena is clearly present in the workflows:
  - Implement/review runs export `SERENA_REPORT_FILE=.../serena_efficiency_report.md`.
  - Review logs attempt to generate a Serena efficiency report and fall back to a placeholder if generation fails.
- Review logs also show repeated PR diff and conflict-resolver file wiring:
  - `PR_DIFF_SOURCE: gh_pr_diff`
  - `PR_DIFF_ATTEMPTED_PATHS: gh_pr_diff:/tmp/.../pr_diff.patch`
  - repeated re-emission of diff/conflict-resolver env blocks within the same run.

### Efficiency issues
1. **Tool-use observability is too weak**
   - Without per-tool counts or summaries, there is no way to confirm whether Serena is being used efficiently or whether broad/raw reads dominate.
2. **Repeated diff/context rehydration likely adds token and turnaround overhead**
   - The same diff-related context is surfaced many times in long review runs.
3. **Serena reporting may run even on short-circuit or low-value paths**
   - That overhead is probably small, but it adds churn when the workflow is already skipping meaningful review work.

### Recommendations
1. **Emit a one-line Serena usage summary into job logs**
   - Example fields: tool call counts, files touched, duplicate region reads avoided, symbol lookups vs raw reads.
   - **Impact:** Better auditability and easier optimization targeting.
2. **Materialize PR diff once and reuse it across reviewer/editor/conflict phases**
   - Avoid re-reading or re-announcing the same diff metadata repeatedly.
   - **Impact:** Lower token/context churn and slightly faster review setup.
3. **Skip Serena report generation on deterministic-skip/comment-only/no-op paths**
   - **Impact:** Small but safe runtime reduction.
4. **Parallelize independent read-only setup work**
   - Diff fetch, metadata fetch, and policy/preflight generation can often be prepared concurrently before the expensive model call.
   - **Impact:** Small-to-moderate speedup on long review runs.

## Prompt Cache & Memory System

### What the sample shows
- Prompt cache is **not disabled** in sampled runs: `OPENROUTER_PROMPT_CACHE_DISABLED: false` appears in `workflow_log_analysis`, `review_autofix`, and other AI workflows.
- However, the sampled logs do **not** expose explicit prompt-cache **create/read/hit/miss counters**, so cache effectiveness cannot be quantified directly.
- Memory retrieval is active but weak:
  - sampled retrieve hit rate: **20%**
  - most retrieves select **0 records**
  - most use `keyword_method: none`

### Likely cache-fragmentation causes
1. **Highly dynamic prompt prefixes**
   - Temp file paths, run IDs, workflow IDs, and dynamic log excerpts appear frequently in prompt scaffolding.
2. **Repeated context expansion on retries**
   - Implement retries and review re-setup likely create distinct prompt bodies instead of reusing a stable cached prefix.
3. **Large dynamic unselected-run summaries**
   - `workflow_log_analysis` likely produces low cache reuse because run samples and summaries vary substantially run-to-run.

### Concrete improvements
1. **Stabilize prompt prefixes**
   - Keep policy/instruction text fixed; move volatile metadata to the tail or referenced files.
2. **Hash and reuse unchanged PR diff summaries**
   - If the diff hash matches, reuse the prepared summary/context block instead of rebuilding it.
3. **Avoid broadening context on second implement attempt**
   - Empty-output retries should shrink, not expand, the prompt.
4. **Log prompt-cache counters**
   - Add a concise `prompt_cache_reads`, `prompt_cache_writes`, `prompt_cache_hit_tokens` summary per job.

### Estimated impact
- **Tokens:** likely **10–25% savings** in repeated review/analysis flows once cache reuse is made measurable and stable.
- **Latency:** moderate improvement from less prompt preparation and smaller context.
- **Reliability:** better reproducibility, less variance between retries.

## Orchestrator Health

### Healthy signals
- Clarify/plan/respond gating is generally efficient:
  - `clarify` p50: **1s**
  - `plan` p50: **1s**
  - `orchestrate_clarify_respond` p50: **1s**
- Many skipped runs terminate quickly, which is good orchestration hygiene.

### Recurring pain points
1. **No-work polling is still expensive**
   - `orchestrate_poll` success runs commonly take **39–58s** even when `has_work=false`.
2. **Heavy-tail review stage dominates active orchestration**
   - `review_autofix` p95 is **2,020s**, with cancellations further muddying throughput.
3. **Ancillary workflows can poison orchestrator success**
   - Stable-release flows are currently too sensitive to downstream analysis/watch failures.
4. **Support-ref fallback exists**
   - Recent `issue_pr_status` run `25223804348` warned that support checkout ref was unavailable and fell back to `main`; this is safe, but it is still operational drift worth tracking.

### Smallest safe mitigations
- Skip checkout on no-work polls.
- Fail-open ancillary dispatch/watch steps in orchestrated release flows.
- Add explicit reason codes to all review short-circuit paths so expensive paths can be excluded with confidence.
- Surface per-run model cost and prompt-cache counters to make orchestration tuning data-driven.

### Indicators to track
- `% of poll cycles with has_work=false that still perform checkout`
- `review_autofix p95 duration` and cancel rate
- `stable-release runs blocked by child workflow failures`
- `implement empty-output-streak abort count`
- `support checkout fallback-to-main rate`

## Pipeline Flow Bottlenecks

### Dominant bottlenecks by phase

1. **Clarify → Plan**
   - Usually healthy; skip gates are fast.
   - Bottleneck risk is low.

2. **Implement**
   - Active runs are typically **~238–307s**, but failures can waste substantial tokens when the agent loops on empty output.
   - Main bottleneck type: **retry amplification**.

3. **Review / Autofix**
   - The largest active-work bottleneck.
   - Main bottleneck types: **model compute**, **broad reviewer fan-out**, **runner wait**, and **context churn**.

4. **Validate / Orchestrate Poll**
   - Poll cycles are modest individually, but frequent no-work cycles create steady background waste.
   - Main bottleneck types: **unnecessary checkout/clone work** and **runner queue overhead**.

5. **Stable-release / cross-workflow verification**
   - The most severe end-to-end bottleneck.
   - Main bottleneck types: **child-workflow waiting**, **cross-workflow failure propagation**, and **late E2E failure discovery**.

### Ordered fixes by end-to-end impact

1. **Remove synchronous dependency on `workflow_log_analysis` from stable-release critical path**
2. **Conflict-proof report publishing so long analysis runs do not fail at the very end**
3. **Trim `review_autofix` breadth on low-risk paths**
4. **Move known-failing CI tests earlier**
5. **Skip checkout in no-work poll cycles**
6. **Reduce retry amplification in failed implement runs**

### Bottleneck type breakdown
- **Queueing overhead:** frequent hosted-runner waits, especially on short workflows.
- **Compute overhead:** review/autofix and CI lint dominate.
- **Retry overhead:** implement empty-output loop.
- **Merge/conflict overhead:** analysis report add/add conflict.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` critical path is unhealthy: **5 runs, 0 successes**, very long durations (**4,912–5,609s** failures).
- `review_autofix` has a severe heavy tail: **p95 2,020s**, slow runs at **1,552s** and **3,032s**.
- CI is expensive even when failing: **34.6% failure rate**, usually **~9–10 minutes**.

**Top failure modes**
- `workflow_log_analysis` report publish conflict (`25208727402`)
- `e2e-smoke-test / Phase 4b: Verify editor removed bait line` in stable-release tests (`25215477856`, `25212177682`)
- repeated CI failures at `lint / Implement post-Codex recovery unit tests`
- `implement` empty-output retry loop (`25215763575`)

**Highest-cost drivers**
- `workflow_log_analysis` summarization tokens (**1.96M sampled tokens across 10 ops**)
- full review/autofix model stack on broad sets of PRs
- wasted long runs caused by end-of-workflow conflicts and child workflow watching
- no-work orchestrator polls still performing full checkout/history fetch

**Top 3 prioritized actions**
1. **Make analysis report publication conflict-proof and non-blocking for stable-release flows**
2. **Tier `review_autofix` by risk level to reduce reviewer breadth/reasoning on low-risk paths**
3. **Move the new recovery tests to the front of CI and harden implement empty-output retry behavior**

## Metrics Appendix

### Overall repository metrics

| Repository | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 263 | 26 | 30 | 681 | 2.6% | 126.2 | 1.0 | 607.0 |

### Key workflow-family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | Avg duration (s) | p50 (s) | p95 (s) | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| ci | 52 | 34 | 18 | 0 | 602.5 | 609.5 | 644.5 | Expensive red builds; repeated same failure point |
| implement | 184 | 26 | 4 | 6 | 44.2 | 1.0 | 247.7 | Active runs OK; failures can be token-heavy |
| review_autofix | 67 | 45 | 0 | 22 | 387.3 | 40.0 | 2020.2 | Severe heavy tail |
| orchestrate_poll | 18 | 18 | 0 | 0 | 45.5 | 45.5 | 52.9 | Many no-work cycles still do full checkout |
| test_and_mark_stable | 5 | 0 | 3 | 2 | 4272.6 | 4912.0 | 5485.6 | Highest critical-path pain |
| workflow_log_analysis | 5 | 4 | 1 | 0 | 4939.8 | 4834.0 | 5503.8 | One end-of-run publish conflict |
| orchestrate | 5 | 5 | 0 | 0 | 237.6 | 240.0 | 281.2 | Active orchestration itself is not the main issue |

### Notable sampled runs

| Run ID | Workflow family | Conclusion | Duration (s) | Key evidence |
|---|---|---|---:|---|
| 25208727402 | workflow_log_analysis | failure | 4834 | `Commit and push report` failed on add/add merge conflict |
| 25208710605 | test_and_mark_stable | failure | 4912 | Failed while dispatching/watching `workflow-log-analysis` |
| 25215477856 | test_and_mark_stable | failure | 5609 | `Editor Bait. FAILED (bait_remained)` |
| 25212177682 | test_and_mark_stable | failure | 4992 | Same `Phase 4b` bait-line failure |
| 25215763575 | implement | failure | 206 | Empty-output retry loop; 93,486 observed tokens |
| 25215784558 | review_autofix | success | 3032 | Full reviewer stack, `xhigh` reasoning, 2-pass review |
| 25221451024 | review_autofix | success | 1552 | Long review tail; uv cache saved; repeated diff setup |
| 25223781498 | orchestrate_poll | success | 46 | `has_work=false`, yet full checkout with `fetch-depth: 0` |

### Observed token telemetry

| Area | Sample count | Observed token total | Range / breakdown | Comment |
|---|---:|---:|---|---|
| `summarize_unselected_runs` | 10 ops | 1,955,104 | 160,232–304,169 per op | Largest recurring token sink observed |
| Failed implement run `25215763575` | 2 attempts | 93,486 | attempt 1: 5,660; attempt 2: 87,826 | Retry policy is too expensive on empty-output loops |
| Review/autofix model runs | n/a | Not directly exposed | Model stack visible, but token counts absent | Add explicit token counters per review stage |

### Cache metrics observed

| Cache area | Evidence | Status |
|---|---|---|
| Prompt cache enablement | `OPENROUTER_PROMPT_CACHE_DISABLED: false` in sampled analysis/review runs | Enabled |
| Prompt cache read/write counters | Not emitted in sampled logs | **Unavailable** |
| `uv` dependency cache | Cache hit in implement run `25215798370`; cache save in review run `25221451024` | Working |
| Prompt cache effectiveness | No direct hit/miss counters | Cannot quantify from this window |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Parseable telemetry objects | 52 |
| Retrieve ops | 5 |
| Retrieve hits | 1 |
| Retrieve hit rate | 20.0% |
| Avg `estimated_tokens` per retrieve | 5.6 |
| Avg `estimated_tokens` on hits | 28 |
| `keyword_method=none` | 4 |
| `keyword_method=plain` | 1 |
| `keyword_method=llm` | 0 |
| `fail_open: true` retrieves | 0 observed |
| `enabled: false` retrieves | 0 observed |
| Push attempts >1 | 1 observed event |

### GH API hotspot summary

| Workflow / step | Evidence | Main issue | Recommended reduction |
|---|---|---|---|
| implement `25208345846` | collector-derived: `gh_api: 127`, `gh_paginate: 7`, `gh_pr_merge: 18` | repeated metadata/merge calls | cycle-local API result cache |
| orchestrate_poll no-work runs | collector-derived: `gh_api: 2`; recent runs still `has_work=false` | API on empty cycles | skip checkout + avoid generic `/rate_limit` probes |
| copilot PR reviewer | `github.paginate(pulls.listFiles)` + artifact listing/deletes | full pagination / repeated artifact lookups | fetch once and reuse |
| review post-merge validate dispatch | `gh api graphql` + workflow dispatch fallback + `gh issue edit` | serial redundant calls | batch lookups and pre-check workflow availability |


## Deep Audit — Workflows & Scripts (2026-05-01)

### Section 1: Bug & Correctness Sweep

- **ID**: BUG-001  
  **File path**: `.github/workflows/test-and-mark-stable.yml:1249-1360`  
  **Severity**: High  
  **Category tag**: `bug`  
  **Description**: The review wait loop can mark the smoke test successful while the review workflow is still `in_progress`. After 10 minutes it fetches live job logs, counts `Reviewer .* succeeded`, and exits with `status=success` once `SUCCEEDED >= 3` and `SUCCEEDED * 2 > TOTAL_DONE` (lines 1347-1359). That shortcut ignores every downstream stage that runs after reviewers finish: consensus synthesis, editor execution, commit/push, merge-conflict handling, and final workflow conclusion. A run with three successful reviewers and a later editor/push failure will therefore be reported as a passing E2E review.  
  **Recommended fix**: Remove the early-success exit. Keep the reviewer-count probe only as an activity signal for resetting the inactivity timer, or require a terminal workflow signal before returning success (for example `status=completed && conclusion=success`, or an explicit end-of-review marker comment/artifact emitted after the editor/commit path).

- **ID**: BUG-002  
  **File path**: `.github/workflows/review_autofix.yml:485-529,594-607`  
  **Severity**: Medium  
  **Category tag**: `bug`  
  **Description**: The `post-merge-validate-dispatch` job falls back to regex-scanning the PR title/body for issue-like text when `closingIssuesReferences` is empty. It matches broad patterns such as `issues/123`, `issue #123`, and `fixes #123` (lines 486-495), then probes and mutates those issues by dispatching validation and removing `ai:orchestrator-validate-required` (lines 500-529). In the same workflow, the deterministic-skip path explicitly avoids this fallback because incidental prose references can false-match unrelated issues (lines 594-607). That means the post-merge path can clear the validate-required label for an unrelated orchestrator issue that was only mentioned in documentation or examples.  
  **Recommended fix**: Do not use title/body regex fallback on a mutating path. Either require authoritative `closingIssuesReferences`, or add a second authoritative check before any dispatch/label removal (for example timeline/cross-reference verification or explicit orchestrator metadata on the issue).

- **ID**: BUG-003  
  **File path**: `.github/workflows/test-and-mark-stable.yml:2725-2956,3002-3067,3211-3255`  
  **Severity**: Medium  
  **Category tag**: `bug`  
  **Description**: Several “Dispatch & watch” steps correlate the child run only by `id > PRE` and then choose the newest run (`sort_by(.created_at) | last`). That pattern is used for `workflow-log-analysis`, `validation-refresh`, `update_workflows`, `internal-memory-maintenance`, `internal-orchestrate`, and `internal-validate`. If another run of the same workflow starts in `TEST_REPO` after `PRE` but before the poll loop locks onto the intended dispatch, the smoke test can watch the wrong run and report the wrong result.  
  **Recommended fix**: Stamp each dispatch with a unique correlation token and filter on that token when resolving the new run. Where inputs cannot carry a token, use a unique branch/ref or a unique input value already echoed into the child run, then filter the run list by that marker before selecting `NEW_ID`.

### Section 2: GitHub API Call Redundancy Audit

- **ID**: BATCH-001  
  **File path**: `scripts/orchestrate_poll_process.sh:11387-11453`  
  **Severity**: High  
  **Category tag**: `api-batching`  
  **Description**: The standalone PR conflict sweep does one `gh pr list` call to enumerate open PRs, then issues a separate `GET /pulls/{n}` for every candidate to read `state`, `head.ref`, `head.sha`, and `mergeable_state`. Current call count is `1 + N` per sweep, where `N` is the number of open PRs being scanned. On a repo with 100 open PRs, that is 101 calls in a single sweep. This is exactly the per-item REST pattern CLAUDE.md §15 warns about.  
  **Recommended fix**: Reduce this to `1 + ceil(N / batch_size)` by reusing the aliased-GraphQL batching pattern already used in `orchestrate_poll_process.sh` (extend `_fetch_linked_pr_status_graphql` or add a sibling helper with the same alias-chunking contract). For example, with batches of 20 PRs, 100 open PRs drops from 101 calls to 6.

- **ID**: BATCH-002  
  **File path**: `scripts/orchestrate_poll_process.sh:6348-6373`  
  **Severity**: Medium  
  **Category tag**: `api-batching`  
  **Description**: The standalone stall sweep builds its candidate issue set by making seven separate `gh issue list --label ...` calls, one per pipeline label (`ai:clarification`, `ai:planning`, `ai:awaiting-approval`, `ai:implementing`, `ai:done`, `ai:ready-to-merge`, `ai:review-blocked`). Current call count is 7 list calls per sweep before the later candidate-details GraphQL prefetch even starts. This is a repeated fan-out over the same repo/state scope. `[NEEDS VERIFICATION]`  
  **Recommended fix**: Collapse the label fan-out into one batched query and keep `_fetch_candidate_issue_details_graphql` as the downstream detail fetch. A single GraphQL/search query that returns open issues matching any pipeline-phase label would reduce the current 7 calls to 1. The existing candidate-detail batching pattern in `_fetch_candidate_issue_details_graphql` is the right helper family to extend.

- **ID**: BATCH-003  
  **File path**: `.github/workflows/review_autofix.yml:478-530`  
  **Severity**: Medium  
  **Category tag**: `api-batching`  
  **Description**: In the `post-merge-validate-dispatch` fallback path, the workflow first does one PR fetch to regex-extract candidate issue numbers, then does `gh issue view ... --json labels` once per candidate issue to discover whether `ai:orchestrator-validate-required` is present. Current fallback-path call count is `2 + N` (`1` failed/empty `closingIssuesReferences` query, `1` PR fetch, then `N` issue-label fetches).  
  **Recommended fix**: Keep the single PR fetch, then batch the label lookups into one GraphQL alias request for the extracted issue numbers. That cuts the fallback path from `2 + N` to `3` total calls regardless of `N`, or to `2` if the initial empty `closingIssuesReferences` attempt is skipped once the fallback branch is entered. The existing `_fetch_issue_labels_batch_graphql` pattern in `scripts/orchestrate_poll_process.sh` is the natural helper to extend.

### Section 3: Code Duplication & Modularization Opportunities

- **ID**: DUP-001  
  **File path**: `.github/workflows/test-and-mark-stable.yml:2725-2897,2919-2956,3002-3067,3211-3255`  
  **Severity**: Medium  
  **Category tag**: `duplication`  
  **Description**: `test-and-mark-stable.yml` repeats the same dispatch/watch/poll loop for multiple child workflows: snapshot previous run id, dispatch, poll for the new run, then poll `actions/runs/{id}` until terminal. The `workflow-log-analysis`, `validation-refresh`, `update_workflows`, `internal-memory-maintenance`, `internal-orchestrate`, and `internal-validate` blocks are near-identical, with only workflow file, timeout, and allowed conclusions differing.  
  **Recommended fix**: Extract a shared helper under `scripts/`, e.g. `scripts/dispatch_and_watch_workflow.sh`, with a signature like `dispatch_and_watch_workflow <repo> <workflow_file> <deadline_secs> <success_conclusions_csv> [--field key=value ...]`. Update each of the six callers in `test-and-mark-stable.yml` to use the helper and pass only workflow-specific parameters.

- **ID**: DUP-002  
  **File path**: `scripts/label_helpers.sh:102-197; .github/workflows/review_autofix.yml:563-580,3698-3738,3826-3836; scripts/orchestrate_poll_process.sh:1087-1215; scripts/validate_process.sh:496-605; scripts/review_rb_judge.sh:33-77`  
  **Severity**: Medium  
  **Category tag**: `duplication`  
  **Description**: Label creation and resilient phase-label application are implemented multiple times with slightly different local copies. `scripts/label_helpers.sh` already provides `ensure_label_exists` and `set_issue_phase_label_resilient`, but review/autofix, validate, the poller, and the review-blocked judge all carry inline or partial reimplementations. This duplicates label color/description data and phase-swap behavior across several files.  
  **Recommended fix**: Make `scripts/label_helpers.sh` the single owner of label catalog + phase mutation logic. Keep the function signatures `ensure_label_exists <label_name> [repo]` and `set_issue_phase_label_resilient <issue_number> <target_label> <repo>`. Update callers in `review_autofix.yml`, `validate_process.sh`, `review_rb_judge.sh`, and `orchestrate_poll_process.sh` to source that module instead of maintaining local fallbacks, or stage the helper earlier in lightweight jobs that currently avoid it.

- **ID**: DUP-003  
  **File path**: `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/mark-stable.yml:307-334,456-483; .github/workflows/orchestrate_poll.yml:63-97; .github/workflows/review_autofix.yml:1269-1307; .github/workflows/test-and-mark-stable.yml:1133-1155`  
  **Severity**: Medium  
  **Category tag**: `duplication`  
  **Description**: Rate-limit-aware inline retry wrappers (`_rl_wait`, `_gh_retry`, `gh_api_safe`) are copied into multiple workflows with slightly different retry counts, backoff behavior, and stderr handling. The repo already has `scripts/gh_helpers.sh` with `gh_retry`, `gh_retry_to_file`, and `gh_api_json_to_file`, but these workflows maintain parallel implementations.  
  **Recommended fix**: Consolidate on `scripts/gh_helpers.sh` as the shared module. Where early bootstrap prevents sourcing from the checkout, stage a minimal bootstrap copy once and reuse the same interface. Keep the public helper signatures `gh_retry <cmd...>`, `gh_retry_to_file <outfile> <cmd...>`, and `gh_api_json_to_file <outfile> <cmd...>`. Update callers in the five workflows above to stop carrying bespoke retry logic.

### Section 4: Expression Size Limit Risk Assessment

- **ID**: EXPR-001  
  **File path**: `.github/workflows/test-and-mark-stable.yml:1118-1448`  
  **Severity**: High  
  **Category tag**: `expression-limit`  
  **Description**: The `Wait for review & autofix to complete` `run:` block contains GitHub interpolations and is already about **19,696 characters**, leaving only **1,304 characters** before the 21,000-character hard limit. This block has already accumulated log polling, live-log parsing, early-exit logic, and activity heuristics, so future edits have very little headroom.  
  **Recommended fix**: Extract this wait loop to an external script under `scripts/` and pass only small env vars from YAML. That is the safest fix because it removes the entire block from expression-length accounting.

- **ID**: EXPR-002  
  **File path**: `.github/workflows/validate.yml:188-480`  
  **Severity**: Medium  
  **Category tag**: `expression-limit`  
  **Description**: The `Fetch workflow support files` `run:` block contains interpolations and is about **16,529 characters**, leaving **4,471 characters** of headroom. It includes branch/ref resolution, staged checkout fallback logic, large template-copy lists, and schema bootstrap logic in one expression-bearing block.  
  **Recommended fix**: Move the support-file bootstrap into a dedicated script such as `scripts/fetch_validate_support_files.sh`, or split the current block into at least two steps: ref/checkout resolution and file-copy/bootstrap.

- **ID**: EXPR-003  
  **File path**: `.github/workflows/review_autofix.yml:1266-1587`  
  **Severity**: Medium  
  **Category tag**: `expression-limit`  
  **Description**: The `Collect PR metadata` `run:` block is about **16,437 characters**, leaving **4,563 characters** of headroom. It combines local retry helpers, no-PR Claude-branch synthesis, four PR metadata fetches, and linked-issue GraphQL prefetch logic in one interpolation-bearing block.  
  **Recommended fix**: Extract the metadata collection path to `scripts/review_collect_pr_metadata.sh` and keep YAML limited to env wiring plus the script invocation.

- **ID**: EXPR-004  
  **File path**: `.github/workflows/orchestrate_clarify_respond.yml:840-1122`  
  **Severity**: Medium  
  **Category tag**: `expression-limit`  
  **Description**: The `Parse and post answer` `run:` block is about **15,140 characters**, leaving **5,860 characters** of headroom. It bundles processed-command claim logic, memory loop guards, escalation comment building, Telegram notification, and answer posting into one interpolated block.  
  **Recommended fix**: Extract the parsing/posting logic into a script under `scripts/`, or split the current step into separate “claim/guard”, “post escalation”, and “post answer” steps.

No workflow file currently exceeds the 800 KB early-warning threshold; the largest audited workflow is `review_autofix.yml` at 266,996 characters.

### Section 5: Cross-Cutting Concerns

- **ID**: DEAD-001  
  **File path**: `scripts/mark-stable.sh:1-14`  
  **Severity**: Low  
  **Category tag**: `dead-code`  
  **Description**: Repository search found no workflow or script references to `scripts/mark-stable.sh`; only documentation/analysis files mention it. That makes the script effectively orphaned inside this repo and increases the chance it drifts away from the real release path. `[NEEDS VERIFICATION]`  
  **Recommended fix**: Either document it as a supported manual release tool in the main operational docs and keep it intentionally maintained, or remove it from the repository if the workflow-based release path is the only supported path.

- **ID**: SHELL-001  
  **File path**: `scripts/tg_helpers.sh:335-350,405-420`  
  **Severity**: Low  
  **Category tag**: `shellcheck`  
  **Description**: The Telegram cleanup helpers iterate over comment-derived `id_list` with unquoted `for tg_id in ${id_list}; do` after changing `IFS=','`. If the marker is malformed, that expansion can still undergo shell word-splitting/globbing, which is the classic SC2086-style failure mode. The same blocks also delete GitHub tracking comments with raw `curl -X DELETE` while suppressing failures, so malformed input and transient API errors are both hard to see.  
  **Recommended fix**: Parse IDs with `IFS=, read -r -a tg_ids <<< "${id_list}"` and iterate as `for tg_id in "${tg_ids[@]}"; do`. Route the GitHub DELETE through `curl_gh_api` (or at least check the HTTP status once) so cleanup stays rate-limit-aware and observable.

No `TODO`, `FIXME`, or `HACK` markers were found in the audited `.github/workflows/*.yml`, `scripts/*.sh`, or `scripts/*.py` scope.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 3 | BUG-001, BATCH-001, EXPR-001 |
| Medium | 10 | BUG-002, BUG-003, BATCH-002, BATCH-003, DUP-001, DUP-002, DUP-003, EXPR-002, EXPR-003, EXPR-004 |
| Low | 2 | DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3 | Medium |
| API call optimization | 2 | Large |
| Code modularization | 7 | Large |
| Expression size reduction | 4 | Medium |
| Medium/Low fixes | 2 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-01)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is fully proven safe to consolidate without changing endpoint/filter/retry/concurrency behavior; `NEEDS_VERIFICATION` means the overlap is real but a human must confirm freshness/error-handling semantics before changing it; `RISKY_SKIP` means the optimization is visible but must not be auto-implemented because it sits in a poller/recovery/race-sensitive path or otherwise risks changing externally observed behavior.

### Consolidation Candidates (MERGE-###)
No findings.

### Redundant Re-Fetch (REUSE-###)

#### REUSE-001
- **ID:** `REUSE-001`
- **Safety tag:** `NEEDS_VERIFICATION`
- **File path and line ranges:** `.github/workflows/issue_pr_status.yml:297-345` and `.github/workflows/issue_pr_status.yml:503-512`
- **Current call count / proposed call count:** Up to `1 + N + M` → up to `1 + N` on merged-PR runs, where `N` is the per-issue REST fallback count if the earlier GraphQL batch fails, and `M` is the later alert-step body lookups until an orchestrated issue is found or the list is exhausted.
- **Endpoint(s):** GitHub GraphQL `repository.issue(number) { number labels(first: 50) { nodes { name } } body }` via aliased batch; REST `GET /repos/{repo}/issues/{issue_number}`
- **Evidence:** The close-sync step already classifies linked issues using `labels` and `body`, then the merged-alert step refetches issue bodies only to answer the narrower yes/no question “is any linked issue orchestrated?”:
  ```sh
  ORCH_RESP="$(gh_retry gh api graphql -f query="${ORCH_QUERY}" 2>/dev/null || echo '')"
  ...
  _orch_meta="$(gh_retry gh api "repos/${REPOSITORY}/issues/${_orch_num}" \
    --jq '{labels:[.labels[].name], body:(.body // "")}' 2>/dev/null || echo '')"
  ```
  ```sh
  BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
  if printf '%s' "${BODY}" | grep -qF 'Managed by: AI Orchestrator'; then
    IS_ORCHESTRATED="true"
    break
  fi
  ```
  The same earlier step already exports `LINKED_ISSUE_NUMBERS` through `$GITHUB_ENV`, so there is already an established step-to-step cache pattern in this job.
- **Proposed fix:** Extend `Update linked issue labels when PR closes` to export a tri-state signal such as `HAS_ORCHESTRATED_LINKED_ISSUE=true|false|unknown` (or export the resolved `TRACKING_ISSUES` / `MANAGED_ISSUES` lists) via `$GITHUB_ENV`, then update `Send PR merged Telegram alert` to reuse that signal and skip the `_safe_gh_jq "repos/.../issues/${issue_number}"` loop unless the earlier classification is explicitly `unknown`.
- **Safety rationale:** The data overlap is real and in the same job, but the earlier classification path has conservative fail-open behavior on partial lookup failure, so a human must verify that reusing it will not suppress alerts that the current per-issue refetch would send.
- **Downstream signal:** Verify on one merged standalone PR and one merged orchestrator-managed PR that the exported orchestrator flag matches the current alert/no-alert outcome, and confirm the GraphQL-fallback path cannot silently turn an “unknown” classification into a skipped alert.

#### REUSE-002
- **ID:** `REUSE-002`
- **Safety tag:** `NEEDS_VERIFICATION`
- **File path and line ranges:** `.github/workflows/orchestrate_clarify_respond.yml:61-63` and `.github/workflows/orchestrate_clarify_respond.yml:418-420`
- **Current call count / proposed call count:** `2` → `1` on the normal path, with the current second fetch retained only as a cache-miss/unparseable fallback.
- **Endpoint(s):** REST `GET /repos/{repo}/issues/{ISSUE_NUMBER}`
- **Evidence:** The workflow fetches the same child issue twice in the same job and re-parses the same `body`/`title` fields:
  ```sh
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ISSUE_BODY="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.body // ""')"
  ISSUE_TITLE="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.title // ""')"
  ```
  ```sh
  ISSUE_META="$(gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}")"
  ISSUE_BODY="$(printf '%s' "${ISSUE_META}" | jq -r '.body // ""')"
  ISSUE_TITLE="$(printf '%s' "${ISSUE_META}" | jq -r '.title // ""')"
  ```
- **Proposed fix:** In `Check orchestrator metadata`, persist `ISSUE_PAYLOAD` to a runner-local file such as `${RUNNER_TEMP}/orchestrate_issue_payload.json` (or export the parsed `ISSUE_BODY` / `ISSUE_TITLE` via `$GITHUB_ENV`), and update the later prompt-preparation step to read that cached payload first, falling back to the existing `gh_retry gh api` only if the cache is missing or invalid.
- **Safety rationale:** The calls hit the same endpoint for the same issue in the same job, but they sit in different steps with different retry behavior, so freshness and failure semantics are not fully proven from static inspection alone.
- **Downstream signal:** Verify that no step between lines 61 and 418 intentionally relies on a fresher issue-body read, and confirm the cached payload survives checkout/setup so the fallback only triggers on genuine cache-miss or parse errors.

#### REUSE-003
- **ID:** `REUSE-003`
- **Safety tag:** `NEEDS_VERIFICATION`
- **File path and line ranges:** `.github/workflows/review_autofix.yml:1381-1439` and `.github/workflows/review_autofix.yml:1833-1836`
- **Current call count / proposed call count:** `2` logical lookups on the successful metadata path (`1` early linked-issue GraphQL fetch + `1` later REST issue-title fetch) → `1` on that same path, while retaining the REST title lookup only for cache-miss/unparseable fallback.
- **Endpoint(s):** GitHub GraphQL `repository.pullRequest(number) { closingIssuesReferences(first: 50) { nodes { number title body } } }`; REST `GET /repos/{repo}/issues/{issue_number}`
- **Evidence:** `Collect PR metadata` already fetches linked issue `number/title/body` and materializes local context, then the smoke-detection block refetches the issue title:
  ```sh
  if gh_retry "${_linked_tmp}" api graphql ... \
    -f query='query(...){...closingIssuesReferences(first:50){nodes{number title body}}}' \
    --jq '.data.repository.pullRequest.closingIssuesReferences.nodes // []'; then
  ...
  lines.append(f"Issue #{num}: {title}")
  ```
  ```sh
  ISSUE_NUM=$(echo "${PR_BODY}" | grep -oiPm1 '...' || true)
  if [ -n "${ISSUE_NUM:-}" ]; then
    ISSUE_TITLE=$(_safe_gh_jq "repos/${{ github.repository }}/issues/${ISSUE_NUM}" --jq '.title // ""' || echo "")
    if echo "${ISSUE_TITLE}" | grep -qi '\[E2E Smoke Test\]'; then
      IS_SMOKE=true
    fi
  fi
  ```
- **Proposed fix:** Extend `Collect PR metadata` to write a compact `LINKED_ISSUE_TITLES_JSON` cache keyed by issue number (or deterministically parse the already-built `LINKED_ISSUE_CONTEXT_FILE`), then update the smoke-detection block to consult that cache first and call `_safe_gh_jq "repos/.../issues/${ISSUE_NUM}"` only when the cache is absent or invalid.
- **Safety rationale:** The overlap is genuine and lives in the same job, but static reading alone does not fully prove that the regex-selected `ISSUE_NUM` always belongs to the same linked-issue set captured by the earlier GraphQL fetch.
- **Downstream signal:** Verify with one smoke-test PR and one non-smoke PR that the cached-title path returns the same `IS_SMOKE` decision as the current REST lookup, and retain the REST fallback whenever the linked-issue cache is missing or GraphQL failed open.

### Dead Calls (DEAD-API-###)

#### DEAD-API-001
- **ID:** `DEAD-API-001`
- **Safety tag:** `RISKY_SKIP`
- **File path and line ranges:** `scripts/orchestrate_poll_process.sh:11390-11400`
- **Current call count / proposed call count:** `2 + N` → `1 + N` inside the standalone conflict sweep (`gh pr list` + repo default-branch fetch + per-PR `GET /pulls/{n}` calls), where `N` is the number of open PR candidates scanned.
- **Endpoint(s):** REST `GET /repos/{repo}`
- **Evidence:** The standalone conflict sweep fetches `DEFAULT_BRANCH` and never consumes it before the section ends:
  ```sh
  STANDALONE_PRS="$(gh_retry gh pr list ...)"
  STANDALONE_COUNT="$(echo "${STANDALONE_PRS}" | jq 'length')"
  CONFLICT_SWEEP_FIXED=0
  DEFAULT_BRANCH="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}" --jq '.default_branch' || echo "main")"

  for (( sidx=0; sidx<STANDALONE_COUNT; sidx++ )); do
    ...
  done
  ```
  Within this sweep, the logic uses `S_BASE`, `S_HEAD_REF`, `S_HEAD_SHA`, and `S_MERGEABLE_STATE`; `DEFAULT_BRANCH` is not read again.
- **Proposed fix:** Remove the `DEFAULT_BRANCH="$(gh_retry _safe_gh_jq ...)"` fetch from the standalone conflict sweep, or move it down next to the first real consumer if a future edit actually needs it.
- **Safety rationale:** This appears to be a true dead call, but it lives inside `scripts/orchestrate_poll_process.sh`, which the audit policy explicitly marks as `RISKY_SKIP` because poller/recovery changes can alter race handling and log contracts.
- **Downstream signal:** Do not auto-implement; manually review the standalone conflict-sweep section for any hidden dependency on the `DEFAULT_BRANCH` assignment and re-run the sweep path in a controlled environment before removing the fetch.

### Cross-References to Deep Audit Section
- BATCH-001: `RISKY_SKIP` — the per-PR conflict-sweep batching idea is valid, but it sits in `scripts/orchestrate_poll_process.sh` and must preserve stage-1 update-branch vs stage-2 dispatch behavior, retry boundaries, and log keys under manual review.
- BATCH-002: `RISKY_SKIP` — the seven-label stall sweep is real fan-out, but collapsing it changes a poller recovery path inside `scripts/orchestrate_poll_process.sh`, so batching must be manually audited rather than auto-applied.
- BATCH-003: `NEEDS_VERIFICATION` — batching the fallback issue-label probes is directionally right, but implement must preserve the current per-issue fail-open semantics so one lookup failure cannot suppress standalone validate dispatch for unrelated issues.

### Summary Counts
| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 4 | REUSE-001, REUSE-002, REUSE-003, BATCH-003 |
| RISKY_SKIP | 3 | DEAD-API-001, BATCH-001, BATCH-002 |

### Implement-Stage Handoff
No SAFE_TO_MERGE findings in this pass.
