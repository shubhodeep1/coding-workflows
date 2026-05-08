## Executive Summary

- **Review/autofix is the largest end-to-end latency and waste source.** In `shubhodeep1/coding-workflows`, `review_autofix` has **106 total runs, 68 cancelled, p95 1696.5s**; sampled cancelled runs still ran for **2262s** (`run 25546727171`) and **1274s** (`run 25547336679`) before cancellation, and both were on the `claude-branch-review` comment-only path. **Estimated impact:** cut stale-review wall time by **15–35 minutes per superseded PR event**. **Confidence:** high.

- **The stable-release smoke suite is failing for orchestration plumbing, not just product logic.** `test_and_mark_stable` has **3 failures in 4 runs** and **p50 3410.5s**. Failures include a wrong-source dispatch guard (`run 25542716411`, dispatched from `main` instead of `stable`) and orchestrator tracking-issue discovery failure after dispatch (`run 25542750558`, `orchestrate-decompose-test`). **Estimated impact:** avoid **30–60 minute** reruns of the release smoke workflow. **Confidence:** high.

- **Failed implement runs are burning meaningful token budget on “no actionable output” loops.** Evidence-grade diagnostics in `workflow_log_analysis` `run 25505931104` show failed implement `run 25496323404` used **11,954 + 10,696 tokens** across two attempts, and `run 25496338569` used **11,805 + 12,152 tokens**, before bailing with “2 consecutive attempts with no actionable output.” **Estimated impact:** save **~22k–24k tokens per doomed implement run**. **Confidence:** high.

- **CI is consistently slow but stable; it is the main predictable compute bottleneck.** `ci` runs are tightly clustered at **p50 620s / p95 658.9s** across **68 successful runs**. Recent run `25547336554` spent roughly **09:11:18–09:21:34** inside `lint`, which dominated the workflow. **Estimated impact:** **3–5 minutes** wall-clock reduction with safe sharding/parallelization. **Confidence:** high.

- **AI memory is present and operational, but reviewer retrieval is weak.** Across deep-dive logs I found **51 structured `AI_MEMORY_TELEMETRY` JSON records**. `retrieve` telemetry appeared **11 times**, but only **27.3%** returned any records; all reviewer retrieves in sampled slow `review_autofix` runs returned **0 records** with `keyword_method: "none"`, while implement retrieves returned **2 records** with `keyword_method: "plain"` and `estimated_tokens: 56`. **Estimated impact:** moderate quality/cost improvement if reviewer retrieval is fixed. **Confidence:** high.

- **Prompt cache is enabled broadly, but current telemetry cannot prove it is paying off.** `OPENROUTER_PROMPT_CACHE_DISABLED: false` appears in sampled `implement`, `review_autofix`, and `orchestrate_poll` runs, but there are **no direct cache hit/miss/read/create metrics** in the deep-dive logs. **Estimated impact:** likely **10–25% prompt-token reduction** if cache prefix stability is improved and hit/miss telemetry is emitted. **Confidence:** medium-low.

---

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1) Cancel superseded `review_autofix` runs earlier, before Codex work starts
**Critical-path win**

- **Evidence**
  - `review_autofix` family: **106 total**, **68 cancelled**, **p95 1696.5s**.
  - Cancelled run `25546727171` lasted **2262s**.
  - Cancelled run `25547336679` lasted **1274s**; its `codex-agent (claude-branch-review)` step dominated from **09:11:52** to **09:32:24**.
  - Both cancelled samples were comment-only `claude-branch-review` paths rather than full editor/apply/judge flows.

- **Root cause**
  - Superseded review runs continue long after they are no longer the latest useful evaluation.
  - Cancellation is happening too late in the lifecycle, after runner acquisition and substantial Codex/reviewer work.

- **Exact change**
  - Add a `concurrency` group keyed by repository + PR number or head ref for `review_autofix`, with `cancel-in-progress: true`.
  - Move the `claude-branch-review` / comment-only decision to the earliest possible gate, before Codex install and before multi-model reviewer fan-out.
  - If the path is comment-only, skip the full `codex-agent` path entirely.

- **Estimated time savings**
  - **15–35 minutes per superseded run** in the worst review paths.
  - Material reduction in queue pressure for later runs on the same PR.

- **Implementation risk**
  - **Low–medium.**
  - Main risk is dropping a comment from an older run; mitigate by ensuring only the newest run is allowed to publish final review output.

---

### 2) Remove second-runner queueing from the slow `review_autofix` path
**Critical-path win**

- **Evidence**
  - In slow successful run `25543091277`, `review / gate` evaluated true around **07:29:48**, but `review / codex-agent` did not get a hosted runner until **07:48:09**.
  - That is roughly **18 minutes 20 seconds** of queue delay between gate completion and actual AI work starting.
  - The same workflow family is the largest source of long cancellations and long tails.

- **Root cause**
  - Multi-job structure requires a fresh hosted runner for `codex-agent` even when the gate result could have been evaluated inside the same job.
  - Queueing, not compute, is a significant part of elapsed time on this path.

- **Exact change**
  - For non-matrix review paths, fold lightweight gate logic into the job that may run Codex.
  - Alternatively, keep the current gate job only for clearly skippable paths, but run the expensive AI path in the same job context whenever possible.
  - Prioritize same-runner execution for `claude-branch-review` / comment-only flows.

- **Estimated time savings**
  - Up to **~18 minutes** on congested periods for affected runs.
  - Lower p95 for `review_autofix`, even when compute time remains unchanged.

- **Implementation risk**
  - **Medium.**
  - Requires workflow restructuring, but it is backward-compatible if outputs and summaries stay identical.

---

### 3) Replace repo-wide post-dispatch searches with direct orchestrator outputs in `test_and_mark_stable`
**Critical-path win**

- **Evidence**
  - Failed run `25542750558` (`Test & Mark Stable Release`, **3674s**) failed in `orchestrate-decompose-test`.
  - In `step-007-orchestrate-decompose-test.log`, the workflow:
    - dispatches `internal-orchestrate.yml`,
    - then searches the repo for a tracking issue via `issues?state=open&labels=ai:orchestrator-tracking&since=...`,
    - then errors: **“Could not locate tracking issue for run 25542750558 …”**
  - The same run logged **113 `gh api` calls**, **18 GraphQL mentions**, and **7 `gh workflow run` invocations**.

- **Root cause**
  - The smoke test relies on eventual-consistency repository searches (`since`, labels, body contains run ID) instead of consuming deterministic outputs from the dispatch target.
  - GitHub indexing lag can make the test fail even if the orchestrator is progressing correctly.

- **Exact change**
  - Have the dispatched orchestrator workflow emit the tracking issue number or artifact explicitly.
  - Poll the dispatched workflow run directly, then read that output/artifact instead of searching all issues by label/time window.
  - Keep the repository search only as fallback.

- **Estimated time savings**
  - Prevents **30–60 minute** stable-suite reruns caused by false-negative orchestration detection.
  - Also reduces API polling overhead.

- **Implementation risk**
  - **Medium.**
  - Needs a contract between the smoke test and the dispatched orchestrator workflow, but it is safe and internal.

---

### 4) Split the `ci` “lint” bundle into parallel jobs
**Critical-path win**

