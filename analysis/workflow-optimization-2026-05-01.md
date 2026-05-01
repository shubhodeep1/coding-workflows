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

