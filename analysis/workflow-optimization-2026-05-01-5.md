## Executive Summary

- **Decouple `workflow_log_analysis` from release-test critical path.** Run `25208710605` (`Test & Mark Stable Release`) spent **4,912s** failing in `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` only because child run `25208727402` later failed in `Commit and push report`; the watcher polled that child for ~**80 minutes** before surfacing failure. **Estimated impact:** save **~4,800s** on affected release-test runs and remove a major false-red path. **Confidence:** high.
- **Move the flaky `Implement post-Codex recovery unit tests` earlier in CI and fix its assertions.** `ci` has **19 failures / 69 runs = 27.5% failure rate**, and many failures cluster in run IDs `25208029193`, `25208312323`, `25208681700`, `25208929558`, `25210565611`, all failing after ~**542–594s** in the same step. In `25210565611`, only **2 assertions** failed after **34 tests passed**, near the end of the job. **Estimated impact:** save **~9 minutes per failing CI run** and materially cut reruns. **Confidence:** high.
- **Review/autofix is the dominant compute and likely token cost center.** Slow successful runs `25208887475` and `25208956190` took **2009s** and **2025s** with **6 reviewer models**, `ENABLE_REVIEWER_TWO_PASS=true`, and `REVIEWER_REASONING_EFFORT=xhigh`. **Estimated impact:** **30–60% review cost reduction** and multi-minute latency reduction on non-critical PRs if reviewer fanout/reasoning are tiered. **Confidence:** medium-high.
- **The workflow-log report publisher has a concurrency bug.** Run `25208727402` failed on an **add/add conflict** in `analysis/workflow-optimization-2026-05-01-4.md`, which also caused the parent stable-release test to fail. **Estimated impact:** remove **25%** observed `workflow_log_analysis` failures and one major downstream false-red. **Confidence:** high.
- **AI memory retrieval is working for implementation, weak for reviewer flows.** Across JSON telemetry in sampled deep dives, `retrieve` hit rate was **2/8 = 25%**; reviewer retrieves were **0-hit in 6/6 sampled reviewer cases**, while implementation retrieves hit with `estimated_tokens=28`. **Estimated impact:** modest token savings, but better reviewer consistency if memory indexing/querying is improved. **Confidence:** high.
- **Observability around Serena/prompt-cache is incomplete on short-circuit paths.** In `25211901106`, the “Log token usage and Serena stats” step crashed on `SUPPORT_SERENA_DIR: unbound variable`, and the next step skipped report generation because `RUNTIME_DIR/PREVIOUS_REVIEWS_DIR` never initialized. Cache probe lines in `25208887475` reported cache metrics as `na`. **Estimated impact:** mainly reliability/diagnostics, with indirect cost wins once measurements are trustworthy. **Confidence:** high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

1. **Critical-path: stop synchronously watching full `workflow_log_analysis` completion inside `test_and_mark_stable`.**  
   - **Evidence:** Parent run `25208710605` failed after **4,912s** in `orphan-workflows-test / Dispatch & watch — workflow-log-analysis`. The watched child run was `25208727402`, which stayed `queued/pending/in_progress` for roughly **08:54:11Z → 10:14:51Z** before ending in failure.  
   - **Root cause:** The stable-release test is blocked on a long downstream analysis workflow whose final “commit report” side effect is not essential to validating stable-release behavior.  
   - **Exact change:** In `orphan-workflows-test`, change success criteria from “child workflow completed successfully” to one of:
     - dispatch succeeded and child run reached `in_progress`, or
     - child produced the expected artifact/checkpoint before report commit, or
     - invoke a no-push/dry-run variant of `workflow-log-analysis` for test coverage.  
   - **Estimated time savings:** **~4,800s** on failing/slow stable-test runs; removes one of the longest critical-path stalls in the sample.  
   - **Implementation risk:** **Medium**, because it changes what the test asserts. Lowest-risk version is a dry-run/no-push child mode.

