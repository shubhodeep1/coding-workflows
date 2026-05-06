## Executive Summary

- **`review_autofix` is the biggest end-to-end latency and waste driver.** It ran **102 times** with **p95 1669s**, and **71/102 runs were cancelled**; recent cancelled runs still consumed **297s** (`25440969072`), **364s** (`25441201265`), **383s** (`25441510678`), and **694s** (`25438922959`), while successful comment-only review paths still took **1522s** (`25439520487`). **Estimated impact:** save **5–20 minutes per active PR**. **Confidence:** high.

- **`test_and_mark_stable` is both slow and flaky.** It has only **4 runs** in-window, but **2 failed** (**50% failure rate**); failures took **3427s** (`25428461223`) and **4579s** (`25416934394`) waiting for downstream phases to progress. **Estimated impact:** save **20–40 minutes on failed validation runs** and materially reduce false release blockers. **Confidence:** high.

- **CI is consistently slow because `lint` is a monolith.** `ci` has **p50 612s / p95 651s** across **70 runs**. In `25441201354`, the job spent about **615s** in `lint`, while `Orchestrate lib unit tests` itself finished in about **3s** with **81 passed**. **Estimated impact:** cut **4–7 minutes per CI run** by splitting/sharding. **Confidence:** medium.

- **Reviewer-side AI memory retrieval is currently ineffective.** Across deep-dive logs, memory `retrieve` hit rate was only **28.6% (6/21)**, and in `review_autofix` it was **0/8 hits**, always `keyword_method: "none"` and `estimated_tokens: 0`; e.g. `25441201265` and `25441510678`. **Estimated impact:** modest token/latency savings, plus cleaner prompts. **Confidence:** high.

- **Prompt cache is enabled but effectively unmeasured.** `OPENROUTER_PROMPT_CACHE_DISABLED: false` appears in review runs, but cache probe logs in slow runs `25394267845` and `25413999630` reported `prompt_tokens=na`, `completion_tokens=na`, `cache_creation_input_tokens=na`, and `cache_read_input_tokens=na`. **Estimated impact:** unknown until instrumentation improves; likely meaningful on repeated reviewer runs. **Confidence:** medium.

- **GitHub API usage is concentrated in a few avoidable polling/list patterns.** Hotspots are repeated workflow-run polling in E2E tests, PR file pagination in Copilot reviewer prepare, and artifact enumeration/deletion in Copilot cleanup. **Estimated impact:** reduce API calls by **30–70%** in those hotspots and lower rate-limit/queue risk. **Confidence:** medium-high.

## Speed Optimizations

### 1. Shrink `review_autofix` comment-only / Claude-branch review path
**Type:** critical-path win

- **Evidence:**  
  - `review_autofix` family: **102 total**, **p95 1669.15s**, **71 cancelled**.  
  - `25439520487` succeeded in **1522s** with `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... running reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped.`  
  - Cancelled runs still burned minutes before termination: `25440969072` (**297s**), `25441201265` (**364s**), `25441510678` (**383s**), `25438922959` (**694s**).  
  - `25441201265` and `25441510678` both spent repeated 20-second cycles in `Waiting for 1 in-progress/queued check-run(s)...` before cancellation.

- **Root cause:**  
  The branch-review/comment-only path still runs an expensive reviewer panel and long check-run wait policy even when edit/commit/judge/auto-merge are already skipped.

- **Exact change:**  
  - For `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW` and other comment-only paths:
    1. run **single-pass** review instead of two-pass,
    2. use a **smaller reviewer subset**,
    3. reduce `CHECK_RUNS_WAIT_TIMEOUT_SECS` from **1200** to a shorter comment-only value (for example 300),
    4. cancel any in-progress review run immediately when a newer run for the same PR/head SHA starts.

- **Estimated time savings:**  
  **5–20 minutes per affected PR**, especially on active branches with repeated pushes.

- **Implementation risk:**  
  **Medium.** Main risk is lighter review coverage; mitigate by escalating to full panel only when the smaller panel disagrees or the diff exceeds a threshold.

---

### 2. Replace label-only E2E phase waiting with direct child-run tracking in `test_and_mark_stable`
**Type:** critical-path win

- **Evidence:**  
  - `25416934394` failed after **4579s** at `e2e-alt-model-test / Wait for clarify→plan→implement (alt-model)`. The issue stayed at `labels: ai:implementing` until timeout, then emitted `Alt-model run timed out before reaching review stage`.  
  - `25428461223` failed after **3427s** at `e2e-smoke-test / Phase 4: Wait for review & autofix to complete`.  
  - The workflow family has **50% failure rate** in-window (**2 failures / 4 total**).