- **Evidence**
  - `ci` family: **68/68 success**, **p50 620s**, **p95 658.9s**.
  - Recent `ci` run `25547336554` lasted **623s**.
  - Its `lint` step dominated from **09:11:18** to **09:21:34**.
  - Similar CI runs in the sample cluster in the **605–645s** range, so this is a systemic compute bottleneck, not an outlier.

- **Root cause**
  - Many unit-test groups and static checks are serialized into one long job.
  - The current design is stable, but not latency-optimized.

- **Exact change**
  - Split `lint` into 2–4 parallel jobs:
    - static lint / py_compile / script reference checks,
    - orchestrator library unit tests,
    - workflow and validation unit suites,
    - smoke/contract suites.
  - Keep one final “required” aggregator job so branch protection remains simple.

- **Estimated time savings**
  - **3–5 minutes** off CI wall clock, depending on runner concurrency.
  - Faster developer feedback on every PR.

- **Implementation risk**
  - **Medium.**
  - Low behavioral risk, but some setup duplication and required-check naming changes must be handled carefully.

---

### 5) Remove avoidable helper-fetch fallbacks from `issue_pr_status`
**Micro-optimization**

- **Evidence**
  - Recent `issue_pr_status` run `25548214057` took **46s**.
  - The main sync step spanned **09:32:02.7Z–09:32:35.8Z**.
  - Warnings in the run:
    - `Support checkout ref ... is unavailable; using main.`
    - `Could not fetch tg_helpers.sh; skipping TG cleanup.`
    - `tg_helpers.sh is empty; skipping TG cleanup.`

- **Root cause**
  - Every run attempts helper resolution/fallback even when Telegram cleanup may not do useful work.
  - This also creates noisy warnings that obscure real problems.

- **Exact change**
  - Skip helper fetch entirely when no tracked Telegram messages exist.
  - Vendor the minimal Telegram cleanup helper with the workflow if it is always required.
  - Fail soft only once, not through multiple fallback attempts.

- **Estimated time savings**
  - Likely **small per run**; more valuable for noise reduction than raw latency.

- **Implementation risk**
  - **Low.**

---

## Cost Optimizations

Ranked by expected token/dollar savings.

### 1) Stop second-attempt implement retries when the first attempt shows “stuck in exploration”
- **Evidence**
  - Evidence-grade analysis in `workflow_log_analysis` run `25505931104` summarized:
    - failed implement `25496323404`: **Attempt 1 = 11,954 tokens**, **Attempt 2 = 10,696 tokens**.
    - failed implement `25496338569`: **Attempt 1 = 11,805 tokens**, **Attempt 2 = 12,152 tokens**.
  - Deep-dive logs for both failed implement runs show:
    - `Codex produced no actionable output 2 attempts in a row`
    - `Codex bailed ... agent loop stuck in exploration`

- **Root cause**
  - Retry policy is too willing to spend another full model attempt after the first attempt already demonstrated zero actionable output / no-file-write behavior.

- **Exact change**
  - If attempt 1 ends with:
    - no staged file changes,
    - no structured plan advancement,
    - and the “announced-edit-without-changes” or empty-output pattern,
    route immediately to diagnose/clarify instead of spending a second full implement attempt.
  - Keep the second attempt only when the first attempt made partial but incomplete progress.

- **Estimated savings**
  - **~22k–24k tokens per doomed implement run**, based on the two sampled failures.
  - In the observed sample, that is roughly **46k tokens** of avoidable retry spend.

- **Quality-risk notes**
  - **Low** if the heuristic requires both “no changes” and “no actionable output.”
  - Avoid applying this to runs where Codex produced partial edits or concrete diagnostics.

---

### 2) Do not fan out to the full reviewer model panel on comment-only or deterministic-skip paths
- **Evidence**
  - Review runs expose a six-model reviewer panel:
    - `minimax/minimax-m2.5`
    - `moonshotai/kimi-k2.5`
    - `deepseek/deepseek-v4-pro`
    - `z-ai/glm-5`
    - `qwen/qwen3.6-plus`
    - `x-ai/grok-4.1-fast`
  - Recent and slow `review_autofix` runs show these model lists even on paths that are not full autofix flows.
  - Cancelled `review_autofix` run `25547336679` was explicitly:
    - `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... comment-only path; editor/commit/judge/auto-merge skipped.`
  - Successful deterministic-skip runs in the broader sample include:
    - `run 25544799412`: `small_diff=true`, `skip=true`
    - `run 25544715134`: `docs_only`, `skip=true`

- **Root cause**
  - Expensive reviewer breadth is being configured on paths where the system already knows it will not apply edits or may skip substantive review work.

- **Exact change**
  - On `claude-branch-review` comment-only path:
    - skip the full reviewer panel,
    - run at most one lightweight summarizer or a single canonical reviewer.
  - On deterministic-skip (`small_diff`, `docs_only`) paths:
    - avoid panel execution entirely and jump straight to summary/comment generation.

- **Estimated savings**
  - Likely the **largest recurring AI-token saving** in `review_autofix`.
  - I cannot quantify exact tokens from current telemetry, but the structural saving should be substantial on these paths.

- **Quality-risk notes**
  - **Medium.**
  - Safe on comment-only and deterministic-skip paths, but do not reduce reviewer diversity on full-change, merge-risk, or judge-required paths.

---

