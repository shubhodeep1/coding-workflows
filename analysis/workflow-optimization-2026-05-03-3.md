## Executive Summary

- **The dominant release blocker is the stable-release smoke gate, not generic CI instability.** `test_and_mark_stable` failed in **5/5 runs** with durations of **3,676s to 6,255s**, mostly at `e2e-smoke-test` (`Phase 3b: Wait for PR creation` or `Phase 4b: Verify editor removed bait line`). Fixing this path should recover **60–100 minutes per blocked release attempt**. **Estimated impact:** very high. **Confidence:** high.

- **Implement-mode no-op/narration failures are creating both latency and token waste.** The failed implement run `25272034874` blocked PR creation, and the associated follow-up diagnostics recorded **31,395 tokens** across two futile attempts (`20,488` + `10,907`) before bailing. A deterministic plain-text-file path plus slimmer retry logic should cut both reruns and token burn. **Estimated impact:** high. **Confidence:** high.

- **Review/autofix is over-provisioned for many PRs.** `review_autofix` has **42 cancellations out of 81 runs**, with **p95 = 2,062s**. In run `25272986802`, pass 1 completed with **6 reviewer models**, then the workflow was canceled as pass 2 began, meaning expensive work was performed before obsolescence. **Estimated impact:** 8–20 minutes saved on stale/small-diff reviews. **Confidence:** high.

- **CI is reliable enough overall, but its critical-path duration is consistently high.** `ci` succeeds in **69/75 runs** but averages **591s** with **p50 = 606s** and is dominated by the single `lint` job in recent runs `25272071199`, `25272986771`, and `25273014592`. Parallelizing or re-tiering required checks should save **4–6 minutes per PR**. **Estimated impact:** high. **Confidence:** high.

- **Prompt cache is enabled but effectively unmeasurable, and Serena setup overhead is often not paying off.** Review run `25272986802` logged `OPENROUTER_PROMPT_CACHE_DISABLED: false`, but cache counters were `na`, and the same run ended with **“No Serena tool usage stats found.”** This means prompt-cache optimization and Serena ROI are currently guesswork. **Estimated impact:** medium token/latency savings, plus better observability. **Confidence:** medium.

- **AI memory is healthy operationally but weak on the review path.** Deep-dive telemetry shows **8 retrieve operations**, **75% hit rate**, and **42 average estimated tokens**, but both reviewer retrievals in run `25272986802` returned **0 records** with `keyword_method: "none"`. Memory is helping implementation more than review. **Estimated impact:** modest quality/reliability gain. **Confidence:** high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Unblock `test_and_mark_stable` by fixing implement→PR creation detection and plain-text edit execution
- **Evidence**
  - `test_and_mark_stable` is **5/5 failed**, avg **5,259s**, p50 **5,858s**.
  - Run `25271960656` failed at `e2e-smoke-test / Phase 3b: Wait for PR creation (implement phase)` after **3,676s**.
  - Runs `25249170035`, `25252918179`, `25254380200`, and `25265920645` failed at `Phase 4b: Verify editor removed bait line` after **4,457s–6,255s**.
  - In `25271960656`, the gate emitted `All 1 implement workflow run(s) completed but no PR was created`.
  - The failed implement run `25272034874` was a smoke test for a single plain-text file update.
- **Root cause**
  - Critical-path failure in implement-mode for plain-text file tasks, followed by long poll/verification loops in the release smoke workflow.
- **Exact change**
  - Promote the plain-text-file exception fix to the branch used by stable-release smoke runs before re-running release gating.
  - In `test_and_mark_stable`, change the implement-phase detector to:
    1. check implement run conclusion,
    2. inspect any implementation-failure artifact/comment,
    3. do one final direct PR existence query by branch after the run completes,
    4. only then fail `Wait for PR creation`.
- **Estimated time savings**
  - **60–100 minutes per blocked release attempt**.
- **Implementation risk**
  - **Low-medium**. Behavior stays backward-compatible; only detection and task routing change.
- **Type**
  - **Critical-path win**.

### 2. Split or parallelize the monolithic `ci/lint` critical path
- **Evidence**
  - `ci` avg **591s**, p50 **606s**, p95 **643s**.
  - Recent successful runs `25272071199`, `25272986771`, `25273014592` all spent ~**594–600s** in `lint`.
  - In `25273014592`, substeps like `Orchestrate lib unit tests` and `Python syntax check` took only **~3s** and **~2s**, so the bottleneck is the bundled job envelope, not a single long test.
- **Root cause**
  - Too many unrelated checks are serialized under a single required job.
- **Exact change**
  - Split `lint` into at least two required jobs:
    - **fast contracts/syntax**: prompt contracts, workflow/script refs, YAML/actionlint, Python syntax
    - **slower unit suites**: semantic cache, orchestrate lib, validation/self-test suites
  - Keep branch protection on the fast lane if needed; leave slower lane required only where appropriate.
- **Estimated time savings**
  - **4–6 minutes on the PR critical path** if fast checks unblock earlier.
- **Implementation risk**
  - **Medium** due to check-name/branch-protection coordination.
- **Type**
  - **Critical-path win**.

### 3. Gate `review_autofix` second pass and reviewer fan-out by diff size/staleness
- **Evidence**
  - `review_autofix`: **81 total**, **42 canceled**, **p95 2,062s**.
  - Run `25272986802` lasted **1,229s**; pass 1 completed with **6 successful reviewers**, summarization succeeded, then pass 2 began and the run was canceled.
  - A deterministic skip already exists for docs-only PRs in run `25272071242`.
- **Root cause**
  - Multi-model, two-pass review is being applied before confirming the run is still the newest useful review for the branch/PR.
- **Exact change**
  - Add a stale-run check immediately before pass 2.
  - Restrict two-pass review to:
    - larger diffs,
    - non-doc-only changes,
    - PRs with failing checks or workflow changes.
  - For tiny canary/hotfix PRs, run a reduced reviewer set or pass 1 only.
- **Estimated time savings**
  - **8–20 minutes** on stale or low-risk review runs.
- **Implementation risk**
  - **Medium**; review depth changes must be carefully gated.
- **Type**
  - **Critical-path win**.

### 4. Reduce background runtime of `workflow_log_analysis`
- **Evidence**
  - `workflow_log_analysis` avg **4,862s**, p50 **5,476s**, p95 **5,828s**.
  - Slow runs `25249181773`, `25254390226`, `25265928747`, `25271970949` were **3,290s–5,875s**.
  - In `25271970949`, telemetry shows `summarize_unselected_runs` processed **82** runs and consumed **242,097 tokens**.
- **Root cause**
  - Very broad summarization scope and long Codex audit passes for a non-critical background workflow.
- **Exact change**
  - Skip summarization for trivially skipped 0–2s runs.
  - Reuse prior `log_summary` for unchanged run IDs.
  - Limit deep audit scope to failure/cancel/slow cohorts plus a capped freshness sample.
- **Estimated time savings**
  - **25–45 minutes** per analysis run.
- **Implementation risk**
  - **Low** if failure and outlier coverage is preserved.
- **Type**
  - **Non-critical-path but high compute win**.

### 5. Trim copilot reviewer prep/cleanup overhead
- **Evidence**
  - `copilot_pull_request_reviewer` avg **134s**, p95 **274s**.
  - Recent run `25272903357` took **137s**; artifact cleanup used `gh api /actions/runs/.../artifacts`.
  - Other recent summaries show `Cleanup artifacts` taking **~4 minutes** in some runs.
  - `Prepare` uses `github.paginate(github.rest.pulls.listFiles)`.