2. **Critical-path: run `Implement post-Codex recovery unit tests` near the front of CI.**  
   - **Evidence:** `ci` family averages **598s**, p50 **607s**, p95 **643s**, with **19 failures / 69 runs**. Many failures ended at `lint / Implement post-Codex recovery unit tests` after **542–594s**. In `25210565611`, the failing step showed **34 passed, 2 failed** and only then exited.  
   - **Root cause:** A highly flaky/high-signal test gate runs late, so bad changes consume almost the full CI budget before failing.  
   - **Exact change:** Reorder that unit-test file to run immediately after dependency install and before slower lint/unit suites; keep the full suite after it for successful runs.  
   - **Estimated time savings:** **~8–9 minutes per failing CI run**; with 19 failures in 69 runs, this is the clearest repeated latency win.  
   - **Implementation risk:** **Low**.

3. **Critical-path: tier review/autofix fanout by PR size/risk instead of always using 6 reviewers, two-pass, and `xhigh` reasoning.**  
   - **Evidence:** Slow `review_autofix` runs `25208887475` and `25208956190` took **2009s** and **2025s**. Logs show `REVIEWER_MODELS` with 6 models, `ENABLE_REVIEWER_TWO_PASS=true`, `REVIEWER_REASONING_EFFORT=xhigh`, and `EDITOR_REASONING_EFFORT=xhigh`.  
   - **Root cause:** Expensive multi-model review is applied broadly, even though other sampled runs were docs-only or comment-only paths.  
   - **Exact change:** Keep the current panel only for large/high-risk diffs or after a cheap first-pass disagreement signal. For low-risk PRs:
     - use 1–2 reviewers,
     - disable two-pass,
     - reduce reasoning to `medium`/`high`,
     - skip editor/judge setup on comment-only Claude-branch review paths.  
   - **Estimated time savings:** **Several minutes per review_autofix run**, potentially **30%+** on the longest runs.  
   - **Implementation risk:** **Medium**, because quality must be re-validated on complex PRs.

4. **Local but frequent: short-circuit review workflows before runtime workspace/post-processing on closed or comment-only paths.**  
   - **Evidence:** Cancelled runs `25211901106` (**39s**), `25211912812` (**265s**), and `25212017780` (**361s**) all entered Claude-branch/comment-only review paths. `25211901106` later hit post-processing issues (`SUPPORT_SERENA_DIR` unset; runtime workspace absent).  
   - **Root cause:** Gate decisions happen, but downstream telemetry/reporting steps still start or partially initialize.  
   - **Exact change:** Once `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW` or “PR closed / comment-only path” is known, skip runtime workspace, Serena stats, memory post-failure recording, and other post-review steps unless the codex-agent actually ran.  
   - **Estimated time savings:** **30–360s per short-circuited run** plus fewer wasted runner pickups.  
   - **Implementation risk:** **Low**.

5. **Micro-optimization: avoid full-repo checkout in `orchestrate_poll` when `has_work=false`.**  
   - **Evidence:** In `25211445708` (`orchestrate_poll`, **48s**), the poller found **0 active tracking issues** by line 140, yet still performed a full `actions/checkout@v5` with `fetch-depth: 0` plus two support-source checkouts.  
   - **Root cause:** No-work detection happens before heavy work logically, but expensive checkouts still occur in the same run.  
   - **Exact change:** Exit before the full repository checkout when `has_work=false`; keep only the minimal support-source checkout if strictly required.  
   - **Estimated time savings:** **~10–20s per no-work poll run**.  
   - **Implementation risk:** **Low**.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

1. **Tier `review_autofix` model usage instead of defaulting to the full high-cost panel.**  
   - **Evidence:** Long runs `25208887475` and `25208956190` show 6 reviewer models, two-pass enabled, and `xhigh` reasoning. This is the single heaviest AI workflow in the sample by runtime.  
   - **Root cause:** High-cost review configuration is not sufficiently gated by PR complexity or business value.  
   - **Exact change:**  
     - For docs-only/small/no-code PRs: keep deterministic skip.  
     - For low-risk code PRs: 1–2 reviewers, one pass, `medium` or `high` reasoning.  
     - Escalate to the full panel only on disagreement, large diff, workflow edits, or prior autofix failure.  
   - **Estimated savings:** **30–60% review model spend** on a large share of PRs.  
   - **Quality-risk notes:** **Medium**. Use disagreement/escalation as fail-safe.

