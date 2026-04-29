## Executive Summary

- **The biggest end-to-end problem is the stable-release test path, not CI.** `test_and_mark_stable` has **4 total runs, 0 successes, 2 failures, 2 cancellations, p50 2,491s, p95 5,623.5s**. Both failed runs (`25088532565`, `25115169454`) died in **`orchestrate-decompose-test`** after the orchestrator produced only **1 child issue instead of the expected 2**, and the companion `workflow_log_analysis` runs in the same window all cancelled after **2,246s–5,708s**. **Estimated impact:** 30–60 minutes faster release-validation cycles and materially higher gate pass rate. **Confidence:** high.

- **`review_autofix` is the main AI cost center and is highly wasteful under cancellation/conflict scenarios.** Family stats are **125 runs, 79 cancelled, 1 failed, p95 1,861s**. In successful run `25123709478`, step **`Run reviewer models`** consumed **427,088 tokens** for a two-pass, six-reviewer panel. In failed run `25115530167`, the run still reached **6 successful reviewers** but ended with `DID_COMMIT=false`, `MERGE_CONFLICT=true`, `CONFLICT_RESOLVED=false`, and `EDITOR_NOOP_SUSPICIOUS=true`. **Estimated impact:** 30–50% token reduction on heavy review runs plus 10–25 minutes less wasted compute on doomed runs. **Confidence:** high.

- **`implement` failures are clustered around one concrete integration fault: MCP handshake/tool-list corruption.** Error run `25091341828` documents repeated OpenRouter/Azure **HTTP 400** failures caused by an invalid tool entry (`function: undefined`) after an MCP server handshake failure; the same family has 3 failures and long outliers at **705s, 1,394s, and 3,437s**. **Estimated impact:** remove most hard `implement` failures in this window and avoid repeated retry burn. **Confidence:** high.

- **The workflow graph is noisy: most clarify/plan/respond/implement invocations are skipped after startup.** Examples: `clarify` **173/190** “other”, `plan` **155/168**, `implement` **142/168**, `orchestrate_clarify_respond` **165/168**; many recent runs complete in **1–2s** as skips. This keeps overall p50 artificially at **1.0s** while still consuming runner scheduling and event churn. **Estimated impact:** lower runner pressure and less orchestration noise; modest per-run savings, meaningful aggregate savings. **Confidence:** high.

- **Memory telemetry is present and generally healthy, but retrieval is only partially effective and prompt-cache observability is weak.** Across sampled logs there were **12 `retrieve` telemetry events**, **50% hit rate**, average **14 estimated tokens**, and keyword methods split evenly between `plain` and `none`. Prompt cache was almost always **enabled**, but sampled OpenRouter usage lines often reported token/cache fields as **`na`**, so cache hit economics cannot yet be measured. **Estimated impact:** medium token/latency gains once cache metrics are made reliable and reviewer-memory retrieval is tuned. **Confidence:** medium.

## Speed Optimizations

1. **Cut `workflow_log_analysis` scope so stable-release gates stop timing out**
   - **Evidence:** `workflow_log_analysis` has **4 runs, all cancelled**, with durations **2,246s, 3,409s, 3,639s, 5,708s**. In failed stable-release run `25115169454`, `orphan-workflows-test` reports `workflow-log-analysis run #25115192726 concluded cancelled`; in reviewer notes the workflow comment says the `api-redundancy` job was already bumped from **30 → 60 minutes** after prior timeout, yet the run still cancelled near the new cap.
   - **Root cause:** the analysis workflow is too broad for its current timeout budget and is on the critical path of release validation.
   - **Exact change:** keep the existing workflow, but reduce sampled input volume inside `workflow_log_analysis` to the same narrow slices already requested for manual analysis (`summary`, `errors/`, `slow/`, `recent/`) rather than deep-auditing the full log corpus every time.
   - **Estimated time savings:** **30–60 minutes** on `test_and_mark_stable` runs that currently block on orphan-analysis completion.
   - **Implementation risk:** **low** if the reduced scope is limited to release-gate mode; **medium** if applied globally.

2. **Add an orchestrator decomposition repair/validation pass before creating issues**
   - **Evidence:** `orchestrate-decompose-test` failed twice: run `25088532565` and run `25115169454`. In both logs the bootstrap run succeeded after ~**5 minutes** of polling, then the test failed with: **“Decomposition produced 1 child issue(s); expected >= 2”**.
   - **Root cause:** the orchestrator accepted a structurally valid but semantically insufficient decomposition.
   - **Exact change:** after decomposition but before issue creation, validate “expected issue count / wave count / dependency edges” for smoke-test projects; if unmet, re-prompt once with the same project description plus the missing structural constraints.
   - **Estimated time savings:** **5–10 minutes per failed stable test**, plus indirect rerun savings.
   - **Implementation risk:** **low-medium**; validation is deterministic, but the re-prompt path must fail-open cleanly.

3. **Shard CI unit suites across a small matrix instead of one serial lint/test lane**
   - **Evidence:** `ci` is stable but long: **78 runs, p50 606.5s, p95 641.6s**. Sample run `25123709200` executes many Python test files sequentially in one job.
   - **Root cause:** the dominant CI time is serialized test execution, not setup or flakiness.
   - **Exact change:** split Python tests into 2–3 balanced matrix buckets while keeping syntax/lint checks in a lightweight parent job.
   - **Estimated time savings:** **3–5 minutes** off the CI critical path.
   - **Implementation risk:** **low-medium**; main risk is uneven bucket sizing.