- **Root cause**
  - Full file enumeration and artifact lifecycle work even when PRs are small.
- **Exact change**
  - Add an early cutoff for file listing once policy thresholds are met.
  - Skip artifact cleanup when there is nothing to delete or when retention policy is acceptable for tiny artifacts.
- **Estimated time savings**
  - **1–4 minutes** on affected runs.
- **Implementation risk**
  - **Low**.
- **Type**
  - **Micro-optimization with occasional larger payoff**.

## Cost Optimizations

Ranked by expected token/dollar reduction.

### 1. Stop retrying implement with the same heavy prompt after deterministic no-op signatures
- **Evidence**
  - Analysis-context log summary for implement run `25272065644` recorded:
    - attempt 1: **20,488 tokens**
    - attempt 2: **10,907 tokens**
    - then bail after “2 consecutive attempts with no actionable output”.
  - The failed smoke-task run `25272034874` is the same failure class.
- **Root cause**
  - Retry path resends large static context and guidance even when the failure mode is already identified as “announced edit without changes” / empty-output.
- **Exact change**
  - After the first deterministic no-op signature, replace the full retry prompt with a compact delta prompt containing only:
    - failure diagnosis,
    - allowed write methods,
    - target file/path,
    - explicit “must modify filesystem” reminder.
  - Cap this failure mode at **1 heavy attempt + 1 slim retry**.
- **Estimated savings**
  - **~20k–30k tokens per bad implement run**.
- **Quality-risk notes**
  - **Low** if applied only to recognized no-op signatures.

### 2. Reduce reviewer-model fan-out on low-risk PRs
- **Evidence**
  - Run `25272986802` executed **6 reviewer models** in pass 1, then summarized them, before cancellation.
  - Docs-only PR `25272071242` already bypassed the heavy path.
- **Root cause**
  - Full panel review is being used even where deterministic or lightweight review would suffice.
- **Exact change**
  - Introduce reviewer tiers:
    - **Tier 0**: deterministic skip for docs-only/small metadata changes
    - **Tier 1**: 1–2 reviewers + summarizer for tiny code diffs
    - **Tier 2**: full 6-reviewer two-pass workflow for risky changes
- **Estimated savings**
  - **50–80% review-model tokens** on small-diff PRs.
- **Quality-risk notes**
  - **Medium**; keep full panel for workflow, infrastructure, or failing-check PRs.