- **Root cause:**  
  Phase progression is inferred from issue labels and periodic workflow/run polling instead of using authoritative downstream run IDs and conclusions end-to-end.

- **Exact change:**  
  - Persist downstream `clarify`, `plan`, `implement`, and `review_autofix` run IDs immediately after dispatch.
  - Poll those run IDs directly for `status` and `conclusion`.
  - Keep labels only as secondary diagnostics.
  - Add a “stuck in same label/state for N minutes” early-fail rule with automatic log collection.

- **Estimated time savings:**  
  **20–40 minutes** on failed release-validation runs; smaller savings on healthy runs from less redundant polling.

- **Implementation risk:**  
  **Low-medium.** Safe if label-based fallback remains in place when a child run ID is unavailable.

---

### 3. Split the monolithic `ci/lint` job into parallel shards
**Type:** critical-path win

- **Evidence:**  
  - `ci` family: **70 total**, **p50 612s**, **p95 650.75s**.  
  - Recent successful runs were tightly clustered around **598–648s**: `25438232923` (**598s**), `25439520203` (**617s**), `25440968318` (**613s**), `25441201354` (**620s**).  
  - In `25441201354`, the overall `lint` phase ran about **615s**, but `Orchestrate lib unit tests` completed in about **3s** with **81 passed**.

- **Root cause:**  
  Many heterogeneous checks are serialized inside one long job, so setup/wrapper overhead dominates and no parallelism is used.

- **Exact change:**  
  Split `lint` into at least 3 independent jobs:
  1. Python/unit tests,
  2. shell/YAML/actionlint/static checks,
  3. workflow contract tests.  
  If safe, add changed-path gating so workflow contract suites run only when workflow/prompt/test harness files change.

- **Estimated time savings:**  
  **4–7 minutes per CI run**, depending on runner queue overlap.

- **Implementation risk:**  
  **Medium.** Path gating must be conservative to avoid missing regressions; parallel split alone is low risk.

---

### 4. Shorten `review_autofix` check-run waiting for superseded or comment-only runs
**Type:** critical-path win

- **Evidence:**  
  - `25441201265` logged repeated waits every **20s** with deadline starting at **1200s** and then was cancelled.  
  - `25441510678` showed the same pattern, also ending in `##[error]The operation was canceled.`  
  - Env in slow/recent review runs repeatedly shows `CHECK_RUNS_WAIT_TIMEOUT_SECS: 1200` and `CHECK_RUNS_POLL_INTERVAL_SECS: 20`.

- **Root cause:**  
  A fixed, long wait policy is applied broadly, including runs that are low-value or already superseded.

- **Exact change:**  
  - Reduce the timeout for non-merging/comment-only review paths.
  - Exit early when a newer run exists for the same PR/head.
  - Increase poll interval slightly on low-priority waits to reduce churn.

- **Estimated time savings:**  
  **1–5 minutes per run** on many review paths, with larger savings on cancelled runs.

- **Implementation risk:**  
  **Low.** This only changes post-review waiting behavior.

---

### 5. Cut `orchestrate_poll` runner/setup overhead
**Type:** secondary win

- **Evidence:**  
  - Successful polls are usually short but still setup-heavy: `25439160746` (**54s**) with checkout dominating about **10s**; `25436742701` (**51s**); `25434582367` (**49s**).  
  - Failed polls `25383797907` and `25424218738` both lasted **903s**; `25424218738` system log shows repeated runner pickup cycles roughly every 5 minutes without productive work.

- **Root cause:**  
  Poller cycles are paying repeated runner startup/checkout cost, and duplicate/superseded poller activity is not being collapsed aggressively enough.

- **Exact change:**  
  - Add a repo-level concurrency group so only one poller is active.
  - Use minimal checkout (`fetch-depth: 1`, no extra refs/tags) when `has_work=false` path is expected.
  - Skip dispatch when a poller run is already active and healthy.

- **Estimated time savings:**  
  **10–20s per successful poll** plus elimination of some **15-minute failure cases**.

- **Implementation risk:**  
  **Low.**

## Cost Optimizations

### 1. Reduce reviewer-model fan-out and disable two-pass on comment-only reviews
- **Evidence:**  
  - Review runs expose `REVIEWER_MODELS` with **6 models** and `ENABLE_REVIEWER_TWO_PASS: true`.  
  - This configuration appears in slow review runs like `25394267845`, `25413999630`, and recent branch-review runs `25441201265` / `25441510678`.  
  - Comment-only paths still ran for **297–1522s** despite skipping edit/commit/judge/auto-merge.

