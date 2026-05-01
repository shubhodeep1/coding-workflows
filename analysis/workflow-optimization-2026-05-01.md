## Executive Summary

- **The dominant end-to-end bottleneck is the review/autofix handoff in stable-release smoke tests.** `test_and_mark_stable` failed in run **25150961704** after **4,788s** at `e2e-smoke-test → Phase 4: Wait for review & autofix to complete`; the log shows a full **30-minute** wait ending with `No review_autofix run with head_sha=... ever appeared within 30m`. Estimated impact: **20-30 min saved per affected release run + major failure reduction**. Confidence: **high**.
- **Review/autofix churn is the largest avoidable AI-cost sink.** `review_autofix` had **146 total runs**, only **52 successes**, and **94 cancelled** runs; family p50 is **28s** but p95 is **1,892s**, showing frequent cancel/restart churn plus long tails. Recent runs on 2026-04-30 show cancelled review runs immediately preceding **1,470-2,266s** successful reruns. Estimated impact: **30-60% reduction in review-phase AI spend on busy PRs**. Confidence: **high**.
- **CI is spending 9-11 minutes to discover fast structural failures that could be caught earlier.** Run **25155077424** failed after **622s** on mixed-tab Ruff errors in `scripts/consolidate_soft_error_reports.py`; run **25151563308** failed after **567s** on a checkout audit assertion for `forward-merge-stable-to-main.yml`. Estimated impact: **8-10 min faster feedback on failing CI runs**. Confidence: **high**.
- **The orchestrator is healthy in basic uptime but inefficient in flow control.** `orchestrate_poll` is **35/35 successful** with p50 **46s**, yet `clarify`, `plan`, `implement`, and `orchestrate_clarify_respond` have p50 **1-2s** with overwhelmingly skipped/other outcomes, indicating heavy dispatch-before-check behavior. Estimated impact: **lower runner usage, fewer API calls, less state churn**. Confidence: **high**.
- **AI memory is operational but underperforming as retrieval infrastructure.** Across sampled logs there were **7 memory retrieve operations** with only **1 hit** (**14.3% hit rate**); **6/7** returned zero records, and reviewer retrieves in slow `review_autofix` runs all had `keyword_method: none`. Estimated impact: **small direct speed gain, moderate quality/repeatability gain if fixed**. Confidence: **medium**.
- **Prompt caching appears enabled but is not auditable from current telemetry.** Logs repeatedly show `OPENROUTER_PROMPT_CACHE_DISABLED=false`, but sampled runs do **not** emit prompt/completion totals or cache create/read hit metrics, so token savings cannot be quantified. Estimated impact: **medium cost savings once instrumented and stabilized**. Confidence: **medium**.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Fix the review/autofix dispatch gap in stable-release smoke tests
**Type:** Critical-path win

- **Evidence**
  - `test_and_mark_stable` family: p50 **4,404s**, avg **4,016s**, failure rate **66.7%** (2/3).
  - Run **25150961704** failed at `e2e-smoke-test → Phase 4: Wait for review & autofix to complete` after **4,788s**.
  - The run log shows repeated idle polling and ends with: `No review_autofix run with head_sha=... ever appeared within 30m — bait push did not trigger downstream workflows`.
- **Root cause**
  - Downstream `review_autofix` either never dispatches for the bait commit, or the wait logic is matching too narrowly/incorrectly on `head_sha`.
- **Exact change**
  - After the bait commit is pushed, record the expected downstream trigger explicitly:
    1. **Directly dispatch** the review workflow for the PR/head when possible, or
    2. Capture the downstream `run_id` within a short bounded window (60-90s), then wait on that `run_id` instead of polling broadly for a matching SHA.
  - Keep the current polling logic as a fail-open fallback if dispatch confirmation is unavailable.
- **Estimated time savings**
  - **20-30 min** per affected stable-release run.
  - Also avoids wasting nearly the full release-gate runtime on known-bad waits.
- **Implementation risk**
  - **Medium**. Touches workflow orchestration, but can be rolled out behind a fallback path.

### 2. Cut review/autofix cancel-restart churn with stronger concurrency and stale-head gating
**Type:** Critical-path win

- **Evidence**
  - `review_autofix`: **146 total**, **94 cancelled** (**64.4%**), **52 success**, p50 **28s**, p95 **1,892s**.
  - Recent runs on 2026-04-30 show cancel/restart patterns:
    - cancelled **25170789463** (**307s**) before success **25170792689** (**2,266s**)
    - cancelled **25167320416** (**600s**) before success **25167757683** (**1,470s**)
    - cancelled **25176258486** (**314s**) before success **25176480413** (**1,514s**)
- **Root cause**
  - New pushes or retriggers start overlapping review runs that are only cancelled after meaningful work has already started.
- **Exact change**
  - Use a stricter concurrency key based on **PR number + head SHA**.
  - Before launching reviewer/editor work, compare current PR head with the triggering SHA; if stale, exit before AI work starts.
  - Cancel superseded runs **before** reviewer/editor/bootstrap setup.
- **Estimated time savings**
  - **15-30 min** per busy PR in wall-clock waste reduction.
  - Large aggregate runner savings.
- **Implementation risk**
  - **Low-medium**. Behavior is backward-compatible if only stale heads are skipped.

### 3. Reorder CI to fail fast on structural checks before long test suites
**Type:** Critical-path win for failing runs

- **Evidence**
  - Run **25155077424** failed after **622s** with Ruff `E101 Indentation contains mixed spaces and tabs` in `scripts/consolidate_soft_error_reports.py`.
  - Run **25151563308** failed after **567s** with `AssertionError: Unclassified checkout@v5 workflows: ['forward-merge-stable-to-main.yml']`.
  - In both failures, many tests had already completed successfully before the final failure surfaced.
- **Root cause**
  - Fast deterministic checks are sequenced after or mixed into a long-running lint/test job.
- **Exact change**
  - Split the current `lint` job into:
    1. **fast-guard**: Ruff, workflow classification audit, script-ref audit
    2. **unit-and-integration**: the heavier test suites
  - Gate the heavy suite on fast-guard success.
- **Estimated time savings**
  - **8-10 min** faster feedback on failing runs.