### 3. Stabilize prompt prefixes and measure prompt-cache outcomes
- **Evidence**
  - Review run `25272986802` logged `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
  - The same run recorded:
    - `prompt_tokens=na`
    - `completion_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`
  - Workflow log analysis `25271970949` explicitly called prompt cache “enabled but effectively unobservable”.
- **Root cause**
  - Static instructions appear to be re-expanded with too much dynamic noise, while cache counters are not emitted in usable form.
- **Exact change**
  - Keep the first prompt segment stable across retries/pass 2:
    - move run IDs, timestamps, branch names, and large issue/PR bodies later;
    - inject the Serena efficiency block once per run, not per retry.
  - Emit usable cache-read/create counters for every OpenRouter call.
- **Estimated savings**
  - **5–15% token reduction** on prompt-heavy paths once stable prefixes are in place.
- **Quality-risk notes**
  - **Low**.

### 4. Stop paying to summarize trivial skipped runs in workflow-log analysis
- **Evidence**
  - `summarize_unselected_runs` in `25271970949` used **242,097 tokens** to summarize **82** runs.
  - Overall sample contains **612 skipped runs**, many lasting **0–2s** with simple gating outcomes.
- **Root cause**
  - Summarizer budget is being spent on low-information skipped runs.
- **Exact change**
  - Exclude:
    - 0–2s gated skips,
    - repeated identical skip patterns,
    - runs that already have deterministic machine-readable skip reasons.
- **Estimated savings**
  - **30–60%** of summarizer tokens in `workflow_log_analysis`.
- **Quality-risk notes**
  - **Low**, provided failures/cancellations/slow outliers remain covered.

### 5. Avoid Serena-heavy setup for plain-text-only implement tasks
- **Evidence**
  - Failed implement run `25272034874` set up Serena successfully, retrieved memory, and still failed on a one-file plain-text edit.
  - Analysis-context summaries describe “broad verification/read-first behavior”.
- **Root cause**
  - The expensive editing environment is being launched even when the task is fully specified and non-symbolic.
- **Exact change**
  - If the issue/task is:
    - single-file,
    - plain-text/non-code,
    - exact-content overwrite,
    bypass Serena setup and use a direct edit-mode prompt.
- **Estimated savings**
  - Small token savings per run plus **10–20s runtime savings**, but high leverage because it prevents reruns.
- **Quality-risk notes**
  - **Low** when restricted to deterministic plain-text tasks.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Repair the stable-release smoke path before further release promotion
- **Failure evidence**
  - `test_and_mark_stable` failure rate is **100% (5/5)**.
  - Failures:
    - `25271960656` at `Phase 3b: Wait for PR creation`
    - `25249170035`, `25252918179`, `25254380200`, `25265920645` at `Phase 4b: Verify editor removed bait line`
- **Root cause category**
  - Workflow integration contract mismatch / false-negative phase detection.
- **Exact fix**
  - Require implement success plus PR existence before proceeding.
  - If implement fails, surface its diagnosis immediately and end the smoke test early instead of continuing long downstream waits.
  - Backport the plain-text-file execution fix to the tested branch.
- **Expected reliability impact**
  - Could move the release gate from **0% to normal operating success**, eliminating the highest-visibility failure class.
- **Rollback / fail-open**
  - Safe to fail closed; do not allow release promotion on ambiguous results.

### 2. Add a deterministic plain-text-file execution path in implement
- **Failure evidence**
  - Implement run `25272034874` failed on a fully specified overwrite of `tests/e2e_smoke_canary.txt`.
  - Downstream E2E run `25271960656` then failed because no PR appeared.
- **Root cause category**
  - Tool-selection mismatch between Serena-oriented instructions and non-symbolic file edits.
- **Exact fix**
  - Explicitly whitelist `apply_patch` and `printf > file` for plain-text/data-file tasks.
  - Add a preflight branch that skips symbol-tool expectations for such tasks.
- **Expected reliability impact**
  - Large reduction in “narrate-only” or no-op implement failures.
- **Rollback / fail-open**
  - Fail open only to the existing full edit path if the task is not clearly plain-text-only.

### 3. Make prompt-contract tests less brittle to wording drift
- **Failure evidence**
  - CI failures include:
    - `25272902365`: `test_retry_prompt_includes_exec_history_recap` failed (36 passed, 1 failed)
    - `25267991186`: expected `Test script contract (MANDATORY):` fragment missing
    - `25266932433`, `25266996700`, `25267881013` also failed prompt-related unit steps
- **Root cause category**
  - Contract-test brittleness / duplicated prompt wording sources.
- **Exact fix**
  - Render test-asserted prompt fragments from a single source of truth.
  - Assert semantic markers or fragment IDs rather than fragile prose when possible.
- **Expected reliability impact**
  - Should reduce a large share of the **6/75 CI failures**.
- **Rollback / fail-open**
  - Keep a small golden-string subset for critical safety clauses.

### 4. Improve nightly validation-selftest diagnostics and quarantine failing fixtures
- **Failure evidence**
  - `nightly_validation_selftest` run `25268666198` failed with `fixtures=3 passed=1 failed=2`.
- **Root cause category**
  - Nightly fixture instability or regression; current logs do not expose which fixtures failed in the summary excerpt.
- **Exact fix**
  - Emit failing fixture names directly into the top-level summary and markdown output.
  - Quarantine or soft-fail known flaky fixtures until fixed.
- **Expected reliability impact**
  - Prevents a permanent-red nightly signal and speeds triage.
- **Rollback / fail-open**
  - Keep the artifact upload and status JSON even if fixture details cannot be generated.

### 5. Cancel stale review work earlier
- **Failure evidence**
  - `review_autofix` has **42 cancellations / 81 runs**.
  - Run `25272986802` was canceled after expensive pass-1 review completed.
- **Root cause category**
  - Stale-run cancellation happening too late in the workflow.
- **Exact fix**
  - Re-check branch head SHA before pass 2 and before any expensive summarization/editor stage.
- **Expected reliability impact**
  - Reduces stale artifacts, confusing partial reviews, and wasted compute on superseded commits.
- **Rollback / fail-open**
  - Fail open by completing pass 1 output if SHA check cannot run.

## AI Memory Health

- **Telemetry coverage**
  - Deep-dive logs contained **55 `AI_MEMORY_TELEMETRY` events** across implement, review, memory maintenance, orchestrate/poll, and workflow-log-analysis paths.

- **Retrieve performance**
  - Observed `retrieve` ops: **8**
  - Hit rate (`records_selected > 0`): **75%** (**6/8**)
  - Average `estimated_tokens`: **42**
  - `keyword_method` distribution:
    - `plain`: **6**
    - `none`: **2**
  - No observed `retrieve` entries with `fail_open: true`
  - No observed `retrieve` entries with `enabled: false`

- **Where retrieval is failing**
  - Both reviewer retrievals in `review_autofix` run `25272986802` returned:
    - `records_selected: 0`
    - `estimated_tokens: 0`
    - `keyword_method: "none"`
  - By contrast, implement run `25272034874` retrieved **2 records** with `estimated_tokens: 56` and `keyword_method: "plain"`.

- **Other memory operations**
  - `record-run-event`: **24** observed
  - `processed-command-check`: **6**
  - `processed-command-claim`: **6**
  - `compact`: **4**
  - `memory_maintenance` run `25273110199` compacted month `2026-04` with:
    - `archived_candidates: 2914`
    - `did_push: true`
    - `push_attempts: 1`

- **Push/retry health**
  - Maximum observed `push_attempts`: **2**
  - Most memory writes completed with **push_attempts: 1**
  - No evidence of systemic push retry instability

- **Recommendations**
  1. Improve reviewer retrieval keys by seeding from PR title, changed file paths, and failing check names so `keyword_method` does not fall back to `none`.
  2. Log retrieval budget explicitly (`budget_tokens`) to allow “estimated vs budget” tracking; that field was not present in observed retrieve entries.
  3. Keep emitting telemetry in deep-dive logs; this is one of the better-instrumented subsystems.

## GH API Call Audit

Observed patterns are concentrated in a few workflows rather than repo-wide saturation.

### 1. `test_and_mark_stable / e2e-smoke-test` is the biggest GH API hotspot
- **Evidence**
  - Runs `25271960656` and `25265920645` repeatedly call:
    - `repos/${REPO}/actions/runs/${RID}`
    - `repos/${TEST_REPO}/actions/workflows/${WF_FILE}/runs`
    - `repos/${TEST_REPO}/issues/${ISSUE_NUMBER}`
    - `repos/${TEST_REPO}/issues/${ISSUE_NUMBER}/comments`
    - `repos/${TEST_REPO}/pulls?...head=...`
  - The same workflow also launches soft-error analyzer passes that themselves poll run status again.
- **Redundancy pattern**
  - Repeated run-status polling and label/comment rechecks across clarify, plan, implement, and orphan-workflow phases.
- **Concrete fix**
  - Centralize phase watching into one cached poll helper per phase.
  - Reuse run IDs once discovered instead of re-listing workflow runs.
  - When a downstream dispatch returns or can infer a run ID, store it and poll only that ID.
- **Estimated call-count reduction**
  - **~30–50% fewer GH API calls** in stable smoke runs.
- **Rate-limit risk reduction**
  - High, because this is the one workflow already carrying rate-limit-aware wrappers.

### 2. `copilot_pull_request_reviewer / Prepare` is doing broad per-PR file enumeration
- **Evidence**
  - Recent run `25272903357` uses `github.paginate(github.rest.pulls.listFiles)`.
  - Recent run `25272112514` summary explicitly notes full PR file listing, including deleted files.
- **Redundancy pattern**
  - Full pagination even when only summary policy decisions are needed.
- **Concrete fix**
  - Stop pagination after policy thresholds are met.
  - Filter ignored/deleted-only cases earlier.
  - Reuse already-generated diff artifacts when available.
- **Estimated call-count reduction**
  - **50–90%** on large PRs; smaller on tiny PRs.
- **Rate-limit risk reduction**
  - Medium.

### 3. `cancel_on_pr_close` still pays for a no-op GH API scan
- **Evidence**
  - Run `25273362771` performs two paginated calls:
    - queued pull_request runs for branch
    - in-progress pull_request runs for branch
  - Workflow-log-analysis deep audit also flagged `/rate_limit` probing on this path in other recent samples.
- **Redundancy pattern**
  - No-op scans for branches that usually have nothing to cancel.
- **Concrete fix**
  - Only probe rate limit after an actual 403, not pre-emptively.
  - Preserve branch/event filtering, which is already a good optimization.
- **Estimated call-count reduction**
  - Small per run, meaningful over **15** runs in the sample.
- **Rate-limit risk reduction**
  - Low-medium.

### 4. `issue_pr_status` has a good GraphQL-first pattern, but fallback is still expensive
- **Evidence**
  - Run `25273362769` batches orchestrator issue detection via GraphQL, then falls back to per-issue REST if batch detection fails.
- **Redundancy pattern**
  - In fallback mode, each issue is fetched individually before label/close decisions.
- **Concrete fix**
  - Cache the fallback issue metadata within the step and reuse it for both classification and mutation decisions.
- **Estimated call-count reduction**
  - Moderate in fallback cases only.
- **Rate-limit risk reduction**
  - Medium on issue-heavy PRs.

### 5. Artifact cleanup is serial
- **Evidence**
  - `copilot_pull_request_reviewer` run `25272903357` lists artifact IDs via `gh api /actions/runs/25272903357/artifacts` then deletes each artifact individually.
- **Redundancy pattern**
  - One listing call plus N deletes; no batching available, but the cleanup itself may be avoidable in some paths.
- **Concrete fix**
  - Skip cleanup when there are zero artifacts, or when retention is acceptable for low-volume success paths.
- **Estimated call-count reduction**
  - Small to moderate depending on artifact count.
- **Rate-limit risk reduction**
  - Low.

**Repository hygiene note:** these recommendations align with the repo’s own `CLAUDE.md §15` API-hygiene guidance, which the `workflow_log_analysis` audit explicitly referenced in run `25271970949`.

## MCP & Serena Efficiency

- **Observed state**
  - Serena setup succeeds technically:
    - implement run `25272034874` warmed cache and validated the Serena MCP server successfully.
  - But the ROI is weak on some paths:
    - review run `25272986802` ended with **“No Serena tool usage stats found.”**
    - the same run still paid setup and prompt overhead.
  - Analysis-context summaries for implement no-op runs mention **broad verification/read-first behavior** before a trivial edit.

### Findings

1. **Serena is being initialized for tasks that do not benefit from symbol tools**
   - **Evidence:** `25272034874` was a one-file exact overwrite task.
   - **Recommendation:** skip Serena setup entirely for deterministic plain-text overwrite tasks.
   - **Expected impact:** lower startup time and fewer narration/no-op failures.

2. **Review path lacks proof of actual Serena use**
   - **Evidence:** `25272986802` logged no Serena tool usage stats even though Serena setup ran.
   - **Recommendation:** require one of:
     - tool-usage stats artifact,
     - explicit tool-use counters,
     - or a “Serena bypassed” marker.
   - **Expected impact:** better token/latency accounting and fewer unnecessary setup costs.

3. **Prompt guidance is heavier than observed tool behavior**
   - **Evidence:** implement logs contain a large Serena efficiency block; reviewer logs show cache probes and multi-model review, but no Serena usage stats.
   - **Recommendation:** inject Serena guidance conditionally based on task type and expected file modality.
   - **Expected impact:** token savings with minimal correctness risk.

4. **Copilot/review prep can parallelize more safely**
   - **Evidence:** review runs fetch PR metadata, check-run context, memory, and diff data in separate prep steps before the first model call.
   - **Recommendation:** parallelize read-only prep operations:
     - PR metadata
     - changed files
     - check-run failures
     - memory retrieval
   - **Expected impact:** **10–30s** prep reduction on review flows.

5. **Broader reads still appear where targeted diff/symbol context would suffice**
   - **Evidence:** copilot reviewer uses full `listFiles` pagination; implement failure summaries show read-first verification behavior.
   - **Recommendation:** prefer existing symbol-diff summaries and scoped file targets over broad repo reads whenever the changed file set is already known.

## Prompt Cache & Memory System

### Prompt cache

- **What is working**
  - The system is configured with prompt cache enabled in major AI paths (`OPENROUTER_PROMPT_CACHE_DISABLED: false` observed in implement, review, poll).

- **What is not measurable**
  - In review run `25272986802`, OpenRouter cache probe lines reported:
    - `prompt_tokens=na`
    - `completion_tokens=na`
    - `total_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`
  - So cache hit/miss effectiveness is not observable enough to optimize confidently.

- **Likely cache-fragmentation causes**
  - Repeated large instruction blocks with dynamic run-specific context.
  - Retry prompts and two-pass review likely re-emit near-identical static guidance with different noise around it.
  - Serena efficiency guidance appears embedded in prompt bodies for workflows where tool usage may be absent.

- **Concrete improvements**
  1. Keep a stable cacheable prefix:
     - system instructions
     - repo instructions
     - static safety rules
  2. Append dynamic context later:
     - PR number
     - issue body
     - timestamps
     - run-specific diagnostics
  3. Inject Serena guidance once per run rather than on every retry/pass.
  4. Emit real cache-read/create token counters per model call.

- **Estimated impact**
  - **Tokens:** 5–15% reduction on heavy prompt paths
  - **Latency:** modest reduction from better cache hits
  - **Reliability:** improved observability, fewer blind optimizations

### Memory retrieval effectiveness

- **Observed behavior**
  - Implementation retrieval works: `25272034874` selected **2 records**.
  - Reviewer retrieval is ineffective: `25272986802` selected **0 records**.
- **Concrete improvements**
  - Seed review memory retrieval with:
    - failing check names,
    - changed workflow/script paths,
    - PR title keywords.
  - Skip retrieval entirely when the selector would produce `keyword_method: "none"` unless there is a fallback heuristic.
- **Estimated impact**
  - **Tokens:** small savings
  - **Latency:** small savings
  - **Reliability/quality:** moderate gain on review relevance

## Orchestrator Health

- **Current window is sufficient** (`insufficient_data: false`), and the orchestrator is mostly healthy in the narrow sense that the major control workflows are running and gating correctly.

### What looks healthy
- `orchestrate_poll`: **47/47 success**, avg **44.8s**
- `cancel_on_pr_close`: **15/15 success**
- `issue_pr_status`: **15/15 success**
- Many `clarify`, `plan`, and `orchestrate_clarify_respond` runs are intentional skips based on comment gating, not errors.

### Operational pain points

1. **Skip churn is very high**
   - Overall sample: **612 skipped runs / 1000 total**
   - `clarify`: **169 non-success non-failure**
   - `plan`: **143**
   - `orchestrate_clarify_respond`: **159**
   - This appears design-driven, but it creates noise and collector overhead.

2. **Implement failure cascades into orchestrator churn**
   - Failed implement `25272034874` caused downstream follow-on comments/skip runs and then broke the stable smoke gate.

3. **Review workflow cancellation churn is high**
   - `review_autofix` cancellations: **42/81**
   - This is likely from branch updates or superseding runs, but the cancellation point is too late.

4. **Hosted-runner wait is a recurring theme**
   - Queue wait messages appear in CI, review, poll, promote, issue sync, and forward-merge runs.
   - This is not a logic bug, but it is a recurring source of wall-clock inflation.

### Smallest safe mitigations
- Add branch-scoped stale-run checks earlier in review and implement-heavy workflows.
- Collapse or debounce obvious comment-trigger skip storms where possible.
- Track and expose runner-wait share separately from execution time.

### Observable indicators to track
- `review_autofix` canceled-rate
- implement “empty/no-actionable-output” count
- skipped-run ratio by family
- runner-wait time / total runtime
- stable-smoke pass rate
- reviewer memory retrieve hit rate

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

### 1. Implement → PR creation is the main blocking bottleneck
- **Phase:** implement
- **Type:** compute + failure cascade
- **Evidence:** `25272034874` failed; `25271960656` then spent **3,676s** and still failed waiting for PR creation.
- **Fix:** deterministic plain-text path + earlier hard stop on implement failure.

### 2. Stable smoke gate spends too long discovering known-bad states
- **Phase:** validate/orchestrate release
- **Type:** retry/poll overhead
- **Evidence:** stable smoke runs continue through long phase polling before concluding failure.
- **Fix:** fail earlier once implement/editor preconditions are irrecoverably false.

### 3. CI `lint` is the dominant PR-path compute bottleneck
- **Phase:** validate
- **Type:** compute
- **Evidence:** consistent **~10-minute** `lint` jobs across recent successful runs.
- **Fix:** split fast/slow checks and parallelize.

### 4. Review/autofix spends too much time on work that may be canceled
- **Phase:** review/autofix
- **Type:** compute + stale-run overhead
- **Evidence:** canceled run `25272986802` completed expensive pass 1 before cancellation.
- **Fix:** stale-run checks before pass 2 and smaller reviewer tiers.

### 5. Workflow-log analysis is a heavy background drain
- **Phase:** post-run analysis
- **Type:** compute + token overhead
- **Evidence:** **3,290s–5,875s** analysis runs, with **242,097 tokens** observed in one summarizer pass.
- **Fix:** reduce scope for trivial skips and reuse prior summaries.

### 6. Queueing inflates wall-clock across many workflows
- **Phase:** cross-cutting
- **Type:** queueing
- **Evidence:** repeated “waiting for a hosted runner” in CI, review, poll, issue sync, promote, and forward-merge.
- **Fix:** measure queue share separately and prioritize shortening compute in required jobs.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `test_and_mark_stable` release smoke failures and long polls
  - `ci/lint` ~10-minute runtime
  - `review_autofix` long multi-model reviews with high cancellation rate
  - `workflow_log_analysis` very long background runtime

- **Top failure modes**
  - Implement no-op / no-PR-created on trivial plain-text tasks (`25272034874`)
  - Stable smoke gate false negatives or delayed failure detection (`25271960656`, `25265920645`)
  - Prompt contract drift causing CI failures (`25272902365`, `25267991186`)
  - Nightly validation self-test fixture failures (`25268666198`)

- **Highest-cost drivers**
  - Repeated heavy implement retries (`31,395` observed tokens in one no-op failure case)
  - Full-panel review/autofix runs with two-pass logic
  - Workflow-log-analysis summarization (`242,097` observed tokens in one run)
  - Prompt-cache-enabled but unobservable AI calls

- **Top 3 prioritized actions**
  1. **Backport/fix the plain-text implement path and rerun stable-smoke only after that lands.**
  2. **Split CI into fast required checks and slower parallel suites.**
  3. **Gate review/autofix two-pass + full reviewer fan-out by diff size and stale-run status.**

## Metrics Appendix

### Run summary

| Scope | Total runs | Success | Failure | Cancelled | Skipped/Other | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 327 | 13 | 48 | 612 | 151.2 | 1.0 | 620.0 |

### Workflow-family metrics

| Workflow family | Total | Success | Failure | Cancelled | Other | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 75 | 69 | 6 | 0 | 0 | 591.5 | 606.0 | 643.0 |
| review_autofix | 81 | 39 | 0 | 42 | 0 | 434.0 | 47.0 | 2062.0 |
| test_and_mark_stable | 5 | 0 | 5 | 0 | 0 | 5259.0 | 5858.0 | 6213.8 |
| implement | 165 | 23 | 1 | 6 | 135 | 33.4 | 1.0 | 226.4 |
| plan | 165 | 22 | 0 | 0 | 143 | 12.4 | 1.0 | 139.6 |
| clarify | 194 | 25 | 0 | 0 | 169 | 18.5 | 1.0 | 134.0 |
| orchestrate_clarify_respond | 165 | 6 | 0 | 0 | 159 | 1.2 | 1.0 | 2.0 |
| orchestrate_poll | 47 | 47 | 0 | 0 | 0 | 44.8 | 45.0 | 49.0 |
| copilot_pull_request_reviewer | 25 | 25 | 0 | 0 | 0 | 134.1 | 131.0 | 274.4 |
| workflow_log_analysis | 5 | 5 | 0 | 0 | 0 | 4862.0 | 5476.0 | 5828.2 |
| validation_refresh | 7 | 7 | 0 | 0 | 0 | 211.3 | 218.0 | 220.0 |
| memory_maintenance | 6 | 6 | 0 | 0 | 0 | 30.2 | 30.5 | 32.0 |
| cancel_on_pr_close | 15 | 15 | 0 | 0 | 0 | 7.5 | 6.0 | 13.2 |
| issue_pr_status | 15 | 15 | 0 | 0 | 0 | 29.5 | 15.0 | 62.3 |
| promote_main_to_stable | 6 | 6 | 0 | 0 | 0 | 19.3 | 19.0 | 21.5 |
| forward_merge_stable_to_main | 7 | 7 | 0 | 0 | 0 | 18.1 | 18.0 | 21.0 |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 0 | 90.0 | 90.0 | 90.0 |

### Highest-impact failing runs

| Run ID | Workflow family | Duration (s) | Failure point |
|---|---|---:|---|
| 25249170035 | test_and_mark_stable | 6255 | e2e-smoke-test / Phase 4b: Verify editor removed bait line |
| 25254380200 | test_and_mark_stable | 6049 | e2e-smoke-test / Phase 4b: Verify editor removed bait line |
| 25265920645 | test_and_mark_stable | 5858 | e2e-smoke-test / Phase 4b: Verify editor removed bait line |
| 25252918179 | test_and_mark_stable | 4457 | e2e-smoke-test / Phase 4b: Verify editor removed bait line |
| 25271960656 | test_and_mark_stable | 3676 | e2e-smoke-test / Phase 3b: Wait for PR creation (implement phase) |
| 25272034874 | implement | 131 | implement / Run Codex implementation |
| 25268666198 | nightly_validation_selftest | 90 | validation-selftest / Run validation self-test matrix |

### Observed token metrics from deep-dive logs

| Run ID | Workflow | Metric | Value |
|---|---|---|---:|
| 25272065644 | implement | Attempt 1 tokens | 20488 |
| 25272065644 | implement | Attempt 2 tokens | 10907 |
| 25272065644 | implement | Observed total before bail | 31395 |
| 25271970949 | workflow_log_analysis | `summarize_unselected_runs` tokens_used | 242097 |
| 25272986802 | review_autofix | Cache probe prompt/completion/total tokens | `na` |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total telemetry events observed | 55 |
| `retrieve` ops | 8 |
| Retrieve hit rate | 75% |
| Avg `estimated_tokens` on retrieve | 42 |
| `keyword_method=plain` | 6 |
| `keyword_method=none` | 2 |
| Retrieve events with `records_selected=0` | 2 |
| Retrieve events with `fail_open: true` | 0 |
| Retrieve events with `enabled: false` | 0 |
| Max observed `push_attempts` | 2 |

### Observed cache metrics

| Area | Observation |
|---|---|
| OpenRouter prompt cache | Enabled in env (`OPENROUTER_PROMPT_CACHE_DISABLED: false`) but read/create counters were `na` in review run `25272986802` |
| Codex CLI cache | Hit observed in workflow-log-analysis `25271970949` (`codex-v0.114.0`) |
| uv cache | Hits/saves observed in plan/review runs; e.g. hit in plan `25272030427`, save in review `25272107225` |

### GH API hotspot summary

| Workflow / run | Hotspot |
|---|---|
| `test_and_mark_stable` / `25271960656`, `25265920645` | repeated `actions/runs/{id}` polls, workflow-run listing, issue/comment/PR rechecks |
| `copilot_pull_request_reviewer` / `25272903357`, `25272112514` | full `pulls.listFiles` pagination; artifact list/delete loop |
| `cancel_on_pr_close` / `25273362771` | queued + in-progress workflow-run scans per branch |
| `issue_pr_status` / `25273362769` | GraphQL batch with per-issue REST fallback |
| `review_autofix` / `25273362778` | GraphQL linked-issue lookup plus workflow dispatch attempts |

## Deep Audit — Workflows & Scripts (2026-05-03)

### Section 1: Bug & Correctness Sweep

- **ID** — **BUG-001**  
  **File path** — `.github/workflows/test-and-mark-stable.yml:2722-2769,2792-2832,2850-2897,2915-2950,3021-3058`  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — Every “dispatch & watch” block identifies the spawned run by taking the latest run with `.id > PRE` after a `gh workflow run`. That is not a stable correlation to *this* dispatch; it will bind to any newer run of the same workflow that appears first in the indexing window. The pattern is repeated for `workflow-log-analysis`, `validation-refresh`, `update_workflows`, `internal-memory-maintenance`, and `internal-orchestrate`. In a busy repo, a scheduled/manual/concurrent smoke-triggered run can satisfy the same predicate and make the gate watch the wrong run, producing false pass/fail outcomes.  
  **Recommended fix** — Extract a shared watcher helper that records `dispatch_started_at` and matches on a unique dispatch marker, not “latest id after PRE”. Preferred signature: `dispatch_and_wait_workflow_run <repo> <workflow_file> <timeout_secs> <poll_secs> <fields_json> <match_mode>`. For workflows that lack a unique input today, add a no-op `smoke_nonce` / `caller_run_id` input and filter by that marker plus `event=workflow_dispatch`, `created_at >= dispatch_started_at`, and actor. Reuse the helper across all five blocks.

- **ID** — **BUG-002**  
  **File path** — `.github/workflows/test-and-mark-stable.yml:924-933,954-1033`  
  **Severity** — Medium  
  **Category tag** — `bug`  
  **Description** — The PR-head “stability” guard never fails closed. The loop samples `HEAD_A` and `HEAD_B` five times, but if the SHA is still moving on attempt 5, execution just falls through into bait injection anyway. That defeats the protection the comment describes: the subsequent `gh api -X PUT repos/.../contents/...` still runs against a moving PR head, so the step can skip or misattribute failures that are really branch-motion races.  
  **Recommended fix** — Track an explicit `head_stable=false/true` flag and abort with a dedicated status (for example `head_still_moving`) if the SHA never stabilizes. Reuse the stable SHA for the later PR metadata check instead of re-fetching the PR independently.

- **ID** — **BUG-003**  
  **File path** — `.github/workflows/review_autofix.yml:478-530`  
  **Severity** — High  
  **Category tag** — `bug`  
  **Description** — The post-merge validate-dispatch fallback reintroduces the broad issue-link regex that the same workflow’s deterministic-skip path explicitly avoids. At line 488 it matches plain `issues/123` and `issue #123` mentions, not just closing references. If `closingIssuesReferences` is empty, a merged PR body/title that merely documents another issue can still populate `issue_numbers`; the loop at lines 503-527 then calls `gh issue view`, dispatches validation, and removes `ai:orchestrator-validate-required` from those unrelated issues.  
  **Recommended fix** — Make this path GraphQL-only like the deterministic skip path, or narrow fallback parsing to true closing references only (the stricter pattern already used in `.github/workflows/issue_pr_status.yml:196-210`). If fallback must remain, batch-fetch labels for all candidate issues before any mutation and refuse to mutate on ambiguous prose-only matches.

