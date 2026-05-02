## Executive Summary

- **`test_and_mark_stable` is the largest latency and reliability problem.** All 4 sampled runs failed, each taking **4,758–5,609s** before failing. In run **25237291900**, the alt-model test stayed at label `ai:implementing` for roughly **70+ minutes** before timing out; in run **25215477856**, the smoke test reached Phase 4b and still found the bait line present. **Estimated impact:** save **60–75 minutes per failed release attempt** and materially reduce false release failures. **Confidence:** high.

- **CI is consistently dominated by a single long `lint` job.** Sampled successful CI runs such as **25243341540 (542s)**, **25243249104 (633s)**, **25242009056 (612s)**, and **25239378015 (543s)** all spent most runtime in `lint`, including the **81-test orchestrate-lib suite** plus the main **25-test** suite. **Estimated impact:** **3–5 minutes saved per CI run** by parallelizing test groups. **Confidence:** high.

- **`review_autofix` burns substantial time on runs that never need full agent work.** The family has **29 cancelled runs out of 68 total**; examples **25243175899 (305s)**, **25243249158 (322s)**, and **25243341555 (394s)** were cancelled after the workflow had already decided to skip editor/commit/judge work. **Estimated impact:** save **5–6 minutes per superseded review run** plus associated model cost. **Confidence:** high.

- **Implement failures are concentrated in support-asset staging, not model execution.** Failed implement runs such as **25224008847** error out on missing staged assets like `prompts/serena-efficiency-block.txt`, `prompts/mode-implement-diagnose.txt`, and `prompts/mode-implement-repair.txt` before Codex can complete useful work. **Estimated impact:** remove a large share of the current **3.3% implement-family failure rate** and reduce reruns. **Confidence:** high.

- **GH API usage is heavily poll-based and redundant on the critical path.** The e2e smoke/alt-model loops poll issue labels, comments, workflow runs, jobs, and branch state every **10–20s**; the alt-model timeout loop in **25237291900** ran for the full **4,500s** budget. **Estimated impact:** **50–80% fewer API calls** in those workflows, with lower rate-limit risk and faster failure detection. **Confidence:** medium-high.

- **Prompt-cache and AI memory plumbing exists, but effectiveness is weakly observed.** Across sampled telemetry there were **13 memory retrieves** with only **46.2% hit rate** and **7 zero-hit retrieves**, mostly in reviewer flows; prompt-cache instrumentation is present, but sampled runs often emit cache fields as unavailable/`na` rather than real hit data. **Estimated impact:** modest direct savings, but meaningful improvements in token efficiency and diagnosis quality once telemetry is made actionable. **Confidence:** medium.

---

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Fail fast in `test_and_mark_stable` instead of waiting full timeout budgets
**Type:** critical-path

- **Evidence**
  - Workflow family `test_and_mark_stable` has **4/4 failures**, average duration **5,206s**, p50 **5,228.5s**.
  - Run **25237291900** (`e2e-alt-model-test`) stayed on `ai:implementing` from about **23:22:59Z** until timeout at **00:33:23Z**, then failed with `Alt-model run timed out before reaching review stage`.
  - Run **25215477856** failed in `e2e-smoke-test` at `Phase 4b: Verify editor removed bait line`; the canary still contained the bait line and the workflow logged `Editor failed to remove bait line`.

- **Root cause**
  - The alt-model gate is **label-driven**, not **run-state-driven**. It polls labels every 20s and waits until review/merge labels appear, but does not prove a new implement/review run is active and advancing.
  - The smoke-test bait verification waits until late in the sequence to conclude the editor path never produced the expected fix commit.

- **Exact change**
  - In the alt-model wait step:
    - capture the post-issue **implement run ID** immediately after `/approved`;
    - poll **that run’s status and current job/step** rather than only issue labels;
    - if the run is missing for >5 minutes, or if it remains in the same `implementing`/no-run state for N polls, fail early with a phase-specific error.
  - In the smoke-test review/bait path:
    - compare PR head SHA against the bait SHA earlier;
    - if no post-bait commit appears within a short window, or if an editor-noop/failure marker is detected, fail immediately instead of waiting for later verification.

- **Estimated time savings**
  - **60–75 minutes per failed stable-release test run**.

- **Implementation risk**
  - **Low-medium**. Logic changes are local to test orchestration and can fail open to the current timeout path if API reads fail.

