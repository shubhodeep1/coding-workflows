## Executive Summary

- **The biggest end-to-end latency and cost drain is `review_autofix`, not CI.** In `shubhodeep1/coding-workflows`, `review_autofix` ran 124 times with **78 cancellations (62.9%)**, and cancelled runs were often expensive rather than cheap: run **25114215563** consumed **1,113s**, **25102038820** consumed **917s**, and **25103001840** consumed **719s** before cancellation. Estimated impact: **high** (double-digit % reduction in total runner minutes and token spend). Confidence: **high**.
- **`implement` failures are dominated by a known MCP/tool-list failure mode, not repo-specific code defects.** Failed runs **25076992830**, **25091341828**, and **25092547530** all point to OpenRouter→Azure rejecting malformed tool payloads after MCP handshake failure (`HTTP 400`, invalid `tools[N].function`), causing repeated failed retries in `Run Codex implementation`. Estimated impact: **high** on reliability and rerun reduction. Confidence: **high**.
- **Active PR critical path is currently `CI (~10 min) + review_autofix (~18–38 min)`; the slowest single CI sub-step is one test shard.** In CI run **25114215361**, `lint_Orchestrate_poll_process_unit_tests` took **545.6s** of a **626s** run. In `review_autofix` runs **25112551905** and **25114215563`, `Run reviewer models` took **1,033.6s** and **427.1s** respectively, while `Collect PR check-run failures (CI/lint autofix context)` took **411.2s** and **309.1s**. Estimated impact: **high** if parallelized/cached. Confidence: **high**.
- **Prompt-cache and token telemetry are not decision-grade yet.** Cache probes are present in `review_autofix` runs **25112551905** and **25114215563**, but `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens` are logged as **`na`**, so cache ROI cannot be measured. Estimated impact: **medium** (measurement unlocks optimization, not direct savings by itself). Confidence: **high**.
- **AI memory works for implementation more than review.** Across sampled logs, AI memory retrieval hit rate was **53.3% (8/15)** with average retrieved context **21.9 estimated tokens**; implementation retrieves usually returned **1–3 records**, but sampled reviewer retrieves in `review_autofix` returned **0 records**. Estimated impact: **medium** on token reduction and review quality if fixed. Confidence: **high**.
- **Integration/conflict healing is a recurring bottleneck and release blocker.** Both CI failure run **25094556541** and release-test failure run **25088532565** hit repeated `Integration fingerprint verification FAILED` errors, refusing to create `[ai-merge-resolve]` commits and escalating to the integration judge. Estimated impact: **medium-high** on release reliability and long-tail runtime. Confidence: **high**.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

1. **Move stale-run cancellation ahead of expensive review work in `review_autofix`.**  
   - **Evidence:** `review_autofix` has **78 cancelled / 124 total** runs. Recent cancelled run **25114215563** lasted **1,113s**; other cancelled runs lasted **917s**, **719s**, **603s**, **500s**, **485s**, and **344s**. In that cancelled run, the heavy work had already happened: `Run reviewer models` took **427.1s**, `Collect PR check-run failures` took **309.1s**, and `Free disk space` took **246.9s**.  
   - **Root cause:** cancellation often arrives after the workflow has already paid for metadata collection, model passes, and workspace prep. The existing stale-base protection is too late in the flow.  
   - **Exact change:** add an **early head-sha freshness gate** immediately after checkout/PR metadata fetch and before reviewer-model fanout, diff generation, or CI-context collection. If `HEAD` advanced, exit cleanly before any reviewer calls. Keep the existing pre-editor stale-base gate, but add a **pre-review stale-base gate**.  
   - **Estimated time savings:** **6–18 minutes per cancelled review run**; repository-wide likely the largest single runner-minute reduction.  
   - **Implementation risk:** **low**. It is fail-open and backward-compatible if the freshness check cannot resolve metadata.

2. **Parallelize or shard the dominant CI unit-test step.**  
   - **Evidence:** In CI run **25114215361**, `lint_Orchestrate_poll_process_unit_tests` consumed **545.6s** of the **626s** total job. The rest of the suite is comparatively small.  
   - **Root cause:** one large serial test group dominates the CI wall clock.  
   - **Exact change:** split `Orchestrate_poll_process_unit_tests` into 2–4 matrix shards or separate jobs, keyed by test file/class pattern. Keep artifact-less execution; no new infrastructure needed.  
   - **Estimated time savings:** **3–5 minutes per CI run** on the critical path.  
   - **Implementation risk:** **low-medium**. Risk is mostly test-order coupling; mitigate by running shards independently with deterministic selection.

3. **Reduce `review_autofix` pre-model overhead, especially CI failure collection.**  
   - **Evidence:** In run **25112551905**, `Collect_PR_check-run_failures_CI_lint_autofix_context` took **411.2s**; in **25114215563** it took **309.1s**. This is before or alongside model execution.  
   - **Root cause:** expensive PR/check-run context assembly is happening synchronously and appears to walk a large amount of check/log state even when little has changed.  
   - **Exact change:** cache check-run failure summaries per `PR head SHA` and reuse within the run and across same-head reruns; short-circuit collection when the SHA and failing-check set are unchanged.  
   - **Estimated time savings:** **5–7 minutes per review run** when head SHA is unchanged or only reviewer reruns occur.  
   - **Implementation risk:** **low** if keyed strictly by PR head SHA and failed-check signature.

4. **Cap redundant implement retries when the failure is non-recoverable.**  
   - **Evidence:** implement failures **25076992830** (**4,984s**), **25091341828** (**3,437s**), and **25092547530** (**1,394s**) retried despite the same MCP/tool-list failure mode. Run **25092547530** explicitly logged `Codex implement failed after 5 attempts.`  
   - **Root cause:** the retry loop treats deterministic provider/tool-schema failures like transient model failures.  
   - **Exact change:** classify OpenRouter/Azure `HTTP 400` tool-shape errors and MCP initialize-handshake failures as **non-retryable**, or retry once after stripping bad MCP tools, then fail fast.  
   - **Estimated time savings:** **20–65 minutes per affected run**.  
   - **Implementation risk:** **low-medium**. Misclassification is the main risk; keep a fallback single retry.

5. **Shorten `review_autofix` review time by reducing reviewer breadth on unchanged or low-risk diffs.**  
   - **Evidence:** `Run reviewer models` took **1,033.6s** in **25112551905** and **427.1s** in cancelled run **25114215563**. Run **25112551905** used a two-pass review with **6 successful reviewers** plus a summarizer.  
   - **Root cause:** every full review uses broad multi-model consensus even when diff size/risk may not justify it.  
   - **Exact change:** gate reviewer fanout by diff size / file-type risk: e.g., 2–3 reviewers on docs/test-only or narrow workflow edits; full panel only when workflow/core orchestration files change.  
   - **Estimated time savings:** **4–12 minutes per review run**.  
   - **Implementation risk:** **medium**. Quality risk exists; mitigate with conservative routing rules and keep full panel for high-risk paths.

6. **Fail fast in `test_and_mark_stable` before long end-to-end waits.**  
   - **Evidence:** failing run **25088532565** spent **5,940.9s** in `e2e-smoke-test` and **2,253.5s** in `orphan-workflows-test`; the eventual hard failure was repeated `Integration fingerprint verification FAILED` plus wave expectation failures.  
   - **Root cause:** long-running release verification discovers deterministic integration/fingerprint issues too late.  
   - **Exact change:** move fast fingerprint/wave contract checks ahead of long PR/review simulation, and abort remaining release checks on first integration-fingerprint regression.  
   - **Estimated time savings:** **30–90 minutes** on bad release-test runs.  
   - **Implementation risk:** **low**.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

1. **Stop paying for long cancelled `review_autofix` runs.**  
   - **Evidence:** 62.9% of `review_autofix` runs were cancelled; many were expensive. Run **25114215563** did `Run reviewer models` for **427.1s** before being cancelled.  
   - **Root cause:** stale runs survive long enough to launch full reviewer/model work.  
   - **Exact change:** same early stale-head gate as above, plus cancel-before-fanout when a newer synchronize event exists for the same PR.  
   - **Estimated savings:** likely the **largest token and runner-minute reduction** in the system; cuts both model calls and GitHub runner time.  
   - **Quality-risk notes:** very low if keyed to newer PR head SHA.

2. **Reduce reasoning effort from `xhigh` by default on exploration-heavy or low-risk passes.**  
   - **Evidence:** review logs state a real consumer run on `bitsafe.io` issue **#26** (referenced inside runs **25112551905** and **25114215563**) failed after **5 codex attempts**, each spending ~5 minutes on exploration with `MODEL_REASONING_EFFORT=xhigh` and producing no edits. Review logs also show reviewer reasoning effort logged as **xhigh**.  
   - **Root cause:** high reasoning is being spent before the workflow knows the task needs it.  
   - **Exact change:** use **medium** reasoning for first-pass reconnaissance / broad review; escalate to `xhigh` only for edit pass, conflict resolution, or when workflow/core orchestration files are touched.  
   - **Estimated savings:** **15–35% token reduction** on exploration-heavy review/implement attempts.  
   - **Quality-risk notes:** moderate; protect high-risk file paths with escalation.

3. **Right-size multi-reviewer fanout.**  
   - **Evidence:** run **25112551905** completed **6 reviewer successes** in pass 1 and then ran a summarizer (`gpt-5.4-mini`) before further work.  
   - **Root cause:** consensus review breadth is fixed high even when diffs are narrow.  
   - **Exact change:** for small diffs or non-runtime-critical files, run fewer reviewers and skip pass-2 unless pass-1 finds disagreement or blocking issues.  
   - **Estimated savings:** **20–50% review-token savings** on low/medium-risk PRs.  
   - **Quality-risk notes:** moderate; enforce full panel on workflow files, orchestrator logic, or integration branches.

4. **Eliminate deterministic implement retries after provider/tool-shape failure.**  
   - **Evidence:** repeated Azure `HTTP 400` invalid-tool failures in **25076992830** and similar runs, with retries exhausted.  
   - **Root cause:** retries occur even though the error is schema-validity related, not stochastic generation failure.  
   - **Exact change:** detect the signature and either strip bad MCP server entries before retry or fail fast after one remedial attempt.  
   - **Estimated savings:** **up to 80%** of token spend in affected failed implement runs.  
   - **Quality-risk notes:** low if fallback is one remedial retry.

5. **Shrink repeated prompt/context expansion in review and implement.**  
   - **Evidence:** implement run **25092547530** builds a large prompt by concatenating `pre_assembled_static.txt`, rendered prompt template, memory context, and implementation context; review runs show large static instruction blocks and repeated environment logging.  
   - **Root cause:** large static prefixes are intentionally cacheable, but dynamic blocks may still be broader than needed and repeated across retries.  
   - **Exact change:** keep the static prefix stable, but reduce dynamic sections to SHA-scoped deltas only; on retries, append only retry deltas rather than rebuilding full dynamic context.  
   - **Estimated savings:** **moderate** token reduction on retry-heavy runs.  
   - **Quality-risk notes:** low if static prefix remains unchanged and dynamic truncation is SHA-safe.

6. **Fix prompt-cache observability before making bigger cache bets.**  
   - **Evidence:** cache probes in runs **25112551905** and **25114215563** report `prompt_tokens=na`, `completion_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`.  
   - **Root cause:** instrumentation is incomplete, so cache hit/miss value is invisible.  
   - **Exact change:** emit real per-call cache read/create counters and aggregate them into run summary artifacts.  
   - **Estimated savings:** indirect but important; unlocks precise cache tuning.  
   - **Quality-risk notes:** none.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

1. **Harden MCP handshake handling so failed servers never enter the tool list.**  
   - **Failure evidence:** implement failures **25076992830**, **25091341828**, and **25092547530** all tie back to `HTTP 400` invalid tool payloads after MCP initialize failure; logs explicitly mention Context7 handshake failure and invalid `tools[7].function`.  
   - **Root cause category:** external tool bootstrap / request-shape validation.  
   - **Exact fix:** preflight each MCP server before writing it into Codex config; if handshake fails, omit the server entirely from the tool list. Pin all MCP servers to known-good versions.  
   - **Expected reliability impact:** removes a **class** of deterministic implement/review/validate failures.  
   - **Rollback/fail-open considerations:** fail open by skipping the broken MCP server; do not fail the whole job.

2. **Promote integration-fingerprint regressions to an earlier, clearer terminal path.**  
   - **Failure evidence:** CI run **25094556541** and release-test run **25088532565** both failed on repeated `Integration fingerprint verification FAILED` annotations refusing `[ai-merge-resolve]` commits.  
   - **Root cause category:** merge/conflict healing dead-end.  
   - **Exact fix:** once fingerprint verification fails on the same pattern set more than once, stop resolver retries and immediately route to the integration judge / fail-fast path.  
   - **Expected reliability impact:** fewer long dead-end retries, faster deterministic failure classification.  
   - **Rollback/fail-open considerations:** safe; this shortens failure loops rather than changing success behavior.

3. **Treat empty-output reviewer/model results as retryable only once, then degrade gracefully.**  
   - **Failure evidence:** in cancelled review run **25114215563**, reviewer `z-ai/glm-5` produced **empty output on attempt 1**.  
   - **Root cause category:** model/provider response quality.  
   - **Exact fix:** after one empty-output retry, mark that reviewer unavailable for the run and continue with remaining reviewers instead of blocking the whole panel.  
   - **Expected reliability impact:** lower sensitivity to single-provider anomalies.  
   - **Rollback/fail-open considerations:** fail open; consensus degrades but run completes.

4. **Add a stronger early budget guard to `implement`.**  
   - **Failure evidence:** run **25092547530** logged `Insufficient remaining budget and no successful implement attempt completed` and still reached `Codex implement failed after 5 attempts.`  
   - **Root cause category:** retry budget management.  
   - **Exact fix:** stop launching a new attempt unless there is enough remaining budget for one full attempt plus cleanup/comment/failure-recording tail.  
   - **Expected reliability impact:** fewer timeout-adjacent incomplete failures and cleaner terminal states.  
   - **Rollback/fail-open considerations:** low risk; only reduces low-probability late attempts.

5. **Close the release-test blind spot around editor-bait verification and wave expectations.**  
   - **Failure evidence:** release-test run **25088532565** logged `Could not re-fetch ... after review`, `PR head is still the bait commit`, `Expected 3 waves`, `Expected any_review_blocked=true`, and `Expected wave_complete=false`.  
   - **Root cause category:** orchestrator/review simulation contract drift.  
   - **Exact fix:** split these into independent fast contract jobs with clearer pass/fail ownership, rather than discovering them inside a very long smoke sequence.  
   - **Expected reliability impact:** better localization and lower rerun waste.  
   - **Rollback/fail-open considerations:** low.

## AI Memory Health

- **Memory telemetry was observed** in sampled `errors/`, `slow/`, and `recent/` logs.
- Across sampled logs:
  - **Retrieve hit rate:** **53.3%** (**8 / 15** retrieves had `records_selected > 0`)
  - **Average `estimated_tokens`:** **21.9**
  - **`keyword_method` distribution:** `plain` **8**, `none` **7**, `llm` **0**
- **Good signs:**
  - No sampled entries with `fail_open: true`
  - No sampled entries with `enabled: false`
  - No sampled entries with `push_attempts > 1`
  - `record-run-event` writes were stable (`push_attempts: 1`)
- **Problems observed:**
  - **7/15 retrieves returned 0 records**
  - Reviewer memory looks especially weak: sampled `review_autofix` retrieves in runs **25114215563** and other sampled review runs had `records_selected: 0`, `estimated_tokens: 0`, `keyword_method: none`
  - Implementation memory is healthier: sampled implement runs usually retrieved **1–3 records** with **28–80 estimated tokens**
- **Recommendation:** improve reviewer retrieval query generation and keying. Today reviewer memory is often effectively disabled by retrieval quality, even though the memory system itself is operational. Prioritize:
  1. better reviewer keywords from PR title/files/check failures,
  2. issue/PR-linked retrieval fallback when keyword extraction returns `none`,
  3. telemetry breakout by role (`implementation` vs `reviewer`) in run summaries.

## GH API Call Audit

- **Exact repo-wide GH API totals are not exposed in the sampled artifacts.** The current window supports **pattern-level auditing**, not full call-count accounting.

### High-volume / high-redundancy patterns observed

1. **`review_autofix` does multiple PR-scope REST fetches before reviewer execution.**
   - **Evidence:** run **25114215563**, step `Collect_PR_metadata` / adjacent setup:
     - `repos/.../pulls/{PR}`
     - `issues/{PR}/comments --paginate`
     - `pulls/{PR}/reviews --paginate`
     - `pulls/{PR}/comments --paginate`
     - GraphQL `closingIssuesReferences(first:50){number title body}`
   - **Audit:** this is partially optimized already; the linked-issues GraphQL call explicitly supersedes a later fetch and turns the later step into a cache read.
   - **Recommendation:** collapse PR metadata + linked-issues into a single GraphQL fetch where possible, and keep the paginated comments/reviews only.
   - **Estimated reduction:** **1 REST call per review run**, plus downstream no-op fetches.

2. **`implement` appears to fetch issue metadata more than once per run.**
   - **Evidence:** failed implement logs show an initial `gh api repos/.../issues/${ISSUE_NUMBER} --jq '[.labels[].name]'` and later `gh api repos/.../issues/${ISSUE_NUMBER} > ISSUE_META_FILE`.
   - **Root cause:** one call for labels, then another for full issue metadata.
   - **Recommendation:** fetch issue JSON once, derive labels locally with `jq`.
   - **Estimated reduction:** **1 REST call per implement run**.
   - **Rate-limit risk reduction:** small but free.

3. **`test_and_mark_stable` uses heavy polling loops.**
   - **Evidence:** run **25088532565** repeatedly logged idle status checks every ~13 seconds for the same review phase (`Collect PR check-run failures (CI/lint autofix context)`), and repeatedly fetched PR/branch state in the editor-bait verification path.
   - **Root cause:** tight polling for long-running downstream workflows.
   - **Recommendation:** apply shared per-tick caches and ETag/conditional fetches consistently in release-smoke polling code; back off poll interval after first stable observation.
   - **Estimated reduction:** potentially **dozens of API calls per release-test run**.
   - **Rate-limit risk reduction:** meaningful for long E2E windows.

4. **Positive finding: some API hygiene rules are already being followed.**
   - **Evidence:** review logs explicitly note “later step becomes a no-op cache read” for linked-issues fetch, and documentation blocks in logs emphasize “Check first, add second”, batching, cycle-local caches, and fail-open cache behavior.
   - **Recommendation:** enforce these repo rules in code review for all new GH API additions, especially in orchestration and release-test polling code.

### Missed batching / reuse opportunities

- Reuse issue metadata in `implement`
- Merge PR metadata + linked issue fetches in `review_autofix`
- Cache release-smoke polling state per SHA / run-id instead of recomputing every interval
- Prefer GraphQL aliases when several PR/issue fields are needed together

## MCP & Serena Efficiency

- **Serena is present and validated, but overall efficiency is mixed.**
- **Evidence:**
  - In review run **25114215563**, Serena setup succeeded in ~**9.3s** and was validated successfully.
  - The same run had **`GIT_MCP_DISABLED=true`** and `Context7 MCP setup skipped`, so only Serena was active.
  - A generated review summary in run **25112551905** reported **“Serena efficiency | 46%”**.
- **What’s working:**
  - Serena startup validation is hardened (`required=false`, graceful fallback).
  - Symbol-level diff generation is already present (`Generate_symbol-level_diff_summary`, ~**24–25s**).
- **What’s inefficient:**
  - Git MCP is disabled in sampled review runs, so Git/PR context still relies heavily on shell + `gh` + broad diff/context assembly.
  - Pre-model context collection remains expensive even with Serena enabled (`Collect PR check-run failures` 309–411s).
  - Review flow still appears to do a lot of broad context prep before model calls.
- **Recommendations:**
  1. **Enable Git MCP in review runs where stable** instead of disabling it globally; this should reduce shell/GH churn for diff/status/log queries.
  2. **Route more context gathering through symbol/diff summaries** and less through broad full-context assembly.
  3. **Cache Serena project init outputs per run** so repeated substeps consume the same generated artifacts instead of rebuilding around them.
  4. **Add per-run Serena efficiency fields to run summary metadata**, not only log tables, so regressions can be tracked.
- **Parallelization opportunities:**
  - PR metadata fetch, linked-issue fetch, and symbol-diff generation can be parallelized safely after checkout.
  - Memory retrieval can run in parallel with static-context assembly.

## Prompt Cache & Memory System

### Prompt cache

- **Current state:** cache intent is strong, cache measurement is weak.
- **Evidence:**
  - Implement logs explicitly state the static prompt prefix is assembled identically across runs to enable prompt-prefix caching.
  - Review runs **25112551905** and **25114215563** emitted OpenRouter cache probe lines, but all token/cache counters were **`na`**.
- **Observed fragmentation risks:**
  - Large dynamic blocks are still appended after the static prefix.
  - Retry attempts appear to rebuild substantial dynamic context rather than appending only deltas.
  - Review runs include broad repo guidance and long diagnostic context even when only part is needed.
- **Recommendations:**
  1. **Keep the cacheable prefix byte-stable** and move noisy run-specific diagnostics later in the prompt.
  2. **On retries, append retry delta blocks** instead of rebuilding the whole dynamic context.
  3. **Emit real cache read/create counters** in run summaries.
- **Estimated impact:** **moderate token savings** and lower model startup latency once instrumentation is complete.

### Memory system

- **Effectiveness:** moderate overall, weak for reviewers.
- **Evidence:** retrieve hit rate **53.3%**, but reviewer retrieves often returned 0 records.
- **Recommendations:**
  1. generate reviewer retrieval keys from PR title + changed files + failing checks,
  2. fall back to linked issue/PR lineage when keyword extraction returns `none`,
  3. track hit rate separately for `implementation`, `reviewer`, and `judge`.
- **Estimated impact:** lower prompt size on repeated issues/PRs and better consistency on review runs.

## Orchestrator Health

- **Overall health:** basic orchestration is functional, but long-tail conflict/review loops remain the main pain point.
- **Healthy indicators:**
  - `orchestrate_poll` median behavior is acceptable: family p50 **46s**, p95 **272.8s**, recent runs typically **37–47s**
  - `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` often skip in **1–9s**, so the gate logic is cheap when no work is needed
- **Pain points:**
  - Release-test run **25088532565** shows orchestrator simulation drifting on wave expectations and integration conflict handling
  - `review_autofix` long cancellations indicate orchestration isn’t stopping stale work early enough
  - Implement stall-recovery exists, but failed implement runs still burn large retry windows
- **Smallest safe mitigations:**
  1. add **pre-review stale-head termination**,
  2. mark repeated fingerprint conflicts as terminal earlier,
  3. expose **judge-cycle**, **review-blocked loop count**, and **stale-run cancellation count** in top-level summaries.
- **Observable indicators to track:**
  - long cancelled `review_autofix` runs per day
  - integration-fingerprint verification failures per 100 review/merge attempts
  - average time spent in `Collect PR check-run failures`
  - `% reviewer memory retrieves with 0 records`
  - `% runs with non-NA prompt-cache counters`

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

1. **Review/autofix compute + cancellation overhead**
   - Dominant in active PR flow
   - Mix of model runtime, check-run context gathering, and wasted cancelled work

2. **CI single-step serialization**
   - One test shard dominates the entire CI wall clock
   - Pure compute bottleneck, low coordination overhead

3. **Implement retry loops on deterministic failures**
   - Retries consume time without increasing success probability when MCP/tool-shape failure occurs

4. **Merge/conflict healing loops**
   - Integration fingerprint verification failures block progress and cause repeated resolver/judge activity

5. **Release smoke polling and downstream verification**
   - Long queue/poll loops and repeated downstream checks inflate release-test latency

### By phase

- **Clarify → Plan:** cheap; mostly skipped or very fast
- **Implement:** bimodal; usually skipped, but failures are very expensive because retries are long
- **Review/Autofix:** main active-path bottleneck
- **Validate/Orchestrate:** acceptable in normal polling, expensive in integration dead-ends
- **Merge/Conflict handling:** dominant long-tail blocker in failures and release tests

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` cancellation waste and long model/check-context phases
  - CI dominated by `Orchestrate_poll_process_unit_tests`
  - `implement` deterministic MCP/provider failures