### Section 2: GitHub API Call Redundancy Audit

- **ID** — **API-001**  
  **File path** — `.github/workflows/cancel_on_pr_close.yml:68-100`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The workflow performs two separate paginated `GET /repos/{repo}/actions/runs` scans for the same branch/event scope: one with `status=queued` and one with `status=in_progress`, then merges them locally. Current retrieval call count per PR-close event: **2 list-runs calls** before any cancel POSTs.  
  **Recommended fix** — Collapse this to a single list-runs request for `event=pull_request`, `branch=${PR_HEAD_REF}`, `per_page=100`, then filter `queued|in_progress` client-side. Proposed retrieval call count: **1 list-runs call** before cancel POSTs. Extend the single-call filtering pattern already used in `scripts/gh_helpers.sh:1183-1229` (`autofix_retrigger_has_inflight_peer`).

- **ID** — **API-002**  
  **File path** — `.github/workflows/review_autofix.yml:478-530`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — In the fallback branch, the step first fetches PR text once (`repos/{repo}/pulls/{PR_NUMBER}`) and then performs a `gh issue view ... --json labels` call inside the per-issue loop for every inferred issue whose labels were not already present. Current retrieval call count in the fallback path: **1 PR fetch + N issue fetches**. This is both an N+1 pattern and a tight per-iteration API call inside a loop.  
  **Recommended fix** — Batch the candidate issue-label lookup before the loop. Proposed retrieval call count: **1 batched GraphQL query** (or **2** if you retry only missing aliases) instead of `1+N`. Reuse the alias-batching pattern from `.github/workflows/issue_pr_status.yml:291-320` or the richer batched helper in `scripts/orchestrate_poll_process.sh:5850-5969` (`_fetch_candidate_issue_details_graphql`). Keep the per-issue label-removal mutations, but eliminate the per-issue reads.