---

### 2. Split the long CI `lint` job into parallel jobs
**Type:** critical-path

- **Evidence**
  - CI family: **53 runs**, average **606.6s**, p50 **613s**, p95 **647s**, failure rate **11.3%**.
  - Successful runs **25243341540 (542s)**, **25243249104 (633s)**, **25243175894 (641s)**, **25242009056 (612s)**, **25239378015 (543s)** all report `lint` as the dominant runtime.
  - Logs show both the main suite (**25 passed**) and orchestrate-lib unit tests (**81 passed**) running in the same job.

- **Root cause**
  - A single serial job is carrying heterogeneous work: script checks, linting, main unit tests, and orchestrate-lib tests.

- **Exact change**
  - Split `.github/workflows/ci.yml` into separate jobs such as:
    - `lint-static`
    - `unit-core`
    - `unit-orchestrate-lib`
    - `workflow-script-ref-check`
  - Keep a lightweight aggregate status gate if needed.

- **Estimated time savings**
  - **180–300s per CI run** if runners are available.

- **Implementation risk**
  - **Medium**. Extra jobs may add some queue time, but current compute time is large enough that parallelism should still win.

---

### 3. Gate `review_autofix` before provisioning the expensive codex-agent path
**Type:** critical-path

- **Evidence**
  - `review_autofix` family: **68 runs**, **29 cancelled**, p95 **2,019.4s**.
  - Runs **25243175899 (305s)**, **25243249158 (322s)**, and **25243341555 (394s)** were cancelled after the workflow had already decided on a **comment-only / no editor / no judge** path.
  - Logs explicitly show `editor/commit/judge/auto-merge skipped`.

- **Root cause**
  - Expensive setup and runner allocation happen before the workflow proves it needs the full codex-agent execution path.

- **Exact change**
  - Move the `comment-only`, `claude-branch-review`, and deterministic skip decisions into a **minimal gate job**.
  - Only start support-source staging, Serena setup, and codex-agent execution when `should_run == true`.
  - Add `concurrency.cancel-in-progress` keyed by repository + PR + head SHA for `review_autofix`.

- **Estimated time savings**
  - **5–6 minutes per cancelled/superseded review run**.

- **Implementation risk**
  - **Low**. This is a control-flow reorder, not a behavior change in the full-review path.

---

### 4. Lazy-initialize Serena/MCP only when the agent path is actually entered
**Type:** local micro-optimization

- **Evidence**
  - Slow review run **25215784558** and recent review run **25243341555** both show full Serena setup and validation.
  - In **25243341555**, Serena setup runs even though the workflow is later cancelled.
  - Git MCP is explicitly disabled in sampled review runs.

- **Root cause**
  - MCP initialization is treated as unconditional prework, even for paths that never use agent editing.

- **Exact change**
  - Move `setup_serena.sh` behind the same gate that decides whether codex-agent will run.
  - Skip Serena entirely for deterministic-skip/comment-only paths.

- **Estimated time savings**
  - **7–10s per skipped/cancelled review run**, plus lower setup churn.

- **Implementation risk**
  - **Low**.

---

### 5. Shorten or remove per-run artifact cleanup from Copilot reviewer PR runs
**Type:** local micro-optimization

- **Evidence**
  - Copilot reviewer runs show cleanup dominating:
    - **25241997982**: `Cleanup artifacts` ~**171s**
    - **25241576994**: `Cleanup artifacts` ~**4m31s**
    - **25243342317**: cleanup lists artifacts then deletes them one-by-one
  - Cleanup uses `gh api /repos/.../actions/runs/<id>/artifacts` followed by per-artifact delete calls.

- **Root cause**
  - Artifact deletion is happening synchronously on the PR critical path.

- **Exact change**
  - Prefer artifact `retention-days` over immediate deletion for PR-review artifacts, or gate deletion to larger/default-branch runs.
  - If deletion must remain, delete only artifacts matching the workflow’s own naming pattern and skip when none are present.

- **Estimated time savings**
  - **60–240s per Copilot reviewer run**, depending on artifact count.

- **Implementation risk**
  - **Medium**. Must ensure artifact growth remains acceptable.

---

## Cost Optimizations

Ranked by expected token and/or dollar savings.