- **Root cause:**  
  Expensive reviewer breadth is being used even when the workflow has already decided it is only going to comment.

- **Exact change:**  
  - On comment-only/small-diff branch-review paths, run **1–2 reviewers, one pass only**.
  - Escalate to the full 6-model/two-pass panel only on disagreement, policy-sensitive files, or larger diffs.

- **Estimated savings:**  
  Likely **50–80% reviewer token spend** on these paths, plus substantial GitHub-minutes savings.

- **Quality-risk notes:**  
  **Medium.** Use escalation fallback to preserve quality.

---

### 2. Stop burning model time on superseded `review_autofix` runs
- **Evidence:**  
  - `review_autofix` had **71 cancelled runs out of 102**.  
  - Several cancelled runs consumed hundreds of seconds before cancellation: `25440969072` (**297s**), `25441201265` (**364s**), `25441510678` (**383s**), `25438922959` (**694s**).

- **Root cause:**  
  New pushes/cycles arrive before existing review jobs are preempted, so AI work is started and then discarded.

- **Exact change:**  
  - Enforce `concurrency.cancel-in-progress` keyed by PR + head SHA (or PR only for comment-only path).
  - Add a preflight “newer run exists” check before reviewer execution starts.

- **Estimated savings:**  
  High on active PRs; this removes avoidable reviewer calls and GitHub runtime from superseded runs.

- **Quality-risk notes:**  
  **Low.** Latest-run-only behavior is typically preferable for branch review.

---

### 3. Add first-failure model fallback in `implement` instead of repeating known stuck behavior
- **Evidence:**  
  - `25417040196` failed in `Internal: AI Implement` after `Codex announced an edit/apply_patch ... but produced no file changes` and then `Codex produced no actionable output 2 attempts in a row`.  
  - The same run then triggered downstream E2E timeout waste in `25416934394`.  
  - `MODEL_EDITOR` in these runs is `openai/gpt-5.3-codex`.

- **Root cause:**  
  A known non-actionable-output mode is retried within the same failure mode instead of switching strategy immediately.

- **Exact change:**  
  After the first “announced edit/no file changes” detection:
  - switch to repair prompt immediately,
  - swap to the safer editor model/version already used elsewhere in the pipeline,
  - keep the current hard abort if the fallback also fails.

- **Estimated savings:**  
  Saves the cost of failed implement retries and prevents expensive downstream false failures.

- **Quality-risk notes:**  
  **Low-medium.** Keep the fallback narrowly scoped to this failure signature.

---

### 4. Reduce Copilot reviewer artifact churn
- **Evidence:**  
  - `copilot_pull_request_reviewer` runs frequently spend most visible runtime in artifact cleanup: `25441521795` (**259s**), `25438924644` (**323s**), `25431923040` (**85s**).  
  - These runs call `gh api /repos/.../actions/runs/<run>/artifacts` and perform cleanup work even on otherwise simple reviews.

- **Root cause:**  
  Artifact listing/deletion appears to run per review and becomes a dominant non-AI cost center.

- **Exact change:**  
  - Only enumerate/delete artifacts when artifacts for the current run prefix actually exist.
  - Avoid cleanup on no-op or failed-prepare runs.
  - Reuse artifact IDs already discovered earlier in the run instead of re-listing.

- **Estimated savings:**  
  Lower GitHub minutes and API overhead; moderate runtime savings on every Copilot review run.

- **Quality-risk notes:**  
  **Low.** Use prefix-scoped deletion to avoid retention regressions.

---

### 5. Treat prompt cache as an optimization target only after measuring it correctly
- **Evidence:**  
  - Cache is enabled (`OPENROUTER_PROMPT_CACHE_DISABLED: false`), but cache probe logs in `25394267845` and `25413999630` emitted `prompt_tokens=na`, `completion_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`.

- **Root cause:**  
  Cache is on, but the pipeline cannot currently prove whether it is helping.

- **Exact change:**  
  - Emit per-call cache create/read counters for reviewer/editor/summarizer calls.
  - If the provider cannot expose them, stop relying on the probe for decisions and optimize prompt stability directly.

- **Estimated savings:**  
  **Unknown today** because measurement is incomplete.

- **Quality-risk notes:**  
  **Low.** This is instrumentation and prompt-shape hygiene.

## Reliability Improvements