- **ID** — **API-003**  
  **File path** — `.github/workflows/issue_pr_status.yml:295-330`  
  **Severity** — Medium  
  **Category tag** — `api-batching`  
  **Description** — The workflow already does one aliased GraphQL batch to classify linked issues, but if that batch fails it falls back to a per-issue REST fetch inside the loop (`repos/{REPOSITORY}/issues/{_orch_num}`). Current retrieval call count in the degraded path: **1 GraphQL batch + up to N REST issue fetches**. That violates the repo’s own “cycle-local caches / batched GraphQL over per-item REST” rule for the error path.  
  **Recommended fix** — Retry missing/failed issue metadata in another batched GraphQL query rather than dropping to REST-per-issue. Proposed retrieval call count: **2 batched GraphQL calls max** (initial batch + missing-alias retry), falling back to REST only for the specific unresolved IDs if both batches fail. Extend the batching contract from `scripts/orchestrate_poll_process.sh:5850-5969`.

### Section 3: Code Duplication & Modularization Opportunities

- **ID** — **DUP-001**  
  **File path** — `.github/workflows/test-and-mark-stable.yml:2722-2769,2792-2832,2850-2897,2915-2950,3021-3058`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — The workflow contains at least five near-identical “dispatch workflow, discover new run id, poll until completion” shell blocks. They differ mostly by workflow filename, timeout, and success conclusion handling. This duplication already drifted: some blocks accept `skipped`, some do not; some pass distinguishing inputs, some do not.  
  **Recommended fix** — Move the pattern into a shared helper, preferably `scripts/gh_helpers.sh` or a new `scripts/watch_workflow_dispatch.sh`. Suggested signature: `dispatch_and_wait_workflow_run <repo> <workflow_file> <timeout_secs> <poll_secs> <accept_csv> <fields_json> [match_marker]`. Update all `dispatch_*` callers in `test-and-mark-stable.yml` to use the helper, and reuse it in `comprehensive-test-and-release.yml` where the same watch logic exists.

