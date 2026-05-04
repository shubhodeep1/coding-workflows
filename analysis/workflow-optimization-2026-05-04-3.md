## Executive Summary

- **The release gate is currently broken, not just slow.** `test_and_mark_stable` failed in **3/3 runs** with p50 **2,474s** and p95 **3,362s**; two runs (`#25300046587`, `#25305535590`) failed at `e2e-smoke-test / Phase 4b: Verify editor removed bait line`, and one (`#25281876234`) failed in `sync-to-main / Dispatch forward-merge-stable-to-main` because `gh workflow run` was executed outside a git repo (`fatal: not a git repository`). **Estimated impact:** recover the whole stable-release path and cut failed-release waste by **25–55 min/run**. **Confidence:** high.

- **`review_autofix` is the biggest latency and spend sink.** Family metrics show p95 **1,714.7s** with **40 cancelled** runs out of 78 total; deep-dive runs `#25305806238`, `#25307413351`, and `#25303629616` spent **13–31 min** mostly in `review / codex-agent`, often on the **Claude-branch comment-only path** where editor/commit/judge are skipped. **Estimated impact:** **6–17 min** saved per affected run and major token reduction if the reviewer fan-out is trimmed on comment-only paths. **Confidence:** high.

- **Queueing and job fan-out are materially inflating end-to-end time.** CI runs are consistently ~**603–648s** (`ci` avg **609.7s**) with repeated hosted-runner waits; `copilot_pull_request_reviewer` run `#25307976290` waited separately for `Prepare`, `Upload results`, and `Cleanup artifacts`; `review_autofix` waits on both `gate` and `codex-agent`. **Estimated impact:** **30–120s/run** for review/copilot paths, plus indirect queue relief for the whole repo. **Confidence:** medium.

- **Prompt-cache instrumentation is enabled but not proving value.** `OPENROUTER_PROMPT_CACHE_DISABLED=false` is present broadly, and cache probes in `review_autofix` (`#25307413351`, `#25303629616`) report `cache_enabled=true`, but `prompt_tokens`, `total_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens` are all `na`, so the pipeline cannot verify real cache hits on expensive reviewer/editor calls. **Estimated impact:** once fixed, likely **10–30% lower latency/tokens** on repetitive review loops; today the gap is observability, not proof of savings. **Confidence:** medium.

- **AI memory works for implement-style paths but is weak for reviewer paths.** Across deep-dive logs there were **23** `retrieve` operations with **60.9% hit rate** and average `estimated_tokens` **17**; implementation/workflow-analysis retrieves usually returned **1 record / 28 tokens**, while reviewer retrieves in `review_autofix` frequently returned **0 records** (`#25300219172`, `#25303629616`, `#25307413351`). **Estimated impact:** better reviewer memory recall should reduce repeated reviewer/editor retries and long comment-only runs. **Confidence:** medium.

- **There are clear high-redundancy GitHub API patterns in release, review, and issue-sync flows.** `test_and_mark_stable #25305535590` repeatedly lists workflow runs, polls run status, re-fetches issues/comments/labels, and performs per-resource cleanup; `review_autofix #25300219172` separately paginates issue comments, PR reviews, review comments, linked issues, and check-runs. **Estimated impact:** **20–40 API calls** removed from release-gate runs and **dozens** from long review runs, lowering rate-limit risk and runner time. **Confidence:** medium.

## Speed Optimizations

### 1. Critical-path: make `test_and_mark_stable` fail fast and deterministic
- **Evidence**
  - Family `test_and_mark_stable`: **3 total / 3 failed**, avg **2,786.3s**.
  - Run `#25305535590` failed after **2,474s** at `Phase 4b: Verify editor removed bait line`.
  - The verification step shows the canary still contained `# E2E_EDITOR_BAIT_25305535590`.
  - Run `#25281876234` failed after **3,461s** in `sync-to-main` because `gh workflow run forward-merge-stable-to-main.yml --ref stable` hit `fatal: not a git repository`.
- **Root cause**
  - Release gate continues to spend tens of minutes before surfacing deterministic failures.
  - One failure is infra-script misuse (`gh workflow run` from a non-repo dir); the other is a brittle smoke-edit assertion.
- **Exact change**
  - In `sync-to-main`, either:
    - add an explicit checkout before dispatch, or
    - invoke `gh workflow run ... --repo shubhodeep1/coding-workflows` from a guaranteed repo root.
  - In smoke Phase 4, tighten the task to a deterministic overwrite of `tests/e2e_smoke_canary.txt` and fail immediately once the post-review canary still contains the bait marker; do not continue unrelated orphan/refresh work once the smoke gate is red.