### 1. Reduce reviewer/editor reasoning effort and summarizer budget on low-risk review paths
- **Evidence**
  - Review runs expose:
    - `REVIEWER_REASONING_EFFORT: xhigh`
    - `EDITOR_REASONING_EFFORT: xhigh`
    - `XPOLL_SUMMARISER_MODEL: openai/gpt-5.4-mini`
    - `XPOLL_SUMMARISER_MAX_INPUT_LINES: 3000`
    - `XPOLL_SUMMARISER_CALL_TIMEOUT_SECS: 2400`
  - Slow run **25215784558** shows summarizer passes and consolidator input around **237,738 bytes**.
  - Recent run **25243341555** includes the note that the model “burned its budget on internal reasoning over the massive prompt.”

- **Root cause**
  - High-reasoning settings and large summarizer inputs are used even for paths that are comment-only, deterministic-skip candidates, or otherwise low-value for deep reasoning.

- **Exact change**
  - For comment-only / claude-branch-review / deterministic-skip-eligible PRs:
    - drop reviewer/editor reasoning from `xhigh` to `medium` or `low`;
    - bypass the summarizer unless there are multiple substantive reviewer outputs;
    - lower `XPOLL_SUMMARISER_MAX_INPUT_LINES` to a smaller cap after dedupe (for example, 800–1200).

- **Estimated savings**
  - **30–60% token reduction** on those review paths, and **10–25 minutes** saved on the slowest review runs.

- **Quality-risk notes**
  - **Medium** if applied globally.
  - **Low** if restricted to comment-only/small-diff/deterministic-skip paths.

---

### 2. Turn prompt-cache instrumentation into real cache hits
- **Evidence**
  - Prompt cache is enabled (`OPENROUTER_PROMPT_CACHE_DISABLED: false`) in implement and review flows.
  - But sampled review logs show cache fields as unavailable/`na`, e.g. `cache_creation_input_tokens=na` and `cache_read_input_tokens=na`.
  - Implement runs emit the prompt-cache instrumentation block but not actionable hit/miss values.

- **Root cause**
  - Cache plumbing exists, but prompt prefixes appear unstable and telemetry is not exposing real per-call creation/read counts.

- **Exact change**
  - Keep the first prompt segment stable:
    - static system instructions first;
    - stable repo/workflow context second;
    - volatile values like run IDs, issue numbers, timestamps, bait markers, and long issue bodies appended last.
  - Emit actual `cache_creation_input_tokens` and `cache_read_input_tokens` per call in sampled logs.

- **Estimated savings**
  - **10–25% input-token reduction** on repeated implement/review flows once cache reuse is real.

- **Quality-risk notes**
  - **Low**. This is prompt-shape stabilization, not content removal.

---

### 3. Eliminate avoidable `review_autofix` cancellations and reruns
- **Evidence**
  - `review_autofix` has **29 cancelled runs / 68 total**.
  - Several cancelled runs already waited for runners and completed gate/setup work before being superseded.

- **Root cause**
  - New pushes or superseding runs are arriving after the expensive path already started.

- **Exact change**
  - Add workflow concurrency with cancel-in-progress.
  - Defer agent setup until after gate resolution.

- **Estimated savings**
  - Significant runner-minute and model-cost reduction on active PRs with multiple updates.

- **Quality-risk notes**
  - **Low**. Superseded runs already have no final value.

---

### 4. Reduce `workflow_log_analysis` summarization spend on clean runs
- **Evidence**
  - Deep-audit output in sampled logs cites exact telemetry for run **25208727402**: `model=openai/gpt-5.4-mini`, `summarized=83`, `tokens_used=160232`.
  - This indicates materially nontrivial summarization cost inside the log-analysis pipeline.

- **Root cause**
  - The analysis workflow appears to summarize large run sets even when many runs are clean/skipped and structurally similar.

- **Exact change**
  - Group runs by workflow family and warning pattern before summarization.
  - Reuse cached summaries for unchanged/no-warning families.
  - Route obviously clean/skipped runs to a cheaper summarization template or skip full summarization entirely.

- **Estimated savings**
  - **25–50% token reduction** for `workflow_log_analysis`.

- **Quality-risk notes**
  - **Low-medium**. Must preserve detail for failing/slow/outlier runs.

---

### 5. Reduce full-PR file expansion and synchronous artifact deletion in Copilot reviewer
- **Evidence**
  - `Prepare` uses `github.paginate(github.rest.pulls.listFiles, per_page: 100)` and `pulls.get`.
  - Cleanup lists and deletes artifacts via GH API on every run.