### 3) Stabilize prompt-cache prefixes and stop regenerating highly dynamic prompt wrappers
- **Evidence**
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` is present in sampled `implement`, `review_autofix`, and `orchestrate_poll` runs, so cache is enabled.
  - However, the deep-dive logs do **not** emit direct cache hit/miss/read/create counts.
  - Slow AI runs repeatedly create runtime-specific prompt files such as:
    - `reviewer_prompt_body.txt`
    - `editor_prompt_body.txt`
    - `reviewer_prompt.txt`
    - `editor_prompt.txt`
    - runtime directories containing run IDs and temp paths.

- **Root cause**
  - Prompt wrappers appear to vary per run in ways that likely fragment cacheable prefixes.
  - Dynamic noise such as run IDs, branch names, temp paths, and issue-specific boilerplate can reduce cache reuse.

- **Exact change**
  - Move static system + mode instructions into a canonical prefix that is byte-stable.
  - Push dynamic identifiers, timestamps, temp paths, and branch/run metadata to the prompt suffix or tool inputs.
  - Canonicalize ordering of changed files, labels, and metadata blocks.

- **Estimated savings**
  - **Inference:** likely **10–25% prompt-token reduction** on repetitive AI workflows once cacheable prefix stability improves.

- **Quality-risk notes**
  - **Low.**
  - This changes prompt packaging, not task intent.

---

### 4) Avoid spending AI setup cost on runs that are guaranteed to fail due to support-ref staging issues
- **Evidence**
  - Failed implement runs `25496323404` and `25496338569` both logged:
    - `Failed to checkout workflow support source from ${SCRIPT_REF} or main`
    - `Missing required support script ...`
    - `Failed to stage required file prompts/mode-implement-diagnose.txt`
    - `Failed to stage required file prompts/mode-implement-repair.txt`
  - Yet the same runs also progressed far enough to record memory events and enter model-configured logic.

- **Root cause**
  - Preflight validation of the support source is happening too late.
  - Some setup/model overhead is incurred before the workflow conclusively knows it cannot execute correctly.

- **Exact change**
  - Add an early “support source preflight” step that validates the support ref and all required files before any AI/runtime setup.
  - If preflight fails, fail immediately with a configuration error and do not enter Codex or memory retrieval steps.

- **Estimated savings**
  - Small on a per-run basis, but prevents wasting AI-adjacent setup on structurally broken runs.

- **Quality-risk notes**
  - **Low.**
  - This only short-circuits already-doomed runs.

---

## Reliability Improvements

Ranked by expected reduction in failure rate or rerun rate.

### 1) Make support-source staging deterministic for `implement`
- **Failure evidence**
  - Failed implement `run 25496323404`:
    - `Failed to checkout workflow support source from ${SCRIPT_REF} or main`
    - `Missing required support script ${f}`
    - missing staged prompt files.
  - Failed implement `run 25496338569` shows the same pattern.
  - These are the only two failed `implement` runs in the sampled window; `implement` family failure rate is **2/154 = 1.3%**.

- **Root cause category**
  - Configuration / support-ref staging failure.

- **Exact fix**
  - Resolve the support source ref once in the parent workflow and pass a validated ref or artifact to `implement`.
  - Verify the required helper and prompt file manifest before runner-heavy work starts.
  - If fallback to `main` is used, assert all required files exist before proceeding.

- **Expected reliability impact**
  - Should eliminate the observed `implement` failures in this window.
  - Also reduces confusing secondary failures downstream.

- **Rollback / fail-open considerations**
  - Keep the existing fallback-to-`main` behavior behind a flag while rolling out stricter validation.
  - Fail early with a configuration error rather than partial execution.

---

### 2) Replace tracking-issue discovery by label/time-window search in orchestrator tests
- **Failure evidence**
  - `test_and_mark_stable` `run 25542750558` failed after `orchestrate-decompose-test` could not locate the orchestrator tracking issue:
    - `Could not locate tracking issue for run 25542750558 ...`
  - The step relied on:
    - dispatching the workflow,
    - then searching `ai:orchestrator-tracking` issues since a timestamp,
    - then searching managed child issues by label.

- **Root cause category**
  - Eventual-consistency / lookup design flaw.

- **Exact fix**
  - Emit the tracking issue number directly from the orchestrator workflow.
  - Store it as workflow output, artifact, or a machine-readable comment and read that directly.
  - Use repository-wide label searches only as a fallback diagnostic path.

- **Expected reliability impact**
  - High for stable-release smoke workflows.
  - Converts a flaky timing-sensitive test into a deterministic integration contract.

- **Rollback / fail-open considerations**
  - Preserve the current search path as backup while validating the direct-output path.

---

### 3) Tighten dispatch-source hygiene for `test_and_mark_stable`
- **Failure evidence**
  - Failed `run 25542716411` died in `source / Validate dispatch ref` after only **18s**:
    - `test-and-mark-stable.yml must be dispatched from the 'stable' branch (got 'main')`
    - recommended to run `promote-main-to-stable.yml` instead.

- **Root cause category**
  - Invocation / operator error.

- **Exact fix**
  - Hide or discourage direct `main` dispatches for `test-and-mark-stable`.
  - Route operator entry points through `promote-main-to-stable.yml` or a wrapper that auto-normalizes the source branch.
  - Add a `workflow_dispatch` input validator that fails in the UI path before expensive jobs begin.

- **Expected reliability impact**
  - Prevents one class of immediate release-smoke failures entirely.

- **Rollback / fail-open considerations**
  - No fail-open needed; this should be a hard guard.

---

### 4) Surface failing nightly self-test fixtures immediately
- **Failure evidence**
  - `nightly_validation_selftest` `run 25534774492`:
    - `fixtures=3 passed=1 failed=2`
    - `Process completed with exit code 1`
  - Family stats: **1 run, 1 failure** in the current window.

- **Root cause category**
  - Test failure visibility / triage friction.

- **Exact fix**
  - Always print the failing fixture names and stage statuses in the job summary and annotate the run.
  - Optionally open/update a single tracking issue with the artifact summary path.

- **Expected reliability impact**
  - Faster diagnosis and shorter mean time to repair for nightly regressions.

- **Rollback / fail-open considerations**
  - Purely additive; no behavioral risk.

---

### 5) Add concurrency-based dedupe for `review_autofix`
- **Failure evidence**
  - `review_autofix` has **68 cancelled runs out of 106 total**.
  - Multiple cancelled runs still consumed 10–37 minutes before cancellation.

- **Root cause category**
  - Operational churn / superseded-run waste.

- **Exact fix**
  - Concurrency by PR/head ref with cancel-in-progress.
  - Publish review output only from the latest active run.

- **Expected reliability impact**
  - Fewer stale comments, fewer abandoned branch-review paths, less state drift from overlapping runs.

- **Rollback / fail-open considerations**
  - If needed, disable concurrency quickly without changing any workflow contract.

---

## AI Memory Health

I found **structured AI memory telemetry** in the deep-dive logs, so this section is based on emitted evidence rather than inference.

### Structured telemetry coverage
- **Structured JSON telemetry records found:** **51**
- **Non-JSON `AI_MEMORY_TELEMETRY:` lines found:** **55**
  - These were mostly embedded summaries or raw orchestrator state payloads, not operation telemetry.

### Operation distribution
- `record-run-event`: **23**
- `retrieve`: **11**
- `summarize_unselected_runs`: **8**
- `record-candidate`: **3**
- `processed-command-check`: **2**
- `processed-command-claim`: **2**
- `finalize-task`: **2**

### Retrieval effectiveness
- **Retrieve count:** **11**
- **Retrieve hit rate:** **27.3%** (`records_selected > 0` in 3 of 11 retrieves)
- **Average `estimated_tokens`:** **15.3**
- **Average budget tokens:** **0.0** in the emitted JSON samples
- **`keyword_method` distribution:**
  - `plain`: **4**
  - `none`: **7**
  - `llm`: **0**

### What is working
- Implement retrieval worked in both failed implement runs:
  - `run 25496323404`: `retrieve` returned **2 records**, `estimated_tokens: 56`, `keyword_method: "plain"`.
  - `run 25496338569`: same pattern, **2 records**, `estimated_tokens: 56`, `keyword_method: "plain"`.
- Memory writes look healthy in the sampled logs:
  - No JSON telemetry entries showed `push_attempts > 1`.
  - No JSON telemetry entries showed `enabled: false`.
  - No JSON telemetry entries showed `fail_open: true`.

### What is not working well
- Reviewer retrieval in sampled slow `review_autofix` runs was ineffective:
  - `run 25543091277`: reviewer `retrieve` returned **0 records**, `estimated_tokens: 0`, `keyword_method: "none"`.
  - `run 25544013259`: same.
  - `run 25546727171`: same, including its explicit “Retrieve reviewer memory context fail-open” step name.
- In other words, the current memory system is helping `implement` more than `review_autofix`.

### Maintenance health
- Evidence-grade summary for `memory_maintenance` `run 25545199697` shows:
  - `op: "compact"`
  - `archived_candidates: 2914`
  - `did_push: true`
  - `push_attempts: 1`
- That suggests compaction is functioning and not retry-heavy.

### Recommendations
1. **Raise reviewer retrieval quality first.**
   - Reviewer retrieves are mostly returning nothing.
   - Seed reviewer memory with PR/branch fingerprints and prior review outcomes, not just issue-centric keywords.

2. **Emit `budget_tokens` and retrieval query diagnostics consistently.**
   - Current retrieve events expose `estimated_tokens` but not a meaningful budget in sampled JSON.
   - Add `budget_tokens`, `query_terms`, and whether retrieval was skipped for lack of signal.

3. **Separate structured telemetry from raw orchestrator state dumps.**
   - Some `clarify`/`plan`/`respond` runs log raw `ORCHESTRATOR_STATE_V1` content under the `AI_MEMORY_TELEMETRY` prefix.
   - That makes parsing noisier and reduces observability quality.

4. **Track two health KPIs continuously.**
   - Reviewer retrieve hit rate
   - Percentage of implement/review runs with `records_selected = 0`

---

## GH API Call Audit

### Overall observation
The biggest API consumers in the sampled deep-dive runs are **orchestration-heavy smoke tests** and **review/autofix** flows. I saw **many rate-limit handling wrappers**, but I did **not** see actual HTTP 429 or secondary-rate-limit incidents in the sampled deep-dive/recent runs. The current risk is therefore **API inefficiency**, not current rate-limit failure.

### Highest-volume API patterns

#### 1) `test_and_mark_stable` uses repeated polling and repo-wide searches
- **Evidence**
  - Failed `run 25542750558`: **113 `gh api`**, **18 GraphQL**, **7 `gh workflow run`**.
  - Successful slow `run 25505874733`: **117 `gh api`**, **18 GraphQL**, **8 `gh workflow run`**.
  - Failed `run 25496132733`: **63 `gh api`**.
  - `step-007-orchestrate-decompose-test.log` shows:
    - dispatch,
    - repeated workflow-run lookup,
    - issue search by labels/time window,
    - child issue search by label and back-reference.

- **Pattern**
  - Unbatched per-phase polling.
  - Repeated list queries where a single known run ID or output could be used.

- **Recommendation**
  - After dispatch, store and reuse the dispatched workflow run ID.
  - Poll that one run instead of listing workflow runs repeatedly.
  - Have the orchestrator emit tracking issue ID / child issue IDs so the test does not need repo-wide issue searches.

- **Estimated call reduction**
  - **Inference:** **40–70%** reduction in API calls for the smoke suite’s orchestration stages.

- **Rate-limit risk reduction**
  - Moderate. Fewer list and search calls means less exposure to eventual consistency and lower aggregate API pressure.

---

#### 2) `review_autofix` repeats PR / linked-issue discovery work across slow runs
- **Evidence**
  - Slow `run 25543091277`: **26 `gh api`**, **23 GraphQL**.
  - Slow `run 25544013259`: **21 `gh api`**, **39 GraphQL**.
  - Cancelled `run 25546727171`: **36 `gh api`**, **52 GraphQL**.
  - Recent fast post-merge runs `25548214088` and `25548295239` still use:
    - `gh api graphql` to query linked issues,
    - `gh workflow run` for validation dispatch,
    - `gh issue edit --remove-label`.

- **Pattern**
  - Repeated lookups of PR-linked issues and per-issue label handling.
  - GraphQL and workflow-dispatch logic is re-entered even on paths that frequently do nothing.

- **Recommendation**
  - Fetch PR-linked issue metadata once and pass it between jobs as outputs or a small artifact.
  - Short-circuit the entire post-merge validation step when there are no linked issues or no `ai:orchestrator-validate-required` labels.
  - On comment-only branch-review paths, skip any linked-issue discovery that is only relevant for merged PRs.

- **Estimated call reduction**
  - **Inference:** **20–40%** on slow `review_autofix` paths.

- **Rate-limit risk reduction**
  - Low–moderate, but it will reduce noisy API traffic and queue time.

---

#### 3) `cancel_on_pr_close` is low volume, but still does repeated list + cancel logic
- **Evidence**
  - Recent runs `25548214041` and `25548295190` show `_gh_retry gh api` calls to list runs and optionally POST cancellation.
  - In the sampled fast runs there were no matching runs, so these finished in **7–15s**.

- **Pattern**
  - Reasonable current hygiene; not a hotspot.

- **Recommendation**
  - Leave as-is unless API pressure becomes visible elsewhere.
  - Minor improvement: avoid extra list calls when the PR head ref is already known to have no active runs from prior event state.

- **Estimated call reduction**
  - Minimal.

---

#### 4) `issue_pr_status` helper fetches are noisy but not major GH API hotspots
- **Evidence**
  - Recent run `25548214057` logged helper fallback warnings but no major API-heavy pattern in the visible steps.
  - The step did perform support-ref resolution and helper fetch attempts.

- **Pattern**
  - More git/helper resolution churn than GH API overload.

- **Recommendation**
  - Focus optimization effort elsewhere first.

---

### Missed batching / reuse opportunities
1. **Workflow-run polling should reuse a known run ID instead of listing runs repeatedly.**
2. **Linked issue labels should be fetched in one shot and reused across subsequent steps/jobs.**
3. **Smoke-test issue state/comments/labels should be fetched once and cached for the step scope instead of refetching after every phase.**
4. **Use existing actions-runs cache infrastructure more aggressively in orchestration tests.**
   - CI includes tests for `actions_runs_cache` behavior, so the codebase already has primitives for safer reuse.

### Actual rate-limit events
- In the sampled deep-dive and recent runs, I saw **rate-limit handling code** and retry wrappers, but **no confirmed HTTP 429 or GitHub secondary-rate-limit events**.
- That means the system is prudent, but still making more calls than necessary.

---

## Prompt Cache & Memory System

### Prompt cache behavior
- **Observed state:** prompt cache is generally **enabled**.
  - `OPENROUTER_PROMPT_CACHE_DISABLED: false` appears in:
    - failed `implement` runs `25496323404`, `25496338569`
    - slow `review_autofix` runs `25543091277`, `25546727171`
    - recent `review_autofix` runs `25548214088`, `25548295239`
    - recent `orchestrate_poll` run `25547655965`

- **Gap**
  - The current telemetry does **not** expose cache creation/read/hit/miss counts in the deep-dive logs.
  - So I can confirm cache is on, but I cannot quantify whether it is effective.

### Memory retrieval effectiveness
- Memory retrieval is **good for `implement`** and **poor for `review_autofix`**.
- This asymmetry suggests the retrieval keys or stored records are more aligned with implementation tasks than reviewer tasks.

### Likely cache-fragmentation causes
These are inferences from workflow behavior and prompt assembly patterns, not direct cache-hit telemetry:

1. **Dynamic run metadata in prompt wrappers**
   - Runtime directories, run IDs, issue IDs, PR numbers, and branch names appear heavily in prompt-file setup.

2. **Repeated prompt regeneration**
   - Slow review runs generate multiple prompt-body and prompt-wrapper files in temp directories.

3. **High prompt variance in reviewer flows**
   - Multi-model review panel plus per-PR/per-branch context likely reduces shared prefix stability.

### Concrete improvements
1. **Canonicalize the static prefix**
   - Stable order for system instructions, mode prompts, repo instructions, and safety text.

2. **Move dynamic metadata to the suffix**
   - Run ID, PR number, temp file paths, branch names, timestamps, and derived labels should be appended after the shared prefix.

3. **Canonicalize changed-file ordering**
   - Stable sort order for file lists and metadata blocks improves cache reuse.

4. **Emit prompt-cache telemetry**
   - At minimum:
     - cache create count
     - cache read count
     - hit/miss ratio
     - fail-open count

5. **Align reviewer memory with reviewer tasks**
   - Store and retrieve successful prior review summaries, common comment patterns, and deterministic-skip heuristics by PR shape / branch-review mode.

### Estimated impact
- **Tokens:** likely **10–25%** prompt-token reduction on repetitive AI flows, but this remains an inference until hit/miss telemetry is emitted.
- **Latency:** low-to-moderate improvement from lower prompt upload and less model cold-start context.
- **Reliability:** better observability and fewer “memory present but useless” reviewer runs.

---

## Orchestrator Health

### What looks healthy
- The orchestrator poller itself is not obviously failing:
  - `orchestrate_poll` family: **36 successful runs**, **p50 55.5s**, **p95 131.75s**.
  - Recent run `25547655965` succeeded in **113s**.

- Memory maintenance appears healthy:
  - `memory_maintenance` runs succeeded.
  - Evidence-grade summary for `run 25545199697` shows successful compaction and push.

### Recurring pain points

#### 1) Massive skipped-run fan-out across clarify/plan/respond/implement wrappers
- **Evidence**
  - `clarify`: **176 total**, **16 success**, **160 other**
  - `plan`: **154 total**, **13 success**, **141 other**
  - `implement`: **154 total**, **13 success**, **134 other**, **2 fail**, **5 cancelled**
  - `orchestrate_clarify_respond`: **154 total**, **3 success**, **151 other**
- The “other” runs in the recent sample are mostly condition-evaluated false within **0–2s**.

- **Interpretation**
  - The orchestrator is creating a lot of benign no-op workflow invocations.
  - This is not a correctness failure, but it creates operational noise and makes real regressions harder to spot.

- **Smallest safe mitigation**
  - Introduce a lightweight front-door router that parses the event once and dispatches only the relevant reusable workflow.
  - If architecture changes are too heavy, at least add better summary tagging so skipped no-op runs are excluded from failure dashboards.

- **Indicator to track**
  - **Skip ratio by family** (`other_count / total_runs`) for `clarify`, `plan`, `implement`, and `respond`.

---

#### 2) Tracking-issue discovery is still timing-sensitive
- **Evidence**
  - `run 25542750558` failed because the smoke harness could not locate the tracking issue after orchestrator dispatch.

- **Interpretation**
  - The orchestrator’s observable outputs are still too indirect for robust automation.

- **Smallest safe mitigation**
  - Add a deterministic tracking-issue output contract.

- **Indicator to track**
  - Time from orchestrator dispatch to tracking-issue discoverability.

---

#### 3) Some implement flows enter long “ai:implementing” states before failing
- **Evidence**
  - In failed alt-model smoke `run 25496132733`, `e2e-alt-model-test` observed labels progressing to `ai:implementing` and then remaining there for a long stretch, eventually failing with:
    - `Alt-model run timed out before reaching review stage`
  - The overall stable-suite run lasted **4599s**.

- **Interpretation**
  - At least some implement failures surface too late in the state machine.

- **Smallest safe mitigation**
  - Add an implement-phase SLA timer that promotes to diagnose/fail-fast once the workflow remains `ai:implementing` beyond a threshold without run-status progress.

- **Indicator to track**
  - Percentage of implement runs spending **>20 minutes** in `ai:implementing`.

---

#### 4) Conflict-heal / integration-judge paths are an area of ongoing strain
- **Evidence**
  - CI run `25547336554` includes passing tests specifically around:
    - GraphQL → REST fallbacks,
    - validation redispatches,
    - conflict sweeps,
    - integration fingerprint verification.
  - The same run also logs test-side error strings such as:
    - `Refusing to create [ai-merge-resolve] commit...`
    - `Integration fingerprint verification FAILED...`

- **Interpretation**
  - This is evidence of active hardening rather than a live production incident in the sampled runs, but it highlights where operational complexity currently concentrates.

- **Smallest safe mitigation**
  - Continue to bias conflict-heal logic toward deterministic verification and single-dispatch dedupe.
  - Add production counters mirroring the tested failure modes.

- **Indicator to track**
  - Rate of integration-judge invocations per 100 orchestrator polls.
  - Rate of fingerprint-verification failures in production runs.

---

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

### 1) Review/autofix: queueing + superseded work
- **Queue overhead**
  - Slow run `25543091277` waited roughly **18m 20s** between gate success and `codex-agent` runner acquisition.
- **Compute overhead**
  - Slow/cancelled review runs spend **15–37 minutes** in `codex-agent` or `claude-branch-review`.
- **Retry/merge overhead**
  - Less visible than queueing in the sample; the main issue is stale work, not retried work.

**Fix order**
1. Add concurrency cancellation.
2. Collapse queue-sensitive jobs.
3. Skip full panel on comment-only/deterministic paths.

---

### 2) Stable-release smoke: polling and orchestration lookup fragility
- **Queue overhead**
  - Not the main issue.
- **Compute overhead**
  - `test_and_mark_stable` itself is long: **p50 3410.5s**, **p95 4460.25s**.
- **Retry overhead**
  - Multiple dispatch/poll/list loops across clarify/plan/implement/review and orchestrate tests.
- **Merge/conflict overhead**
  - Not dominant in the sampled failures.

**Fix order**
1. Use direct outputs for orchestrator tracking issue/child discovery.
2. Reuse known workflow run IDs instead of listing/polling broadly.
3. Tighten source-branch invocation path.

---

### 3) CI: long but stable serialized compute
- **Queue overhead**
  - Present, but modest.
- **Compute overhead**
  - Dominant; `lint` consumes ~10 minutes consistently.
- **Retry overhead**
  - Minimal in the sample.
- **Merge/conflict overhead**
  - Mostly represented as test coverage rather than runtime failures.

**Fix order**
1. Parallelize the `lint` bundle.
2. Keep one aggregator required check.
3. Revisit only after review/autofix and smoke-suite issues are fixed.

---

### 4) Implement: failure loops waste both time and tokens
- **Queue overhead**
  - Minor.
- **Compute overhead**
  - AI attempts are expensive relative to the short workflow.
- **Retry overhead**
  - Two full token-heavy attempts were spent on runs that ended in “no actionable output.”
- **Merge/conflict overhead**
  - Not the driver in the sampled failures.

**Fix order**
1. Fail before AI work on support-ref staging errors.
2. Short-circuit second attempt when attempt 1 is clearly stuck.
3. Escalate to diagnose/clarify sooner.

---

### 5) Status-sync / post-merge dispatch: mostly small cleanup inefficiencies
- **Queue overhead**
  - Low.
- **Compute overhead**
  - Small.
- **Retry overhead**
  - Minimal.
- **Merge/conflict overhead**
  - Minimal.

**Fix order**
1. Remove noisy helper fallbacks.
2. Keep current logic otherwise.

---

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-tail queueing and cancellation churn:
  - **106 total runs**, **68 cancelled**, **p95 1696.5s**
  - cancelled examples: `25546727171` (**2262s**), `25547336679` (**1274s**)
- `ci` serialized compute:
  - **68 total runs**, **p50 620s**, **p95 658.9s**
- `test_and_mark_stable` orchestration-heavy smoke latency:
  - **4 total runs**, **3 failures**, **p50 3410.5s**

**Top failure modes**
- Support-ref / helper staging failure in `implement`:
  - `25496323404`, `25496338569`
- Wrong dispatch source branch in `test_and_mark_stable`:
  - `25542716411`
- Orchestrator tracking-issue lookup failure:
  - `25542750558`
- Nightly self-test fixture failures:
  - `25534774492`

**Highest-cost drivers**
- Token-heavy failed implement retries:
  - `25496323404`: **22,650 tokens** across two attempts
  - `25496338569`: **23,957 tokens** across two attempts
- Multi-model reviewer panel on long or comment-only review paths
- Long-running stale `review_autofix` runs that are eventually cancelled

**Top 3 prioritized actions**
1. **Add concurrency cancellation + early comment-only short-circuit for `review_autofix`.**
2. **Make orchestrator smoke tests consume direct outputs instead of label/time-window issue searches.**
3. **Short-circuit doomed implement retries after first “no actionable output” attempt and preflight support refs earlier.**

---

## Metrics Appendix

### Overall repository metrics

| Repository | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 948 | 276 | 6 | 73 | 593 | 0.63% | 134.8 | 2.0 | 645.0 |

### Key workflow-family metrics

| Workflow family | Total | Success | Failure | Cancelled | Other | Avg s | p50 s | p95 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| review_autofix | 106 | 34 | 0 | 68 | 4 | 433.9 | 61.0 | 1696.5 |
| ci | 68 | 68 | 0 | 0 | 0 | 620.1 | 620.0 | 658.9 |
| test_and_mark_stable | 4 | 1 | 3 | 0 | 0 | 2859.5 | 3410.5 | 4460.3 |
| implement | 154 | 13 | 2 | 5 | 134 | 24.2 | 1.0 | 205.0 |
| clarify | 176 | 16 | 0 | 0 | 160 | 10.2 | 1.0 | 92.0 |
| plan | 154 | 13 | 0 | 0 | 141 | 11.0 | 1.0 | 129.4 |
| orchestrate_clarify_respond | 154 | 3 | 0 | 0 | 151 | 1.7 | 1.0 | 3.35 |
| orchestrate_poll | 36 | 36 | 0 | 0 | 0 | 75.8 | 55.5 | 131.8 |
| issue_pr_status | 18 | 18 | 0 | 0 | 0 | 38.8 | 17.5 | 89.0 |
| workflow_log_analysis | 3 | 3 | 0 | 0 | 0 | 2593.0 | 2621.0 | 3156.5 |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 0 | 92.0 | 92.0 | 92.0 |

### Selected run hotspots

| Run ID | Family | Conclusion | Duration s | Key evidence |
|---|---|---|---:|---|
| 25546727171 | review_autofix | cancelled | 2262 | `claude-branch-review` path; cancelled after long AI work |
| 25547336679 | review_autofix | cancelled | 1274 | comment-only branch review still dominated **09:11:52–09:32:24** |
| 25543091277 | review_autofix | success | 2075 | gate succeeded around **07:29:48**, `codex-agent` runner arrived only at **07:48:09** |
| 25542750558 | test_and_mark_stable | failure | 3674 | `orchestrate-decompose-test` could not locate tracking issue after dispatch |
| 25496132733 | test_and_mark_stable | failure | 4599 | alt-model smoke remained in `ai:implementing` until timeout before review |
| 25547336554 | ci | success | 623 | `lint` dominated roughly **09:11:18–09:21:34** |
| 25534774492 | nightly_validation_selftest | failure | 92 | `fixtures=3 passed=1 failed=2` |
| 25548214057 | issue_pr_status | success | 46 | repeated support-ref / `tg_helpers.sh` fallback warnings |
| 25548295239 | review_autofix | success | 61 | post-merge validate-dispatch completed fast; no full AI review path |
| 25548214040 | forward_merge_stable_to_main | success | 19 | push/fetch retries present, but merge still completed quickly |

### Partial token evidence from evidence-grade diagnostics

| Run ID | Workflow family | Attempt 1 tokens | Attempt 2 tokens | Observed outcome |
|---|---|---:|---:|---|
| 25496323404 | implement | 11,954 | 10,696 | `Codex bailed` after 2 no-actionable attempts |
| 25496338569 | implement | 11,805 | 12,152 | `Codex bailed` after 2 no-actionable attempts |

**Note:** comprehensive token totals were **not emitted** for the full run window. The table above reflects only evidence-grade token data surfaced by `workflow_log_analysis`.

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Structured JSON telemetry records | 51 |
| Structured `retrieve` ops | 11 |
| Retrieve hit rate | 27.3% |
| Avg retrieve `estimated_tokens` | 15.3 |
| `keyword_method=plain` | 4 |
| `keyword_method=none` | 7 |
| `keyword_method=llm` | 0 |
| JSON retrieves with `records_selected=0` | 8 |
| JSON retrieves with `fail_open=true` | 0 |
| JSON retrieves with `enabled=false` | 0 |
| JSON events with `push_attempts > 1` | 0 |

### Selected GH API volume from deep-dive / recent runs

| Run ID | Family | `gh api` count | GraphQL mentions | `gh workflow run` mentions | Notes |
|---|---|---:|---:|---:|---|
| 25542750558 | test_and_mark_stable | 113 | 18 | 7 | high-volume orchestration polling/search |
| 25505874733 | test_and_mark_stable | 117 | 18 | 8 | successful but still API-heavy |
| 25543091277 | review_autofix | 26 | 23 | 1 | slow AI review path |
| 25544013259 | review_autofix | 21 | 39 | 0 | slow branch-review path |
| 25546727171 | review_autofix | 36 | 52 | 0 | cancelled after heavy API + AI path |
| 25548295239 | review_autofix | 18 | 44 | 4 | fast post-merge validation dispatch path |
| 25548214088 | review_autofix | 12 | 2 | 4 | fast post-merge validation dispatch path |
| 25548214057 | issue_pr_status | 10 | 10 | 0 | low-volume, more helper churn than API pain |

### Prompt cache visibility

| Observation | Value |
|---|---|
| Cache enabled in sampled AI runs | Yes (`OPENROUTER_PROMPT_CACHE_DISABLED: false`) |
| Direct cache hit/miss metrics in deep-dive logs | Not present |
| Cache effectiveness quantifiable from current window | No |
| Recommended next metric | cache read/create/hit/miss counters per workflow |

If you want, I can turn this into a shorter “engineering action plan” with owners, rollout order, and a 2-week measurement checklist.

## Deep Audit — Workflows & Scripts (2026-05-08)

Inspected all workflow files under `.github/workflows/` and all repository scripts under `scripts/`. I did **not** repeat already-documented report topics that were already covered in the in-progress report (notably the broad `review_autofix` stale-run/concurrency problem and the `test-and-mark-stable` tracking-issue eventual-consistency failure), and focused this audit on additional evidence-backed findings.

### Section 1: Bug & Correctness Sweep

#### BUG-001 — Fail-open PR lookup can dispatch the no-PR review path even when an open PR already exists
- **File path**: `.github/workflows/internal-review.yml:91-103`
- **Severity**: Medium
- **Category tag**: `bug`
- **Description**: The `resolve-claude-branch-pr` step uses raw `gh api` calls with `|| echo ""` / `|| echo 'main'` fallbacks:
  - `existing_pr="$(gh api ... || echo "")"` at lines 98-100
  - `base_ref="$(gh api ... || echo 'main')"` at line 101  
  On any transient GitHub API failure, `existing_pr` becomes empty and line 112 emits `proceed=true`, which dispatches the no-PR `review_autofix.yml` path even though the step’s own comments at lines 68-74 say this lookup is the guard against double-firing when a PR already exists for the branch.
- **Recommended fix**: Fail closed on PR-discovery errors instead of defaulting to “no PR”. Concretely, source `scripts/gh_helpers.sh` and use `gh_retry` + `_safe_gh_jq`, or replace the two raw REST calls with a single retried `gh pr list --head ... --state open --json number,baseRefName` call. If lookup still fails after retries, set `proceed=false` and log a warning rather than dispatching review work.

#### BUG-002 — Stable-release smoke uses a weaker consumer-dispatch path than production
- **File path**: `.github/workflows/test-and-mark-stable.yml:4663-4668`
- **Severity**: Medium
- **Category tag**: `bug`
- **Description**: The release-callback step dispatches `repository_dispatch` to each consumer repo with a bare `gh api "repos/${REPO}/dispatches"` and only logs a warning on failure. The production workflow `mark-stable.yml` uses a rate-limit-aware `_gh_retry` wrapper for the same loop at `.github/workflows/mark-stable.yml:472-491`. That means the smoke path does not exercise the same retry/backoff behavior as production, so transient 403/429/5xx errors can be misclassified as harmless warnings during rehearsal.
- **Recommended fix**: Reuse the production dispatch helper in smoke. The smallest safe change is to extract the `_gh_retry`/dispatch loop from `mark-stable.yml` into a shared helper script and source it from both workflows so smoke and production use identical retry semantics.

_No additional evidence-backed secret-leak or shell-injection findings rose above the reporting threshold in the audited `run:` blocks and sourced shell scripts._

### Section 2: GitHub API Call Redundancy Audit

#### BATCH-001 — Standalone stall sweep burns seven label-list calls every poll cycle before it even starts candidate enrichment
- **File path**: `scripts/orchestrate_poll_process.sh:6310-6335`
- **Severity**: Medium
- **Category tag**: `api-batching`
- **Description**: The standalone stall sweep builds `labeled_issues` by iterating seven labels and calling `gh issue list` once per label at lines 6313-6317 (`ai:clarification`, `ai:planning`, `ai:awaiting-approval`, `ai:implementing`, `ai:done`, `ai:ready-to-merge`, `ai:review-blocked`). It then performs one more GraphQL marker search at lines 6323-6326 and one candidate-details batch call at line 6334. So the baseline path is **9 API calls per poll cycle** before any fallback work: **7 label-list REST calls + 1 marker GraphQL call + 1 candidate-details GraphQL call**.
- **Current call count**: **9** baseline calls per cycle.
- **Proposed call count after fix**: **2** baseline calls per cycle.
- **Existing batching pattern to extend**: Extend `_fetch_standalone_marker_issues_graphql()` in `scripts/orchestrate_poll_process.sh:5727-5767` to issue one aliased GraphQL `search` request for all seven labels plus the two marker searches, then keep the existing `_fetch_candidate_issue_details_graphql()` call.
- **Recommended fix**: Replace the seven `gh issue list` calls with a single aliased GraphQL search helper that returns all label buckets in one response, union that result with the existing marker-search response, and then pass the merged candidate set into `_fetch_candidate_issue_details_graphql()` unchanged.

#### BATCH-002 — `review_autofix` fallback path does N per-issue label lookups after reconstructing linked issues from PR text
- **File path**: `.github/workflows/review_autofix.yml:522-567`
- **Severity**: Medium
- **Category tag**: `api-batching`
- **Description**: When `closingIssuesReferences` is empty, the workflow falls back to parsing linked issue numbers from the PR title/body at lines 523-532. That fallback constructs `issue_nodes_json` with `labels: null`, and the loop at lines 537-567 then executes `gh issue view "${issue_number}" --json labels` at line 541 for every linked issue to discover whether `ai:orchestrator-validate-required` is present. On this fallback path, the pre-dispatch discovery cost becomes **1 PR fetch + N issue-label fetches**.
- **Current call count**: **1 + N** pre-dispatch discovery calls on the fallback path, where **N** is the number of parsed linked issues.
- **Proposed call count after fix**: **2** pre-dispatch discovery calls on the fallback path.
- **Existing batching pattern to extend**: `_fetch_candidate_issue_details_graphql()` in `scripts/orchestrate_poll_process.sh:5807-6037`.
- **Recommended fix**: Keep the single PR fetch that provides the fallback text, then batch-fetch labels for all parsed issue numbers with one aliased GraphQL query that returns `{number, labels}` for the parsed set. That removes the `gh issue view` call from the loop entirely.

#### API-001 — Tracking-issue comment hydration is still linear in tracking-issue count
- **File path**: `scripts/orchestrate_poll_process.sh:6284-6301`
- **Severity**: Medium
- **Category tag**: `api-redundancy`
- **Description**: When `RUNTIME_DIR/tracking_issues.json` is present, the loop fetches comments for each tracking issue individually using `gh_retry gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${t_num}/comments?per_page=100"` at line 6290. The only data consumed from those responses is the recent comment body set used by `extract_latest_valid_orchestrator_state`, which already matches the comment shape returned by `_fetch_candidate_issue_details_graphql()`.
- **Current call count**: **T** REST comment-list calls per poll cycle, where **T** is the number of tracking issues in `tracking_issues.json`.
- **Proposed call count after fix**: **ceil(T / 25)** GraphQL calls per poll cycle.
- **Existing batching pattern to extend**: `_fetch_candidate_issue_details_graphql()` in `scripts/orchestrate_poll_process.sh:5807-6037`.
- **Recommended fix**: Batch the tracking issue numbers into the existing issue-details GraphQL helper and read `.comments` from that cache instead of calling `/issues/{n}/comments` in a loop. Keep the current per-issue REST path only as fail-open fallback when the batch helper returns no entry.

