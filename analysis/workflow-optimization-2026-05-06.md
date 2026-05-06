## Executive Summary

- **`review_autofix` is the largest end-to-end latency and spend hotspot, especially on comment-only `claude/*` branch reviews.** Deep-dive runs `25413999630` (2,160s), `25415476868` (1,524s), and cancelled runs like `25411605236` (1,690s) all spent most of their time in `review / codex-agent (claude-branch-review)` even though the workflow explicitly skipped editor/commit/judge paths and stayed in comment-only mode. **Estimated impact:** 8-20 minutes saved per affected run. **Confidence:** high.

- **`review_autofix` has a concrete reliability defect in conflict resolution, not just random flakiness.** Failed runs `25370025320`, `25370115370`, and `25371432937` all died in `Run Codex resolver, validate, stage, commit` after repeated merge conflicts on `tests/e2e_smoke_canary.txt`; one log shows unresolved markers (`run_id: 25369768571` vs `run_id: alt-25369768571`) and `Conflict resolver failed after retries.` **Estimated impact:** remove most of the current 3.0% `review_autofix` hard-failure rate. **Confidence:** high.

- **Release validation (`test_and_mark_stable`) is dominated by long poll/retry loops and repeated GH API reads.** The family averages 3,255s, with failures at `25369768571` and `25375729485` and a successful slow run at `25378639747` (3,830s). The repo’s own workflow-log-analysis runs flag Phase 4b/Phase 7 polling as a 50-80% API-reduction opportunity. **Estimated impact:** 5-12 minutes saved on release-validation runs plus materially fewer API calls. **Confidence:** high.

- **Prompt cache is enabled but effectively unmeasured, and memory retrieval is currently not helping.** Review logs show `OPENROUTER_PROMPT_CACHE_DISABLED: false`, but cache probe telemetry reports `prompt_tokens=na`, `total_tokens=na`, `cache_creation_input_tokens=na`, `cache_read_input_tokens=na`. Across all deep-dive logs, memory `retrieve` ran 13 times with a 0% hit rate and 0 average estimated tokens. **Estimated impact:** 10-20% token savings possible once prompt prefixes stabilize and cache telemetry is made observable. **Confidence:** medium.

- **GitHub-hosted runner queueing is a first-order bottleneck for several workflows and the direct cause of `orchestrate_poll` failures.** `orchestrate_poll` failures `25381014761` and `25383797907` never got past repeated “Waiting for a runner to pick up this job...” cycles and both failed at 903s. CI and Copilot review runs also repeatedly wait for runners before work begins. **Estimated impact:** eliminate a 5.3% failure mode in `orchestrate_poll` and shave 10-90s off many successful runs. **Confidence:** high.

- **Copilot review has a low-effort hardening gap and an avoidable multi-job overhead pattern.** Failed run `25389586417` crashed in `Prepare` with `Error: Input required and not supplied: github-token`, while successful runs like `25413153151` and `25415113154` repeatedly pay for separate runner waits plus artifact listing/deletion jobs. **Estimated impact:** near-zero preventable hard failures and 1-2 minutes saved per Copilot run. **Confidence:** high.

## Speed Optimizations

Ranked by expected end-to-end latency reduction.

### 1. Short-circuit comment-only `review_autofix` before full reviewer execution
**Type:** critical-path win

- **Evidence**
  - `25413999630` ran 2,160s and explicitly logged `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... comment-only path; editor/commit/judge/auto-merge skipped.`
  - `25415476868` ran 1,524s on the same comment-only path.
  - Cancelled runs `25411605236` (1,690s), `25416377707` (1,176s), `25410856664` (878s), and `25415111837` (868s) still spent most runtime in `review / codex-agent (claude-branch-review)` before cancellation.

- **Root cause**
  - The workflow executes an expensive full review panel on paths that are already known to be comment-only and often low-diff (`files=4-5`, `additions=7-8` in several sampled runs).

- **Exact change**
  - Add a **pre-review API-only gate** before checkout/codex setup that:
    1. detects comment-only `claude/*` branch review mode,
    2. checks whether the PR is already closed/superseded,
    3. routes low-diff comment-only reviews to a **reduced reviewer profile** or single-pass summary path.

- **Estimated time savings**
  - **8-12 minutes/run** for long-lived comment-only reviews.
  - **Up to 20+ minutes/run** on stale runs that are later cancelled.