- **Root cause**
  - Full file enumeration and post-run cleanup are done eagerly on every PR run.

- **Exact change**
  - Reuse the fetched file list across all steps in the run.
  - Skip synchronous artifact deletion when retention settings already bound storage.

- **Estimated savings**
  - **Low-moderate** token/API/runtime savings per run.

- **Quality-risk notes**
  - **Low** if reuse is internal to the run.

---

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Fix self-repo support-asset staging in `implement`
- **Failure evidence**
  - Implement failures **25224008847**, **25224028373**, **25237418726**, **25237690797**, **25237704374**, **25215763575** all center on missing staged assets before useful work completes.
  - Example errors in **25224008847** include:
    - `Failed to stage required file prompts/serena-efficiency-block.txt`
    - `Failed to stage required file prompts/mode-implement-diagnose.txt`
    - `Failed to stage required file prompts/mode-implement-repair.txt`

- **Root cause category**
  - **Workflow bootstrap / asset staging bug**

- **Exact fix**
  - Stage required prompts/support files into a dedicated runtime support directory **unconditionally** before launching Codex.
  - Add a single preflight that enumerates all missing required assets and aborts once, early, with a compact list.

- **Expected reliability impact**
  - Should eliminate a large share of the current **6 implement failures in 180 runs**.

- **Rollback / fail-open considerations**
  - Keep existing `main` fallback checkout, but make the final missing-asset test explicit and early.

---

### 2. Replace label-only alt-model waiting with direct phase-run tracking
- **Failure evidence**
  - Run **25237291900** timed out after **5,465s** while the issue label remained `ai:implementing`.
  - The post-timeout analyzer could not find new clarify/plan/implement/review runs for that issue.

- **Root cause category**
  - **State-machine observation gap**

- **Exact fix**
  - Anchor on newly created run IDs and PR head branch.
  - Fail when the expected implement run never materializes, or when it stays non-advancing beyond a bounded stale threshold.

- **Expected reliability impact**
  - High for `test_and_mark_stable`, currently **4/4 failed**.

- **Rollback / fail-open considerations**
  - On API lookup failure, continue to the current label-based fallback instead of hard failing.

---

### 3. Suppress deterministic auto-merge while the smoke-test bait check is active
- **Failure evidence**
  - In **25215477856**, the workflow explicitly warns that the PR may already have been merged/closed before bait injection, citing interaction with `deterministic-skip-merge`.
  - The final canary still contained the bait line.

- **Root cause category**
  - **Cross-workflow race**

- **Exact fix**
  - When a PR carries the smoke-test/force-review markers used by the e2e harness:
    - disable deterministic-skip auto-merge;
    - delay auto-merge until bait verification completes.

- **Expected reliability impact**
  - High for smoke-test false negatives.

- **Rollback / fail-open considerations**
  - Restrict the guard to explicit e2e labels only.

---

### 4. Add retry/backoff to Copilot reviewer `Prepare` GH script calls
- **Failure evidence**
  - Copilot reviewer `Prepare` logs show `actions/github-script@v8` with `retries: 0`.

- **Root cause category**
  - **Transient API flake sensitivity**

- **Exact fix**
  - Set `retries: 2` or `3` with bounded exponential backoff for `pulls.get` and `pulls.listFiles` reads.

- **Expected reliability impact**
  - Moderate reduction in occasional prepare-stage flakes without changing semantics.

- **Rollback / fail-open considerations**
  - Low-risk; retried reads are idempotent.

---

### 5. Make nightly validation self-test failures diagnosable from the job summary
- **Failure evidence**
  - Run **25242537588** only reports `fixtures=3 passed=1 failed=2` and exits 1.
  - No failing fixture identifiers are surfaced in the sampled excerpt.

- **Root cause category**
  - **Insufficient failure observability**

- **Exact fix**
  - Write failing fixture names and failure classes into the step summary and uploaded summary artifact.

- **Expected reliability impact**
  - Faster mean-time-to-repair and fewer blind reruns.

- **Rollback / fail-open considerations**
  - Low. This is additive reporting only.

---

## AI Memory Health

AI memory telemetry **was observed** in sampled deep-dive logs.