- **ID** — **DUP-002**  
  **File path** — `scripts/gh_helpers.sh:76-121,391-557; scripts/label_helpers.sh:110-144; .github/workflows/cancel_on_pr_close.yml:27-53; .github/workflows/mark-stable.yml:308-347,457-496; .github/workflows/orchestrate_poll.yml:66-95; .github/workflows/review_autofix.yml:566-580,1293-1341; .github/workflows/implement.yml:3098-3110; scripts/implement_diagnose_post_codex_failure.sh:42-49`  
  **Severity** — Medium  
  **Category tag** — `duplication`  
  **Description** — Core GitHub helper behavior is duplicated in many places: custom `_rl_wait/_gh_retry` wrappers, inline `gh_retry() { "$@"; }` shims, duplicated `_safe_gh_jq`, and inline `ensure_label_exists`. The repo already has canonical implementations in `scripts/gh_helpers.sh` and `scripts/label_helpers.sh`, so these copies create drift risk in retry semantics, rate-limit handling, permanent-failure detection, and label metadata.  
  **Recommended fix** — Centralize on the existing helper modules. If some jobs intentionally avoid the full support checkout, add a tiny bootstrap helper module (for example `scripts/gh_helpers_min.sh`) with the exact subset needed: `gh_retry`, `_safe_gh_jq`, and `ensure_label_exists`. Update the listed callers to source that module instead of reimplementing local variants.