### Section 3: Code Duplication & Modularization Opportunities

#### DUP-001 — `test-and-mark-stable.yml` contains five local copies of the same GitHub polling helper family, and they have already drifted
- **File path**: `.github/workflows/test-and-mark-stable.yml:467-503,592-626,785-825,1232-1254,2386-2397`
- **Severity**: Medium
- **Category tag**: `duplication`
- **Description**: The workflow defines near-identical `gh_api_safe()` helpers three times with the same retry/backoff body (lines 467-481, 592-604, 785-797), plus two more simplified copies (lines 1235-1254 and 2386-2397). The first three copies also duplicate the same `capture_run_id()` helper (lines 488-503, 611-626, 810-825). The copies have already diverged: the earlier versions log non-rate-limit stderr (`cat /tmp/gh_api_err >&2`), while the later simplified copies silently return empty output on non-rate-limit failures.
- **Recommended fix**: Extract a shared module, e.g. `scripts/e2e_gh_watch_helpers.sh`, with:
  - `gh_api_safe <endpoint> [gh api args...]`
  - `capture_run_id <repo> <created_after> <name_regex>`
  - optionally `watch_run_until_complete <repo> <run_id> <deadline_epoch>`  
  Update the clarify, plan, implement, review, and orchestrate-poll watcher steps to source that module instead of redefining the helpers inline.