### 1. Harden `implement` against the “announced edit, no change” failure mode
- **Failure evidence:**  
  - `25417040196` failed at `Run Codex implementation` with:
    - `Codex announced an edit/apply_patch on attempt 3 ... but produced no file changes`
    - `Codex produced no actionable output 2 attempts in a row`
    - final `Codex bailed... agent loop stuck in exploration.`

- **Root cause category:**  
  AI tool-use / stuck-output failure.

- **Exact fix:**  
  - On first occurrence, force:
    - repair-mode prompt,
    - explicit `git diff --stat` verification,
    - alternate editor model/version,
    - immediate failure summary with captured logs if still unchanged.

- **Expected reliability impact:**  
  Reduces direct `implement` failures and downstream `test_and_mark_stable` false failures.

- **Rollback / fail-open:**  
  If fallback path misbehaves, keep current 2-strike abort as the final safeguard.

---

### 2. Make `test_and_mark_stable` use authoritative downstream run status
- **Failure evidence:**  
  - `25416934394` timed out waiting for alt-model progress while issue labels remained `ai:implementing`.  
  - `25428461223` failed waiting for review/autofix completion after nearly an hour.

- **Root cause category:**  
  Orchestrator monitoring / phase-detection fragility.

- **Exact fix:**  
  - Track child workflow run IDs from dispatch through completion.
  - Poll child run conclusions directly.
  - Use issue labels/comments only as auxiliary evidence.

- **Expected reliability impact:**  
  Strong reduction in false red release-validation runs.

- **Rollback / fail-open:**  
  If run ID discovery fails, revert to current label-based logic for that phase only.

---

### 3. Fix missing token/input plumbing in Copilot reviewer `Prepare`
- **Failure evidence:**  
  - `25389586417` failed in **42s** at `Prepare` with `Unhandled error: Error: Input required and not supplied: github-token`.  
  - The same step was configured with `retries: 0`.

- **Root cause category:**  
  Workflow input/secrets wiring.

- **Exact fix:**  
  - Add explicit preflight validation for required token inputs.
  - Pass the token unambiguously through reusable workflow boundaries.
  - If absent, mark the run neutral/skipped with a clear summary rather than failing after startup.

- **Expected reliability impact:**  
  Removes an entire hard-failure class from `copilot_pull_request_reviewer`.

- **Rollback / fail-open:**  
  Safe fail-open is “skip reviewer and summarize missing auth”.

---

### 4. Add bounded retries to API-heavy Copilot `Prepare`
- **Failure evidence:**  
  - `25389586417` and successful prepare-heavy runs show `actions/github-script@v8` with `retries: 0`.  
  - API-heavy work includes `github.rest.pulls.get` and `github.paginate(github.rest.pulls.listFiles, ...)`.

- **Root cause category:**  
  No resilience on transient API or abuse/secondary-limit errors.

- **Exact fix:**  
  Set small retry/backoff for retryable statuses while preserving the current exempt list for permanent client errors.

- **Expected reliability impact:**  
  Moderate improvement in reviewer stability under transient GitHub API conditions.

- **Rollback / fail-open:**  
  Retry count can be dialed back without changing behavior.

---

### 5. Prevent spurious `orchestrate_poll` failures from runner churn
- **Failure evidence:**  
  - `25383797907` and `25424218738` both failed after **903s**.  
  - `25424218738` system log shows repeated runner pickup cycles instead of productive work.

- **Root cause category:**  
  Operational scheduling / duplicate dispatch.

- **Exact fix:**  
  - Concurrency-limit pollers per repo.
  - Detect active healthy poller and skip duplicate launch.
  - If queue delay exceeds threshold, exit soft/neutral and let next scheduled poll continue.

- **Expected reliability impact:**  
  Lowers spurious poller red runs and reduces noise in orchestrator health.

- **Rollback / fail-open:**  
  Safe, because the next poll cycle still runs.

## AI Memory Health

- **Telemetry coverage found:** yes. I found **88** `AI_MEMORY_TELEMETRY:` entries in the deep-dive logs.
- **Operation mix:**  
  - `record-run-event`: **41**  
  - `retrieve`: **21**  
  - `record-candidate`: **9**  
  - `processed-command-check`: **6**  
  - `processed-command-claim`: **6**  
  - `summarize_unselected_runs`: **5**

- **Retrieve hit rate:** **28.6%** (**6/21** had `records_selected > 0`).
- **Average retrieve `estimated_tokens`:** **16.0** overall.
- **`keyword_method` distribution:**  
  - `none`: **15**  
  - `plain`: **6**  
  - `llm`: **0** observed in deep-dive logs.