4. **Abort superseded `review_autofix` runs before Pass 2 and before summarization**
   - **Evidence:** `review_autofix` has **79 cancellations out of 125 runs**. Recent and slow data include cancelled runs at **85s, 90s, 254s, 603s, 917s, 1,113s, 1,523s**. Run `25123709478` spent ~**7+ minutes** in Pass 1 before starting Pass 2.
   - **Root cause:** concurrency cancellation happens, but often only after substantial reviewer work is already sunk.
   - **Exact change:** immediately before Pass 2 and before consensus summarization, re-check whether a newer run exists for the same PR/head branch; self-abort if superseded.
   - **Estimated time savings:** **5–20 minutes** on cancelled heavy review runs.
   - **Implementation risk:** **medium**; must avoid aborting the sole active run.

5. **Skip Serena boot/setup for non-code or workflow-only review runs**
   - **Evidence:** run `25123709478` stages Serena support, installs/sets up Serena, then later logs **“No Serena tool usage stats found.”** The same PR was mostly workflow-file changes, and no stats file was produced.
   - **Root cause:** Serena startup cost is being paid even when semantic symbol tooling may not be used.
   - **Exact change:** gate Serena startup on file types or diff shape; for YAML/workflow/comment-only reviews, bypass Serena initialization unless a later step explicitly requests it.
   - **Estimated time savings:** **8–20s** per such review run.
   - **Implementation risk:** **low** if the fallback path remains available.

6. **Reduce aggregate runner churn from skipped clarify/plan/respond/implement workflows**
   - **Evidence:** `clarify` p50 **1s** with **173/190** “other”; `plan` p50 **1s** with **155/168** “other”; `implement` p50 **1s** with **142/168** “other”; `orchestrate_clarify_respond` p50 **1s** with **165/168** “other**. Recent logs show many 1–2s skipped runs.
   - **Root cause:** triggers are broad, and workflows are deciding to skip after launch rather than before launch.
   - **Exact change:** tighten workflow-level event filters and top-level `if` guards so obviously inapplicable paths never start.
   - **Estimated time savings:** **small per run**, but meaningful aggregate runner-availability gains.
   - **Implementation risk:** **low** if rolled out one family at a time.

## Cost Optimizations

1. **Trim the two-pass six-reviewer panel for low-signal or superseded reviews**
   - **Evidence:** in `review_autofix` run `25123709478`, step **`Run reviewer models`** logged **427,088 tokens used**. The run used **6 reviewers**, two passes, and **xhigh** reasoning for both reviewer/editor paths.
   - **Root cause:** expensive broad-sweep + deep-review is being applied too often, including on work that may not merit a second pass.
   - **Exact change:** keep Pass 1 broad, but run Pass 2 only when Pass 1 finds disagreement, failing checks, code changes above a threshold, or unresolved findings. Also short-circuit entirely when the run is already superseded.
   - **Estimated savings:** **30–50% tokens** on heavy review runs.
   - **Quality-risk notes:** **medium**; keep Pass 2 mandatory for risky diffs, merge conflicts, or failing CI.

2. **Preflight MCP health and strip failed servers before Codex invocation**
   - **Evidence:** `implement` run `25091341828` documents repeated Azure/OpenRouter 400s from an invalid tools payload after MCP handshake failure; the same family has three failures, with one run lasting **3,437s** before failing.
   - **Root cause:** failed MCP servers still contribute malformed tool entries, causing every retry to fail identically.
   - **Exact change:** in MCP setup, probe each server handshake and exclude failed servers from the tool list before the first Codex request.
   - **Estimated savings:** avoids entire failed runs plus retry loops; likely **thousands of seconds** and large token waste per affected run.
   - **Quality-risk notes:** **low** if failed tools are removed fail-open rather than making the whole run fail.

3. **Lower reasoning level or model count for review Pass 2**
   - **Evidence:** `REVIEWER_REASONING_EFFORT: xhigh`, `EDITOR_REASONING_EFFORT: xhigh` in run `25123709478`; Pass 2 still used multiple reviewers after Pass 1 already succeeded across all six.
   - **Root cause:** the highest-cost reasoning mode is being used as a default escalation, not a selective escalation.
   - **Exact change:** switch Pass 2 to `high` by default, and reserve `xhigh` for conflict resolution, failing-check runs, or high-risk file classes.
   - **Estimated savings:** **10–25% tokens** on heavy review runs.
   - **Quality-risk notes:** **medium**; monitor escaped defects before making it the default everywhere.

4. **Make prompt-cache economics observable before investing more in prompt engineering**
   - **Evidence:** cache is usually enabled (`OPENROUTER_PROMPT_CACHE_DISABLED: false`), and cache probe lines appear in `25123709478`, but sampled OpenRouter usage still shows `prompt_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`.
   - **Root cause:** prompt cache may exist, but the logs do not reveal hit/miss or token deltas.
   - **Exact change:** emit per-call cache-read/cache-create counters for all production model calls, not just probes.
   - **Estimated savings:** enables targeted follow-up; direct savings cannot yet be quantified from current telemetry.
   - **Quality-risk notes:** **low**; observability-only change.

5. **Reduce repeated context expansion in `review_autofix`**
   - **Evidence:** the run builds large prompt bodies, symbol summaries, runtime context, check-run context, and two-pass reviewer summaries before consensus. The sampled run had a **15,963-byte** review summary prompt after already collecting six reviewer outputs.
   - **Root cause:** repeated prompt assembly around largely static policy/instruction blocks.
   - **Exact change:** keep static instructions in one cached prefix and append only run-specific artifacts after the cache breakpoint; avoid re-inserting unchanged policy blocks into each reviewer invocation.
   - **Estimated savings:** likely **single-digit to low-double-digit percent** token savings once cache metrics are visible.
   - **Quality-risk notes:** **low-medium**; ensure reviewers still receive all required context.