#### DUP-002 — Multiple workflows re-implement GitHub retry wrappers instead of using the canonical helper library
- **File path**: `.github/workflows/cancel_on_pr_close.yml:26-53`; `.github/workflows/mark-stable.yml:303-336,472-485`; `.github/workflows/orchestrate_poll.yml:79-111`; `.github/workflows/review_autofix.yml:600-612,1291-1329`; `.github/workflows/test-and-mark-stable.yml:4523-4536`; `scripts/gh_helpers.sh:381-512`
- **Severity**: Medium
- **Category tag**: `duplication`
- **Description**: The repository already has canonical retry helpers in `scripts/gh_helpers.sh` (`gh_retry`, `gh_retry_to_file`, `_safe_gh_jq`), but several workflows still carry bespoke wrappers. The duplicates are not identical:
  - `scripts/gh_helpers.sh` distinguishes permanent vs retryable failures and includes rate-limit trip-breaker/alert behavior.
  - `review_autofix.yml:603-612` retries every failure with simple doubling sleep.
  - `mark-stable.yml`, `cancel_on_pr_close.yml`, `orchestrate_poll.yml`, and `test-and-mark-stable.yml` each keep their own `_gh_retry` variant with fixed `/tmp/_gh_rl_err` scratch files and slightly different logging.