- **Implementation risk**
  - **Low**.

### 4. Stop dispatching skip-prone phase workflows when state already proves they will no-op
**Type:** Mixed critical-path + systemic win

- **Evidence**
  - `clarify`: **132 total**, **11 success**, **121 other**, p50 **1s**
  - `plan`: **118 total**, **9 success**, **109 other**, p50 **1s**
  - `implement`: **118 total**, **13 success**, **102 other**, p50 **1s**
  - `orchestrate_clarify_respond`: **118 total**, **2 success**, **116 other**, p50 **1s**
- **Root cause**
  - Orchestrator is dispatching phase workflows that immediately skip after checking labels/state.
- **Exact change**
  - Move the state/label eligibility check into the upstream dispatcher so only the next valid phase is launched.
  - Preserve workflow-level guardrails, but use them as safety nets rather than primary routing.
- **Estimated time savings**
  - **Small per run**; **large aggregate** across the pipeline.
  - Also reduces queue pressure and API churn.
- **Implementation risk**
  - **Low**.

### 5. Reduce empty poller cycle overhead
**Type:** Local micro-optimization with meaningful aggregate impact

- **Evidence**
  - `orchestrate_poll`: **35/35 success**, p50 **46s**, p95 **97.2s**.
  - Recent poll logs show successful `poll_completed` events with `has_work=false`.
  - The poller still performs repo/workflow-support setup in successful empty cycles.
- **Root cause**
  - Poll cadence and early-cycle setup are not optimized for no-work cycles.
- **Exact change**
  - Add a fast pre-check path:
    - list active tracking issues first
    - if none, exit before repository checkout/support-file staging
  - Consider a slightly longer cron interval when there are no active orchestration issues.
- **Estimated time savings**
  - **40-50s per empty poll cycle**.
- **Implementation risk**
  - **Low**.

## Cost Optimizations

Ranked by expected token and/or dollar savings.

### 1. Eliminate cancelled `review_autofix` AI runs before model work starts
- **Evidence**
  - `review_autofix` has **94 cancelled** runs out of **146** total.
  - Recent cancelled runs lasted **25s**, **307s**, **314s**, **600s**, and **655s** before being superseded.
- **Root cause**
  - Reviewer/editor work begins on runs that are already stale by head SHA.
- **Exact change**
  - Apply head-SHA stale-run gating before reviewer bootstrap and before any model invocation.
  - Strengthen concurrency cancellation at workflow start.
- **Estimated savings**
  - Likely the **largest single AI-cost reduction** in the sampled window.
  - Best estimate: **30-60% of review_autofix token spend on active PRs**.
- **Quality-risk notes**
  - **Low** if only stale runs are suppressed.

### 2. Lower reasoning effort for bounded or retry-path tasks
- **Evidence**
  - Logs show `MODEL_REASONING_EFFORT: xhigh` in implement and `REVIEWER_REASONING_EFFORT: xhigh`, `EDITOR_REASONING_EFFORT: xhigh` in review flows.
  - The failed implement run **25151206656** was for a narrowly scoped single-file canary edit, yet still ran with `xhigh`.
- **Root cause**
  - Expensive reasoning defaults are applied broadly, including retries and narrow-change tasks.
- **Exact change**
  - Keep first-pass high reasoning only where needed.
  - Use:
    - **medium** for narrow single-file implement issues
    - **medium or low** for retry attempts after the first unsuccessful attempt
    - **xhigh** only for conflict resolution / complex multi-file repair
- **Estimated savings**
  - **20-40% token and latency reduction** on AI-heavy phases, depending on model pricing.
- **Quality-risk notes**
  - **Medium**. Start with retries and constrained issues to minimize quality regression.

### 3. Ensure deterministic soft-error consolidation is fully backported and remove any residual second-pass summarization
- **Evidence**
  - The slow review log for run **25165138809** documents replacement of the old Phase 8 LLM re-summarization with deterministic concatenation because the previous flow introduced fidelity loss and extra wait time.
  - Failing `test_and_mark_stable` runs in the sampled window are from before/around this transition.
- **Root cause**
  - Redundant LLM summarization added cost and latency while degrading accuracy.
- **Exact change**
  - Verify that all stable-release branches now use deterministic consolidation only.
  - Remove any remaining wait-for-analyzer or second-pass LLM summarization hooks.
- **Estimated savings**
  - **One or more model calls plus several minutes** per release-gate run.
- **Quality-risk notes**
  - **Low**; this is a simplification and accuracy improvement.

### 4. Make prompt cache measurable and stabilize cacheable prompt prefixes
- **Evidence**
  - Logs repeatedly show `OPENROUTER_PROMPT_CACHE_DISABLED=false`.
  - No sampled logs expose cache read/create counts, prompt tokens, completion tokens, or total token usage.
- **Root cause**
  - Prompt cache is enabled but not instrumented; dynamic prompt assembly likely fragments cache keys.
- **Exact change**
  - Emit per-call telemetry for:
    - prompt tokens
    - completion tokens
    - cache create/read counts
    - model name and reasoning effort
  - Normalize prompts so static instructions come first, dynamic issue/PR content last.
- **Estimated savings**
  - **Unquantified but likely medium** for repeated implement/review prompts.
- **Quality-risk notes**
  - **Low**.

### 5. Reduce empty poller cost and skip-heavy workflow fan-out
- **Evidence**
  - Frequent successful `orchestrate_poll` cycles with no active work.
  - Skip-heavy families consume dispatch and setup overhead with little useful output.
- **Root cause**
  - Workflow routing is paying orchestration overhead for no-op decisions.
- **Exact change**
  - Pre-check active work before launching poller heavy setup and phase workflows.
- **Estimated savings**
  - Mostly runner/API savings; **small direct model savings**.
- **Quality-risk notes**
  - **Low**.

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Make stable-release smoke tests wait on a confirmed downstream review run, not inferred trigger behavior
- **Failure evidence**
  - Run **25150961704** failed after **4,788s** waiting for review/autofix.
  - Run **25147636528** failed earlier in the same flow when the editor did not remove the bait line.
- **Root cause category**
  - **Cross-workflow handoff / trigger reliability**