- **Estimated time savings**
  - **25–55 min per failed release run**.
- **Implementation risk**
  - **Low** for the dispatch fix.
  - **Low-medium** for the smoke-task tightening; keep existing acceptance criteria, only narrow the edit path.

### 2. Critical-path: shrink or short-circuit the Claude-branch comment-only `review_autofix` path
- **Evidence**
  - `review_autofix` p95 **1,714.7s**; **40 cancelled** runs out of 78.
  - Run `#25307413351` was cancelled after **850s**; logs say `AUTOFIX_GATE_CLAUDE_BRANCH_REVIEW ... reviewer panel + comment-only path; editor/commit/judge/auto-merge skipped`.
  - Run `#25305806238` succeeded but took **1,021s**, with ~**9 min runner wait** before `review codex-agent`.
  - Run `#25303629616` took **1,730s** on the same review family.
- **Root cause**
  - The expensive multi-reviewer path is still used when the workflow already knows it is in a comment-only mode.
- **Exact change**
  - Add an early gate for Claude-branch/comment-only reviews:
    - use a smaller reviewer panel (for example 1–2 strongest models),
    - skip consensus/editor scaffolding entirely,
    - and avoid starting a second runner-backed job if the gate already resolves to comment-only output.
- **Estimated time savings**
  - **6–17 min per comment-only review run**.
- **Implementation risk**
  - **Medium**; preserve the full panel for merge-bound PRs and only reduce it on explicitly comment-only paths.

### 3. Critical-path: stop dispatching `implement` when upstream state already guarantees no work
- **Evidence**
  - `implement` family has **155** runs, but **135** are non-success/non-failure control-path runs.
  - Recent run `#25305681142` took **195s** only to conclude `Issue #2060 is not in ai:awaiting-approval phase`.
  - Recent run `#25305678061` took **143s** only to conclude `Issue #2061 is closed`.
- **Root cause**
  - Dispatch/gating is happening late enough that some no-op cases still consume a runner and full setup.
- **Exact change**
  - Push phase/closed-state checks earlier in the orchestrator dispatch logic so `implement` is not started for:
    - closed issues,
    - issues not in `ai:awaiting-approval`,
    - or comments that cannot possibly satisfy the approval path.
- **Estimated time savings**
  - **2–3 min per avoidable implement run**, plus queue relief.
- **Implementation risk**
  - **Low** if the dispatch condition mirrors the existing in-workflow checks.

### 4. Micro-optimization with broad reach: reduce full-history/no-op checkouts in poll/promote/forward-merge paths
- **Evidence**
  - `orchestrate_poll #25307556256`: `Checkout repository` took ~**9.5s** of a **44s** run.
  - `forward_merge_stable_to_main #25308070711`: the run was a no-op but still spent most of its **22s** in the merge-check step.
  - `promote_main_to_stable #25308055219` contains bounded fetch/ls-remote retry loops even on a **30s** success path.
- **Root cause**
  - Full ref discovery/fetch happens before the workflow knows whether there is real work.
- **Exact change**
  - Default to shallow/minimal ref fetch for no-op detection; only deepen/fetch tags/full history once work is confirmed.
  - Reuse fetched refs between adjacent steps instead of repeating fetch logic per step.
- **Estimated time savings**
  - **8–15s** per poll/promote/forward-merge run.
- **Implementation risk**
  - **Low**, as long as release/version steps still deepen before tag-dependent work.

### 5. Micro-optimization: collapse runner-backed jobs in `copilot_pull_request_reviewer`
- **Evidence**
  - `copilot_pull_request_reviewer #25307976290` waited separately for `Prepare`, `Upload results`, and `Cleanup artifacts`; total runtime **191s**.
  - Earlier run `#25305719068` spent **82s**, with `Cleanup artifacts` alone ~**51s**.
- **Root cause**
  - Multiple short runner-backed jobs create repeated queue/startup overhead.
- **Exact change**
  - Combine `Prepare`, artifact upload, and artifact cleanup into fewer jobs where artifact boundaries are not required.
  - Reuse the artifact list between upload and cleanup instead of re-querying.
- **Estimated time savings**
  - **20–90s per run**, depending on queue delay.
- **Implementation risk**
  - **Low-medium**; validate artifact retention semantics before merging jobs.

## Cost Optimizations

### 1. Highest-impact: reduce reviewer fan-out on comment-only `review_autofix` runs
- **Evidence**
  - Comment-only review runs still load the full reviewer stack:
    - `REVIEWER_MODELS: minimax/minimax-m2.5 moonshotai/kimi-k2.5 deepseek/deepseek-v4-pro z-ai/glm-5 qwen/qwen3.6-plus x-ai/grok-4.1-fast`
  - Observed in `#25307413351`, `#25307975779`, `#25307182866`, `#25305806238`.
  - These runs often skip editor/commit/judge/auto-merge but still last **385–1,021s+**.