- **Recommended fix**: Standardize on `scripts/gh_helpers.sh` as the single owner. Where a workflow cannot assume the repo is already checked out, add a minimal fetched helper bootstrap or a tiny shared script dedicated to retry logic. The function surface should stay the canonical one already in use: `gh_retry`, `gh_retry_to_file`, and `_safe_gh_jq`.

#### DUP-003 — `comprehensive-test-and-release.yml` duplicates its dispatch-watch utility block inside the same file
- **File path**: `.github/workflows/comprehensive-test-and-release.yml:57-103,305-345`
- **Severity**: Low
- **Category tag**: `duplication`
- **Description**: The workflow defines the same helper set twice in the same file:
  - `sanitize_single_line()`
  - `is_positive_integer()`
  - `gh_api_safe()`
  - `list_dispatch_runs()`  
  The two copies are materially identical aside from surrounding local variables (`WORKFLOW_FILE`, `TARGET_BRANCH`).
- **Recommended fix**: Move the helper block into a shared script, e.g. `scripts/workflow_dispatch_watch.sh`, with a narrow interface such as `list_dispatch_runs <repo> <workflow_file>` and `gh_api_safe <endpoint> [args...]`. Then invoke that helper from both dispatch-watch sections.

### Section 4: Expression Size Limit Risk Assessment