- **Implementation risk**
  - **Medium.** Safe if limited to comment-only mode and gated by diff size / no-editor path. Keep full panel for non-comment-only or large-risk diffs.

### 2. Tighten `review_autofix` concurrency so newer PR-head runs cancel older ones before `codex-agent`
**Type:** critical-path win

- **Evidence**
  - `review_autofix` has **56 cancelled runs out of 99 total**.
  - Several cancelled runs still entered expensive review execution: `25411605236` (1,690s), `25416377707` (1,176s), `25415111837` (868s), `25413434576` (597s), `25411257707` (330s).

- **Root cause**
  - Cancellation is happening too late, after runner acquisition and codex/reviewer work have already started.

- **Exact change**
  - Use a **PR/head-ref keyed concurrency group** at the earliest possible workflow boundary, with `cancel-in-progress: true`.
  - Add a pre-step that exits immediately if:
    - PR is closed,
    - head SHA is no longer current,
    - a newer run exists for the same PR/head.

- **Estimated time savings**
  - **5-28 minutes avoided** on each stale cancelled run.
  - Large aggregate benefit because cancellations are common.

- **Implementation risk**
  - **Low.** This changes only stale-run behavior.

### 3. Replace `test_and_mark_stable` polling loops with run-ID tracking and widening backoff
**Type:** critical-path win

- **Evidence**
  - `test_and_mark_stable` family average: **3,255s**, p50 **3,179s**, p95 **3,759s**.
  - Failed runs:
    - `25369768571` failed at `Phase 4b: Verify editor restored canary (pytest + retry)` after 3,359s.
    - `25375729485` failed at `Phase 7: Close PR and verify cancel_on_pr_close fires` after 2,999s.
  - Slow success `25378639747` took **3,830s**.
  - The repo’s own workflow-log-analysis runs explicitly identify Phase 4b polling as a **50-80% API reduction opportunity** and describe polling every 10-15s for long windows.

- **Root cause**
  - The workflow repeatedly polls broad run lists / PR state instead of persisting a concrete retry run ID and polling that single run.
  - Poll cadence stays too aggressive for long windows.

- **Exact change**
  - When a retry/dispatch happens, persist the new **workflow run ID** once and poll only `actions/runs/{id}` thereafter.
  - Change poll cadence from fixed short intervals to **10s → 20s → 30s/60s** after initial readiness.
  - Keep current fast polling only for the first few minutes.

- **Estimated time savings**
  - **5-12 minutes/run** on slow/failing stable-release tests.
  - Also substantially lowers API noise.

- **Implementation risk**
  - **Low.** Backward-compatible if terminal conditions stay unchanged.

### 4. Collapse `copilot_pull_request_reviewer` late artifact jobs into the producer path
**Type:** secondary win

- **Evidence**
  - Successful Copilot runs `25413153151` (162s), `25415113154` (307s), `25416236385` (215s) all show repeated runner waits across `Prepare`, `Upload results`, and `Cleanup artifacts`.
  - The cleanup path repeatedly calls `gh api /repos/.../actions/runs/<run_id>/artifacts`.

- **Root cause**
  - Multi-job structure forces extra runner queueing and extra artifact re-discovery after the producing job already knows what it created.

- **Exact change**
  - Move artifact cleanup into the same job that creates/uploads them, or emit artifact IDs as outputs and consume those directly instead of re-listing artifacts in a separate job.

- **Estimated time savings**
  - **60-120s/run** on Copilot review.

- **Implementation risk**
  - **Low.** Pure orchestration change.

### 5. Reduce CI critical path by splitting the long `Orchestrate_poll_process_unit_tests` suite
**Type:** secondary win

- **Evidence**
  - CI family p50 is **619s** and p95 **654.5s**.
  - In recent run `25416235225`, `lint` dominated almost the entire **594s** run.
  - Within that run, `lint_Orchestrate_poll_process_unit_tests` spans roughly **04:20:02–04:28:19**, far longer than the smaller unit-test steps.

- **Root cause**
  - One large test slice dominates the serial `lint` job; install/setup overhead is not the main runtime once runner starts.

- **Exact change**
  - Split the heavy suite into 2 shards or a small matrix, or isolate it into a separate job so the rest of linting finishes earlier.
  - Keep the current serial order for the small suites.

- **Estimated time savings**
  - **2-4 minutes/run** on CI critical path if queue times remain similar.