- **Exact fix**
  - After bait commit push:
    - capture the exact downstream run ID, or
    - explicitly dispatch downstream review/autofix with required PR/SHA inputs
  - Fail fast if no downstream run appears within a short bootstrap window.
- **Expected reliability impact**
  - **High**. This directly addresses the highest-severity sampled release failures.
- **Rollback/fail-open considerations**
  - Keep current polling as fallback if explicit run binding is unavailable.

### 2. Add a preflight support-bundle integrity check before implement/review model work
- **Failure evidence**
  - Implement run **25151206656** logged:
    - `Failed to checkout workflow support source`
    - `Missing required support script`
    - failed staging of `prompts/serena-efficiency-block.txt`, `mode-implement-diagnose.txt`, `mode-implement-repair.txt`
  - The run then still entered the Codex loop and later failed after repeated empty-output attempts.
- **Root cause category**
  - **Bootstrap artifact drift / missing runtime assets**
- **Exact fix**
  - Add a dedicated early preflight step that verifies the support manifest and required prompt files before launching any AI work.
  - If the canonical support source is unavailable, fall back to a known-good snapshot only if the snapshot passes manifest validation.
- **Expected reliability impact**
  - **Medium-high**. Prevents expensive failed runs caused by missing bootstrap assets.
- **Rollback/fail-open considerations**
  - Fail open only to validated fallback snapshots, not to partially missing bundles.

### 3. Fail fast on Ruff/audit regressions before long CI test execution
- **Failure evidence**
  - Run **25155077424**: Ruff indentation failure after **622s**
  - Run **25151563308**: workflow classification assertion after **567s**
- **Root cause category**
  - **Pipeline ordering / late surfacing of deterministic failures**
- **Exact fix**
  - Move Ruff and workflow audits ahead of long-running suites.
- **Expected reliability impact**
  - **Medium**. Reduces wasted reruns and speeds developer recovery.
- **Rollback/fail-open considerations**
  - None needed.

### 4. Strengthen canary/bait enforcement so the editor must actually touch the expected file
- **Failure evidence**
  - Run **25147636528**:
    - bait commit `91d6077` pushed successfully
    - final error: `Editor failed to remove bait line E2E_EDITOR_BAIT_25147636528`
    - canary file still contained the bait marker
- **Root cause category**
  - **Validation contract mismatch**
- **Exact fix**
  - Inject the canary file into the editor’s allowed/required scope for smoke runs.
  - Require the review gate to confirm bait removal before marking success.
- **Expected reliability impact**
  - **High** for smoke-test validity.
- **Rollback/fail-open considerations**
  - Keep current warning-only bait push behavior only when bait commit creation itself fails.

### 5. Auto-enforce workflow classification updates when new checkout workflows are added
- **Failure evidence**
  - Run **25151563308** failed on:
    - `AssertionError: Unclassified checkout@v5 workflows: ['forward-merge-stable-to-main.yml']`
- **Root cause category**
  - **Metadata drift / audit contract mismatch**
- **Exact fix**
  - Generate or validate the audit classification list as part of workflow creation/modification.
- **Expected reliability impact**
  - **Low-medium**.
- **Rollback/fail-open considerations**
  - None.

## AI Memory Health

Observed `AI_MEMORY_TELEMETRY` in sampled logs.

### Retrieval effectiveness
- **Retrieve operations observed:** **7**
- **Hit rate:** **14.3%** (**1/7** had `records_selected > 0`)
- **Average `estimated_tokens`:** **4.0**
- **Average budget vs actual:** budget was **not meaningfully emitted** in sampled retrieves (`budget_tokens` absent/0), so budget adherence cannot be audited from this window.
- **`keyword_method` distribution:**
  - `none`: **6**
  - `plain`: **1**
  - `llm`: **0**

### Notable findings
- **Zero-record retrieves dominate:** **6/7** retrieves returned `records_selected: 0`.
  - In slow `review_autofix` runs **25155963108**, **25161156595**, **25163198735**, and **25165138809**, reviewer retrieves all logged `keyword_method: none` and `records_selected: 0`.
- **One successful retrieve appeared in implement run `25151206656`:**
  - `role: implementation`
  - `records_selected: 1`
  - `estimated_tokens: 28`
  - `keyword_method: plain`
- **No silent-failure indicators were observed:**
  - `fail_open: true`: **0**
  - `enabled: false`: **0**
  - push retry counts > 1: **0**
- **Ledger/event recording looks healthy:**
  - `record-run-event` telemetry was common in `implement`, `review_autofix`, and `orchestrate_poll`.
  - `orchestrate_poll` logged clean `poll_started` and `poll_completed` events.

### Recommendations
1. **Fix reviewer retrieval inputs first.** `keyword_method: none` on most review retrieves means memory is effectively bypassed.
2. **Emit retrieval budgets explicitly** so budget-vs-usage can be audited.
3. **Track per-role hit rate** (`implementation`, `reviewer`, `judge`, `poller`) to see where memory is providing real value.
4. **Alert on repeated zero-hit streaks** for reviewer flows; current sample suggests systematic underuse rather than sparse memory contents.

## GH API Call Audit

The bundle does not contain a full per-request API trace, so findings below are based on direct command evidence in sampled step logs. Where exact counts could be polluted by embedded prompt text, I use the pattern evidence rather than claiming exact request totals.

### 1. `orchestrate_poll` performs a proactive `/rate_limit` probe on empty cycles
- **Evidence**
  - Recent poll run **25198719902** logs:
    - `gh api -i /rate_limit`
    - retry helper setup
    - `gh issue list`
  - The same family ran **35 successful cycles** with p50 **46s**.
- **Redundancy pattern**
  - Poll cycles are paying at least one explicit rate-limit API call even when no actual retry event occurs.
- **Concrete change**
  - Only query `/rate_limit` inside the retry branch after a 429/secondary-limit signal, not preemptively.
- **Estimated call-count reduction**
  - Roughly **1 API call per poll cycle**.
- **Rate-limit risk reduction**
  - Small per cycle, meaningful in aggregate because polling is frequent.