- **Top failure modes**
  - OpenRouter→Azure invalid tool payload after MCP handshake failure
  - Integration fingerprint verification regression during conflict resolution
  - Release-test/editor-bait/wave-contract drift
- **Highest-cost drivers**
  - cancelled `review_autofix` runs
  - multi-reviewer two-pass review fanout
  - repeated implement retries on non-retryable failures
- **Top 3 prioritized actions**
  1. Add **pre-review stale-head cancellation** in `review_autofix`
  2. **Shard the dominant CI test suite**
  3. **Strip failed MCP servers before Codex invocation** and fail fast on provider schema errors

## Metrics Appendix

### Repository summary

| Repository | Total runs | Success | Failure | Cancelled | Other/Skipped | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 259 | 7 | 86 | 648 | 0.7% | 152.9 | 1.0 | 635.0 |

### Workflow-family summary

| Workflow family | Total runs | Success | Failure | Cancelled | Other | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 71 | 70 | 1 | 0 | 0 | 601.0 | 607.0 | 639.5 |
| implement | 171 | 19 | 4 | 3 | 145 | 118.7 | 1.0 | 293.5 |
| review_autofix | 124 | 44 | 0 | 78 | 2 | 431.2 | 31.5 | 1862.1 |
| orchestrate_poll | 25 | 25 | 0 | 0 | 0 | 98.7 | 46.0 | 272.8 |
| test_and_mark_stable | 3 | 0 | 1 | 2 | 0 | 2602.7 | 1305.0 | 5500.8 |
| workflow_log_analysis | 3 | 0 | 0 | 3 | 0 | 3787.7 | 3409.0 | 5478.1 |
| copilot_pull_request_reviewer | 33 | 33 | 0 | 0 | 0 | 202.3 | 210.0 | 333.0 |
| clarify | 190 | 15 | 0 | 0 | 175 | 11.5 | 1.0 | 77.6 |
| plan | 171 | 13 | 0 | 0 | 158 | 19.9 | 1.0 | 182.0 |