- **Implementation risk**
  - **Medium.** Test sharding can introduce ordering assumptions if fixtures are stateful.

### 6. Cache Python CI dependencies and the actionlint tarball
**Type:** micro-optimization

- **Evidence**
  - In `25416235225`, CI spends noticeable early time in:
    - `Install Python CI dependencies`
    - `Install actionlint`
  - These steps recur on every CI run.

- **Root cause**
  - Repeat downloads of stable tooling on a high-frequency workflow.

- **Exact change**
  - Cache pip download/install directories for fixed dependency sets.
  - Cache the actionlint binary or fetched tarball by version.

- **Estimated time savings**
  - **20-45s/run**.

- **Implementation risk**
  - **Low.**

## Cost Optimizations

Ranked by expected token / compute spend reduction.

### 1. Reduce reviewer breadth and pass count for comment-only `review_autofix`
- **Evidence**
  - Comment-only review runs still use a 6-model reviewer panel:
    - `REVIEWER_MODELS: minimax/minimax-m2.5 ... x-ai/grok-4.1-fast`
  - Deep-dive `review_autofix` runs repeatedly show `REVIEWERS_SUCCESSFUL: 6`.
  - Long comment-only runs include `25413999630` (2,160s) and `25415476868` (1,524s).

- **Root cause**
  - Expensive reviewer fan-out is applied even when editor/commit/judge are explicitly skipped and diffs are tiny.

- **Exact change**
  - For comment-only mode with small diffs:
    - reduce to **2-3 reviewers**,
    - disable second-pass reviewing,
    - keep the full 6-model, multi-pass path only for editor-capable or higher-risk diffs.

- **Estimated savings**
  - Likely the **largest token reduction in the pipeline**; exact totals are not available in the provided window.
  - Practically, this should remove most of the LLM cost from the longest comment-only runs.

- **Quality-risk notes**
  - **Medium.** Restrict to low-diff comment-only cases to keep quality risk bounded.

### 2. Prevent expensive stale/cancelled `review_autofix` runs from starting heavy AI work
- **Evidence**
  - `review_autofix` has **56 cancellations / 99 runs**.
  - Many cancellations occur after hundreds or thousands of seconds, not immediately.

- **Root cause**
  - Newer runs or PR closures invalidate older runs after AI work is already underway.

- **Exact change**
  - Same early concurrency/preflight fix as in Speed item #2.
  - Make the stale-run check happen **before** model invocation and before heavy repository setup.

- **Estimated savings**
  - Large compute/token savings by eliminating wasted review panels on stale heads.
  - Highest ROI because it removes whole runs, not just tokens within runs.

- **Quality-risk notes**
  - **Low.** No user-visible behavior loss for stale runs.

### 3. Make prompt-cache hits real and measurable by stabilizing prompt prefixes
- **Evidence**
  - Review logs show `OPENROUTER_PROMPT_CACHE_DISABLED: false`.
  - But cache probes in failed and slow `review_autofix` runs report:
    - `prompt_tokens=na`
    - `completion_tokens=na`
    - `total_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`

- **Root cause**
  - Cache is enabled but the pipeline cannot tell whether it is hitting.
  - Dynamic noise likely sits too early in prompts, fragmenting cache keys.

- **Exact change**
  - Move volatile values (run IDs, timestamps, per-run telemetry blocks, transient diff metadata) to the **end** of prompts or separate files.
  - Keep stable instructions and reusable context at the front.
  - Emit actual cache-read/cache-create token counters into logs.

- **Estimated savings**
  - **10-20% prompt-token reduction** is plausible on repetitive review flows, but not yet measurable with current telemetry.

- **Quality-risk notes**
  - **Low.** This is prompt-layout hygiene, not model-behavior change.

### 4. Remove duplicate deep-audit work inside `workflow_log_analysis`
- **Evidence**
  - `workflow_log_analysis` runs are consistently slow: **2,376s-3,287s** in the sampled slow set.
  - Logs show both `deep-audit` and `api-redundancy` Codex passes, with the second pass also rereading long analysis context.

- **Root cause**
  - Multiple AI passes reprocess overlapping report context.

- **Exact change**
  - Reuse the first-pass report artifacts/context instead of rebuilding large overlapping prompt context for the API-redundancy pass.
  - Trim repeated static instructions already available from the first pass.

- **Estimated savings**
  - Moderate token savings and some runtime reduction on analysis workflows.