### 2. `issue_pr_status` already batches, but fallback can still degenerate into per-issue REST fetches
- **Evidence**
  - Recent run **25196359113** shows:
    - batched GraphQL issue detection
    - fallback path to per-issue REST when batch detection fails
    - comment in log explicitly describing “Single batched GraphQL call”
- **Redundancy pattern**
  - Current fallback appears coarse-grained: batch failure can trigger per-item REST lookups.
- **Concrete change**
  - Keep the batch-first design, but make fallback **partial**:
    - only REST-fetch items absent from GraphQL results
    - cache batch payload for the rest of the step
- **Estimated call-count reduction**
  - From **1 batched query + N issue GETs** down to **1 batched query + only missing-item fetches**.
- **Rate-limit risk reduction**
  - Moderate on PRs linked to multiple issues.

### 3. `review_post-merge-validate-dispatch` does repeated metadata lookups for a single merged PR event
- **Evidence**
  - Recent run **25196359103**:
    - GraphQL lookup for linked issues
    - `gh api repos/.../pulls/${PR_NUMBER}`
    - `gh workflow run ...`
    - `gh issue edit ... --remove-label`
- **Redundancy pattern**
  - The merged PR event already contains metadata that is re-fetched via REST.
- **Concrete change**
  - Use the event payload for PR title/body where possible.
  - Only call PR REST if the event payload is missing required fields.
- **Estimated call-count reduction**
  - **1 API call per merged PR** in the common case.
- **Rate-limit risk reduction**
  - Low individually, worthwhile at scale.

### 4. `implement` fetches issue state more than once within the same run
- **Evidence**
  - Failed implement run **25151206656** shows:
    - issue labels fetched early with `gh api repos/.../issues/${ISSUE_NUMBER}`
    - later issue metadata fetched again into `ISSUE_META_FILE`
    - comments fetched separately with `--paginate`
    - labels fetched again before edit operations
- **Redundancy pattern**
  - Same issue object is retrieved repeatedly rather than materialized once into a cycle-local cache.
- **Concrete change**
  - Fetch issue JSON once at the beginning of the run and derive:
    - labels
    - title/body
    - state
  - Refresh only immediately before write operations if stale data matters.
- **Estimated call-count reduction**
  - Likely **2-3 REST calls per implement run**.
- **Rate-limit risk reduction**
  - Moderate over many orchestrated issues.

### 5. Over-dispatch of skip-prone workflows inflates API traffic indirectly
- **Evidence**
  - `clarify`, `plan`, `implement`, `orchestrate_clarify_respond` all have extremely high skipped/other counts and p50 **1-2s**.
- **Redundancy pattern**
  - Each dispatched workflow still performs setup, state checks, and some GitHub API work before skipping.
- **Concrete change**
  - Decide phase eligibility upstream in the orchestrator before workflow dispatch.
- **Estimated call-count reduction**
  - Not directly measurable from this sample, but likely **large aggregate reduction**.
- **Rate-limit risk reduction**
  - High over time due to fewer no-op launches.

## MCP & Serena Efficiency

Direct Serena/MCP invocation traces were **not** present in a form that allows a true symbol-lookup-vs-broad-read audit. The sampled logs do show Serena-related bootstrap assets and report files, so the assessment below is bounded to what was observable.

### Observed issues
1. **Serena-related assets are on the critical bootstrap path**
   - Implement run **25151206656** failed staging:
     - `prompts/serena-efficiency-block.txt`
     - `mode-implement-diagnose.txt`
     - `mode-implement-repair.txt`
   - This means Serena/reporting support artifacts can block the whole implement phase before useful work starts.

2. **Serena report generation exists, but operational telemetry is missing**
   - Logs reference `SERENA_REPORT_FILE` and `setup_serena.sh`.
   - No structured counters were emitted for:
     - symbol lookups
     - raw file reads
     - repeated file-region reads
     - tool fallback rates

### Recommendations
- **Make Serena efficiency reporting non-blocking for core implement/review execution.**
  - If `serena-efficiency-block.txt` or related prompt assets are missing, degrade the reporting layer, not the whole AI phase.
- **Emit structured Serena telemetry into logs/artifacts.**
  - At minimum:
    - symbol lookup count
    - raw read count
    - repeated-region read count
    - fallback-to-non-Serena count
- **Separate bootstrap integrity from runtime AI work.**
  - Validate Serena prompt/report assets in a preflight step so failures are early and attributable.
- **Parallelize safe static asset staging.**
  - Support-file staging is deterministic and can be parallelized or cached independently from AI execution.

### Expected impact
- **Latency:** medium
- **Token efficiency:** medium once telemetry exists
- **Correctness risk:** low if reporting remains optional and core AI paths stay intact

## Prompt Cache & Memory System

### Prompt cache behavior
- **Observed**
  - Multiple workflows log `OPENROUTER_PROMPT_CACHE_DISABLED=false`.
  - No sampled logs emit:
    - prompt tokens
    - completion tokens
    - total tokens
    - cache create counts
    - cache read/hit counts
- **Assessment**
  - Cache is likely enabled, but effectiveness is currently **not auditable**.
  - This makes it impossible to distinguish:
    - healthy cache reuse
    - fragmentation from dynamic prefixes
    - silent fail-open behavior

### Likely cache-fragmentation causes
- Large dynamic issue/PR bodies are injected into prompts.
- Runtime paths, branch names, run IDs, and environment-specific details appear frequently in prompt-adjacent logs.
- Some phases appear to embed long workflow guidance blocks into model context, increasing prefix instability.

### Memory retrieval effectiveness
- See AI Memory Health: retrieval exists, but most sampled reviewer fetches are effectively no-ops.

### Recommendations
1. **Instrument every model call**
   - Emit:
     - model
     - reasoning effort
     - prompt/completion/total tokens
     - cache create/read metrics
2. **Stabilize prompt prefixes**
   - Put invariant system/mode instructions first.
   - Append dynamic issue/PR/run context after the cacheable header.
3. **Reduce prompt variance**
   - Normalize whitespace and ordering of injected metadata.
   - Avoid embedding volatile diagnostics into the reusable prompt prefix.
4. **Treat memory budgets as first-class telemetry**
   - Emit budget tokens and selected-record counts consistently for all roles.