### Sampled critical-path step timings

| Run ID | Workflow | Step | Duration |
|---|---|---|---:|
| 25114215361 | CI | `lint_Orchestrate_poll_process_unit_tests` | 545.6s |
| 25112551905 | review_autofix | `Run_reviewer_models` | 1033.6s |
| 25112551905 | review_autofix | `Collect_PR_check-run_failures_CI_lint_autofix_context` | 411.2s |
| 25114215563 | review_autofix | `Run_reviewer_models` | 427.1s |
| 25114215563 | review_autofix | `Collect_PR_check-run_failures_CI_lint_autofix_context` | 309.1s |
| 25092547530 | implement | `implement / implement` | 1387.6s |
| 25088532565 | test_and_mark_stable | `e2e-smoke-test` | 5940.9s |
| 25088532565 | test_and_mark_stable | `orphan-workflows-test` | 2253.5s |

### Review/autofix cancellation summary

| Metric | Value |
|---|---:|
| Total `review_autofix` runs | 124 |
| Successful | 44 |
| Cancelled | 78 |
| Cancelled share | 62.9% |
| Example expensive cancelled run | 25114215563 = 1113s |
| Other expensive cancelled runs | 25102038820 = 917s; 25103001840 = 719s; 25105259044 = 603s |

### AI memory telemetry (sampled logs)