- **Workflow-specific memory effectiveness:**  
  - `implement`: **2 retrieves**, **2 hits**, average `estimated_tokens=56.0`, `keyword_method=plain` in failed runs `25417030055` and `25417040196`.  
  - `review_autofix`: **8 retrieves**, **0 hits**, average `estimated_tokens=0.0`, all `keyword_method=none`; e.g. `25441201265` and `25441510678`.  
  - `workflow_log_analysis`: **11 retrieves**, **4 hits**, average `estimated_tokens=20.4`.

- **Flagged patterns:**  
  - **Zero-record retrieves are common**, especially in reviewer flows; example: `25394267845` logged `{"op":"retrieve","records_selected":0,"keyword_method":"none","estimated_tokens":0,"role":"reviewer"}`.  
  - **No `fail_open: true` entries** were observed in telemetry payloads.  
  - **No `enabled: false` entries** were observed in telemetry payloads.  
  - **Push retries were usually clean**, but **3 telemetry events used `push_attempts: 2`**, with **max 2** overall.  
  - `25433288590` (`memory_maintenance`, from run-row `log_summary`) successfully compacted memory for month `2026-04`, archiving **2914 candidates**, with `push_attempts: 1`.

- **Assessment:**  
  The memory system is functioning operationally, but reviewer retrieval is not currently adding useful context.

- **Recommendation:**  
  - Improve reviewer retrieval query generation so it does not default to `keyword_method=none`.
  - Add retrieval-quality counters per workflow (`selected`, `discarded`, `used in final prompt`).
  - If reviewer retrieval stays at 0-hit, disable it for that path until query quality is improved, to keep prompts deterministic.

## GH API Call Audit

### 1. E2E stable tests are over-polling issue/workflow state
- **Evidence:**  
  - `25416934394` repeatedly polls:
    - issue labels via `gh api "repos/${TEST_REPO}/issues/${ISSUE_NUMBER}"`,
    - workflow run lists via `gh api "repos/${TEST_REPO}/actions/workflows/${WF}.yml/runs?..."`
  - `25428461223` also uses repeated `gh api` wrappers for issue labels/comments plus workflow status checks and rate-limit-aware polling.

- **Pattern:**  
  Repeated per-phase `gh api` calls in loops, often polling the same resources every **10–20s**.

- **Recommendation:**  
  - Fetch all downstream run statuses in **one poll cycle** using a broader run-list query and filter locally.
  - Reuse the same issue payload per cycle instead of separate issue/comment/label calls.
  - Track child run IDs once and stop listing workflow runs repeatedly.

- **Estimated call-count reduction:**  
  **50–70%** in these E2E wait loops.

- **Rate-limit risk reduction:**  
  High for release-validation workflows.

---

### 2. Copilot reviewer `Prepare` uses paginated PR file enumeration every run
- **Evidence:**  
  - `25441009065` `Prepare_Get_pull_request_details` explicitly uses:
    - `github.rest.pulls.get`
    - `github.paginate(github.rest.pulls.listFiles, ...)`
  - Similar behavior appears in `25389586417`, `25431923040`, and `25437898841`.

- **Pattern:**  
  Full PR metadata and full changed-file lists are re-fetched in every reviewer run, even on repeated/superseded runs.

- **Recommendation:**  
  - Persist PR metadata/file list once per workflow chain and pass it via outputs/artifacts to downstream jobs.
  - Avoid re-fetching if the same PR/head SHA has already been prepared in the same run family.

- **Estimated call-count reduction:**  
  **2–N paginated calls per run**, depending on PR size.

- **Rate-limit risk reduction:**  
  Moderate.

---

### 3. Copilot artifact cleanup is API-heavy and high-redundancy
- **Evidence:**  
  - `25441521795`, `25438924644`, and `25431923040` all showed `Cleanup artifacts` as a runtime hotspot and called `gh api /repos/.../actions/runs/<run>/artifacts`.

- **Pattern:**  
  Artifact list/delete operations are running even when they dominate the workflow.

- **Recommendation:**  
  - Reuse already-known artifact IDs where possible.
  - Skip cleanup when no result artifact was created.
  - Delete only artifacts for the current run prefix, not broader lists.

- **Estimated call-count reduction:**  
  Likely **30–60%** in the cleanup phase.

- **Rate-limit risk reduction:**  
  Moderate.

---

### 4. No-op `review_autofix` branch-resolution still incurs API lookups
- **Evidence:**  
  - Recent no-op branch-review runs `25438232939` and `25440964223` logged:
    - `gh api "repos/${REPOSITORY}/pulls?state=open&head=..."`
    - `gh api "repos/${REPOSITORY}" --jq '.default_branch'`