### Observed telemetry summary
- **Total observed operations:** 68
- **Operation mix:**
  - `record-run-event`: 33
  - `retrieve`: 13
  - `processed-command-check`: 8
  - `processed-command-claim`: 8
  - `summarize_unselected_runs`: 4
  - `record-candidate`: 2

### Retrieve effectiveness
- **Retrieve hit rate:** **46.2%** (`6/13` returned at least one record)
- **Zero-hit retrieves:** **7/13**
- **Average `estimated_tokens`:** **19.4**
- **`keyword_method` distribution:**
  - `none`: 7
  - `plain`: 6
  - `llm`: 0 observed

### Flags
- **`fail_open: true` entries:** none observed in parsed telemetry JSON
- **`enabled: false` entries:** none observed
- **Push retries >1:** 1 observed event (`record-run-event` in implement run **25215763575** had `push_attempts: 2`)

### What this means
- The **reviewer** memory path is underperforming. Zero-hit retrieves appear repeatedly in review flows:
  - slow review run **25215784558**
  - slow review run **25237552686**
  - recent cancelled review runs **25243341555** and **25243249158**
- Retrieve budgets are not explicitly emitted, so `estimated_tokens` can be compared only against itself, not a declared cap.
- The absence of any observed `llm` keywording suggests reviewer memory retrieval is using only `plain` matching or no keyword extraction at all.

### Recommendation
- For reviewer flows, derive keywords from **PR title + changed file paths + gate reason** before defaulting to `keyword_method: none`.
- Track a simple KPI: **reviewer memory zero-hit rate**, with a target below **20%**.

---

## GH API Call Audit

### 1. `test_and_mark_stable` has the highest redundant API volume
- **Evidence**
  - In smoke-test wait loops, the workflow polls:
    - issue labels
    - comment count
    - recent bot comments
    - actions run lists
    - jobs for the latest run
    - branch/commit state
  - Example loop logic is visible in **25215477856**:
    - clarify phase reads labels/comments/latest comment every **10s**
    - implement phase reads PR existence, labels, run list, jobs, and branch head every **10s**
  - Alt-model run **25237291900** polls labels every **20s** for a **4,500s** deadline.

- **High-volume pattern**
  - Unbatched per-iteration REST polling.

- **Recommendation**
  - Replace multi-call loop bodies with **one GraphQL query** for issue/PR label state plus **one cached runs query** per cycle.
  - Only fetch jobs when the run is actually `in_progress`.
  - Back off polling from **10–20s** to **30–60s** once the state has remained unchanged for several cycles.

- **Estimated reduction**
  - **50–80% fewer API calls** in the e2e workflows.
  - Meaningful reduction in rate-limit exposure.

- **Repo API hygiene alignment**
  - This directly matches the repo’s own logged rules around **mandatory batching**, **cycle-local caches**, and **fail-open behavior**.

---

### 2. `review_autofix` still performs broad paginated fetches where cycle-local caching should dominate
- **Evidence**
  - Slow review logs show `gh api --paginate --slurp` patterns and explicit internal guidance about reusing cycle-local caches.
  - Recent review run **25243341555** shows rate-limit-aware wrappers and paginated API access in the codex-agent path.

- **High-volume pattern**
  - Paginated PR/file scans and repeated PR metadata lookups within the same execution.

- **Recommendation**
  - Materialize a single per-run JSON cache for:
    - PR metadata
    - changed files
    - label set
    - mergeability / base-branch info
  - Reuse it across gate, reviewer prep, summarizer prep, and conflict logic.

- **Estimated reduction**
  - **Low-moderate call-count reduction per run**, but high cumulative benefit because `review_autofix` is frequent.

- **Repo API hygiene alignment**
  - Matches the repo’s explicit “cycle-local caches are first-class” rule.

---

### 3. Post-merge validate dispatch does per-issue lookups and mutations
- **Evidence**
  - In **25243477064**, post-merge validate dispatch:
    - fetches linked issues via GraphQL,
    - may call `gh issue view` for each linked issue when labels are not known,
    - dispatches validation,
    - then calls `gh issue edit --remove-label` per issue.

- **High-redundancy pattern**
  - Per-item follow-up calls after already fetching issue nodes.

- **Recommendation**
  - Ensure the initial GraphQL fetch always returns the needed label state, so the `gh issue view` fallback is only for API failure.
  - Remove labels only after a confirmed dispatch, and stop iterating after the one issue that actually triggered the standalone validation when appropriate.