6. **Avoid wasted review spend on conflict-doomed runs**
   - **Evidence:** failed `review_autofix` run `25115530167` ended with `MERGE_CONFLICT=true`, `CONFLICT_RESOLVED=false`, `DID_COMMIT=false` after six reviewers had already finished.
   - **Root cause:** expensive review work continues even when the mergeability state makes commit completion unlikely.
   - **Exact change:** perform a merge/conflict preflight before launching the full reviewer/editor stack; if conflicted, branch directly to targeted conflict-resolution flow.
   - **Estimated savings:** **hundreds of thousands of tokens** on conflict-doomed runs.
   - **Quality-risk notes:** **low** if the conflict flow preserves the same final correctness checks.

## Reliability Improvements

1. **Harden MCP startup so failed servers cannot poison Codex requests**
   - **Failure evidence:** `implement` run `25091341828`, step `Run Codex implementation`, records repeated Azure/OpenRouter **400** errors tied to `function: undefined` in the tool list after MCP initialize failure.
   - **Root cause category:** external integration / tool bootstrap.
   - **Exact fix:** handshake-probe each MCP server; if it fails, omit it from Codex config and continue with remaining tools.
   - **Expected reliability impact:** should remove the dominant hard-failure mode across the sampled `implement` failures.
   - **Rollback/fail-open:** if probing misbehaves, fall back to current behavior behind a flag.

2. **Validate orchestrator decomposition outputs before issue creation**
   - **Failure evidence:** `test_and_mark_stable` runs `25088532565` and `25115169454` both failed because the decomposition produced **1 child issue** when the test required **>=2**.
   - **Root cause category:** orchestration/spec enforcement gap.
   - **Exact fix:** add a deterministic decomposition validator for child count, wave count, and dependency edges, with one auto-repair re-prompt.
   - **Expected reliability impact:** should materially improve the current **0% success** rate of this family in the sampled window.
   - **Rollback/fail-open:** if validation fails unexpectedly, emit a clear artifact and stop before creating partial issue trees.

3. **Move merge/conflict detection ahead of the expensive reviewer/editor path**
   - **Failure evidence:** run `25115530167` failed after full reviewer work with `CONFLICT_RESOLVED=false`.
   - **Root cause category:** flow ordering / late conflict detection.
   - **Exact fix:** add early mergeability preflight and route conflicted PRs straight to conflict preparation/resolution logic.
   - **Expected reliability impact:** fewer terminal review failures and fewer “no-op suspicious” dead ends.
   - **Rollback/fail-open:** if preflight cannot determine state, continue into existing review flow.

4. **Make `workflow_log_analysis` bounded enough to stop breaking release validation**
   - **Failure evidence:** all sampled `workflow_log_analysis` runs cancelled; `orphan-workflows-test` then failed release validation because the downstream analysis run concluded `cancelled`.
   - **Root cause category:** timeout/overwork on secondary validation.
   - **Exact fix:** reduce analysis breadth, limit families/repos processed per pass, and checkpoint partial results earlier.
   - **Expected reliability impact:** should eliminate a deterministic cancellation chain that currently blocks stable-release validation.
   - **Rollback/fail-open:** on partial analysis, mark the analysis incomplete but preserve the main test signal.

5. **Fix the CI regression around success-no-op detection**
   - **Failure evidence:** CI run `25094556541` failed `test_codex_success_detection_uses_baseline_diff`; result was **26 passed, 1 failed**.
   - **Root cause category:** regression in post-Codex recovery logic.
   - **Exact fix:** restore the explicit success/no-op signal contract used by `test_implement_post_codex_recovery.py`.
   - **Expected reliability impact:** eliminates the only sampled CI failure and stabilizes post-Codex recovery behavior.
   - **Rollback/fail-open:** revert to previous success-detection heuristic if the stricter path causes false negatives.

## AI Memory Health

- **Memory telemetry was observed** in the sampled logs.
- Across sampled runs, there were **12 `retrieve` operations**.
  - **Hit rate:** **50.0%** (`6/12` had `records_selected > 0`)
  - **Average `estimated_tokens`:** **14.0**
  - **`keyword_method` distribution:** `plain` **6**, `none` **6**, `llm` **0**
- **Zero-record retrieves:** **6/12**
  - All sampled `review_autofix` retrieves returned **0 records** with `keyword_method: none` and `estimated_tokens: 0` (e.g. runs `25115530167`, `25089325371`, `25103490010`, `25105660638`, `25123709478`).
  - All sampled `implement` retrieves returned **1 record** with `keyword_method: plain` and `estimated_tokens: 28` (e.g. runs `25091341828`, `25092547530`, `25115618107`).
- **`fail_open: true` telemetry entries:** **none observed**
- **`enabled: false` telemetry entries:** **none observed**
- **Push retry health:** all sampled `record-run-event` and `record-candidate` operations succeeded with **`push_attempts: 1`**, so no push-retry hotspot is visible in this window.
- **Gap to fix:** run `25126746595` logged **“AI memory disabled; skipping lineage finalization.”** in `issue_pr_status`, but that disabled path did not appear as structured `AI_MEMORY_TELEMETRY`. Telemetry coverage is therefore not fully uniform across memory-disabled branches.

## GH API Call Audit