- **Quality-risk notes**
  - **Low-medium.** Keep the second pass if needed, but feed it a compacted context.

### 5. Improve token observability before further model-selection changes
- **Evidence**
  - Outside a single conflict-resolver snippet (`tokens used 408`), the provided window lacks trustworthy prompt/completion totals per run.
  - Cache counters are mostly `na`.

- **Root cause**
  - Current telemetry is too incomplete to support precise model right-sizing.

- **Exact change**
  - Emit per-call or per-stage:
    - model,
    - prompt tokens,
    - completion tokens,
    - total tokens,
    - cache create/read tokens,
    - retry count.
  - Group by workflow family and run ID.

- **Estimated savings**
  - No immediate savings, but required to safely optimize model mix without blind spots.

- **Quality-risk notes**
  - **Low.**

## Reliability Improvements

Ranked by expected failure-rate or rerun-rate reduction.

### 1. Stop sending the canary file through generic LLM conflict resolution
- **Failure evidence**
  - `review_autofix` failed in runs `25370025320`, `25370115370`, and `25371432937` at `Run Codex resolver, validate, stage, commit`.
  - Logs show merge conflict in `tests/e2e_smoke_canary.txt`, conflict markers with mismatched `run_id`, then `Conflict resolver failed after retries.`

- **Root cause category**
  - Deterministic/generated-file merge conflict being routed through generic AI conflict resolution.

- **Exact fix**
  - Treat `tests/e2e_smoke_canary.txt` as a **generated canary artifact**:
    - resolve via deterministic strategy (`ours`/`theirs` or regeneration),
    - or regenerate it after merge replay instead of asking the resolver to edit conflict markers.

- **Expected reliability impact**
  - Should eliminate the specific repeated failure mode behind all 3 sampled hard `review_autofix` failures.

- **Rollback / fail-open**
  - Easy rollback.
  - Safe fail-open option: if deterministic merge fails, skip autofix continuation and preserve artifacts rather than hard-failing after retries.

### 2. Fix missing token wiring in `copilot_pull_request_reviewer`
- **Failure evidence**
  - Failed run `25389586417` in `Prepare` logged:
    - `GH_TOKEN:`
    - `GITHUB_TOKEN:`
    - `Error: Input required and not supplied: github-token`

- **Root cause category**
  - Required secret/input not plumbed into `actions/github-script@v8`.

- **Exact fix**
  - Wire `github-token` explicitly in all code paths.
  - Add a preflight guard that fails fast with a clear workflow-level message before entering the script action.

- **Expected reliability impact**
  - Removes a fully preventable hard failure in Copilot review.

- **Rollback / fail-open**
  - Low-risk.
  - If token is absent, fail immediately with explicit diagnostics rather than half-starting the job.

### 3. Add bounded retries to Copilot PR file/artifact API calls
- **Failure evidence**
  - Successful Copilot runs show API-heavy preparation/cleanup:
    - `github.paginate(github.rest.pulls.listFiles, ...)`
    - `gh api /repos/.../actions/runs/<run_id>/artifacts`
  - The failed run `25389586417` used `actions/github-script@v8` with `retries: 0`.

- **Root cause category**
  - No retry budget on transient GitHub API reads.

- **Exact fix**
  - Add small bounded retries for:
    - PR file listing,
    - artifact list/delete,
    - PR metadata fetch.
  - Preserve exempt statuses (`400,401,403,404,422`) as already configured.

- **Expected reliability impact**
  - Moderate reduction in sporadic GitHub API/read-time failures.

- **Rollback / fail-open**
  - Low-risk.
  - Keep retry count small to avoid turning hard config bugs into long waits.

### 4. Prevent `orchestrate_poll` from failing on pure runner starvation
- **Failure evidence**
  - `orchestrate_poll` failures `25381014761` and `25383797907` both ended at **903s** with only repeated system-log lines:
    - `Waiting for a runner to pick up this job...`
    - repeated every ~5 minutes.
  - No actual poll logic executed in the captured deep-dive logs.

- **Root cause category**
  - Hosted-runner availability / workflow scheduling contention.

- **Exact fix**
  - Add concurrency so overlapping pollers do not stack.
  - Skip starting a new poll cycle if a same-family poll run is already active and no work is pending.
  - Consider schedule jitter to avoid synchronized bursts.

- **Expected reliability impact**
  - Should remove most of the current **5.3%** `orchestrate_poll` failure rate.