5. **Alert on cache blindness**
   - If cache is enabled but no create/read metrics are emitted, mark the run as telemetry-incomplete.

### Estimated impact
- **Tokens:** medium potential savings, currently unquantified
- **Latency:** small-to-medium improvement on repeated prompts
- **Reliability:** moderate telemetry improvement

## Orchestrator Health

### Current health assessment
- **Stable uptime for polling core**
  - `orchestrate_poll` is **35/35 successful** with p50 **46s**.
- **Inefficient flow progression**
  - Skip-heavy phase families indicate dispatch-first, decide-later orchestration:
    - `clarify`: **121 other/skipped** out of **132**
    - `plan`: **109 other/skipped** out of **118**
    - `implement`: **102 other/skipped** out of **118**
    - `orchestrate_clarify_respond`: **116 other/skipped** out of **118**
- **Operational pain point**
  - `review_autofix` shows severe cancel/restart churn:
    - **94 cancelled** of **146**
    - p50 **28s**, p95 **1,892s**
  - This usually means the orchestrator is driving redundant or superseded work faster than cancellation can catch up.

### Smallest safe mitigations
1. **Eligibility-first dispatch**
   - Don’t launch phase workflows unless state proves they are eligible.
2. **Head-SHA dedupe before review work**
   - Cancel or short-circuit stale review runs immediately.
3. **Short bootstrap SLA for downstream workflows**
   - If a downstream workflow should exist but hasn’t appeared within 60-90s, treat that as a handoff problem instead of waiting 30 minutes.
4. **Fast no-work exit in poller**
   - Skip expensive setup when there are no active tracking issues.

### Indicators to track after changes
- `% skipped/other` by workflow family
- `% cancelled` for `review_autofix`
- median time from PR head update to first matching `review_autofix` run
- poll cycles with `has_work=false`
- count of downstream dispatches that never materialize as runs within 90s
- average number of workflow launches per successful orchestration cycle

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

### 1. Review/autofix handoff failure and wait-loop overhead
- **Stage:** implement → review/autofix
- **Evidence:** run **25150961704** spent a full **30-minute** review wait window before failing.
- **Bottleneck type:** **retry/coordination overhead**
- **Fix:** direct downstream run binding or dispatch confirmation.

### 2. Review/autofix compute tail
- **Stage:** review/autofix
- **Evidence:** successful slow runs of **1,514s**, **1,641s**, **2,038s**, **2,048s**, **2,211s**, **2,266s**, **2,722s**
- **Bottleneck type:** **compute**
- **Fix:** stale-head dedupe, lower reasoning on retries, and avoid launching doomed runs.

### 3. CI long feedback loop for deterministic failures
- **Stage:** validate/lint
- **Evidence:** failures after **567-622s** for issues that are fast to detect.
- **Bottleneck type:** **compute/order-of-operations**
- **Fix:** split fast guards from long tests.

### 4. Empty or skip-heavy orchestration cycles
- **Stage:** clarify → plan → implement dispatching, plus poller cycles
- **Evidence:** phase families with p50 **1s** and dominant skipped/other outcomes; poller p50 **46s** even when no work.
- **Bottleneck type:** **queueing/control-flow overhead**
- **Fix:** upstream state gating and fast no-work exits.

### 5. Queue/start lag in implement handoff during E2E
- **Stage:** plan → implement
- **Evidence:** in run **25150961704**, implement stayed `pending/queued` before progressing to `in_progress`.
- **Bottleneck type:** **queueing**
- **Fix:** reduce unnecessary concurrent workflow launches to free runner capacity.