- **Root cause**
  - Multi-model panel cost is being paid even when the output is only a comment.
- **Exact change**
  - For explicit comment-only/Claude-branch review modes:
    - use 1–2 reviewers,
    - skip consensus synthesis if only one reviewer is used,
    - and reserve the 6-model panel for merge-bound or judge-bound reviews.
- **Estimated savings**
  - **50–80% token/call savings** on those runs; likely the single largest AI-cost reduction in the dataset.
- **Quality-risk notes**
  - **Medium**. Mitigate by keeping the full panel for high-risk PRs and only slimming the path when edits are already disabled.

### 2. Eliminate avoidable implement retries caused by stale line-level context
- **Evidence**
  - `implement #25293966619` failed after **5 attempts**.
  - The log explicitly says: `I’m blocked by a plan/code mismatch: the exact lines targeted in .github/workflows/orchestrate_poll.yml don't match the current file content`.
  - Retry backoff consumed **10s + 20s + 40s + 80s**, plus repeated full prompt regeneration.
- **Root cause**
  - The retry loop reuses stale or brittle edit instructions instead of refreshing context after the first failed patch.
- **Exact change**
  - After the first patch-context miss:
    - re-read the target file snippet,
    - switch from exact-line patching to symbol/section anchored patching,
    - and re-prompt once with refreshed context instead of replaying the full long retry loop.
- **Estimated savings**
  - **150s+ runner time per failed run**, plus avoided repeated model calls.
- **Quality-risk notes**
  - **Low** if only the retry strategy changes and the first-attempt path stays intact.

### 3. Stop paying for release-gate reruns until smoke and dispatch failures are fixed
- **Evidence**
  - All `test_and_mark_stable` runs in-window failed.
  - Each failure consumes **41–58 min** of pipeline time.
- **Root cause**
  - Known deterministic failures are still allowing full release-gate execution.
- **Exact change**
  - Temporarily make the release gate block on a lightweight preflight:
    - verify `sync-to-main` dispatch environment,
    - and verify the smoke edit path on a reduced canary workflow
    - before running the full stable-release suite.
- **Estimated savings**
  - Avoids entire failed release reruns; the biggest dollar saving is simply not rerunning a known-bad flow.
- **Quality-risk notes**
  - **Low** if implemented as an additive preflight, not a replacement for the real gate.

### 4. Convert prompt-cache instrumentation from “enabled in theory” to “measured in production”
- **Evidence**
  - Cache probes in `review_autofix` show `cache_enabled=true`, but token/cache-read fields are `na`.
  - The internal `workflow_log_analysis` runs (`#25281892914` excerpts) explicitly note that current logs do **not** prove real prompt-cache hits on reviewer/editor calls.
- **Root cause**
  - Only probes are instrumented; real expensive calls are not exposing cache-read vs cache-create behavior.
- **Exact change**
  - Emit `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens` for the actual reviewer/editor requests, not just cache probes.
  - Keep static instructions and support prompts as the stable prefix; append dynamic PR/check-run/comment context at the end.
- **Estimated savings**
  - No immediate savings from instrumentation alone, but it unlocks likely **10–30% token/latency reductions** on repeated review loops.
- **Quality-risk notes**
  - **Low**; this is observability-first.

### 5. Lower offline-analysis spend by narrowing `workflow_log_analysis`
- **Evidence**
  - `workflow_log_analysis` avg duration is **2,387s** with runs at **2,046s**, **2,091s**, and **3,024s**.
- **Root cause**
  - The analysis workflow is itself heavy and likely sweeping more logs than needed for every run.
- **Exact change**
  - Restrict deep analysis to:
    - failures,
    - top slow outliers,
    - and a small recent success sample.
  - Skip full re-analysis when no new failed/slow runs exist since the last artifact.
- **Estimated savings**
  - **15–30 min per analysis run**.
- **Quality-risk notes**
  - **Low-medium**; keep a manual “full sweep” option.

## Reliability Improvements

### 1. Fix `sync-to-main` dispatch so the release path can complete
- **Failure evidence**
  - `test_and_mark_stable #25281876234` failed in `sync-to-main / Dispatch forward-merge-stable-to-main`.
  - Step log: `failed to run git: fatal: not a git repository`.
- **Root cause category**
  - Workflow scripting / environment assumption.
- **Exact fix**
  - Run `gh workflow run` from a checked-out repo, or pass `--repo` explicitly and avoid repo-dependent git context.