| Metric | Value |
|---|---:|
| Total telemetry records | 62 |
| `retrieve` ops | 15 |
| Retrieve hit rate | 53.3% (8/15) |
| Avg retrieve `estimated_tokens` | 21.9 |
| `keyword_method=plain` | 8 |
| `keyword_method=none` | 7 |
| Zero-record retrieves | 7 |
| `fail_open: true` observed | 0 |
| `enabled: false` observed | 0 |
| `push_attempts > 1` observed | 0 |

### Prompt cache visibility (sampled)

| Run ID | Workflow | Cache probe present | Token metrics populated? |
|---|---|---|---|
| 25112551905 | review_autofix | Yes | No (`na`) |
| 25114215563 | review_autofix | Yes | No (`na`) |
| 25092547530 | implement | Static cacheable prompt prefix documented | No usable aggregate token totals in sampled excerpt |

### GH API call summary from sampled patterns

| Area | Observed pattern | Quantified from logs? | Optimization target |
|---|---|---|---|
| implement | Separate issue-label fetch and issue-metadata fetch | Yes, pattern observed | Reuse single issue payload |
| review_autofix | PR payload + issue comments + reviews + review comments + linked issues | Yes, pattern observed | Merge PR metadata + linked-issue fetch |
| review_autofix | Later linked-issues step turned into cache read | Yes | Keep and expand |
| test_and_mark_stable | Repeated polling/status checks over long windows | Yes, pattern observed | Backoff + conditional fetch + tick-local cache |
| Rate-limit handling | Wrapper present, no sampled rate-limit event | Partial | Keep fail-open; improve aggregate counts |