2. **Reduce unselected-run summarization volume in `workflow_log_analysis`.**  
   - **Evidence:** In `25208727402`, `summarize_unselected_runs` with `openai/gpt-5.4-mini` summarized **83 runs** and consumed **160,232 tokens**. The budget was **1,500,000**, far above observed use. The run set contains many 0–2s skipped clarify/plan/respond runs that add little marginal value.  
   - **Root cause:** The summarizer spends tokens on a wide long tail of low-information runs.  
   - **Exact change:** Lower `WORKFLOW_LOG_SUMMARY_MAX_RUNS` or prioritize:
     - failed/slow families first,
     - successful runs only when duration or warnings exceed thresholds,
     - skip 0–2s gating-only runs unless they contain warnings/telemetry.  
   - **Estimated savings:** **40–50%** of summarizer tokens in similar windows, or about **60k–80k tokens** on a run like `25208727402`.  
   - **Quality-risk notes:** **Low**, because deep-dive errors/slow/recent logs already carry the highest-value evidence.

3. **Eliminate full-run waste from report-push conflicts.**  
   - **Evidence:** `25208727402` completed the expensive analysis, then failed at the very end on an add/add merge conflict while pushing the report.  
   - **Root cause:** Concurrency conflict wastes the entire upstream compute and token cost of the run.  
   - **Exact change:** Use conflict-proof filenames or recompute the filename after fetch/rebase; if the report path already exists remotely, write a new unique suffix before commit.  
   - **Estimated savings:** Avoids wasting **one full 4,834s analysis run** per conflict occurrence.  
   - **Quality-risk notes:** **Low**.

4. **Fail fast on the known-bad CI test instead of paying full runner minutes first.**  
   - **Evidence:** Repeated CI failures all end late in the same test file after ~9 minutes of job time.  
   - **Root cause:** High-signal failure occurs after most compute is already spent.  
   - **Exact change:** Run the recovery test file immediately after setup.  
   - **Estimated savings:** **~9 runner-minutes per failing CI run**; also reduces developer rerun churn.  
   - **Quality-risk notes:** **Low**.

5. **Stabilize prompt prefixes so prompt-cache can actually help.**  
   - **Evidence:** Prompt cache is enabled (`OPENROUTER_PROMPT_CACHE_DISABLED=false`), but cache probe lines in `25208887475` show `cache_creation_input_tokens=na` and `cache_read_input_tokens=na`, so effective hit rate is unobservable. Reviewer/env blocks also repeat many dynamic values.  
   - **Root cause:** Prompt-cache observability is incomplete, and volatile metadata likely fragments cacheability.  
   - **Exact change:** Keep the reusable system/policy prefix stable and move volatile PR metadata, timestamps, or run-specific noise after the cacheable prefix/breakpoint.  
   - **Estimated savings:** **Unquantified but likely meaningful** on repeated reviewer/editor prompts once hit-rate telemetry is fixed.  
   - **Quality-risk notes:** **Low**, if content order changes but semantics do not.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

1. **Fix concurrent report filename collisions in `workflow_log_analysis`.**  
   - **Failure evidence:** `25208727402` failed in `Commit and push report` with `CONFLICT (add/add): Merge conflict in analysis/workflow-optimization-2026-05-01-4.md`.  
   - **Root cause category:** Concurrency / branch-write race.  
   - **Exact fix:** Generate conflict-proof filenames using run ID or final remote state after fetch/rebase; do not select `-N` suffix before syncing with remote.  
   - **Expected reliability impact:** Removes the observed **1/4 failure rate** in `workflow_log_analysis` attributable to report-push conflict and prevents cascading parent failures.  
   - **Rollback/fail-open:** If push still fails, upload the report artifact and mark the reporting step non-blocking for parent test workflows.