- **Expected reliability impact**
  - Removes one whole class of release-gate failure; likely recovers **33%** of observed `test_and_mark_stable` failures immediately.
- **Rollback / fail-open**
  - Safe rollback.
  - If dispatch still fails, emit a clear release-preflight failure before the long test suite starts.

### 2. Make the smoke editor test deterministic and single-purpose
- **Failure evidence**
  - `#25305535590` and `#25300046587` both failed in `Phase 4b: Verify editor removed bait line`.
  - In `#25305535590`, the canary file still contained the bait comment after review.
- **Root cause category**
  - Prompt/task ambiguity and brittle editor success criteria.
- **Exact fix**
  - Force the smoke task into a single-file exact-overwrite mode and validate on the PR head immediately after the edit, before longer downstream phases.
  - Keep the current bait-line assertion, but make the implementation prompt more deterministic.
- **Expected reliability impact**
  - Addresses **2 of 3** observed release-gate failures.
- **Rollback / fail-open**
  - Low risk; if needed, keep the old smoke task behind a feature flag for one release cycle.

### 3. Harden `review_autofix` against conflict-resolver exhaustion
- **Failure evidence**
  - `review_autofix #25300219172`:
    - reviewer manifest validation failed on editor attempts 1 and 2,
    - then `MERGE_CONFLICT=true`,
    - `Conflict resolver retry 2/3`,
    - `Conflict resolver retry 3/3`,
    - `Conflict resolver failed after retries`.
  - Slow run `#25303629616` also warned that `integration-sync-conflict-resolver-retry-prelude.txt` was missing from support sources.
- **Root cause category**
  - Merge/conflict recovery + support-file integrity.
- **Exact fix**
  - Fail fast when required conflict-resolver prompt/support files are missing from the checked-out support source.
  - Preserve residual-marker/fingerprint scanning even on empty-output branches so retries are informed by real conflict state.
  - If editor output fails manifest validation twice, skip merge attempt and fall back to human-intervention mode earlier.
- **Expected reliability impact**
  - Reduces long-running failure/cancel loops in the most expensive family.
- **Rollback / fail-open**
  - Use a flag for the earlier human-escalation path; fail-open to comment-only if resolver assets are unavailable.

### 4. Refresh patch context on the first implement mismatch instead of burning all retries
- **Failure evidence**
  - `implement #25293966619` ended with `Codex implement failed after 5 attempts`.
  - Log states the failure was due to line mismatch against current file content.
- **Root cause category**
  - Stale context / retry policy.
- **Exact fix**
  - Add a first-failure branch:
    - detect patch-context mismatch,
    - refresh target-file excerpts,
    - regenerate a narrower retry prompt,
    - and cap repeated identical retries.
- **Expected reliability impact**
  - Should materially reduce `implement` failures and no-op reruns.
- **Rollback / fail-open**
  - Low risk; can fall back to the existing retry loop if refresh logic errors.

### 5. Address future runtime breakage from Node 20 deprecation
- **Failure evidence**
  - `review_autofix #25300219172` emitted the GitHub warning that `actions/cache/*@v4` are still running on Node 20 and Node 24 will become required.
- **Root cause category**
  - Action runtime compatibility.
- **Exact fix**
  - Upgrade or confirm Node 24 compatibility for all cache-related actions; where already safe, set `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` consistently across workflows, not just selected ones.
- **Expected reliability impact**
  - Prevents time-based platform breakage in June/September 2026.
- **Rollback / fail-open**
  - Low; validate in CI before broad rollout.

## AI Memory Health

- **Telemetry coverage**
  - Found **82** structured `AI_MEMORY_TELEMETRY` JSON lines across deep-dive logs.
  - Operation mix:
    - `record-run-event`: **34**
    - `retrieve`: **23**
    - `processed-command-check`: **8**
    - `processed-command-claim`: **8**
    - `record-candidate`: **5**
    - `summarize_unselected_runs`: **4**

- **Retrieve performance**
  - `retrieve` count: **23**
  - **Hit rate:** **60.9%** (`14/23` had `records_selected > 0`)
  - **Average `estimated_tokens`:** **17.0**
  - **Keyword method distribution:**
    - `plain`: **14**
    - `none`: **9**
    - `llm`: **0**
  - **0-record retrieves:** **9**
  - **`fail_open: true`:** **0 observed**
  - **`enabled: false`:** **0 observed**

- **What is working**
  - Implementation-style flows are consistently retrieving small, cheap memory context:
    - `implement #25293966619`, `#25293932552`, `#25293940145`, `#25294005792` each selected **1 record** with `estimated_tokens=28`.
  - Push reliability is mostly good:
    - only **2** telemetry events had `push_attempts > 1`,
    - max observed `push_attempts` was **2**.