No additional expression-size findings crossed the reporting thresholds.

- **Largest interpolated `run:` block found**: `.github/workflows/test-and-mark-stable.yml:4501-4571` at approximately **2,999 characters**, leaving roughly **18,001 characters** of headroom before the 21,000-character hard limit.
- **Next largest interpolated `run:` blocks**:
  - `.github/workflows/plan.yml:1245-1288` at approximately **2,962 characters**
  - `.github/workflows/mark-stable.yml:303-368` at approximately **2,743 characters**
- **Largest workflow file sizes**:
  - `.github/workflows/review_autofix.yml` — **286,105 bytes**
  - `.github/workflows/test-and-mark-stable.yml` — **271,478 bytes**
  - `.github/workflows/implement.yml` — **187,126 bytes**

All workflow files are well below the **800 KB** early-warning threshold and far below the **1 MB** hard file-size limit.

### Section 5: Cross-Cutting Concerns

#### DEAD-001 — Reserved label-repair evidence helpers are present but not wired into any workflow/script entrypoint
- **File path**: `scripts/orchestrate_lib.py:988-1371`
- **Severity**: Low
- **Category tag**: `dead-code`
- **Description**: `parse_phase_failure_markers()`, `resolve_label_repair_evidence()`, and `choose_most_advanced_conclusive_evidence()` are fully implemented in `scripts/orchestrate_lib.py`, but repo-local workflow/script references do not call them. The architecture note in `agents.md:124-131` explicitly says these helpers are “contract/reserved and not yet wired into poller reconciliation,” which matches the absence of call sites in the workflow/script surface.
- **Recommended fix**: Either wire these helpers into the active label-repair path in `scripts/orchestrate_poll_process.sh` or remove/defer them until the integration is ready. Leaving them half-integrated increases maintenance cost because behavior can drift without any production caller exercising it.