### Token totals

| Metric | Status |
|---|---|
| Repository-wide prompt/completion/total tokens | Not available in provided sampled artifacts |
| Per-run reviewer token totals | Not available in provided sampled artifacts |
| Cache creation/read token totals | Probe present, but values logged as `na` in sampled review runs |


## Deep Audit — Workflows & Scripts (2026-04-29)

### Section 1: Bug & Correctness Sweep

#### BUG-001
- **File path:** `.github/workflows/review_autofix.yml:3629-3668, 3744-3789`; `scripts/label_helpers.sh:144-195`
- **Severity:** High
- **Category tag:** `bug`
- **Description:** `review_autofix.yml` has two late-stage fallback implementations of `set_issue_phase_label_resilient` that only `POST` the target label. The canonical helper in `scripts/label_helpers.sh` first reads current labels, removes all phase-group labels, and then writes the exclusive replacement set with `PUT`. If the support copy of `label_helpers.sh` is unavailable — a case the workflow explicitly anticipates at `.github/workflows/review_autofix.yml:3632-3633` — linked issues can retain stale phase labels such as `ai:review-blocked` together with `ai:ready-to-merge`, violating the repo’s single-phase label invariant and confusing downstream orchestrator logic.
- **Recommended fix:** Stop using the inline POST-only fallbacks. Either: (a) restore/source the canonical `scripts/label_helpers.sh` before these late jobs, or (b) replace the fallback bodies with a byte-for-byte equivalent of `scripts/label_helpers.sh:set_issue_phase_label_resilient`. Reuse one shared helper rather than maintaining divergent copies.