- **What is not working**
  - Reviewer memory retrieval is weak:
    - `review_autofix #25300219172`, `#25303629616`, `#25279043495`, `#25278175531`, `#25276795302`, and `#25307413351` all showed reviewer retrieves with `records_selected=0`, `estimated_tokens=0`, `keyword_method=none`.
  - Some review runs also logged memory-helper degradation:
    - `#25303629616`: `memory helper script missing; skipping run-start event`
    - `memory helper script missing; writing fallback reviewer memory context`
    - `memory helper script missing; skipping run-end completion event`
  - CI and some review logs still show no AI-memory telemetry at all, so coverage is uneven.

- **Gap**
  - No explicit retrieval token **budget** field was emitted in these telemetry lines, so `estimated_tokens vs budget` cannot be quantified from the current window.

- **Recommendation**
  - Prioritize reviewer-memory retrieval quality:
    - add stable review-specific keywords,
    - ensure candidate promotion for successful review patterns,
    - and treat missing memory helper scripts as a hard warning in review setup.
  - Also emit an explicit retrieval token budget so future reports can compare actual retrieval size against intended limits.

## GH API Call Audit

### 1. `test_and_mark_stable` has the highest API redundancy
- **Evidence**
  - Deep-dive run `#25305535590` contains repeated patterns across phases:
    - list latest workflow runs before/after dispatch,
    - poll `actions/runs/{id}`,
    - fetch issue labels/comments separately,
    - then cleanup PR/issue/comment/branch individually.
  - This run is visibly API-heavy across `clarify`, `plan`, `implement`, review wait, orphan-workflow checks, and cleanup.
- **Pattern**
  - Unbatched per-phase polling and repeated “find newest run ID” calls.
- **Concrete change**
  - Capture dispatched run IDs directly and reuse them instead of:
    - `.../workflows/<wf>/runs?per_page=1`
    - then `.../workflows/<wf>/runs?per_page=10`
    - then `.../actions/runs/{id}`.
  - Reuse issue/PR payloads across wait and cleanup phases instead of refetching comments/labels/bodies separately.
- **Estimated call-count reduction**
  - **20–40 API calls per release-gate run**.
- **Rate-limit risk reduction**
  - Medium-high; this is the repo’s most API-dense path.

### 2. `review_autofix` repeatedly refetches adjacent PR context
- **Evidence**
  - `review_autofix #25300219172` separately pulls:
    - PR payload,
    - issue comments,
    - PR reviews,
    - PR review comments,
    - linked issues via GraphQL,
    - paginated check-runs.
  - The same run also polls check-runs with a `gh_retry gh api --paginate --slurp` loop governed by `CHECK_RUNS_WAIT_TIMEOUT_SECS=1200` and `CHECK_RUNS_POLL_INTERVAL_SECS=20`.
- **Pattern**
  - Repeated lookups across neighboring steps and a potentially expensive check-run wait loop.
- **Concrete change**
  - Build a single PR context bundle at gate time and pass it forward to reviewer/editor/consolidator steps.
  - Snapshot check-runs once after a bounded wait rather than repeatedly rebuilding the same context in later steps.
- **Estimated call-count reduction**
  - **Dozens of calls** on long review runs.
- **Rate-limit risk reduction**
  - High for the slowest `review_autofix` runs.

### 3. `issue_pr_status` still does per-issue REST work after GraphQL discovery
- **Evidence**
  - `issue_pr_status #25308045567` uses:
    - GraphQL to get linked issues,
    - then REST calls for PR text,
    - then per-linked-issue label/body fetches,
    - then close/edit operations.
- **Pattern**
  - Hybrid GraphQL discovery followed by N-per-issue REST enrichment.
- **Concrete change**
  - Expand the GraphQL query to include the linked issue fields now fetched one-by-one, or persist the first REST payload and reuse it.
- **Estimated call-count reduction**
  - **3–5 calls per PR-close event**.
- **Rate-limit risk reduction**
  - Low-medium, but high leverage because the workflow is frequent.

### 4. `orchestrate_poll` needlessly checks `/rate_limit` in the happy path
- **Evidence**
  - `orchestrate_poll #25307556256` includes an inline retry helper that queries `gh api -i /rate_limit`.
  - No actual rate-limit event was observed in that run.
- **Pattern**
  - Prefetching rate-limit metadata before a failure exists.
- **Concrete change**
  - Only query `/rate_limit` after the first 403/429/rate-limit match; cache the reset timestamp for the remainder of the step.
- **Estimated call-count reduction**
  - **1–2 calls per poll cycle**.