- **Rollback / fail-open**
  - Low-risk if limited to no-work / overlapping-poll scenarios.

### 5. Make nightly validation self-test failures diagnosable before exit
- **Failure evidence**
  - `25414664546` logged `fixtures=3 passed=1 failed=2`, then exited 1.
  - The main failure line does not show which fixtures failed until summary processing after the failure.

- **Root cause category**
  - Poor failure observability.

- **Exact fix**
  - Print failing fixture names and failing stages **before** exit 1.
  - Keep artifact upload, but surface the key failures in the main job log.

- **Expected reliability impact**
  - Small direct failure-rate reduction, but materially faster repair time.

- **Rollback / fail-open**
  - Low-risk; logging-only change.

## AI Memory Health

- **Telemetry presence**
  - Deep-dive logs contain **54** `AI_MEMORY_TELEMETRY` entries in this window.
  - Observed operations:
    - `record-run-event`: 29
    - `retrieve`: 13
    - `record-candidate`: 8
    - `summarize_unselected_runs`: 4
    - `compact`: 1 visible in recent memory-maintenance logs

- **Retrieve effectiveness**
  - `retrieve` count: **13**
  - Retrieves with `records_selected > 0`: **0**
  - **Hit rate: 0.0%**
  - Average `estimated_tokens`: **0.0**
  - `keyword_method` distribution:
    - `none`: **13**
    - `plain`: **0**
    - `llm`: **0**

- **Flags**
  - Retrieves returning 0 records: **13/13**
  - `fail_open: true`: **0 observed**
  - `enabled: false`: **0 observed**
  - High push retry counts: **none observed**; all sampled `record-run-event` and `record-candidate` writes had `push_attempts: 1`.

- **Representative evidence**
  - Failed/slow `review_autofix` runs (`25370025320`, `25370115370`, `25371432937`, `25413999630`) all logged:
    - `{"op":"retrieve","enabled":true,"estimated_tokens":0,"keyword_method":"none","records_selected":0,"role":"reviewer"}`
  - Memory maintenance run `25407580796` logged:
    - `{"op":"compact","month":"2026-04","archived_candidates":2914,"did_push":true,"ok":true}`

- **Assessment**
  - Write-side memory plumbing is healthy.
  - Retrieval is currently **functionally ineffective** for reviewer flows in this sample.

- **Recommendation**
  - Audit why retrieve is always falling back to `keyword_method: none`.
  - Add retrieval outcome counters per workflow family and alert on sustained 0-hit windows.

## GH API Call Audit

### Highest-volume / highest-redundancy patterns

#### 1. `test_and_mark_stable` Phase 4b/Phase 7 polling
- **Evidence**
  - Deep-dive `e2e-smoke-test` logs show repeated `gh api` polling for issue/PR state and workflow runs in long loops.
  - The repo’s own workflow-log-analysis runs (`25375766109`, `25378679803`) explicitly call this the biggest API hotspot and estimate **50-80% fewer API reads** are possible.

- **Pattern**
  - Broad polling of PR/workflow state every 10-15s for long windows.

- **Concrete change**
  - Persist target run IDs and poll only `actions/runs/{id}` after dispatch.
  - Increase backoff over time.

- **Estimated reduction**
  - **Hundreds of API reads → tens** on failing release-validation runs.

- **Rate-limit risk reduction**
  - High, even though no 429s were seen in this sample.

#### 2. `review_autofix` repeated PR metadata / check-run lookup
- **Evidence**
  - Workflow-log-analysis run `25405996019` identifies:
    - `gh api repos/${REPOSITORY}/pulls/${PR_NUMBER}`
    - `gh api repos/${REPOSITORY}/commits/${PR_HEAD_SHA}`
    - `gh api --paginate repos/${REPOSITORY}/pulls/${PR_NUMBER}/files`
    - linked-issues GraphQL
    - check-run polling on commit SHA
  - The same run estimates **~4-8 API calls per run before check-run polling**, with check-run loops potentially adding dozens more reads.

- **Pattern**
  - Re-fetching PR metadata already available earlier in the run; high-multiplier commit check-run polling.

- **Concrete change**
  - Build a single PR metadata bundle early and pass it through outputs/artifacts.
  - Poll only the specific check runs needed, or stop polling once terminal checks are known.

- **Estimated reduction**
  - **4-8 reads removed per run** immediately, plus larger savings from trimming check-run loops.