#### BUG-002
- **File path:** `scripts/orchestrate_poll_process.sh:3427-3456`
- **Severity:** Medium
- **Category tag:** `bug`
- **Description:** `finalize_integration_merge_if_needed()` first lists an existing integration PR, then attempts `gh pr create`, and if creation fails it immediately declares failure when no URL is parsed. There is no second lookup after the create attempt. That creates a TOCTOU race: another poller tick or a human can create the PR between the `gh pr list` and `gh pr create` calls, causing `gh pr create` to fail even though the desired PR now exists.
- **Recommended fix:** After any failed `gh pr create`, re-run `gh pr list --base "${default_branch}" --head "${integration_branch}"` (or `gh pr view`) before emitting “Unable to create or locate the final integration PR”. This keeps the path idempotent under concurrent creators.

#### BUG-003
- **File path:** `.github/workflows/test-and-mark-stable.yml:2301-2317, 2346-2361, 2394-2412, 2444-2459, 2524-2535, 2688-2702`
- **Severity:** Medium
- **Category tag:** `bug`
- **Description:** Multiple “dispatch & watch” steps poll workflow-run state with `gh api ... || echo ""`, then parse the empty string with `jq` into blank `STATUS`/`CONCLUSION`. Transient API failures and rate limits are therefore converted into apparent “still running” states until the deadline expires, producing false timeouts instead of retrying intelligently. Earlier in the same workflow, other blocks already define `gh_api_safe` with rate-limit backoff, but these watcher loops do not reuse it.
- **Recommended fix:** Extract a single watcher helper that wraps `gh api` with retry/backoff and an indexing grace period, then use it for all dispatch/watch jobs. Prefer putting it in `scripts/gh_helpers.sh` or a new `scripts/watch_workflow_run.sh` so every watch path shares the same failure handling.

#### SEC-001
- **File path:** `scripts/run_validation_repo_checks.sh:18-23`; `workflow-templates/validation-harness/python-repo-checks/tests/40_repo_checks.sh.j2:9-15`
- **Severity:** Low
- **Category tag:** `security`
- **Description:** The repo-check entrypoint is executed through `/bin/sh -c "${check_cmd}"`, and the validation-harness template passes `REPO_CHECK_ENTRY` directly into `/bin/sh -c`. That means the configuration surface is “arbitrary shell program”, not “path to a test script”. Any unexpected shell metacharacters in the configured entry string will execute additional commands inside the validation container.
- **Recommended fix:** Change the contract from “shell command string” to “executable path + arguments”. A simple option is a JSON-array or newline-delimited argv format parsed into an array and executed directly. If string form must remain, validate it against a strict allowlist pattern before execution and reject shell metacharacters.

### Section 2: GitHub API Call Redundancy Audit

#### API-001
- **File path:** `.github/workflows/implement.yml:53-63, 511-523, 601-607, 2892-2897`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** `implement.yml` fetches the same issue payload multiple times on the main execution path: once in “Precheck approval phase label”, again in “Fetch issue metadata”, and again in later label-validation/failure paths when the file cache is absent or ignored. The first call only needs labels, but the second already downloads the full issue JSON to `ISSUE_META_FILE`.
- **Current call count:** 3 logical issue `GET /repos/{repo}/issues/{n}` calls on the common path.
- **Proposed call count after fix:** 1.
- **Existing batching pattern to extend:** Reuse the existing `ISSUE_META_FILE` snapshot pattern already present in `implement.yml:594-607`.
- **Recommended fix:** Move the full issue fetch to the earliest point in the job, derive labels/title/body from `ISSUE_META_FILE` everywhere else, and make later checks fail open to the cached file before re-querying.

#### API-002
- **File path:** `.github/workflows/clarify.yml:369-374`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** When semantic cache is enabled, `clarify.yml` first fetches the first 50 issue comments into `ISSUE_COMMENTS_FILE`, then immediately performs a second paginated fetch of the same comment thread to build `THREAD_HISTORY_FILE`. The first payload is a strict prefix of the second.
- **Current call count:** 2 logical comment-fetch calls.
- **Proposed call count after fix:** 1.
- **Existing batching pattern to extend:** Reuse the already-paginated `--paginate --slurp` fetch and derive the bounded prompt context locally.
- **Recommended fix:** Always fetch the full comment history once, write it to a temp JSON file, then materialize both `ISSUE_COMMENTS_FILE` (trimmed subset) and `THREAD_HISTORY_FILE` from that cached JSON.