1. **Release-test polling loops are the highest visible API hot spot**
   - **Evidence:** in `test_and_mark_stable` run `25088532565`, `orchestrate-decompose-test` polled the orchestrator bootstrap roughly every **20s**, reaching at least **13 status checks** before completion. Similar `PRE -> NEW_ID -> JSON status` polling appears in `validate-standalone`, `orphan-workflows-test`, and the second failed stable run `25115169454`.
   - **Pattern:** repeated REST polling of workflow runs and run status.
   - **Concrete change:** after run registration, back off polling to **20s → 30s → 45s → 60s**, or switch to a single watch loop helper reused by all release-test steps.
   - **Estimated reduction:** **40–60% fewer polling calls** on 5–60 minute waits.
   - **Rate-limit risk reduction:** moderate.

2. **Implement does a redundant issue fetch early in the run**
   - **Evidence:** `implement` runs fetch issue labels first (`issues/<n> --jq '[.labels[].name]'`), then later fetch full issue metadata again (`gh api .../issues/<n> > ISSUE_META_FILE`), plus paginated comments.
   - **Pattern:** unbatched repeated lookup for the same issue.
   - **Concrete change:** fetch the full issue JSON once, derive labels from that response, then fetch comments only if needed.
   - **Estimated reduction:** about **1 REST call per implement run**.
   - **Rate-limit risk reduction:** low individually, useful at scale.

3. **Cleanup in failed orchestrate-decompose tests performs duplicate issue-close calls**
   - **Evidence:** run `25088532565` closed `#1747`, `#1748`, then `#1747` again; run `25115169454` closed `#1776`, `#1777`, then `#1776` again.
   - **Pattern:** duplicate PATCHes in cleanup loops.
   - **Concrete change:** de-duplicate the union of tracking/child/straggler issue numbers before closing.
   - **Estimated reduction:** **1–2 PATCH calls** per failed decompose test.
   - **Rate-limit risk reduction:** low, but correctness improves too.

4. **`issue_pr_status` is a good example of the repo’s intended API hygiene**
   - **Evidence:** run `25126746595` explicitly documents a **“Single batched GraphQL call (one API request regardless of N)”** for linked issue status handling, with fail-open REST fallback.
   - **Pattern:** batched GraphQL + minimal REST.
   - **Concrete change:** replicate this pattern in other N-item flows, especially release-test issue discovery and any per-issue status fanout.
   - **Estimated reduction:** medium in aggregate.
   - **Rate-limit risk reduction:** moderate-high.

5. **No concrete rate-limit incidents were observed in sampled logs**
   - **Evidence:** many workflows define rate-limit-aware retry wrappers, but sampled excerpts did not show actual `403/429` backoff events firing.
   - **Implication:** focus first on call-volume reduction and de-duplication rather than retry tuning.

## MCP & Serena Efficiency

- **Observed issue: Serena setup cost is not consistently yielding measurable usage.**
  - In `review_autofix` run `25123709478`, Serena was installed and initialized, but later `Log token usage and Serena stats` reported **“No Serena tool usage stats found.”**
  - The same run’s `Generate Serena efficiency report` step warned that required runtime context was missing and skipped report generation.
  - This makes it impossible to verify whether the agent used targeted Serena operations or fell back to broader reads.

- **Observed issue: full Serena bootstrap is paid even on workflow-heavy changes.**
  - The same review run was centered on workflow files, yet still staged `.serena`, setup scripts, and support assets.
  - This is likely low-value overhead on YAML/comment-centric reviews.

- **Observed issue: GitHub/exec fallback appears active in reviewer reasoning traces.**
  - In run `25123709478`, the reviewer transcript shows direct shell inspection (`head`, `cat`) of workflow files and symbol summary artifacts.
  - That does not prove Serena misuse, but combined with the missing stats file it suggests the intended “semantic-first” path is not observable.

- **Concrete recommendations:**
  1. **Make Serena stats emission mandatory** when Serena is initialized; treat missing `tool_usage_stats.json` as a warning surfaced in the job summary.
  2. **Skip Serena startup** for workflow-only, docs-only, or comment-only diffs.
  3. **Precompute and pass symbol summaries only when code files changed**; avoid the full semantic-tooling bootstrap otherwise.
  4. **Parallelize safe read-only prep steps**: PR metadata fetch, last-run diff retrieval, and static-context assembly can overlap because they read independent inputs.
  5. **Keep raw-file fallback**, but report when the fallback was used so Serena ROI can be measured.

## Prompt Cache & Memory System