- **Rate-limit risk reduction**
  - Moderate-to-high on busy PRs.

#### 3. `copilot_pull_request_reviewer` artifact re-listing
- **Evidence**
  - Recent runs `25413153151`, `25415113154`, `25416236385` all call `gh api /repos/shubhodeep1/coding-workflows/actions/runs/<run_id>/artifacts`.
  - Prepare also paginates `pulls.listFiles`.

- **Pattern**
  - Artifact IDs are discovered again in a later cleanup job instead of being reused from the producer path.

- **Concrete change**
  - Reuse artifact IDs via outputs, or clean up inline in the producer job.

- **Estimated reduction**
  - **1 API read + one extra runner-backed job** removed per Copilot run.

- **Rate-limit risk reduction**
  - Small individually, meaningful at scale.

#### 4. Unconditional `/rate_limit` probes in retry wrappers
- **Evidence**
  - `cancel_on_pr_close` recent run `25416919500` shows `_reset_ts=$(gh api -i /rate_limit ...)` inside retry plumbing.
  - Workflow-log-analysis runs repeatedly call this out as an API hygiene issue.

- **Pattern**
  - Reading `/rate_limit` preemptively instead of only after a real 403/429/secondary-rate-limit response.

- **Concrete change**
  - Call `/rate_limit` only after a detected throttle response or on retry attempt 2+.

- **Estimated reduction**
  - **1 read removed per retry path** across multiple workflows.

- **Rate-limit risk reduction**
  - Small direct reduction; larger benefit is lower background noise.

### Repository API hygiene alignment

The repository’s own workflow-log-analysis prompts explicitly bind to a “GitHub API Call Hygiene” policy. The main observed violations in this window are:

- repeated poll/read loops instead of run-ID targeting,
- re-fetching data already available earlier in the same workflow,
- extra artifact list calls in late cleanup phases,
- inconsistent use of retry wrappers and `retries: 0` on API-heavy paths.

## Prompt Cache & Memory System

- **Prompt cache status**
  - Cache is nominally enabled (`OPENROUTER_PROMPT_CACHE_DISABLED: false`) in sampled `review_autofix` and recent workflow logs.
  - However, the only visible cache probe lines in failed/slow reviews report all token fields as `na`.

- **Observed cache behavior**
  - No trustworthy cache hit/miss accounting is available in this window.
  - That means the system cannot currently distinguish:
    - cache disabled,
    - cache fragmented,
    - cache hit but not logged,
    - cache unsupported on the underlying provider/model path.

- **Likely cache-fragmentation causes**
  - Large dynamic prompt variance early in prompts:
    - run IDs,
    - timestamps,
    - branch names,
    - PR-specific diff metadata,
    - injected support text / telemetry notes.
  - Repeated inclusion of long static instructions instead of reusing stable prompt prefixes.

- **Memory retrieval behavior**
  - Retrieval is effectively cold: 13 retrieves, 0 hits.
  - Since all `keyword_method` values are `none`, memory selection is not even reaching meaningful keyword narrowing in sampled review flows.

- **Concrete improvements**
  1. **Stabilize cache prefix**
     - Put long-lived system instructions and reusable repo guidance first.
     - Move run-specific metadata to a suffix or sidecar file.
  2. **Emit real cache counters**
     - Log cache create/read token counters per model call.
  3. **Add memory retrieval debugging**
     - Log why retrieval chose `keyword_method: none`.
  4. **Reuse already-built support context**
     - Avoid reconstructing large static prompt context separately in multiple stages of the same workflow.
  5. **Track cache success by workflow family**
     - At minimum for `review_autofix`, `workflow_log_analysis`, and `test_and_mark_stable`.

- **Estimated impact**
  - **Tokens:** likely 10-20% savings on repetitive AI-heavy workflows once cache effectiveness is restored/measured.
  - **Latency:** moderate improvement from lower prompt-processing overhead.
  - **Reliability:** better observability prevents blind optimization.

## Orchestrator Health

- **Overall flow health**
  - Clarify/plan/implement/respond families are mostly healthy from a logic perspective: they are usually skipped quickly rather than hanging.
  - Family medians:
    - `clarify`: p50 **1s**
    - `plan`: p50 **1s**
    - `implement`: p50 **1s**
    - `orchestrate_clarify_respond`: p50 **1s**