- **Pattern:**  
  PR resolution and repo metadata lookup occur even when `existing_pr` is found quickly and execution is skipped.

- **Recommendation:**  
  - If `existing_pr` is found, skip default-branch lookup.
  - Cache `existing_pr` result between adjacent reusable-workflow stages.

- **Estimated call-count reduction:**  
  Small per run, but worthwhile because these no-op review runs are frequent.

- **Rate-limit risk reduction:**  
  Low-moderate.

---

### 5. Implement path still fans out issue/comment/label API reads
- **Evidence:**  
  - `25417040196` includes separate calls for:
    - issue metadata,
    - paginated issue comments,
    - labels,
    - failure-comment posting,
    - run jobs lookup.

- **Pattern:**  
  Multiple sequential issue-related calls where some payloads overlap.

- **Recommendation:**  
  - Fetch issue + labels once and reuse.
  - Only fetch paginated comments when command parsing actually needs them.
  - Cache run-jobs payload before post-failure summary logic.

- **Estimated call-count reduction:**  
  Moderate on implement failures.

- **Rate-limit risk reduction:**  
  Low-moderate.

## Prompt Cache & Memory System

- **Prompt cache status:** enabled in review flows (`OPENROUTER_PROMPT_CACHE_DISABLED: false`), but not meaningfully observable in current telemetry.
- **Concrete evidence:**  
  - Slow review runs `25394267845` and `25413999630` logged `INFO: openrouter usage phase=review_autofix_cache_probe ... prompt_tokens=na completion_tokens=na total_tokens=na cache_creation_input_tokens=na cache_read_input_tokens=na`.
  - Review jobs already contain a `Pre-assemble static context cacheable across runs` step, which is the right architectural direction.
  - Reviewer memory retrieval in `review_autofix` returned **0 records on 8/8 deep-dive retrieves**.

- **Assessment:**  
  - **Cache likely exists but is unmeasured.**
  - **Memory retrieval is currently helping implement, not review.**
  - **Inference:** repeated branch-review runs probably suffer cache fragmentation from dynamic noise in prompts, because they rerun similar logic with different PR metadata/timestamps while cache value remains unproven.

- **Concrete improvements:**  
  1. **Measure real cache behavior per model call.**  
     Emit `prompt_tokens`, `completion_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens` for reviewer/editor/summarizer calls, not just cache probes.
  2. **Stabilize cache prefixes.**  
     Keep static instructions, invariant repo policy, and stable workflow guidance ahead of volatile PR-specific context; move run IDs, timestamps, and mutable status text to the suffix.
  3. **Normalize reviewer panel order and prompt assembly.**  
     Avoid prompt variance from reordered reviewers or transient env noise.
  4. **Disable ineffective reviewer memory retrieval or improve query selection.**  
     Since review retrieval is currently 0-hit, either improve query generation or skip retrieval in that path until it is useful.
  5. **Promote cache-aware fallbacks.**  
     If cache reads fail or are unsupported, do not add retry loops; fall back to the smallest prompt shape.

- **Estimated impact:**  
  - **Tokens:** unknown today because measurement is incomplete.  
  - **Latency:** potentially meaningful on repeated branch-review runs.  
  - **Reliability:** modest improvement from lower prompt variance and simpler reviewer context.

## Orchestrator Health

- **Healthy signals observed:**
  - Guard workflows are cheap when triggers do not match:
    - `clarify` p50 **1s** with **172 total** and **147 “other”** (mostly skips),
    - `plan` p50 **1s** with **140 total** and **119 “other”**,
    - `implement` p50 **1s** with **140 total** and **118 “other”**.
  - Recent skip runs repeatedly show `clarify.if`, `plan.if`, `implement.if`, and `respond.if` short-circuiting correctly in **1–3s**.

- **Pain points observed:**
  1. **Review path gets stuck in long-running comment-only cycles** rather than resolving quickly.
  2. **Poller health is noisy** because runner churn can create red runs even when no work is shown.
  3. **Stable-test orchestration depends too much on issue-state heuristics** rather than direct child-run outcomes.
  4. **Implement can fail in a model-stuck state and then poison higher-level orchestration.**

- **Smallest safe mitigations:**
  - Add stronger supersession/cancellation semantics in review.
  - Track child run IDs explicitly across orchestrator phases.
  - Introduce “stalled in same state” fast-fail logic with evidence capture.
  - Narrow comment-only review policy to a cheaper, deterministic mode.