2. **Repair the `Implement post-Codex recovery` test contract immediately.**  
   - **Failure evidence:** `ci` family failure rate is **27.5%**; repeated failures in the same step across many runs. In `25210565611`, two tests failed:
     - `test_codex_empty_output_streak_bail_and_flag`
     - `test_failure_diagnostics_posted_to_source_issue`  
   - **Root cause category:** Test/workflow contract drift.  
   - **Exact fix:** Align the workflow’s failure message and diagnostics reason strings with the updated test expectations, or update the tests if the new behavior is intentional.  
   - **Expected reliability impact:** Potentially removes most of the current CI red rate.  
   - **Rollback/fail-open:** If behavior is still in flux, temporarily mark the test as quarantined/xfail while aligning message contract, but only briefly.

3. **Guard Serena/token logging steps against missing runtime/setup variables.**  
   - **Failure evidence:** In `25211901106`, `Log token usage and Serena stats` failed with `SUPPORT_SERENA_DIR: unbound variable`; the next step emitted `RUNTIME_DIR/PREVIOUS_REVIEWS_DIR not set ... Skipping Serena efficiency report.`  
   - **Root cause category:** Missing preconditions on short-circuit path.  
   - **Exact fix:** Use `${SUPPORT_SERENA_DIR:-}` guards and skip telemetry/report generation unless the runtime workspace step completed.  
   - **Expected reliability impact:** Reduces post-gate noise and prevents secondary failures from masking the real reason a run short-circuited.  
   - **Rollback/fail-open:** Safe to fail-open; these are observability steps, not correctness-critical.

4. **Make parent release tests fail-open on non-critical analysis-report publishing errors.**  
   - **Failure evidence:** Parent run `25208710605` failed because child analysis run ended in report-push failure, not because core release validation failed.  
   - **Root cause category:** Over-coupled orchestration / false dependency.  
   - **Exact fix:** Treat report publication as advisory in `orphan-workflows-test`, or assert only that analysis executed and produced artifacts.  
   - **Expected reliability impact:** Prevents false-red stable-release runs caused by side-effect failures.  
   - **Rollback/fail-open:** Keep the child failure visible in logs/artifacts even if the parent remains green.

5. **Add adaptive polling/backoff to long-running watcher steps.**  
   - **Failure evidence:** `Dispatch & watch — workflow-log-analysis` polled a child run every **15s** for ~80 minutes.  
   - **Root cause category:** Control-loop inefficiency under long runtimes.  
   - **Exact fix:** Poll fast while `queued`, then back off to 60–120s once `in_progress` is stable.  
   - **Expected reliability impact:** Lower API pressure and fewer timeout-induced flakes as long-running workflows scale.  
   - **Rollback/fail-open:** Low risk; only affects watcher cadence.

## AI Memory Health

- **Memory telemetry was observed** in sampled deep-dive logs, though not in every sampled run.
- **Observed JSON telemetry count:** **58** events.
- **Operation distribution:** `record-run-event` **32**, `summarize_unselected_runs` **11**, `retrieve` **8**, `record-candidate` **3**, `processed-command-check` **2**, `processed-command-claim` **2**.

### Retrieval effectiveness
- **Retrieve hit rate:** **25.0%** (**2/8** retrieves had `records_selected > 0`).
- **Average `estimated_tokens`:** **7.0** across all retrieves.
- **Budget comparison:** sampled retrieve telemetry did **not** emit a retrieval budget field, so comparison to budget is **not possible from this window**.
- **`keyword_method` distribution:**  
  - `none`: **6/8 = 75%**  
  - `plain`: **2/8 = 25%**  
  - `llm`: **0**

### Retrieval failure patterns
- **Reviewer retrieves were consistently empty** in sampled deep dives:
  - `25208887475` reviewer retrieve: `records_selected=0`
  - `25207020260` reviewer retrieve: `records_selected=0`
  - `25208956190` reviewer retrieve: `records_selected=0`
  - `25211912812` reviewer retrieve: `records_selected=0` in two sampled log files