- **Prompt cache is enabled but not measurable enough yet.**
  - Sampled runs consistently showed `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
  - `review_autofix` run `25123709478` logged cache probes, but the emitted usage lines still showed `prompt_tokens=na`, `cache_creation_input_tokens=na`, and `cache_read_input_tokens=na`.
  - Result: the system may already be caching effectively, but current telemetry cannot prove it.

- **Cache fragmentation risk appears non-trivial.**
  - Review runs assemble large, highly dynamic runtime contexts containing PR numbers, SHAs, temp paths, branch names, and artifact file paths.
  - A dedicated `Pre-assemble static context cacheable across runs` step exists, which is good, but the absence of cache-read metrics means it is unclear whether the static prefix is actually staying stable enough.

- **Memory retrieval is useful in `implement` but mostly ineffective in `review_autofix`.**
  - `implement`: all sampled retrieves hit **1 record** at ~**28 tokens**.
  - `review_autofix`: all sampled retrieves hit **0 records** with **0 estimated tokens**.
  - That suggests the reviewer-memory path may be low-value or under-keyed.

- **Concrete recommendations:**
  1. **Emit real cache read/create token counters for every model call.**
     - Impact: unlocks evidence-based prompt optimization.
  2. **Move all run-unique metadata behind the cache breakpoint.**
     - Impact: improves cache reuse probability; token/latency savings likely medium.
  3. **Tune reviewer-memory retrieval keywords or skip retrieval when no reviewer-specific corpus exists.**
     - Impact: small latency savings and cleaner telemetry.
  4. **Add structured telemetry for memory-disabled branches.**
     - Impact: improves trust in health dashboards and fail-open behavior auditing.
  5. **Preserve the existing fail-open posture.**
     - Evidence: no sampled `fail_open: true` telemetry incidents caused visible job failure.

## Orchestrator Health

- **The orchestrator poller itself looks healthy.**
  - `orchestrate_poll` has **22/22 successes**, **p50 45.5s**, **p95 113.7s**.
  - Recent run `25125062970` completed successfully in **47s** and recorded both `poll_started` and `poll_completed` memory events.

- **The main orchestrator weakness is decomposition quality, not the poll loop.**
  - Stable-release failures were caused by under-decomposition, not by poller errors.
  - This points to a correctness gap in the bootstrap/decomposition stage.

- **Skipped workflow fanout suggests an overly chatty orchestrator perimeter.**
  - The very high skipped counts across clarify/plan/respond/implement indicate many workflow entries are triggered only to decide “not applicable.”
  - This is not catastrophic per run, but it is a health smell: it obscures real latency and consumes runner scheduling.

- **Recurring pain point: long-running secondary workflows cancel inside larger orchestrated test flows.**
  - `workflow_log_analysis` cancellation cascades into release validation failures.
  - This is a classic orchestrator dependency-health problem: secondary analysis is behaving like a hard dependency without being bounded tightly enough.

- **Smallest safe mitigations:**
  1. Add deterministic decomposition validation/repair before issue creation.
  2. Reduce skipped workflow fanout with tighter top-level triggers.
  3. Bound secondary analysis workloads so they cannot exceed their orchestration budget.

- **Indicators to track after changes:**
  - `test_and_mark_stable` success rate
  - `% of orchestrate smoke runs producing >=2 child issues`
  - `review_autofix` cancelled-after-300s count
  - skipped-run counts for `clarify`, `plan`, `implement`, `orchestrate_clarify_respond`
  - `workflow_log_analysis` cancellation rate
  - reviewer-memory retrieve hit rate
  - prompt-cache read/create token counters once exposed

## Pipeline Flow Bottlenecks

1. **Review/autofix compute is the dominant AI bottleneck**
   - `review_autofix` long runs reach **1,200–2,800s**, with p95 **1,861.2s**.
   - Heavy two-pass reviewer panels dominate both latency and token burn.

2. **Stable-release orchestration is the dominant end-to-end bottleneck**
   - `test_and_mark_stable` p50 **2,491s**, p95 **5,623.5s**, zero successes.
   - Failures combine long dispatch/poll time with downstream cancellation of ancillary analysis.

3. **Retry overhead is concentrated in MCP/provider failures**
   - `implement` failures ran for **705s, 1,394s, 3,437s** because the same bad MCP/tool payload kept retrying.
   - This is pure avoidable retry cost.

4. **Merge/conflict overhead is late and expensive**
   - Sampled failed review run consumed the review stack and only then discovered an unresolved conflict state.
   - This is a flow-ordering problem more than a model-quality problem.

5. **Queueing exists but is secondary**
   - Many system logs show brief “waiting for a hosted runner,” but sampled waits were small relative to compute time.
   - Queueing is not the dominant bottleneck in this window.

6. **Skipped-workflow overhead is broad but shallow**
   - Clarify/plan/respond/implement skipped runs are cheap individually but create orchestration clutter and consume slots/events.
   - This is more of an aggregate efficiency issue than a single-run critical path issue.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `test_and_mark_stable` release-validation path: slow and currently non-passing
  - `review_autofix` heavy two-pass reviewer compute and cancellations
  - `workflow_log_analysis` timing out inside broader validation flows

- **Top failure modes**
  - Orchestrator decomposition under-producing child issues (`25088532565`, `25115169454`)
  - MCP handshake/tool-list corruption causing `implement` failures (`25091341828`)
  - Late unresolved merge conflict in `review_autofix` (`25115530167`)
  - Single CI regression in post-Codex recovery tests (`25094556541`)

- **Highest-cost drivers**
  - Two-pass six-reviewer `review_autofix` runs, especially at `xhigh` reasoning
  - Long cancelled `workflow_log_analysis` runs
  - Retry-heavy `implement` failures that never had a chance to succeed

- **Top 3 prioritized actions**
  1. **Sanitize failed MCP servers before Codex requests** to stop `implement` hard failures.
  2. **Bound `workflow_log_analysis` and add decomposition validation** so stable-release validation can complete successfully.
  3. **Add early supersede/conflict gates in `review_autofix` and narrow Pass 2 scope** to cut both latency and token burn.

## Metrics Appendix

### Repo Summary

| Repository | Total Runs | Success | Failure | Cancelled | Other | Failure Rate | Avg Duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 267 | 8 | 89 | 636 | 0.8% | 157.4 | 1.0 | 636.0 |

### Key Workflow Family Metrics

| Workflow Family | Total | Success | Failure | Cancelled | Other | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 78 | 77 | 1 | 0 | 0 | 600.5 | 606.5 | 641.6 |
| implement | 168 | 19 | 3 | 4 | 142 | 63.8 | 1.0 | 254.0 |
| review_autofix | 125 | 44 | 1 | 79 | 1 | 457.2 | 39.0 | 1861.2 |
| test_and_mark_stable | 4 | 0 | 2 | 2 | 0 | 2871.3 | 2491.0 | 5623.5 |
| workflow_log_analysis | 4 | 0 | 0 | 4 | 0 | 3750.5 | 3524.0 | 5397.7 |
| orchestrate_poll | 22 | 22 | 0 | 0 | 0 | 59.2 | 45.5 | 113.7 |
| clarify | 190 | 17 | 0 | 0 | 173 | 13.5 | 1.0 | 121.6 |
| plan | 168 | 13 | 0 | 0 | 155 | 17.4 | 1.0 | 167.5 |
| orchestrate_clarify_respond | 168 | 3 | 0 | 0 | 165 | 1.4 | 1.0 | 2.0 |

### Notable Failed / Slow Runs Used

| Run ID | Family | Conclusion | Duration s | Failure Point / Note |
|---|---|---|---:|---|
| 25088532565 | test_and_mark_stable | failure | 5967 | `orchestrate-decompose-test` produced 1 child issue, expected >=2 |
| 25115169454 | test_and_mark_stable | failure | 3677 | same under-decomposition failure |
| 25091341828 | implement | failure | 3437 | MCP/OpenRouter/Azure invalid tool payload failure |
| 25092547530 | implement | failure | 1394 | implement hard failure |
| 25115618107 | implement | failure | 705 | implement hard failure |
| 25115530167 | review_autofix | failure | 1508 | 6 reviewers succeeded, ended with unresolved merge conflict |
| 25094556541 | ci | failure | 539 | one failing unit test in post-Codex recovery suite |
| 25123709478 | review_autofix | success | 1243 | sampled heavy-cost successful review run |
| 25125062970 | orchestrate_poll | success | 47 | sampled healthy poller run |

### Sampled Token / Model Metrics

| Run ID | Family | Step | Observed Token Metric | Notes |
|---|---|---|---|---|
| 25123709478 | review_autofix | `Run reviewer models` | **427,088 tokens used** | 6 reviewers, 2 passes |
| 25123709478 | review_autofix | same | reviewer reasoning=`xhigh`, editor reasoning=`xhigh` | summariser model=`openai/gpt-5.4-mini`, reasoning=`medium` |
| other sampled runs | multiple | OpenRouter usage lines | `prompt_tokens=na`, `cache_*_tokens=na` | observability gap; no aggregate token totals |

### Prompt Cache / Memory Metrics

| Metric | Value |
|---|---:|
| Prompt cache enabled in sampled runs | yes (`OPENROUTER_PROMPT_CACHE_DISABLED: false`) |
| Structured cache hit/miss token counters | not reliably present |
| `retrieve` telemetry count | 12 |
| `retrieve` hit rate | 50.0% |
| Avg `estimated_tokens` on retrieve | 14.0 |
| `keyword_method=plain` | 6 |
| `keyword_method=none` | 6 |
| `keyword_method=llm` | 0 |
| `retrieve` zero-record count | 6 |
| `enabled:false` telemetry entries | 0 |
| `fail_open:true` telemetry entries | 0 |
| Avg push attempts for `record-run-event` / `record-candidate` | 1.0 |

### GH API Summary (Pattern-Based; Exact Totals Not Available in Window)

| Workflow / Step | Observed Pattern | Audit Result |
|---|---|---|
| `test_and_mark_stable` / dispatch-poll steps | repeated `gh api` polling of workflow runs every ~20s | main high-volume hotspot |
| `implement` / startup | issue labels fetch + full issue fetch + comments fetch | one redundant issue GET per run |
| `orchestrate-decompose-test` cleanup | duplicate close calls on same tracking issue | de-duplicate close list |
| `issue_pr_status` | single batched GraphQL call plus minimal REST | good pattern; reuse elsewhere |
| sampled wrappers | rate-limit-aware retry helpers widely present | good hygiene; no actual rate-limit events observed |

If you want, I can turn this into a shorter executive memo or a backlog-style prioritized action list with owners and expected effort.

## Deep Audit — Workflows & Scripts (2026-04-29)

### Section 1: Bug & Correctness Sweep

- **ID** — `BUG-001`  
  **File path** — `.github/workflows/plan.yml:404-427`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The `Skip when issue already has a PR` step computes `LINKED_PR_COUNT` with `gh api --paginate ... --jq '[...] | length'`, then compares it numerically with `[ "${LINKED_PR_COUNT}" -gt 0 ]`. With more than one timeline page, `gh` emits one count per page, so `LINKED_PR_COUNT` becomes a multi-line string like `1\n0`; the numeric test then errors under `set -euo pipefail`. Busy issues with long timelines can therefore fail the planning workflow instead of cleanly skipping.  
  **Recommended fix** — Slurp paginated timeline pages before counting, e.g. `gh_retry_to_file <tmp> gh api --paginate ...` followed by `jq -s 'add // [] | [ ... ] | length'`. The repo already uses this safe pattern in `scripts/label_helpers.sh:160-163` and `scripts/orchestrate_poll_process.sh:7644-7649`.

- **ID** — `BUG-002`  
  **File path** — `.github/workflows/review_autofix.yml:486-495,4500-4505; scripts/review_rb_judge.sh:153-156`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — These fallback issue-extraction paths accept bare `issues/123` and `issue #123` mentions from PR title/body, not just explicit closing references. That is materially looser than the hardened fallback in `.github/workflows/issue_pr_status.yml:196-210`, which intentionally limits matches to closing keywords and repo-scoped links to avoid false-positive issue mutation. In the affected paths, a prose mention can drive real side effects: standalone validation dispatch / label removal in `review_autofix.yml`, and wrong issue context or labeling in `review_rb_judge.sh`.  
  **Recommended fix** — Centralize fallback linked-issue extraction and reuse the stricter `issue_pr_status.yml` contract: only GraphQL `closingIssuesReferences`, explicit closing keywords, or repo-scoped issue URLs/paths should be allowed to mutate issue state.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — `API-001`  
  **File path** — `scripts/orchestrate_poll_process.sh:3407-3413,3427-3468,3500-3519`  
  **Severity** — Medium  
  **Category tag** — `api-redundancy`  
  **Description** — `ensure_final_merge` refetches the same final PR repeatedly to read `.state`, `.mergeable`, and `.merged_at`. On the normal path it does 2 GETs before the main mergeability gate, then 3 more GETs immediately after state persistence, then 3 more after a failed merge attempt. **Current call count:** 6 GETs per tick for one PR on the steady-state path. **Proposed call count:** 2 GETs (one cached fetch before merge logic, one cached fetch after a merge attempt). This is also a correctness drag because the fields are read from different responses, widening race windows.  
  **Recommended fix** — Reuse a single `_fetch_pr_json "${final_pr}"` payload and parse all needed fields from it, then refresh once after the merge attempt. Extend the existing cycle-local cache pattern already used later in the same script at `scripts/orchestrate_poll_process.sh:6593-6607,6810-6820`.