- **Observable indicators teams should track:**
  - `review_autofix` cancelled rate,
  - median and p95 time spent in `Waiting for ... check-run(s)`,
  - `test_and_mark_stable` failure rate,
  - `orchestrate_poll` queue-only failure count,
  - AI memory retrieve hit rate by workflow,
  - percent of reviewer runs with measurable prompt-cache read tokens.

## Pipeline Flow Bottlenecks

### 1. Clarify → Plan → Implement
- **Current state:**  
  Trigger guards are efficient; most non-matching events skip in **1–2s**.
- **Bottleneck:**  
  Not the guard phases themselves, but downstream `implement` failure propagation. `25417040196` failed fast, and that cascaded into `test_and_mark_stable` timeout behavior.
- **Fix:**  
  Improve implement fallback and child-run tracking.

### 2. Review / Autofix
- **Current state:**  
  Dominant compute bottleneck in the whole pipeline.  
  Long-running reviewer work and check-run waiting dominate end-to-end latency.
- **Bottleneck type:**  
  Compute + waiting + cancellation waste.
- **Fix order:**  
  1. latest-run-only cancellation,  
  2. smaller branch-review policy,  
  3. shorter check-run waits for comment-only paths.

### 3. Validate / Stable-marking
- **Current state:**  
  `test_and_mark_stable` is the biggest single-run latency source.
- **Bottleneck type:**  
  Polling / orchestration overhead, not pure execution.
- **Fix:**  
  Replace label heuristics with direct run tracking and earlier stuck-state detection.

### 4. CI
- **Current state:**  
  Reliable when green, but consistently slow around **10 minutes**.
- **Bottleneck type:**  
  Serial compute/wrapper overhead.
- **Fix:**  
  Split into parallel shards and optionally gate heavy contract suites by safe path rules.

### 5. Copilot reviewer
- **Current state:**  
  Runtime is dominated by prepare/cleanup artifact operations more than AI logic in the visible logs.
- **Bottleneck type:**  
  Queueing + API cleanup overhead.
- **Fix:**  
  Reuse PR metadata, reduce artifact enumeration/deletion, add retries to prepare.

### 6. Orchestrate poller
- **Current state:**  
  Usually quick, but occasional 15-minute failures create disproportionate noise.
- **Bottleneck type:**  
  Queueing/scheduling overhead.