- **Implementation retrieves worked better:**
  - `25208345846` implementation retrieve selected **1** record with `estimated_tokens=28`
  - similar implementation hit also appears in `25204185528`

### Fail-open / disabled / retries
- **`fail_open: true` entries:** **none observed** in JSON telemetry.
- **`enabled: false` entries:** **none observed**.
- **Push retry counts:** all sampled memory write events showed **`push_attempts=1`**; no evidence of high retry pressure.
- **Processed-command idempotency:** observed in `25208345846`:
  - `processed-command-check`
  - `processed-command-claim`

### Recommendation
- Improve reviewer memory indexing/querying first:
  - include PR/head ref and changed-file keys in reviewer retrieval,
  - avoid `keyword_method=none` as the common path,
  - emit retrieval budget and latency so future analyses can measure efficiency, not just hits.

## GH API Call Audit

1. **Highest-volume hotspot: child-workflow watcher in `test_and_mark_stable`.**  
   - **Evidence:** In `25208710605`, the watcher:
     - called `gh api repos/.../actions/workflows/.../runs?per_page=1`
     - then `...runs?per_page=10`
     - then `gh api repos/.../actions/runs/${NEW_ID}` every **15s**
     - for roughly **80 minutes**, until child run `25208727402` failed.  
   - **Pattern:** unbatched repeated polling of a single run.  
   - **Recommendation:** adaptive backoff:
     - 10–15s while `queued/pending`
     - 60s after 10 minutes of `in_progress`
     - 120s after 30 minutes  
   - **Estimated reduction:** roughly **70–85% fewer status calls** on long waits; likely **200+ API calls saved** on a run like this.
   - **Rate-limit risk reduction:** meaningful; this is the clearest avoidable API churn in the sample.

2. **`copilot_pull_request_reviewer` repeats PR metadata/artifact lookups.**  
   - **Evidence:** In `25211915189`:
     - `Prepare_Get_pull_request_details` calls `github.rest.pulls.get`
     - `github.paginate(github.rest.pulls.listFiles, per_page: 100)`
     - `Cleanup_artifacts_Get_artifact_IDs` calls `gh api /repos/.../actions/runs/<run>/artifacts`  
   - **Pattern:** repeated per-run metadata fetches across jobs instead of reusing outputs/manifests.  
   - **Recommendation:** pass PR file list and artifact IDs via job outputs or generated manifest files so downstream jobs do not re-query the same run metadata.  
   - **Estimated reduction:** **2–3 API calls per reviewer run**.  
   - **Rate-limit risk reduction:** low per run, moderate at scale.

3. **Post-merge validate dispatch loops can batch linked-issue edits.**  
   - **Evidence:** Run summary for `25209053473` explicitly notes `gh workflow run` and `gh issue edit` executing in a loop.  
   - **Pattern:** per-item issue updates instead of cycle-local batching/reuse.  
   - **Recommendation:** gather linked issues once, compute final label/body state, then do one edit per issue at the end; avoid repeated reads inside the loop.  
   - **Estimated reduction:** proportional to linked-issue fanout; likely **2–N fewer calls per dispatch run**.  
   - **Rate-limit risk reduction:** moderate on repos with many linked issues.

4. **`cancel_on_pr_close` API behavior is bounded and healthy.**  
   - **Evidence:** `25212161794` shows `_gh_retry`, `/rate_limit`, and cancel POST usage with no 429s.  
   - **Assessment:** no urgent change needed; this workflow already uses bounded retry/backoff appropriately.

### Audit summary
- **Observed high-redundancy pattern:** status polling loops, not per-item data expansion.
- **Observed missing batching/reuse:** PR file list reuse and linked-issue edit loops.
- **Observed 429 / secondary rate-limit events:** **none in sampled logs**.

## MCP & Serena Efficiency