- **ID** — `BATCH-001`  
  **File path** — `.github/workflows/review_autofix.yml:485-530`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — In the post-merge standalone-validation path, once GraphQL fails to return linked issue nodes and the workflow falls back to regex-derived issue numbers, it loops and calls `gh issue view ... --json labels` once per issue. **Current call count on the fallback path:** `1 + N` fetches (1 PR-level GraphQL query + N issue-label REST calls). **Proposed call count:** `1` additional batched lookup regardless of N by hydrating labels for all derived issue numbers in one GraphQL alias query. Existing repo pattern: `.github/workflows/issue_pr_status.yml:280-330` batches arbitrary issue-number metadata in a single GraphQL request.  
  **Recommended fix** — After building `issue_nodes_json` from fallback issue numbers, run one alias-batched GraphQL query to fetch labels for all of them, then drive the loop from that payload instead of calling `gh issue view` inside the loop.

- **ID** — `BATCH-002`  
  **File path** — `scripts/review_rb_judge.sh:146-170`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The review-blocked judge first fetches linked issue numbers via GraphQL, then fetches each issue body with a separate REST call. **Current call count:** `1 + N`. **Proposed call count:** `1`. The repo already has a direct example of the richer batched query in `.github/workflows/review_autofix.yml:1381-1439`, which fetches `closingIssuesReferences { number title body }` in a single call and builds context files from it.  
  **Recommended fix** — Expand the judge’s GraphQL selection set to include `title` and `body`, and populate `FIRST_ISSUE` / `FIRST_ISSUE_BODY` from that single payload.