- **Estimated reduction**
  - **1 API call saved per linked issue** in the common case.

- **Repo API hygiene alignment**
  - Batching first, fail open second.

---

### 4. Copilot reviewer uses full file pagination plus per-artifact deletes
- **Evidence**
  - `Prepare` in Copilot reviewer uses:
    - `github.rest.pulls.get`
    - `github.paginate(github.rest.pulls.listFiles, per_page: 100)`
  - `Cleanup artifacts` lists artifacts, then deletes each artifact individually.

- **High-volume pattern**
  - Full file pagination on every run and per-artifact delete loops.

- **Recommendation**
  - Persist the fetched file list for all downstream steps in the run.
  - If artifact deletion remains necessary, filter by exact artifact naming convention before deleting.

- **Estimated reduction**
  - **Small per run**, but meaningful over many PR-review runs.

---

## MCP & Serena Efficiency

### Observed state
- Serena MCP is **installed and validated** in sampled review runs:
  - **25215784558**
  - **25243341555**
- Git MCP is **disabled** in those runs.
- The logs show Serena setup, but **no visible evidence of symbol-first tool usage** such as targeted symbol lookups in the sampled excerpts.
- Support files like `serena-efficiency-block.txt` are staged as required assets.

### Findings
1. **Setup overhead exists even when the full edit path is later skipped or cancelled**
   - This is wasted time on comment-only and superseded review runs.

2. **Git MCP is disabled, so the workflow cannot benefit from targeted Git-side fetches**
   - That pushes more work toward raw `gh api`, broader diff handling, and larger prompt context.

3. **The sampled logs do not show strong evidence that Serena’s symbol-oriented flow is being exercised enough to justify unconditional setup**
   - The gap may be logging visibility, actual usage, or both.

### Recommendations
- **Lazy-init Serena** only after the gate decides codex-agent work is required.
- **Enable Git MCP** for review/edit flows where repo policy allows it; this should reduce broad GH API and raw-diff dependence.
- **Emit a compact Serena usage summary** per run:
  - symbol lookups count
  - pattern searches count
  - broad file-read count
  - fallback-to-raw-read count

### Expected impact
- **Latency:** save setup overhead on skipped/cancelled runs.
- **Token efficiency:** smaller context slices if symbol-first retrieval is actually used.
- **Correctness:** improved targeted edits, especially in larger files.

---

## Prompt Cache & Memory System

### Prompt cache
- **Observed**
  - Prompt cache is enabled in sampled implement/review runs.
  - Instrumentation headers are present.
  - Real per-call cache hit/miss quantities are often **not emitted** as usable numbers in sampled runs.

- **Likely cache-fragmentation causes**
  - unstable prompt prefixes containing:
    - run IDs
    - issue numbers
    - timestamps
    - bait markers
    - large issue bodies/comments injected early

- **Recommendations**
  1. Move all volatile run-specific values to the **tail** of the prompt.
  2. Keep system/workflow instructions identical across retries and sibling runs.
  3. Emit real cache counters per call so hit rate can be measured.

- **Estimated impact**
  - **Tokens:** likely **10–25% input savings** on repeated flows.
  - **Latency:** lower on cache-hit paths.
  - **Reliability:** better observability of cache regressions.

### Memory system
- **Observed**
  - Implementation memory retrieval is sometimes useful:
    - run **25224008847** retrieved **2** records with `estimated_tokens: 56`
  - Reviewer memory retrieval is frequently not useful:
    - multiple review runs retrieved **0** records with `keyword_method: none`

- **Recommendations**
  1. Improve reviewer retrieval queries with deterministic keywords from file paths and PR metadata.
  2. Record more reviewer-side successful patterns so the reviewer role has reusable memory to retrieve.
  3. Add an alert threshold for reviewer zero-hit rate.

- **Estimated impact**
  - **Tokens:** small direct savings
  - **Quality:** moderate improvement from better prior-context retrieval
  - **Reliability:** easier diagnosis when memory silently stops helping

---

## Orchestrator Health

### What looks healthy
- `orchestrate_poll` sampled runs are short and mostly healthy:
  - **25243468081** completed in **45s**
  - `poll_started` and `poll_completed` both recorded with `push_attempts: 1`
- No stuck terminal state was directly observed in sampled poller telemetry.

### Recurring operational pain points
1. **High review churn**
   - `review_autofix` has many cancellations after nontrivial work has already started.