1. **Serena observability breaks on short-circuit review paths.**  
   - **Evidence:** In `25211901106`, `Log token usage and Serena stats` failed with `SUPPORT_SERENA_DIR: unbound variable`. The next step then skipped Serena reporting because `RUNTIME_DIR/PREVIOUS_REVIEWS_DIR` were unset.  
   - **Impact:** Serena adoption and tool-efficiency metrics become unreliable exactly on the paths most likely to short-circuit or cancel.  
   - **Recommendation:** Initialize `SUPPORT_SERENA_DIR` everywhere or guard the step with `${SUPPORT_SERENA_DIR:-}` and “runtime workspace completed” checks.

2. **Successful long review runs generate Serena report files, but logs do not surface enough stats to audit actual tool discipline.**  
   - **Evidence:** `25208956190` exported `SERENA_REPORT_FILE=/tmp/.../serena_efficiency_report.md`, but sampled logs did not expose concrete tool-usage counters or repeated-region-read stats.  
   - **Impact:** cannot verify from this sample whether runs used targeted symbol lookups vs. broad reads, or whether repeated file-region reads occurred.  
   - **Recommendation:** always emit `tool_usage_stats.json` (or a compact summary) into logs/artifacts for sampled runs.

3. **No direct evidence of Serena tool churn was captured in sampled deep dives.**  
   - **Evidence gap:** no sampled log exposed repeated `mcp__serena__...` tool traces or raw file-read counts.  
   - **Recommendation:** extend the current Serena efficiency report to include:
     - broad-read count,
     - symbol-lookup count,
     - repeated-region-read count,
     - parallelizable read opportunities found,
     - per-run “top waste” summary.

4. **Safe parallelization opportunity:** independent metadata reads.**  
   - **Evidence:** Several workflows fetch PR metadata, file lists, artifact IDs, and support-source checkouts serially.  
   - **Recommendation:** where correctness allows, parallelize independent read-only prep steps before model invocation.  
   - **Expected effect:** small latency gains and less idle wall time without changing semantics.

## Prompt Cache & Memory System