#### API-003
- **File path:** `scripts/orchestrate_poll_process.sh:3407-3412, 3427-3468, 3517-3519`
- **Severity:** Medium
- **Category tag:** `api-redundancy`
- **Description:** `finalize_integration_merge_if_needed()` repeatedly fetches the same final PR as separate field lookups: `state`, `merged_at`, `mergeable`, then the same trio again after the merge attempt. Elsewhere in the same script, PR JSON is already cached and reused (`_fetch_pr_json` / iteration-scoped PR JSON caches), but this path does not reuse that pattern.
- **Current call count:** 8 logical PR fetch calls on the create/merge path.
- **Proposed call count after fix:** 2–3.
- **Existing batching pattern to extend:** Extend the existing `_fetch_pr_json` / cached PR-JSON reuse pattern already used later in `scripts/orchestrate_poll_process.sh` (for example around `6817-6822` and `8687-8691`).
- **Recommended fix:** Fetch the PR JSON once after discovery/creation, parse `state`, `mergeable`, and `merged_at` from the same blob, then refresh only once after the merge attempt.

#### API-004
- **File path:** `.github/workflows/issue_pr_status.yml:280-340, 503-512`
- **Severity:** Low
- **Category tag:** `api-redundancy`
- **Description:** `issue_pr_status.yml` already classifies linked issues as tracking/managed in a batched GraphQL-or-REST step, but the later Telegram gate discards that result and re-fetches each linked issue body one-by-one just to re-detect the orchestrator marker.
- **Current call count:** 1 extra issue-body fetch per linked issue.
- **Proposed call count after fix:** 0 extra calls.
- **Existing batching pattern to extend:** Reuse the earlier batched classification result from the “Update linked issue labels when PR closes” step.
- **Recommended fix:** Export `IS_ORCHESTRATED` or the resolved managed/tracking issue set to `$GITHUB_ENV` in the earlier step and consume that in the Telegram gate instead of re-querying issue bodies.

#### BATCH-001
- **File path:** `.github/workflows/review_autofix.yml:478-530`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** In `post-merge-validate-dispatch`, the fallback path extracts issue numbers from PR text and then loops over them with `gh issue view ... --json labels` to decide whether `ai:orchestrator-validate-required` is present. That makes label detection O(N) REST calls even though the issue list is already known.
- **Current call count:** 2 + N logical calls on the fallback path (GraphQL linked-issues miss, PR fetch, then one issue-label fetch per issue).
- **Proposed call count after fix:** 3 total, regardless of N.
- **Existing batching pattern to extend:** Reuse the GraphQL alias-batching approach already used in `scripts/orchestrate_poll_process.sh` and `.github/workflows/issue_pr_status.yml`.
- **Recommended fix:** After fallback regex extraction, batch-resolve all candidate issue labels in one GraphQL query with aliases (`issue(number: X) { number labels(...) }`) and drive the loop from that response instead of per-issue `gh issue view`.

#### BATCH-002
- **File path:** `scripts/review_rb_judge.sh:146-170, 191-203`
- **Severity:** Medium
- **Category tag:** `api-batching`
- **Description:** `review_rb_judge.sh` first gets all linked issue numbers in one GraphQL call, then immediately loops over every issue and fetches each issue body individually, even though only the first issue body is retained in `FIRST_ISSUE_BODY`. This is a review-blocker style per-item API loop.
- **Current call count:** 1 + N logical calls.
- **Proposed call count after fix:** 1 total, or 2 if you intentionally fetch only the first issue body separately.
- **Existing batching pattern to extend:** Extend the existing `closingIssuesReferences` GraphQL query to request `number` and `body`, or follow the GraphQL-first consolidation pattern from `scripts/gh_helpers.sh:733-850`.
- **Recommended fix:** Request `nodes { number body }` in the initial GraphQL query and populate `FIRST_ISSUE`/`FIRST_ISSUE_BODY` from that result; if only one body is needed, stop after the first node instead of looping REST calls.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001
- **File path:** `.github/workflows/issue_pr_status.yml:69-120`; `.github/workflows/validate.yml:214-282`; `.github/workflows/validation-improvements-intake.yml:72-120`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** Three workflows independently implement near-identical support-bootstrap helpers (`checkout_support_ref`, `fetch_from_ref_or_local` / `copy_from_ref_or_local`, staged primary/main fallback roots, and `WF_REMOTE_URL` setup). The blocks are already drifting: `validate.yml` has the most complete variant, while the others carry slimmer versions and additional one-off clones later in the file.
- **Recommended fix:** Move this into a shared module, e.g. `scripts/fetch_workflow_support.sh`, with a function signature like `fetch_workflow_support <wf_source_repo> <script_ref> <stage_root> <primary_dir> <main_dir> <path...>`. Update `issue_pr_status.yml`, `validate.yml`, and `validation-improvements-intake.yml` to source/invoke the same helper.

#### DUP-002
- **File path:** `.github/workflows/test-and-mark-stable.yml:280-315, 391-425, 570-605, 855-877, 2275-2317, 2327-2361, 2377-2412, 2423-2459, 2501-2535, 2666-2702`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** `test-and-mark-stable.yml` contains multiple copies of the same API-watch machinery: `gh_api_safe`, `capture_run_id`, “dispatch workflow”, “poll for NEW_ID”, and “watch run conclusion”. The copies already differ in timeout handling and success conditions, which is how BUG-003 slipped in.
- **Recommended fix:** Extract a shared script, e.g. `scripts/watch_dispatched_workflow.sh`, with a signature like `watch_dispatched_workflow --repo <repo> --workflow <file> --dispatch-args <...> --deadline-secs <n> --success-conclusions success,skipped`. Update every dispatch/watch block in `test-and-mark-stable.yml` to use it.