2. **Conflict-heavy repository surface**
   - Poller fetch logs show many conflict-oriented branch names; this aligns with existing merge/conflict complexity in the repo.

3. **Memory-helper gaps still appear in some review runs**
   - Several recent summaries mention `memory helper script missing; skipping run-end failure event`.

4. **Label/state transitions can diverge from actual run progress**
   - Most visible in stable-release e2e failures.

### Smallest safe mitigations
- Add workflow concurrency on `review_autofix`.
- Track and alert on:
  - **cancelled review_autofix rate**
  - **age of `ai:implementing` without a live implement run**
  - **runner queue wait p50/p95**
  - **memory-helper-missing warning count**
  - **merge deferral count by PR**

### Verification indicators
- `review_autofix` cancellation rate drops below current **42.6%** (`29/68`).
- `test_and_mark_stable` failure duration drops from ~**80–93 min** to under **15–20 min** for false-path failures.
- Reviewer memory zero-hit rate falls below **20%**.
- CI p50 drops materially below current ~**613s**.

---

## Pipeline Flow Bottlenecks

Ordered by end-to-end impact.

### 1. Release-test polling and timeout overhead
- **Where:** `clarify -> plan -> implement -> review` in `test_and_mark_stable`
- **Dominant overhead:** retry/poll + stalled state observation
- **Evidence:** failed runs lasting **4,758–5,609s**
- **Fix:** direct run tracking, stale-state detection, early failure on absent phase runs

### 2. Serial CI compute
- **Where:** `ci -> lint`
- **Dominant overhead:** compute
- **Evidence:** repeated **542–650s** successful CI runs dominated by the same job
- **Fix:** parallelize suites/jobs

### 3. Review-autofix supersession churn
- **Where:** `review/autofix`
- **Dominant overhead:** compute + queueing + cancellation waste
- **Evidence:** cancelled runs spending **305–394s** before dying
- **Fix:** lightweight gate first, concurrency cancellation

### 4. GH API polling loops
- **Where:** e2e waits, review prep, post-merge dispatch
- **Dominant overhead:** retry/poll + API redundancy
- **Evidence:** loops polling every **10–20s**
- **Fix:** batch, cache, slower backoff after stable state

### 5. Merge/conflict handling overhead
- **Where:** review/autofix + orchestrator
- **Dominant overhead:** merge/conflict churn
- **Evidence:** conflict-heavy branch inventory in poller logs and repo’s extensive conflict-guard logic
- **Fix:** preserve current fail-open design, but reduce unnecessary re-entry and improve pre-merge gating

### 6. Runner queueing on short jobs
- **Where:** many small utility workflows
- **Dominant overhead:** queueing
- **Evidence:** frequent `Job is waiting for a hosted runner to come online`
- **Fix:** coalesce tiny follow-up jobs where safe, and skip unnecessary ones early

---

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `test_and_mark_stable` failures running **~79–93 minutes**
- CI `lint` job running **~9–10.5 minutes** repeatedly
- `review_autofix` churn from cancelled/superseded runs

**Top failure modes**
- Stable-release e2e false negatives:
  - alt-model stuck in `ai:implementing`
  - editor bait line not removed
- Implement bootstrap failures from missing staged support assets
- Nightly validation self-test failing with insufficient per-fixture detail

**Highest-cost drivers**
- High-reasoning review/editor settings on low-value review paths
- Large summarizer inputs in `review_autofix`
- Full workflow-log summarization runs with six-figure token usage evidence in deep-audit output

**Top 3 prioritized actions**
1. **Fail-fast and run-aware `test_and_mark_stable`**
2. **Parallelize CI `lint`/test work**
3. **Move `review_autofix` gate ahead of codex-agent/Serena setup and add concurrency cancellation**

---

## Metrics Appendix

### Overall repository metrics