### 6. Workflow-log-analysis long runtime
- **Stage:** deep audit / analysis
- **Evidence:** runs **25147648667** and **25150979660** took **3,990s** and **4,328s**
- **Bottleneck type:** **compute**
- **Fix:** keep out of critical release path unless explicitly needed; cache intermediate analysis artifacts.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` cancel/restart churn and long-tail execution
- `test_and_mark_stable` review handoff failures
- CI failing late on fast deterministic checks
- skip-heavy orchestrator fan-out

**Top failure modes**
- Stable-release E2E wait failure: run **25150961704**
- Stable-release editor bait not removed: run **25147636528**
- CI Ruff indentation failures: runs **25155077424**, **25155367775**
- CI workflow classification drift: runs **25151563308**, **25151747897**, **25156894907**
- Implement bootstrap/support bundle drift plus Codex empty-output bail: run **25151206656**

**Highest-cost drivers**
- Cancelled/superseded `review_autofix` runs
- Long `review_autofix` successes (25-45 min class)
- Stable-release smoke tests with 47-80 min total runtime
- Workflow-log-analysis runs near 66-72 min

**Top 3 prioritized actions**
1. **Fix downstream review/autofix run binding in `test_and_mark_stable`**
   - Highest reliability and largest critical-path speed win.
2. **Add head-SHA concurrency dedupe to `review_autofix`**
   - Highest likely token/cost reduction.
3. **Split CI fast guards from long suites**
   - Fastest low-risk feedback improvement.

## Metrics Appendix

### Overall run summary

| Repo | Total Runs | Success | Failure | Cancelled | Other/Skipped | Failure Rate | p50 Duration | p95 Duration |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 849 | 294 | 9 | 96 | 450 | 1.06% | 2s | 643s |

### Key workflow-family metrics

| Workflow Family | Total | Success | Failure | Cancelled | Other | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 90 | 85 | 5 | 0 | 0 | 605.6 | 608 | 651.7 |
| review_autofix | 146 | 52 | 0 | 94 | 0 | 422.9 | 28 | 1892 |
| test_and_mark_stable | 3 | 1 | 2 | 0 | 0 | 4016.0 | 4404 | 4749.6 |
| orchestrate_poll | 35 | 35 | 0 | 0 | 0 | 55.1 | 46 | 97.2 |
| implement | 118 | 13 | 1 | 2 | 102 | 27.4 | 1 | 207.3 |
| clarify | 132 | 11 | 0 | 0 | 121 | 11.9 | 1 | 93.7 |
| plan | 118 | 9 | 0 | 0 | 109 | 14.3 | 1 | 151.7 |
| orchestrate_clarify_respond | 118 | 2 | 0 | 0 | 116 | 1.3 | 1 | 2.0 |
| workflow_log_analysis | 2 | 2 | 0 | 0 | 0 | 4159.0 | 4159 | 4311.1 |

### Notable failed runs

| Run ID | Workflow Family | Duration (s) | Failure Point |
|---|---|---:|---|
| 25150961704 | test_and_mark_stable | 4788 | e2e-smoke-test → Phase 4: Wait for review & autofix to complete |
| 25147636528 | test_and_mark_stable | 4404 | e2e-smoke-test → Phase 4b: Verify editor removed bait line |
| 25155077424 | ci | 622 | lint → Python lint (ruff) |
| 25155367775 | ci | 597 | lint → Python lint (ruff) |
| 25151563308 | ci | 567 | lint → Validation self-test unit tests |
| 25151747897 | ci | 581 | lint → Validation self-test unit tests |
| 25156894907 | ci | 499 | lint → Validation self-test unit tests |
| 25151206656 | implement | 476 | implement / implement → Run Codex implementation |
| 25145624630 | nightly_validation_selftest | 94 | validation-selftest → Run validation self-test matrix |

### Representative GH API usage patterns from sampled logs

| Run ID | Step | Observed Pattern |
|---|---|---|
| 25198719902 | `poll_poll` | `gh issue list`, proactive `/rate_limit` probe, retry helper present |
| 25196359113 | `sync-status_sync-issue-status` | batched GraphQL classification with per-issue REST fallback path |
| 25196359103 | `review_post-merge-validate-dispatch` | GraphQL linked-issue lookup + PR REST fetch + workflow dispatch + issue label edit |
| 25151206656 | `implement_implement` | repeated issue metadata/labels fetches + paginated comments fetch within one run |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total telemetry entries observed | 46 |
| `record-run-event` ops | 32 |
| `retrieve` ops | 7 |
| `record-candidate` ops | 5 |
| `processed-command-check` ops | 1 |
| `processed-command-claim` ops | 1 |
| Retrieve hit rate | 14.3% |
| Zero-record retrieves | 6 / 7 |
| Avg estimated tokens per retrieve | 4.0 |
| `keyword_method=none` | 6 |
| `keyword_method=plain` | 1 |
| `keyword_method=llm` | 0 |
| `fail_open: true` entries | 0 |
| `enabled: false` entries | 0 |
| push attempts > 1 | 0 |

### Token/cache telemetry availability

| Signal | Observed? | Notes |
|---|---|---|
| Prompt tokens | No | Not emitted in sampled logs |
| Completion tokens | No | Not emitted in sampled logs |
| Total tokens | No | Not emitted in sampled logs |
| Prompt cache create/read hits | No | Cache enabled flag present, effectiveness not measurable |
| Memory telemetry | Yes | Limited but usable |
| Model/reasoning config | Yes | Seen in environment logs for implement/review phases |


## Deep Audit — Workflows & Scripts (2026-05-01)

### Section 1: Bug & Correctness Sweep

#### Finding BUG-001
- **File path** — `.github/workflows/issue_pr_status.yml:253-279,353-380`
- **Severity** — High
- **Category tag** — `bug`
- **Description** — The standalone/orchestrator close policy documented in the comment block is not what the shell condition implements. The comment says standalone issues should keep the historical `PR_BASE_REF == main` close gate, but the actual condition is `if [ "${PR_MERGED}" != "true" ] || [ "${PR_BASE_REF}" = "main" ] || [ "${is_managed_child}" = "true" ]; then`. For any unmerged PR close, the first disjunct is true, so linked standalone issues are always relabeled and closed, including abandoned feature/integration PRs that were never merged. That is a correctness bug and directly contradicts the policy described a few lines above.
- **Recommended fix** — Split the close logic into explicit cases that match the documented policy: (1) orchestrator tracking issues: skip; (2) orchestrator-managed child issues: close only on merge, regardless of non-main integration base; (3) standalone issues: require the historical `PR_BASE_REF == main` gate before applying terminal label/close behavior. Add a regression test covering “closed without merge on non-main branch leaves standalone issue open.”

#### Finding BUG-002
- **File path** — `.github/workflows/review_autofix.yml:4133-4140,4224-4229,4342-4362`
- **Severity** — Medium
- **Category tag** — `bug`
- **Description** — Both retrigger paths read `${AUTOFIX_RETRIGGER_PEER_WAIT_SECS:-8}` in shell, but neither step exports that repository variable through `env:` or `$GITHUB_ENV`. As written, the configured repo variable is ignored and both paths always fall back to `8`, so operators cannot tune peer-settle timing even though README/agents.md document that knob.
- **Recommended fix** — Add `AUTOFIX_RETRIGGER_PEER_WAIT_SECS: ${{ vars.AUTOFIX_RETRIGGER_PEER_WAIT_SECS }}` to both retrigger steps, or promote it to workflow/job `env:` once. Log the resolved value before sleeping so tuning errors are visible in the run log.

#### Finding BUG-003
- **File path** — `.github/workflows/validation-refresh.yml:147-174`
- **Severity** — Medium
- **Category tag** — `bug`
- **Description** — The failure notification step claims it suppresses raw-curl Telegram pings for `ALERT_MSG_LEVEL=SILENT`, but the step never exports `ALERT_MSG_LEVEL`. Because the shell expands `${ALERT_MSG_LEVEL:-DEBUG}`, the suppression gate always behaves as `DEBUG`, so smoke-gate or silent dispatches still emit Telegram on failure.
- **Recommended fix** — Export `ALERT_MSG_LEVEL: ${{ inputs.alert_msg_level || vars.ALERT_MSG_LEVEL || 'DEBUG' }}` into the step, or route the notification through `scripts/tg_helpers.sh` so the existing level filtering is reused consistently.

### Section 2: GitHub API Call Redundancy Audit

#### Finding API-001
- **File path** — `.github/workflows/review_autofix.yml:1269-1307`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — `Collect PR metadata` defines an inline `gh_retry` that probes `gh api -i /rate_limit` inside `_rl_wait` on every rate-limit event. That makes each rate-limited retry cost one failed target call plus one extra `/rate_limit` request, even though the repo already has a shared retry helper in `scripts/gh_helpers.sh`.
- **Current call count** — `2` logical API calls per rate-limited attempt (`target call` + `/rate_limit` probe).
- **Proposed call count after fix** — `1` logical API call per rate-limited attempt (shared helper backoff only).
- **Existing batching/helper pattern to extend** — Reuse `scripts/gh_helpers.sh:381-445` (`gh_retry`) instead of the inline retry function.
- **Recommended fix** — Source `scripts/gh_helpers.sh` before this step and replace the local `_rl_wait`/`gh_retry` block with the shared helper. That removes the redundant `/rate_limit` request and also keeps retry semantics aligned with the rest of the repo.

#### Finding BATCH-001
- **File path** — `.github/workflows/issue_pr_status.yml:280-350,448-513`
- **Severity** — Medium
- **Category tag** — `api-batching`
- **Description** — The close/update step already pays for one aliased GraphQL classification pass to determine which linked issues are tracking vs orchestrator-managed. The later “Send PR merged Telegram alert” step then loops over `LINKED_ISSUE_NUMBERS` and re-fetches each issue body with `_safe_gh_jq "repos/${REPOSITORY}/issues/${issue_number}"` just to rediscover whether any linked issue is orchestrator-managed. That is an avoidable N-per-PR REST loop in the same execution path.
- **Current call count** — `1 + N` logical calls per merged PR (`1` GraphQL classification + `N` issue GETs in the alert loop).
- **Proposed call count after fix** — `1` logical call total (reuse/export the existing classification result).
- **Existing batching/helper pattern to extend** — Extend the existing `ORCH_ALIAS_FRAGMENT` GraphQL block in this workflow, or move that pattern into a shared helper alongside the aliased GraphQL approach already used in `scripts/orchestrate_poll_process.sh`.
- **Recommended fix** — Export `IS_ORCHESTRATED` or the `MANAGED_ISSUES` set from the classification step into `$GITHUB_ENV` and consume that in the alert step. Do not re-read each issue body one-by-one.

#### Finding API-002
- **File path** — `.github/workflows/test-and-mark-stable.yml:1081-1399`
- **Severity** — Medium
- **Category tag** — `api-redundancy`
- **Description** — The review wait loop is API-heavy on every poll iteration. Once the 10-minute threshold is crossed, one iteration can do: (1) list matching review runs, (2) fetch run jobs, (3) fetch PR head SHA, (4) fetch PR review-comment count, and (5) fetch job logs for byte size; when `JOBS_JSON` is absent, it can also re-fetch jobs for current-step extraction. That is materially more polling than necessary for an inactivity heartbeat.
- **Current call count** — Up to `5` logical API reads per poll iteration after `ELAPSED >= 600`, and up to `7` in the fallback path that refetches jobs.
- **Proposed call count after fix** — `3` logical reads per iteration: keep `runs` + `jobs/logs`, and batch PR head SHA plus comment count into a single GraphQL query only when `RUN_STATUS/JOBS_JSON` did not already change.
- **Existing batching/helper pattern to extend** — Follow the aliased GraphQL batching pattern already used in `scripts/orchestrate_poll_process.sh` and the GraphQL alias block in `.github/workflows/issue_pr_status.yml`.
- **Recommended fix** — Extract the poll body into a helper script that caches `JOBS_JSON` for the full iteration, batches PR metadata needed for `CUR_STATE`, and only pays the expensive comment/head probes when the run/job state is otherwise unchanged.

### Section 3: Code Duplication & Modularization Opportunities

#### Finding DUP-001
- **File path** — `.github/workflows/clarify.yml:47-120; .github/workflows/plan.yml:80-118; .github/workflows/implement.yml:220-260; .github/workflows/validate.yml:80-120; .github/workflows/orchestrate_clarify_respond.yml:83-120`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — The “resolve integration ref” bootstrap is effectively copy-pasted across five workflows: same temp-dir staging, same authenticated clone wrapper, same `stable`→`main` fallback, same masked clone-log handling, same trap cleanup, same execution of `scripts/resolve_integration_ref.sh`. This is high-maintenance duplication around auth, git error handling, and fallback semantics.
- **Recommended fix** — Move the staging/bootstrap logic into a shared module, e.g. `scripts/workflow_support.sh`, with a function like `resolve_integration_ref_from_canonical <issue_number> <repo> <gh_token>`. Update callers in `clarify.yml`, `plan.yml`, `implement.yml`, `validate.yml`, and `orchestrate_clarify_respond.yml` to source that helper and keep only workflow-specific inputs/outputs in YAML.

#### Finding DUP-002
- **File path** — `.github/workflows/review_autofix.yml:3698-3738,3826-3858; .github/workflows/issue_pr_status.yml:240-249`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — The fallback implementations of `ensure_label_exists` / `set_issue_phase_label_resilient` are duplicated in multiple late-stage workflows. The copies are close but not identical, which is risky because label semantics and descriptions are already centralized in `scripts/label_helpers.sh`.
- **Recommended fix** — Let `scripts/label_helpers.sh` own one bootstrap entrypoint, e.g. `load_label_helpers_or_fallback <support_scripts_dir> <repo>`, that guarantees `ensure_label_exists` and `set_issue_phase_label_resilient` exist. Update the late-stage `review_autofix` and `issue_pr_status` callers to use that shared contract instead of embedding ad hoc fallbacks.

#### Finding DUP-003
- **File path** — `.github/workflows/test-and-mark-stable.yml:2663-2893,2941-2990,3147-3170`
- **Severity** — Medium
- **Category tag** — `duplication`
- **Description** — `test-and-mark-stable.yml` repeats the same “dispatch workflow → poll for new run ID → watch run status until completion/timeout” shell block for multiple downstream workflows (`workflow-log-analysis`, `validation-refresh`, `update_workflows`, `internal-memory-maintenance`, and additional smoke dispatches later in the file). The repeated logic already drifted in deadlines and success-conclusion handling.
- **Recommended fix** — Extract a shared watcher, preferably `scripts/watch_dispatched_workflow.sh` or a `scripts/gh_helpers.sh` function like `watch_dispatched_workflow_run <repo> <workflow_file> <deadline_secs> <allowed_conclusions_csv>`. Update each dispatch site in `test-and-mark-stable.yml` to call the helper with only workflow-specific inputs.

### Section 4: Expression Size Limit Risk Assessment

#### Finding EXPR-001
- **File path** — `.github/workflows/test-and-mark-stable.yml:1081-1401`
- **Severity** — High
- **Category tag** — `expression-limit`
- **Description** — The `Phase 4: Wait for review & autofix to complete` `run:` block contains `${{ }}` interpolations and is already about **18,970 characters**, leaving only about **2,030 characters** of headroom before GitHub’s hard 21,000-character expression limit. This is the highest-risk block I found and it is on the same critical path that the repo has already had to split in other places.
- **Recommended fix** — Extract the loop into an external script such as `scripts/wait_for_review_run.sh`, pass only the needed env vars/inputs, and keep the YAML `run:` wrapper tiny. That matches the repo’s prior mitigation pattern of moving oversized logic out of workflow YAML.

#### Finding EXPR-002
- **File path** — `.github/workflows/review_autofix.yml:1266-1587`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — `Collect PR metadata` is about **16,437 characters**, leaving about **4,563 characters** of headroom. The block mixes retry helpers, PR fetches, linked-issue GraphQL, Python context generation, and file assembly in one interpolated `run:` block, so even modest additions can push it over the limit.
- **Recommended fix** — Split this into at least two steps or move it to a script like `scripts/review_collect_pr_metadata.sh`. The natural seam is: (1) API fetch/cache population, then (2) context-file rendering.

#### Finding EXPR-003
- **File path** — `.github/workflows/validate.yml:188-480`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — `Fetch workflow support files` is about **16,529 characters**, leaving about **4,471 characters** of headroom. It is a long interpolated shell program with many `${{ github.* }}` references, fallback branches, and embedded file lists.
- **Recommended fix** — Move the support-fetch logic into a dedicated script, e.g. `scripts/fetch_validate_support.sh`, and keep only input/env wiring in the workflow. This block is also a good candidate for reuse by other workflows that stage support assets.

#### Finding EXPR-004
- **File path** — `.github/workflows/orchestrate_clarify_respond.yml:840-1122`
- **Severity** — Medium
- **Category tag** — `expression-limit`
- **Description** — The auto-answer posting block is about **15,140 characters**, leaving about **5,860 characters** of headroom. It is below the 18k high-risk threshold, but still large enough that any new prompt text, ledger metadata, or loop-guard logic could push it into failure territory.
- **Recommended fix** — Extract the `/answer` assembly + processed-command completion path into an external script such as `scripts/orchestrate_auto_answer.sh`, or split the step into “decide” and “post/record” phases.

### Section 5: Cross-Cutting Concerns

#### Finding DEAD-001
- **File path** — `scripts/memory_helpers.sh:172-234`
- **Severity** — Low
- **Category tag** — `dead-code`
- **Description** — `memory_processed_command_list()` and `memory_promote()` are defined but have no repository references outside their own definitions. That is dead API surface in a central helper and increases maintenance/testing burden for the memory subsystem.
- **Recommended fix** — Either wire both functions into real callers and add tests, or remove them after confirming they are not part of an external compatibility contract.

#### Finding CONSIST-001
- **File path** — `.github/workflows/implement.yml:2074-2088,2203-2216; .github/workflows/test-and-mark-stable.yml:4319-4322; .github/workflows/update_workflows.yml:364-370; .github/workflows/validation-refresh.yml:169-174; .github/workflows/issue_pr_status.yml:531-537`
- **Severity** — Low
- **Category tag** — `consistency`
- **Description** — Telegram sending is implemented three different ways across workflows: some steps use `tg_helpers.sh`, some manually replicate alert-level filtering, and several fall back to raw `curl` directly in workflow YAML. That drift already shows up in behavior differences (`validation-refresh.yml` missed `ALERT_MSG_LEVEL` entirely) and makes notification semantics harder to reason about.
- **Recommended fix** — Standardize on one helper path, ideally `scripts/tg_helpers.sh` with a thin fail-open wrapper shared across workflows. Update the raw-curl callers so alert-level filtering, tracked/untracked sends, and cleanup behavior are consistent.

#### Finding SHELL-001
- **File path** — `scripts/validate_changed_files_syntax.sh:70-75`
- **Severity** — Low
- **Category tag** — `shellcheck`
- **Description** — The redaction case pattern has an unreachable later branch: `*.env*` on line 71 already matches `.envrc`, so the later `*,*.envrc|*,.env*` arm can never fire. ShellCheck flags this as SC2221/SC2222. The current behavior is still “skip dump,” but the pattern list no longer reflects the comment’s stated intent cleanly.
- **Recommended fix** — Reorder or collapse the overlapping patterns so the case expression is non-ambiguous and the comment/catalog match what the shell actually evaluates.

#### Finding DEBT-001
- **File path** — `.github/workflows/review_autofix.yml:1269-1307; .github/workflows/cancel_on_pr_close.yml:26-53; .github/workflows/test-and-mark-stable.yml:396-408,523-535,716-728,1096-1117`
- **Severity** — Low
- **Category tag** — `tech-debt`
- **Description** — Multiple workflows carry bespoke rate-limit/retry wrappers even though `scripts/gh_helpers.sh` already provides shared GitHub retry helpers. The local copies have already drifted in temp-file names, backoff behavior, and logging, which makes API behavior harder to audit and fix uniformly.
- **Recommended fix** — Fetch/source `scripts/gh_helpers.sh` earlier in those workflows and delete the inline retry clones. Keep one canonical retry contract in `gh_helpers.sh`, especially for rate-limit handling.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 2 | BUG-001, EXPR-001 |
| Medium | 11 | BUG-002, BUG-003, API-001, BATCH-001, API-002, DUP-001, DUP-002, DUP-003, EXPR-002, EXPR-003, EXPR-004 |
| Low | 4 | DEAD-001, CONSIST-001, SHELL-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 1-2 | Small |
| API call optimization | 3-4 | Medium |
| Code modularization | 6-8 | Large |
| Expression size reduction | 4-8 | Large |
| Medium/Low fixes | 6-10 | Medium |