### Section 4: Expression Size Limit Risk Assessment

No new expression-length findings met the reporting threshold.

- Largest interpolated `run:` blocks observed in this audit were far below the 15,000-character medium-risk floor:  
  - `.github/workflows/test-and-mark-stable.yml:3801-3871` ≈ **2,999 chars**  
  - `.github/workflows/plan.yml:1278-1321` ≈ **2,962 chars**  
  - `.github/workflows/mark-stable.yml:301-366` ≈ **2,743 chars**
- Largest `if:` expression observed was in `.github/workflows/review_autofix.yml:3746` at ≈ **444 chars**.
- No workflow file exceeded the 800 KB early-warning threshold. The largest audited workflow was `.github/workflows/review_autofix.yml` at **271,416 chars**, leaving **777,160 chars** of headroom under GitHub’s 1 MB workflow file limit.

### Section 5: Cross-Cutting Concerns

- **ID** — **DEAD-001**  
  **File path** — `scripts/orchestrate_poll_process.sh:4770-4776`  
  **Severity** — Low  
  **Category tag** — `dead-code`  
  **Description** — `read_standalone_state_json()` is defined, but repository-wide symbol/text search found no callers; only `write_standalone_state_json()` is used from the standalone stall-recovery flow. That leaves an untested dead helper in the hottest script in the repo.  
  **Recommended fix** — Remove `read_standalone_state_json()` if it is obsolete, or switch an existing caller to it so the read-path is exercised. If kept, document why it remains intentionally unused.

- **ID** — **DEBT-001**  
  **File path** — `.github/workflows/memory_maintenance.yml:37-55`  
  **Severity** — Low  
  **Category tag** — `tech-debt`  
  **Description** — The reusable memory-maintenance workflow still threads `BATCH_API_DISABLED`, `BATCH_API_PROVIDER`, and `BATCH_API_POLL_TIMEOUT_HOURS` through its env and emits them in a `batch_noop` log line, while the same block explicitly states there is “no_llm_path” and “no_codex_execution_path”. This preserves a dead configuration surface that operators can still set even though it changes nothing.  
  **Recommended fix** — Remove the deprecated variables from the reusable workflow interface and keep any compatibility telemetry in a dedicated wrapper or migration shim. At minimum, add a sunset date and stop exporting the vars into runtime env once downstream scrapers are updated.

No TODO/FIXME/HACK markers were found in the audited `.github/workflows/*.yml`, `scripts/*.sh`, or `scripts/*.py` files.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, BUG-003 |
| Medium | 6 | BUG-002, API-001, API-002, API-003, DUP-001, DUP-002 |
| Low | 2 | DEAD-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 2 | Medium |
| API call optimization | 3 | Medium |
| Code modularization | 6 | Large |
| Expression size reduction | 0 | Small |
| Medium/Low fixes | 2 | Small |

## API Call Consolidation & Dead-Call Analysis (2026-05-03)

### Safety Tag Legend
`SAFE_TO_MERGE` means the overlap is fully proven safe to collapse without changing filters, retries, failure behavior, or concurrency semantics. `NEEDS_VERIFICATION` means the overlap is real but static reading does not fully prove equivalence, so a human or follow-on analysis must confirm behavior first. `RISKY_SKIP` means the call sits in a retry/poll/race-defense-sensitive path where automatic consolidation is not authorized even if overlap exists.

### Consolidation Candidates (MERGE-###)

- **ID** — **MERGE-001**  
  **Safety tag** — `RISKY_SKIP`  
  **File path and line ranges** — `scripts/orchestrate_poll_process.sh:3412-3418`, `scripts/orchestrate_poll_process.sh:3468-3473`, `scripts/orchestrate_poll_process.sh:3521-3524`  
  **Current call count** — 8 PR fetches  
  **Proposed call count** — 3 PR fetches  
  **Endpoint(s)** — `GET /repos/{repo}/pulls/{pull_number}`  
  **Evidence** — The final-merge path fetches the same PR three times in three tight clusters, extracting different fields from separate calls instead of one cached payload per cluster.
  ```bash
  existing_pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  existing_pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"

  pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' || echo "")"
  pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"

  pr_state="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.state' || echo "")"
  pr_mergeable="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.mergeable' || echo "")"
  pr_merged="$(gh_retry _safe_gh_jq "repos/${GITHUB_REPOSITORY}/pulls/${final_pr}" --jq '.merged_at != null' || echo "")"
  ```
  The script already has `_fetch_pr_json()` available earlier in the file, so the consolidation target is local and explicit.  
  **Proposed fix** — In `mark_validation_complete`, fetch the PR once per decision cluster via `_fetch_pr_json "${final_pr}"`, then derive `.state`, `.mergeable`, and `.merged_at` from the cached JSON with local `jq`; do not change the three-cluster structure across the pre-merge, pre-checks, and post-merge phases.  
  **Safety rationale** — This is inside `scripts/orchestrate_poll_process.sh` and specifically in a mergeability/race-sensitive poll path, which the audit contract marks as `RISKY_SKIP` even when overlap is obvious.  
  **Downstream signal** — Do not auto-implement: manually prove that one cached PR read per cluster preserves mergeability recomputation behavior, retry semantics, and existing `[final-merge]` log diagnostics before changing this path.

- **ID** — **MERGE-002**  
  **Safety tag** — `RISKY_SKIP`  
  **File path and line ranges** — `.github/workflows/test-and-mark-stable.yml:924-927`, `.github/workflows/test-and-mark-stable.yml:954-960`  
  **Current call count** — 3 PR fetches on the successful stability path  
  **Proposed call count** — 2 PR fetches  
  **Endpoint(s)** — `GET /repos/{repo}/pulls/{pull_number}`  
  **Evidence** — The bait-injection race guard reads the PR twice for head SHA stability, then immediately performs a third full PR fetch for closed/merged checks.
  ```bash
  HEAD_A=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")
  sleep 3
  HEAD_B=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // ""' 2>/dev/null || echo "")

  PR_META=$(gh api "repos/${TEST_REPO}/pulls/${PR_NUMBER}" 2>/dev/null || echo "")
  PR_STATE=$(printf '%s' "${PR_META}" | jq -r '.state // ""' 2>/dev/null || echo "")
  PR_MERGED=$(printf '%s' "${PR_META}" | jq -r '.merged // false' 2>/dev/null || echo "false")
  ```
  The second stability read could carry full PR JSON, with `HEAD_B` extracted locally and `PR_META` reused if the head is stable.  
  **Proposed fix** — Replace the second `--jq '.head.sha'` read with a full `gh api "repos/.../pulls/${PR_NUMBER}"`, derive `HEAD_B` locally, and reuse that JSON as `PR_META` after the stability loop succeeds.  
  **Safety rationale** — This code explicitly defends against upstream indexing races, so even a same-endpoint consolidation must be treated as `RISKY_SKIP` rather than auto-safe.  
  **Downstream signal** — Do not auto-implement: manually verify that reusing the second stability read preserves the two-sample race check, failure messaging, and timing behavior under delayed PR indexing.

### Redundant Re-Fetch (REUSE-###)