- **Rate-limit risk reduction**
  - Small direct savings, but good hygiene for a frequent workflow.

### 5. `copilot_pull_request_reviewer` duplicates artifact/file metadata retrieval across jobs
- **Evidence**
  - `#25307976290` and `#25305719068` both hit artifact-list endpoints in cleanup/upload flows; `Prepare` also paginates PR files.
- **Pattern**
  - Repeated metadata retrieval across separate runner jobs.
- **Concrete change**
  - Persist artifact IDs and PR file metadata from `Prepare` into step outputs/artifacts for downstream reuse.
- **Estimated call-count reduction**
  - **2–4 calls per run**.
- **Rate-limit risk reduction**
  - Low, but reduces queue-backed overhead too.

### Repo-specific API hygiene alignment
- The repo’s own embedded instructions, echoed in `review_autofix` logs, already say to avoid adding new `gh api` calls unless data cannot be batched or reused. The observed hot paths violate that intent mainly through **re-fetching already-known context**, not through single large calls.

## Prompt Cache & Memory System

- **Observed state**
  - `OPENROUTER_PROMPT_CACHE_DISABLED=false` appears consistently in `implement`, `review_autofix`, and `orchestrate_poll`.
  - `review_autofix` cache probes (`#25307413351`, `#25303629616`, older slow runs) show:
    - `cache_enabled=true`
    - but `prompt_tokens=na`
    - `completion_tokens=na`
    - `total_tokens=na`
    - `cache_creation_input_tokens=na`
    - `cache_read_input_tokens=na`

- **Assessment**
  - Prompt cache is **configured**, but not **verifiably effective**.
  - The current logs can prove cache instrumentation exists; they cannot prove that expensive reviewer/editor requests are actually getting cache hits.

- **Likely cache-fragmentation causes**
  - Dynamic context appears to be large and volatile:
    - PR comments,
    - check-run snapshots,
    - linked-issue context,
    - runtime summaries,
    - and repeated environment/status scaffolding.
  - `review_autofix` explicitly assembles large reviewer/editor prompts and check-run context, which likely destabilizes prompt prefixes if dynamic content appears too early.
  - Some long runs also regenerate prompts multiple times inside retry loops, increasing prompt variance.

- **Concrete improvements**
  1. **Instrument the real calls**
     - Emit cache-read/cache-create metrics on actual reviewer/editor requests.
  2. **Stabilize the prompt prefix**
     - Keep system instructions, repo rules, and reusable support prompts first.
     - Move PR-specific volatile data to the tail of the prompt.
  3. **Reuse pre-assembled static context**
     - The workflow already has steps like `Pre-assemble static context cacheable across runs`; extend that idea so retries reuse the same stable prefix artifact.
  4. **Separate retry nudge from base prompt**
     - On retries, append only a compact delta note instead of reconstructing the whole prompt body.

- **Estimated impact**
  - Once measured and stabilized: likely **10–30% lower tokens and latency** on repetitive review loops.
  - Reliability also improves because cache failure already fails open; the main benefit is reduced cost and shorter long reviews.

- **Memory-system note**
  - Memory retrieval is helping implement-like flows, but reviewer retrieval is underperforming. Improving reviewer memory recall and fixing missing memory-helper scripts should complement cache gains by reducing redundant long-context prompts.

## Orchestrator Health

- **Healthy signals**
  - `orchestrate_poll` is operational:
    - family avg **53.1s**, p50 **45s**, 48/48 successes.
    - Recent run `#25307556256` completed successfully with `poll_completed` telemetry.
  - `clarify`, `plan`, and `orchestrate_clarify_respond` skip quickly when conditions are false; p50 is **1s** across those families.

- **Pain points**
  1. **Control-plane churn**
     - Large volumes of skipped runs:
       - `clarify`: **181 total**, only **20** successes
       - `plan`: **155 total**, only **17** successes
       - `orchestrate_clarify_respond`: **155 total**, only **3** successes
     - Most are cheap, but they add workflow noise and some still wait for runners when upstream gating is late.
  2. **Late “no work” detection**
     - `implement #25305681142` and `#25305678061` consumed minutes only to discover phase/closed-state no-ops.
  3. **Conflict-heal exhaustion in review**
     - `review_autofix #25300219172` exhausted conflict retries and surfaced a terminal failure after a long run.
  4. **Memory-helper partial degradation**
     - Missing helper scripts lead to fallback behavior rather than a crisp operational signal.

- **Smallest safe mitigations**
  - Tighten dispatch rules upstream for closed/non-approvable issues.
  - Add a stronger early exit for comment-only review paths.
  - Turn missing memory-helper support assets into a visible operational warning metric.
  - On repeated conflict-resolver failure, escalate earlier to human intervention instead of burning the full long loop.