1. **Prompt cache is enabled, but sampled cache telemetry is not actionable.**  
   - **Evidence:** `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears across review runs, but cache probe lines in `25208887475` reported:
     - `prompt_tokens=na`
     - `completion_tokens=na`
     - `total_tokens=na`
     - `cache_creation_input_tokens=na`
     - `cache_read_input_tokens=na`  
   - **Assessment:** cache may be on, but hit/miss effectiveness cannot be measured in this window.
   - **Recommendation:** make real call-level cache metrics mandatory on reviewer/editor requests, not only probe calls.

2. **Prompt variance likely fragments cache utility in review flows.**  
   - **Evidence:** review runs repeatedly inject dynamic env/context blocks (`PR_NUMBER`, paths, runtime files, reviewer settings, ledger paths, etc.) across many stages.  
   - **Root cause:** unstable prompt prefixes reduce cache reuse.  
   - **Recommendation:** keep policy/system/instructions as a stable prefix, and move volatile PR/run metadata later in the prompt body.  
   - **Estimated impact:** unquantified token/latency savings until metrics are fixed, but likely meaningful on repeated reviewer/editor calls.

3. **Memory retrieval is effective for implementation, weak for reviewer context.**  
   - **Evidence:** implementation retrieves hit twice; reviewer retrieves missed six times.  
   - **Root cause:** reviewer retrieval keying is probably too weak or not aligned to PR-level semantics.  
   - **Recommendation:** bias reviewer retrieval to PR/head-ref/file overlap and recent reviewer-pattern memories instead of generic keyword search.

4. **Workflow-log summarization has good explicit token telemetry and should be the model for the rest of the stack.**  
   - **Evidence:** `25208727402` emitted exact telemetry: `model=openai/gpt-5.4-mini`, `summarized=83`, `tokens_used=160232`.  
   - **Recommendation:** adopt the same explicit token-emission standard for review/editor/judge/model fanout so future cost reports are not inference-heavy.

## Orchestrator Health

- **Overall orchestrator gating is functional but noisy.**
  - `clarify`: **198 total**, only **21 success**, **177 other/skipped**
  - `plan`: **174 total**, **17 success**, **157 other/skipped**
  - `orchestrate_clarify_respond`: **174 total**, **5 success**, **169 other/skipped**
  - `implement`: **173 total**, **25 success**, **140 other/skipped**, **3 failures**, **5 cancelled**
- **Interpretation:** the orchestrator is correctly short-circuiting many events, but the pipeline still pays runner/workflow overhead for a large number of no-op runs.

### Observable pain points
1. **High no-op workflow volume**
   - Many sampled `clarify/plan/respond/implement` runs end in **0–2s** because the body/comment prefix does not match.
2. **Poller still does meaningful setup on no-work cycles**
   - `25211445708` had `has_work=false` but still performed repository and support-source checkouts.
3. **Review short-circuit paths still leak into observability steps**
   - `25211901106` hit post-gate variable/runtime issues even though it was effectively a comment-only/short-circuit path.

### Smallest safe mitigations
- Tighten workflow/job-level conditions so irrelevant comment bodies do not spawn separate no-op runs where GitHub syntax allows it.
- Move “no-work” exit paths ahead of heavyweight setup in poller/review flows.
- Mark observability steps as conditional on runtime initialization completion.

### Indicators to track
- `% of clarify/plan/respond runs that are skipped`
- `% of poll cycles with `has_work=false``
- `review_autofix` cancel rate (currently **50 cancelled / 100 total**)
- runner wait share for short workflows (<60s)
- median/95th percentile `workflow_log_analysis` runtime and push-conflict count

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

1. **Retry/watch overhead**
   - `test_and_mark_stable` waits on child analysis workflow for up to ~80 minutes.
   - `review_autofix` check-run polling is configured at **20s** intervals up to **1200s**.
   - **Fix:** adaptive polling and decoupling of non-critical child workflows.

2. **Heavy AI compute in review/autofix**
   - Longest sustained compute comes from `review_autofix` (`1311–2555s` slow runs, including `2009s` and `2025s`).
   - **Fix:** tier reviewer fanout/reasoning and avoid full-panel review for low-risk cases.

3. **Late failure detection in CI**
   - Same flaky unit-test step fails after most of the CI wall time is already spent.
   - **Fix:** move it earlier; fix the test contract.

4. **Queueing overhead on short workflows**
   - Many short runs are dominated by “waiting for a hosted runner,” including `forward_merge_stable_to_main`, `issue_pr_status`, `review` gate/post-merge validate, `orchestrate_poll`, and `copilot_pull_request_reviewer`.
   - **Fix:** consolidate tiny jobs where practical and reduce no-op workflow fanout.

5. **Merge/conflict overhead**
   - `workflow_log_analysis` can do all the work, then fail on final branch write conflict.
   - **Fix:** conflict-proof report publishing.

6. **Repository/setup overhead on no-work paths**
   - `orchestrate_poll` and some review short-circuit paths still perform extra checkout/setup.
   - **Fix:** exit before heavyweight setup when no work is present.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `workflow_log_analysis` is extremely long: avg **4685s**, p50 **4651.5s**, p95 **5465.5s**.
  - `test_and_mark_stable` is dominated by watched child workflows: avg **3760.8s**, p50 **4390.5s**, p95 **4850.2s**.
  - `review_autofix` has a very long tail: avg **306s**, p95 **1502.2s**, with multiple **2000s+** runs.
  - `ci` is steady but expensive: avg **598.3s**, p50 **607s**, p95 **643s**.

- **Top failure modes**
  1. CI failures concentrated in `lint / Implement post-Codex recovery unit tests`.
  2. `workflow_log_analysis` failure in `Commit and push report` due to add/add merge conflict.
  3. `test_and_mark_stable` failure from watching the above child workflow to completion.
  4. Short-circuit review paths with missing telemetry/runtime vars.

- **Highest-cost drivers**
  1. Multi-model/two-pass `review_autofix`
  2. Long `workflow_log_analysis`
  3. Late-failing CI
  4. High volume of no-op orchestrator sub-workflows

- **Top 3 prioritized actions**
  1. **Remove full child-workflow watch from stable-release tests** or switch to a no-push analysis mode.
  2. **Fix and front-load the `Implement post-Codex recovery` tests**.
  3. **Make report publishing conflict-proof** and non-blocking for parent validation workflows.

## Metrics Appendix

### Repository summary

| Repository | Total runs | Success | Failure | Cancelled | Other/skipped | Failure rate |
|---|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 273 | 26 | 57 | 644 | 2.6% |

### Duration summary

| Scope | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|
| All sampled runs | 127.2 | 1.0 | 613.0 |
| `ci` | 598.3 | 607.0 | 643.0 |
| `review_autofix` | 306.0 | 37.5 | 1502.2 |
| `workflow_log_analysis` | 4685.3 | 4651.5 | 5465.5 |
| `test_and_mark_stable` | 3760.8 | 4390.5 | 4850.2 |
| `orchestrate_poll` | 80.8 | 45.0 | 246.6 |

### Reliability summary by key workflow family

| Workflow family | Total runs | Success | Failure | Cancelled | Failure rate |
|---|---:|---:|---:|---:|---:|
| `ci` | 69 | 50 | 19 | 0 | 27.5% |
| `review_autofix` | 100 | 50 | 0 | 50 | 0.0% failure, 50.0% cancelled |
| `workflow_log_analysis` | 4 | 3 | 1 | 0 | 25.0% |
| `test_and_mark_stable` | 4 | 0 | 2 | 2 | 50.0% |
| `implement` | 173 | 25 | 3 | 5 | 1.7% |

### Observed token/cache metrics

| Workflow / run | Metric | Observed value |
|---|---|---|
| `workflow_log_analysis` / `25208727402` | summarizer model | `openai/gpt-5.4-mini` |
| `workflow_log_analysis` / `25208727402` | summarized runs | 83 |
| `workflow_log_analysis` / `25208727402` | targeted runs | 100 |
| `workflow_log_analysis` / `25208727402` | tokens used | 160,232 |
| `review_autofix` / `25208887475` | cache enabled | true |
| `review_autofix` / `25208887475` | cache creation/read token metrics | `na` / `na` |
| `review_autofix` / sampled long runs | reviewer models | 6 configured |
| `review_autofix` / sampled long runs | reasoning | reviewer/editor `xhigh`, summarizer `medium` |

### AI memory metrics

| Metric | Value |
|---|---:|
| JSON telemetry events observed | 58 |
| Retrieve operations | 8 |
| Retrieve hit rate | 25.0% |
| Avg retrieve `estimated_tokens` | 7.0 |
| Reviewer retrieve hits | 0 / 6 |
| Implementation retrieve hits | 2 / 2 sampled |
| `fail_open: true` entries | 0 |
| `enabled: false` entries | 0 |
| Memory push attempts >1 | 0 observed |

### GH API summary

| Workflow / step | Observed API pattern | Approx. volume | Main issue |
|---|---|---:|---|
| `test_and_mark_stable` / `Dispatch & watch — workflow-log-analysis` (`25208710605`) | `gh api` run-status polling every 15s | ~320+ status calls on one child run | highest redundancy |
| `copilot_pull_request_reviewer` / `Prepare_Get_pull_request_details` (`25211915189`) | `pulls.get` + paginated `pulls.listFiles` | 2 core reads/run | reusable but currently repeated by run |
| `copilot_pull_request_reviewer` / `Cleanup_artifacts_Get_artifact_IDs` (`25211915189`) | list artifacts for current run | 1 call/run | low volume, can reuse IDs |
| `review_autofix` / post-merge validate dispatch (`25209053473` summary) | `gh workflow run` + `gh issue edit` in loop | depends on linked issues | batching opportunity |
| `cancel_on_pr_close` (`25212161794`) | `/rate_limit` + cancel POST under retry helper | bounded | healthy pattern |

If you want, I can turn this into a shorter “top 10 changes” action list sorted by effort vs impact.