- **ID** — `BATCH-003`  
  **File path** — `scripts/orchestrate_poll_process.sh:8880-8915`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The phase-agnostic feature sweep lists open PRs once, then issues an extra `GET /pulls/{n}` for every `behind` PR just to recover `head.sha` before calling `update-branch`. **Current call count:** `1 + B`, where `B` is the number of behind PRs on that poll tick. **Proposed call count:** `1` [NEEDS VERIFICATION] if the sweep switches to a GraphQL shape that includes the head OID in the initial batch. Existing batching pattern to extend: the same script’s GraphQL batching helpers and the alias-query technique used in `issue_pr_status.yml`.  
  **Recommended fix** — Replace the `gh pr list` + per-PR REST follow-up with one GraphQL batch that returns `number`, `headRefName`, `headRefOid`, `mergeStateStatus`, and `mergeable` for the candidate PR set, then pass the cached `headRefOid` into `update-branch`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — `DUP-001`  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/review_autofix.yml:1273-1307; .github/workflows/mark-stable.yml:199-228,322-351; .github/workflows/orchestrate_poll.yml:66-92`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Four workflows carry near-identical inline rate-limit/retry implementations (`_rl_wait`, `_gh_retry`, local `gh_retry`). The copies already differ in attempt budgets, stderr handling, and backoff policy, so operational behavior is drifting by workflow.  
  **Recommended fix** — Make `scripts/gh_helpers.sh` the single owner of retry behavior and remove the inline copies. Shared owner/module: `scripts/gh_helpers.sh`. Shared interface: `gh_retry <command...>` and `gh_retry_to_file <outfile> <command...>`. Callers to update: `cancel_on_pr_close.yml`, `review_autofix.yml`, `mark-stable.yml`, and `orchestrate_poll.yml`.

- **ID** — `DUP-002`  
  **File path** — `.github/workflows/review_autofix.yml:577-580,3653-3676,3776-3796,4475-4491; .github/workflows/issue_pr_status.yml:241-248; scripts/label_helpers.sh:108-195`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Label creation and resilient phase-label mutation are implemented repeatedly inline in workflow YAML instead of reusing `scripts/label_helpers.sh`. That duplicates color/description mappings and label-mutation fallback logic, making the repo vulnerable to catalog drift.  
  **Recommended fix** — Make `scripts/label_helpers.sh` the canonical owner of `ensure_label_exists <label_name> <repo>` and `set_issue_phase_label_resilient <issue_number> <target_label> <repo>`. Update `review_autofix.yml` and `issue_pr_status.yml` to source the helper instead of defining local variants.

- **ID** — `DUP-003`  
  **File path** — `.github/workflows/clarify.yml:218-262; .github/workflows/plan.yml:245-287; .github/workflows/implement.yml:389-444; .github/workflows/orchestrate.yml:318-370; .github/workflows/orchestrate_poll.yml:266-363; .github/workflows/orchestrate_clarify_respond.yml:259-360; .github/workflows/validate.yml:188-320`  
  **Severity** — Low  
  **Category tag** — `duplication`  
  **Description** — Seven workflows repeat the same “checkout coding-workflows support files / fall back to main / install scripts + prompts + schemas” bootstrap logic. The blocks are no longer identical, which means a hardening fix in one bootstrap path must be hand-ported to six others.  
  **Recommended fix** — Extract a shared bootstrap owner such as `scripts/fetch_workflow_support.sh`. Suggested interface: `fetch_workflow_support <phase> <script_ref> <workflow_source_repo> <primary_checkout_dir> <fallback_checkout_dir>`. Update all seven callers to invoke the shared script.

### Section 4: Expression Size Limit Risk Assessment

- **ID** — `EXPR-001`  
  **File path** — `.github/workflows/validate.yml:188-481`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The `Fetch workflow support files` step is approximately **16,529 characters** of `run:` body with `${{ }}` interpolations present, leaving roughly **4,471 characters** of headroom before GitHub’s 21,000-character template ceiling. That is already above the repo’s requested 15,000-character warning threshold. [NEEDS VERIFICATION]  
  **Recommended fix** — Extract the step into an external script under `scripts/` or split it into smaller “checkout support refs” / “copy assets” steps. The repo already uses this mitigation pattern successfully for `scripts/review_conflict_prepare.sh` and `scripts/review_commit_changes.sh`.

- **ID** — `EXPR-002`  
  **File path** — `.github/workflows/review_autofix.yml:1266-1588`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — `Collect PR metadata` is approximately **16,437 characters**, leaving about **4,563 characters** of headroom. This workflow has already been one of the repo’s known expression-limit hotspots, so this block is close enough to warrant preemptive extraction. [NEEDS VERIFICATION]  
  **Recommended fix** — Move the metadata-fetch / context-assembly logic into a dedicated script such as `scripts/collect_pr_review_context.sh`, leaving only a thin YAML wrapper.

- **ID** — `EXPR-003`  
  **File path** — `.github/workflows/orchestrate_clarify_respond.yml:840-1123`  
  **Severity** — Medium  
  **Category tag** — `expression-limit`  
  **Description** — The long interpolated clarify-response generation block is approximately **15,140 characters**, leaving about **5,860 characters** of headroom. It has crossed the medium-risk threshold and is still an inline heredoc-heavy block that can grow accidentally. [NEEDS VERIFICATION]  
  **Recommended fix** — Extract prompt/context construction to an external script or split the step into separate “prepare prompt” and “run Codex” steps.

**Workflow file size note:** no audited workflow exceeds the 800 KB warning threshold. The largest file is `.github/workflows/review_autofix.yml` at **260,537 bytes**.

### Section 5: Cross-Cutting Concerns

- **ID** — `DEAD-001`  
  **File path** — `scripts/mark-stable.sh:1-14`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — Repository-wide reference search found no workflow or script invoking `scripts/mark-stable.sh`; the only hit is the file’s own usage comment. That makes it dormant release-mutating code that is outside the exercised pipeline.  
  **Recommended fix** — Either remove it after approval, or have `mark-stable.yml` call it so stable-tag mutation has one exercised code path.

- **ID** — `CONSIST-001`  
  **File path** — `.github/workflows/review_autofix.yml:517-527; .github/workflows/workflow-log-analysis.yml:346-359,767-770,1089-1091`  
  **Severity** — Low  
  **Category tag** — `consistency`  
  **Description** — These mutation paths use raw `gh workflow run`, `gh issue comment`, and `gh issue edit` calls instead of the repo-standard `gh_retry` wrapper. The rest of the automation stack treats GitHub API rate limits and secondary limits as first-class failures; these blocks silently degrade instead.  
  **Recommended fix** — Source `scripts/gh_helpers.sh` in these steps and wrap mutating GH calls with `gh_retry`, keeping failure semantics consistent with `orchestrate_poll_process.sh`, `label_helpers.sh`, and the rest of the repo.

- **ID** — `SHELL-001`  
  **File path** — `scripts/review_commit_changes.sh:448-455; scripts/review_conflict_resolve.sh:852-853`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — Both scripts call `git remote set-url origin https://x-access-token:${GH_PAT}@github.com/${...}` without quoting the URL argument, which ShellCheck flags as SC2086. That leaves the command vulnerable to shell word-splitting/globbing if the token or slug is ever unexpectedly formatted.  
  **Recommended fix** — Quote the entire URL argument, matching safer patterns already used in workflows such as `.github/workflows/implement.yml:317`.

- **ID** — `SHELL-002`  
  **File path** — `scripts/review_apply_fixes.sh:1002-1034`  
  **Severity** — Low  
  **Category tag** — `shellcheck`  
  **Description** — Fallback artifact discovery uses `ls -1 ... | sort -V | tail -n 1`, which ShellCheck flags as SC2012. Filenames containing whitespace or newlines can break selection of the latest `editor_attempt_*` artifact.  
  **Recommended fix** — Replace the `ls` pipeline with `find`/glob-based enumeration and null-delimited sorting.

**TODO/FIXME/HACK scan:** no `TODO`, `FIXME`, or `HACK` markers were found in the audited workflow/script files.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 11 | BUG-001, BUG-002, API-001, BATCH-001, BATCH-002, BATCH-003, DUP-001, DUP-002, EXPR-001, EXPR-002, EXPR-003 |
| Low | 5 | DUP-003, DEAD-001, CONSIST-001, SHELL-001, SHELL-002 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 3 | Medium |
| Code modularization | 10 | Large |
| Expression size reduction | 3 | Medium |
| Medium/Low fixes | 8 | Medium |