- **ID** — **REUSE-001**  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/review_autofix.yml:201-210`, `.github/workflows/review_autofix.yml:1371-1385`  
  **Current call count** — 2 overlapping PR fetches  
  **Proposed call count** — 1 overlapping PR fetch  
  **Endpoint(s)** — `GET /repos/{repo}/pulls/{pull_number}`  
  **Evidence** — The gate step fetches PR state/head/labels/diff totals from `/pulls/{PR_NUMBER}`, then the same job later fetches the full PR payload again before reviewer prep.
  ```bash
  if _pr_gate="$(gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq '[.state, (.merged // false), (.head.ref // ""), ((.labels // []) | map(.name) | join(",")), (.additions // 0), (.deletions // 0)] | @tsv')"; then
    IFS=$'\t' read -r pr_state pr_merged pr_head_ref pr_labels pr_additions pr_deletions <<< "${_pr_gate}" || true
  fi
  ```
  ```bash
  gh_retry "${PR_PAYLOAD_FILE}" api repos/${{ github.repository }}/pulls/"${PR_NUMBER}"

  jq '{
    title: (.title // ""),
    body: (.body // ""),
    baseRefName: (.base.ref // ""),
    headRefName: (.head.ref // ""),
    headRepoFullName: (.head.repo.full_name // "")
  }' "${PR_PAYLOAD_FILE}" > "${PR_META_FILE}"
  ```
  The second call is not adding a new object identity; it is re-fetching the same PR resource already queried at gate time.  
  **Proposed fix** — Extend the gate step to persist the full PR JSON to `PR_PAYLOAD_FILE` (or a temp file promoted into `PR_PAYLOAD_FILE`), derive the gate TSV from local `jq`, and make `Collect PR metadata and changed files` skip the second `/pulls/{PR_NUMBER}` fetch when the cached payload validates.  
  **Safety rationale** — The overlap is clear, but the calls live in different workflow steps and currently have different retry/error-handling semantics (`gh api` vs `gh_retry`), so this cannot be tagged `SAFE_TO_MERGE` from static reading alone.  
  **Downstream signal** — Verify in a real review run that the cached gate-time PR payload remains valid for the later prep step, and confirm behavior is acceptable when the PR is updated between those two steps before removing the second GET.

- **ID** — **REUSE-002**  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/implement.yml:53-76`, `.github/workflows/implement.yml:543-575`  
  **Current call count** — 2 overlapping issue fetches  
  **Proposed call count** — 1 overlapping issue fetch  
  **Endpoint(s)** — `GET /repos/{repo}/issues/{issue_number}`  
  **Evidence** — The job fetches the issue once for precheck state/labels, then fetches the full issue again later for metadata and prompt material.
  ```bash
  ISSUE_PAYLOAD="$(gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" --jq '{state: (.state // "open"), labels: [.labels[].name]}')"
  ISSUE_STATE="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -r '.state')"
  ISSUE_LABELS_JSON="$(printf '%s' "${ISSUE_PAYLOAD}" | jq -c '.labels')"
  ```
  ```bash
  gh_retry gh api "repos/${{ github.repository }}/issues/${ISSUE_NUMBER}" > "${ISSUE_META_FILE}"

  ISSUE_BODY="$(jq -r '.body // ""' "${ISSUE_META_FILE}")"
  ISSUE_TITLE="$(jq -r '.title // ""' "${ISSUE_META_FILE}")"
  ISSUE_NUMBER_JSON="$(jq -r '.number' "${ISSUE_META_FILE}")"
  ISSUE_URL_JSON="$(jq -r '.html_url' "${ISSUE_META_FILE}")"
  ```
  The workflow already reuses `ISSUE_META_FILE` later in label-restore paths, so there is an existing cache contract once the full payload exists.  
  **Proposed fix** — Have `Precheck approval phase label` fetch and preserve the full issue JSON in a temp/cache file, derive the state/labels locally, and let `Fetch issue metadata` consume that cached payload with the current API call kept only as a missing/invalid-cache fallback.  
  **Safety rationale** — The calls are in different steps separated by setup/install work, so a human must decide whether reusing a slightly older issue snapshot is acceptable for implement prompt construction and failure recovery.  
  **Downstream signal** — Verify whether issue title/body edits between precheck and metadata fetch must be observed by the same run; if freshness is required, keep the second GET or add an explicit freshness check before removing it.

- **ID** — **REUSE-003**  
  **Safety tag** — `NEEDS_VERIFICATION`  
  **File path and line ranges** — `.github/workflows/issue_pr_status.yml:295-349`, `.github/workflows/issue_pr_status.yml:501-512`  
  **Current call count** — 1 GraphQL classification batch + up to N extra REST issue fetches for alert suppression  
  **Proposed call count** — 1 GraphQL classification batch + 0 extra REST issue fetches  
  **Endpoint(s)** — GraphQL `repository { issue(number) { number labels body } }`; `GET /repos/{repo}/issues/{issue_number}`  
  **Evidence** — The workflow already batches `labels` and `body` for linked issues while classifying tracking/managed issues, then the later Telegram step re-fetches each issue body to answer the narrower question “is any linked issue orchestrator-managed?”
  ```bash
  ORCH_ALIAS_FRAGMENT+=" i${ORCH_IDX}: issue(number: ${_orch_num}) { number labels(first: 50) { nodes { name } } body }"
  ...
  ORCH_RESP="$(gh_retry gh api graphql -f query="${ORCH_QUERY}" 2>/dev/null || echo '')"
  ```
  ```bash
  while IFS= read -r issue_number; do
    [ -n "${issue_number}" ] || continue
    BODY="$(_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}" --jq '.body // ""' || echo "")"
    if printf '%s' "${BODY}" | grep -qF 'Managed by: AI Orchestrator'; then
      IS_ORCHESTRATED="true"
      break
    fi
  done <<< "${LINKED_ISSUE_NUMBERS}"
  ```
  The second step is strictly consuming a subset of data already materialized in the earlier batch path.  
  **Proposed fix** — In the classification step, export `HAS_ORCHESTRATED_LINKED_ISSUE=true|false` (or serialize `MANAGED_ISSUES`/`TRACKING_ISSUES` into `$GITHUB_ENV`), then make the Telegram alert step read that cached decision instead of refetching issue bodies.  
  **Safety rationale** — The overlap is real, but reuse crosses workflow steps and depends on the earlier step’s fail-open/fallback classification semantics, so verification is required before changing alert gating.  
  **Downstream signal** — Verify that the update/classification step always runs before the merged-alert step on the paths you care about, and compare alert-suppression behavior on GraphQL batch failure before removing the per-issue REST reads.

### Dead Calls (DEAD-API-###)

No findings.

### Cross-References to Deep Audit Section

- API-001: `RISKY_SKIP` — Correct overlap, but both calls are paginated list-runs scans in a cancellation path, so page-boundary and failure-behavior changes require manual review.
- API-002: `NEEDS_VERIFICATION` — The N+1 issue-label reads are real, but this is a fallback branch whose degraded-path semantics should be validated before batching changes land.
- API-003: `NEEDS_VERIFICATION` — Agreed that per-issue REST fallback is expensive, but replacing degraded-path classification logic with batched retry needs explicit fail-open verification.

### Summary Counts

| Tag | Count | IDs |
|---|---:|---|
| SAFE_TO_MERGE | 0 | — |
| NEEDS_VERIFICATION | 3 | REUSE-001, REUSE-002, REUSE-003 |
| RISKY_SKIP | 2 | MERGE-001, MERGE-002 |

### Implement-Stage Handoff

No SAFE_TO_MERGE findings in this pass.