- **Fix:**  
  Concurrency-limit and short-circuit duplicate pollers.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix`: **102 runs**, **71 cancelled**, **p95 1669.15s**.
- `test_and_mark_stable`: **avg 3707.25s**, **50% failure rate**.
- `ci`: **70 runs**, **p50 612s**, **p95 650.75s**.

**Top failure modes**
- `implement` model/tool-use stall: `25417030055`, `25417040196`.
- `test_and_mark_stable` phase waits timing out: `25416934394`, `25428461223`.
- Copilot reviewer missing token input: `25389586417`.
- Poller runner-churn failures: `25383797907`, `25424218738`.

**Highest-cost drivers**
- Multi-model two-pass reviewer panel on comment-only runs.
- Cancelled review runs that still execute for minutes.
- Slow monolithic CI `lint`.
- Artifact cleanup/listing in Copilot reviewer.

**Top 3 prioritized actions**
1. **Refactor `review_autofix` comment-only paths** to single-pass/smaller-panel and enable aggressive latest-run cancellation.  
2. **Rework `test_and_mark_stable` phase tracking** to poll child run IDs instead of labels.  
3. **Split `ci/lint` into parallel shards** and keep path gating conservative.

## Metrics Appendix

### Overall window summary

| Scope | Total Runs | Success | Failure | Cancelled | Other/Skipped | Avg Duration (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| All analyzed runs | 894 | 278 | 13 | 77 | 526 | 146.59 | 2.0 | 647.0 |
| Repo: `shubhodeep1/coding-workflows` | 894 | 278 | 13 | 77 | 526 | 146.59 | 2.0 | 645.7 |

### Key workflow-family metrics

| Workflow Family | Total Runs | Success | Failure | Cancelled | Other | Failure Rate | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `review_autofix` | 102 | 29 | 0 | 71 | 2 | 0.0% | 424.16 | 57.0 | 1669.15 |
| `ci` | 70 | 65 | 5 | 0 | 0 | 7.14% | 603.27 | 612.0 | 650.75 |
| `test_and_mark_stable` | 4 | 2 | 2 | 0 | 0 | 50.0% | 3707.25 | 3708.5 | 4490.65 |
| `orchestrate_poll` | 35 | 33 | 2 | 0 | 0 | 5.71% | 111.06 | 51.0 | 415.10 |
| `copilot_pull_request_reviewer` | 29 | 28 | 1 | 0 | 0 | 3.45% | 192.34 | 180.0 | 350.6 |
| `clarify` | 172 | 25 | 0 | 0 | 147 | 0.0% | 14.96 | 1.0 | 96.0 |
| `plan` | 140 | 21 | 0 | 0 | 119 | 0.0% | 10.93 | 1.0 | 124.05 |
| `implement` | 140 | 14 | 2 | 6 | 118 | 1.43% | 22.24 | 1.0 | 179.75 |
| `memory_maintenance` | 4 | 4 | 0 | 0 | 0 | 0.0% | 39.0 | 41.5 | 42.0 |

### AI memory telemetry metrics

| Metric | Value |
|---|---:|
| Telemetry entries found | 88 |
| `retrieve` operations | 21 |
| Retrieve hit rate | 28.6% (6/21) |
| Avg retrieve estimated tokens | 16.0 |
| `keyword_method=none` | 15 |
| `keyword_method=plain` | 6 |
| `keyword_method=llm` | 0 |
| `enabled: false` entries | 0 |
| `fail_open: true` entries | 0 |
| Telemetry entries with `push_attempts > 1` | 3 |
| Max `push_attempts` seen | 2 |

### Workflow-specific memory retrieval

| Workflow | Retrieve Count | Hits | Hit Rate | Avg Estimated Tokens | Keyword Methods |
|---|---:|---:|---:|---:|---|
| `implement` | 2 | 2 | 100% | 56.0 | `plain` |
| `review_autofix` | 8 | 0 | 0.0% | 0.0 | `none` |
| `workflow_log_analysis` | 11 | 4 | 36.4% | 20.4 | `none`, `plain` |

### Prompt/token/cache visibility

| Signal | Status | Evidence |
|---|---|---|
| Total prompt/completion tokens | Not available in sampled deep-dive logs | No reliable aggregate token totals emitted in the provided run logs |
| Prompt cache enabled | Yes | `OPENROUTER_PROMPT_CACHE_DISABLED: false` in review runs |
| Cache read/create metrics | Not measurable | Cache probes in `25394267845` and `25413999630` logged `prompt_tokens=na`, `completion_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na` |
| Reviewer memory usefulness | Poor | `review_autofix` retrieves were 0/8 hits in deep-dive logs |

### GH API hotspot summary

| Workflow / Step | Observed Pattern | Example Runs | Optimization Priority |
|---|---|---|---|
| `test_and_mark_stable` E2E wait steps | Repeated `gh api` polling for issue labels/comments and workflow runs in loops | `25416934394`, `25428461223` | High |
| Copilot reviewer `Prepare` | `github.rest.pulls.get` + paginated `pulls.listFiles` | `25441009065`, `25431923040`, `25389586417` | Medium-High |
| Copilot reviewer `Cleanup artifacts` | `gh api /actions/runs/<id>/artifacts` + deletion work dominates runtime | `25441521795`, `25438924644`, `25431923040` | Medium-High |
| `review_autofix` branch resolution | PR lookup + repo default-branch lookup even on no-op paths | `25438232939`, `25440964223` | Medium |
| `implement` failure handling | Separate issue/comments/labels/jobs API calls | `25417040196` | Medium |

### Notable failing runs referenced

| Run ID | Workflow Family | Duration (s) | Failure Point |
|---|---|---:|---|
| `25416934394` | `test_and_mark_stable` | 4579 | `e2e-alt-model-test / Wait for clarify→plan→implement (alt-model)` |
| `25428461223` | `test_and_mark_stable` | 3427 | `e2e-smoke-test / Phase 4: Wait for review & autofix to complete` |
| `25417040196` | `implement` | 137 | `implement / Run Codex implementation` |
| `25417030055` | `implement` | 130 | `implement / Run Codex implementation` |
| `25424218738` | `orchestrate_poll` | 903 | No specific step captured; repeated runner pickup visible |
| `25383797907` | `orchestrate_poll` | 903 | No specific step captured |
| `25389586417` | `copilot_pull_request_reviewer` | 42 | `Prepare` missing `github-token` input |
| `25425170301` | `ci` | 563 | `Validate process cross-cycle escalation unit tests` |
| `25425264723` | `ci` | 580 | `Validate process cross-cycle escalation unit tests` |
| `25414664546` | `nightly_validation_selftest` | 90 | `validation-selftest` summary: fixtures=3, passed=1, failed=2 |
