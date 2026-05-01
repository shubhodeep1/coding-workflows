## Executive Summary

- **The dominant end-to-end bottlenecks are `workflow_log_analysis` and `review_autofix`, not CI.** `workflow_log_analysis` averaged **4669.8s** over 5 runs, and `review_autofix` had a **p95 of 1943.1s** with worst sampled successes at **2776s**, **2555s**, **2025s**, and **2009s**. Estimated impact from fixing these two families first: **30–60% reduction in full release-cycle latency**. **Confidence: high**
- **A single report-publish race is cascading into parent workflow failures.** `workflow_log_analysis` run **25208727402** failed in **Commit and push report** after an `add/add` rebase conflict on `analysis/workflow-optimization-2026-05-01-4.md`; parent `test_and_mark_stable` run **25208710605** then spent ~**80 minutes** polling and failed when that child concluded failure. Estimated impact from making report publish collision-safe and fail-open: **eliminate a major class of false-red release tests**. **Confidence: high**
- **Review/editor stages are over-provisioned for the common case.** Long `review_autofix` runs used **6 reviewer models**, `ENABLE_REVIEWER_TWO_PASS=true`, and both reviewer/editor reasoning at **`xhigh`**; in `test_and_mark_stable` run **25212177682**, the editor sat idle for **724s** before failing bait-line removal. Estimated impact from tiering reviewer fanout/reasoning and adding earlier stall cutoffs: **multi-minute savings per review run plus lower token burn**. **Confidence: medium-high**
- **GH API polling is materially redundant in watcher paths.** In `test_and_mark_stable` run **25208710605**, the `orphan-workflows-test` watcher emitted **318 status lines** for a single child `workflow_log_analysis` run before it failed. Estimated impact from backoff-aware phase polling: **60–80% fewer API status checks** on long child runs and lower rate-limit exposure. **Confidence: high**
- **Prompt cache is enabled but not yet measurable where it matters.** `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears across implement/review/poll runs, but sampled long review runs logged cache usage as **`prompt_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`**. Estimated impact from stabilizing prompt prefixes and fixing cache telemetry: **meaningful but currently unquantified token and latency savings**. **Confidence: medium**
- **AI memory is functioning, but reviewer retrieval quality is weak in the sampled window.** Across observed JSON telemetry, `retrieve` hit rate was only **40%** (2/5), with **3/5 retrieves returning 0 records**, mostly in reviewer flows; however all observed write operations succeeded with **`push_attempts=1`** and no `fail_open:true` or `enabled:false` entries. Estimated impact from improving reviewer retrieval signals: **modest cost/quality gains, low reliability risk**. **Confidence: medium**

## Speed Optimizations

1. **Cut `workflow_log_analysis` publish-loop overhead and fail parent tests open on report-publish errors**
   - **Evidence:** `workflow_log_analysis` averages **4669.8s**; failed run **25208727402** spent **4834s** and died at `analyze-commit-notify / Commit and push report` after `git pull --rebase` hit an `add/add` conflict on `analysis/workflow-optimization-2026-05-01-4.md`. Parent `test_and_mark_stable` run **25208710605** then failed at `orphan-workflows-test / Dispatch & watch — workflow-log-analysis`.
   - **Root cause:** Long-running child analysis plus non-unique report path plus blocking parent assertion on child success.
   - **Exact change:**  
     - Make report filenames collision-proof using the child **run ID** or full timestamp, not a date-indexed path.  
     - `git pull --rebase` **before** generating the report, not after local commit creation.  
     - In parent release tests, treat report publication as advisory: assert that analysis ran and produced artifact/output, not necessarily that it pushed a branch commit.
   - **Estimated time savings:** **~60–80 minutes** on failed release-test cycles that currently wait for child completion and then fail for publish-only reasons.
   - **Implementation risk:** **Low**

2. **Reduce review/autofix critical-path duration by tiering reviewer fanout and reasoning**
   - **Evidence:** `review_autofix` worst successful runs were **25212403764 = 2776s**, **25207020260 = 2555s**, **25208956190 = 2025s**, **25208887475 = 2009s**, **25212344500 = 1570s**. Sampled logs show `REVIEWER_MODELS` with **6 models**, `ENABLE_REVIEWER_TWO_PASS=true`, `REVIEWER_REASONING_EFFORT=xhigh`, `EDITOR_REASONING_EFFORT=xhigh`.
   - **Root cause:** Heavy multi-model review stack is applied even when the PR does not need maximal scrutiny.
   - **Exact change:**  
     - Keep current settings only for high-risk diffs or explicit force-review markers.  
     - For single-file/small-diff/non-workflow changes, reduce to **2–3 reviewer models**, disable second pass, and drop reviewer/editor reasoning from `xhigh` to `high` or `medium`.
   - **Estimated time savings:** **8–20 minutes** per long review run; **30–60%** on the family’s critical path.
   - **Implementation risk:** **Medium** due to review-quality tradeoff; deploy behind diff-size and file-type gates.

3. **Add early stall cutoffs inside editor/reviewer loops instead of waiting near full timeout**
   - **Evidence:** In `test_and_mark_stable` run **25212177682**, `e2e-smoke-test` logged reviewer/model idle growth from **210s** through **724s** while in `Run reviewer models` and especially `Apply fixes with editor model`, then failed at `Phase 4b: Verify editor removed bait line`. Final error: `Editor failed to remove bait line E2E_EDITOR_BAIT_25212177682`.
   - **Root cause:** The loop waits a long time for non-productive model phases before concluding failure.
   - **Exact change:**  
     - If editor phase shows **no diff change** after N heartbeat intervals, abort early and retry once with a narrower single-file repair prompt.  
     - Add a hard “no-progress” cutoff well below the 30-minute phase timeout.
   - **Estimated time savings:** **10–20 minutes** on stalled review/editor failures; also shortens feedback to the orchestrator.
   - **Implementation risk:** **Low-Medium**

4. **Short-circuit `orchestrate_poll` before full repository fetch when no active work exists**
   - **Evidence:** `orchestrate_poll` successful no-work runs such as **25215312050** and **25214458964** finished in **45–48s**, but still used `actions/checkout@v5` with **`fetch-depth: 0`** and full ref/tag fetches. Sampled logs show poll completion with `has_work: false`.
   - **Root cause:** Full repository checkout happens before confirming whether any active tracking issue actually needs processing.
   - **Exact change:**  
     - Query active tracking issues first via GH API.  
     - Skip full checkout/support-source checkout when no issues are returned.
   - **Estimated time savings:** **20–35s** per no-work poll run.
   - **Implementation risk:** **Low**
   - **Type:** Critical-path improvement for idle-control cycles, not a local micro-optimization.

5. **Stop full-graph fetches in short administrative workflows**
   - **Evidence:** `forward_merge_stable_to_main` run **25215478144** was only **16s**, but still used `git fetch origin stable main`; `orchestrate_poll` and `workflow_log_analysis` used broad ref fetches (`+refs/heads/*`, `+refs/tags/*`) and loaded large branch/tag sets.
   - **Root cause:** Broad fetch defaults in workflows that only need a small ref subset.
   - **Exact change:**  
     - Replace broad fetches with targeted branch/tag fetches.  
     - Avoid `fetch-depth: 0` unless history is truly required.
   - **Estimated time savings:** **5–15s** per admin/poller run.
   - **Implementation risk:** **Low**
   - **Type:** Local micro-optimization unless applied to high-frequency pollers.

## Cost Optimizations

1. **Shrink `workflow_log_analysis` summarization scope**
   - **Evidence:** `summarize_unselected_runs` telemetry reported:  
     - run **25208727402**: **160,232 tokens** for **83** summarized runs  
     - run **25212191835**: **206,255 tokens** for **85** runs  
     - other sampled runs: **186,487**, **171,203**, **304,169** tokens  
     Across 5 observed runs: **1,028,346 total tokens**, **205,669 avg/run**.
   - **Root cause:** Summarizing up to **100** unselected runs is expensive relative to the marginal value of low-signal/no-op runs.
   - **Exact change:**  
     - Lower `WORKFLOW_LOG_SUMMARY_MAX_RUNS` from 100 to a smaller cap for routine runs.  
     - Exclude **0–2s** skip-only runs unless they contain warnings, telemetry, or failure indicators.  
     - Prioritize failed, cancelled, slow, warning-bearing, and token/API-bearing runs.
   - **Estimated savings:** **80k–150k tokens per analysis run**.
   - **Quality-risk notes:** **Low** if failure/slow/warning runs remain prioritized.

2. **Tier review model fanout by risk instead of always using the full reviewer set**
   - **Evidence:** Long `review_autofix` runs use 6 reviewer models plus two-pass review and `xhigh` reasoning. Deterministic-skip review runs such as **25212476286** and **25212344024** show the system already has gating logic for low-risk changes.
   - **Root cause:** Expensive review stack is applied too broadly after gate success.
   - **Exact change:**  
     - Use docs-only/small-diff/file-class heuristics to choose reviewer tier.  
     - Reserve full 6-model, two-pass, `xhigh` mode for workflow/script/core orchestration diffs.
   - **Estimated savings:** **30–60%** review token cost on non-critical PRs.
   - **Quality-risk notes:** **Medium**; keep override path for risky changes.

3. **Reduce repeated prompt/context expansion in implement retries**
   - **Evidence:** Failed implement run **25208345846** repeatedly logged large Serena/tooling instruction blocks and multiple retry loops before bailing with `2 consecutive attempts with no actionable output`; `No Serena tool usage stats found` was printed at the end.
   - **Root cause:** Retry attempts appear to resend large policy/context blocks even for a one-file, one-line change.
   - **Exact change:**  
     - Cache static instruction prefix once per run.  
     - Retry with a delta prompt (“what changed since last attempt / why prior attempt failed”) rather than full prompt re-expansion.  
     - Add a fast path for narrowly scoped tasks.
   - **Estimated savings:** **Modest-to-medium** per implement failure or retry-heavy run.
   - **Quality-risk notes:** **Low** if static policy prefix remains unchanged.

4. **Fix prompt-cache fragmentation and telemetry blindness**
   - **Evidence:** Long review runs logged cache probe lines like `prompt_tokens=na`, `completion_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na` despite `OPENROUTER_PROMPT_CACHE_DISABLED=false`.
   - **Root cause:** Cache is enabled but prompt structure and/or telemetry capture are unstable.
   - **Exact change:**  
     - Keep system/policy prefix stable.  
     - Move volatile run metadata, timestamps, PR details, and dynamic diagnostics later in the prompt.  
     - Make real cache counters mandatory for actual editor/reviewer calls, not just probes.
   - **Estimated savings:** **Unquantified but likely meaningful** on repeated reviewer/editor prompts.
   - **Quality-risk notes:** **Low**

5. **Lower no-work poll cost before any model/judge preparation**
   - **Evidence:** `orchestrate_poll` no-work runs still export `MODEL_EDITOR: openai/gpt-5.4`, `MODEL_REASONING_EFFORT_JUDGE: xhigh`, create prompt file paths, and perform full checkout/setup before concluding `has_work: false`.
   - **Root cause:** Expensive setup path starts before confirming work exists.
   - **Exact change:** Move no-work GH query to the very front and skip model/prompt/bootstrap setup when there is nothing to process.
   - **Estimated savings:** Mostly **latency/runtime**, with some token avoidance if judge invocation currently occurs on some no-work paths.
   - **Quality-risk notes:** **Low**

## Reliability Improvements

1. **Make `workflow_log_analysis` report publication collision-safe**
   - **Failure evidence:** Run **25208727402** failed at `analyze-commit-notify / Commit and push report` after:
     - creating `analysis/workflow-optimization-2026-05-01-4.md`
     - `git pull --rebase origin main`
     - `CONFLICT (add/add)` on the same report path
   - **Root cause category:** Concurrent write / branch rebase conflict.
   - **Exact fix:**  
     - Generate unique report filenames using run ID.  
     - Pull/rebase before rendering.  
     - On push conflict, upload artifact and finish with warning instead of hard failure.
   - **Expected reliability impact:** Should remove one of the highest-cost false failures and prevent parent `test_and_mark_stable` red runs caused only by report publication.
   - **Rollback/fail-open considerations:** Safe to fail open; report artifact is enough for auditability.

2. **Stabilize implement retry handling for empty-output loops**
   - **Failure evidence:** Implement run **25208345846** failed in `implement / implement / Run Codex implementation` with:
     - `Codex returned empty output on attempt 2`
     - `Codex returned empty output on attempt 3`
     - `Codex produced no actionable output 2 attempts in a row`
     - `Posted Codex implement failure diagnostics to issue #1869`
   - **Root cause category:** AI execution loop stall / prompt-response mismatch.
   - **Exact fix:**  
     - Add a deterministic narrow-task fast path for single-file edits.  
     - After first empty attempt, switch to a shorter “repair-only” prompt instead of reusing the full stack.  
     - If no Serena usage stats exist, log whether Serena was unavailable vs merely unused.
   - **Expected reliability impact:** Should reduce `implement` hard failures and downstream follow-on failures in review/release tests.
   - **Rollback/fail-open considerations:** Keep current diagnostics posting if fallback path also fails.

3. **Fix red CI loop caused by message-contract test drift**
   - **Failure evidence:** **18** CI failures in this window; failed runs such as **25208317081**, **25208681700**, **25208929558** all failed in `lint / Implement post-Codex recovery unit tests`. Deep-dive run **25208317081** ended with two failing tests:
     - `test_codex_empty_output_streak_bail_and_flag`
     - `test_failure_diagnostics_posted_to_source_issue`
   - **Root cause category:** Test/code contract drift.
   - **Exact fix:**  
     - Move expected failure-message strings into shared constants used by both workflow logic and tests.  
     - Run the impacted test shard before merging changes to implement failure wording.
   - **Expected reliability impact:** Could eliminate the bulk of the **35.3% CI family failure rate** in the sampled window.
   - **Rollback/fail-open considerations:** None needed; this is a correctness alignment fix.

4. **Make parent E2E release tests fail-open on non-critical child-analysis errors**
   - **Failure evidence:** `test_and_mark_stable` run **25208710605** failed at `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` solely because child `workflow_log_analysis` concluded failure; `soft-error-report-25208710605-workflow_log_analysis` artifact was still produced successfully.
   - **Root cause category:** Over-strict dependency on advisory child workflow.
   - **Exact fix:**  
     - Distinguish “child executed and produced artifact” from “child published branch report successfully.”  
     - Keep the child result visible in summary/artifacts, but do not fail the release test on report-publication-only faults.
   - **Expected reliability impact:** Reduces false-negative release tests without hiding real analysis failures.
   - **Rollback/fail-open considerations:** Child artifact and logs preserve debuggability.

5. **Harden support-source checkout fallback paths**
   - **Failure evidence:** Multiple workflows logged support-source fallback warnings, e.g. recent `issue_pr_status` run **25215455855**: `Support checkout ref ${script_ref} is unavailable; using main.` Review/implement logs also contain `Failed to checkout workflow support source from ${SCRIPT_REF} or main` scaffolding.
   - **Root cause category:** Support ref drift / bootstrap source resolution.
   - **Exact fix:**  
     - Validate support ref existence before downstream stages begin.  
     - Cache resolved fallback ref once and reuse it across child jobs in the same run.
   - **Expected reliability impact:** Lowers intermittent bootstrap and missing-support-file failures.
   - **Rollback/fail-open considerations:** Continue current fallback-to-main behavior, but make it explicit and shared.

## AI Memory Health

- **Memory telemetry was observed** in sampled deep-dive logs, with **51 valid JSON telemetry events** across **19 log files**.
- **Observed operation mix:** `record-run-event` (24), `retrieve` (5), `processed-command-check` (4), `processed-command-claim` (4), `record-candidate` (2), `compact` (2), `summarize_unselected_runs` (10).
- **Retrieve health:**
  - **Hit rate:** **40%** (`2/5` retrieves had `records_selected > 0`)
  - **Average `estimated_tokens`:** **11.2**
  - **Keyword method distribution:** `plain` = **2**, `none` = **3**, `llm` = **0**
- **Flags:**
  - **Zero-record retrieves:** **3/5**, all in sampled reviewer contexts
  - **`fail_open: true`:** **0 observed**
  - **`enabled: false`:** **0 observed**
  - **High push retry counts:** **none observed**; all sampled writes showed **`push_attempts=1`**
- **Positive signal:** Memory maintenance run **25214288925** compacted **2914 archived candidates** for month `2026-04` and pushed successfully.
- **Concern:** Reviewer-side retrieval is weak in this sample. The three zero-hit retrieves all came from reviewer contexts, suggesting the retrieval keys or memory selection strategy are not yet helping the most expensive phase.
- **Telemetry hygiene issue:** The sampled corpus also contained many non-JSON strings prefixed as `AI_MEMORY_TELEMETRY:` inside generated analysis content, which makes downstream parsing noisy. The emitter appears functional, but analyzer-facing formatting should stay strictly JSON after the prefix.

## GH API Call Audit

1. **`test_and_mark_stable` child-workflow polling is the biggest redundant API pattern**
   - **Evidence:** In run **25208710605**, `orphan-workflows-test / Dispatch & watch — workflow-log-analysis` logged **318 status lines** before the child finished with failure at **10:14:51Z**. Poll cadence is roughly **15s**.
   - **Pattern:** Long-lived per-run status polling against the same child workflow.
   - **Recommendation:**  
     - Use phase-aware polling: slow poll interval while `pending`, modest interval while `in_progress`, and cap maximum checks.  
     - Stop early once the child enters a known terminal failure class.  
     - Reuse the child run ID/result across soft-error analysis rather than re-querying independently.
   - **Estimated call-count reduction:** **60–80%** on long child runs.
   - **Rate-limit risk reduction:** **High**

2. **`copilot_pull_request_reviewer` repeats metadata/artifact lookups across jobs**
   - **Evidence:** Recent run **25215416590**:
     - `Prepare` called `github.rest.pulls.get`
     - then `github.paginate(github.rest.pulls.listFiles, per_page: 100)`
     - later `Cleanup artifacts` called `gh api /repos/.../actions/runs/25215416590/artifacts`
   - **Pattern:** Separate jobs re-fetch adjacent run/PR metadata instead of passing it through outputs/artifacts.
   - **Recommendation:**  
     - Emit PR metadata and file list as a single JSON artifact/output from `Prepare`.  
     - Pass artifact IDs directly from producer step to cleanup.
   - **Estimated call-count reduction:** **1–3 API calls per review run**
   - **Rate-limit risk reduction:** **Low-Medium**

3. **Short admin workflows use healthy retry wrappers, but still probe `/rate_limit` per run**
   - **Evidence:** `cancel_on_pr_close` runs **25215455847**, **25212488684**, **25212363916** use `_gh_retry` plus `/rate_limit` probes and cancel POSTs; no 429s were observed.
   - **Pattern:** Good hygiene already present; low redundancy.
   - **Recommendation:** Keep current behavior. Only consider caching one `/rate_limit` result per workflow invocation if logs later show higher volume.
   - **Estimated call-count reduction:** Small
   - **Rate-limit risk reduction:** Small

4. **Repository-local API hygiene rules are mostly being followed, but one watcher path still violates the spirit**
   - **Evidence:** Generated audit content inside `workflow_log_analysis` explicitly states cycle-local caches and batching are required, yet the long child-watch path still performs repetitive single-run polling.
   - **Recommendation:** Apply the repo’s own batching/prefetch/fail-open rules to workflow-child watchers, especially in `test_and_mark_stable` and report analyzers.

## MCP & Serena Efficiency

- **Observed reality:** The sampled logs contain many **instructions about how Serena should be used**, but very little proof that Serena/MCP produced measurable efficiency gains in the expensive runs.
- **Evidence of weak observability:**
  - Implement failure **25208345846** ended with: `No Serena tool usage stats found (Serena may not have been used or stats not recorded).`
  - The analysis workflow also surfaced `No Serena tool usage stats found` for long review runs.
  - Review run scaffolding includes `SERENA_REPORT_FILE`, `SUPPORT_SERENA_DIR`, and `serena_efficiency_report.py`, but sampled logs do not expose concrete per-tool counts or repeated-region-read stats.
- **Likely inefficiency:** Prompt policy about Serena is repeated heavily across retries in `implement` logs, which adds context weight even when no Serena stats are later produced.
- **Concrete recommendations:**
  1. **Always emit `tool_usage_stats.json` or a compact summary** for sampled implement/review runs.
  2. **Do not repeat Serena policy blocks on each retry**; include once in a stable prefix and reuse.
  3. **Only generate Serena efficiency reports when there was actual Serena activity**; otherwise log a lightweight `unused/unavailable` state.
  4. **Parallelize safe read-only metadata fetches** in review prep: PR details, changed files, and support-file presence checks can run concurrently before edit/model phases begin.
- **Expected impact:** Primarily **token efficiency and turnaround** via less prompt churn; direct Serena gains are currently **unproven** because telemetry is incomplete.

## Prompt Cache & Memory System

- **Prompt cache status:** Enabled in sampled runs (`OPENROUTER_PROMPT_CACHE_DISABLED=false`), but **effective hit/miss behavior is not measurable** in the highest-cost paths.
- **Evidence:**
  - Long `review_autofix` runs logged cache probes with:
    - `prompt_tokens=na`
    - `completion_tokens=na`
    - `total_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`
  - Implement/review flows repeatedly printed long prompt-assembly and policy blocks across retries.
- **Cache-fragmentation causes likely present:**
  - Dynamic run IDs, issue bodies, branch names, and diagnostics are likely mixed too early into prompts.
  - Retry loops appear to re-send large static instructions.
- **Memory retrieval effectiveness:**
  - Implementation retrievals succeeded in the sampled cases (`records_selected=1`, `estimated_tokens=28`).
  - Reviewer retrievals were mostly misses (`records_selected=0` in 3 sampled reviewer retrieves).
- **Concrete improvements:**
  1. **Stabilize prompt prefixes**: keep policy/system/core workflow instructions in a fixed header.
  2. **Append volatile metadata later**: run IDs, issue text, timestamps, and retry diagnostics should come after the cacheable prefix.
  3. **Emit real call-level cache counters** for editor/reviewer requests, not just probes.
  4. **Use role-specific memory retrieval keys**: current reviewer retrieval looks underpowered relative to implementation retrieval.
- **Estimated impact:**
  - **Tokens:** medium savings once cacheability is real and measurable
  - **Latency:** medium on repeated review/editor prompts
  - **Reliability:** low direct effect, but better telemetry improves safe tuning

## Orchestrator Health

- **Clarify/plan gates are cheap and generally healthy.**
  - `clarify`: **216 total**, **26 success**, **190 other/skipped**, **p50 1s**
  - `plan`: **185 total**, **21 success**, **164 other/skipped**, **p50 1s**
- **Issue:** The orchestrator fan-out generates many no-op workflow runs. These are individually cheap, but they create queue noise and hosted-runner wait across the control plane.
- **Evidence:** Many recent clarify/plan/respond/implement runs were skipped in **0–2s**, often because comment bodies did not match `/answer` or `/approved`. Meanwhile many short workflows still logged hosted-runner wait.
- **Recurring pain points:**
  1. **Queueing noise from skip-only workflows**
  2. **Long-running review/editor loops without earlier no-progress termination**
  3. **Child-workflow dependency chains where advisory tasks can fail the orchestrator path**
- **Smallest safe mitigations:**
  - Pre-gate more aggressively before dispatching downstream workflows.
  - Collapse some skip-only branches into a lighter coordinator step.
  - Track and alert on:
    - skip-only run count by family
    - runner wait seconds by family
    - review/editor idle seconds before success/failure
    - child-workflow poll count per parent run
- **Observable indicators to track next:**
  - `review_autofix` p50/p95
  - `workflow_log_analysis` token usage per run
  - `test_and_mark_stable` false-failure rate
  - no-work `orchestrate_poll` median duration
  - skipped-run volume for `clarify`, `plan`, and `orchestrate_clarify_respond`

## Pipeline Flow Bottlenecks

1. **Review/autofix compute is the main execution bottleneck**
   - Longest successful `review_autofix` runs: **2776s**, **2555s**, **2025s**, **2009s**
   - Dominated by reviewer/editor model stages, not by setup alone.

2. **Release-test watcher chains amplify downstream latency**
   - `test_and_mark_stable` average duration: **4007s**
   - Failures were at:
     - `Phase 4: Wait for review & autofix to complete` (**25204168842**)
     - `Phase 4b: Verify editor removed bait line` (**25212177682**)
     - `Dispatch & watch — workflow-log-analysis` (**25208710605**)

3. **Analysis/reporting child workflows are too heavy for their current role**
   - `workflow_log_analysis` average: **4669.8s**
   - It is both expensive itself and capable of failing its caller for non-core reasons like report-push conflicts.

4. **CI is slower than implement, but not the top end-to-end blocker**
   - `ci` avg **597.9s**, p50 **605s**, p95 **642s**
   - It is a consistent 10-minute wall but smaller than review/release bottlenecks.

5. **Queueing overhead affects many “short” workflows**
   - Runner wait appears in `forward_merge_stable_to_main`, `issue_pr_status`, `orchestrate_poll`, `copilot_pull_request_reviewer`, `review_autofix`, and `ci`.
   - For several 10–60s workflows, runner wait is a large share of total elapsed time.

**Ordered fixes by end-to-end impact:**
1. Fix `workflow_log_analysis` publish collisions and make parent release tests fail-open on advisory publish errors.
2. Tier `review_autofix` reviewer fanout/reasoning.
3. Add no-progress abort/retry logic to editor/reviewer loops.
4. Short-circuit no-work `orchestrate_poll` before checkout.
5. Reduce skip-only orchestrator workflow dispatches.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `workflow_log_analysis` avg **4669.8s**
  - `test_and_mark_stable` avg **4007.0s**, **60% failure rate**
  - `review_autofix` p95 **1943.1s**
  - `ci` steady ~**10 minutes**
- **Top failure modes**
  - Report commit/push conflict in `workflow_log_analysis` (**25208727402**)
  - Editor/review stage stall and bait-line verification failure in `test_and_mark_stable` (**25212177682**)
  - Implement empty-output retry-loop failure (**25208345846**)
  - CI contract-test drift causing repeated red runs (**18 CI failures**, mostly same step)
- **Highest-cost drivers**
  - `summarize_unselected_runs` token usage of **160k–304k** per sampled analysis run
  - 6-model, two-pass, `xhigh` review configuration on long `review_autofix` runs
  - Full checkout/fetch on no-work poller paths
- **Top 3 prioritized actions**
  1. **Make `workflow_log_analysis` publishing collision-safe and non-blocking to parent release tests**
  2. **Tier `review_autofix` reviewer fanout/reasoning by risk**
  3. **Fix implement empty-output retry handling plus shared message constants for CI-stabilizing tests**

## Metrics Appendix

### Overall run summary

| Scope | Total runs | Success | Failure | Cancelled | Other | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 262 | 25 | 29 | 684 | 117.2 | 1.0 | 596.0 |

### Workflow family summary

| Workflow family | Total | Success | Failure | Cancelled | Other | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 51 | 33 | 18 | 0 | 0 | 597.9 | 605.0 | 642.0 |
| implement | 185 | 28 | 3 | 6 | 148 | 44.0 | 1.0 | 246.0 |
| review_autofix | 64 | 43 | 0 | 21 | 0 | 320.1 | 33.0 | 1943.1 |
| orchestrate_poll | 12 | 12 | 0 | 0 | 0 | 72.2 | 46.0 | 196.9 |
| test_and_mark_stable | 5 | 0 | 3 | 2 | 0 | 4007.0 | 4500.0 | 4976.0 |
| workflow_log_analysis | 5 | 4 | 1 | 0 | 0 | 4669.8 | 4608.0 | 5428.4 |
| copilot_pull_request_reviewer | 24 | 24 | 0 | 0 | 0 | 113.5 | 94.5 | 210.2 |
| plan | 185 | 21 | 0 | 0 | 164 | 22.1 | 1.0 | 176.4 |
| clarify | 216 | 26 | 0 | 0 | 190 | 15.7 | 1.0 | 111.3 |
| orchestrate | 5 | 5 | 0 | 0 | 0 | 229.0 | 240.0 | 249.2 |

### Slowest sampled runs

| Workflow family | Run ID | Conclusion | Duration (s) | Notable point |
|---|---:|---|---:|---|
| workflow_log_analysis | 25208157500 | success | 5577 | Longest analysis run |
| test_and_mark_stable | 25212177682 | failure | 4992 | Failed bait-line verification |
| test_and_mark_stable | 25208710605 | failure | 4912 | Failed child workflow-log-analysis watch |
| workflow_log_analysis | 25208727402 | failure | 4834 | Failed report commit/push |
| workflow_log_analysis | 25212191835 | success | 4608 | Successful but still very long |
| review_autofix | 25212403764 | success | 2776 | Long review/editor path |

### Token and cache telemetry observed

| Workflow / Run | Metric | Value |
|---|---|---:|
| workflow_log_analysis / 25208727402 | summarize_unselected_runs tokens_used | 160,232 |
| workflow_log_analysis / 25212191835 | summarize_unselected_runs tokens_used | 206,255 |
| workflow_log_analysis / 25206805901 | summarize_unselected_runs tokens_used | 186,487 |
| workflow_log_analysis / 25208157500 | summarize_unselected_runs tokens_used | 171,203 and 304,169 observed in sampled logs |
| 5 sampled workflow_log_analysis runs | Total summarize_unselected_runs tokens | 1,028,346 |
| 5 sampled workflow_log_analysis runs | Avg summarize_unselected_runs tokens | 205,669 |
| review_autofix / 25212403764 | prompt cache enabled | true |
| review_autofix / 25212403764 | prompt/cache token counters | `na` / `na` / `na` |
| implement / 25208345846 | memory retrieve estimated_tokens | 28 |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Valid JSON memory events observed | 51 |
| Retrieve events | 5 |
| Retrieve hit rate | 40% |
| Avg retrieve estimated_tokens | 11.2 |
| Retrieve keyword_method = plain | 2 |
| Retrieve keyword_method = none | 3 |
| Retrieve keyword_method = llm | 0 |
| Retrieve zero-record responses | 3 |
| `fail_open:true` observed | 0 |
| `enabled:false` observed | 0 |
| Observed push_attempts > 1 | 0 |

### GH API call summary

| Workflow / Run | Pattern | Observed volume | Risk |
|---|---|---:|---|
| test_and_mark_stable / 25208710605 | Child workflow status polling | 318 status lines for one child run | High redundancy |
| copilot_pull_request_reviewer / 25215416590 | `pulls.get` + paginated `pulls.listFiles` | 2 core PR metadata reads | Moderate |
| copilot_pull_request_reviewer / 25215416590 | List artifacts then delete per artifact | 1 list + N delete calls | Low-Moderate |
| cancel_on_pr_close / recent runs | `/rate_limit` + cancel POST with retry | Bounded, healthy | Low |
| orchestrate_poll / 25215312050 | `gh issue list` plus full repo checkout/fetch | API okay; git fetch heavier than API | Low API, medium runtime |


## Deep Audit — Workflows & Scripts (2026-05-01)

### Section 1: Bug & Correctness Sweep

- **ID** — BUG-001  
  **File path** — `scripts/tg_helpers.sh:167-206,240-277`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — `tg_store_msg_id()` and `tg_store_phase_msg_id()` both implement a read-modify-write append on a single GitHub tracking comment: they fetch recent comments, select the first matching marker comment, append the new Telegram ID with `sed`, and `PATCH` the whole body back. That sequence is not concurrency-safe. If two workflows send tracked Telegram messages for the same issue at nearly the same time, both can read the same old body and the later `PATCH` will overwrite the earlier append, permanently losing one message ID. Cleanup then relies on the preserved marker list in `tg_cleanup_msgs()` / `tg_cleanup_phase_msgs()`, so a lost ID becomes an orphaned Telegram message.  
  **Recommended fix** — Stop using a shared append-only comment as the source of truth. Preferred: write one GitHub tracking comment per Telegram message ID/phase and let cleanup sweep all matching comments. If the single-comment design must remain, add a retry loop that re-reads the current marker comment immediately before patching, merges IDs uniquely, and retries on mismatch.

- **ID** — BUG-002  
  **File path** — `.github/workflows/issue_pr_status.yml:253-350,501-518`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The workflow correctly classifies linked issues as tracking vs. managed vs. standalone in the “Update linked issue labels when PR closes” step using labels plus body markers, but the later “Send PR merged Telegram alert” step discards that classification and only treats an issue as orchestrated if its body contains the literal `Managed by: AI Orchestrator`. That misses `ai:orchestrator-tracking` issues entirely and can also miss managed issues whose body marker changes or is absent. The step comment says orchestrator issues should not emit this alert because the poller owns completion alerts, but the implementation no longer matches that rule.  
  **Recommended fix** — Export the earlier `TRACKING_ISSUES` / `MANAGED_ISSUES` classification to `GITHUB_ENV` or a JSON artifact and reuse it in the alert step. If the step must stay independent, reuse the same batched GraphQL classification logic instead of body-only detection.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — API-001  
  **File path** — `scripts/review_rb_judge.sh:146-170`  
  **Severity** — Low  
  **Category tag** — `api-redundancy`  
  **Description** — The judge resolves all linked issue numbers, then loops through every issue and fetches each body with `_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}"`, but only `FIRST_ISSUE_BODY` is ever consumed. After the first non-empty body is captured, every later `GET /issues/{n}` is redundant. Current call count is **N** issue-body fetches for **N** linked issues; proposed call count is **1**.  
  **Recommended fix** — Break the loop once `FIRST_ISSUE` and `FIRST_ISSUE_BODY` are populated, or fetch only the first linked issue body. No batching helper is required for the current use case; if future judge context needs multiple issue bodies, extend the aliased GraphQL batching pattern already used elsewhere in `scripts/orchestrate_poll_process.sh`.

- **ID** — BATCH-001  
  **File path** — `.github/workflows/review_autofix.yml:478-530`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — In `post-merge-validate-dispatch`, the fast path uses GraphQL to fetch closing issues with labels, but the fallback path extracts issue numbers from PR title/body and then does a per-issue `gh issue view ... --json labels` inside the loop whenever `labels_known != "true"`. Current call count on the fallback path is **1** PR fetch (`GET /pulls/{n}`) + **N** issue-label fetches; proposed call count is **1** PR fetch + **1** aliased GraphQL batch for all extracted issue numbers.  
  **Recommended fix** — After regex extraction, batch-resolve labels for the extracted issue numbers in one GraphQL call before entering the loop. Reuse the same aliasing pattern already implemented in `.github/workflows/issue_pr_status.yml:286-320` and mirrored in `scripts/orchestrate_poll_process.sh`’s batch-label helpers.

- **ID** — BATCH-002  
  **File path** — `.github/workflows/issue_pr_status.yml:280-350,501-518`  
  **Severity** — Low  
  **Category tag** — `api-batching`  
  **Description** — This is the API-efficiency side of **BUG-002**. The workflow already spends **1** batched GraphQL call (plus per-issue fallback only on batch failure) to classify orchestrator tracking/managed issues in the close-handling step, but the later merged-alert step still performs **N** per-issue body fetches to rediscover whether the issues are orchestrated. Current alert-step call count is **N**; proposed call count is **0 additional** if the earlier classification is exported, or **1** batched GraphQL call if the alert step must remain isolated.  
  **Recommended fix** — Persist the earlier classification to `GITHUB_ENV` / step outputs and consume it in the alert step. If isolation is required, reuse the same `ORCH_ALIAS_FRAGMENT` batched query pattern instead of looping over `_safe_gh_jq "repos/.../issues/{n}"`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — DUP-001  
  **File path** — `scripts/label_helpers.sh:102-197; scripts/validate_process.sh:496-590; scripts/orchestrate_poll_process.sh:1087-1225`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — Label creation and phase-label mutation logic is duplicated three ways: a reusable version in `scripts/label_helpers.sh`, a validate-local version in `scripts/validate_process.sh`, and a poller-local version in `scripts/orchestrate_poll_process.sh`. The bodies are near-identical: load label metadata, ensure labels exist, fetch current labels, compute add/remove sets, and apply mutations. This duplication is already drifting: only the poller version has `_ENSURED_LABELS_CACHE`, and failure-return semantics differ across copies.  
  **Recommended fix** — Make `scripts/label_helpers.sh` the single owner of:
  - `ensure_label_exists <label_name> [repo]`
  - `set_issue_phase_label <issue_number> <phase_label> [repo] [contract_file]`
  - `set_tracking_phase_label <tracking_issue_number> <phase_label> [repo]`  
  Then update callers in `scripts/validate_process.sh` and `scripts/orchestrate_poll_process.sh` to source that module rather than carrying private copies.

- **ID** — DUP-002  
  **File path** — `.github/workflows/issue_pr_status.yml:41-131,466-499,555-588`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — `issue_pr_status.yml` contains three separate implementations of support-source checkout/fallback logic: one for memory helpers, one for the merged-alert Telegram helper fetch, and one for Telegram cleanup. Each rebuilds `WF_REMOTE_URL`, allocates temp directories, tries `script_ref`, falls back to `main`, and copies `scripts/tg_helpers.sh` or related helpers into place. This is >70% structurally identical and already varies in small but important ways (different temp roots, different fallback logging, different file-copy guards).  
  **Recommended fix** — Extract a shared helper script, e.g. `scripts/fetch_support_source.sh`, with a function like `checkout_support_source <workflow_repo> <preferred_ref> <stage_root>`, returning the resolved root and ref. Update the three `issue_pr_status.yml` call sites first; then fold similar blocks from other workflows into the same helper.

- **ID** — DUP-003  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/orchestrate_poll.yml:63-97; .github/workflows/mark-stable.yml:307-333,456-483`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — The inline `_rl_wait` + `_gh_retry` retry/backoff implementation appears multiple times across workflows with only cosmetic differences. The duplication increases maintenance risk: any fix to rate-limit parsing, breaker-file handling, or backoff policy must be applied in several places.  
  **Recommended fix** — Centralize this bootstrap logic in a small shared script, e.g. `scripts/gh_retry_bootstrap.sh`, with `_rl_wait` and `_gh_retry <cmd...>`. Update `cancel_on_pr_close.yml`, `orchestrate_poll.yml`, and `mark-stable.yml` to source or generate that shared helper early in the job.

### Section 4: Expression Size Limit Risk Assessment

No reportable `expression-limit` findings in the current tree.

- I did not find any current `run:` or `if:` block containing `${{ }}` that crossed the 15,000-character static-body threshold during this audit.
- No workflow file exceeded the 800 KB early-warning threshold. The two largest current workflow files are:
  - `review_autofix.yml` — **267,353 bytes**
  - `test-and-mark-stable.yml` — **229,098 bytes**

The repo still has clear historical risk because the largest files remain `review_autofix.yml` and `test-and-mark-stable.yml`, but there is no present block I can support as an actionable size-limit finding.

### Section 5: Cross-Cutting Concerns

- **ID** — DEAD-001  
  **File path** — `scripts/orchestrate_poll_process.sh:4765-4772`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `read_standalone_state_json()` is defined but has no live call sites in the repository. The surrounding code paths use cached comment JSON plus `_extract_standalone_state_json_from_comments()` and `write_standalone_state_json()` instead. Keeping the unused wrapper is misleading because it suggests an endorsed API that no caller actually uses, and it duplicates a paginated comments fetch.  
  **Recommended fix** — Remove `read_standalone_state_json()` if the cached-comment path is the intended contract. If a wrapper is still desirable, convert one real caller to use it so the function stops being dead code.

- **ID** — CONSIST-001  
  **File path** — `scripts/label_helpers.sh:124-143; scripts/validate_process.sh:511-529; scripts/orchestrate_poll_process.sh:1121-1141`  
  **Severity** — Medium  
  **Category tag** — `consistency`  
  **Description** — The same `ensure_label_exists` operation has three different error contracts. In `scripts/label_helpers.sh`, unrecoverable create failures return non-zero. In `scripts/validate_process.sh` and `scripts/orchestrate_poll_process.sh`, the helper logs a warning but always returns success. That means callers cannot reason consistently about label-creation failures, and future code that expects canonical helper semantics will silently fail open in some execution paths but not others.  
  **Recommended fix** — Standardize on one contract in `scripts/label_helpers.sh`: return non-zero on unrecoverable create failure, and make fail-open explicit at call sites with `|| true` where desired. Then delete the duplicated local variants.

- **ID** — SHELL-001  
  **File path** — `scripts/validate_process.sh:197-205`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — ShellCheck flags `local msg="$1$(_tg_link_suffix)"` as `SC2155`. Declaring and assigning in one command masks the exit status of `_tg_link_suffix`, which is easy to miss under `set -euo pipefail` if that helper later grows a real failure mode.  
  **Recommended fix** — Split declaration from assignment:
  ```bash
  local msg
  msg="$1$(_tg_link_suffix)"
  ```
  This preserves command-substitution failure semantics and removes the ShellCheck warning.

Additional cross-cutting notes:
- I did not find any `TODO`, `FIXME`, or `HACK` markers in the audited workflows/scripts during this pass.
- ShellCheck on a targeted subset also reported two unused-variable warnings worth cleaning up next: `scripts/review_run_reviewers.sh:119` (`probe_prompt`) and `scripts/memory_helpers.sh:57` (`token`).

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 4 | BUG-001, BUG-002, BATCH-001, CONSIST-001 |
| Low | 7 | API-001, BATCH-002, DUP-001, DUP-002, DUP-003, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 3-4 | Medium |
| Code modularization | 7-10 | Large |
| Expression size reduction | 0 | Small |
| Medium/Low fixes | 4-6 | Medium |