#### DUP-003
- **File path:** `.github/workflows/review_autofix.yml:3634-3668, 3757-3789`; `.github/workflows/issue_pr_status.yml:240-249`
- **Severity:** Medium
- **Category tag:** `duplication`
- **Description:** The repo has at least three ad-hoc fallback implementations of `set_issue_phase_label_resilient`, all maintained inline in workflow YAML. Two of them are the broken POST-only form from BUG-001. This is duplicated behavior in a correctness-critical path.
- **Recommended fix:** Make `scripts/label_helpers.sh` the only owner of phase-label mutation, with a minimal sourceable fallback script if runtime checkout cleanup is a concern. Callers should not re-implement label-phase transitions inline.

### Section 4: Expression Size Limit Risk Assessment

#### EXPR-001
- **File path:** `.github/workflows/review_autofix.yml:1266-1588`
- **Severity:** Medium
- **Category tag:** `expression-limit`
- **Description:** The “Collect PR metadata” `run:` block is already very large and contains multiple `${{ }}` interpolations inside a 16,437-character block. This workflow has prior history of hitting the 21,000-character expression limit, and this block combines retry helpers, GraphQL queries, Python inline transforms, and diff generation in one place.
- **Estimated current character count:** ~16,437
- **Headroom remaining:** ~4,563
- **Recommended fix:** Extract the whole metadata-assembly flow into an external script under `scripts/` (preferred), or split it into separate steps for PR payload/comments, linked-issue context, and diff collection.

#### EXPR-002
- **File path:** `.github/workflows/validate.yml:188-481`
- **Severity:** Medium
- **Category tag:** `expression-limit`
- **Description:** The “Fetch workflow support files” `run:` block is an inline bootstrap framework with `${{ github.repository }}` / `${{ github.sha }}` interpolations and ~16,529 characters of shell. It bundles clone helpers, file-copy policy, prompt/schema bootstrapping, and optional asset fetches into one expression-bearing step.
- **Estimated current character count:** ~16,529
- **Headroom remaining:** ~4,471
- **Recommended fix:** Move the support-bootstrap logic into a dedicated script such as `scripts/fetch_validate_support.sh`, or split the current block into separate checkout/copy/schema/prompt phases.

#### EXPR-003
- **File path:** `.github/workflows/orchestrate_clarify_respond.yml:840-1123`
- **Severity:** Medium
- **Category tag:** `expression-limit`
- **Description:** The auto-answer post-processing block is ~15,140 characters with several `${{ }}` interpolations and lots of inline jq/Python/heredoc logic. It is below the 18k high-risk threshold, but already above the 15k medium-risk threshold and likely to keep growing as more loop-guard and memory metadata is added.
- **Estimated current character count:** ~15,140
- **Headroom remaining:** ~5,860
- **Recommended fix:** Extract the post-Codex answer/loop-guard logic to a `scripts/orchestrate_clarify_finalize.sh` helper, or split claim-check, loop-guard escalation, and answer posting into separate steps.

### Section 5: Cross-Cutting Concerns

#### CONSIST-001
- **File path:** `scripts/label_helpers.sh:144-195`; `.github/workflows/review_autofix.yml:3634-3668, 3757-3789`; `.github/workflows/issue_pr_status.yml:240-249`
- **Severity:** Medium
- **Category tag:** `consistency`
- **Description:** The repo uses inconsistent implementations for the same phase-label operation. The canonical shell helper performs an exclusive phase swap; inline workflow fallbacks merely add the new label. This inconsistency is the mechanism behind **BUG-001** and makes future label-contract changes fragile because call sites do not share one source of truth.
- **Recommended fix:** Centralize all phase-label mutations in `scripts/label_helpers.sh` and have workflows source that helper or a verbatim fallback copy. Do not keep semantic variants in YAML.

#### DEAD-001
- **File path:** `scripts/mark-stable.sh:1-14`
- **Severity:** Low
- **Category tag:** `dead-code`
- **Description:** `scripts/mark-stable.sh` appears unused by workflows or repository code; the release workflow re-implements stable-tagging inline in `.github/workflows/mark-stable.yml` instead. The only in-repo reference found was an analysis artifact, not runtime code.
- **Recommended fix:** Either wire `mark-stable.yml` to invoke `scripts/mark-stable.sh` so the logic has one owner, or remove the script after confirming it is not part of an external operator workflow.

#### SHELL-001
- **File path:** `.github/workflows/mark-stable.yml:141-157`
- **Severity:** Low
- **Category tag:** `shellcheck`
- **Description:** The “Script-workflow cross-reference” step uses `for ref in $(grep -rhoE ... | sort -u); do ...; done`, which is a classic ShellCheck-style word-splitting/globbing hazard. It works with the repo’s current no-spaces naming convention, but the loop is not robust if a referenced path ever contains whitespace or glob characters. [NEEDS VERIFICATION]
- **Recommended fix:** Replace the command-substitution loop with `while IFS= read -r ref; do ...; done < <(...)`, or delegate the check to the existing Python validator `scripts/check_workflow_script_refs.py`.

- **Additional cross-cutting note:** No `TODO`, `FIXME`, or `HACK` markers were found across `.github/workflows/*.yml` and `scripts/*.sh` / `scripts/*.py` during this pass.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 1 | BUG-001 |
| Medium | 14 | BUG-002, BUG-003, API-001, API-002, API-003, BATCH-001, BATCH-002, DUP-001, DUP-002, DUP-003, EXPR-001, EXPR-002, EXPR-003, CONSIST-001 |
| Low | 4 | SEC-001, API-004, DEAD-001, SHELL-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 3 | Medium |
| API call optimization | 6 | Large |
| Code modularization | 5 | Large |
| Expression size reduction | 3 | Medium |
| Medium/Low fixes | 5 | Medium |