- **Observable indicators to track**
  - `review_autofix` cancellation rate.
  - Median and p95 queue time by workflow family.
  - Fraction of `review_autofix` runs entering comment-only mode.
  - `AI_MEMORY_TELEMETRY retrieve` hit rate for `role=reviewer`.
  - Count of runs with `memory helper script missing`.
  - Count of implement runs that end in “no actionable output” retry exhaustion.

## Pipeline Flow Bottlenecks

### Clarify → Plan
- **Bottleneck type:** control-plane noise, not compute.
- **Evidence:** most runs skip in **0–2s**.
- **Fix:** upstream trigger filtering can reduce workflow noise, but this is not the main latency problem.

### Plan → Implement
- **Bottleneck type:** avoidable runner usage and retry overhead.
- **Evidence:** `implement #25293966619` spent **331s** failing after 5 retries; `#25305681142` and `#25305678061` spent **143–195s** on eventual no-op/closed-state exits.
- **Fix order:** tighten dispatch gating first, then refresh stale patch context on first retry.

### Implement → Review/Autofix
- **Bottleneck type:** compute-heavy AI review plus queue delay.
- **Evidence:** `review_autofix` slow runs at **661s**, **850s**, **1,021s**, **1,730s+**; long comment-only Claude-branch runs dominate.
- **Fix order:** shrink comment-only reviewer path, then reduce runner job fan-out.

### Review/Autofix → Validate
- **Bottleneck type:** merge/conflict retry overhead and check-run polling.
- **Evidence:** `#25300219172` hit merge conflict retries and check-run snapshotting logic; `CHECK_RUNS_WAIT_TIMEOUT_SECS=1200` with 20s poll interval creates a potentially expensive wait loop.
- **Fix order:** earlier conflict escalation and PR context reuse before touching polling intervals.

### Validate / Stable Release
- **Bottleneck type:** end-to-end sequential gate with deterministic failures.
- **Evidence:** `test_and_mark_stable` fails after **41–58 min**.
- **Fix order:** fix dispatch bug and smoke determinism before optimizing any lower-priority path.

### Queueing overhead across the pipeline
- **Bottleneck type:** hosted-runner waits.
- **Evidence:** visible in `ci`, `review_autofix`, `copilot_pull_request_reviewer`, `implement`.
- **Fix order:** reduce number of runner-backed jobs and avoid dispatching no-op workflows; no new infrastructure needed.

## Per-Repo Breakdown

### shubhodeep1/coding-workflows

- **Top bottlenecks**
  - `review_autofix` long-running comment-only Claude-branch reviews.
  - `ci` consistently ~10 minutes with runner wait.
  - `test_and_mark_stable` failing after long end-to-end execution.
  - Multi-job `copilot_pull_request_reviewer` queue overhead.

- **Top failure modes**
  - Release smoke gate: editor did not remove bait line (`#25300046587`, `#25305535590`).
  - Release dispatch bug: `gh workflow run` invoked outside git repo (`#25281876234`).
  - `review_autofix` merge conflict resolver exhaustion (`#25300219172`).
  - `implement` stale patch context / no-actionable-output retry exhaustion (`#25293966619`).
  - Nightly validation self-test marking overall status `fail` and failing the workflow (`#25299383150`).

- **Highest-cost drivers**
  - Six-model reviewer fan-out on comment-only review paths.
  - Repeated long retries in `implement` and `review_autofix`.
  - Full release-gate reruns despite deterministic failure modes.
  - Offline `workflow_log_analysis` runs lasting 34–50 minutes.

- **Top 3 prioritized actions**
  1. **Repair `test_and_mark_stable` immediately**: fix `sync-to-main` dispatch context and make smoke canary editing deterministic.
  2. **Slim `review_autofix` comment-only mode**: smaller reviewer panel, earlier gate exit, fewer runner jobs.
  3. **Refresh context on first implement patch miss**: avoid 5-attempt stale retries and stop dispatching implement for known no-op states.

## Metrics Appendix

### Overall repository metrics