#### DEBT-001 — Deterministic-skip label metadata is hard-coded inline and must stay “in lockstep” with the real label catalog
- **File path**: `.github/workflows/review_autofix.yml:614-625`; `scripts/label_helpers.sh:22-98`
- **Severity**: Low
- **Category tag**: `tech-debt`
- **Description**: The deterministic-skip job in `review_autofix.yml` defines its own `ensure_label_exists()` and hard-codes the `ai:review-skipped` color/description inline. The comment at lines 619-624 explicitly says this copy “must stay in lockstep with the catalog,” while the canonical catalog already exists in `scripts/label_helpers.sh` and ultimately mirrors `.github/ai/label_contract.v1.json`. This is a known drift trap rather than a hypothetical one.
- **Recommended fix**: Make `scripts/label_helpers.sh` the only owner of label metadata. If the job must stay lightweight, fetch/source just that helper or generate the one-off label metadata from the same central contract during bootstrap rather than hand-copying color/description strings.

#### CONSIST-001 — `review_autofix` still uses step-local retry semantics that differ from the canonical GitHub helper behavior
- **File path**: `.github/workflows/review_autofix.yml:600-612,1291-1329`; `scripts/gh_helpers.sh:381-512`
- **Severity**: Medium
- **Category tag**: `consistency`
- **Description**: `review_autofix.yml` contains two different inline retry implementations:
  - a simple exponential backoff wrapper at lines 603-612
  - a file-capturing wrapper at lines 1308-1329  
  Neither matches the canonical behavior in `scripts/gh_helpers.sh`, which classifies permanent failures, retries JSON fetches safely, and centralizes rate-limit handling. Because the review path is one of the repository’s most API-heavy flows, this inconsistency creates workflow-specific retry semantics and makes future incident analysis harder.
- **Recommended fix**: Replace both inline wrappers with the canonical `scripts/gh_helpers.sh` interface. If early bootstrap constraints prevent sourcing it directly, extract a minimal shared retry shim from `gh_helpers.sh` and consume that same shim in all review-related jobs.

_Notes from the cross-cutting sweep:_
- The `TODO|FIXME|HACK|XXX` grep did **not** surface genuine debt markers in workflow/script bodies; the hits were `mktemp` suffixes such as `XXXXXX`, not real TODO comments.
- I did not elevate the unquoted `for ... in ${var}` loops I found in `scripts/tg_helpers.sh`, `scripts/check_external_branch_advance.sh`, and selected workflows because the audited values are repo slugs or numeric/comma-delimited IDs, not free-form user text.

### Section 6: Summary & Severity Matrix

#### 6A. Findings Summary Table

| Severity | Count | IDs |
|---|---:|---|
| Critical | 0 | — |
| High | 0 | — |
| Medium | 8 | BUG-001, BUG-002, BATCH-001, BATCH-002, API-001, DUP-001, DUP-002, CONSIST-001 |
| Low | 3 | DUP-003, DEAD-001, DEBT-001 |

#### 6B. Estimated Remediation Scope

| Category | Files Touched | Estimated Effort |
|---|---:|---|
| Critical/High bug fixes | 0 | Small |
| API call optimization | 2-3 | Large |
| Code modularization | 5-7 | Medium |
| Expression size reduction | 0 | Small |
| Medium/Low fixes | 4-6 | Medium |