| Repository | Total Runs | Success | Failure | Cancelled | Other/Skipped | Failure Rate | p50 Duration (s) | p95 Duration (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 1000 | 279 | 17 | 34 | 670 | 1.7% | 1.0 | 608.0 |

### Key workflow-family metrics

| Workflow Family | Total | Success | Failure | Cancelled | Other | Failure Rate | Avg (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 53 | 47 | 6 | 0 | 0 | 11.3% | 606.6 | 613.0 | 647.0 |
| review_autofix | 68 | 38 | 0 | 29 | 1 | 0.0% failure / 42.6% cancelled | 394.1 | 40.5 | 2019.4 |
| implement | 180 | 22 | 6 | 5 | 147 | 3.3% | 36.7 | 1.0 | 236.1 |
| test_and_mark_stable | 4 | 0 | 4 | 0 | 0 | 100.0% | 5206.0 | 5228.5 | 5587.4 |
| orchestrate_poll | 36 | 36 | 0 | 0 | 0 | 0.0% | 56.4 | 45.0 | 55.0 |
| copilot_pull_request_reviewer | 22 | 22 | 0 | 0 | 0 | 0.0% | 134.6 | 121.0 | 253.4 |
| nightly_validation_selftest | 1 | 0 | 1 | 0 | 0 | 100.0% | 89.0 | 89.0 | 89.0 |

### Sampled run evidence used most heavily

| Run ID | Workflow Family | Conclusion | Duration (s) | Primary Evidence |
|---|---|---:|---:|---|
| 25237291900 | test_and_mark_stable | failure | 5465 | alt-model stayed `ai:implementing` until timeout |
| 25215477856 | test_and_mark_stable | failure | 5609 | bait line still present in canary at Phase 4b |
| 25243341540 | ci | success | 542 | `lint` dominates runtime |
| 25243249104 | ci | success | 633 | `lint` dominates runtime |
| 25242009056 | ci | success | 612 | `lint` dominates runtime |
| 25215784558 | review_autofix | success | 3032 | long codex-agent + summarizer + zero-hit reviewer memory |
| 25243341555 | review_autofix | cancelled | 394 | comment-only/superseded path still paid setup cost |
| 25243477064 | review_autofix | success | 28 | post-merge validate dispatch GH API pattern |
| 25224008847 | implement | failure | 240 | missing staged support assets |
| 25242537588 | nightly_validation_selftest | failure | 89 | 3 fixtures, 2 failed, poor detail |
| 25243342317 | copilot_pull_request_reviewer | success | 272 | artifact cleanup API pattern |
| 25243468081 | orchestrate_poll | success | 45 | healthy poll start/end telemetry |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total observed memory telemetry ops | 68 |
| Retrieve ops | 13 |
| Retrieve hit rate | 46.2% |
| Zero-hit retrieves | 7 |
| Avg retrieve estimated tokens | 19.4 |
| `keyword_method=plain` | 6 |
| `keyword_method=none` | 7 |
| `keyword_method=llm` | 0 |
| `fail_open: true` observed | 0 |
| `enabled: false` observed | 0 |
| Push attempts >1 observed | 1 |

### Exact token/cache evidence observed

| Source Run | Workflow | Evidence |
|---|---|---|
| 25212191835 deep-audit output citing run 25208727402 | workflow_log_analysis | `model=openai/gpt-5.4-mini`, `summarized=83`, `tokens_used=160232` |
| 25215784558 | review_autofix | summarizer/consolidator input about `237738` bytes, `model=openai/gpt-5.4-mini`, reasoning `medium` |
| 25243477064 | review_autofix | `XPOLL_SUMMARISER_MAX_INPUT_LINES=3000`, timeout `2400`, editor/reviewer reasoning high |
| 25215784558 / 25243341555 | review_autofix | prompt-cache instrumentation present, but cache creation/read values not surfaced as actionable counts |

### GH API hotspot summary

| Workflow / Step | Pattern | Evidence | Optimization |
|---|---|---|---|
| test_and_mark_stable / e2e-smoke-test | poll labels/comments/runs/jobs every 10s | run 25215477856 | batch + cache + slower stable-state polling |
| test_and_mark_stable / e2e-alt-model-test | label polling every 20s for 4500s | run 25237291900 | switch to direct phase-run tracking |
| review_autofix / post-merge-validate-dispatch | GraphQL + per-issue `gh issue view` + `gh workflow run` + `gh issue edit` | run 25243477064 | reuse GraphQL labels, reduce per-issue fallbacks |
| copilot reviewer / Prepare | `pulls.get` + `github.paginate(pulls.listFiles)` | run 25243342317 | fetch once, reuse within run |
| copilot reviewer / Cleanup artifacts | list artifacts + delete per artifact | run 25243342317 | reduce synchronous deletes or narrow scope |

If you want, I can turn this into a **PR-ready action plan** with concrete YAML/script changes ranked by implementation effort.