| Repo | Total runs | Success | Failure | Cancelled | Other | Failure rate | Avg dur (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| shubhodeep1/coding-workflows | 908 | 263 | 9 | 43 | 593 | 0.99% | 113.2 | 1.0 | 629.0 |

### Key workflow-family metrics

| Workflow family | Total | Success | Failure | Cancelled | Other | Avg dur (s) | p50 (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ci | 54 | 54 | 0 | 0 | 0 | 609.7 | 609.0 | 649.1 |
| review_autofix | 78 | 34 | 1 | 40 | 3 | 467.5 | 46.5 | 1714.7 |
| implement | 155 | 13 | 4 | 3 | 135 | 21.9 | 1.0 | 166.0 |
| test_and_mark_stable | 3 | 0 | 3 | 0 | 0 | 2786.3 | 2474.0 | 3362.3 |
| orchestrate_poll | 48 | 48 | 0 | 0 | 0 | 53.1 | 45.0 | 118.6 |
| plan | 155 | 17 | 0 | 0 | 138 | 16.4 | 1.0 | 140.5 |
| clarify | 181 | 20 | 0 | 0 | 161 | 11.4 | 1.0 | 89.0 |
| issue_pr_status | 12 | 12 | 0 | 0 | 0 | 36.5 | 35.5 | 63.5 |
| copilot_pull_request_reviewer | 23 | 23 | 0 | 0 | 0 | 173.6 | 166.0 | 305.6 |
| workflow_log_analysis | 3 | 3 | 0 | 0 | 0 | 2387.0 | 2091.0 | 2930.7 |

### Notable run outliers used in analysis

| Run ID | Workflow family | Conclusion | Duration (s) | Key bottleneck/failure |
|---|---|---|---:|---|
| 25305535590 | test_and_mark_stable | failure | 2474 | Smoke Phase 4b bait line remained |
| 25300046587 | test_and_mark_stable | failure | 2424 | Same smoke failure mode |
| 25281876234 | test_and_mark_stable | failure | 3461 | `sync-to-main` dispatch outside git repo |
| 25300219172 | review_autofix | failure | 1628 | Conflict resolver failed after retries |
| 25303629616 | review_autofix | success | 1730 | Long Claude-branch review + weak reviewer memory retrieval |
| 25307413351 | review_autofix | cancelled | 850 | Comment-only Claude-branch review consumed full reviewer path |
| 25305806238 | review_autofix | success | 1021 | ~9 min runner wait before codex-agent |
| 25293966619 | implement | failure | 331 | 5 retry attempts; stale patch context |
| 25299383150 | nightly_validation_selftest | failure | 95 | Status marked `overall_status=fail` |

### AI memory telemetry summary

| Metric | Value |
|---|---:|
| Structured telemetry lines | 82 |
| `retrieve` operations | 23 |
| Retrieve hit rate | 60.9% |
| Avg `estimated_tokens` | 17.0 |
| `keyword_method=plain` | 14 |
| `keyword_method=none` | 9 |
| `keyword_method=llm` | 0 |
| 0-record retrieves | 9 |
| `fail_open=true` retrieves | 0 |
| `enabled=false` retrieves | 0 |
| Telemetry events with `push_attempts > 1` | 2 |
| Max observed `push_attempts` | 2 |

### Prompt cache metrics

| Metric | Observed value |
|---|---|
| `OPENROUTER_PROMPT_CACHE_DISABLED` | `false` in implement/review/poll deep dives |
| Real reviewer/editor cache-read token metrics | Not emitted |
| Cache probes present | Yes (`review_autofix_cache_probe`) |
| `cache_enabled=true` on probes | Yes |
| `cache_creation_input_tokens` / `cache_read_input_tokens` | `na` in observed probe logs |
| Dependency cache hits | Observed (`setup-uv` cache hit in prior deep dives) |
| Conclusion | Cache is configured, but actual hit effectiveness is not measurable from current telemetry |

### Token/model usage availability

| Area | Availability |
|---|---|
| Reviewer/editor model names | Present in review/implement/orchestrate logs |
| Prompt/completion/total token counts | Mostly absent / `na` in sampled runs |
| Dollar-accurate spend accounting | Not possible from current window |
| Highest-confidence cost proxy | Duration + model fan-out + reruns |

### Observed GitHub API hotspots

| Workflow family / run | Observed pattern | Optimization target |
|---|---|---|
| `test_and_mark_stable #25305535590` | repeated run listing, run polling, issue/comment/label fetches, cleanup calls | capture and reuse run IDs/payloads |
| `review_autofix #25300219172` | paginated PR comments/reviews/review-comments + linked issues + check-runs | build one reusable PR context bundle |
| `issue_pr_status #25308045567` | GraphQL discovery followed by per-issue REST enrichments | expand GraphQL or cache enrichments |
| `orchestrate_poll #25307556256` | `/rate_limit` helper in retry wrapper | query only after first real rate-limit signal |
| `copilot_pull_request_reviewer #25307976290` | repeated artifact metadata and PR file queries across jobs | persist IDs/metadata between jobs |

If you want, I can turn this into a prioritized implementation checklist mapped to specific workflow files and scripts in this repo.