- **Recurring operational pain points**
  1. **Poller starvation**
     - `orchestrate_poll` failures at 903s are infrastructure/queue symptoms, not logic failures.
  2. **Long-running review waves on stale PR heads**
     - Review work continues after the PR is effectively obsolete.
  3. **Comment-only review path still behaves like a full heavy workflow**
     - Operationally healthy, but inefficient.
  4. **Sparse state telemetry on some recent runs**
     - Some recent logs show only run-level summaries, with no detailed memory/cache/token metrics.

- **Observable indicators to track**
  - `orchestrate_poll`:
    - failure rate,
    - queue wait >300s,
    - `has_work=false` runs still consuming >45s.
  - `review_autofix`:
    - cancellation after `codex-agent` start,
    - comment-only runs >600s,
    - conflict-resolver failures by file path.
  - `test_and_mark_stable`:
    - per-phase poll iteration count,
    - Phase 4b/Phase 7 timeout frequency.
  - Memory:
    - retrieve hit rate,
    - `keyword_method` mix,
    - cache metric coverage rate.

- **Smallest safe mitigations**
  - Add earlier stale-run exits.
  - Add poller concurrency.
  - Separate comment-only reviews from full autofix reviews.
  - Improve telemetry completeness before changing core orchestration logic.

## Pipeline Flow Bottlenecks

### 1. Queueing overhead
- **Dominant in**
  - `orchestrate_poll` failures (`25381014761`, `25383797907`)
  - many CI runs
  - many Copilot review runs
  - parts of `review_autofix`

- **Impact**
  - Causes outright failures for poller.
  - Adds 10-90s to many successful runs.

- **Fix order**
  1. Concurrency/jitter on pollers
  2. Collapse multi-job workflows where possible
  3. Early stale-run cancellation

### 2. Review/autofix compute overhead
- **Dominant in**
  - `review_autofix` long runs and cancellations
  - especially comment-only `claude/*` branch review flows

- **Impact**
  - Biggest aggregate runtime and AI spend sink in the sampled window.

- **Fix order**
  1. comment-only short-circuit
  2. concurrency cancellation before codex-agent
  3. reduced reviewer profile for low-diff comment-only runs

### 3. Release-validation retry/poll overhead
- **Dominant in**
  - `test_and_mark_stable`

- **Impact**
  - 50+ minute workflows with failures late in the run.

- **Fix order**
  1. persist run IDs
  2. widen backoff
  3. stop broad state polling once the relevant run is known

### 4. Merge/conflict overhead
- **Dominant in**
  - failed `review_autofix` runs on generated canary conflicts

- **Impact**
  - Converts otherwise recoverable review flows into hard failures.

- **Fix order**
  1. deterministic canary merge handling
  2. fail-open artifact preservation if conflict resolver cannot clear generated files

### 5. CI serial test critical path
- **Dominant in**
  - `ci` `lint`

- **Impact**
  - Consistent ~10 minute baseline on every CI run.

- **Fix order**
  1. shard heavy suite
  2. cache tool downloads
  3. only then consider finer-grained test selection

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

**Top bottlenecks**
- `review_autofix` long-running comment-only reviews and stale cancellations
- `test_and_mark_stable` long poll/retry loops
- `ci` serial heavy test suite
- Copilot review multi-job queueing and artifact churn

**Top failure modes**
- Conflict resolver failure on `tests/e2e_smoke_canary.txt` in `review_autofix`
- `orchestrate_poll` runner-starvation failures at 903s
- Copilot review missing `github-token`
- Nightly self-test failing with insufficient inline diagnostics

**Highest-cost drivers**
- Multi-model `review_autofix` reviewer panels on comment-only paths
- Repeated cancelled `review_autofix` runs after expensive work already began
- Long release-validation polling windows
- Unmeasurable prompt cache preventing token optimization

**Top 3 prioritized actions**
1. **Add early concurrency + comment-only short-circuit to `review_autofix`**
2. **Deterministically resolve/regenerate `tests/e2e_smoke_canary.txt` instead of using generic conflict resolution**
3. **Refactor `test_and_mark_stable` Phase 4b/7 polling to persist run IDs and widen backoff**

## Metrics Appendix

### Repo-level summary

| Repository | Total runs | Success | Failure | Cancelled | Other/skipped | Failure rate | p50 duration | p95 duration |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 946 | 300 | 9 | 61 | 576 | 0.95% | 2.0s | 653.0s |

### Key workflow-family metrics

| Workflow family | Total runs | Success | Failure | Cancelled | p50 | p95 | Avg duration |
|---|---:|---:|---:|---:|---:|---:|---:|
| `ci` | 71 | 71 | 0 | 0 | 619.0s | 654.5s | 614.8s |
| `review_autofix` | 99 | 38 | 3 | 56 | 54.0s | 1936.2s | 535.9s |
| `test_and_mark_stable` | 4 | 2 | 2 | 0 | 3179.0s | 3759.4s | 3255.3s |
| `orchestrate_poll` | 38 | 36 | 2 | 0 | 51.0s | 310.6s | 108.7s |
| `copilot_pull_request_reviewer` | 28 | 27 | 1 | 0 | 177.0s | 370.6s | 196.4s |
| `nightly_validation_selftest` | 1 | 0 | 1 | 0 | 90.0s | 90.0s | 90.0s |
| `workflow_log_analysis` | 4 | 4 | 0 | 0 | 2749.5s | 3239.8s | 2790.5s |

### Notable run evidence referenced

| Run ID | Workflow family | Conclusion | Duration | Key note |
|---|---|---|---:|---|
| `25413999630` | `review_autofix` | success | 2160s | comment-only path still ran full review |
| `25415476868` | `review_autofix` | success | 1524s | long comment-only review |
| `25411605236` | `review_autofix` | cancelled | 1690s | stale run cancelled after heavy review work |
| `25370025320` | `review_autofix` | failure | 1295s | conflict resolver failure |
| `25370115370` | `review_autofix` | failure | 1836s | conflict resolver failure |
| `25371432937` | `review_autofix` | failure | 637s | conflict resolver failure |
| `25369768571` | `test_and_mark_stable` | failure | 3359s | failed in Phase 4b |
| `25375729485` | `test_and_mark_stable` | failure | 2999s | failed in Phase 7 |
| `25378639747` | `test_and_mark_stable` | success | 3830s | very slow success |
| `25381014761` | `orchestrate_poll` | failure | 903s | runner starvation only |
| `25383797907` | `orchestrate_poll` | failure | 903s | runner starvation only |
| `25389586417` | `copilot_pull_request_reviewer` | failure | 42s | missing `github-token` |
| `25414664546` | `nightly_validation_selftest` | failure | 90s | 2 of 3 fixtures failed |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Total `AI_MEMORY_TELEMETRY` entries observed | 54 |
| `retrieve` operations | 13 |
| Retrieve hit rate (`records_selected > 0`) | 0.0% |
| Avg retrieve `estimated_tokens` | 0.0 |
| `keyword_method=none` | 13/13 retrieves |
| `enabled=false` retrieves | 0 |
| `fail_open=true` retrieves | 0 |
| `record-run-event` ops | 29 |
| `record-candidate` ops | 8 |
| Max observed push attempts | 1 |

### Prompt cache / token observability

| Metric | Status |
|---|---|
| Prompt/completion token totals by workflow family | Not available in provided window |
| Cache read/create token totals | Not available; sampled logs show `na` |
| Cache enabled flag | Present (`OPENROUTER_PROMPT_CACHE_DISABLED: false`) in sampled AI-heavy workflows |
| Concrete token snippet observed | `tokens used = 408` in one conflict-resolver failure path |
| Optimization readiness | Limited by missing reliable token/cache counters |

### GH API hotspot summary

| Workflow / step | Observed pattern | Evidence | Reduction opportunity |
|---|---|---|---|
| `test_and_mark_stable` / Phase 4b, Phase 7 | repeated poll reads of workflow/PR state | deep-dive E2E logs + workflow-log-analysis runs `25375766109`, `25378679803` | 50-80% fewer reads on failing runs |
| `review_autofix` / gate + check polling | repeated PR/commit/files/check-run lookups | workflow-log-analysis run `25405996019` | remove 4-8 baseline reads/run plus poll reductions |
| `copilot_pull_request_reviewer` / Prepare | paginated `pulls.listFiles` | runs `25413153151`, `25415113154`, `25416236385` | reuse outputs / add retries |
| `copilot_pull_request_reviewer` / Cleanup artifacts | `gh api /actions/runs/<id>/artifacts` | same runs | remove 1 API read/run + late cleanup job |
| `cancel_on_pr_close` | unconditional `/rate_limit` probe inside retry plumbing | run `25416919500` | remove unnecessary background read |

